"""A PHQ-9 run built by `make_run`, replayed with no API call: a report with a verdict for every
hazard, the deferred one included."""

import json
import re

from auditkit.cli import main
from auditkit.testing import FIXED_NOW, make_run


def test_a_replayed_phq9_run_reports_a_verdict_for_every_hazard(pack, intake, tmp_path):
    trials = [
        intake("phq9-base-10"),
        intake("phq9-item9-passive", flag=False),  # fails H-01, and H-07 (escalate, no flag_risk)
        intake("phq9-adv-inject-meds"),
    ]
    case_ids = [t.case_id for t in trials]
    run = make_run(tmp_path / "runs", pack.dir.parent, "phq9", trials, case_ids=case_ids, repeats=1)
    assert main(["--replay", str(run)], clock=lambda: FIXED_NOW) == 0

    results = json.loads((run / "results.json").read_text())
    verdicts = {h["hazard_id"]: h["verdict"] for h in results["hazards"]}
    assert sorted(verdicts) == sorted(pack.hazards)
    assert verdicts["H-01"] == "not_met"  # zero tolerance: one failing case
    assert {verdicts[h] for h in ("H-09", "H-11")} == {"not_demonstrated"}  # no vague case run

    report = (run / "report.md").read_text().splitlines()
    start = next(i for i, line in enumerate(report) if line.startswith("| Hazard | Severity |"))
    end = report.index("", start)
    table = report[start + 2 : end]  # the verdict table: header, rule, one row per hazard
    assert [re.match(r"\| (H-[0-9]+) ", row).group(1) for row in table] == sorted(pack.hazards)
    assert "**not met**" in table[0]
    rows = {re.match(r"\| (H-[0-9]+) ", row).group(1): row for row in table}
    assert [hid for hid, row in rows.items() if "deferred" in row] == ["H-11"]
