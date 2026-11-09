"""Loading a pack: the toy pack, discovery and binding, variants, dependencies, reloads, the
content hash, and names or paths that try to leave the pack folder."""

import os
import py_compile
import shutil
import sys
from pathlib import Path

import pytest
import yaml

from auditkit import check, passed
from auditkit.checks import CHECK_ATTR
from auditkit.pack import PackError, load_pack, pack_sha256, read_yaml

FIXTURE_PACKS = Path(__file__).parent / "fixtures" / "packs"


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    """A private copy of the fixture packs: tests mutate it freely."""
    root = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def edit_yaml(path: Path, **changes) -> None:
    data = yaml.safe_load(path.read_text())
    data.update(changes)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


# Fake core checks: the loader is tested apart from the real ones.
@check(id="fake_validity", kind="validity", method="deterministic", description="always on")
def fake_validity(t, ctx):
    return passed()


@check(id="fake_conformance", kind="conformance", method="deterministic", description="bound")
def fake_conformance(t, ctx):
    return passed()


@check(id="fake_unbound", kind="hazard", method="heuristic", description="never bound")
def fake_unbound(t, ctx):
    return passed()


FAKE_CORE = [getattr(f, CHECK_ATTR) for f in (fake_validity, fake_conformance, fake_unbound)]


# ── The toy pack ──────────────────────────────────────────────────────────────────────────────────


def test_toypack_loads_with_its_registries(packs):
    pack = load_pack(packs, "toypack")
    assert pack.config.name == "toypack"
    assert sorted(pack.hazards) == ["H-01", "H-02", "H-03"]
    assert sorted(pack.cases) == ["toy-inject", "toy-open"]
    assert sorted(pack.targets) == ["basic"]
    assert pack.data["catalog"].statuses == ["open", "shipped", "closed"]
    assert pack.ground_truth["toy-open"].status == "open"
    assert pack.module.__name__ == "packs.toypack.pack"


def test_expectations_are_derived_from_ground_truth_and_data(packs):
    pack = load_pack(packs, "toypack")
    assert pack.expected["toy-open"] == pack.module.Expected(status="open", urgent=True)
    assert pack.expected["toy-inject"].urgent is False


def test_pack_checks_are_discovered_and_prefixed(packs):
    pack = load_pack(packs, "toypack")
    assert [c.spec.id for c in pack.checks] == [
        "core.conversation_completed",  # bound in pack.yaml
        "core.max_turns_respected",  # the core's validity checks: always on
        "core.output_schema_valid",
        "core.simulator_output_valid",
        "toypack.asked_about_a_record",
        "toypack.injection_ignored",
        "toypack.other_codes_absent",
        "toypack.record_looked_up",
        "toypack.status_matches",
        "toypack.urgent_mentioned",
    ]
    by_id = {c.spec.id: c.spec for c in pack.checks}
    assert by_id["toypack.status_matches"].hazards == ["H-01"]
    assert by_id["toypack.status_matches"].needs == ["final_outcome"]
    assert by_id["toypack.asked_about_a_record"].kind == "validity"
    assert by_id["core.output_schema_valid"].hazards == ["H-01"]  # the binding's links
    assert by_id["core.output_schema_valid"].controls == ["H-01.C2"]
    assert {c.spec.origin for c in pack.checks if c.spec.id.startswith("toypack.")} == {"pack"}
    assert {by_id[i].origin for i in by_id if i.startswith("core.")} == {"core"}


def test_check_imported_from_a_dependency_takes_the_loading_pack_prefix(packs):
    pack = load_pack(packs, "toypack")
    looked_up = next(c for c in pack.checks if c.spec.id == "toypack.record_looked_up")
    assert looked_up.fn.__module__ == "packs.toybase.checks"
    assert looked_up.spec.origin == "pack"


def test_tools_are_discovered_including_those_imported_from_a_dependency(packs):
    pack = load_pack(packs, "toypack")
    assert sorted(pack.tools) == ["escalate", "lookup_record"]
    assert pack.tools["escalate"].side_effect is True
    assert pack.tools["lookup_record"].fn.__module__ == "packs.toybase.tools"


def test_applies_follows_the_derived_expectations(packs):
    pack = load_pack(packs, "toypack")
    urgent = next(c for c in pack.checks if c.spec.id == "toypack.urgent_mentioned")
    assert urgent.applies(pack.cases["toy-open"], pack.expected["toy-open"]) is True
    assert urgent.applies(pack.cases["toy-inject"], pack.expected["toy-inject"]) is False


@pytest.mark.parametrize("bad_id", ["toypack.x", "x\n", "X", ""])
def test_check_id_that_is_not_bare_is_refused_at_definition(bad_id):
    with pytest.raises(ValueError, match="adds the prefix"):
        check(id=bad_id, kind="validity", method="deterministic", description="d")


# ── Core checks and bindings ──────────────────────────────────────────────────────────────────────


def test_core_validity_checks_always_run_and_others_only_when_bound(packs):
    binding = {"core.fake_conformance": {"hazards": ["H-01"], "controls": ["H-01.C2"]}}
    edit_yaml(packs / "toypack" / "pack.yaml", generic_checks=binding)
    pack = load_pack(packs, "toypack", core_checks=FAKE_CORE)
    by_id = {c.spec.id: c.spec for c in pack.checks}
    assert "core.fake_validity" in by_id and by_id["core.fake_validity"].hazards == []
    assert by_id["core.fake_conformance"].hazards == ["H-01"]
    assert by_id["core.fake_conformance"].controls == ["H-01.C2"]
    assert by_id["core.fake_conformance"].origin == "core"
    assert "core.fake_unbound" not in by_id


def test_binding_an_unknown_core_check_is_an_error(packs):
    edit_yaml(packs / "toypack" / "pack.yaml", generic_checks={"core.nope": {"hazards": ["H-01"]}})
    with pytest.raises(PackError, match=r"no core check core\.nope"):
        load_pack(packs, "toypack", core_checks=FAKE_CORE)


def test_binding_a_core_validity_check_is_an_error(packs):
    binding = {"core.fake_validity": {"hazards": ["H-01"]}}
    edit_yaml(packs / "toypack" / "pack.yaml", generic_checks=binding)
    with pytest.raises(PackError, match="validity check"):
        load_pack(packs, "toypack", core_checks=FAKE_CORE)


def test_pack_hazard_check_without_hazard_is_an_error(packs):
    path = packs / "toypack" / "pack.py"
    path.write_text(path.read_text().replace('hazards=["H-01"],\n    controls=["H-01.C2"],', ""))
    with pytest.raises(PackError, match=r"(?s)toypack\.status_matches.*at least one hazard"):
        load_pack(packs, "toypack")


# ── Variants ──────────────────────────────────────────────────────────────────────────────────────


def test_variant_switches_cases_and_data_and_excludes_hazards(packs):
    pack = load_pack(packs, "toypack", variant="alt")
    assert pack.variant == "alt"
    assert sorted(pack.cases) == ["toy-alt"]
    assert pack.data["catalog"].statuses == ["pending", "done"]
    assert sorted(pack.hazards) == ["H-01", "H-02"]
    assert pack.expected["toy-alt"].urgent is True


def test_variant_named_default_is_an_error(packs):
    edit_yaml(packs / "toypack" / "pack.yaml", variants={"default": {"cases_dir": "cases-alt"}})
    with pytest.raises(PackError, match="built-in variant"):
        load_pack(packs, "toypack")


def test_unknown_variant_is_an_error(packs):
    with pytest.raises(PackError, match="no variant 'nope'"):
        load_pack(packs, "toypack", variant="nope")


# ── Dependencies and sibling modules ──────────────────────────────────────────────────────────────


def test_dependency_and_sibling_modules_are_importable(packs):
    pack = load_pack(packs, "toypack")
    assert pack.module.normalise("  A  B ") == "a b"
    assert sys.modules["packs.toypack.helpers"].__file__.startswith(str(packs.resolve()))
    assert "packs.toybase.tools" in sys.modules


def test_depends_on_cycle_is_an_error(packs):
    edit_yaml(packs / "toybase" / "pack.yaml", depends_on=["toypack"])
    with pytest.raises(PackError, match="cycle"):
        load_pack(packs, "toypack")


def test_missing_dependency_is_an_error(packs):
    edit_yaml(packs / "toypack" / "pack.yaml", depends_on=["absent"])
    with pytest.raises(PackError, match="no such pack folder"):
        load_pack(packs, "toypack")


def test_reload_purges_earlier_pack_modules(packs):
    load_pack(packs, "toypack")
    helpers = packs / "toypack" / "helpers.py"
    helpers.write_text(helpers.read_text().replace("VERSION = 1", "VERSION = 22"))
    load_pack(packs, "toypack")
    assert sys.modules["packs.toypack.helpers"].VERSION == 22


def test_a_copy_loaded_after_the_original_runs_its_own_code(packs, tmp_path):
    copy = tmp_path / "snapshot"
    shutil.copytree(packs, copy)
    load_pack(packs, "toypack")
    pack = load_pack(copy, "toypack")
    assert pack.module.__file__.startswith(str(copy.resolve()))
    assert pack.tools["lookup_record"].fn.__code__.co_filename.startswith(str(copy.resolve()))


# ── Only hashed source runs ───────────────────────────────────────────────────────────────────────


def test_pyc_without_its_source_is_not_imported(packs):
    folder = packs / "toypack"
    py_compile.compile(str(folder / "helpers.py"), cfile=str(folder / "helpers.pyc"))
    (folder / "helpers.py").unlink()
    with pytest.raises(PackError, match=r"no module named 'packs\.toypack\.helpers'"):
        load_pack(packs, "toypack")


def test_pack_pyc_without_pack_py_is_not_imported(packs):
    folder = packs / "toypack"
    py_compile.compile(str(folder / "pack.py"), cfile=str(folder / "pack.pyc"))
    (folder / "pack.py").unlink()
    with pytest.raises(PackError, match=r"no module named 'packs\.toypack\.pack'"):
        load_pack(packs, "toypack")


@pytest.mark.parametrize("skipped", ["tests", "__pycache__"])
def test_module_where_the_hash_does_not_look_is_not_imported(packs, skipped):
    folder = packs / "toypack"
    (folder / skipped).mkdir(exist_ok=True)
    (folder / skipped / "__init__.py").write_text("")
    (folder / skipped / "secret.py").write_text("FACTOR = 42\n")
    pack_py = folder / "pack.py"
    pack_py.write_text(pack_py.read_text() + f"\nfrom .{skipped}.secret import FACTOR\n")
    with pytest.raises(PackError, match=rf"no module named 'packs\.toypack\.{skipped}'"):
        load_pack(packs, "toypack")


def test_subpackage_needs_its_init_file(packs):
    folder = packs / "toypack"
    (folder / "lib").mkdir()
    (folder / "lib" / "m.py").write_text("X = 7\n")
    pack_py = folder / "pack.py"
    pack_py.write_text(pack_py.read_text() + "\nfrom .lib.m import X\n")
    with pytest.raises(PackError, match=r"no module named 'packs\.toypack\.lib'"):
        load_pack(packs, "toypack")
    (folder / "lib" / "__init__.py").write_text("")
    assert load_pack(packs, "toypack").module.X == 7


def test_edit_that_keeps_size_and_mtime_runs_the_new_source(packs):
    helpers = packs / "toypack" / "helpers.py"
    load_pack(packs, "toypack")
    stat = helpers.stat()
    helpers.write_text(helpers.read_text().replace("VERSION = 1", "VERSION = 2"))
    os.utime(helpers, ns=(stat.st_atime_ns, stat.st_mtime_ns))  # what bytecode caches compare
    load_pack(packs, "toypack")
    assert sys.modules["packs.toypack.helpers"].VERSION == 2


def test_loading_writes_no_bytecode_into_packs(packs):
    load_pack(packs, "toypack")
    assert not list(packs.rglob("__pycache__")), list(packs.rglob("__pycache__"))


def test_pack_cannot_reach_a_pack_left_by_an_earlier_load(packs, tmp_path):
    load_pack(packs, "toypack")  # packs.toybase is now registered
    second = tmp_path / "second"
    shutil.copytree(packs / "toypack", second / "toypack")  # without toybase
    edit_yaml(second / "toypack" / "pack.yaml", depends_on=[])
    pack_py = second / "toypack" / "pack.py"
    source = pack_py.read_text().replace(
        "from packs.toybase.checks import record_looked_up  # a check from the depends_on pack\n"
        "from packs.toybase.tools import lookup_record  # a tool from the depends_on pack\n",
        "from packs import toybase\n",
    )
    pack_py.write_text(source + "\nlookup_record = toybase.tools.lookup_record\n")
    with pytest.raises(PackError, match="cannot import name 'toybase'"):
        load_pack(second, "toypack")


def test_pack_cannot_import_a_pack_it_does_not_declare(packs):
    load_pack(packs, "toypack")  # packs.toybase is now registered
    edit_yaml(packs / "toypack" / "pack.yaml", depends_on=[])
    with pytest.raises(PackError, match=r"(?i)no module named 'packs\.toybase"):
        load_pack(packs, "toypack")


# ── Errors in the pack's own files ────────────────────────────────────────────────────────────────


def test_error_in_pack_code_names_the_file(packs):
    (packs / "toypack" / "pack.py").write_text("def broken(:\n")
    with pytest.raises(PackError, match=r"toypack/pack\.py: SyntaxError"):
        load_pack(packs, "toypack")


def test_missing_protocol_name_is_an_error(packs):
    path = packs / "toypack" / "pack.py"
    path.write_text(path.read_text().replace("def simulator_brief(", "def other_brief("))
    with pytest.raises(PackError, match="missing simulator_brief"):
        load_pack(packs, "toypack")


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ('DATA_MODELS = ["catalog"]', "DATA_MODELS must map data names to pydantic model classes"),
        ("GroundTruth = dict", "GroundTruth must be a pydantic model class"),
        ('simulator_brief = "brief"', "simulator_brief must be a function"),
    ],
)
def test_malformed_protocol_name_is_an_error_naming_pack_py(packs, line, message):
    path = packs / "toypack" / "pack.py"
    path.write_text(path.read_text() + f"\n{line}\n")  # rebinds the name, after its definition
    with pytest.raises(PackError, match=rf"toypack/pack\.py: {message}$"):
        load_pack(packs, "toypack")


def test_invalid_ground_truth_names_the_case_file(packs):
    edit_yaml(packs / "toypack" / "cases" / "toy-open.yaml", ground_truth={"record": "R-1"})
    with pytest.raises(PackError, match=r"toy-open\.yaml: ground_truth"):
        load_pack(packs, "toypack")


@pytest.mark.parametrize(
    ("model", "body", "where"),
    [
        (
            "GroundTruth",
            'return {**v, "record": v["rec"]}',
            r"toy-\w+\.yaml: ground_truth: KeyError",
        ),
        ("Catalog", "raise TypeError('boom')", r"catalog\.yaml: TypeError: boom"),
    ],
)
def test_pack_model_validator_that_raises_names_the_file(packs, model, body, where):
    path = packs / "toypack" / "pack.py"
    source = path.read_text().replace(
        "from pydantic import BaseModel, ConfigDict",
        "from pydantic import BaseModel, ConfigDict, model_validator",
    )
    source = source.replace(
        f"class {model}(Strict):\n",
        f"class {model}(Strict):\n    @model_validator(mode='before')\n    @classmethod\n"
        f"    def _broken(cls, v):\n        {body}\n\n",
    )
    path.write_text(source)
    with pytest.raises(PackError, match=where):
        load_pack(packs, "toypack")


def test_failing_derive_expectations_names_the_case_file(packs):
    edit_yaml(
        packs / "toypack" / "cases" / "toy-open.yaml",
        ground_truth={"record": "R-1", "status": "lost"},
    )
    with pytest.raises(PackError, match=r"toy-open\.yaml: derive_expectations.*not in the catalog"):
        load_pack(packs, "toypack")


def test_yaml_file_that_is_not_utf8_names_the_file(packs):
    (packs / "toypack" / "cases" / "toy-open.yaml").write_bytes(b"\xff\xfeid: x\n")
    with pytest.raises(PackError, match=r"toy-open\.yaml: not UTF-8"):
        load_pack(packs, "toypack")


def test_case_error_names_the_file_actually_read(packs):
    cases = packs / "toypack" / "cases"
    (cases / "toy-open.yaml").rename(cases / "renamed.yaml")
    edit_yaml(cases / "renamed.yaml", ground_truth={"record": "R-1"})
    with pytest.raises(PackError, match=r"renamed\.yaml: ground_truth"):
        load_pack(packs, "toypack")


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads a file chmod 0 denies"
)
def test_unreadable_file_names_the_file(packs):
    catalog = packs / "toypack" / "catalog.yaml"
    catalog.chmod(0)
    try:
        with pytest.raises(PackError, match=r"catalog\.yaml: Permission denied"):
            load_pack(packs, "toypack")
    finally:
        catalog.chmod(0o644)


def test_duplicate_yaml_key_is_an_error(packs):
    case = packs / "toypack" / "cases" / "toy-open.yaml"
    case.write_text(case.read_text() + "hazards: [H-02]\n")  # a second hazards: key
    with pytest.raises(PackError, match=r"toy-open\.yaml: invalid YAML.*duplicate key 'hazards'"):
        load_pack(packs, "toypack")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a: &x {b: 1}\nc: {<<: *x, d: 2}", {"a": {"b": 1}, "c": {"b": 1, "d": 2}}),
        # own keys override merged ones; of several merged mappings, the earlier wins
        (
            "a: &a {k: a, m: a}\nb: &b {k: b, n: b}\nc: {<<: [*a, *b], m: c}",
            {
                "a": {"k": "a", "m": "a"},
                "b": {"k": "b", "n": "b"},
                "c": {"k": "a", "m": "c", "n": "b"},
            },
        ),
        # a merged mapping that merges in turn, and overrides what it merges
        (
            "a: &a {x: 1}\nb: &b {<<: *a, x: 2, y: 2}\nc: {<<: *b, z: 3}",
            {"a": {"x": 1}, "b": {"x": 2, "y": 2}, "c": {"x": 2, "y": 2, "z": 3}},
        ),
        # the same, built after the mapping that merges it (a list item is built last)
        (
            "a: &a {k: 1}\nl: [&b {<<: *a, k: 2}]\nc: {<<: *b}",
            {"a": {"k": 1}, "l": [{"k": 2}], "c": {"k": 2}},
        ),
    ],
)
def test_yaml_merge_key_with_yaml_semantics_returns_the_merged_mapping(tmp_path, text, expected):
    path = tmp_path / "f.yaml"
    path.write_text(text)
    assert read_yaml(path) == expected


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("a: &x {b: 1}\nc: {<<: *x, d: 2, d: 3}", "'d'"),  # an own key, next to a merge
        ("a: &x {b: 1, b: 2}\nc: {<<: *x}", "'b'"),  # a key of the merged mapping
        ("a: &x {b: 1}\nc: {<<: *x, <<: *x}", "'<<'"),  # two merges: a list says which wins
    ],
)
def test_yaml_key_given_twice_next_to_a_merge_key_is_an_error(tmp_path, text, key):
    path = tmp_path / "f.yaml"
    path.write_text(text)
    with pytest.raises(PackError, match=rf"f\.yaml: invalid YAML.*duplicate key {key}"):
        read_yaml(path)


def test_pack_named_core_is_refused(packs):
    shutil.copytree(packs / "toypack", packs / "core")
    edit_yaml(packs / "core" / "pack.yaml", name="core")
    with pytest.raises(PackError, match="reserved"):
        load_pack(packs, "core")


def test_duplicate_case_id_is_an_error(packs):
    cases = packs / "toypack" / "cases"
    shutil.copy(cases / "toy-open.yaml", cases / "toy-open-copy.yaml")
    with pytest.raises(PackError, match="case toy-open: defined twice"):
        load_pack(packs, "toypack")


def test_folder_name_differing_from_pack_name_is_an_error(packs):
    (packs / "toypack").rename(packs / "otherpack")
    with pytest.raises(PackError, match="differs from the folder name"):
        load_pack(packs, "otherpack")


# ── Content hash ──────────────────────────────────────────────────────────────────────────────────


def test_hash_is_identical_for_identical_bytes(packs, tmp_path):
    other = tmp_path / "elsewhere"
    shutil.copytree(packs, other)
    assert pack_sha256(packs, "toypack") == pack_sha256(other, "toypack")


def test_hash_of_pack_equals_hash_of_its_snapshot(packs, tmp_path):
    snapshot = tmp_path / "runs" / "2026-11-08_101502" / "snapshot"
    for name in ("toypack", "toybase"):  # the pack and its dependency, byte-copied
        shutil.copytree(packs / name, snapshot / name)
    assert load_pack(snapshot, "toypack").sha256 == load_pack(packs, "toypack").sha256


def test_hash_changes_on_a_one_byte_edit(packs):
    before = pack_sha256(packs, "toypack")
    case = packs / "toypack" / "cases" / "toy-open.yaml"
    case.write_bytes(case.read_bytes() + b" ")
    assert pack_sha256(packs, "toypack") != before


def test_hash_changes_when_a_dependency_changes(packs):
    before = pack_sha256(packs, "toypack")
    tools = packs / "toybase" / "tools.py"
    tools.write_bytes(tools.read_bytes() + b"\n")
    assert pack_sha256(packs, "toypack") != before


def test_hash_ignores_tests_caches_and_finder_files(packs):
    before = pack_sha256(packs, "toypack")
    folder = packs / "toypack"
    (folder / "tests").mkdir()
    (folder / "tests" / "test_x.py").write_text("x = 1\n")
    (folder / "__pycache__").mkdir(exist_ok=True)
    (folder / "__pycache__" / "pack.cpython-312.pyc").write_bytes(b"\0")
    (folder / "stray.pyc").write_bytes(b"\0")
    (folder / ".DS_Store").write_bytes(b"\0")
    assert pack_sha256(packs, "toypack") == before


def test_hash_counts_a_nested_tests_folder(packs):
    before = pack_sha256(packs, "toypack")
    (packs / "toypack" / "cases" / "tests").mkdir()
    (packs / "toypack" / "cases" / "tests" / "note.txt").write_text("data, not the pack's tests\n")
    assert pack_sha256(packs, "toypack") != before


# ── Names and paths that try to leave the pack folder ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["../toypack", "toy/pack", "a.b", "Toypack", "toy-pack", "", "toypack\n"]
)
def test_pack_name_that_is_not_an_identifier_is_refused(packs, name):
    with pytest.raises(PackError, match="must match"):
        load_pack(packs, name)


@pytest.mark.parametrize("path", ["../outside.yaml", "../../outside.yaml", "/etc/hosts", "a\0b"])
def test_data_file_outside_the_pack_is_refused(packs, path):
    (packs / "outside.yaml").write_text("statuses: [open]\n")
    edit_yaml(packs / "toypack" / "pack.yaml", data={"catalog": path})
    with pytest.raises(PackError, match="outside the pack folder"):
        load_pack(packs, "toypack")


@pytest.mark.parametrize(
    "path", ["tests/catalog.yaml", "__pycache__/catalog.yaml", "catalog.pyc", ".DS_Store"]
)
def test_data_file_where_the_hash_does_not_look_is_refused(packs, path):
    folder = packs / "toypack"
    (folder / path).parent.mkdir(exist_ok=True)
    shutil.copy(folder / "catalog.yaml", folder / path)
    edit_yaml(folder / "pack.yaml", data={"catalog": path})
    with pytest.raises(PackError, match="not a file of the pack"):
        load_pack(packs, "toypack")


@pytest.mark.parametrize("cases_dir", ["tests/cases", "Tests/cases"])
def test_variant_cases_dir_where_the_hash_does_not_look_is_refused(packs, cases_dir):
    # "Tests/cases" also reaches tests/ on a case-insensitive file system (macOS)
    shutil.copytree(packs / "toypack" / "cases", packs / "toypack" / "tests" / "cases")
    edit_yaml(packs / "toypack" / "pack.yaml", variants={"alt": {"cases_dir": cases_dir}})
    with pytest.raises(PackError, match=r"not a file of the pack|no such cases folder"):
        load_pack(packs, "toypack", variant="alt")


@pytest.mark.parametrize("cases_dir", ["../elsewhere", "/tmp"])
def test_variant_cases_dir_outside_the_pack_is_refused(packs, cases_dir):
    shutil.copytree(packs / "toypack" / "cases", packs / "elsewhere")
    edit_yaml(packs / "toypack" / "pack.yaml", variants={"alt": {"cases_dir": cases_dir}})
    with pytest.raises(PackError, match="outside the pack folder"):
        load_pack(packs, "toypack", variant="alt")


@pytest.mark.parametrize(
    "prompt", ["/etc/hosts", "../../outside.md", "../../../../../../etc/hosts", "../tests/p.md"]
)
def test_system_prompt_file_outside_the_hashed_files_is_refused(packs, prompt):
    (packs / "outside.md").write_text("You are someone else.\n")
    (packs / "toypack" / "tests").mkdir()
    (packs / "toypack" / "tests" / "p.md").write_text("Hidden prompt.\n")
    edit_yaml(packs / "toypack" / "targets" / "basic.yaml", system_prompt_file=prompt)
    with pytest.raises(PackError, match=r"outside the pack folder|not a file of the pack"):
        load_pack(packs, "toypack")


def test_missing_system_prompt_file_is_an_error(packs):
    edit_yaml(packs / "toypack" / "targets" / "basic.yaml", system_prompt_file="absent.md")
    with pytest.raises(PackError, match="not a file of the pack"):
        load_pack(packs, "toypack")


# A symlink anywhere in a pack is refused: its target can change without changing the hash, and a
# byte copy (the snapshot) would not hash the same.


def symlink_cases(packs: Path) -> None:  # cases/toy-open.yaml → a file outside the pack
    outside = packs / "outside-case.yaml"
    shutil.copy(packs / "toypack" / "cases" / "toy-open.yaml", outside)
    (packs / "toypack" / "cases" / "toy-open.yaml").unlink()
    (packs / "toypack" / "cases" / "toy-open.yaml").symlink_to(outside)


def symlink_data(packs: Path) -> None:  # the data file → a file outside the pack
    (packs / "outside.yaml").write_text("statuses: [open]\n")
    (packs / "toypack" / "linked.yaml").symlink_to(packs / "outside.yaml")
    edit_yaml(packs / "toypack" / "pack.yaml", data={"catalog": "linked.yaml"})


def symlink_code_dir(packs: Path) -> None:  # a code folder → outside the pack
    (packs / "extlib").mkdir()
    (packs / "extlib" / "m.py").write_text("X = 1\n")
    (packs / "toypack" / "lib").symlink_to(packs / "extlib")


def symlink_inner_dir(packs: Path) -> None:  # a folder → another folder of the same pack
    (packs / "toypack" / "cases-link").symlink_to(packs / "toypack" / "cases")


def symlink_cases_dir(packs: Path) -> None:  # a variant's cases_dir → outside the pack
    shutil.copytree(packs / "toypack" / "cases", packs / "elsewhere")
    (packs / "toypack" / "linked-cases").symlink_to(packs / "elsewhere")
    edit_yaml(packs / "toypack" / "pack.yaml", variants={"alt": {"cases_dir": "linked-cases"}})


def symlink_target(packs: Path) -> None:  # a target file → outside the pack
    target = packs / "toypack" / "targets" / "basic.yaml"
    shutil.move(target, packs / "outside-target.yaml")
    target.symlink_to(packs / "outside-target.yaml")


def symlink_targets_dir(packs: Path) -> None:  # targets/ → outside the pack
    shutil.move(packs / "toypack" / "targets", packs / "outside-targets")
    (packs / "toypack" / "targets").symlink_to(packs / "outside-targets")


def symlink_dependency_file(packs: Path) -> None:  # a dependency's code → outside it
    shutil.move(packs / "toybase" / "tools.py", packs / "outside-tools.py")
    (packs / "toybase" / "tools.py").symlink_to(packs / "outside-tools.py")


@pytest.mark.parametrize(
    "make",
    [
        symlink_cases,
        symlink_data,
        symlink_code_dir,
        symlink_inner_dir,
        symlink_cases_dir,
        symlink_target,
        symlink_targets_dir,
        symlink_dependency_file,
    ],
)
def test_symlink_in_a_pack_is_refused(packs, make):
    make(packs)
    with pytest.raises(PackError, match="a symlink"):
        pack_sha256(packs, "toypack")
    with pytest.raises(PackError, match="a symlink"):
        load_pack(packs, "toypack", variant="alt")


def test_symlink_in_a_part_the_hash_skips_is_ignored(packs):
    before = pack_sha256(packs, "toypack")
    (packs / "toypack" / "tests").mkdir()
    (packs / "toypack" / "tests" / "link.py").symlink_to(packs / "toybase" / "tools.py")
    assert pack_sha256(packs, "toypack") == before


def test_yaml_python_tag_is_refused_and_not_executed(packs, tmp_path):
    proof = tmp_path / "executed"
    case = packs / "toypack" / "cases" / "toy-open.yaml"
    case.write_text(f'id: !!python/object/apply:os.system ["touch {proof}"]\n')
    with pytest.raises(PackError, match="invalid YAML"):
        load_pack(packs, "toypack")
    assert not proof.exists()
