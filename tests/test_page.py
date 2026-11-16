"""docs/index.html, the page about the published run, stays true to it: its generated parts are
fresh, every number it quotes is the run's, every quote is in its transcript, and it loads
nothing from anywhere."""

import html
import importlib.util
import json
import re
import sys
from pathlib import Path

import yaml

from auditkit.stats import cases_needed, cp_upper

ROOT = Path(__file__).resolve().parents[1]
PAGE = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")


def generator():
    spec = importlib.util.spec_from_file_location("gen_page", ROOT / "tools" / "gen_page.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_page"] = module
    spec.loader.exec_module(module)
    return module


GEN = generator()
RUN = GEN.RUN  # the run the page is about, as the generator names it


def text_of(fragment: str) -> str:
    """What a reader sees of an HTML fragment, its whitespace folded."""
    return " ".join(html.unescape(re.sub(r"<[^>]*>", "", fragment)).split())


def hand_written(page: str) -> str:
    """The page without its generated parts, its styles and its head."""
    page = re.sub(r"(?s)<!-- generated: (\w+) -->.*?<!-- /generated: \1 -->", "", page)
    return re.sub(r"(?s)<head>.*?</head>", "", page)


def test_the_generated_parts_are_fresh():
    assert GEN.render(PAGE) == PAGE, "run: uv run python tools/gen_page.py"


def test_every_number_the_page_quotes_is_the_run_s():
    facts = GEN.facts()
    shown = re.findall(r'<(\w+) data-fact="([a-z0-9_]+)">([^<]*)</\1>', PAGE)
    assert shown, "the page quotes no fact"
    assert [(key, value) for _, key, value in shown if facts.get(key) != value] == []
    # every fact is shown, so none is computed for nothing
    assert set(facts) == {key for _, key, _ in shown}


def test_no_number_in_the_page_s_text_escapes_the_facts():
    """A number written by hand outside a data-fact, a quote or a code block must be one the
    method fixes (95% confidence, the 5% limit), not one the run gives."""
    page = hand_written(PAGE)
    page = re.sub(r'(?s)<(\w+) data-(?:fact|quote)="[^"]*">.*?</\1>', "", page)
    page = re.sub(r"(?s)<(pre|code|a)\b.*?</\1>", "", page)
    numbers = set(re.findall(r"\d+(?:\.\d+)?", text_of(page).replace("PHQ-9", "PHQ")))
    assert numbers - {"95", "5", "14971"} == set()
    # the head's description is shown by search engines and link previews: no run number there
    (described,) = re.findall(r'<meta name="description" content="([^"]*)">', PAGE)
    assert not re.search(r"\d", described)


def test_the_run_the_page_names_is_the_one_it_shows():
    named = set(re.findall(r"(?:examples|runs)/(\d{4}-\d\d-\d\d_\d{6})", PAGE))
    assert named == {GEN.RUN.name}
    assert GEN.GRADED.parent == GEN.RUN


def test_every_quote_is_in_its_transcript():
    quotes = re.findall(r'(?s)<(\w+) data-quote="([^:"]+):(\d+):(\d+)">(.*?)</\1>', PAGE)
    assert len(quotes) >= 5
    for _, case_id, n, turn, fragment in quotes:
        trial = json.loads((RUN / case_id / f"run_{n}.json").read_text(encoding="utf-8"))
        said = " ".join(trial["transcript"]["turns"][int(turn)]["text"].split())
        assert text_of(fragment) in said, (case_id, n, turn)


def test_the_flagged_sentences_are_the_ones_the_keyword_rule_flagged():
    # the run's own grading flagged them; the regrade the page shows, with the rule fixed, none
    own = json.loads((RUN / "results.json").read_text(encoding="utf-8"))
    (h04,) = [h for h in own["hazards"] if h["hazard_id"] == "H-04"]
    notes = [f["note"] for f in h04["failures"] + h04["outside_sample"]]
    regraded = json.loads((GEN.GRADED / "results.json").read_text(encoding="utf-8"))
    (now,) = [h for h in regraded["hazards"] if h["hazard_id"] == "H-04"]
    assert now["failures"] + now["outside_sample"] == []
    for case_id, n in (
        ("phq9-adv-meds-stop", 2),
        ("phq9-adv-meds-stop", 3),
        ("phq9-item9-minimizer", 1),
    ):
        (fragment,) = re.findall(rf'(?s)data-quote="{case_id}:{n}:\d+">(.*?)</', PAGE)
        assert any(text_of(fragment) in note for note in notes), (case_id, n)


def test_each_risk_with_no_failure_and_a_5_percent_limit_needs_59_cases_in_all():
    results = json.loads((GEN.GRADED / "results.json").read_text(encoding="utf-8"))
    dots = GEN.dots(results, GEN.registry())
    assert dots
    enough = min(n for n in range(1, 200) if cp_upper(0, n) <= 0.05)
    assert enough == 59
    assert {h["cases_counted"] + h["cases_needed"] for h in dots} == {enough}
    assert all(cases_needed(0, h["cases_counted"], 0.05, 0.95) == h["cases_needed"] for h in dots)


def test_every_risk_has_a_plain_name_and_a_short_one():
    ids = {
        h["id"] for h in yaml.safe_load((ROOT / "domains/phq9/hazards.yaml").read_text())["hazards"]
    }
    assert set(GEN.PLAIN) == ids == set(GEN.SHORT)
    for hid, name in GEN.PLAIN.items():
        assert html.escape(name) in PAGE, hid


def test_the_page_loads_nothing_and_runs_nothing():
    assert "<script" not in PAGE.lower()
    assert not re.search(r"(?i)\b(?:src|srcset)\s*=", PAGE)
    assert not re.search(r"(?i)@import|url\(", PAGE)
    links = re.findall(r'href="([^"]+)"', PAGE)
    assert links and all(
        link.startswith("https://github.com/theolegr/agent-audit-kit") for link in links
    )


def test_the_page_carries_the_whole_notice_and_the_pack_s_sentence():
    disclaimer = yaml.safe_load((ROOT / "domains/phq9/pack.yaml").read_text())["disclaimer"]
    plain = text_of(PAGE)
    assert " ".join(disclaimer.split()) in plain
    for point in (
        "Not a medical device",
        "medical device",
        "screening tool",
        "No clinical advice",
        "Synthetic data only",
        "no real patient or user data",
        "Not an audit or a certification",
    ):
        assert point in plain, point
    top = text_of(PAGE[: PAGE.index("<h2")])
    assert "Synthetic data only. Not a medical device, not clinical advice" in top


def test_a_verdict_is_never_told_by_colour_alone():
    grid = re.search(r"(?s)<!-- generated: grid -->(.*?)<!-- /generated: grid -->", PAGE)[1]
    verdicts = re.findall(r'<span class="vm (\w+)" aria-hidden="true"></span>([a-z ]+)</td>', grid)
    assert len(verdicts) == len(GEN.PLAIN)
    assert {(kind, word) for kind, word in verdicts} <= {
        ("met", "met"),
        ("notmet", "not met"),
        ("nd", "not demonstrated"),
        ("nd", "not evaluable"),
    }
    shapes = re.findall(
        r'<span class="m (\w+)" title="([^"]+)" aria-hidden="true">([^<]*)</span>'
        r'<span class="sr">([^<]+)</span>',
        grid,
    )
    assert (
        len(shapes)
        == grid.count('class="m ')
        == len(GEN.PLAIN) * len(GEN.load(RUN / "manifest.json")["case_ids"])
    )
    marks = {kind or "none": shown for kind, shown in GEN.MARKS.items()}
    for kind, title, mark, said in shapes:  # each mark has its shape, and names its result in words
        assert (mark, said.split(": ", 1)[1]) == marks[kind] and title == said
