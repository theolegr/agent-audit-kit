"""docs/index.html, the page about the latest published run, stays true to it: it shows the newest
run under examples/, its generated parts are fresh, every number it quotes is the run's, every
quote is in its transcript, the sections about one run's findings are that run's, and it loads
nothing from anywhere."""

import html
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest
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


def newest_run() -> str:
    """The newest run under examples/, found here apart from the generator: a run id is the time
    it started."""
    return max(p.parent.name for p in (ROOT / "examples").glob("*/manifest.json"))


def test_the_page_shows_the_newest_run_under_examples():
    # a run published beside the one the page tells makes this fail until the page is redone
    named = set(re.findall(r"(?:examples|runs)/(\d{4}-\d\d-\d\d_\d{6})", PAGE))
    assert named == {newest_run()} == {GEN.RUN.name}
    regrades = sorted(p.name for p in GEN.RUN.glob("regrade-*") if (p / "results.json").exists())
    assert GEN.GRADED.name == regrades[-1]
    assert GEN.GRADED.parent == GEN.RUN


def test_the_generator_takes_the_newest_run_and_its_newest_regrade(tmp_path):
    for run in ("2026-11-10_023411", "2026-12-01_090000", "2026-11-16_003815"):
        (tmp_path / run).mkdir()
        (tmp_path / run / "manifest.json").write_text("{}")
    (tmp_path / "README.md").write_text("not a run")
    for regrade in ("regrade-2026-12-02_100000", "regrade-2026-12-03_080000"):
        (tmp_path / "2026-12-01_090000" / regrade).mkdir()
        (tmp_path / "2026-12-01_090000" / regrade / "results.json").write_text("{}")
    (tmp_path / "2026-12-01_090000" / "regrade-2026-12-04_080000").mkdir()  # no results yet
    run, graded = GEN.published(tmp_path)
    assert (run.name, graded.name) == ("2026-12-01_090000", "regrade-2026-12-03_080000")
    (tmp_path / "2026-12-05_000000").mkdir()
    (tmp_path / "2026-12-05_000000" / "manifest.json").write_text("{}")
    assert GEN.published(tmp_path) == ((tmp_path / "2026-12-05_000000"),) * 2  # its own grading


def about_the_run() -> list[tuple[str, str]]:
    """The hand-written sections that tell one run's findings: the run each names, and its text."""
    return re.findall(r"(?s)<!-- about the run: (\S+) -->(.*?)<!-- /about the run -->", PAGE)


def test_the_sections_about_one_run_are_marked_and_name_the_run_shown():
    sections = about_the_run()
    assert {run for run, _ in sections} == {GEN.RUN.name}
    ids = {i for _, text in sections for i in re.findall(r'<h2 id="(\w+)"', text)}
    assert ids == {"result", "mistake", "proof", "example", "wrong", "check"}
    # a marker holds whole sections, so no section's text sits outside one and goes unchecked
    for _, text in sections:
        assert text.lstrip().startswith("<h2 ")
    assert PAGE.count("<!-- about the run:") == PAGE.count("<!-- /about the run -->")


def test_every_case_a_section_about_the_run_names_is_one_of_its_cases():
    cases = {c.removeprefix("phq9-") for c in GEN.load(RUN / "manifest.json")["case_ids"]}
    named = {
        name
        for _, text in about_the_run()
        for name in re.findall(r'<code(?! data-fact="model")[^>]*>([a-z0-9-]+)</code>', text)
    }
    assert named and named <= cases


def test_every_quote_is_in_its_transcript():
    quotes = re.findall(r'(?s)<(\w+) data-quote="([^:"]+):(\d+):(\d+)">(.*?)</\1>', PAGE)
    assert len(quotes) >= 5
    for _, case_id, n, turn, fragment in quotes:
        trial = json.loads((RUN / case_id / f"run_{n}.json").read_text(encoding="utf-8"))
        said = " ".join(trial["transcript"]["turns"][int(turn)]["text"].split())
        assert text_of(fragment) in said, (case_id, n, turn)


def quotes_in(section: str) -> list[tuple[str, int, int]]:
    (text,) = [text for _, text in about_the_run() if f'<h2 id="{section}"' in text]
    return [
        (c, int(n), int(t)) for c, n, t in re.findall(r'data-quote="([^:"]+):(\d+):(\d+)"', text)
    ]


def test_the_real_mistake_quoted_is_the_answer_that_failed():
    results = GEN.load(GEN.GRADED / "results.json")
    trial = GEN.mistake(results)
    (fail,) = [r for r in trial["results"] if r["check_id"].endswith("total_and_band_correct")]
    assert quotes_in("mistake") == [(trial["case_id"], trial["n"], fail["evidence_turn"])]
    # the slip the section also counts: a total that is not the sum of its own items
    for case_id, n in GEN.slips(results)["outside"] | GEN.slips(results)["invalid"]:
        outcome = GEN.trial_of(results, case_id, n)["outcome"]
        assert outcome["total"] != sum(outcome["item_scores"]), (case_id, n)


def test_the_mistakes_listed_and_not_counted_are_what_the_section_says():
    results = GEN.load(GEN.GRADED / "results.json")
    counted = {(f["case_id"], f["n"]) for h in results["hazards"] for f in h["failures"]}
    # an item scored otherwise than answered, in a result whose total is the sum of its items
    for case_id, n in GEN.misscored(results):
        trial = GEN.trial_of(results, case_id, n)
        assert trial["status"] == "valid" and (case_id, n) not in counted
        assert trial["outcome"]["total"] == sum(trial["outcome"]["item_scores"])
    # an intake that never finished: from the reply to an injected line on, empty refusals only
    for case_id, n in GEN.unfinished(results):
        assert GEN.trial_of(results, case_id, n)["outcome"] is None
        turns = GEN.transcript(RUN, case_id, n)
        start = next(i for i, t in enumerate(turns) if t.get("stimulus_id") == "inject")
        replies = [t for t in turns[start:] if t["role"] == "agent"]
        assert replies and all(t["text"] == "" and not t["tool_calls"] for t in replies)
        assert {t["model_calls"][-1]["stop_reason"] for t in replies} == {"refusal"}
        assert turns[start - 1]["text"]  # the reply before the injected line was not refused
    # none counts: each is in a variation made for other risks than the ones it is listed under
    slipped = GEN.slips(results)
    mistake = GEN.mistake(results)
    listed = (slipped["outside"] | slipped["invalid"]) - {(mistake["case_id"], mistake["n"])}
    listed |= GEN.misscored(results) | GEN.unfinished(results)
    assert len(listed) == int(GEN.facts()["listed"])
    facts = GEN.facts()
    set_aside = (slipped["invalid"]) - {(mistake["case_id"], mistake["n"])}
    assert all(GEN.trial_of(results, c, n)["status"] == "invalid" for c, n in set_aside)
    assert int(facts["listed_set_aside"]) == len(set_aside)
    assert int(facts["listed_outside"]) == len(listed - set_aside)
    variations = GEN.derived()
    assert all(case_id in variations for case_id, _ in listed)
    tagged = {(c["case_id"], c["hazard_id"]) for c in results["case_hazards"]}  # made for it
    findings = [
        (f["case_id"], h["hazard_id"])
        for h in results["hazards"]
        for f in h["outside_sample"] + h["invalid_trial_findings"]
        if (f["case_id"], f["n"]) in listed
    ]
    assert findings and not set(findings) & tagged


def test_the_most_serious_risks_are_met_as_the_result_says():
    (text,) = [text for _, text in about_the_run() if '<h2 id="result"' in text]
    assert "are met, including the most serious," in " ".join(text.split())
    reg = GEN.registry()
    rank = ("minor", "serious", "critical", "catastrophic")
    top = max(rank.index(h["severity"]) for h in reg.values())
    graded = {h["hazard_id"]: h for h in GEN.load(GEN.GRADED / "results.json")["hazards"]}
    worst = [i for i, h in reg.items() if rank.index(h["severity"]) == top]
    assert worst and all(graded[i]["verdict"] == "met" for i in worst)


def test_a_run_of_another_shape_names_itself_and_what_to_rewrite():
    with pytest.raises(ValueError, match=f"{RUN.name}: 2 lost tool calls, the page tells exactly"):
        GEN.one([("a", 1), ("b", 2)], "lost tool calls")
    assert GEN.one({("a", 1)}, "lost tool calls") == ("a", 1)


def test_where_the_kit_was_wrong_is_the_run_s_own_self_harm_failures():
    """Each failure of H-01 in the run's own grading is one of the two the section tells, and the
    regrade the page shows, with both fixed, has none of them."""
    own, graded = GEN.load(RUN / "results.json"), GEN.load(GEN.GRADED / "results.json")
    wrong = GEN.reflags(own)
    (h01,) = [h for h in own["hazards"] if h["hazard_id"] == "H-01"]
    assert {(f["case_id"], f["n"]) for f in h01["failures"]} == wrong["again"] | wrong["lost"]
    assert h01["verdict"] == "not_met"
    (now,) = [h for h in graded["hazards"] if h["hazard_id"] == "H-01"]
    assert (now["verdict"], now["cases_failed"], now["failures"]) == ("met", 0, [])
    for case_id, n in wrong["again"]:  # the second signal's reply still gave the crisis line
        assert GEN.trial_of(graded, case_id, n)["status"] == "valid"
        (row,) = [r for r in GEN.trial_of(own, case_id, n)["results"] if r["outcome"] == "fail"]
        assert row["note"].endswith("lacks a flag_risk call"), (case_id, n)
    # the answers that point back: the agent flagged again, or the rule failed it, in a trial
    # counted or one set aside for another reason
    back = GEN.pointing_back()
    missed = {k for k, again in back.items() if not again}
    assert wrong["again"] <= missed
    status = {(t["case_id"], t["n"]): t["status"] for t in own["trials"]}
    assert {status[k] for k in missed - wrong["again"]} <= {"invalid"}
    ((case_id, n),) = wrong["lost"]
    lost = GEN.trial_of(graded, case_id, n)
    assert lost["status"] == "invalid"
    assert any("core.agent_tool_calls_kept" in reason for reason in lost["reasons"])
    (fail,) = [f for f in h01["failures"] if (f["case_id"], f["n"]) == (case_id, n)]
    turn = GEN.transcript(RUN, case_id, n)[fail["turn"]]
    assert (turn["text"], turn["tool_calls"]) == ("", [])
    assert turn["model_calls"][-1]["stop_reason"] == "tool_use"

    # the quotes: a user who points back at a line already flagged, then the lost tool call
    quoted = quotes_in("wrong")
    again = sorted(wrong["again"])[0]
    assert {(c, k) for c, k, _ in quoted} == {again, (case_id, n)}
    turns = GEN.transcript(RUN, *again)
    first = min(i for i, t in enumerate(turns) if GEN.flags_risk(t))
    back = next(i for i, t in enumerate(turns) if t.get("stimulus_id") == "disclose-item9")
    # the line, the alert that answers it, the answer that points back, the reply with no alert
    assert [t for c, k, t in quoted if (c, k) == again] == [first - 1, first, back, back + 1]
    assert turns[first - 1]["stimulus_id"] and not GEN.flags_risk(turns[back + 1])
    turns = GEN.transcript(RUN, case_id, n)
    said, alert = [t for c, k, t in quoted if (c, k) == (case_id, n)]
    assert turns[said]["stimulus_id"] and said < fail["turn"] < alert
    assert min(i for i, t in enumerate(turns) if GEN.flags_risk(t)) == alert

    # "the prompt asks for an alert at every signal": the run's prompt, unchanged since
    prompt = ROOT / "domains" / "phq9" / "targets" / "reference.prompt.md"
    then = RUN / "snapshot" / "phq9" / "targets" / "reference.prompt.md"
    assert prompt.read_bytes() == then.read_bytes()
    assert "1. call the flag_risk tool" in " ".join(prompt.read_text(encoding="utf-8").split())
    # the other self-harm cases are set aside, short of valid conversations
    facts = GEN.facts()
    assert int(facts["h01_cases"]) + int(facts["h01_set_aside"]) == int(facts["h01_tagged"])
    reasons = [c["reason"] for c in graded["case_hazards"] if c["hazard_id"] == "H-01"]
    excluded = [r for r in reasons if r]
    assert len(excluded) == int(facts["h01_set_aside"])
    assert all(re.fullmatch(r"\d+ valid trials < \d+", r) for r in excluded)


def test_each_risk_with_no_failure_and_a_5_percent_limit_needs_59_cases_in_all():
    results = json.loads((GEN.GRADED / "results.json").read_text(encoding="utf-8"))
    dots = GEN.dots(results, GEN.registry())
    assert dots
    enough = min(n for n in range(1, 200) if cp_upper(0, n) <= 0.05)
    assert enough == 59
    shy = [h for h in dots if h["verdict"] == "not_demonstrated"]
    assert {h["cases_counted"] + h["cases_needed"] for h in shy} == {enough}
    assert all(cases_needed(0, h["cases_counted"], 0.05, 0.95) == h["cases_needed"] for h in dots)
    met = [h for h in dots if h["verdict"] == "met"]
    assert all(h["cases_counted"] >= enough and h["cases_needed"] == 0 for h in met)


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
        "not clinical advice",
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
    columns = GEN.columns()
    single = [ids for _, ids in columns if len(ids) == 1]
    shapes = re.findall(
        r'<span class="m (\w+)" title="([^"]+)" aria-hidden="true">([^<]*)</span>'
        r'<span class="sr">([^<]+)</span>',
        grid,
    )
    marks = {kind or "none": shown for kind, shown in GEN.MARKS.items()}
    for kind, title, mark, said in shapes:  # each mark has its shape, and names its result in words
        assert (mark, said.split(": ", 1)[1]) == marks[kind] and title == said
    # the kinds of variations: each count after its shape, and all of them in words
    counted = re.findall(
        r'<td class="c f"><span class="ks" title="([^"]+)" aria-hidden="true">(.*?)</span>'
        r'<span class="sr">([^<]+)</span></td>',
        grid,
    )
    words = {shown[1]: kind for kind, shown in GEN.MARKS.items() if kind}
    for title, shown, said in counted:
        assert title == said
        pairs = re.findall(r'<span class="m (\w+)" aria-hidden="true">([^<]*)</span>(\d+)', shown)
        told = re.findall(r"(\d+) ([a-z:, ]+?)(?:, (?=\d)|$)", said.split(": ", 1)[1])
        assert [(kind, n) for kind, _, n in pairs] == [(words[w], n) for n, w in told]
        assert all(mark == GEN.MARKS[kind][0] for kind, mark, _ in pairs)
    none = sum(1 for kind, _, _, _ in shapes if kind == "none")
    assert len(shapes) + len(counted) == len(GEN.PLAIN) * len(columns)
    assert grid.count('class="m ') == len(shapes) + sum(s.count('class="m ') for _, s, _ in counted)
    assert len(shapes) - none <= len(GEN.PLAIN) * len(single)


def test_the_heading_names_the_run_it_shows():
    # the day the run started and the agent's model, as the README's run heading names them
    (h1,) = re.findall(r"(?s)<h1>(.*?)</h1>", PAGE)
    assert re.findall(r'data-fact="([a-z_]+)"', h1) == ["run_date", "model_name"]
    assert GEN.run_name() == (GEN.RUN.name[:10], GEN.model_name(GEN.facts()["model"]))


def test_a_model_id_reads_as_its_name():
    assert GEN.model_name("claude-haiku-5-5") == "Claude Haiku 5.5"
    assert GEN.model_name("claude-opus-5-5") == "Claude Opus 5.5"
    assert GEN.model_name("claude-sonnet-4-5-20250929") == "Claude Sonnet 4.5"  # dated id
    assert GEN.model_name("claude-opus-4-1") == "Claude Opus 4.1"
    assert GEN.model_name("local-model") == "Local Model"  # no version


def shooter():
    spec = importlib.util.spec_from_file_location("shoot_grid", ROOT / "tools" / "shoot_grid.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SHOOT = shooter()
PAGES = "https://theolegr.github.io/agent-audit-kit/"  # where GitHub Pages serves this page


def test_the_readme_pictures_show_the_grid_the_page_shows():
    # each picture names the hash of the HTML it was shot from: a new run, or any change to the
    # page's grid, legend or styles, makes it stale until tools/shoot_grid.py shoots it again
    assert SHOOT.stale(PAGE) == [], "the README's pictures are stale: run tools/shoot_grid.py"
    for theme in SHOOT.THEMES:
        assert SHOOT.path_of(theme).stat().st_size < 1_000_000  # the export's size cap


def test_a_change_to_the_grid_or_a_theme_makes_its_pictures_stale():
    light, dark = (str(SHOOT.path_of(t).relative_to(ROOT)) for t in ("light", "dark"))
    grid = PAGE.replace("Risks by invented user", "Risks by user", 1)
    assert sorted(SHOOT.stale(grid)) == sorted([light, dark])
    # the dark colours only: the light picture still holds
    dark_only = PAGE.replace("--met: #4CC38A", "--met: #4CC38B", 1)
    assert dark_only != PAGE and SHOOT.stale(dark_only) == [dark]


def test_a_picture_carries_one_stamp_and_reads_it_back():
    png = SHOOT.path_of("light").read_bytes()
    again = SHOOT.stamped(SHOOT.stamped(png, "a" * 64), "b" * 64)
    assert SHOOT.source_hash(again) == "b" * 64
    assert again.count(SHOOT.KEY + b"\0") == 1
    assert [kind for kind, *_ in SHOOT.chunks(again)][:2] == [b"IHDR", b"tEXt"]


def test_the_readme_opens_on_the_picture_linked_to_the_page():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    top = readme[: readme.index("\n## ")]
    # each tag on its own line, a blank line before and after: on one line, "<a ...><picture>"
    # opens no HTML block on GitHub, so the link and the picture come apart and the dark picture
    # never shows; with no blank line before, the same; with none after, the next line shows raw
    picture = re.search(r'(?s)\n\n<a href="([^"]+)">\n<picture>\n(.*?)\n</picture>\n</a>\n\n', top)
    assert picture and picture[1] == PAGES
    lines = [line.strip() for line in picture[2].splitlines()]
    assert [line.split()[0] for line in lines] == ["<source", "<img"]
    assert 'media="(prefers-color-scheme: dark)" srcset="docs/img/grid-dark.png"' in picture[2]
    assert 'src="docs/img/grid-light.png"' in picture[2]
    assert re.search(r'<img alt="[^"]{20,}"', picture[2])  # said in words for a screen reader


def test_the_readme_links_the_page():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    top = readme[: readme.index("\n## ")]
    assert f"**[The latest run and the method, in pictures]({PAGES})**" in top


def test_the_readme_carries_the_badge_of_the_tests_on_github():
    # the workflow in .github/workflows/tests.yml, which runs them on every push
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    top = readme[: readme.index("\n## ")]
    workflow = "https://github.com/theolegr/agent-audit-kit/actions/workflows/tests.yml"
    assert f"[![tests]({workflow}/badge.svg)]({workflow})" in top
