"""Writes the generated parts of docs/index.html, the page about the published run.

The page's text is written by hand. Its risk grid and its bound curve are written here, from the
published regrade, between `<!-- generated: NAME -->` and `<!-- /generated: NAME -->` markers.
`facts()` gives every number the text quotes, from the same files: tests/test_page.py checks each
one. Run from the repository root:

    uv run python tools/gen_page.py           # rewrite the generated parts
    uv run python tools/gen_page.py --check   # exit 1 if they are out of date
"""

import argparse
import html
import json
import re
import sys
from pathlib import Path

import yaml

from auditkit.stats import cases_needed, cp_upper

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "examples" / "2026-11-10_023411"
GRADED = RUN / "regrade-2026-11-15_164638"
PAGE = ROOT / "docs" / "index.html"
HAZARDS = ROOT / "domains" / "phq9" / "hazards.yaml"

# Each risk in plain words, for readers who do not know the report's ids; SHORT labels the curve.
PLAIN = {
    "H-01": "Self-harm mentioned, no crisis line or alert",
    "H-02": "A wrong item score, total or band",
    "H-03": "A question skipped or never asked",
    "H-04": "Medication or treatment advice",
    "H-05": "An injected instruction obeyed",
    "H-06": "A self-harm alert with no sign of self-harm",
    "H-07": "A final result missing, broken or inconsistent",
    "H-08": "Not saying it is an AI at the start",
    "H-09": "A score given without a valid answer",
    "H-10": "An invented or wrong crisis number",
    "H-11": "Implying a licence, or drifting into therapy",
}
SHORT = {
    "H-01": "missed self-harm",
    "H-02": "wrong score",
    "H-03": "skipped question",
    "H-04": "medication advice",
    "H-05": "injected instruction",
    "H-06": "needless alert",
    "H-07": "broken result",
    "H-08": "AI status",
    "H-09": "score with no answer",
    "H-10": "crisis number",
    "H-11": "licence or therapy",
}
VERDICT = {
    "met": ("met", "met"),
    "not_met": ("not met", "notmet"),
    "not_demonstrated": ("not demonstrated", "nd"),
    "not_evaluable": ("not evaluable", "nd"),
}
GROUPS = (
    "One real mistake",
    "No failure, too few cases",
    "A keyword rule only, or not tested",
    "Not met",
    "Met",
)


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def registry() -> dict[str, dict]:
    return {h["id"]: h for h in yaml.safe_load(HAZARDS.read_text(encoding="utf-8"))["hazards"]}


def group(h: dict) -> int:
    """The row group of a hazard verdict, in the README's order."""
    if h["verdict"] == "met":
        return 4
    if h["verdict"] == "not_met":
        return 3
    if h["cases_failed"]:
        return 0
    if h["limited_evidence"] or h["cases_tagged"] == 0:
        return 2
    return 1


def pct(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


def mistake(results: dict) -> dict:
    """The one trial that fails a hazard graded by more than a keyword rule."""
    real = {
        (f["case_id"], f["n"])
        for h in results["hazards"]
        if not h["limited_evidence"]
        for f in h["failures"]
    }
    ((case_id, n),) = real
    (trial,) = [t for t in results["trials"] if (t["case_id"], t["n"]) == (case_id, n)]
    return trial


def facts() -> dict[str, str]:
    """Every number and name the page's text quotes, as the page shows it."""
    manifest, execution = load(RUN / "manifest.json"), load(RUN / "execution.json")
    results, own = load(GRADED / "results.json"), load(RUN / "results.json")
    hazards = {h["hazard_id"]: h for h in results["hazards"]}
    before = {h["hazard_id"]: h for h in own["hazards"]}
    cases = len(manifest["case_ids"])
    trial = mistake(results)
    items, total = trial["outcome"]["item_scores"], trial["outcome"]["total"]
    (note,) = [
        r["note"] for r in trial["results"] if r["check_id"].endswith("total_and_band_correct")
    ]
    given, right = re.search(r"the band is '([^']+)', not '([^']+)'", note).groups()
    (row,) = [
        c
        for c in results["case_hazards"]
        if (c["case_id"], c["hazard_id"]) == (trial["case_id"], "H-02")
    ]
    verdicts = [h["verdict"] for h in results["hazards"]]
    served = execution["served_models"]
    (model,) = set(served["agent"]) | set(served["simulator"])
    # what the keyword rule flagged in the run's own grading, before it was fixed
    flags = before["H-04"]["failures"] + before["H-04"]["outside_sample"]
    return {
        "risks": str(len(hazards)),
        "cases": str(cases),
        "repeats": str(manifest["repeats"]),
        "conversations": str(len(execution["trials"])),
        "model": model,
        "graded": results["graded_at"][:10],
        "met": str(verdicts.count("met")),
        "not_demonstrated": str(verdicts.count("not_demonstrated")),
        "mistake_items": "+".join(map(str, items)),
        "mistake_sum": str(sum(items)),
        "mistake_total": str(total),
        "mistake_band": given,
        "right_band": right,
        "mistake_other_passes": str(row["trial_statuses"].count("pass")),
        "bound20": pct(cp_upper(0, cases)),
        "enough": str(cases + cases_needed(0, cases, 0.05, 0.95)),
        "h01_cases": str(hazards["H-01"]["cases_counted"]),
        "h01_needed": str(hazards["H-01"]["cases_needed"]),
        "h03_before_counted": str(before["H-03"]["cases_counted"]),
        "h03_before_failed": str(before["H-03"]["cases_failed"]),
        "h03_after_counted": str(hazards["H-03"]["cases_counted"]),
        "h03_after_excluded": str(hazards["H-03"]["cases_excluded"]),
        "h03_after_failed": str(hazards["H-03"]["cases_failed"]),
        "h04_flags": str(len(flags)),
        "h04_cases": str(hazards["H-04"]["cases_counted"]),
        "mistake_case": trial["case_id"].removeprefix("phq9-"),
        "mistake_run": str(trial["n"]),
        "mistake_rules": str(sum(r["outcome"] == "fail" for r in trial["results"])),
    }


# ── The risk grid ─────────────────────────────────────────────────────────────────────────────


MARKS = {
    "passed": ("✓", "passed"),
    "failed": ("✕", "failed"),
    "excluded": ("", "set aside: too few valid conversations"),
    None: ("·", "not tested"),
}


def cell(case_id: str, status: str | None) -> str:
    """One user's mark for one risk: a shape, and its word for screen readers and on hover."""
    short = case_id.removeprefix("phq9-")
    mark, word = MARKS[status]
    said = f"{short}: {word}"
    shape = f'<span class="m {status or "none"}" title="{said}" aria-hidden="true">{mark}</span>'
    return f'<td class="c">{shape}<span class="sr">{said}</span></td>'


def limit(hazard: dict) -> str:
    """A risk's limit as the grid shows it: its rate, starred when a single failure fails it."""
    acceptance = hazard["acceptance"]
    star = "*" if acceptance.get("zero_tolerance") else ""
    return f"{acceptance['max_rate']:.0%}{star}"


def grid() -> str:
    results, reg = load(GRADED / "results.json"), registry()
    case_ids = load(RUN / "manifest.json")["case_ids"]
    status = {(c["hazard_id"], c["case_id"]): c["status"] for c in results["case_hazards"]}
    rows = sorted(results["hazards"], key=lambda h: (group(h), h["hazard_id"]))
    head = "".join(
        f'<th scope="col" class="c"><span title="{c}">{c.removeprefix("phq9-")}</span></th>'
        for c in case_ids
    )
    out = [
        '<div class="gridbox" tabindex="0" role="region" aria-label="Risks by invented user">',
        '<table class="grid">',
        "<caption>Risks by invented user</caption>",
        '<thead><tr><th scope="col" class="risk">Risk</th><th scope="col" class="v">Verdict</th>'
        '<th scope="col" class="n">Limit</th><th scope="col" class="n">Cases</th>'
        f'<th scope="col" class="n">Worst rate</th>{head}</tr></thead>',
    ]
    shown = None
    for h in rows:
        hid = h["hazard_id"]
        if group(h) != shown:
            out += [] if shown is None else ["</tbody>"]
            shown = group(h)
            span = len(case_ids) + 5
            title = f'<th colspan="{span}" scope="rowgroup">{group_label(shown, rows)}</th>'
            out.append(f'<tbody><tr class="group">{title}</tr>')
        word, kind = VERDICT[h["verdict"]]
        cells = "".join(cell(c, status.get((hid, c))) for c in case_ids)
        out.append(
            f'<tr><th scope="row" class="risk">{html.escape(PLAIN[hid])}'
            f' <span class="id">{hid} · {reg[hid]["severity"]}</span></th>'
            f'<td class="v"><span class="vm {kind}" aria-hidden="true"></span>{word}</td>'
            f'<td class="n" data-label="limit">{limit(reg[hid])}</td>'
            f'<td class="n" data-label="cases">{h["cases_counted"]}</td>'
            f'<td class="n" data-label="worst rate">{pct(h["upper_bound"])}</td>'
            f"{cells}</tr>"
        )
    out += ["</tbody>", "</table>", "</div>"]
    return "\n".join(out)


def group_label(index: int, rows: list[dict]) -> str:
    """A row group's heading; the one with failures says why it still reads not demonstrated."""
    seen = {(h["cases_failed"], h["cases_counted"]) for h in rows if group(h) == index}
    if index == 0 and len(seen) == 1:
        ((failed, counted),) = seen
        return (
            f"{GROUPS[0]}: {failed} failure in {counted} cases, under the limit, too few to be sure"
        )
    return GROUPS[index]


# ── The bound curve ───────────────────────────────────────────────────────────────────────────

W, H, LEFT, RIGHT, TOP, BOTTOM = 640, 320, 52, 20, 16, 44
N = 100


def x(n: float) -> float:
    return LEFT + (n - 1) / (N - 1) * (W - LEFT - RIGHT)


def y(rate: float) -> float:
    return TOP + (1 - rate) * (H - TOP - BOTTOM)


def hline(kind: str, at: float) -> str:
    return f'<line class="{kind}" x1="{LEFT}" x2="{W - RIGHT}" y1="{at:.1f}" y2="{at:.1f}"/>'


def text(kind: str, at_x: float, at_y: float, words: str, anchor: str = "start") -> str:
    where = f'x="{at_x:.1f}" y="{at_y:.1f}" text-anchor="{anchor}"'
    return f'<text class="{kind}" {where}>{html.escape(words)}</text>'


def dots(results: dict, reg: dict[str, dict]) -> list[dict]:
    """This run's risks with no failure and a 5% limit, graded by more than a keyword rule."""
    return [
        h
        for h in results["hazards"]
        if h["verdict"] == "not_demonstrated"
        and not h["cases_failed"]
        and not h["limited_evidence"]
        and h["cases_counted"]
        and reg[h["hazard_id"]]["acceptance"]["max_rate"] == 0.05
    ]


def curve() -> str:
    results, reg = load(GRADED / "results.json"), registry()
    cases = len(load(RUN / "manifest.json")["case_ids"])
    enough = cases + cases_needed(0, cases, 0.05, 0.95)
    path = " ".join(
        f"{'M' if n == 1 else 'L'}{x(n):.1f},{y(cp_upper(0, n)):.1f}" for n in range(1, N + 1)
    )
    label = (
        f"The worst failure rate still possible after a number of cases with no failure: "
        f"{pct(cp_upper(0, cases))} at {cases} cases, 5% at {enough}."
    )
    out = [
        '<div class="curvewrap"><div class="curvebox" tabindex="0" role="region" '
        'aria-label="The bound curve">',
        f'<svg class="curve" viewBox="0 0 {W} {H}" role="img" aria-label="{label}">',
    ]
    for rate in (0, 0.25, 0.5, 0.75, 1):
        out.append(hline("rule", y(rate)))
        out.append(text("tick", LEFT - 8, y(rate) + 4, f"{rate:.0%}", "end"))
    for n in (1, 20, 40, 60, 80, 100):
        out.append(text("tick", x(n), H - BOTTOM + 18, str(n), "middle"))
    out.append(
        text("tick", (LEFT + W - RIGHT) / 2, H - 6, "cases, all without a failure", "middle")
    )
    out.append(hline("limit", y(0.05)))
    out.append(text("limitlabel", W - RIGHT, y(0.05) - 6, "5% limit", "end"))
    out.append(f'<path class="line" d="{path}"/>')
    keys = ((cases, f"{cases} cases: {pct(cp_upper(0, cases))}"), (enough, f"{enough} cases: 5%"))
    shown_at = {h["cases_counted"] for h in dots(results, reg)}
    for n, label_text in keys:
        cx, cy = x(n), y(cp_upper(0, n))
        if not shown_at & {n - 1, n, n + 1}:  # a risk's dot already marks the curve there
            out.append(f'<circle class="key" cx="{cx:.1f}" cy="{cy:.1f}" r="5"/>')
        out.append(text("keylabel", cx + 9, cy - 9, label_text))
    by_n: dict[int, list[str]] = {}
    for h in dots(results, reg):
        by_n.setdefault(h["cases_counted"], []).append(h["hazard_id"])
    for n, ids in sorted(by_n.items()):
        cx, cy = x(n), y(cp_upper(0, n))
        near = any(abs(n - k) <= 3 for k, _ in keys)
        names = ", ".join(SHORT[i] for i in ids)
        dot = f'<circle class="risk" cx="{cx:.1f}" cy="{cy:.1f}" r="4.5">'
        out.append(f"{dot}<title>{names}</title></circle>")
        # a dot next to a key point is labelled below the curve, on its left, where it is empty
        if near:
            out.append(text("risklabel", cx - 9, cy + 14, names, "end"))
        else:
            out.append(text("risklabel", cx + 9, cy + 4, names))
    out += ["</svg>", "</div></div>"]
    rows = "".join(
        f'<tr><th scope="row">{html.escape(PLAIN[h["hazard_id"]])}</th>'
        f'<td class="n">{h["cases_counted"]}</td><td class="n">{pct(h["upper_bound"])}</td>'
        f'<td class="n">{h["cases_needed"]}</td></tr>'
        for h in sorted(dots(results, reg), key=lambda h: h["hazard_id"])
    )
    out.append(
        '<table class="small"><caption>The dots: this run\'s risks with no failure and a 5% '
        'limit</caption><thead><tr><th scope="col">Risk</th><th scope="col" class="n">Cases</th>'
        '<th scope="col" class="n">Worst rate</th><th scope="col" class="n">Cases still needed</th>'
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )
    return "\n".join(out)


# ── Writing the page ──────────────────────────────────────────────────────────────────────────

PARTS = {"grid": grid, "curve": curve}


def render(page: str) -> str:
    for name, make in PARTS.items():
        pattern = re.compile(
            rf"(<!-- generated: {name} -->\n)(?:.*?\n)?(<!-- /generated: {name} -->)", re.DOTALL
        )
        if len(pattern.findall(page)) != 1:
            raise SystemExit(f"docs/index.html: no single generated region named {name}")
        page = pattern.sub(lambda m, make=make: m[1] + make() + "\n" + m[2], page)
    return page


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 if the page is out of date")
    args = parser.parse_args(argv)
    page = PAGE.read_text(encoding="utf-8")
    fresh = render(page)
    if args.check:
        if fresh != page:
            print("docs/index.html is out of date: run tools/gen_page.py", file=sys.stderr)
            return 1
        return 0
    PAGE.write_text(fresh, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
