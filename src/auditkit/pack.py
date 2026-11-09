"""Load a pack folder: YAML → models, folder → package `packs.<name>`, checks and tools.

The loader stops at the first problem with a `PackError` that names the file. Reporting every issue
at once is the lint's job, built on the step functions below.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from auditkit import generic_checks
from auditkit.checks import Applies, CheckDef, CheckFn, collect
from auditkit.schema import Case, CheckSpec, Hazard, HazardFile, PackConfig, Target
from auditkit.tools import ToolDef

PACK_NAME = re.compile(r"^[a-z][a-z0-9_]*$")  # schema.PackName: importable as packs.<name>
PACKAGE_ROOT = "packs"
REQUIRED = (
    "GroundTruth",
    "Expected",
    "DATA_MODELS",
    "derive_expectations",
    "parse_outcome",
    "simulator_brief",
)
HASH_SKIPPED_TOP_DIRS = {"tests"}  # a pack's own tests: not snapshotted, not hashed
HASH_SKIPPED_DIRS = {"__pycache__"}
HASH_SKIPPED_FILES = {".DS_Store"}


class PackError(Exception):
    """A pack cannot be loaded. The message names the file."""


@dataclass(frozen=True)
class BoundCheck:
    spec: CheckSpec
    fn: CheckFn
    applies: Applies


@dataclass(frozen=True)
class Pack:
    dir: Path
    config: PackConfig
    variant: str
    hazards: dict[str, Hazard]  # the hazard registry (variant exclusions removed)
    cases: dict[str, Case]
    targets: dict[str, Target]
    data: dict[str, BaseModel]
    module: ModuleType  # packs.<name>.pack
    checks: list[BoundCheck]  # the check registry, sorted by id
    tools: dict[str, ToolDef]
    ground_truth: dict[str, BaseModel]  # per case id
    expected: dict[str, BaseModel]  # per case id
    sha256: str


# ── Files ─────────────────────────────────────────────────────────────────────────────────────────


def pack_dir(packs_dir: Path, name: str) -> Path:
    if not PACK_NAME.fullmatch(name):
        raise PackError(f"pack name {name!r} must match {PACK_NAME.pattern}")
    if name == "core":
        raise PackError("pack name 'core' is reserved: it prefixes the core's own checks")
    folder = Path(packs_dir) / name
    if not folder.is_dir():
        raise PackError(f"{folder}: no such pack folder")
    return folder


def pack_files(folder: Path) -> list[Path]:
    """The files of one pack folder that the content hash covers: the only files the loader reads.
    A symlink in the folder is an error: its target could change without changing the hash, and a
    byte copy (the snapshot) would not hash the same."""
    found: list[Path] = []
    for root, dirs, files in os.walk(folder):
        here = Path(root)
        dirs[:] = sorted(
            d
            for d in dirs
            if d not in HASH_SKIPPED_DIRS and not (here == folder and d in HASH_SKIPPED_TOP_DIRS)
        )
        kept = [f for f in files if f not in HASH_SKIPPED_FILES and not f.endswith(".pyc")]
        for entry in (*dirs, *kept):
            if (here / entry).is_symlink():
                raise PackError(f"{here / entry}: a symlink; a pack holds regular files only")
        found += [here / f for f in kept]
    return sorted(found)


def pack_path(folder: Path, base: Path, relative: str) -> Path:
    """`base/relative`, for a path written in a pack file: relative, and inside the pack folder."""
    path = base / relative
    try:
        ok = not Path(relative).is_absolute() and path.resolve().is_relative_to(folder.resolve())
    except (OSError, ValueError):  # e.g. an embedded NUL byte
        ok = False
    if not ok:
        raise PackError(f"{folder}: {relative!r} points outside the pack folder")
    return path


def pack_file(path: Path, allowed: set[Path]) -> Path:
    """`path`, if it is one of the pack's hashed files (`allowed`: resolved `pack_files`). A file
    outside the folder, or where the hash does not look (tests/, __pycache__/…), would escape the
    content hash and the run's snapshot."""
    try:
        ok = path.resolve() in allowed
    except (OSError, ValueError):
        ok = False
    if not ok:
        raise PackError(
            f"{path}: not a file of the pack (outside it, or where its hash does not look)"
        )
    return path


YAML_MERGE_TAG = "tag:yaml.org,2002:merge"  # the `<<` key


class _UniqueKeyLoader(yaml.SafeLoader):
    """`yaml.safe_load`, except that a key given twice in one mapping is an error: "last wins"
    would silently change a case (two `hazards:` keys, one list lost). Merge keys keep their YAML
    meaning: a mapping's own keys override the merged ones; in `<<: [*a, *b]`, `a` wins."""

    def __init__(self, stream: Any) -> None:
        super().__init__(stream)
        self._checked: set[yaml.Node] = set()

    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        # runs before each mapping is built and on each mapping merged into it, while its own keys
        # are still apart from the merged ones. Flattening mixes them (an override then looks like
        # a duplicate), so each node is checked once, before its first flattening.
        if node not in self._checked:
            self._checked.add(node)
            seen: set[Any] = set()
            for key_node, _ in node.value:
                merge = key_node.tag == YAML_MERGE_TAG  # `<<` has no value of its own
                key = "<<" if merge else self.construct_object(key_node)
                try:
                    duplicate = (merge, key) in seen
                except TypeError:  # an unhashable key: construct_mapping reports it
                    continue
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"duplicate key {key!r}", key_node.start_mark
                    )
                seen.add((merge, key))
        super().flatten_mapping(node)


def read_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as f:
            return yaml.load(f, Loader=_UniqueKeyLoader)  # a SafeLoader: no object construction
    except UnicodeDecodeError as e:
        raise PackError(f"{path}: not UTF-8 text ({e.reason} at byte {e.start})") from e
    except OSError as e:
        raise PackError(f"{path}: {e.strerror or e}") from e
    except yaml.YAMLError as e:
        raise PackError(f"{path}: invalid YAML: {e}") from e


def read_model[M: BaseModel](path: Path, model: type[M]) -> M:
    data = read_yaml(path)
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise PackError(f"{path}: {e}") from e
    except Exception as e:  # a pack model's own validator broke: reported, never raised raw
        raise PackError(f"{path}: {type(e).__name__}: {e}") from e


def read_config(packs_dir: Path, name: str) -> PackConfig:
    folder = pack_dir(packs_dir, name)
    config = read_model(folder / "pack.yaml", PackConfig)
    if config.name != name:
        raise PackError(
            f"{folder / 'pack.yaml'}: name {config.name!r} differs from the folder name"
        )
    return config


def dependency_closure(packs_dir: Path, name: str) -> list[str]:
    """`name` and every pack it depends on, dependencies first. A cycle is an error."""
    order: list[str] = []

    def visit(current: str, path: tuple[str, ...]) -> None:
        if current in path:
            raise PackError(f"depends_on cycle: {' → '.join((*path, current))}")
        if current in order:
            return
        for dep in read_config(packs_dir, current).depends_on:
            visit(dep, (*path, current))
        order.append(current)

    visit(name, ())
    return order


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as e:
        raise PackError(f"{path}: {e.strerror or e}") from e


def folders_sha256(packs_dir: Path, names: Sequence[str]) -> str:
    """sha256 over the sorted lines "<path relative to packs_dir>\\t<sha256 of the file>" of the
    hashed files of the named pack folders. Reads bytes only: no file is parsed."""
    packs_dir = Path(packs_dir)
    lines = [
        f"{path.relative_to(packs_dir).as_posix()}\t{hashlib.sha256(_read_bytes(path)).hexdigest()}"
        for current in names
        for path in pack_files(packs_dir / current)
    ]
    return hashlib.sha256("\n".join(sorted(lines)).encode()).hexdigest()


def pack_sha256(packs_dir: Path, name: str) -> str:
    """The content hash of the pack and its dependencies (`folders_sha256`). A pack and its
    byte-copied snapshot hash identically."""
    return folders_sha256(packs_dir, dependency_closure(packs_dir, name))


# ── Code ──────────────────────────────────────────────────────────────────────────────────────────


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """Compiles from the .py file every time: no bytecode is read from, or written to, a pack."""

    def get_code(self, fullname: str):
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _PackFinder(importlib.abc.MetaPathFinder):
    """Resolves every `packs.<name>.…` import, ahead of Python's own finders, to a hashed .py file
    of a registered pack (`pack_files`), or refuses it. So the code that runs is the code the
    content hash covers: no .pyc without its .py, no module under tests/ or __pycache__/, no stale
    bytecode, and no pack the loading pack does not declare in `depends_on`."""

    def __init__(self) -> None:
        self.folders: dict[str, Path] = {}  # pack name → resolved folder
        self.allowed: dict[str, set[Path]] = {}  # pack name → its resolved hashed files

    def find_spec(self, fullname: str, path: object = None, target: object = None):
        parts = fullname.split(".")
        if parts[0] != PACKAGE_ROOT or len(parts) < 3:
            return None  # `packs` and `packs.<name>` are the synthetic packages, already registered
        name = parts[1]
        if name in self.folders:
            base = self.folders[name].joinpath(*parts[2:])
            candidates = ((base / "__init__.py", True), (base.with_name(f"{base.name}.py"), False))
            for file, is_package in candidates:
                if file in self.allowed[name]:
                    return importlib.util.spec_from_file_location(
                        fullname,
                        file,
                        loader=_SourceOnlyLoader(fullname, str(file)),
                        submodule_search_locations=[str(base)] if is_package else None,
                    )
        raise ModuleNotFoundError(
            f"no module named {fullname!r}: a pack imports only its own .py files and those of "
            "its depends_on packs, where the content hash looks",
            name=fullname,
        )


_FINDER = _PackFinder()


def _package(name: str, path: list[str]) -> ModuleType:
    spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
    spec.submodule_search_locations = path
    return importlib.util.module_from_spec(spec)


def register_packages(packs_dir: Path, names: Sequence[str]) -> None:
    """Make each folder the package `packs.<name>`, so a pack's code can use relative imports and
    import its dependencies. Every earlier `packs.*` module is purged first: a snapshot copy can be
    loaded after the original in one process, and only the packs named here can be imported. One
    pack is loaded at a time: a later call replaces the namespace."""
    for key in [k for k in sys.modules if k.startswith(f"{PACKAGE_ROOT}.")]:
        del sys.modules[key]
    _FINDER.folders.clear()
    _FINDER.allowed.clear()
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)
    # a fresh root: `from packs import <name>` must not find a pack left by an earlier load
    root = sys.modules[PACKAGE_ROOT] = _package(PACKAGE_ROOT, [])
    for name in names:
        folder = (Path(packs_dir) / name).resolve()
        _FINDER.folders[name] = folder
        _FINDER.allowed[name] = {f.resolve() for f in pack_files(folder)}
        package = _package(f"{PACKAGE_ROOT}.{name}", [str(folder)])
        sys.modules[package.__name__] = package
        setattr(root, name, package)


def import_pack_module(packs_dir: Path, name: str) -> ModuleType:
    register_packages(packs_dir, dependency_closure(packs_dir, name))
    try:
        module = importlib.import_module(f"{PACKAGE_ROOT}.{name}.pack")
    except Exception as e:  # any error in a pack's code is reported, never raised raw
        raise PackError(f"{Path(packs_dir) / name / 'pack.py'}: {type(e).__name__}: {e}") from e
    if missing := [attr for attr in REQUIRED if not hasattr(module, attr)]:
        raise PackError(f"{Path(packs_dir) / name / 'pack.py'}: missing {', '.join(missing)}")
    return module


def protocol_errors(module: ModuleType) -> list[str]:
    """What is wrong with the shape of the `REQUIRED` names of a module `import_pack_module`
    returned. Shared by the loader (one PackError) and the lint (one L03 each)."""
    errors: list[str] = []
    for attr in ("GroundTruth", "Expected"):
        value = getattr(module, attr)
        if not (isinstance(value, type) and issubclass(value, BaseModel)):
            errors.append(f"{attr} must be a pydantic model class")
    models = module.DATA_MODELS
    if not isinstance(models, Mapping) or not all(
        isinstance(m, type) and issubclass(m, BaseModel) for m in models.values()
    ):
        errors.append("DATA_MODELS must map data names to pydantic model classes")
    for attr in ("derive_expectations", "parse_outcome", "simulator_brief"):
        if not callable(getattr(module, attr)):
            errors.append(f"{attr} must be a function")
    return errors


# ── Checks and tools ──────────────────────────────────────────────────────────────────────────────


def check_spec(
    definition: CheckDef,
    check_id: str,
    hazards: Sequence[str],
    controls: Sequence[str],
    origin: str,
) -> CheckSpec:
    try:
        return CheckSpec(
            id=check_id,
            kind=definition.kind,
            method=definition.method,
            hazards=list(hazards),
            controls=list(controls),
            needs=list(definition.needs),
            description=definition.description,
            origin=origin,
        )
    except ValidationError as e:
        raise PackError(f"check {check_id}: {e}") from e


def bind_checks(
    config: PackConfig, pack_defs: Sequence[CheckDef], core_defs: Sequence[CheckDef]
) -> list[BoundCheck]:
    """The check registry: core validity checks (always on), the core checks `pack.yaml` binds to
    the pack's hazards, and the pack's own checks, prefixed with the pack name."""
    bound: dict[str, BoundCheck] = {}

    def add(definition: CheckDef, spec: CheckSpec) -> None:
        if spec.id in bound:
            raise PackError(f"check {spec.id}: defined twice")
        bound[spec.id] = BoundCheck(spec, definition.fn, definition.applies)

    core_ids = {f"core.{d.id}" for d in core_defs}
    if unknown := sorted(set(config.generic_checks) - core_ids):
        raise PackError(f"pack.yaml generic_checks: no core check {', '.join(unknown)}")
    for d in core_defs:
        check_id = f"core.{d.id}"
        binding = config.generic_checks.get(check_id)
        if d.kind == "validity":
            if binding is not None:
                raise PackError(
                    f"pack.yaml generic_checks: {check_id} is a validity check (always on)"
                )
            add(d, check_spec(d, check_id, (), (), "core"))
        elif binding is not None:
            add(d, check_spec(d, check_id, binding.hazards, binding.controls, "core"))
    for d in pack_defs:
        add(d, check_spec(d, f"{config.name}.{d.id}", d.hazards, d.controls, "pack"))
    return [bound[k] for k in sorted(bound)]


def index_tools(definitions: Sequence[ToolDef]) -> dict[str, ToolDef]:
    return _unique([(t.name, t) for t in definitions], "tool")


# ── The loader ────────────────────────────────────────────────────────────────────────────────────


def _unique[T](items: Sequence[tuple[str, T]], what: str) -> dict[str, T]:
    out: dict[str, T] = {}
    for key, value in items:
        if key in out:
            raise PackError(f"{what} {key}: defined twice")
        out[key] = value
    return out


def load_pack(
    packs_dir: Path,
    name: str,
    *,
    variant: str = "default",
    core_checks: Sequence[CheckDef] | None = None,
) -> Pack:
    """Load `packs_dir/name` for one variant. `core_checks` defaults to the core's own checks
    (`generic_checks.py`); tests pass their own."""
    packs_dir = Path(packs_dir)
    folder = pack_dir(packs_dir, name)

    # 1. pack.yaml and the variant
    config = read_config(packs_dir, name)
    if "default" in config.variants:
        raise PackError(
            f"{folder / 'pack.yaml'}: 'default' is the built-in variant (cases/ + data)"
        )
    if variant == "default":
        cases_dir, data_files, excluded = "cases", dict(config.data), []
    elif variant in config.variants:
        v = config.variants[variant]
        cases_dir, data_files, excluded = v.cases_dir, {**config.data, **v.data}, v.exclude_hazards
    else:
        raise PackError(f"{folder / 'pack.yaml'}: no variant {variant!r}")
    sha256 = pack_sha256(packs_dir, name)  # refuses symlinks, in the pack and its dependencies
    allowed = {p.resolve() for p in pack_files(folder)}  # the only files read below

    # 2-3. dependencies first, then the pack's own code
    module = import_pack_module(packs_dir, name)
    if errors := protocol_errors(module):
        raise PackError(f"{folder / 'pack.py'}: {'; '.join(errors)}")

    # 4. data, hazards, cases, targets, ground truth
    data_models: Mapping[str, type[BaseModel]] = module.DATA_MODELS
    data: dict[str, BaseModel] = {}
    for key, file in sorted(data_files.items()):
        if key not in data_models:
            raise PackError(f"{folder / 'pack.py'}: DATA_MODELS has no model for data {key!r}")
        data[key] = read_model(
            pack_file(pack_path(folder, folder, file), allowed), data_models[key]
        )

    hazard_file = read_model(folder / "hazards.yaml", HazardFile)
    hazards = _unique(
        [(h.id, h) for h in hazard_file.hazards], f"{folder / 'hazards.yaml'}: hazard"
    )
    if unknown := sorted(set(excluded) - set(hazards)):
        raise PackError(f"{folder / 'pack.yaml'}: variant {variant} excludes unknown {unknown}")
    hazards = {k: h for k, h in hazards.items() if k not in excluded}

    cases_path = pack_path(folder, folder, cases_dir)
    if not cases_path.is_dir():
        raise PackError(f"{cases_path}: no such cases folder")
    read = [(p, read_model(pack_file(p, allowed), Case)) for p in sorted(cases_path.glob("*.yaml"))]
    cases = _unique([(c.id, c) for _, c in read], f"{cases_path}: case")
    case_files = {c.id: p for p, c in read}  # errors name the file actually read

    targets_path = folder / "targets"
    targets = _unique(
        [
            (t.name, t)
            for p in sorted(targets_path.glob("*.yaml"))
            for t in [read_model(pack_file(p, allowed), Target)]
        ],
        f"{targets_path}: target",
    )
    for t in targets.values():  # read by the runner: checked here, with the other files
        if t.system_prompt_file is not None:
            pack_file(pack_path(folder, targets_path, t.system_prompt_file), allowed)

    ground_truth: dict[str, BaseModel] = {}
    expected: dict[str, BaseModel] = {}
    for case_id, case in cases.items():
        where = case_files[case_id]
        try:
            ground_truth[case_id] = module.GroundTruth.model_validate(case.ground_truth)
        except ValidationError as e:
            raise PackError(f"{where}: ground_truth: {e}") from e
        except Exception as e:  # the pack model's own validator broke
            raise PackError(f"{where}: ground_truth: {type(e).__name__}: {e}") from e

        # 5. derived expectations (the case model has no field for them: they cannot be typed)
        try:
            expected[case_id] = module.derive_expectations(case, ground_truth[case_id], data)
        except Exception as e:
            raise PackError(f"{where}: derive_expectations: {type(e).__name__}: {e}") from e
        if not isinstance(expected[case_id], module.Expected):
            raise PackError(f"{where}: derive_expectations did not return an Expected")

    # 6. checks and tools
    pack_defs, tool_defs = collect(module)
    core_defs = collect(generic_checks)[0] if core_checks is None else list(core_checks)
    return Pack(
        dir=folder,
        config=config,
        variant=variant,
        hazards=hazards,
        cases=cases,
        targets=targets,
        data=data,
        module=module,
        checks=bind_checks(config, pack_defs, core_defs),
        tools=index_tools(tool_defs),
        ground_truth=ground_truth,
        expected=expected,
        sha256=sha256,
    )
