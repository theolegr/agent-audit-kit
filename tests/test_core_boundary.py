"""The core never knows a pack (boundary tests 1 to 5).

Tests 1 to 4 read the source with `ast`: comments and docstrings are ignored. Test 5 runs the toy
pack, which sits outside `domains/`, end to end through the live runner with a scripted client.
"""

import ast
import json
import re
from pathlib import Path

from auditkit.cli import main
from auditkit.testing import FIXED_NOW, ScriptedLLM, say, use, user_says

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "src" / "auditkit"
CORE_FILES = sorted(CORE.rglob("*.py"))  # the ui/ package too
PACK_DIRS = sorted(
    d
    for parent in (ROOT / "domains", ROOT / "tests" / "fixtures" / "packs")
    if parent.is_dir()
    for d in parent.iterdir()
    if d.is_dir() and not d.name.startswith((".", "_"))
)
RUNTIME = {"llm", "adapters", "simulator", "runner"}
GRADING_PATH = {
    "schema",
    "pack",
    "checks",
    "tools",
    "cost",
    "lint",
    "stats",
    "grade",
    "generic_checks",
    "runfolder",
    "report",
}


def squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def words(s: str) -> str:
    """Lower-case words, one space apart, cut at anything but letters and digits, camelCase humps
    and letter-digit changes: `Phq9_BANDS` -> `phq 9 bands`."""
    return " ".join(re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+", s)).lower()


def names(path: Path) -> set[str]:
    """Identifiers and non-docstring string literals of one module, as words."""
    tree = ast.parse(path.read_text())
    docs = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    raw: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            raw.add(n.id)
        elif isinstance(n, ast.Attribute):
            raw.add(n.attr)
        elif isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            raw.add(n.name)
        elif isinstance(n, ast.arg) or (isinstance(n, ast.keyword) and n.arg):
            raw.add(n.arg)
        elif isinstance(n, ast.alias):
            raw.add(n.asname or n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs:
            raw.add(n.value)
    return {words(s) for s in raw}


def pack_name_hits(files: list[Path], pack_names: set[str]) -> list[tuple[str, str]]:
    """The squashed pack name over whole words: words may split it (`PHQ-9`), never cut it
    (`demo` in `not_demonstrated`)."""
    packs = {p: re.compile(rf"\b{' ?'.join(p)}\b") for p in map(squash, pack_names)}
    return sorted(
        {(f.name, p) for f in files for s in names(f) for p, rx in packs.items() if rx.search(s)}
    )


def imported(path: Path) -> list[tuple[ast.Import | ast.ImportFrom, str]]:
    """Every import statement of a module, with the dotted name it imports."""
    out: list[tuple[ast.Import | ast.ImportFrom, str]] = []
    for n in ast.walk(ast.parse(path.read_text())):
        if isinstance(n, ast.Import):
            out += [(n, a.name) for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            base = ("auditkit." * (n.level > 0)) + (n.module or "")
            out += [(n, f"{base.rstrip('.')}.{a.name}") for a in n.names]
            out.append((n, base.rstrip(".")))
    return out


def core_edges(path: Path) -> set[str]:
    """The core modules and the external roots one module imports. Importing `auditkit.x` runs
    `auditkit/__init__.py` first, so it counts as an edge too."""
    modules = {f.stem for f in CORE_FILES}
    edges: set[str] = set()
    for _, dotted in imported(path):
        parts = dotted.split(".")
        if parts[0] != "auditkit":
            edges.add(parts[0])
            continue
        edges.add("__init__")
        if len(parts) > 1 and parts[1] in modules:
            edges.add(parts[1])
    return edges


# ── 1. No pack imports ────────────────────────────────────────────────────────────────────────────


def test_core_modules_import_no_pack_code():
    hits = [
        (f.name, dotted)
        for f in CORE_FILES
        for _, dotted in imported(f)
        if dotted.split(".")[0] in {"domains", "packs"}
    ]
    assert not hits, hits


# ── 2. No pack names ──────────────────────────────────────────────────────────────────────────────


def test_core_names_no_pack():
    assert PACK_DIRS, "no pack folder found: the test would pass vacuously"
    hits = pack_name_hits(CORE_FILES, {d.name for d in PACK_DIRS})
    assert not hits, hits


def test_pack_name_check_catches_a_pack_name_in_core_like_code(tmp_path):
    module = tmp_path / "bad.py"
    module.write_text('"""Docstrings may say phq9."""\n\n\ndef phq9_band():\n    return "PHQ-9"\n')
    assert pack_name_hits([module], {"phq9"}) == [("bad.py", "phq9")]
    assert names(module) == {"phq 9 band", "phq 9"}  # the function and the string


def test_pack_name_check_ignores_docstrings_and_squashed_near_misses(tmp_path):
    module = tmp_path / "fine.py"
    module.write_text('"""About phq9."""\n\nSTATUS = "supported"\n')
    assert pack_name_hits([module], {"phq9", "saas_support"}) == []


def test_pack_name_check_matches_whole_words_not_parts_of_one(tmp_path):
    module = tmp_path / "words.py"
    module.write_text('VERDICT = "not_demonstrated"\n\n\nclass Phq9Bands:\n    pass\n')
    assert pack_name_hits([module], {"demo", "band", "phq9"}) == [("words.py", "phq9")]
    assert pack_name_hits(CORE_FILES, {"demo"}) == []


# ── 3. Only llm.py imports anthropic, inside AnthropicClient.__init__ ─────────────────────────────


def anthropic_imports_outside_the_client(path: Path) -> list[int]:
    allowed: set[int] = set()  # the lines of AnthropicClient.__init__
    if path.name == "llm.py":
        for cls in ast.parse(path.read_text()).body:
            if isinstance(cls, ast.ClassDef) and cls.name == "AnthropicClient":
                for fn in cls.body:
                    if isinstance(fn, ast.FunctionDef) and fn.name == "__init__":
                        allowed |= set(range(fn.lineno, (fn.end_lineno or fn.lineno) + 1))
    return sorted(
        {
            node.lineno
            for node, dotted in imported(path)
            if dotted.split(".")[0] == "anthropic" and node.lineno not in allowed
        }
    )


def test_only_llm_imports_anthropic():
    hits = {f.name: lines for f in CORE_FILES if (lines := anthropic_imports_outside_the_client(f))}
    assert not hits, hits


def test_anthropic_import_check_catches_a_module_level_import(tmp_path):
    module = tmp_path / "llm.py"
    module.write_text(
        "import anthropic\n\n\nclass AnthropicClient:\n"
        "    def __init__(self):\n        import anthropic\n"
    )
    assert anthropic_imports_outside_the_client(module) == [1]


# ── 4. The grading path never reaches the runtime ─────────────────────────────────────────────────


def reachable(start: str) -> set[str]:
    graph = {f.stem: core_edges(f) for f in CORE_FILES}
    seen: set[str] = set()
    todo = [start]
    while todo:
        for nxt in graph.get(todo.pop(), set()) - seen:
            seen.add(nxt)
            todo.append(nxt)
    return seen


def test_grading_path_never_imports_runtime():
    present = sorted(GRADING_PATH & {f.stem for f in CORE_FILES})
    assert "pack" in present and "schema" in present
    hits = {m: sorted(r) for m in present if (r := reachable(m) & (RUNTIME | {"anthropic"}))}
    assert not hits, hits


# ── 5. A pack the core has never heard of runs end to end ─────────────────────────────────────────


def toy_agent(request):
    if request["messages"][-1].blocks[0].type == "tool_result":
        return say('Urgent: R-1 is open. {"status": "open"}')
    return use("lookup_record", {"record": "R-1"})


def test_toy_pack_runs_end_to_end_with_scripted_llm(tmp_path, monkeypatch):
    for name in ("AGENT_MODEL", "SIMULATOR_MODEL", "EVAL_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)
    llm = ScriptedLLM(
        simulator=lambda r: user_says("Where is R-1?", {"asking_about": "R-1"}), agent=toy_agent
    )
    packs = ROOT / "tests" / "fixtures" / "packs"
    argv = ["--domain", "toypack", "--packs-dir", str(packs), "--runs-dir", str(tmp_path)]
    code = main([*argv, "--cases", "toy-open"], clock=lambda: FIXED_NOW, make_client=lambda: llm)
    assert code == 0
    (run,) = tmp_path.iterdir()
    results = json.loads((run / "results.json").read_text())
    assert {h["hazard_id"] for h in results["hazards"]} == {"H-01", "H-02", "H-03"}
    assert [t["status"] for t in results["trials"]] == ["valid", "valid"]
    assert (run / "report.md").is_file()
