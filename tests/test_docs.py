"""The pack guide and the README follow the code: a lint rule, a required pack.py name, a core
check, the reading of a canary or a CLI option that changes makes these fail, not the docs
silently drift."""

import os
import re
from pathlib import Path

import yaml

from auditkit import generic_checks
from auditkit.checks import collect
from auditkit.cli import _usd_amount, build_parser, load_dotenv
from auditkit.cost import price
from auditkit.pack import REQUIRED

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "docs" / "DOMAIN_PACKS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
NOT_EVAL_FLAGS = {"--check", "--fix"}  # `ruff format --check`, `ruff check --fix`


def flags(text: str) -> set[str]:
    return set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", text))


def test_guide_lists_exactly_the_lint_rules_the_lint_emits():
    source = (ROOT / "src" / "auditkit" / "lint.py").read_text(encoding="utf-8")
    emitted = set(re.findall(r'"(L[0-9]{2})"', source))
    documented = set(re.findall(r"^\| (L[0-9]{2}) \|", GUIDE, re.MULTILINE))
    assert emitted and documented == emitted


def test_guide_names_the_required_pack_py_names():
    assert [name for name in REQUIRED if f"`{name}`" not in GUIDE] == []


def test_guide_lists_every_core_check_with_its_kind_and_method():
    defs = collect(generic_checks)[0]
    rows = re.findall(r"^\| `core\.([a-z_]+)` \| (\w+) \| (\w+) \|", GUIDE, re.MULTILINE)
    assert sorted(rows) == sorted((d.id, d.kind, d.method) for d in defs)


def test_guide_examples_of_a_match_across_words_are_what_the_check_reads():
    # the guide warns pack authors that a canary, and a forbidden string read as one, matches
    # across words and numbers; if the reading changes, its examples must change with it
    stated = set(re.findall(r'`(\w+)` in (?:the date )?"([^"]+)"', " ".join(GUIDE.split())))
    examples = {
        ("Leeann", "flee annually"),
        ("441789", "1-800-441-7890"),
        ("202611", "2026-11-05"),
        ("PINEAPPLE", "spine apple"),
    }
    assert examples <= stated
    assert [(n, text) for n, text in stated if not generic_checks.holds_canary(text, n)] == []


def test_docs_use_only_options_the_cli_has():
    options = {s for a in build_parser()._actions for s in a.option_strings}
    assert flags(README + GUIDE) - NOT_EVAL_FLAGS - options == set()


def test_readme_env_example_loads_as_written(tmp_path, monkeypatch):
    (block,) = [b for b in re.findall(r"```bash\n(.*?)```", README, re.DOTALL) if "_MODEL=" in b]
    (tmp_path / ".env").write_text(block, encoding="utf-8")
    environ: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", environ)  # load_dotenv writes here, not in the real one
    load_dotenv(tmp_path / ".env")
    price(environ["AGENT_MODEL"])
    price(environ["SIMULATOR_MODEL"])
    assert _usd_amount(environ["EVAL_BUDGET_USD"]) == 3.0


def test_readme_carries_the_positioning_sentence_the_phq9_pack_prints():
    disclaimer = yaml.safe_load((ROOT / "domains" / "phq9" / "pack.yaml").read_text())["disclaimer"]
    plain = " ".join(re.sub(r"^> ?", "", README, flags=re.MULTILINE).replace("*", "").split())
    assert " ".join(disclaimer.split()) in plain


def test_readme_says_eval_is_a_shell_builtin_and_states_the_disclaimer():
    assert "`eval` is a shell builtin" in README
    for phrase in ("Not a medical device", "No clinical advice", "Synthetic data only"):
        assert phrase in README
