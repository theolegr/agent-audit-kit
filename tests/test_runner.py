"""Live runs with `ScriptedLLM`, no model behind it: the run folder and its write
order, what the agent is sent, every stop reason, round-robin repeats, the cost guard (refusal
before any client, the cap, a partial report), `--resume`, and live = replay."""

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import pytest

from auditkit import runfolder
from auditkit.cli import load_dotenv, main
from auditkit.llm import ProviderError
from auditkit.testing import FIXED_NOW, REFUSAL, Reply, ScriptedLLM, say, use, user_says

FIXTURE_PACKS = Path(__file__).resolve().parent / "fixtures" / "packs"
SECRET = "SIDE-7731"  # a side-channel value the agent must never see


@pytest.fixture(autouse=True)
def _no_model_settings(monkeypatch):
    """The shell's model and budget settings never reach these runs."""
    for name in ("AGENT_MODEL", "SIMULATOR_MODEL", "EVAL_BUDGET_USD"):
        monkeypatch.delenv(name, raising=False)


def toy_user(request):
    """The toy simulator: asks about the record of its brief."""
    record = re.search(r"record (R-[0-9]+)", request["system"]).group(1)
    return user_says(f"Where is {record}?", {"asking_about": record})


def toy_agent(request, *, output_tokens=50):
    """The toy agent: looks the record up, then answers with its status and the final JSON."""
    last = request["messages"][-1].blocks[0]
    if last.type == "tool_result":
        status = json.loads(last.content)["status"]
        return Reply(f'It is {status}. {{"status": "{status}"}}', output_tokens=output_tokens)
    record = re.search(r"R-[0-9]+", last.text).group(0)
    return Reply("", [("lookup_record", {"record": record})], output_tokens=output_tokens)


def scripted(**scripts):
    return ScriptedLLM(**({"agent": toy_agent, "simulator": toy_user} | scripts))


def live(tmp_path, llm, *extra, cases="toy-open", packs=FIXTURE_PACKS, made=None):
    argv = ["--domain", "toypack", "--packs-dir", str(packs), "--runs-dir", str(tmp_path / "runs")]
    if cases:
        argv += ["--cases", cases]

    def make():
        if made is not None:
            made.append(1)
        return llm

    code = main([*argv, *extra], clock=lambda: FIXED_NOW, make_client=make)
    runs = sorted((tmp_path / "runs").iterdir()) if (tmp_path / "runs").is_dir() else []
    return code, (runs[-1] if runs else None)


def read(run, name):
    return json.loads((run / name).read_text())


def trial(run, case_id, n, attempt=None):
    return json.loads((run / runfolder.trial_file(case_id, n, attempt)).read_text())


# ── The run folder ────────────────────────────────────────────────────────────────────────────────


def test_a_live_run_writes_the_run_folder_and_grades_it(tmp_path, capsys):
    llm = scripted(served={"agent": "claude-haiku-5-5-20261001"})
    code, run = live(tmp_path, llm)
    assert code == 0
    names = {p.relative_to(run).as_posix() for p in run.rglob("*") if p.is_file()}
    assert {"manifest.json", "execution.json", "expectations.json", "lint.json"} <= names
    assert {"toy-open/run_1.json", "toy-open/run_2.json", "results.json", "report.md"} <= names
    assert "snapshot/toypack/pack.yaml" in names and "snapshot/toybase/pack.yaml" in names
    manifest, execution = read(run, "manifest.json"), read(run, "execution.json")
    assert [(m["role"], m["requested"], m["effort"]) for m in manifest["models"]] == [
        ("agent", "claude-haiku-5-5", "medium"),
        ("simulator", "claude-haiku-5-5", "low"),
    ]
    assert execution["status"] == "completed" and execution["first_call_at"] is not None
    assert execution["served_models"] == {
        "agent": ["claude-haiku-5-5-20261001"],
        "simulator": ["claude-haiku-5-5"],
    }
    assert execution["spent_usd"] == pytest.approx(sum(t["cost_usd"] for t in execution["trials"]))
    for entry in execution["trials"]:  # the index holds each file's hash
        assert entry["sha256"] == hashlib.sha256((run / entry["file"]).read_bytes()).hexdigest()
    first = trial(run, "toy-open", 1)
    assert (first["status"], first["stop_reason"], first["source"]) == (
        "completed",
        "outcome_emitted",
        "live",
    )
    assert first["exposes"] == sorted(
        ["turn_text", "tool_calls", "final_outcome", "side_channel", "ground_truth"]
    )
    assert (run / "report.md").read_text().startswith("# Risk-based evaluation report")
    out = capsys.readouterr().out
    assert f"graded {run.name}: 2 trial(s) graded against its snapshot" in out


def test_the_manifest_and_snapshot_are_written_before_the_first_model_call(tmp_path):
    seen = []

    def first_user(request):
        if not seen:
            (run,) = (tmp_path / "runs").iterdir()
            seen.append(
                (
                    (run / "manifest.json").is_file(),
                    (run / "snapshot" / "toypack" / "pack.yaml").is_file(),
                    read(run, "execution.json")["first_call_at"] is not None,
                    list(run.glob("toy-open/*")),
                )
            )
        return toy_user(request)

    assert live(tmp_path, scripted(simulator=first_user))[0] == 0
    assert seen == [(True, True, True, [])]


def test_tool_calls_are_recorded_with_arguments_result_and_timestamps(tmp_path):
    _, run = live(tmp_path, scripted())
    (agent,) = [t for t in trial(run, "toy-open", 1)["transcript"]["turns"] if t["role"] == "agent"]
    (call,) = agent["tool_calls"]
    assert (call["name"], call["arguments"], call["is_error"], call["executed_by"]) == (
        "lookup_record",
        {"record": "R-1"},
        False,
        "harness_mock",
    )
    assert call["result"]["status"] == "open" and call["called_at"] and call["returned_at"]
    assert [c["stop_reason"] for c in agent["model_calls"]] == ["tool_use", "end_turn"]


def test_no_agent_request_holds_the_side_channel_the_ground_truth_or_the_test_data(tmp_path):
    def user(request):
        return user_says("Where is R-2?", {"asking_about": SECRET})

    llm = scripted(simulator=user)
    assert live(tmp_path, llm, "--repeats", "1", cases="toy-inject")[0] == 0
    sent = json.dumps(
        [
            [
                r["system"],
                [m.model_dump() for m in r["messages"]],
                [t.parameters for t in r["tools"]],
            ]
            for r in llm.sent_to("agent")
        ]
    )
    case_text = ["note-injection", "ask-other", "PINEAPPLE", "chatty"]
    persona = "Synthetic customer asking about one record."
    brief = "You want to know the status of record R-2."
    for leak in [SECRET, *case_text, persona, brief, '"asking_about"']:
        assert leak not in sent, leak
    assert "Where is R-2?" in sent  # the user's text does cross


# ── Stop reasons ──────────────────────────────────────────────────────────────────────────────────


def ending_pack(tmp_path):
    packs = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, packs, ignore=shutil.ignore_patterns("__pycache__"))
    target = packs / "toypack" / "targets" / "basic.yaml"
    target.write_text(target.read_text() + "end_when: simulator\n")
    return packs


@pytest.mark.parametrize(
    ("scripts", "extra", "status", "stop"),
    [
        pytest.param({}, (), "completed", "outcome_emitted", id="outcome_emitted"),
        pytest.param(
            {"agent": [say("Let me think.")] * 2},
            ("--max-turns", "2"),
            "completed",
            "max_turns",
            id="max_turns",
        ),
        pytest.param(
            {"agent": lambda r: use("lookup_record", {"record": "R-1"})},
            (),
            "completed",
            "tool_round_cap",
            id="tool_round_cap",
        ),
        pytest.param(
            {"simulator": [REFUSAL]}, (), "completed", "simulator_failed", id="simulator_failed"
        ),
        pytest.param(
            {"agent": [ProviderError("overloaded")]}, (), "error", "error", id="provider error"
        ),
        pytest.param({"agent": [RuntimeError("a bug")]}, (), "error", "error", id="harness error"),
    ],
)
def test_every_way_a_conversation_ends_is_its_stop_reason(tmp_path, scripts, extra, status, stop):
    code, run = live(tmp_path, scripted(**scripts), "--repeats", "1", *extra)
    got = trial(run, "toy-open", 1)
    assert (got["status"], got["stop_reason"]) == (status, stop)
    assert code == (4 if status == "error" else 0)
    if stop == "max_turns":
        assert sum(t["role"] == "user" for t in got["transcript"]["turns"]) == 2
    if stop == "tool_round_cap":
        (agent,) = [t for t in got["transcript"]["turns"] if t["role"] == "agent"]
        assert len(agent["tool_calls"]) == 5 and len(agent["model_calls"]) == 6
    if stop == "simulator_failed":
        assert [t["role"] for t in got["transcript"]["turns"]] == ["user"]
    if status == "error":
        assert got["fault"] == ("provider" if "overloaded" in got["error"] else "harness")


def test_the_simulator_ends_a_conversation_only_when_the_target_lets_it(tmp_path):
    users = [user_says("Where is R-1?", {"asking_about": "R-1"}), user_says("Bye.", {}, end=True)]
    llm = scripted(simulator=users, agent=[say("Let me check.")])
    code, run = live(tmp_path, llm, "--repeats", "1", packs=ending_pack(tmp_path))
    got = trial(run, "toy-open", 1)
    assert (code, got["stop_reason"]) == (0, "simulator_ended")
    assert "end_conversation" in llm.sent_to("simulator")[0]["tools"][0].parameters["properties"]


def test_a_provider_error_fails_that_trial_and_the_run_goes_on(tmp_path, capsys):
    calls = []

    def agent(request):
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("RateLimitError (429): slow down")
        return toy_agent(request)

    code, run = live(tmp_path, scripted(agent=agent))
    assert code == 4
    assert [(e["n"], e["status"]) for e in read(run, "execution.json")["trials"]] == [
        (1, "error"),
        (2, "completed"),
    ]
    assert trial(run, "toy-open", 1)["error"] == "RateLimitError (429): slow down"
    assert "1 trial(s) errored" in capsys.readouterr().err
    assert (run / "report.md").is_file()


# ── Repeats and models ────────────────────────────────────────────────────────────────────────────


def test_trials_run_round_robin_every_case_s_trial_1_first(tmp_path):
    code, run = live(tmp_path, scripted(), cases="toy-open,toy-inject")
    assert code == 0
    order = [(e["case_id"], e["n"]) for e in read(run, "execution.json")["trials"]]
    assert order == [("toy-open", 1), ("toy-inject", 1), ("toy-open", 2), ("toy-inject", 2)]


def test_the_models_come_from_the_environment_and_unknown_ones_stop_before_any_client(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5-5")
    llm = scripted()
    _, run = live(tmp_path, llm, "--repeats", "1")
    assert {r["model"] for r in llm.sent_to("agent")} == {"claude-sonnet-5-5"}
    assert {r["model"] for r in llm.sent_to("simulator")} == {"claude-haiku-5-5"}
    assert read(run, "manifest.json")["models"][0]["requested"] == "claude-sonnet-5-5"

    monkeypatch.setenv("AGENT_MODEL", "gpt-unknown")
    made = []
    code, _ = live(tmp_path / "second", scripted(), made=made)
    assert (code, made) == (2, [])
    assert "gpt-unknown" in capsys.readouterr().err


def test_dotenv_fills_missing_variables_and_the_environment_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# models\nexport AGENT_MODEL='claude-sonnet-5-5'\nEVAL_BUDGET_USD=1.5\n\nbad line\n"
    )
    environ = {"EVAL_BUDGET_USD": "2"}
    monkeypatch.setattr(os, "environ", environ)  # load_dotenv writes here, not in the real one
    load_dotenv(env)
    assert environ == {"AGENT_MODEL": "claude-sonnet-5-5", "EVAL_BUDGET_USD": "2"}
    load_dotenv(tmp_path / "missing")  # no file: nothing happens
    assert len(environ) == 2


# ── The cost guard ────────────────────────────────────────────────────────────────────────────────


def test_an_estimate_above_the_cap_exits_3_before_any_client_exists(tmp_path, capsys):
    made = []
    code, run = live(tmp_path, scripted(), "--budget-usd", "0.0001", made=made)
    assert (code, run, made) == (3, None, [])
    assert "refused: the typical estimate" in capsys.readouterr().err


def costly(request):  # about $0.0005 a call: close to a call's reservation at max_tokens 1024
    reply = (
        toy_agent(request, output_tokens=1000) if request["role"] == "agent" else toy_user(request)
    )
    reply.output_tokens = 1000
    return reply


def budget_stopped(tmp_path):
    llm = ScriptedLLM(agent=costly, simulator=costly)
    return live(tmp_path, llm, "--repeats", "3", "--budget-usd", "0.0025")


def test_the_cap_stops_the_run_with_its_trials_kept_and_a_partial_report(tmp_path, capsys):
    code, run = budget_stopped(tmp_path)
    assert code == 3
    execution = read(run, "execution.json")
    assert execution["status"] == "aborted_budget"
    assert [(e["n"], e["status"], e["stop_reason"]) for e in execution["trials"]] == [
        (1, "completed", "outcome_emitted"),
        (2, "aborted", "budget"),
    ]
    assert not (run / runfolder.trial_file("toy-open", 3)).exists()
    assert execution["spent_usd"] <= 0.0025 + 0.0001
    assert "Partial run" in (run / "report.md").read_text()
    assert "the budget cap was reached: partial report written" in capsys.readouterr().err


def test_resume_keeps_the_earlier_attempt_and_runs_only_the_missing_trials(tmp_path, monkeypatch):
    _, run = budget_stopped(tmp_path)
    monkeypatch.setenv("AGENT_MODEL", "claude-sonnet-5-5")  # the manifest's models win
    llm = scripted()
    code = main(
        ["--resume", str(run), "--budget-usd", "1"],
        clock=lambda: FIXED_NOW,
        make_client=lambda: llm,
    )
    assert code == 0
    assert len(llm.sent_to("simulator")) == 2 and len(llm.sent_to("agent")) == 4  # trials 2 and 3
    assert {r["model"] for r in llm.requests} == {"claude-haiku-5-5"}
    kept = trial(run, "toy-open", 2, attempt=1)
    again = trial(run, "toy-open", 2)
    assert (kept["attempt"], kept["status"]) == (1, "aborted")
    assert (again["attempt"], again["status"]) == (2, "completed")
    execution = read(run, "execution.json")
    assert execution["status"] == "completed" and len(execution["resumed_at"]) == 1
    assert [(e["n"], e["attempt"], e["file"]) for e in execution["trials"]] == [
        (1, 1, "toy-open/run_1.json"),
        (2, 1, "toy-open/run_2.attempt-1.json"),
        (2, 2, "toy-open/run_2.json"),
        (3, 1, "toy-open/run_3.json"),
    ]
    assert list((run / "history").iterdir())  # the partial grading is kept
    assert main(["--replay", str(run)], clock=lambda: FIXED_NOW) == 0  # the folder still verifies

    nothing = ScriptedLLM()
    assert main(["--resume", str(run)], clock=lambda: FIXED_NOW, make_client=lambda: nothing) == 0
    assert nothing.requests == []


def always_fails(request):
    raise ProviderError("InternalServerError (529): overloaded")


def resume(run, llm, *extra):
    return main(["--resume", str(run), *extra], clock=lambda: FIXED_NOW, make_client=lambda: llm)


def test_a_resume_cut_short_can_itself_be_resumed_every_attempt_kept(tmp_path):
    two = "toy-open,toy-inject"
    code, run = live(tmp_path, scripted(agent=always_fails), "--repeats", "1", cases=two)
    assert code == 4

    def interrupt(request):
        raise KeyboardInterrupt

    assert resume(run, scripted(agent=interrupt)) == 130  # stopped inside the first retry
    assert read(run, "execution.json")["status"] == "interrupted"
    llm = scripted()
    assert resume(run, llm) == 0
    assert len(llm.sent_to("simulator")) == 2  # one retry per case, nothing else
    files = sorted(e["file"] for e in read(run, "execution.json")["trials"])
    assert files == [
        "toy-inject/run_1.attempt-1.json",
        "toy-inject/run_1.json",
        "toy-open/run_1.attempt-1.json",
        "toy-open/run_1.json",
    ]
    assert trial(run, "toy-open", 1)["attempt"] == 2 and trial(run, "toy-inject", 1)["attempt"] == 2


class Billed:
    """A client that keeps the cost of every call it returns: what the meter charges."""

    def __init__(self, inner):
        self.inner, self.costs = inner, []

    def complete(self, **request):
        completion = self.inner.complete(**request)
        self.costs.append(completion.call.cost_usd)
        return completion


def test_a_ctrl_c_mid_trial_still_counts_what_that_trial_paid_for(tmp_path):
    def agent(request):
        if len(llm.sent_to("agent")) > 2:  # trial 2, after its charged simulator call
            raise KeyboardInterrupt
        return toy_agent(request)

    llm = scripted(agent=agent)
    billed = Billed(llm)
    code, run = live(tmp_path, billed, "--repeats", "2")
    assert code == 130
    execution = read(run, "execution.json")
    assert execution["status"] == "interrupted" and len(execution["trials"]) == 1
    assert len(billed.costs) == 4  # trial 1's three calls, then trial 2's simulator call
    assert execution["spent_usd"] == pytest.approx(sum(billed.costs), abs=1e-8)


def test_a_resume_stopped_by_its_cap_can_be_resumed_again(tmp_path):
    two = "toy-open,toy-inject"
    _, run = live(tmp_path, scripted(agent=always_fails), "--repeats", "1", cases=two)
    stopped = ScriptedLLM(agent=costly, simulator=costly)
    assert resume(run, stopped, "--budget-usd", "0.0025") == 3  # the 2nd retry hits the cap
    assert resume(run, scripted(), "--budget-usd", "1") == 0
    entries = [
        (e["case_id"], e["attempt"], e["status"]) for e in read(run, "execution.json")["trials"]
    ]
    assert sorted(entries) == [
        ("toy-inject", 1, "error"),
        ("toy-inject", 2, "aborted"),
        ("toy-inject", 3, "completed"),
        ("toy-open", 1, "error"),
        ("toy-open", 2, "completed"),
    ]
    assert (run / "toy-inject" / "run_1.attempt-2.json").is_file()


def test_an_exchange_cut_short_keeps_what_it_did_and_paid_for(tmp_path):
    replies = [
        use("lookup_record", {"record": "R-1"}),
        ProviderError("RateLimitError (429): later"),
    ]
    _, run = live(tmp_path, scripted(agent=replies), "--repeats", "1")
    got = trial(run, "toy-open", 1)
    assert (got["status"], got["stop_reason"]) == ("error", "error")
    agent = got["transcript"]["turns"][-1]
    assert agent["role"] == "agent" and [c["name"] for c in agent["tool_calls"]] == [
        "lookup_record"
    ]
    paid = [c["cost_usd"] for t in got["transcript"]["turns"] for c in t["model_calls"]]
    assert len(paid) == 2 and sum(paid) == pytest.approx(got["cost_usd"])


def test_resume_refuses_a_tampered_snapshot(tmp_path, capsys):
    _, run = budget_stopped(tmp_path)
    pack_yaml = run / "snapshot" / "toypack" / "pack.yaml"
    pack_yaml.write_text(pack_yaml.read_text() + "\n# edited\n")
    made = []
    code = main(["--resume", str(run)], make_client=lambda: made.append(1))
    assert (code, made) == (5, [])
    assert "integrity error, nothing sent" in capsys.readouterr().err


# ── Live and replay grade alike ───────────────────────────────────────────────────────────────────


def wrong_status(request):
    """Looks the record up, then reports a status it never saw."""
    if request["messages"][-1].blocks[0].type == "tool_result":
        return say('It is closed. {"status": "closed"}')
    return toy_agent(request)


def test_a_replay_of_a_live_run_gives_the_same_verdicts(tmp_path):
    _, run = live(tmp_path, scripted(agent=wrong_status), cases="toy-open,toy-inject")
    graded = read(run, "results.json")
    assert main(["--replay", str(run)], clock=lambda: FIXED_NOW) == 0
    replayed = read(run, "results.json")
    assert [(h["hazard_id"], h["verdict"]) for h in replayed["hazards"]] == [
        (h["hazard_id"], h["verdict"]) for h in graded["hazards"]
    ]
    assert graded["trials"] == replayed["trials"] and graded["cases"] == replayed["cases"]
    assert any(h["verdict"] == "not_met" for h in graded["hazards"])  # the wrong status is caught
