"""The `eval` command line. The live modes are tested in test_runner.py, --replay in
test_replay.py."""

import shutil
from pathlib import Path

import pytest

from auditkit import __version__
from auditkit.cli import main

FLAGS = [
    "--domain NAME",
    "--lint NAME",
    "--replay RUN_DIR",
    "--resume RUN_DIR",
    "--ui",
    "--variant NAME",
    "--target NAME",
    "--cases IDS",
    "--repeats N",
    "--max-turns N",
    "--budget-usd USD",
    "--regrade-current",
    "--port N",
    "--no-browser",
    "--packs-dir DIR",
    "--runs-dir DIR",
]


def test_help_exits_0_with_the_approved_usage(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: eval [-h] [--version]\n")
    assert "(--domain NAME | --lint NAME | --replay RUN_DIR | --resume RUN_DIR | --ui)" in out
    for flag in FLAGS:
        assert f"  {flag}" in out, flag
    assert "--domain and --resume call the model API and cost money" in out
    assert "a live run from it asks first" in out
    assert "exit codes: 0 report written" in out
    assert "(reserved: 1 = a hazard not met, only with the future --gate)" in out


def test_help_does_not_depend_on_the_terminal_width(capsys, monkeypatch):
    main(["--help"])
    wide = capsys.readouterr().out
    monkeypatch.setenv("COLUMNS", "40")
    main(["--help"])
    assert capsys.readouterr().out == wide


def test_version_prints_the_package_version(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"eval {__version__}"


def test_no_mode_is_a_usage_error(capsys):
    assert main([]) == 2
    assert "one of the arguments --domain --lint --replay --resume --ui is required" in (
        capsys.readouterr().err
    )


def test_modes_are_exclusive(capsys):
    assert main(["--lint", "toypack", "--replay", "runs/x"]) == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_a_live_run_without_credentials_exits_2_and_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    argv = ["--domain", "toypack", "--packs-dir", str(FIXTURE_PACKS), "--runs-dir", str(tmp_path)]
    assert main(argv) == 2
    assert (
        "no ANTHROPIC_API_KEY in the environment or .env: nothing sent" in capsys.readouterr().err
    )
    assert list(tmp_path.iterdir()) == []


def test_resume_needs_a_run_folder(tmp_path, capsys):
    assert main(["--resume", str(tmp_path)]) == 2
    assert "not a run folder" in capsys.readouterr().err


def test_regrade_current_without_replay_is_a_usage_error(capsys):
    assert main(["--lint", "toypack", "--regrade-current"]) == 2
    assert "--regrade-current goes with --replay" in capsys.readouterr().err


# ── --lint ────────────────────────────────────────────────────────────────────────────────────────

FIXTURE_PACKS = Path(__file__).parent / "fixtures" / "packs"
TOY = ["--lint", "toypack", "--packs-dir", str(FIXTURE_PACKS)]


@pytest.fixture(autouse=True)
def _no_model_settings(monkeypatch):
    for var in ("AGENT_MODEL", "SIMULATOR_MODEL", "EVAL_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)


def test_lint_of_the_toy_pack_exits_0_and_prints_the_estimate(capsys):
    assert main(TOY) == 0
    out = capsys.readouterr().out
    assert out.startswith("lint toypack (variant default): 0 error(s), 3 warning(s)\n")
    assert "estimate: 2 case(s) x 2 repeat(s) = 4 trial(s), target basic" in out
    assert "claude-haiku-5-5" in out
    assert "cap $3.00" in out
    assert "would be refused" not in out


def test_lint_selection_changes_the_estimate(capsys):
    assert main([*TOY, "--cases", "toy-open", "--repeats", "3", "--max-turns", "4"]) == 0
    assert "1 case(s) x 3 repeat(s) = 3 trial(s)" in capsys.readouterr().out


def test_lint_error_exits_2_and_writes_nothing(tmp_path, capsys):
    packs = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, packs, ignore=shutil.ignore_patterns("__pycache__"))
    hazards = packs / "toypack" / "hazards.yaml"
    hazards.write_text(hazards.read_text().replace("severity: serious", "severity: fatal"))
    before = sorted(p for p in tmp_path.rglob("*"))
    runs = tmp_path / "runs"
    assert main(["--lint", "toypack", "--packs-dir", str(packs), "--runs-dir", str(runs)]) == 2
    captured = capsys.readouterr()
    assert "error   L11" in captured.out
    assert "nothing run, nothing written" in captured.err
    assert "estimate" not in captured.out
    assert sorted(p for p in tmp_path.rglob("*")) == before


def test_lint_with_an_unpriced_model_exits_2(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_MODEL", "claude-unknown")
    assert main(TOY) == 2
    assert "no price for model 'claude-unknown'" in capsys.readouterr().err


def test_lint_names_the_model_settings_it_used(monkeypatch, capsys):
    monkeypatch.setenv("SIMULATOR_MODEL", "claude-sonnet-5-5")
    assert main(TOY) == 0
    out = capsys.readouterr().out
    assert "claude-sonnet-5-5" in out and "(SIMULATOR_MODEL)" in out and "(default)" in out


def test_a_case_named_twice_is_counted_once(capsys):
    assert main([*TOY, "--cases", "toy-open,toy-open"]) == 0
    assert "1 case(s) x 2 repeat(s) = 2 trial(s)" in capsys.readouterr().out


def test_lint_with_an_unknown_case_exits_2(capsys):
    assert main([*TOY, "--cases", "toy-open,nope"]) == 2
    assert "no case nope" in capsys.readouterr().err


def test_lint_says_when_a_live_run_would_be_refused(monkeypatch, capsys):
    monkeypatch.setenv("EVAL_BUDGET_USD", "0.0001")
    assert main(TOY) == 0
    assert "a live run would be refused" in capsys.readouterr().out


def test_budget_flag_wins_over_the_environment(monkeypatch, capsys):
    monkeypatch.setenv("EVAL_BUDGET_USD", "0.0001")
    assert main([*TOY, "--budget-usd", "5"]) == 0
    out = capsys.readouterr().out
    assert "cap $5.00" in out and "would be refused" not in out


@pytest.mark.parametrize("flag", ["--repeats", "--max-turns"])
def test_counts_below_1_are_usage_errors(flag, capsys):
    assert main([*TOY, flag, "0"]) == 2
    assert "not a positive integer" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "0", "lots"])
def test_invalid_budget_in_the_environment_exits_2(monkeypatch, value, capsys):
    monkeypatch.setenv("EVAL_BUDGET_USD", value)
    assert main(TOY) == 2
    assert "not a positive amount of USD" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["nan", "inf", "-5", "0"])
def test_invalid_budget_flag_is_a_usage_error(value, capsys):
    assert main([*TOY, "--budget-usd", value]) == 2
    assert "not a positive amount of USD" in capsys.readouterr().err
