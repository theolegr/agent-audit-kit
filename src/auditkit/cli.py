"""The `eval` command: flags, not subcommands; one mode per call."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from pathlib import Path
from typing import Literal

from auditkit import __version__, runfolder, runner
from auditkit.cost import PRICES_AS_OF, RoleEstimate, UnknownModel, estimate, price
from auditkit.grade import grade_run
from auditkit.lint import lint_pack
from auditkit.llm import AnthropicClient, LLMClient
from auditkit.pack import Pack, PackError, load_pack
from auditkit.report import render
from auditkit.runfolder import IntegrityError
from auditkit.schema import Execution, LintIssue, ModelInfo, RunManifest, Target, Trial
from auditkit.tools import now

DEFAULT_BUDGET_USD = 3.00

USAGE = """eval [-h] [--version]
            (--domain NAME | --lint NAME | --replay RUN_DIR | --resume RUN_DIR | --ui)
            [--variant NAME] [--target NAME] [--cases IDS] [--repeats N] [--max-turns N]
            [--budget-usd USD] [--regrade-current] [--port N] [--no-browser]
            [--packs-dir DIR] [--runs-dir DIR]"""

DESCRIPTION = """Risk-based evaluation of a conversational agent against a domain pack.
--domain and --resume call the model API and cost money; --lint and --replay make no API call.
--ui serves a local page (127.0.0.1 only) that does the same; a live run from it asks first."""

EPILOG = """\
exit codes: 0 report written, whatever the verdicts | 2 usage, configuration or test-data
lint error, nothing sent | 3 budget: estimate above the cap, or cap reached (partial report
written) | 4 harness or provider error (partial report written) | 5 replay or resume integrity
error (snapshot or transcript hash mismatch; later: judge cache miss)
(reserved: 1 = a hazard not met, only with the future --gate)"""


def _usd_amount(text: str) -> float:
    """A budget: a finite amount above zero (NaN would make every comparison false)."""
    try:
        value = float(text)
    except ValueError:
        value = float("nan")
    if not (isfinite(value) and value > 0):
        raise ValueError(f"budget {text!r} is not a positive amount of USD")
    return value


def _cli_usd(text: str) -> float:
    try:
        return _usd_amount(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text} is not a positive integer")
    return value


def _port(text: str) -> int:
    value = int(text)
    if not 0 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"{text} is not a port (0 to 65535)")
    return value


class _Formatter(argparse.RawDescriptionHelpFormatter):
    def __init__(self, prog: str) -> None:  # a fixed width: the same text in every terminal
        super().__init__(prog, max_help_position=22, width=100)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval",
        usage=USAGE,
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=_Formatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    modes = p.add_argument_group("modes (choose one)").add_mutually_exclusive_group(required=True)
    modes.add_argument(
        "--domain",
        metavar="NAME",
        help="live run: lint, estimate, then every case of domains/NAME against a target",
    )
    modes.add_argument(
        "--lint",
        metavar="NAME",
        help="lint the pack's test data and print the cost estimate; no API call",
    )
    modes.add_argument(
        "--replay",
        metavar="RUN_DIR",
        type=Path,
        help="re-grade the saved transcripts from the run's snapshot; rewrites results.json and "
        "report.md (the previous pair moves to RUN_DIR/history/); no API call",
    )
    modes.add_argument(
        "--resume",
        metavar="RUN_DIR",
        type=Path,
        help="finish an interrupted or budget-stopped run; completed trials are kept",
    )
    modes.add_argument(
        "--ui",
        action="store_true",
        help="serve the local page on 127.0.0.1: results, parameters, estimates, replays, and "
        "live runs after a confirmation that names their cost and cap",
    )

    sel = p.add_argument_group("selection (--domain, --lint)")
    sel.add_argument(
        "--variant", metavar="NAME", help="pack variant (default: the pack's own cases/ and data)"
    )
    sel.add_argument(
        "--target",
        metavar="NAME",
        help="target in domains/NAME/targets/ (default: the pack's default_target)",
    )
    sel.add_argument(
        "--cases",
        metavar="IDS",
        help="comma-separated case ids (default: every case of the variant)",
    )
    sel.add_argument(
        "--repeats",
        metavar="N",
        type=_positive,
        help="trials per case (default: the pack's default_repeats)",
    )
    sel.add_argument(
        "--max-turns",
        metavar="N",
        type=_positive,
        help="user-turn cap per trial (default: the pack's max_turns)",
    )

    cost = p.add_argument_group("cost (--domain, --resume)")
    cost.add_argument(
        "--budget-usd",
        metavar="USD",
        type=_cli_usd,
        help="hard cap for this run (default: EVAL_BUDGET_USD in .env, else 3.00). The run is "
        "refused before any API call if the estimate is above it; a call whose reservation would "
        "cross it is not made, and completed trials are kept.",
    )

    replay = p.add_argument_group("replay")
    replay.add_argument(
        "--regrade-current",
        action="store_true",
        help="grade against the current domains/ files instead of the snapshot; writes "
        "RUN_DIR/regrade-<timestamp>/ and leaves the run's own report untouched",
    )

    ui = p.add_argument_group("local page (--ui)")
    ui.add_argument(
        "--port",
        metavar="N",
        type=_port,
        default=8765,
        help="port on 127.0.0.1 (default: 8765; 0: any free port)",
    )
    ui.add_argument(
        "--no-browser", action="store_true", help="print the page's address, open no browser"
    )

    paths = p.add_argument_group("paths")
    paths.add_argument(
        "--packs-dir",
        metavar="DIR",
        type=Path,
        default=Path("domains"),
        help="where packs live (default: ./domains)",
    )
    paths.add_argument(
        "--runs-dir",
        metavar="DIR",
        type=Path,
        default=Path("runs"),
        help="where runs are written (default: ./runs)",
    )
    return p


def load_dotenv(path: Path) -> None:
    """`.env`: `KEY=VALUE` lines; blanks and comments ignored; the environment wins."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.removeprefix("export ").split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def console() -> int:
    """The installed `eval` command: `main`, with `./.env` read into the environment first."""
    return main(env_file=Path(".env"))


def main(
    argv: Sequence[str] | None = None,
    *,
    clock: Callable[[], datetime] = now,
    make_client: Callable[[], LLMClient] | None = None,
    env_file: Path | None = None,
) -> int:
    """`make_client` replaces the Anthropic client (tests pass a scripted one); `env_file` is
    read into the environment first (`console` passes `./.env`; tests read none)."""
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # --help and --version exit 0, a usage error exits 2
        return int(e.code or 0)
    if args.regrade_current and args.replay is None:
        print("eval: --regrade-current goes with --replay", file=sys.stderr)
        return 2
    if env_file is not None:
        load_dotenv(env_file)
    if args.ui:
        from auditkit.ui.server import serve  # the page's code loads with its mode only

        return serve(
            packs_dir=args.packs_dir,
            runs_dir=args.runs_dir,
            port=args.port,
            open_browser=not args.no_browser,
        )
    if args.lint is not None:
        return _lint(args)
    if args.replay is not None:
        return _grade_folder(
            args.replay,
            current=args.regrade_current,
            packs_dir=args.packs_dir,
            graded_by="replay",
            clock=clock,
        )
    if args.domain is not None:
        return _domain(args, clock, make_client)
    return _resume(args, clock, make_client)


def _usd(value: float) -> str:
    return f"${value:.4f}" if value < 0.01 else f"${value:.2f}"


def default_budget() -> float:
    """The cap when none is given: EVAL_BUDGET_USD (from the environment or .env), else 3.00."""
    return _usd_amount(os.environ.get("EVAL_BUDGET_USD") or str(DEFAULT_BUDGET_USD))


def _budget(args: argparse.Namespace) -> float:
    return args.budget_usd if args.budget_usd is not None else default_budget()


class Refused(Exception):
    """A selection or configuration problem: printed, nothing sent."""


@dataclass
class Selection:
    pack: Pack
    target: Target
    case_ids: list[str]
    repeats: int
    max_turns: int
    worst_turns: int  # the longest trial's user-turn cap
    models: list[ModelInfo]
    roles: list[RoleEstimate]

    @property
    def typical_usd(self) -> float:
        return sum(r.typical_usd for r in self.roles)

    @property
    def worst_usd(self) -> float:
        return sum(r.worst_usd for r in self.roles)

    def source(self, role: str) -> str:
        """Where a role's model comes from: its environment variable when set, else "default"."""
        env_var = next(m.env_var for m in self.models if m.role == role)
        return env_var if os.environ.get(env_var) else "default"


def _select(args: argparse.Namespace, name: str) -> Selection:
    return select(
        args.packs_dir,
        name,
        variant=args.variant,
        target=args.target,
        cases=args.cases,
        repeats=args.repeats,
        max_turns=args.max_turns,
    )


def select(
    packs_dir: Path,
    name: str,
    *,
    variant: str | None = None,
    target: str | None = None,
    cases: str | None = None,
    repeats: int | None = None,
    max_turns: int | None = None,
) -> Selection:
    """What `--lint` estimates and `--domain` runs: the pack, target, cases, repeats and models.
    The arguments are the flags' values (`cases`: comma-separated ids). Call it after a lint with
    no error (an unknown target is the lint's to report). It loads the pack's code."""
    try:
        pack = load_pack(packs_dir, name, variant=variant or "default")
    except PackError as e:
        raise Refused(str(e)) from e
    config = pack.config
    chosen = pack.targets[target or config.default_target]
    named = [c.strip() for c in cases.split(",") if c.strip()] if cases else []
    case_ids = list(dict.fromkeys(named))  # a case named twice runs once
    if unknown := sorted(set(case_ids) - set(pack.cases)):
        raise Refused(f"--cases: no case {', '.join(unknown)} in {name}")
    selected = [pack.cases[c] for c in case_ids or sorted(pack.cases)]
    if not selected:
        raise Refused(f"no case selected in {name}")
    repeats = repeats or config.default_repeats
    max_turns = max_turns or config.max_turns
    worst_turns = max(c.max_turns or max_turns for c in selected)
    models = runner.model_infos(chosen)
    try:
        roles = estimate(
            config.estimate,
            trials=len(selected) * repeats,
            max_turns=worst_turns,
            models={m.role: m.requested for m in models},
        )
    except UnknownModel as e:
        raise Refused(str(e)) from e
    return Selection(
        pack, chosen, [c.id for c in selected], repeats, max_turns, worst_turns, models, roles
    )


def _print_lint(name: str, variant: str, issues: Sequence[LintIssue]) -> bool:
    """Every issue; True when there is an error."""
    errors = sum(i.level == "error" for i in issues)
    warnings = len(issues) - errors
    print(f"lint {name} (variant {variant}): {errors} error(s), {warnings} warning(s)")
    for i in issues:
        print(f"  {i.level:<7} {i.code} {i.where}: {i.message}")
    if errors:
        print("eval: lint errors: nothing run, nothing written", file=sys.stderr)
    return errors > 0


def _print_estimate(sel: Selection, budget: float) -> float:
    """The estimate per role and in total; returns the typical total."""
    trials = len(sel.case_ids) * sel.repeats
    print(
        f"estimate: {len(sel.case_ids)} case(s) x {sel.repeats} repeat(s) = {trials} "
        f"trial(s), target {sel.target.name}, up to {sel.worst_turns} user turns per trial "
        f"(prices as of {PRICES_AS_OF}; thinking tokens not counted)"
    )
    for r in sel.roles:
        print(
            f"  {r.role:<10} {r.model:<20} typical {_usd(r.typical_usd):>9}   "
            f"worst case {_usd(r.worst_usd):>9}   ({sel.source(r.role)})"
        )
    typical, worst = sel.typical_usd, sel.worst_usd
    print(
        f"  {'total':<31} typical {_usd(typical):>9}   worst case {_usd(worst):>9}   "
        f"cap {_usd(budget)}"
    )
    return typical


def _lint(args: argparse.Namespace) -> int:
    """--lint: every issue, then (no error) the selection and the estimate. Writes nothing."""
    variant = args.variant or "default"
    issues = lint_pack(
        args.packs_dir,
        args.lint,
        variant=variant,
        target=args.target,
        repeats=args.repeats,
        max_turns=args.max_turns,
    )
    if _print_lint(args.lint, variant, issues):
        return 2
    try:
        sel = _select(args, args.lint)
        budget = _budget(args)
    except (Refused, ValueError) as e:
        print(f"eval: {e}", file=sys.stderr)
        return 2
    if _print_estimate(sel, budget) > budget:
        print(
            f"  a live run would be refused: the typical estimate is above the {_usd(budget)} cap"
        )
    return 0


def _client(make_client: Callable[[], LLMClient] | None) -> LLMClient | None:
    """The model client, made only once a run is allowed to start; None: no credentials."""
    if make_client is not None:
        return make_client()
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        print(
            "eval: no ANTHROPIC_API_KEY in the environment or .env: nothing sent", file=sys.stderr
        )
        return None
    return AnthropicClient()


def _domain(
    args: argparse.Namespace,
    clock: Callable[[], datetime],
    make_client: Callable[[], LLMClient] | None,
) -> int:
    """--domain: lint, estimate, refuse above the cap before any client exists, then the run
    folder, the trials and the grading."""
    name, variant = args.domain, args.variant or "default"
    issues = lint_pack(
        args.packs_dir,
        name,
        variant=variant,
        target=args.target,
        repeats=args.repeats,
        max_turns=args.max_turns,
    )
    if _print_lint(name, variant, issues):
        return 2
    try:
        sel = _select(args, name)
        budget = _budget(args)
    except (Refused, ValueError) as e:
        print(f"eval: {e}", file=sys.stderr)
        return 2
    typical = _print_estimate(sel, budget)
    if typical > budget:
        print(
            f"eval: refused: the typical estimate {_usd(typical)} is above the {_usd(budget)} cap; "
            "nothing sent (raise it with --budget-usd)",
            file=sys.stderr,
        )
        return 3
    client = _client(make_client)
    if client is None:
        return 2
    command = ["eval", "--domain", name, "--variant", variant, "--target", sel.target.name]
    if args.cases:
        command += ["--cases", ",".join(sel.case_ids)]
    command += ["--repeats", str(sel.repeats), "--max-turns", str(sel.max_turns)]
    run_dir, manifest, execution = runner.start(
        sel.pack,
        sel.target,
        packs_dir=args.packs_dir,
        runs_dir=args.runs_dir,
        case_ids=sel.case_ids,
        repeats=sel.repeats,
        max_turns=sel.max_turns,
        models=sel.models,
        issues=[i for i in issues if i.level == "warning"],
        budget_usd=budget,
        estimate_usd=typical,
        command=[*command, "--budget-usd", f"{budget:g}"],
        clock=clock,
    )
    todo = runner.planned(sel.case_ids, sel.repeats)
    print(f"run {manifest.run_id}: {len(todo)} trial(s), cap {_usd(budget)}")
    return _run(run_dir, sel.pack, manifest, execution, client, todo, budget, 0.0, clock)


def _resume(
    args: argparse.Namespace,
    clock: Callable[[], datetime],
    make_client: Callable[[], LLMClient] | None,
) -> int:
    """--resume: the run's own snapshot and models; every planned trial without a completed
    file runs again, its earlier file kept as an attempt. The spend adds to the run's total under
    its cap, unless --budget-usd sets a cap for this resume alone."""
    run_dir: Path = args.resume
    if not (run_dir / runfolder.MANIFEST).is_file():
        print(f"eval: {run_dir}: not a run folder (no {runfolder.MANIFEST})", file=sys.stderr)
        return 2
    try:
        manifest = runfolder.read_manifest(run_dir)
        execution = runfolder.read_execution(run_dir, manifest)
        runfolder.verify_snapshot(run_dir, manifest)
        trials = runfolder.load_trials(run_dir, manifest, execution)
    except IntegrityError as e:
        print(f"eval: integrity error, nothing sent: {e}", file=sys.stderr)
        return 5
    try:
        pack = load_pack(run_dir / runfolder.SNAPSHOT, manifest.pack_name, variant=manifest.variant)
        for info in manifest.models:
            price(info.requested)
    except (PackError, UnknownModel) as e:
        print(f"eval: {e}", file=sys.stderr)
        return 2
    missing = [c for c in manifest.case_ids if c not in pack.cases]
    if manifest.target_name not in pack.targets or missing:
        print(
            "eval: integrity error: the snapshot lacks the run's target or cases", file=sys.stderr
        )
        return 5
    todo = runner.missing(manifest, trials)
    if not todo:
        print(f"resume {manifest.run_id}: every planned trial is completed; grading again")
        return _finish(run_dir, clock)
    if args.budget_usd is not None:
        cap, spent = args.budget_usd, 0.0
    else:
        cap, spent = manifest.budget_usd, execution.spent_usd
    client = _client(make_client)
    if client is None:
        return 2
    execution.resumed_at.append(clock())
    runfolder.write_execution(run_dir, execution)
    print(f"resume {manifest.run_id}: {len(todo)} trial(s), {_usd(spent)} of {_usd(cap)} spent")
    return _run(run_dir, pack, manifest, execution, client, todo, cap, spent, clock)


def _run(
    run_dir: Path,
    pack: Pack,
    manifest: RunManifest,
    execution: Execution,
    client: LLMClient,
    todo: Sequence[runner.Planned],
    cap: float,
    spent: float,
    clock: Callable[[], datetime],
) -> int:
    def progress(trial: Trial) -> None:
        print(
            f"  {trial.case_id} run {trial.n} (attempt {trial.attempt}): {trial.status}, "
            f"{trial.stop_reason}, {_usd(trial.cost_usd)}"
        )

    try:
        runner.execute(
            run_dir,
            pack,
            manifest,
            execution,
            client,
            todo=todo,
            cap_usd=cap,
            spent_usd=spent,
            clock=clock,
            on_trial=progress,
        )
    except KeyboardInterrupt:
        print(f"eval: interrupted; finish it with --resume {run_dir}", file=sys.stderr)
        return 130
    return _finish(run_dir, clock)


def _finish(run_dir: Path, clock: Callable[[], datetime]) -> int:
    """Grade the run with the replay's code, then the run's exit code: 3 when the budget
    stopped it, 4 when a trial errored; the report is written either way."""
    code = _grade_folder(run_dir, current=False, packs_dir=None, graded_by="run", clock=clock)
    if code:
        return code
    manifest = runfolder.read_manifest(run_dir)
    execution = runfolder.read_execution(run_dir, manifest)
    print(f"spent {_usd(execution.spent_usd)} of {_usd(manifest.budget_usd)}")
    if execution.status == "aborted_budget":
        print(
            f"eval: the budget cap was reached: partial report written; finish it with "
            f"--resume {run_dir}",
            file=sys.stderr,
        )
        return 3
    latest = runfolder.load_trials(run_dir, manifest, execution)
    if errors := [t for t, _ in latest if t.status == "error"]:
        print(
            f"eval: {len(errors)} trial(s) errored (see the report); retry them with "
            f"--resume {run_dir}",
            file=sys.stderr,
        )
        return 4
    return 0


def _grade_folder(
    run_dir: Path,
    *,
    current: bool,
    packs_dir: Path | None,
    graded_by: Literal["run", "replay"],
    clock: Callable[[], datetime],
) -> int:
    """Grade a run folder: the live run's last step, and `--replay`. Exit 5 when the folder
    does not match its own record; 2 when it is not a run folder, or when the pack no longer loads
    with this harness. Lint issues never stop it: they go to the report."""
    if not (run_dir / runfolder.MANIFEST).is_file():
        print(f"eval: {run_dir}: not a run folder (no {runfolder.MANIFEST})", file=sys.stderr)
        return 2
    try:
        manifest = runfolder.read_manifest(run_dir)
        execution = runfolder.read_execution(run_dir, manifest)
        runfolder.verify_snapshot(run_dir, manifest)
        trials = runfolder.load_trials(run_dir, manifest, execution)
    except IntegrityError as e:
        print(f"eval: integrity error, nothing written: {e}", file=sys.stderr)
        return 5

    source = packs_dir if current and packs_dir is not None else run_dir / runfolder.SNAPSHOT
    name, variant = manifest.pack_name, manifest.variant
    issues = lint_pack(
        source,
        name,
        variant=variant,
        target=manifest.target_name,
        repeats=manifest.repeats,
        max_turns=manifest.max_turns,
        coverage_as_warnings=True,
    )
    try:
        pack = load_pack(source, name, variant=variant)
    except PackError as e:
        where = "--regrade-current" if current else "the snapshot matches its hash but"
        print(f"eval: {where} does not load with this harness: {e}", file=sys.stderr)
        return 2
    missing = [f"case {c}" for c in sorted(set(manifest.case_ids) - set(pack.cases))]
    if manifest.target_name not in pack.targets:
        missing.insert(0, f"target {manifest.target_name}")
    if missing:
        if current:
            print(
                f"eval: --regrade-current: pack {name} has no {', '.join(missing)}", file=sys.stderr
            )
            return 2
        print(f"eval: integrity error: the snapshot has no {', '.join(missing)}", file=sys.stderr)
        return 5

    try:
        results = grade_run(
            pack,
            pack.targets[manifest.target_name],
            trials,
            run_id=manifest.run_id,
            case_ids=manifest.case_ids,
            repeats=manifest.repeats,
            max_turns=manifest.max_turns,
            graded_against="current" if current else "snapshot",
            expectations_match=runfolder.read_expectations(run_dir)
            == runfolder.expectations(pack, manifest.case_ids),
            lint=issues,
            harness_git_sha=runfolder.harness_git()[0],
            clock=clock,
        )
        by_key = {(trial.case_id, trial.n): trial for trial, _ in trials}
        declared = runfolder.declared_acceptance(run_dir, manifest)
        text = render(
            results, pack, manifest, execution, by_key, graded_by=graded_by, declared=declared
        )
        if current:
            out = runfolder.write_regrade(run_dir, results, text, clock=clock)
        else:
            out = runfolder.write_grading(run_dir, results, text)
    except Exception as e:  # a harness bug: exit 4, never 1 (reserved for --gate)
        traceback.print_exc()
        step = "replay" if graded_by == "replay" else "grading"
        print(f"eval: harness error during {step}: {type(e).__name__}: {e}", file=sys.stderr)
        return 4
    against = "the current pack files" if current else "its snapshot"
    verb = "replay" if graded_by == "replay" else "graded"
    print(f"{verb} {manifest.run_id}: {len(trials)} trial(s) graded against {against}")
    for v in results.hazards:
        print(f"  {v.hazard_id} {v.verdict.replace('_', ' ')}")
    print(f"report: {out / runfolder.REPORT}")
    return 0
