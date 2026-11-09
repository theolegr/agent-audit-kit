"""The risk-based evaluation report: `Results`, the pack and the run's record → `report.md`.

Text the system under test controls (transcript turns, check notes, error messages) goes through
`inline`: one line, Markdown escaped, so that it can never change the report's structure. Pack text
goes through it too: a stray `|` would break a table. Wording: verdicts are met / not met / not
demonstrated / not evaluable, against the criterion declared before the run; when the run's criteria
are not confirmed, against the criterion as read from the run's snapshot by this harness, under a
"Criteria not confirmed" banner; in a `--regrade-current` report, against the current pack's
criterion. The harness is named by its version and git SHA only.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Literal

from auditkit.grade import run_exposes
from auditkit.pack import Pack
from auditkit.runfolder import acceptance_sha256, criteria_sha256, older_acceptance_fields
from auditkit.schema import (
    Acceptance,
    EvidenceRef,
    Execution,
    Hazard,
    HazardVerdict,
    Results,
    RunManifest,
    Trial,
    TrialGrade,
)
from auditkit.stats import cases_needed, percent

# Markdown syntax that acts in the middle of a line (`>` and `#` only start one: never escaped).
# `_` between two letters or digits cannot open emphasis, so `snake_case` stays as written.
ESCAPED = frozenset("\\`*_[]<|~$&")
EXCERPT_LIMIT = 400  # characters of a transcript turn in an excerpt
NOTE_LIMIT = 300  # characters of a note, a reason or an error
TOP_FINDINGS = 3
CHANNELS = ("final_outcome", "ground_truth", "side_channel", "tool_calls", "turn_text")
WHAT_THIS_IS_NOT = [
    "> **What this is not.** Not a certification, not a conformity assessment, not a medical "
    "device.",
    "> All personas and data are synthetic.",
]
CANNOT_SHOW = "> **What this evidence cannot show.**"
TIMES, TO = "\N{MULTIPLICATION SIGN}", "\N{EN DASH}"  # "3 cases TIMES 2 repeats", "2.5%TO100%"
SHOWN = frozenset("LMNPS")  # the visible classes: letters, marks, numbers, punctuation, symbols
# Characters of those classes that still render as nothing: Unicode's Default_Ignorable_Code_Point
# outside categories C and Z, and the blank Braille pattern.
BLANK = (
    (0x034F, 0x034F),  # combining grapheme joiner
    (0x115F, 0x1160),  # Hangul fillers
    (0x17B4, 0x17B5),  # Khmer inherent vowels
    (0x180B, 0x180F),  # Mongolian free variation selectors
    (0x2800, 0x2800),  # Braille pattern blank
    (0x3164, 0x3164),  # Hangul filler
    (0xFE00, 0xFE0F),  # variation selectors
    (0xFFA0, 0xFFA0),  # halfwidth Hangul filler
    (0xE0100, 0xE01EF),  # variation selectors supplement
)


def _shown(text: str, i: int) -> bool:
    """Whether `text[i]` may appear as itself (an allowlist): a visible character, not one that
    renders as nothing, and not a combining mark with no letter of the text to sit on."""
    ch, code = text[i], ord(text[i])
    kind = unicodedata.category(ch)[0]
    if kind not in SHOWN or any(low <= code <= high for low, high in BLANK):
        return False
    return kind != "M" or (i > 0 and text[i - 1] != " ")


def inline(text: str, limit: int | None = None) -> str:
    """`text` as one line of Markdown that shows it as written. Whitespace runs, newlines included,
    become one space; Markdown syntax is backslash-escaped; any character not on the allowlist (an
    invisible, blank or control one) shows as [U+XXXX]; past `limit` characters, the text is cut
    and ends with an ellipsis."""
    flat = " ".join(text.split())
    if limit is not None and len(flat) > limit:
        flat = flat[: limit - 1].rstrip() + "…"
    out = []
    for i, ch in enumerate(flat):
        intraword = 0 < i < len(flat) - 1 and flat[i - 1].isalnum() and flat[i + 1].isalnum()
        if ch in ESCAPED and not (ch == "_" and intraword):
            out.append(f"\\{ch}")
        elif ch == " " or _shown(flat, i):
            out.append(ch)
        else:
            out.append(f"\\[U+{ord(ch):04X}\\]")
    return "".join(out)


def _tool(name: str) -> str:
    """A tool-call name, which the agent writes: a code span when it is an identifier (escaped text
    cannot make one), otherwise every other character as [U+XXXX]."""
    if name and all(c.isascii() and (c.isalnum() or c in "_.-") for c in name):
        return f"`{name}`"
    return "".join(
        c if c.isascii() and c.isalnum() else f"\\[U+{ord(c):04X}\\]" for c in name[:NOTE_LIMIT]
    )


def _rate(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _words(verdict: str) -> str:
    return verdict.replace("_", " ")


def _time(moment: datetime | None) -> str:
    return "—" if moment is None else moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _usd(value: float) -> str:
    return f"${value:.4f}" if 0 < value < 0.01 else f"${value:.2f}"


def _short(sha: str | None) -> str:
    return "unknown" if sha is None else inline(sha[:12])


def _harness(version: str, sha: str | None) -> str:
    return f"{inline(version)} ({'git SHA unknown' if sha is None else f'git {_short(sha)}'})"


def _ids(ids: Iterable[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids)


def _said(note: str) -> str:
    """A check's note, which may quote the agent: always last on its line, after the report's own
    words, so it cannot pass for them."""
    return f" Note: {inline(note, NOTE_LIMIT)}" if note.strip() else ""


def _table(header: Sequence[str], rows: Iterable[Sequence[str]]) -> list[str]:
    """A GFM table. Every cell is already escaped (`inline`) or built from validated ids."""
    lines = [f"| {' | '.join(header)} |", f"|{'---|' * len(header)}"]
    lines += [f"| {' | '.join(row)} |" for row in rows]
    return [*lines, ""]


def criterion(acc: Acceptance) -> str:
    """An acceptance criterion in words: "≤ 10% (95% upper bound), ≥ 10 cases"."""
    parts = [f"≤ {percent(acc.max_rate)} ({percent(acc.confidence)} upper bound)"]
    if acc.zero_tolerance:
        parts.append("zero tolerance")
    parts.append(f"≥ {acc.min_cases} cases")
    if acc.min_valid_trials_per_case > 1:
        parts.append(f"{acc.min_valid_trials_per_case} valid trials per case")
    return ", ".join(parts)


def _needed(hazard: Hazard) -> int | None:
    """Failure-free cases the hazard's criterion needs to be met; None: over 10,000."""
    acc = hazard.acceptance
    need = cases_needed(0, 0, acc.max_rate, acc.confidence)
    return None if need is None else max(need, acc.min_cases)


def render(
    results: Results,
    pack: Pack,
    manifest: RunManifest,
    execution: Execution,
    trials: Mapping[tuple[str, int], Trial],
    *,
    graded_by: Literal["run", "replay"],
    declared: Mapping[str, Acceptance] | None = None,
) -> str:
    """The report. `trials`: the graded trials by (case, n), for excerpts and stimulus delivery.
    `declared`: the criteria the run declared, per hazard (`runfolder.declared_acceptance`), which
    a grading against the current pack files compares with its own."""
    report = _Report(results, pack, manifest, execution, trials, graded_by, declared)
    lines = [*report.page_one(), *report.body(), *report.appendix()]
    return "\n".join(lines).rstrip() + "\n"


class _Report:
    def __init__(
        self,
        results: Results,
        pack: Pack,
        manifest: RunManifest,
        execution: Execution,
        trials: Mapping[tuple[str, int], Trial],
        graded_by: Literal["run", "replay"],
        declared: Mapping[str, Acceptance] | None,
    ) -> None:
        self.results, self.pack, self.manifest, self.execution = results, pack, manifest, execution
        self.trials, self.graded_by, self.declared = trials, graded_by, declared
        # graded against the current pack files: its criteria are not the ones declared before
        self.current = results.graded_against == "current"
        self.acceptance_sha256 = acceptance_sha256(pack.hazards)  # the criteria graded
        # the declared criteria as read here, when they can be, against the manifest's hash: the
        # Acceptance fields the model of the harness that wrote it lacked, () if none; unconfirmed
        # when no such fields explain it (a default may have changed since the run)
        self.declared_sha256, self.lacked = None, None
        if declared is not None:
            self.declared_sha256 = criteria_sha256(declared)
            self.lacked = older_acceptance_fields(declared, manifest.acceptance_sha256)
        self.unconfirmed = declared is not None and self.lacked is None
        # the criteria graded, as the verdict lines and the hazard table name them: unconfirmed
        # ones may not be what the run declared, only what this harness reads from its snapshot
        if self.current:
            self.criterion = "the current pack's criterion"
            self.criteria_from = "current pack files"
        elif self.unconfirmed:
            self.criterion = "the criterion as read from the run's snapshot by this harness"
            self.criteria_from = "as read from the run's snapshot"
        else:
            self.criterion = "the criterion declared before the run"
            self.criteria_from = "declared before the run"
        self.target = pack.targets[manifest.target_name]
        self.hazards = dict(sorted(pack.hazards.items()))
        self.verdicts = {v.hazard_id: v for v in results.hazards}
        self.specs = {c.id: c for c in results.checks}
        self.grades = {(g.case_id, g.n): g for g in results.trials}
        self.case_stats = {c.case_id: c for c in results.cases}
        self.rank = {s.name: s.rank for s in pack.config.severity_scale}
        self.exposes = run_exposes(self.target, list(trials.values()))
        self.served = {role: ids for role, ids in execution.served_models.items() if ids}

    # ── Page 1 ────────────────────────────────────────────────────────────────────────────────────

    def page_one(self) -> list[str]:
        config = self.pack.config
        lines = [
            f"# Risk-based evaluation report — {inline(config.title)} {inline(config.version)}, "
            f"target `{self.target.name}`",
            "",
            *self._configuration(),
            "",
        ]
        for title, text in self._banners():
            lines += [f"**⚠ {title}.** {text}", ""]
        lines += [*WHAT_THIS_IS_NOT, f"> {inline(config.disclaimer)}", ""]
        out_of_scope = "; ".join(inline(item) for item in config.out_of_scope)
        lines += [
            f"**Intended use.** {inline(config.intended_use)} Out of scope: {out_of_scope}.",
            "",
        ]
        lines += self._verdict_table()
        lines += ["**Top findings**", ""]
        top = sorted(self._findings(), key=lambda f: f[0])[:TOP_FINDINGS]
        if not top and not any(g.status != "not_run" for g in self.results.trials):
            lines += ["No trial ran to an end: this run holds no evidence about the agent.", ""]
        elif not top:
            lines += [
                "No check failed or errored, no trial was invalid, and every control has a check.",
                "",
            ]
        for i, (_, text, excerpt) in enumerate(top, 1):
            lines += [f"{i}. {text}", *(f"   {line}" for line in excerpt), ""]
        lines += [CANNOT_SHOW, *(f"> {s}" for s in self._cannot_show()), ""]
        return lines

    def _configuration(self) -> list[str]:
        m, x, r = self.manifest, self.execution, self.results
        git = self._git(m.harness_git_sha, m.harness_git_dirty)
        harness = f"harness {inline(m.harness_version)} ({git})"
        models = " · ".join(
            f"{info.role}: {inline(info.env_var)} {inline(info.requested)} → served "
            f"{', '.join(inline(s) for s in self.served.get(info.role, [])) or 'not recorded'}"
            for info in m.models
        )
        statuses = Counter(g.status for g in r.trials)
        planned = len(m.case_ids) * m.repeats
        trials = (
            f"{len(m.case_ids)} cases {TIMES} {m.repeats} repeats = {planned} trials: "
            f"{statuses['valid']} counted, {statuses['invalid']} invalid, {statuses['error']} "
            f"error, {statuses['not_run']} not run"
        )
        if missing := planned - len(r.trials):
            trials += f", {missing} without a transcript"
        declared = f"sha256 {_short(m.acceptance_sha256)}, declared {_time(m.created_at)}"
        if x.first_call_at is None:
            declared += "; no model call recorded"
        elif m.created_at <= x.first_call_at:
            declared += f", before the first model call at {_time(x.first_call_at)}"
        else:
            declared += f", after the first model call at {_time(x.first_call_at)}"
        if self.current:
            declared = (
                f"graded with sha256 {_short(self.acceptance_sha256)}, from the current pack "
                f"files; the run's: {declared}"
            )
        return [
            f"- **Run** {inline(m.run_id)} · {harness} · pack {inline(m.pack_name)} "
            f"{inline(m.pack_version)}, variant {inline(m.variant)}, sha256 "
            f"{_short(m.pack_sha256)} · target sha256 {_short(m.target_sha256)}",
            f"- **Models** {models}",
            f"- **Trials** {trials}",
            f"- **Cost** {_usd(x.spent_usd)} of a {_usd(m.budget_usd)} cap (estimate "
            f"{_usd(m.estimate_usd)}, prices as of {inline(m.prices_as_of)})",
            f"- **Acceptance criteria** {declared}",
            f"- **Graded** {self._graded()}",
        ]

    def _git(self, sha: str | None, dirty: bool | None) -> str:
        if sha is None:
            return "git SHA unknown"
        state = "dirty" if dirty else "clean" if dirty is False else "state unknown"
        return f"git {_short(sha)}, {state}"

    def _graded(self) -> str:
        m, r = self.manifest, self.results
        when = f"at {_time(r.graded_at)}"
        if self.graded_by == "run":
            how = f"at the end of the run, {when}"
        elif r.graded_against == "snapshot":
            how = f"by replay {when}, from the run's snapshot (sha256 {_short(r.pack_sha256)})"
        else:
            how = (
                f"by replay {when}, against the current pack files (sha256 "
                f"{_short(r.pack_sha256)}), not the run's snapshot"
            )
        return (
            f"{how}; harness {_harness(r.harness_version, r.harness_git_sha)}, run made with "
            f"{_harness(m.harness_version, m.harness_git_sha)}"
        )

    def _banners(self) -> list[tuple[str, str]]:
        m, x, r = self.manifest, self.execution, self.results
        out = []
        statuses = Counter(g.status for g in r.trials)
        planned = len(m.case_ids) * m.repeats
        ended = statuses["valid"] + statuses["invalid"] + statuses["error"]
        if x.status != "completed" or statuses["not_run"] or len(r.trials) < planned:
            out.append(
                (
                    "Partial run",
                    f"Execution status {x.status}: {ended} of {planned} planned trials ran to an "
                    "end; the verdicts use those only.",
                )
            )
        if self.current:
            out.append(
                (
                    "Graded against the current pack files",
                    "Not the run's snapshot: the hazards, checks and criteria are those of the "
                    "current files, which may differ from the run's own. The run's own report is "
                    "in the run folder.",
                )
            )
        elif self.graded_by == "replay":
            out.append(
                (
                    "Re-graded",
                    f"Rebuilt by replay at {_time(r.graded_at)} from the saved transcripts and the "
                    "run's snapshot, with no model call; earlier gradings are kept in history/.",
                )
            )
        changed = self._changed_criteria()
        # per hazard when the declared criteria can be read; the manifest's hash, which depends on
        # the Acceptance model of the harness that wrote it, only when they cannot
        if changed if self.declared is not None else self.acceptance_sha256 != m.acceptance_sha256:
            which = f": {', '.join(changed)}" if changed else ""
            graded, run = _short(self.acceptance_sha256), _short(m.acceptance_sha256)
            out.append(
                (
                    "Criteria changed",
                    f"The acceptance criteria graded here (sha256 {graded}) are not those the run "
                    f"declared (sha256 {run}){which}. The verdicts below hold against the criteria "
                    "graded here only.",
                )
            )
        if self.unconfirmed:
            read, run = _short(self.declared_sha256), _short(m.acceptance_sha256)
            # graded against the current files, the verdicts do not hold against the run's
            # criteria (the banner above says so): only the comparison with them does
            held = (
                "Whether the criteria changed is judged against the run's criteria as this "
                "harness reads them."
                if self.current
                else "The verdicts below hold against the criteria as this harness reads them."
            )
            out.append(
                (
                    "Criteria not confirmed",
                    f"The run's criteria, as this harness reads them from its snapshot (sha256 "
                    f"{read}), do not hash to the sha256 its manifest declared ({run}): that hash "
                    "came from another Acceptance model, and a default it used may differ from "
                    f"today's. {held}",
                )
            )
        same_version = r.harness_version == m.harness_version
        if not same_version or r.harness_git_sha != m.harness_git_sha:
            out.append(
                (
                    "Harness changed",
                    f"The run was made with harness "
                    f"{_harness(m.harness_version, m.harness_git_sha)} and graded with "
                    f"{_harness(r.harness_version, r.harness_git_sha)}: the core checks are "
                    "identified by that version and SHA.",
                )
            )
        for role, ids in sorted(self.served.items()):
            if len(ids) > 1:
                served = ", ".join(inline(i) for i in ids)
                text = f"{role}: {served}. The trials ran on different model versions."
                out.append(("More than one served model", text))
        voided = statuses["invalid"] + statuses["error"]
        if ended and voided / ended > 0.2:
            out.append(
                (
                    "High invalid rate",
                    f"{voided} of {ended} trials ({voided / ended:.0%}) do not count (invalid or "
                    "error): see Test-data quality.",
                )
            )
        if not r.expectations_match:
            out.append(
                (
                    "Expectations differ",
                    "The expectations recomputed for this grading differ from expectations.json, "
                    "the oracle recorded when the run started.",
                )
            )
        if x.first_call_at is not None and m.created_at > x.first_call_at:
            out.append(
                (
                    "Criteria declared late",
                    f"The manifest was written at {_time(m.created_at)}, after the first model "
                    f"call at {_time(x.first_call_at)}.",
                )
            )
        return out

    def _verdict_table(self) -> list[str]:
        rows = []
        for hid, h in self.hazards.items():
            v = self.verdicts[hid]
            rows.append(
                [
                    f"{hid} {inline(h.title)}",
                    inline(h.severity),
                    criterion(h.acceptance),
                    f"{v.cases_counted} / {v.cases_tagged}",
                    str(v.cases_failed) if v.cases_counted else "—",
                    _rate(v.observed_rate),
                    _rate(v.upper_bound),
                    self._verdict(v),
                ]
            )
        header = [
            "Hazard",
            "Severity",
            f"Criterion ({self.criteria_from})",
            "Cases counted / tagged",
            "Failed",
            "Observed rate",
            "Upper bound",
            "Verdict",
        ]
        lines = _table(header, rows)
        variant = self.pack.config.variants.get(self.pack.variant)
        if variant is not None and variant.exclude_hazards:
            excluded = ", ".join(variant.exclude_hazards)
            lines += [f"Not applicable to variant {inline(self.pack.variant)}: {excluded}.", ""]
        return lines

    def _changed_criteria(self) -> list[str]:
        """The hazards whose criterion differs from the one the run declared (added or removed
        ones included); empty when the declared criteria could not be read."""
        if self.declared is None:
            return []
        graded = {hid: h.acceptance for hid, h in self.hazards.items()}
        hazards = set(graded) | set(self.declared)
        return sorted(hid for hid in hazards if graded.get(hid) != self.declared.get(hid))

    def _verdict(self, v: HazardVerdict) -> str:
        reasons = "; ".join(inline(reason) for reason in v.reasons)
        return f"**{_words(v.verdict)}**" + (f" — {reasons}" if reasons else "")

    def _severity(self, hazard_id: str) -> int:
        return self.rank.get(self.hazards[hazard_id].severity, 0)

    def _ref(self, ref: EvidenceRef) -> str:
        return f"`{ref.case_id}`, run {ref.n}, turn {ref.turn}: `{ref.check_id}`"

    def _linked(self, check_id: str) -> str:
        """The hazards a check links, after its id: " (H-01, H-03)"; nothing for a validity one."""
        spec = self.specs.get(check_id)
        return f" ({', '.join(spec.hazards)})" if spec is not None and spec.hazards else ""

    def _findings(self) -> list[tuple[tuple, str, list[str]]]:
        """Every finding as (rank key, text, excerpt lines): counted failures by severity, then
        failures shown but not counted, errors, invalid trials and gaps. On every line the report's
        own words come first and the text it quotes (notes, reasons) last."""
        out: list[tuple[tuple, str, list[str]]] = []
        for hid, h in self.hazards.items():
            v, title, sev = self.verdicts[hid], f"{hid} {inline(h.title)}", self._severity(hid)
            if v.failures:
                ref = v.failures[0]
                text = (
                    f"**{title}: {_words(v.verdict)}** — {self._ref(ref)} failed (fault: agent); "
                    f"{v.cases_failed} of {v.cases_counted} counted case(s) failed."
                    f"{self._instability(ref.case_id)}{_said(ref.note)}"
                )
                out.append(((0, -sev, -v.cases_failed, hid), text, self._excerpt(ref)))
            if v.outside_sample:
                ref = v.outside_sample[0]
                text = (
                    f"**{title}: outside the declared sample, not counted** — `{ref.case_id}` is "
                    f"not tagged {hid}; {self._ref(ref)} failed.{_said(ref.note)}"
                )
                out.append(((1, -sev, hid), text, self._excerpt(ref)))
            if v.invalid_trial_findings:
                ref = v.invalid_trial_findings[0]
                status = self.grades[(ref.case_id, ref.n)].status
                text = (
                    f"**{title}: in a trial that does not count** — {self._ref(ref)} failed in an "
                    f"{status} trial.{_said(ref.note)}"
                )
                out.append(((2, -sev, hid), text, self._excerpt(ref)))
        for g in self.results.trials:
            where = f"`{g.case_id}`, run {g.n}"
            if g.status == "error":  # its reasons name the checks that errored
                text = f"**Trial error**, not counted — {where}: {self._reasons(g)}."
                out.append(((3, g.case_id, g.n), text, []))
            else:
                for r in g.results:
                    if r.outcome == "error":
                        check = f"`{r.check_id}`{self._linked(r.check_id)}"
                        text = f"**Harness error** — {where}: {check} errored ({r.fault})."
                        text += _said(r.note)
                        out.append(((3, g.case_id, g.n, r.check_id), text, []))
            if g.status == "invalid":
                text = f"**Test data** — {where}, invalid (not counted): {self._reasons(g)}."
                out.append(((4, g.case_id, g.n), text, []))
            elif g.status == "valid":
                for r in g.results:
                    if r.outcome == "invalid":
                        text = (
                            f"**Test data** — {where}: `{r.check_id}`{self._linked(r.check_id)} "
                            f"invalid (fault: {r.fault}).{_said(r.note)}"
                        )
                        out.append(((4, g.case_id, g.n, r.check_id), text, []))
        out += [
            ((5, -self._severity(hid), hid, i), text, [])
            for i, (hid, text) in enumerate(self._gaps())
        ]
        return out

    def _instability(self, case_id: str) -> str:
        stats = self.case_stats.get(case_id)
        if stats is None or not stats.mixed or stats.pass_at_1 is None:
            return ""
        hat = "—" if stats.pass_hat_k is None else f"{stats.pass_hat_k:.2f}"
        return (
            f" Unstable: {stats.passed_trials} of "
            f"{stats.valid_trials - stats.unscored_trials} scored trials passed "
            f"(pass@1 {stats.pass_at_1:.2f}, pass^{stats.k} {hat})."
        )

    def _reasons(self, grade: TrialGrade) -> str:
        return (
            "; ".join(inline(reason, NOTE_LIMIT) for reason in grade.reasons)
            or "no reason recorded"
        )

    def _excerpt(self, ref: EvidenceRef) -> list[str]:
        """The evidence turn as blockquote lines; an agent turn comes with the user turn it
        answers. The label is the report's (bold, tool names in code spans: escaped text can make
        neither); the turn's text comes last, so it cannot pass for the label."""
        trial = self.trials.get((ref.case_id, ref.n))
        if trial is None or not 0 <= ref.turn < len(trial.transcript.turns):
            return []
        turns = trial.transcript.turns
        shown = [turns[ref.turn]]
        if shown[0].role == "agent" and ref.turn > 0 and turns[ref.turn - 1].role == "user":
            shown.insert(0, turns[ref.turn - 1])
        lines = []
        for turn in shown:
            label = f"**turn {turn.index} ({turn.role})**"
            if turn.tool_calls:
                label += f", tool calls {', '.join(_tool(call.name) for call in turn.tool_calls)}"
            said = f'"{inline(turn.text, EXCERPT_LIMIT)}"' if turn.text.strip() else "no text"
            lines.append(f"> {label}: {said}")
        return lines

    def _cannot_show(self) -> list[str]:
        out = []
        assessed = [hid for hid, h in self.hazards.items() if not h.deferred_reason]
        if assessed:
            counted = sorted(self.verdicts[hid].cases_counted for hid in assessed)
            span = f"{counted[0]}" + (f"{TO}{counted[-1]}" if counted[-1] != counted[0] else "")
            needs = {hid: _needed(self.hazards[hid]) for hid in assessed}
            # the most demanding criterion; on a tie, the most severe hazard, then the first id
            hid = sorted(
                assessed,
                key=lambda h: (needs[h] is None, needs[h] or 0, self._severity(h)),
                reverse=True,
            )[0]
            need = "over 10,000" if needs[hid] is None else f"at least {needs[hid]}"
            rate = percent(self.hazards[hid].acceptance.max_rate)
            out.append(
                f"This run counted {span} distinct case(s) per hazard; {hid}, at ≤ {rate}, needs "
                f"{need} failure-free cases to be met."
            )
        heuristic = sorted(
            c.id for c in self.results.checks if c.method == "heuristic" and c.kind != "validity"
        )
        if heuristic:
            verb = "is" if len(heuristic) == 1 else "are"
            out.append(
                f'{_ids(heuristic)} {verb} heuristic, with unknown recall: "not detected" is not '
                '"absent".'
            )
        if self.manifest.repeats == 1:
            out.append("One trial per case: instability across repeats is not measured.")
        if self.pack.config.side_channel:
            keys = ", ".join(inline(k) for k in self.pack.config.side_channel)
            out.append(
                f"What the simulated user meant ({keys}) is read from the simulator's own notes, "
                "not checked independently."
            )
        out.append("Designed, synthetic personas are not a field sample.")
        if hidden := [ch for ch in CHANNELS if ch not in self.exposes]:
            unevaluable = sorted({c for v in self.results.hazards for c in v.unevaluable_checks})
            out.append(
                f"The target does not expose {', '.join(hidden)}"
                + (f": {_ids(unevaluable)} could not be evaluated." if unevaluable else ".")
            )
        out.append("An observed failure rate under test conditions is not a probability of harm.")
        if self.served:
            out.append("Results hold for the served model ids above only.")
        else:
            out.append("Results hold for this target as run only: no served model id was recorded.")
        return out

    # ── Body ──────────────────────────────────────────────────────────────────────────────────────

    def body(self) -> list[str]:
        return [
            *self._severity_view(),
            *self._hazard_sections(),
            *self._case_section(),
            *self._check_section(),
            *self._findings_section(),
            *self._test_data_section(),
            *self._limitations(),
        ]

    def _severity_view(self) -> list[str]:
        order = sorted(self.hazards, key=lambda hid: (-self._severity(hid), hid))
        rows = [
            [
                inline(self.hazards[hid].severity),
                str(self._severity(hid)),
                f"{hid} {inline(self.hazards[hid].title)}",
                _rate(self.verdicts[hid].upper_bound),
                f"**{_words(self.verdicts[hid].verdict)}**",
            ]
            for hid in order
        ]
        return [
            "## Severity and upper bound",
            "",
            *_table(["Severity", "Rank", "Hazard", "Upper bound", "Verdict"], rows),
        ]

    def _hazard_sections(self) -> list[str]:
        lines = ["## Hazards", ""]
        for hid, h in self.hazards.items():
            v = self.verdicts[hid]
            linked = [c for c in self.results.checks if hid in c.hazards]
            lines += [
                f"### {hid} {inline(h.title)}",
                "",
                f"{self._verdict(v)}, against {self.criterion}: {criterion(h.acceptance)}.",
                "",
                f"Severity {inline(h.severity)}: {inline(h.severity_rationale)} Failure mode: "
                f"{inline(h.failure_mode)} Hazardous situation: {inline(h.hazardous_situation)} "
                f"Harm: {inline(h.harm)}",
                "",
            ]
            if h.deferred_reason:
                lines += [f"Deferred: {inline(h.deferred_reason)}", ""]
            lines += [
                f"Cases: {v.cases_counted} counted of {v.cases_tagged} tagged, {v.cases_excluded} "
                f"excluded, {v.cases_failed} failed; observed rate {_rate(v.observed_rate)}, upper "
                f"bound {_rate(v.upper_bound)}; unstable cases: {v.unstable_cases}.",
                "",
            ]
            controls = [
                [
                    control.id,
                    control.type,
                    inline(control.requirement),
                    self._implementation(control.id),
                    _ids(c.id for c in linked if control.id in c.controls) or "**none**",
                ]
                for control in h.controls
            ]
            lines += _table(
                ["Control", "Type", "Requirement", "Implemented in", "Checks"], controls
            )
            checks = [
                [
                    f"`{c.id}`",
                    c.kind,
                    c.method,
                    "no: the target does not expose "
                    + ", ".join(ch for ch in c.needs if ch not in self.exposes)
                    if c.id in v.unevaluable_checks
                    else "yes",
                ]
                for c in linked
            ]
            if checks:
                lines += _table(["Check", "Kind", "Method", "Evaluable"], checks)
            else:
                lines += ["No check links this hazard.", ""]
            cases = [
                [
                    f"`{row.case_id}`",
                    row.status,
                    ", ".join(row.trial_statuses) or "—",
                    inline(row.reason),
                ]
                for row in self.results.case_hazards
                if row.hazard_id == hid
            ]
            if cases:
                lines += _table(["Case", "Status", "Trials", "Reason"], cases)
            else:
                lines += ["No case of this run is tagged with this hazard.", ""]
        return lines

    def _implementation(self, control_id: str) -> str:
        implementation = self.target.control_implementations.get(control_id)
        if implementation is None:
            return "not declared"
        return (
            "prompt (prompt only: the weakest kind)"
            if implementation == "prompt"
            else implementation
        )

    def _case_section(self) -> list[str]:
        k = self.manifest.repeats
        rows = []
        for stats in self.results.cases:
            case = self.pack.cases[stats.case_id]
            interval = stats.pass_rate_interval
            rows.append(
                [
                    f"`{stats.case_id}`",
                    inline(case.title),
                    ", ".join(case.hazards),
                    str(stats.trials),
                    str(stats.valid_trials),
                    str(stats.invalid_trials),
                    str(stats.error_trials),
                    str(stats.unscored_trials),
                    str(stats.passed_trials),
                    "—" if stats.pass_at_1 is None else f"{stats.pass_at_1:.2f}",
                    "—" if stats.pass_hat_k is None else f"{stats.pass_hat_k:.2f}",
                    "—" if interval is None else f"{interval[0]:.1%}{TO}{interval[1]:.1%}",
                    "yes" if stats.mixed else "no",
                ]
            )
        header = [
            "Case",
            "Title",
            "Hazards",
            "Trials",
            "Valid",
            "Invalid",
            "Error",
            "Unscored",
            "Passed",
            "pass@1",
            f"pass^k (k = {k})",
            "Pass rate, 95% interval",
            "Mixed",
        ]
        return [
            "## Cases",
            "",
            "A valid trial fails when one of its conformance or hazard checks failed. Otherwise "
            "it is unscored when one of them errored or was invalid, or when none of them could "
            "produce a result; it passes only when every one that produced a result passed. "
            "Validity checks decide whether a trial counts, not whether it passed. pass@1, "
            "pass^k and the interval count the scored trials only (valid - unscored). pass^k is "
            "the chance that k trials drawn from the scored ones all pass; mixed means passed "
            "and failed across repeats.",
            "",
            *_table(header, rows),
        ]

    def _check_section(self) -> list[str]:
        outcomes = ("pass", "fail", "not_evaluable", "invalid", "error")
        counts = Counter((r.check_id, r.outcome) for g in self.results.trials for r in g.results)
        rows = [
            [
                f"`{c.id}`",
                c.kind,
                c.method,
                ", ".join(c.hazards) or "—",
                ", ".join(c.controls) or "—",
                *(str(counts[(c.id, o)]) for o in outcomes),
            ]
            for c in self.results.checks
        ]
        header = ["Check", "Kind", "Method", "Hazards", "Controls", "pass", "fail", "not evaluable"]
        return [
            "## Checks",
            "",
            "Outcome counts over every graded trial, counted or not. A check that does not apply "
            "to a case gives no row.",
            "",
            *_table([*header, "invalid", "error"], rows),
        ]

    def _findings_section(self) -> list[str]:
        lines = ["## Findings", "", "### Failures that count", ""]
        tagged = {cid: set(case.hazards) for cid, case in self.pack.cases.items()}
        counted = []
        for g in self.results.trials:
            if g.status != "valid":
                continue
            for r in g.results:
                if r.outcome != "fail" or r.evidence_turn is None:
                    continue
                spec = self.specs[r.check_id]
                toward = [h for h in spec.hazards if h in tagged[g.case_id]]
                outside = [h for h in spec.hazards if h not in tagged[g.case_id]]
                ref = EvidenceRef(
                    case_id=g.case_id, n=g.n, check_id=r.check_id, turn=r.evidence_turn, note=r.note
                )
                text = f"{self._ref(ref)} failed (fault: agent)"
                if toward:
                    text += f"; counts toward {', '.join(toward)}"
                if outside:
                    text += f"; outside the sample of {', '.join(outside)}: shown, not counted"
                counted.append((f"{text}.{_said(r.note)}", self._excerpt(ref)))
        lines += self._gallery(counted, "No failure in a trial that counts.")
        lines += ["### Failures in trials that do not count (shown, not counted)", ""]
        found: dict[tuple[str, int, str, int], tuple[EvidenceRef, list[str]]] = {}
        for v in self.results.hazards:
            for ref in v.invalid_trial_findings:
                key = (ref.case_id, ref.n, ref.check_id, ref.turn)
                found.setdefault(key, (ref, []))[1].append(v.hazard_id)
        shown = []
        for ref, hazards in found.values():
            status = self.grades[(ref.case_id, ref.n)].status
            text = (
                f"{self._ref(ref)} failed in an {status} trial; not counted for "
                f"{', '.join(hazards)}.{_said(ref.note)}"
            )
            shown.append((text, self._excerpt(ref)))
        lines += self._gallery(shown, "None.")
        lines += ["### Errors", ""]
        errors = [
            (text, [])
            for key, text, _ in sorted(self._findings(), key=lambda f: f[0])
            if key[0] == 3
        ]
        lines += self._gallery(errors, "None.")
        return lines

    def _gallery(self, items: Sequence[tuple[str, list[str]]], empty: str) -> list[str]:
        if not items:
            return [empty, ""]
        lines = []
        for text, excerpt in items:
            lines += [f"- {text}", *(f"  {line}" for line in excerpt)]
        return [*lines, ""]

    def _test_data_section(self) -> list[str]:
        statuses = Counter(g.status for g in self.results.trials)
        graded = statuses["valid"] + statuses["invalid"] + statuses["error"]
        share = f"{(statuses['invalid'] + statuses['error']) / graded:.1%}" if graded else "—"
        lines = [
            "## Test-data quality",
            "",
            f"{len(self.results.trials)} trials: {statuses['valid']} valid, {statuses['invalid']} "
            f"invalid, {statuses['error']} error, {statuses['not_run']} not run. Invalid or error "
            f"share: {share}.",
            "",
        ]
        voided = [g for g in self.results.trials if g.status != "valid"]
        if voided:
            by_reason = Counter(
                inline(reason.split(":", 1)[0], NOTE_LIMIT) for g in voided for reason in g.reasons
            )
            lines += _table(
                ["Reason", "Trials"], [[reason, str(n)] for reason, n in sorted(by_reason.items())]
            )
            rows = [[f"`{g.case_id}`", str(g.n), g.status, self._reasons(g)] for g in voided]
            lines += _table(["Case", "Run", "Status", "Reasons"], rows)
        simulator = next((info for info in self.manifest.models if info.role == "simulator"), None)
        if simulator is not None:
            served = (
                ", ".join(inline(s) for s in self.served.get("simulator", [])) or "not recorded"
            )
            lines += [
                f"Simulator model: {inline(simulator.env_var)} {inline(simulator.requested)}, "
                f"served {served}.",
                "",
            ]
        rows = []
        for case_id in sorted(set(self.manifest.case_ids)):
            ran = [
                t
                for (c, _), t in sorted(self.trials.items())
                if c == case_id and t.status == "completed"
            ]
            for s in self.pack.cases[case_id].stimuli:
                delivered = sum(t.transcript.stimulus_turn(s) is not None for t in ran)
                rows.append(
                    [
                        f"`{case_id}`",
                        f"`{s.id}`",
                        s.purpose,
                        s.channel,
                        f"{delivered} of {len(ran)}",
                    ]
                )
        if rows:
            lines += [
                "Stimulus delivery, over completed trials:",
                "",
                *_table(["Case", "Stimulus", "Purpose", "Channel", "Delivered"], rows),
            ]
        return lines

    def _limitations(self) -> list[str]:
        items = [
            *self._cannot_show(),
            "Hash checks show that the run folder is consistent with itself, not where it came "
            "from: nothing is signed.",
            "Tool results came from the pack's mocked tools: no real system was reached.",
            "No check uses an LLM judge: the judge is designed, not built.",
        ]
        return ["## Limitations", "", *(f"- {item}" for item in items), ""]

    # ── Appendix ──────────────────────────────────────────────────────────────────────────────────

    def appendix(self) -> list[str]:
        return [
            *self._matrix(),
            *self._reverse_trace(),
            *self._manifest_section(),
            *self._criteria_section(),
            *self._lint_section(),
            "## Appendix F. Harness self-test evidence",
            "",
            "Not attached in this version.",
        ]

    def _result(self, v: HazardVerdict) -> str:
        if not v.cases_counted:
            return _words(v.verdict)
        return (
            f"{v.cases_failed}/{v.cases_counted} failed, upper bound {_rate(v.upper_bound)}, "
            f"{_words(v.verdict)}"
        )

    def _matrix(self) -> list[str]:
        rows = []
        for hid, h in self.hazards.items():
            v = self.verdicts[hid]
            for control in h.controls:
                checks = [c for c in self.results.checks if control.id in c.controls]
                rows.append(
                    [
                        f"{hid} {inline(h.title)}",
                        control.id,
                        control.type,
                        self._implementation(control.id),
                        ", ".join(f"`{c.id}` ({c.kind}, {c.method})" for c in checks) or "**none**",
                        str(v.cases_tagged),
                        self._result(v),
                    ]
                )
        header = ["Hazard", "Control", "Type", "Implemented in", "Checks (kind, method)", "Cases"]
        lines = [
            "## Appendix A. Traceability matrix",
            "",
            "Forward: hazard → control → check → cases → result. One row per hazard and control.",
            "",
            *_table([*header, "Result"], rows),
        ]
        hazard_level = [
            f"`{c.id}` → {hid}"
            for c in self.results.checks
            for hid in c.hazards
            if hid in self.hazards
            and not any(control.id in c.controls for control in self.hazards[hid].controls)
        ]
        if hazard_level:
            lines += [
                "Checks linked to a hazard but to none of its controls: "
                f"{', '.join(hazard_level)}.",
                "",
            ]
        lines += ["**Gaps**", ""]
        gaps = self._gaps()
        lines += [f"- {text}" for _, text in gaps] or [
            "None: every control has a check, every hazard a case, every check is evaluable."
        ]
        lines += ["", *self._validity_conditions()]
        return lines

    def _gaps(self) -> list[tuple[str, str]]:
        """(hazard id, text) of each gap: a control with no check, a hazard with no case, a check
        the target cannot evaluate. Deferred hazards have none: they are not assessed yet."""
        out = []
        for hid, h in self.hazards.items():
            if h.deferred_reason:
                continue
            v = self.verdicts[hid]
            for control in h.controls:
                if not any(control.id in c.controls for c in self.results.checks):
                    requirement = inline(control.requirement)
                    text = f"**Gap** — {control.id} ({requirement}): no check verifies it."
                    out.append((hid, text))
            if not v.cases_tagged:
                out.append((hid, f"**Gap** — {hid}: no case of this run is tagged with it."))
            for check_id in v.unevaluable_checks:
                missing = ", ".join(
                    ch for ch in self.specs[check_id].needs if ch not in self.exposes
                )
                text = (
                    f"**Gap** — `{check_id}` cannot be evaluated for {hid}: the target does not "
                    f"expose {missing}."
                )
                out.append((hid, text))
        return out

    def _validity_conditions(self) -> list[str]:
        validity = [c for c in self.results.checks if c.kind == "validity"]
        counts = Counter((r.check_id, r.outcome) for g in self.results.trials for r in g.results)
        rows = [
            [
                f"`{c.id}`",
                c.method,
                inline(c.description),
                str(counts[(c.id, "invalid")]),
                str(counts[(c.id, "error")]),
            ]
            for c in validity
        ]
        return [
            "**Trial-validity conditions.** These checks decide whether a trial counts. They link "
            "no hazard, by design, and appear in no row above.",
            "",
            *_table(["Check", "Method", "Condition", "Invalid in", "Error in"], rows),
        ]

    def _reverse_trace(self) -> list[str]:
        statuses: dict[str, list[str]] = {}
        for row in self.results.case_hazards:
            statuses.setdefault(row.case_id, []).append(f"{row.hazard_id} ({row.status})")
        cases = [
            [
                f"`{case_id}`",
                inline(self.pack.cases[case_id].title),
                ", ".join(statuses.get(case_id, [])) or "—",
            ]
            for case_id in sorted(set(self.manifest.case_ids))
        ]
        checks = [
            [
                f"`{c.id}`",
                c.kind,
                c.method,
                ", ".join(c.hazards) or "—",
                ", ".join(c.controls) or "—",
            ]
            for c in self.results.checks
        ]
        return [
            "## Appendix B. Reverse trace",
            "",
            *_table(["Case", "Title", "Hazards (status)"], cases),
            *_table(["Check", "Kind", "Method", "Hazards", "Controls"], checks),
        ]

    def _manifest_section(self) -> list[str]:
        m, x, r = self.manifest, self.execution, self.results
        models = "; ".join(
            f"{info.role}: {inline(info.env_var)} = {inline(info.requested)}, effort "
            f"{info.effort}, max tokens {info.max_tokens}"
            for info in m.models
        )
        served = "; ".join(
            f"{role}: {', '.join(inline(i) for i in ids)}"
            for role, ids in sorted(self.served.items())
        )
        dirty = {True: "yes", False: "no", None: "unknown"}[m.harness_git_dirty]
        packages = ", ".join(
            f"{inline(name)} {inline(v)}" for name, v in sorted(m.packages.items())
        )
        fields = [
            ("Run id", inline(m.run_id)),
            ("Created at", _time(m.created_at)),
            ("Command", inline(" ".join(m.command))),
            (
                "Harness",
                f"{inline(m.harness_version)}, git SHA {inline(m.harness_git_sha or 'unknown')}, "
                f"uncommitted changes: {dirty}",
            ),
            ("Python and packages", f"{inline(m.python)}; {packages}"),
            (
                "Pack",
                f"{inline(m.pack_name)} {inline(m.pack_version)}, variant {inline(m.variant)}",
            ),
            ("Pack sha256 (snapshot)", inline(m.pack_sha256)),
            ("Graded pack sha256", f"{inline(r.pack_sha256)} ({r.graded_against})"),
            ("Acceptance criteria sha256", inline(m.acceptance_sha256)),
            (
                "Target",
                f"{inline(m.target_name)} ({inline(m.target_kind)}), sha256 "
                f"{inline(m.target_sha256)}",
            ),
            ("System prompt sha256", inline(m.system_prompt_sha256 or "none")),
            ("Tools sha256", inline(m.tools_sha256)),
            ("Models requested", models or "none"),
            ("Models served", served or "not recorded"),
            ("Cases", _ids(m.case_ids)),
            ("Repeats, max turns", f"{m.repeats}, {m.max_turns}"),
            ("Seed", "none: the Messages API takes no seed"),
            (
                "Budget, estimate, spent",
                f"{_usd(m.budget_usd)}, {_usd(m.estimate_usd)}, {_usd(x.spent_usd)} (prices as of "
                f"{inline(m.prices_as_of)})",
            ),
            (
                "Execution",
                f"{x.status}; first model call {_time(x.first_call_at)}; updated "
                f"{_time(x.updated_at)}",
            ),
            ("Resumed at", ", ".join(_time(t) for t in x.resumed_at) or "never"),
        ]
        files = [
            [inline(e.file), str(e.attempt), e.status, inline(e.stop_reason), inline(e.sha256)]
            for e in sorted(x.trials, key=lambda e: (e.case_id, e.n, e.attempt))
        ]
        return [
            "## Appendix C. Manifest and snapshot hashes",
            "",
            *_table(["Field", "Value"], [list(f) for f in fields]),
            "Transcripts, each checked against its sha256 before grading:",
            "",
            *_table(["File", "Attempt", "Status", "Stop reason", "sha256"], files),
        ]

    def _criteria_section(self) -> list[str]:
        scale = [
            [str(s.rank), inline(s.name), inline(s.definition)]
            for s in sorted(self.pack.config.severity_scale, key=lambda s: -s.rank)
        ]
        criteria = []
        for hid, h in self.hazards.items():
            a = h.acceptance
            criteria.append(
                [
                    hid,
                    a.metric,
                    percent(a.max_rate),
                    percent(a.confidence),
                    a.interval,
                    str(a.min_cases),
                    str(a.min_valid_trials_per_case),
                    percent(a.max_invalid_share),
                    "yes" if a.zero_tolerance else "no",
                ]
            )
        header = ["Hazard", "Metric", "Max rate", "Confidence", "Interval", "Min cases"]
        if self.current:
            source = "From the current pack files, as graded here"
        elif self.unconfirmed:
            source = "As read from the run's snapshot by this harness"
        else:
            source = "As declared before the run"
        source += f" (sha256 {inline(self.acceptance_sha256)})."
        # the manifest's sha256 is printed too (page one, Appendix C): say why the two differ
        run = inline(self.manifest.acceptance_sha256)
        if self.lacked:
            fields = ", ".join(f"`{name}`" for name in self.lacked)
            source += (
                f" The run's manifest holds sha256 {run}: the snapshot's criteria, hashed by a "
                f"harness whose Acceptance model had no {fields}."
            )
        elif self.unconfirmed:
            source += (
                f" The run's manifest holds sha256 {run}, which the snapshot's criteria, as read "
                "here, do not reproduce."
            )
        return [
            "## Appendix D. Acceptance criteria and severity scale",
            "",
            source,
            "",
            *_table(["Rank", "Severity", "Definition"], scale),
            *_table(
                [*header, "Valid trials per case", "Max invalid share", "Zero tolerance"], criteria
            ),
        ]

    def _lint_section(self) -> list[str]:
        lines = ["## Appendix E. Lint findings", ""]
        if not self.results.lint:
            return [*lines, "None.", ""]
        rows = [
            [i.level, i.code, inline(i.where), inline(i.message, NOTE_LIMIT)]
            for i in self.results.lint
        ]
        return [*lines, *_table(["Level", "Code", "Where", "Message"], rows)]
