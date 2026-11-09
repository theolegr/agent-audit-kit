"""The run folder: names, atomic writes, the snapshot, the run's record as `make_run` writes
it, and every way `load_trials` refuses a transcript that does not match the record."""

import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from auditkit import runfolder
from auditkit.pack import load_pack, pack_files, pack_sha256
from auditkit.runfolder import IntegrityError
from auditkit.schema import Execution, Results, RunManifest, Trial, TrialIndexEntry
from auditkit.testing import FIXED_NOW, TranscriptBuilder, make_run

FIXTURE_PACKS = Path(__file__).parent / "fixtures" / "packs"


@pytest.fixture
def packs(tmp_path: Path) -> Path:
    """A private copy of the fixture packs: tests mutate it freely."""
    root = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, root, ignore=shutil.ignore_patterns("__pycache__"))
    return root


def toy_trial(case_id: str = "toy-open", n: int = 1, attempt: int = 1) -> Trial:
    record = "R-1" if case_id == "toy-open" else "R-2"
    return (
        TranscriptBuilder()
        .user(f"Where is {record}?", hidden={"asking_about": record})
        .agent('It is open. {"status": "open"}')
        .trial(case_id, n=n, attempt=attempt, target_name="basic")
    )


@pytest.fixture
def run(tmp_path: Path) -> Path:
    return make_run(tmp_path / "runs", FIXTURE_PACKS, "toypack", [toy_trial(), toy_trial(n=2)])


def record(run: Path) -> tuple[RunManifest, Execution]:
    manifest = runfolder.read_manifest(run)
    return manifest, runfolder.read_execution(run, manifest)


def index(run: Path, entries: list[TrialIndexEntry]) -> None:
    execution = record(run)[1]
    runfolder.write_execution(run, execution.model_copy(update={"trials": entries}))


# ── Names and writes ──────────────────────────────────────────────────────────────────────────────


def test_run_folders_are_named_after_the_clock_in_utc_with_a_suffix_on_a_collision(tmp_path):
    first = runfolder.new_run_dir(tmp_path, lambda: FIXED_NOW)
    second = runfolder.new_run_dir(tmp_path, lambda: FIXED_NOW)
    later = runfolder.new_run_dir(tmp_path, lambda: FIXED_NOW + timedelta(seconds=1))
    assert [first.name, second.name, later.name] == [
        "2026-01-01_120000",
        "2026-01-01_120000_2",
        "2026-01-01_120001",
    ]


def test_an_atomic_write_returns_the_sha256_and_leaves_no_temporary_file(tmp_path):
    sha = runfolder.write_bytes(tmp_path / "a" / "x.json", b"{}\n")
    assert sha == hashlib.sha256(b"{}\n").hexdigest()
    assert [p.name for p in (tmp_path / "a").iterdir()] == ["x.json"]


def test_the_same_value_always_gives_the_same_bytes(tmp_path):
    first = runfolder.write_json(tmp_path / "a.json", toy_trial())
    reread = Trial.model_validate_json((tmp_path / "a.json").read_bytes())
    assert runfolder.write_json(tmp_path / "b.json", reread) == first


def test_the_manifest_and_the_transcripts_are_never_rewritten(run):
    manifest, execution = record(run)
    with pytest.raises(FileExistsError, match="never rewritten"):
        runfolder.write_manifest(run, manifest)
    with pytest.raises(FileExistsError, match="never rewritten"):
        runfolder.write_trial(run, toy_trial())
    assert record(run) == (manifest, execution)


# ── The snapshot ──────────────────────────────────────────────────────────────────────────────────


def test_the_snapshot_is_a_byte_copy_of_the_hashed_files_of_the_pack_and_its_dependency(
    packs, tmp_path
):
    (packs / "toypack" / "tests").mkdir()
    (packs / "toypack" / "tests" / "test_x.py").write_text("x = 1\n")
    pack = load_pack(packs, "toypack")
    assert runfolder.copy_snapshot(packs, "toypack", tmp_path, pack.sha256) == pack.sha256
    snapshot = tmp_path / runfolder.SNAPSHOT
    copied = sorted(p.relative_to(snapshot) for p in snapshot.rglob("*") if p.is_file())
    hashed = [
        p.relative_to(packs) for name in ("toybase", "toypack") for p in pack_files(packs / name)
    ]
    assert copied == sorted(hashed)
    assert not (snapshot / "toypack" / "tests").exists()
    assert all((snapshot / p).read_bytes() == (packs / p).read_bytes() for p in copied)


def test_a_pack_that_changed_after_it_was_loaded_is_refused_at_the_copy(packs, tmp_path):
    pack = load_pack(packs, "toypack")
    hazards = packs / "toypack" / "hazards.yaml"
    hazards.write_text(hazards.read_text().replace("max_rate: 0.10", "max_rate: 0.20"))
    with pytest.raises(IntegrityError, match="changed in between"):
        runfolder.copy_snapshot(packs, "toypack", tmp_path, pack.sha256)


# ── The record make_run writes, as the live runner will ───────────────────────────────────────────


def test_make_run_writes_the_whole_run_record_and_no_grading(run):
    files = {p.name for p in run.iterdir()}
    assert files == {
        "manifest.json",
        "execution.json",
        "snapshot",
        "expectations.json",
        "lint.json",
        "toy-open",
    }
    manifest, execution = record(run)
    pack = load_pack(FIXTURE_PACKS, "toypack")
    prompt = (FIXTURE_PACKS / "toypack" / "targets" / "basic.prompt.md").read_bytes()
    assert manifest.run_id == run.name == execution.run_id
    assert manifest.pack_sha256 == pack.sha256 == pack_sha256(run / "snapshot", "toypack")
    assert manifest.acceptance_sha256 == runfolder.acceptance_sha256(pack.hazards)
    assert manifest.system_prompt_sha256 == hashlib.sha256(prompt).hexdigest()
    assert (manifest.target_name, manifest.repeats, manifest.max_turns) == ("basic", 2, 6)
    assert manifest.case_ids == ["toy-inject", "toy-open"] and manifest.created_at == FIXED_NOW
    assert json.loads((run / "expectations.json").read_text()) == {
        "toy-inject": {"status": "shipped", "urgent": False},
        "toy-open": {"status": "open", "urgent": True},
    }
    assert [issue["code"] for issue in json.loads((run / "lint.json").read_text())] == ["L25"] * 3
    assert [e.file for e in execution.trials] == ["toy-open/run_1.json", "toy-open/run_2.json"]
    for entry in execution.trials:
        data = (run / entry.file).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry.sha256
        assert Trial.model_validate_json(data).run_id == manifest.run_id  # stamped


def test_make_run_refuses_a_pack_with_lint_errors_and_writes_nothing(packs, tmp_path):
    hazards = packs / "toypack" / "hazards.yaml"
    hazards.write_text(hazards.read_text().replace("severity: serious", "severity: fatal"))
    with pytest.raises(ValueError, match="L11"):
        make_run(tmp_path / "runs", packs, "toypack", [])
    assert not (tmp_path / "runs").exists()


def test_the_acceptance_hash_follows_the_declared_criteria_only(packs):
    before = runfolder.acceptance_sha256(load_pack(packs, "toypack").hazards)
    hazards = packs / "toypack" / "hazards.yaml"
    hazards.write_text(hazards.read_text().replace("harm: A privacy breach.", "harm: A leak."))
    assert runfolder.acceptance_sha256(load_pack(packs, "toypack").hazards) == before
    hazards.write_text(hazards.read_text().replace("max_rate: 0.10", "max_rate: 0.20"))
    assert runfolder.acceptance_sha256(load_pack(packs, "toypack").hazards) != before


def test_an_older_acceptance_model_explains_a_hash_only_by_fields_no_hazard_sets():
    criteria = {h: c.acceptance for h, c in load_pack(FIXTURE_PACKS, "toypack").hazards.items()}
    older = runfolder.older_acceptance_fields
    assert older(criteria, runfolder.criteria_sha256(criteria)) == ()
    lacking = runfolder.criteria_sha256(criteria, ["confidence", "max_invalid_share"])
    assert older(criteria, lacking) == ("confidence", "max_invalid_share")
    # H-02 sets zero_tolerance: a model without it could not have read the file
    assert older(criteria, runfolder.criteria_sha256(criteria, ["zero_tolerance"])) is None
    assert older(criteria, "0" * 64) is None


def test_the_harness_git_state_is_unknown_without_git(monkeypatch):
    def no_git(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", no_git)
    assert runfolder.harness_git() == (None, None)


# ── Reading transcripts back ──────────────────────────────────────────────────────────────────────


def add_trial_file(run: Path, trial: Trial, file: str) -> None:
    """Write and index one more transcript file, as `--resume` does."""
    manifest, execution = record(run)
    stamped = trial.model_copy(update={"run_id": manifest.run_id})
    entry = TrialIndexEntry(
        case_id=trial.case_id,
        n=trial.n,
        attempt=trial.attempt,
        file=file,
        sha256=runfolder.write_json(run / file, stamped),
        status=trial.status,
        stop_reason=trial.stop_reason,
        cost_usd=0.0,
    )
    index(run, [*execution.trials, entry])


def test_load_trials_returns_the_latest_attempt_of_each_trial_with_its_sha256(run):
    add_trial_file(run, toy_trial("toy-inject", attempt=1), "toy-inject/run_1.attempt-1.json")
    add_trial_file(run, toy_trial("toy-inject", attempt=2), "toy-inject/run_1.json")
    loaded = runfolder.load_trials(run, *record(run))
    assert [(t.case_id, t.n, t.attempt) for t, _ in loaded] == [
        ("toy-inject", 1, 2),
        ("toy-open", 1, 1),
        ("toy-open", 2, 1),
    ]
    latest = (run / "toy-inject" / "run_1.json").read_bytes()
    assert loaded[0][1] == hashlib.sha256(latest).hexdigest()


def tamper(run: Path, how: str) -> None:
    """Break the record of toy-open's trial 1 in one way."""
    file = run / "toy-open" / "run_1.json"
    first, *rest = record(run)[1].trials
    if how == "changed":
        file.write_text(file.read_text().replace("It is open.", "It is shut."))
    elif how == "missing":
        file.unlink()
    elif how == "not a trial":  # a consistent index: only the content is wrong
        file.write_text("{}\n")
        index(
            run, [first.model_copy(update={"sha256": hashlib.sha256(b"{}\n").hexdigest()}), *rest]
        )
    elif how == "another run":
        sha = runfolder.write_json(file, toy_trial().model_copy(update={"run_id": "another-run"}))
        index(run, [first.model_copy(update={"sha256": sha}), *rest])
    elif how == "outside the folder":
        index(run, [first.model_copy(update={"file": "../elsewhere.json"}), *rest])
    elif how == "indexed twice":
        index(run, [first, *rest, first])
    elif how == "case not selected":
        index(run, [first.model_copy(update={"case_id": "toy-alt"}), *rest])
    elif how == "beyond the repeats":
        index(run, [first.model_copy(update={"n": 3}), *rest])


@pytest.mark.parametrize(
    ("how", "message"),
    [
        ("changed", "the transcript changed"),
        ("missing", "No such file"),
        ("not a trial", "no longer a valid trial"),
        ("another run", "the manifest and the index say otherwise"),
        ("outside the folder", "not the file of trial 1"),
        ("indexed twice", "indexed twice"),
        ("case not selected", "not in the run's selection"),
        ("beyond the repeats", "beyond the run's repeats"),
    ],
)
def test_load_trials_refuses_a_transcript_that_does_not_match_the_record(run, how, message):
    tamper(run, how)
    with pytest.raises(IntegrityError, match=message):
        runfolder.load_trials(run, *record(run))


def test_a_manifest_that_does_not_parse_or_an_execution_of_another_run_is_refused(run):
    manifest, execution = record(run)
    runfolder.write_execution(run, execution.model_copy(update={"run_id": "another-run"}))
    with pytest.raises(IntegrityError, match="the manifest's is"):
        runfolder.read_execution(run, manifest)
    (run / "manifest.json").write_text("{not json")
    with pytest.raises(IntegrityError, match="not a valid RunManifest"):
        runfolder.read_manifest(run)


def test_verify_snapshot_catches_a_changed_or_missing_snapshot_file(run):
    manifest = record(run)[0]
    runfolder.verify_snapshot(run, manifest)
    pack_yaml = run / "snapshot" / "toypack" / "pack.yaml"
    pack_yaml.write_text(pack_yaml.read_text().replace("max_turns: 6", "max_turns: 7"))
    with pytest.raises(IntegrityError, match="a snapshot file changed"):
        runfolder.verify_snapshot(run, manifest)
    pack_yaml.unlink()
    with pytest.raises(IntegrityError, match="snapshot"):
        runfolder.verify_snapshot(run, manifest)


# ── Gradings ──────────────────────────────────────────────────────────────────────────────────────


def graded_at(moment: datetime) -> Results:
    return Results(
        run_id="r",
        graded_at=moment,
        graded_against="snapshot",
        pack_sha256="0" * 64,
        harness_version="0.1.0",
        harness_git_sha=None,
        expectations_match=True,
        checks=[],
        trials=[],
        cases=[],
        case_hazards=[],
        hazards=[],
        lint=[],
    )


def test_a_new_grading_moves_the_previous_pair_to_history_named_after_its_grading_time(run):
    days = [FIXED_NOW + timedelta(days=d) for d in range(4)]
    for day in days:
        runfolder.write_grading(run, graded_at(day), f"report of {day:%d}\n")
    assert (run / "report.md").read_text() == "report of 04\n"
    history = run / "history"
    assert sorted(p.name for p in history.iterdir()) == [
        "2026-01-01_120000",
        "2026-01-02_120000",
        "2026-01-03_120000",
    ]
    assert (history / "2026-01-02_120000" / "report.md").read_text() == "report of 02\n"
    earlier = Results.model_validate_json(
        (history / "2026-01-02_120000" / "results.json").read_bytes()
    )
    assert earlier.graded_at == days[1]


def test_two_gradings_at_the_same_time_keep_both(run):
    for text in ("first\n", "second\n", "third\n"):
        runfolder.write_grading(run, graded_at(FIXED_NOW), text)
    history = run / "history"
    assert (history / "2026-01-01_120000" / "report.md").read_text() == "first\n"
    assert (history / "2026-01-01_120000_2" / "report.md").read_text() == "second\n"


def test_an_unreadable_earlier_grading_is_kept_not_lost(run):
    (run / "results.json").write_text("{broken")
    runfolder.write_grading(run, graded_at(FIXED_NOW), "new report\n")
    assert (run / "history" / "unknown" / "results.json").read_text() == "{broken"


def test_a_regrade_never_touches_the_runs_own_pair(run):
    runfolder.write_grading(run, graded_at(FIXED_NOW), "the run's report\n")
    out = runfolder.write_regrade(
        run, graded_at(FIXED_NOW), "regraded\n", clock=lambda: FIXED_NOW + timedelta(hours=1)
    )
    assert out == run / "regrade-2026-01-01_130000"
    assert (out / "report.md").read_text() == "regraded\n"
    assert (run / "report.md").read_text() == "the run's report\n"
    assert not (run / "history").exists()
