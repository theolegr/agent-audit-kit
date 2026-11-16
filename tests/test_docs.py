"""The pack guide, the usage guide and the README follow the code and the published run: a lint
rule, a required pack.py name, a core check, the reading of a canary, a CLI option or a published
number that changes makes these fail, not the docs silently drift."""

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

import yaml

from auditkit import generic_checks
from auditkit.checks import collect
from auditkit.cli import _usd_amount, build_parser, load_dotenv, main
from auditkit.cost import price
from auditkit.pack import REQUIRED
from auditkit.stats import cases_needed, cp_upper

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / "docs" / "DOMAIN_PACKS.md").read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
USAGE = (ROOT / "docs" / "USAGE.md").read_text(encoding="utf-8")
PAGE = (ROOT / "docs" / "index.html").read_text(encoding="utf-8").split("</head>", 1)[1]  # its text
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
    assert flags(README + GUIDE + USAGE + PAGE) - NOT_EVAL_FLAGS - options == set()


def test_usage_env_example_loads_as_written(tmp_path, monkeypatch):
    (block,) = [b for b in re.findall(r"```bash\n(.*?)```", USAGE, re.DOTALL) if "_MODEL=" in b]
    (tmp_path / ".env").write_text(block, encoding="utf-8")
    environ: dict[str, str] = {}
    monkeypatch.setattr(os, "environ", environ)  # load_dotenv writes here, not in the real one
    load_dotenv(tmp_path / ".env")
    price(environ["AGENT_MODEL"])
    price(environ["SIMULATOR_MODEL"])
    assert _usd_amount(environ["EVAL_BUDGET_USD"]) == 3.0


def test_usage_carries_the_positioning_sentence_the_phq9_pack_prints():
    disclaimer = yaml.safe_load((ROOT / "domains" / "phq9" / "pack.yaml").read_text())["disclaimer"]
    plain = " ".join(re.sub(r"^> ?", "", USAGE, flags=re.MULTILINE).replace("*", "").split())
    assert " ".join(disclaimer.split()) in plain


def test_readme_says_eval_is_a_shell_builtin_and_states_the_disclaimer():
    plain = " ".join(re.sub(r"^> ?", "", README, flags=re.MULTILINE).split())
    assert "`eval` is a shell builtin" in plain
    for phrase in ("Not a medical device", "No clinical advice", "Synthetic data only"):
        assert phrase in plain


# ── The README's first run: every number from the published run ───────────────────────────────


def page_generator():
    """tools/gen_page.py, which names the published run for the README's tests and the page's."""
    spec = importlib.util.spec_from_file_location("gen_page", ROOT / "tools" / "gen_page.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_page"] = module
    spec.loader.exec_module(module)
    return module


PUBLISHED, REGRADE = page_generator().RUN, page_generator().GRADED
# The hazards a regrade today decides differently from the published regrade: the AI-status rule
# was tested on reviewed labelled examples after that regrade was made. The next published run
# brings this back to an empty set.
CHANGED_SINCE_PUBLISHED = {"H-08"}


def readme_section(heading: str) -> str:
    """A README section, its whitespace folded: rewrapping a paragraph changes nothing here."""
    start = README.index(f"\n## {heading}\n")
    end = README.find("\n## ", start + 1)
    return " ".join(README[start:end].split())


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def verdict_groups(hazards: list[dict]) -> list[tuple[str, int]]:
    """The README table's rows, in order: the verdict and how many hazards it holds."""
    nd = [h for h in hazards if h["verdict"] == "not_demonstrated"]
    rough = [h for h in nd if h["limited_evidence"] or h["cases_tagged"] == 0]
    groups = [
        ("not demonstrated", [h for h in nd if h["cases_failed"]]),
        ("not demonstrated", [h for h in nd if not h["cases_failed"] and h not in rough]),
        ("not demonstrated", [h for h in rough if not h["cases_failed"]]),
        ("not met", [h for h in hazards if h["verdict"] == "not_met"]),
        ("met", [h for h in hazards if h["verdict"] == "met"]),
    ]
    assert sum(len(g) for _, g in groups) == len(hazards)  # every hazard in exactly one row
    return [(verdict, len(g)) for verdict, g in groups if g]  # an empty group has no row


def test_readme_box_keeps_the_whole_disclaimer_above_the_first_heading():
    top = README[: README.index("\n## ")]
    assert "> **What this is not.**" in top
    box = top.split("> **What this is not.**", 1)[1]
    plain = " ".join(re.sub(r"^> ?", "", box, flags=re.MULTILINE).replace("*", "").split())
    points = (
        "Not a medical device",
        "screening tool",
        "test target",
        "not a suicide-risk assessment",
        "No clinical advice",
        "Synthetic data only",
        "no real patient or user data",
        "Not an audit or a certification",
    )
    assert [p for p in points if p not in plain] == []


def test_the_readmes_name_the_published_run():
    examples = (ROOT / "examples" / "README.md").read_text(encoding="utf-8")
    for text in (README, examples):
        named = set(re.findall(r"(?:examples|runs|\[`)/?(\d{4}-\d\d-\d\d_\d{6})", text))
        assert named == {PUBLISHED.name}
    assert REGRADE.name in examples


def test_readme_names_no_hazard_or_backlog_id():
    assert re.findall(r"\b[HFBD]-\d{2,3}\b|\bQ\d{2}\b", README) == []


def test_readme_first_run_numbers_come_from_the_published_run():
    """Each number of the section, tied to what it counts by the words around it: a wrong number,
    or a number the run does not give, fails. Rewording a tied phrase means updating it here."""
    text = readme_section("The first run")
    manifest, execution = load(PUBLISHED / "manifest.json"), load(PUBLISHED / "execution.json")
    results, own = load(REGRADE / "results.json"), load(PUBLISHED / "results.json")
    hazards = {h["hazard_id"]: h for h in results["hazards"]}
    limits = {
        h["id"]: round(h["acceptance"]["max_rate"] * 100)
        for h in yaml.safe_load((ROOT / "domains" / "phq9" / "hazards.yaml").read_text())["hazards"]
    }
    cases, repeats, trials = (
        len(manifest["case_ids"]),
        manifest["repeats"],
        len(execution["trials"]),
    )

    def says(phrase: str) -> None:
        assert phrase in text, f"the README's first run does not say: {phrase!r}"

    says(f"{cases} test cases (invented users), {repeats} conversations each")
    says(f"{trials} conversations, for ${execution['spent_usd']:.2f}")
    says(f"The report, graded again after two fixes, judges {len(hazards)} risks")
    says(f"A case fails if any of its {repeats} conversations fails")

    # the one real mistake: the only trial that fails a hazard graded by more than a keyword rule
    real = {
        (f["case_id"], f["n"])
        for h in hazards.values()
        if not h["limited_evidence"]
        for f in h["failures"]
    }
    assert len(real) == 1
    ((case_id, n),) = real
    (trial,) = [t for t in results["trials"] if (t["case_id"], t["n"]) == (case_id, n)]
    items, total = trial["outcome"]["item_scores"], trial["outcome"]["total"]
    (note,) = [
        r["note"] for r in trial["results"] if r["check_id"] == "phq9.total_and_band_correct"
    ]
    given, right = re.search(r"the band is '([^']+)', not '([^']+)'", note).groups()
    says(f"add up to {sum(items)} (`{'+'.join(map(str, items))}`)")
    says(f'total of {total}, in the band "{given}" instead of "{right}"')

    groups = verdict_groups(results["hazards"])
    rows = re.findall(r"\| (not demonstrated|not met|met) \| (\d+)\b", text)
    assert [(verdict, int(count)) for verdict, count in rows] == groups
    mistake = [
        h for h in hazards.values() if h["verdict"] == "not_demonstrated" and h["cases_failed"]
    ]
    assert {limits[h["hazard_id"]] for h in mistake} == {10}  # one limit, as the row says
    (h,) = {(h["cases_failed"], h["cases_counted"]) for h in mistake}
    says(f"{h[0]} failure in {h[1]} cases: under their {limits[mistake[0]['hazard_id']]}% limit")
    (met,) = [h for h in hazards.values() if h["verdict"] == "met"]
    says(f"{met['cases_counted']} cases, no failure, under its {limits[met['hazard_id']]}% limit")
    bound = cp_upper(0, cases)
    enough = cases + cases_needed(0, cases, 0.05, 0.95)
    says(f"With {cases} cases and no failure")
    says(f"as high as {bound:.1%} (95% confidence)")
    says(f"needs about {enough} such cases to show it stays under 5%")

    # the keyword rule's false alarms: the one verdict the regrade changed, from the run's own
    # not met to not demonstrated, on a risk a keyword rule alone grades
    before = {h["hazard_id"]: h for h in own["hazards"]}
    (flagged,) = [hid for hid, h in hazards.items() if h["verdict"] != before[hid]["verdict"]]
    advice = hazards[flagged]
    assert (before[flagged]["verdict"], advice["verdict"]) == ("not_met", "not_demonstrated")
    assert advice["limited_evidence"] and not advice["cases_failed"]
    quote = re.search(r'such as "([^"]+)"', text)
    assert quote, "the README quotes no flagged sentence"
    assert any(quote.group(1) in f["note"] for f in before[flagged]["failures"])
    says("The run's own report gives that risk not met")
    says("It now reads not demonstrated")
    says(f"under its {limits[flagged]}% limit, and only {advice['cases_counted']} cases probe it")

    # the rule that blamed the agent for a skip: the risk its set-aside conversation counts for
    # reads as it did
    (skip,) = [hid for hid, h in hazards.items() if h["cases_excluded"]]
    assert before[skip]["verdict"] == hazards[skip]["verdict"] == "not_demonstrated"
    says("that risk reads not demonstrated, as before")

    allowed = {str(x) for x in (cases, repeats, trials, len(hazards), 95, 5, enough)}
    allowed |= {str(x) for x in (*items, sum(items), total, *(c for _, c in groups))}
    allowed |= {str(x) for x in (met["cases_counted"], advice["cases_counted"], *limits.values())}
    allowed |= {f"{execution['spent_usd']:.2f}", f"{bound * 100:.1f}"}
    assert set(re.findall(r"\d+(?:\.\d+)?", text)) - allowed == set()


def test_readme_stays_short():
    # the README is the short way in, the reference lives in docs/USAGE.md; the bound leaves room
    # for one line that links a page about the run
    prose = README.split("```")[::2]  # outside the code blocks
    words = sum(1 for part in prose for run in part.split() if re.search(r"[^\W_]", run))
    assert words <= 820


def test_a_replay_of_the_published_run_gives_its_own_results(tmp_path):
    # the README's first developer command: a plain replay of the copied run
    run = tmp_path / "run"
    shutil.copytree(PUBLISHED, run)
    assert main(["--replay", str(run), "--packs-dir", str(ROOT / "domains")]) == 0
    assert load(run / "results.json")["hazards"] == load(PUBLISHED / "results.json")["hazards"]


def test_a_regrade_of_the_published_run_today_gives_the_published_regrade(tmp_path):
    # the README tells readers to run --regrade-current: what they get must be what it says
    run = tmp_path / "run"
    shutil.copytree(PUBLISHED, run, ignore=shutil.ignore_patterns("history", "regrade-*"))
    argv = ["--replay", str(run), "--regrade-current", "--packs-dir", str(ROOT / "domains")]
    assert main(argv) == 0
    (out,) = run.glob("regrade-*")
    ours, published = load(out / "results.json"), load(REGRADE / "results.json")

    def unchanged(results: dict) -> list[dict]:
        return [h for h in results["hazards"] if h["hazard_id"] not in CHANGED_SINCE_PUBLISHED]

    assert unchanged(ours) == unchanged(published)
    status = {(t["case_id"], t["n"]): (t["status"], t["reasons"]) for t in ours["trials"]}
    assert status == {
        (t["case_id"], t["n"]): (t["status"], t["reasons"]) for t in published["trials"]
    }


def test_a_regrade_of_the_published_run_decides_h08_with_the_tested_rule(tmp_path):
    # the AI-status examples are reviewed, so a regrade today decides H-08 on its 20 cases
    labelled = ROOT / "domains" / "phq9" / "labelled" / "ai_status.yaml"
    reviewer = yaml.safe_load(labelled.read_text(encoding="utf-8"))["reviewed_by"]
    assert reviewer
    run = tmp_path / "run"
    shutil.copytree(PUBLISHED, run, ignore=shutil.ignore_patterns("history", "regrade-*"))
    argv = ["--replay", str(run), "--regrade-current", "--packs-dir", str(ROOT / "domains")]
    assert main(argv) == 0
    (out,) = run.glob("regrade-*")
    ours, published = load(out / "results.json"), load(REGRADE / "results.json")

    (spec,) = [c for c in ours["checks"] if c["id"] == "core.ai_status_disclosed_first_turn"]
    test = spec["rule_test"]
    assert (test["failures"], test["passes"], test["reviewed_by"]) == (60, 40, reviewer)
    missed, false_alarms = len(test["missed"]), len(test["false_alarms"])
    (h08,) = [h for h in ours["hazards"] if h["hazard_id"] == "H-08"]
    assert (h08["cases_counted"], h08["cases_failed"], h08["limited_evidence"]) == (20, 0, False)
    assert not any("limited evidence" in reason for reason in h08["reasons"])
    bound = cp_upper(0, 20, 0.975) / (1 - cp_upper(missed, 60, 0.975))
    assert h08["upper_bound"] == bound
    assert h08["verdict"] == ("met" if bound <= 0.20 else "not_demonstrated")
    (before,) = [h for h in published["hazards"] if h["hazard_id"] == "H-08"]
    assert (before["verdict"], before["limited_evidence"]) == ("not_demonstrated", True)

    report = (out / "report.md").read_text(encoding="utf-8")
    assert f"missed {missed} of 60 failures" in report
    assert f"raised {false_alarms} false alarms on 40 correct ones" in report
    assert "divided by 1" in report
    for example_id in test["missed"] + test["false_alarms"]:
        assert f"`{example_id}`" in report

    assert "H-08" in CHANGED_SINCE_PUBLISHED

    def others(results: dict) -> list[dict]:
        return [h for h in results["hazards"] if h["hazard_id"] != "H-08"]

    assert others(ours) == others(published)
    status = {(t["case_id"], t["n"]): (t["status"], t["reasons"]) for t in ours["trials"]}
    assert status == {
        (t["case_id"], t["n"]): (t["status"], t["reasons"]) for t in published["trials"]
    }
