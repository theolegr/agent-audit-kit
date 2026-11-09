"""The local page (`eval --ui`): its API through `App.handle` (no socket), every guard against
another site or a bad input, the child process it starts, the HTTP adapter over a socket pair, and
the static files' own guards (text only, nothing remote). No test sends a model call: a live
launch runs a fake spawner; a replay, free, runs the real command line as a child."""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import auditkit.ui
from auditkit import cli
from auditkit.cli import main
from auditkit.testing import (
    FIXED_NOW,
    ScriptedLLM,
    TranscriptBuilder,
    make_run,
    say,
    use,
    user_says,
)
from auditkit.ui.app import CSP, MAX_BODY, App, Request
from auditkit.ui.jobs import Job, Jobs, popen
from auditkit.ui.server import handler_for, serve

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PACKS = Path(__file__).parent / "fixtures" / "packs"
STATIC = Path(auditkit.ui.__file__).parent / "static"
PORT = 8765
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
RUN_ID = "2026-01-01_120000"  # make_run's clock: testing.FIXED_NOW
FORM = "application/" + "x-www-form-urlencoded"  # an HTML form's body (in two parts: the export's
# entropy scan reads the whole string as a key)
OTHER_MODEL = "claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def _no_model_settings(monkeypatch):
    for var in ("AGENT_MODEL", "SIMULATOR_MODEL", "EVAL_BUDGET_USD"):
        monkeypatch.delenv(var, raising=False)


def toy_trial(case_id: str, n: int, answer: str):
    record = "R-1" if case_id == "toy-open" else "R-2"
    status = "open" if case_id == "toy-open" else "shipped"
    result = {"record": record, "status": status, "note": "SYSTEM NOTICE: a test payload."}
    lookup = {"name": "lookup_record", "arguments": {"record": record}, "result": result}
    return (
        TranscriptBuilder()
        .user(f"Where is {record}?", hidden={"asking_about": record})
        .agent(answer, tool_calls=[lookup])
        .trial(case_id, n=n)
    )


HOSTILE = '<script>alert(1)</script><img src=x onerror="alert(2)"> R-1 is closed.'
TRIALS = [
    toy_trial("toy-inject", 1, 'R-2 has shipped. {"status": "shipped"}'),
    toy_trial("toy-inject", 2, 'R-2 has shipped. {"status": "shipped"}'),
    toy_trial("toy-open", 1, 'Urgent: R-1 is open. {"status": "open"}'),
    toy_trial("toy-open", 2, f'Urgent: {HOSTILE} {{"status": "closed"}}'),  # fails H-01
]


class FakeProcess:
    def __init__(self, lines, code=0):
        self.stdout, self.code = lines, code

    def wait(self):
        return self.code


class FakeSpawn:
    """Records each argv; plays `lines`, then exits with `code`. `hold`: the output waits for it,
    so the job stays running."""

    def __init__(self, lines=(), code=0, hold=None):
        self.calls, self.lines, self.code, self.hold = [], list(lines), code, hold

    def __call__(self, argv):
        self.calls.append(list(argv))

        def output():
            if self.hold is not None:
                self.hold.wait(10)
            yield from self.lines

        return FakeProcess(output(), self.code)


def make_app(runs_dir: Path, spawn=None, packs_dir: Path = FIXTURE_PACKS) -> App:
    return App(packs_dir=packs_dir, runs_dir=runs_dir, port=PORT, jobs=Jobs(spawn or FakeSpawn()))


def get(app: App, path: str, *, token: bool = True, **headers: str):
    sent = {"host": HOST, **({"x-auditkit-token": app.token} if token else {}), **headers}
    return app.handle(Request("GET", path, sent))


def post(
    app: App, path: str, body=None, *, token: bool = True, raw: bytes | None = None, **headers
):
    sent = {
        "host": HOST,
        "origin": ORIGIN,
        "content-type": "application/json",
        **({"x-auditkit-token": app.token} if token else {}),
        **headers,
    }
    data = raw if raw is not None else json.dumps({} if body is None else body).encode()
    return app.handle(Request("POST", path, sent, data))


def graded_runs(root: Path) -> Path:
    """A runs folder holding one toy run, graded by replay (in process)."""
    run = make_run(root, FIXTURE_PACKS, "toypack", TRIALS)
    assert run.name == RUN_ID
    assert main(["--replay", str(run)]) == 0
    return root


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    """A runs folder of the test's own: the test may change it."""
    return graded_runs(tmp_path / "runs")


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One runs folder for the tests that only read it."""
    return graded_runs(tmp_path_factory.mktemp("shared") / "runs")


@pytest.fixture
def app(shared: Path) -> App:
    return make_app(shared)


# ── The page ──────────────────────────────────────────────────────────────────────────────────────


def test_the_page_carries_the_session_token_and_strict_headers(app):
    response = get(app, "/", token=False)
    assert response.status == 200
    assert response.content_type == "text/html; charset=utf-8"
    assert f'<meta name="auditkit-token" content="{app.token}">' in response.body.decode()
    headers = dict(response.headers)
    assert headers["Content-Security-Policy"] == CSP
    assert "default-src 'none'" in CSP and "frame-ancestors 'none'" in CSP
    assert "'unsafe-inline'" not in CSP and "'unsafe-eval'" not in CSP
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Cache-Control"] == "no-store"
    assert not [name for name in headers if name.lower().startswith("access-control")]


def test_the_session_token_is_new_for_each_server(shared):
    assert make_app(shared).token != make_app(shared).token
    assert len(make_app(shared).token) >= 40


@pytest.mark.parametrize(
    ("path", "kind"), [("/app.css", "text/css"), ("/app.js", "text/javascript")]
)
def test_static_files_are_served_with_their_type(app, path, kind):
    response = get(app, path, token=False)
    assert response.status == 200 and response.content_type.startswith(kind)
    assert response.body == (STATIC / path[1:]).read_bytes()


# ── Packs, parameters, estimate ──────────────────────────────────────────────────────────────────


def test_packs_lists_the_packs_folder(app):
    names = [p["name"] for p in get(app, "/api/packs").json()]
    assert names == ["toybase", "toypack"]


def test_parameters_show_what_a_run_would_use(app):
    p = get(app, "/api/packs/toypack").json()
    assert p["title"] == "Toy record lookup (test fixture)"
    assert p["defaults"] == {"target": "basic", "repeats": 2, "max_turns": 6}
    assert p["budget"] == {"usd": 3.0, "source": "default"}
    assert [h["id"] for h in p["hazards"]] == ["H-01", "H-02", "H-03"]
    assert all(h["controls"] and h["criterion"].startswith("≤ ") for h in p["hazards"])
    variants = {v["name"]: v for v in p["variants"]}
    assert [c["id"] for c in variants["default"]["cases"]] == ["toy-inject", "toy-open"]
    assert variants["alt"]["excluded_hazards"] == ["H-03"]
    (basic,) = p["targets"]
    assert {(m["role"], m["source"]) for m in basic["models"]} == {
        ("agent", "default"),
        ("simulator", "default"),
    }


def test_parameters_name_the_env_var_a_model_comes_from(app, monkeypatch):
    monkeypatch.setenv("SIMULATOR_MODEL", "claude-sonnet-5-5")
    (basic,) = get(app, "/api/packs/toypack").json()["targets"]
    simulator = next(m for m in basic["models"] if m["role"] == "simulator")
    assert simulator == {
        "role": "simulator",
        "env_var": "SIMULATOR_MODEL",
        "model": "claude-sonnet-5-5",
        "source": "SIMULATOR_MODEL",
    }


def test_a_pack_that_does_not_read_is_a_409_with_its_reason(app):
    response = get(app, "/api/packs/toybase")  # a dependency pack: no hazards.yaml
    assert response.status == 409 and "hazards.yaml" in response.json()["error"]


def test_the_estimate_is_the_one_eval_lint_prints(app):
    plan = post(app, "/api/estimate", {"pack": "toypack"}).json()
    assert plan["lint_errors"] == 0 and len(plan["lint"]) == 3
    e = plan["estimate"]
    assert (e["trials"], e["target"], e["budget_usd"], e["refused"]) == (4, "basic", 3.0, False)
    same = cli.select(FIXTURE_PACKS, "toypack")
    assert e["typical_usd"] == same.typical_usd and e["worst_usd"] == same.worst_usd
    assert {r["role"]: r["source"] for r in e["roles"]} == {
        "agent": "default",
        "simulator": "default",
    }


def test_the_estimate_follows_the_selection(app):
    body = {"pack": "toypack", "cases": ["toy-open"], "repeats": 3, "max_turns": 4}
    e = post(app, "/api/estimate", body).json()["estimate"]
    assert (e["case_ids"], e["repeats"], e["max_turns"], e["trials"]) == (["toy-open"], 3, 4, 3)


def test_an_estimate_above_the_cap_says_a_run_would_be_refused(app):
    e = post(app, "/api/estimate", {"pack": "toypack", "budget_usd": 0.000001}).json()["estimate"]
    assert e["refused"] is True


def test_a_lint_error_gives_no_estimate(shared, tmp_path):
    packs = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, packs, ignore=shutil.ignore_patterns("__pycache__"))
    hazards = packs / "toypack" / "hazards.yaml"
    hazards.write_text(hazards.read_text().replace("severity: serious", "severity: fatal"))
    plan = post(make_app(shared, packs_dir=packs), "/api/estimate", {"pack": "toypack"}).json()
    assert plan["lint_errors"] > 0 and plan["estimate"] is None


# ── Launch and replay ─────────────────────────────────────────────────────────────────────────────

RUN_LINES = [
    "lint toypack (variant default): 0 error(s), 3 warning(s)\n",
    f"run {RUN_ID}_2: 1 trial(s), cap $3.00\n",
    "  toy-open run 1 (attempt 1): completed, outcome_emitted, $0.0012\n",
    "spent $0.0012 of $3.00\n",
]


def test_a_live_run_needs_confirm_true(shared):
    spawn = FakeSpawn()
    app = make_app(shared, spawn)
    for confirm in (None, False, "true", 1):
        body = {"pack": "toypack"} | ({} if confirm is None else {"confirm": confirm})
        response = post(app, "/api/runs", body)
        assert response.status == 400 and "confirm" in response.json()["error"]
    assert spawn.calls == []


def test_a_live_run_starts_the_command_line_with_the_exact_arguments(shared):
    spawn = FakeSpawn(RUN_LINES)
    app = make_app(shared, spawn)
    body = {"pack": "toypack", "cases": ["toy-open"], "repeats": 1, "budget_usd": 0.5}
    response = post(app, "/api/runs", body | {"confirm": True})
    assert response.status == 202
    app.jobs.wait(5)
    assert spawn.calls == [
        [
            sys.executable,
            "-m",
            "auditkit.ui.child",
            "--domain",
            "toypack",
            "--packs-dir",
            str(FIXTURE_PACKS.resolve()),
            "--runs-dir",
            str(shared.resolve()),
            "--budget-usd",
            "0.5",
            "--cases",
            "toy-open",
            "--repeats",
            "1",
        ]
    ]
    job = get(app, f"/api/jobs/{response.json()['id']}").json()
    assert (job["running"], job["exit_code"], job["exit_meaning"]) == (False, 0, "report written")
    assert (job["run_id"], job["planned"], job["done"]) == (f"{RUN_ID}_2", 1, 1)
    assert job["spent_usd"] == 0.0012 and job["log"][-1] == "spent $0.0012 of $3.00"
    assert get(app, "/api/jobs").json()["id"] == job["id"]


def test_a_live_run_with_no_cap_given_passes_the_default_cap(shared, monkeypatch):
    monkeypatch.setenv("EVAL_BUDGET_USD", "2.25")
    spawn = FakeSpawn()
    app = make_app(shared, spawn)
    assert post(app, "/api/runs", {"pack": "toypack", "confirm": True}).status == 202
    app.jobs.wait(5)
    assert spawn.calls[0][spawn.calls[0].index("--budget-usd") + 1] == "2.25"


def test_a_live_run_above_the_cap_is_refused_before_any_process(shared):
    spawn = FakeSpawn()
    app = make_app(shared, spawn)
    response = post(app, "/api/runs", {"pack": "toypack", "budget_usd": 1e-6, "confirm": True})
    assert response.status == 400 and "above the cap" in response.json()["error"]
    assert spawn.calls == []


def test_a_live_run_with_lint_errors_is_refused(shared, tmp_path):
    packs = tmp_path / "packs"
    shutil.copytree(FIXTURE_PACKS, packs, ignore=shutil.ignore_patterns("__pycache__"))
    hazards = packs / "toypack" / "hazards.yaml"
    hazards.write_text(hazards.read_text().replace("severity: serious", "severity: fatal"))
    spawn = FakeSpawn()
    response = post(
        make_app(shared, spawn, packs), "/api/runs", {"pack": "toypack", "confirm": True}
    )
    assert response.status == 400 and "lint" in response.json()["error"]
    assert spawn.calls == []


def test_one_job_at_a_time(shared):
    hold = threading.Event()
    spawn = FakeSpawn(["done\n"], hold=hold)
    app = make_app(shared, spawn)
    try:
        assert post(app, "/api/runs", {"pack": "toypack", "confirm": True}).status == 202
        assert get(app, "/api/jobs").json()["running"] is True
        assert post(app, "/api/runs", {"pack": "toypack", "confirm": True}).status == 409
        assert post(app, f"/api/runs/{RUN_ID}/replay").status == 409
    finally:
        hold.set()
        app.jobs.wait(5)
    assert len(spawn.calls) == 1
    assert post(app, f"/api/runs/{RUN_ID}/replay").status == 202


def test_a_replay_runs_the_real_command_line_as_a_child_free(runs):
    results = runs / RUN_ID / "results.json"
    first = json.loads(results.read_bytes())["graded_at"]
    app = App(packs_dir=FIXTURE_PACKS, runs_dir=runs, port=PORT, jobs=Jobs(popen(ROOT)))
    response = post(app, f"/api/runs/{RUN_ID}/replay")
    assert response.status == 202
    app.jobs.wait(120)
    job = get(app, f"/api/jobs/{response.json()['id']}").json()
    assert job["exit_code"] == 0, job["log"]
    assert job["run_id"] == RUN_ID and f"replay {RUN_ID}: 4 trial(s)" in "\n".join(job["log"])
    assert json.loads(results.read_bytes())["graded_at"] != first
    assert len(list((runs / RUN_ID / "history").iterdir())) == 1  # the earlier grading, kept


def test_a_replay_takes_no_field(app):
    assert post(app, f"/api/runs/{RUN_ID}/replay", {"argv": ["--domain"]}).status == 400


# ── Runs, matrix, transcript, report ─────────────────────────────────────────────────────────────


def test_runs_lists_each_run_folder_with_the_rail_facts(app):
    (run,) = get(app, "/api/runs").json()
    assert run["run_id"] == RUN_ID and run["pack"] == "toypack"
    assert (run["cases"], run["repeats"], run["status"], run["graded_by"]) == (
        2,
        2,
        "completed",
        "replay",
    )


def test_run_detail_holds_the_matrix_from_the_run_results(app, shared):
    run = get(app, f"/api/runs/{RUN_ID}").json()
    results = json.loads((shared / RUN_ID / "results.json").read_bytes())
    assert run["case_ids"] == ["toy-inject", "toy-open"]
    assert [h["id"] for h in run["hazards"]] == [v["hazard_id"] for v in results["hazards"]]
    by_cell = {(c["case_id"], c["hazard_id"]): c["status"] for c in results["case_hazards"]}
    for hazard in run["hazards"]:
        for case_id, cell in hazard["cells"].items():
            expected = by_cell.get((case_id, hazard["id"]))
            assert (cell or {}).get("status") == expected, (hazard["id"], case_id)
    h01 = next(h for h in run["hazards"] if h["id"] == "H-01")
    assert h01["cells"]["toy-open"]["status"] == "failed"
    assert h01["verdict"] == next(
        v["verdict"] for v in results["hazards"] if v["hazard_id"] == "H-01"
    )
    assert h01["checks"] and h01["controls"] and h01["criterion"].startswith("≤ ")
    assert run["facts"]["planned"] == 4 and run["facts"]["graded_by"] == "replay"
    assert "Re-graded" in [n["title"] for n in run["notices"]]
    assert run["findings"] and "H-01" in run["findings"][0]["text"]
    assert run["limits"] and all("**" not in line and "`" not in line for line in run["limits"])
    assert {t["case_id"] for t in run["trials"]} == {"toy-inject", "toy-open"}


def test_a_run_not_graded_yet_says_so(tmp_path):
    runs = tmp_path / "runs"
    make_run(runs, FIXTURE_PACKS, "toypack", TRIALS)
    run = get(make_app(runs), f"/api/runs/{RUN_ID}").json()
    assert [n["title"] for n in run["notices"]] == ["Not graded"]
    assert all(c is None or c["status"] for h in run["hazards"] for c in h["cells"].values())
    assert run["facts"]["graded_at"] is None and run["findings"] == []


def test_a_snapshot_that_changed_shows_an_integrity_notice(runs):
    pack_yaml = runs / RUN_ID / "snapshot" / "toypack" / "pack.yaml"
    pack_yaml.write_text(pack_yaml.read_text() + "\n# changed\n")
    run = get(make_app(runs), f"/api/runs/{RUN_ID}").json()
    assert "Integrity error" in [n["title"] for n in run["notices"]]


def test_the_transcript_comes_whole_and_as_data(app):
    response = get(app, f"/api/runs/{RUN_ID}/trials/toy-open/2")
    assert response.status == 200 and response.content_type.startswith("application/json")
    trial = response.json()["trial"]
    assert trial["case_id"] == "toy-open" and trial["n"] == 2
    agent = trial["transcript"]["turns"][1]
    assert HOSTILE in agent["text"]  # as written: the page puts it in a text node
    assert agent["tool_calls"][0]["name"] == "lookup_record"


def test_a_transcript_that_changed_is_a_409_integrity_error(runs):
    path = runs / RUN_ID / "toy-open" / "run_2.json"
    path.write_text(path.read_text().replace("Urgent", "Calm"))
    response = get(make_app(runs), f"/api/runs/{RUN_ID}/trials/toy-open/2")
    assert response.status == 409 and "integrity error" in response.json()["error"]


def test_the_report_is_served_as_plain_text(app, shared):
    response = get(app, f"/api/runs/{RUN_ID}/report")
    assert response.status == 200 and response.content_type == "text/plain; charset=utf-8"
    assert response.body == (shared / RUN_ID / "report.md").read_bytes()


# ── Attacks: another site ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "evil.example",
        "127.0.0.1",
        "localhost",
        "127.0.0.1:9999",
        f"127.0.0.1:{PORT}.evil.example",
        f"evil.example:{PORT}",
        f"[::1]:{PORT}",
        f"0.0.0.0:{PORT}",
    ],
)
@pytest.mark.parametrize("path", ["/", "/app.js", "/api/runs", f"/api/runs/{RUN_ID}"])
def test_a_foreign_host_is_refused_dns_rebinding(app, host, path):
    headers = {"x-auditkit-token": app.token} | ({} if host is None else {"host": host})
    response = app.handle(Request("GET", path, headers))
    assert response.status == 403 and "Host" in response.json()["error"]


def test_both_local_names_are_served(app):
    for host in (HOST, f"localhost:{PORT}"):
        assert app.handle(Request("GET", "/", {"host": host})).status == 200


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_a_cross_site_fetch_is_refused(app, site):
    assert get(app, "/api/runs", **{"sec-fetch-site": site}).status == 403
    assert get(app, "/", token=False, **{"sec-fetch-site": site}).status == 403
    response = post(
        app, "/api/runs", {"pack": "toypack", "confirm": True}, **{"sec-fetch-site": site}
    )
    assert response.status == 403


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", f"http://localhost:{PORT + 1}", f"https://{HOST}"]
)
def test_a_foreign_origin_cannot_start_a_run(shared, origin):
    spawn = FakeSpawn()
    app = make_app(shared, spawn)
    response = post(app, "/api/runs", {"pack": "toypack", "confirm": True}, origin=origin)
    assert response.status == 403 and spawn.calls == []
    assert get(app, "/api/runs", origin=origin).status == 403


def test_the_api_needs_the_session_token(app):
    assert get(app, "/api/runs", token=False).status == 403
    assert get(app, "/api/runs", token=False, **{"x-auditkit-token": "x" * 43}).status == 403
    assert get(app, "/api/runs", token=False, **{"x-auditkit-token": ""}).status == 403
    assert get(app, f"/api/runs?token={app.token}", token=False).status == 403  # never in a URL
    assert post(app, "/api/estimate", {"pack": "toypack"}, token=False).status == 403
    assert get(app, "/api/runs").status == 200


@pytest.mark.parametrize("kind", ["text/plain", FORM, "multipart/form-data", ""])
def test_a_post_that_a_form_could_send_is_refused(shared, kind):
    spawn = FakeSpawn()
    app = make_app(shared, spawn)
    response = post(
        app, "/api/runs", {"pack": "toypack", "confirm": True}, **{"content-type": kind}
    )
    assert response.status == 415 and spawn.calls == []


def test_a_json_content_type_with_a_charset_is_accepted(app):
    response = post(
        app,
        "/api/estimate",
        {"pack": "toypack"},
        **{"content-type": "application/json; charset=utf-8"},
    )
    assert response.status == 200


# ── Attacks: paths ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/api/runs/..",
        "/api/runs/%2e%2e",
        "/api/runs/..%2f..%2fetc%2fpasswd",
        "/api/runs/%2Fetc%2Fpasswd",
        "/api/runs//etc/passwd",
        f"/api/runs/{RUN_ID}/../../secrets",
        f"/api/runs/{RUN_ID}%00",
        f"/api/runs/{RUN_ID}\x00",
        "/api/runs/" + "1" * 600,
        f"/api/runs/{RUN_ID}_1{'1' * 20}",
        f"/api/runs/{RUN_ID}/trials/../../../etc/passwd/1",
        f"/api/runs/{RUN_ID}/trials/toy-open/../1",
        f"/api/runs/{RUN_ID}/trials/%2e%2e/1",
        f"/api/runs/{RUN_ID}/trials/toy-open/1e3",
        f"/api/runs/{RUN_ID}/trials/toy-open/0",
        f"/api/runs/{RUN_ID}/trials/toy-open/3",
        f"/api/runs/{RUN_ID}/trials/toy-elsewhere/1",
        f"/api/runs/{RUN_ID}/trials/TOY-OPEN/1",
        "/api/packs/..",
        "/api/packs/%2e%2e%2fsrc",
        "/api/packs/core",
        "/api/packs/nope",
        "/../etc/passwd",
        "/app.js/../../pyproject.toml",
        "/index.html",
        "/static/app.js",
        "/api",
        "/api/",
        "/api/jobs/1",
        "/api/jobs/x",
        "http://evil.example/api/runs",
    ],
)
def test_a_path_outside_the_routes_is_a_404(app, path):
    response = get(app, path)
    assert response.status == 404, path
    assert response.content_type.startswith("application/json")


def test_a_run_id_shaped_symlink_is_not_followed(runs, tmp_path):
    app = make_app(runs)
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(runs / RUN_ID, elsewhere / RUN_ID)
    (runs / "2026-02-02_000000").symlink_to(elsewhere / RUN_ID, target_is_directory=True)
    (runs / "2026-03-03_000000").symlink_to("/", target_is_directory=True)
    for run_id in ("2026-02-02_000000", "2026-03-03_000000"):
        assert get(app, f"/api/runs/{run_id}").status == 404
        assert get(app, f"/api/runs/{run_id}/report").status == 404
        assert post(app, f"/api/runs/{run_id}/replay").status == 404
    assert [r["run_id"] for r in get(app, "/api/runs").json()] == [RUN_ID]


def test_a_symlinked_report_is_not_read(runs, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("a secret line")
    report = runs / RUN_ID / "report.md"
    report.unlink()
    report.symlink_to(secret)
    response = get(make_app(runs), f"/api/runs/{RUN_ID}/report")
    assert response.status == 404 and b"secret" not in response.body


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/"),
        ("POST", "/app.js"),
        ("GET", "/api/estimate"),
        ("GET", f"/api/runs/{RUN_ID}/replay"),
        ("POST", f"/api/runs/{RUN_ID}"),
        ("POST", "/api/packs"),
        ("PUT", "/api/runs"),
        ("DELETE", f"/api/runs/{RUN_ID}"),
    ],
)
def test_a_wrong_method_is_a_405(app, method, path):
    headers = {"host": HOST, "x-auditkit-token": app.token, "content-type": "application/json"}
    assert app.handle(Request(method, path, headers, b"{}")).status == 405


# ── Attacks: bad input ────────────────────────────────────────────────────────────────────────────


def test_a_body_over_64_kb_is_a_413(app):
    raw = json.dumps({"pack": "toypack", "pad": "x" * MAX_BODY}).encode()
    assert post(app, "/api/estimate", raw=raw).status == 413


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[1, 2]",
        b'"toypack"',
        b'{"pack": "toypack", "repeats": NaN}',
        b'{"pack": "toypack", "budget_usd": Infinity}',
        b'{"pack": "toypack", "budget_usd": -Infinity}',
        b"\xff\xfe{}",
        b"[" * 10_000 + b"]" * 10_000,
    ],
)
def test_a_body_that_is_not_a_json_object_is_a_400(app, raw):
    assert post(app, "/api/estimate", raw=raw).status in (400, 413)


@pytest.mark.parametrize(
    "fields",
    [
        {"repeats": 0},
        {"repeats": -1},
        {"repeats": 101},
        {"repeats": 1.5},
        {"repeats": "2"},
        {"repeats": True},
        {"max_turns": 0},
        {"max_turns": 201},
        {"budget_usd": 0},
        {"budget_usd": -1},
        {"budget_usd": 1e9},
        {"budget_usd": "3"},
        {"budget_usd": True},
        {"cases": []},
        {"cases": "toy-open"},
        {"cases": [1]},
        {"cases": ["../toy-open"]},
        {"cases": ["--runs-dir"]},
        {"cases": ["toy-open,--runs-dir=/"]},
        {"cases": ["nope"]},
        {"cases": ["x"] * 1001},
        {"pack": "nope"},
        {"pack": "../domains"},
        {"pack": None},
        {"pack": ["toypack"]},
        {"variant": "--packs-dir"},
        {"variant": "../x"},
        {"target": "-x"},
        {"target": 3},
        {"argv": ["--domain"]},
    ],
)
def test_an_out_of_range_or_unknown_field_is_a_400(shared, fields):
    spawn = FakeSpawn()
    app = make_app(shared, spawn)
    body = {"pack": "toypack"} | fields
    assert post(app, "/api/estimate", body).status == 400, fields
    assert post(app, "/api/runs", body | {"confirm": True}).status == 400, fields
    assert spawn.calls == []


def test_an_error_never_leaks_a_trace(app, monkeypatch, capsys):
    def broken(*args):
        raise RuntimeError("a bug with a /private/path in it")

    monkeypatch.setattr("auditkit.ui.views.list_runs", broken)
    response = get(app, "/api/runs")
    assert response.status == 500 and "path" not in response.json()["error"]
    assert "RuntimeError" in capsys.readouterr().err  # the terminal gets the trace


# ── The HTTP adapter, over a socket pair (no connect) ─────────────────────────────────────────────


def exchange(app: App, raw: bytes) -> tuple[int, dict[str, str], bytes]:
    ours, theirs = socket.socketpair()
    handler = handler_for(app)

    def serve_one():
        try:
            handler(ours, ("127.0.0.1", 50000), None)
        finally:
            ours.close()

    thread = threading.Thread(target=serve_one)
    thread.start()
    theirs.sendall(raw)
    chunks = []
    while chunk := theirs.recv(65536):
        chunks.append(chunk)
    thread.join(10)
    theirs.close()
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status_line, *lines = head.decode("latin-1").split("\r\n")
    headers = dict(line.split(": ", 1) for line in lines)
    return int(status_line.split()[1]), headers, body


def test_the_adapter_serves_the_page_with_its_headers(app):
    status, headers, body = exchange(app, f"GET / HTTP/1.1\r\nHost: {HOST}\r\n\r\n".encode())
    assert status == 200 and headers["Content-Security-Policy"] == CSP
    assert headers["Content-Length"] == str(len(body)) and app.token.encode() in body
    assert not [h for h in headers if h.lower().startswith("access-control")]


def test_the_adapter_passes_a_post_body(app):
    data = b'{"pack": "toypack"}'
    raw = (
        f"POST /api/estimate HTTP/1.1\r\nHost: {HOST}\r\nOrigin: {ORIGIN}\r\n"
        f"X-Auditkit-Token: {app.token}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(data)}\r\n\r\n"
    ).encode() + data
    status, _, body = exchange(app, raw)
    assert status == 200 and json.loads(body)["estimate"]["trials"] == 4


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        (f"Host: {HOST}\r\nHost: evil.example\r\n", 400),
        ("", 403),
        (f"Host: {HOST}\r\nContent-Length: {MAX_BODY + 1}\r\n", 413),
        (f"Host: {HOST}\r\nContent-Length: -1\r\n", 400),
        (f"Host: {HOST}\r\nContent-Length: 1e3\r\n", 400),
        (f"Host: {HOST}\r\nTransfer-Encoding: chunked\r\n", 411),
    ],
)
def test_the_adapter_refuses_odd_framing(app, headers, status):
    raw = f"POST /api/estimate HTTP/1.1\r\n{headers}\r\n".encode()
    assert exchange(app, raw)[0] == status


def test_the_server_refuses_a_taken_port(shared, capsys):
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen()
    try:
        port = taken.getsockname()[1]
        assert serve(packs_dir=FIXTURE_PACKS, runs_dir=shared, port=port, open_browser=False) == 2
    finally:
        taken.close()
    assert "cannot listen on 127.0.0.1" in capsys.readouterr().err


def test_the_port_flag_is_checked(capsys):
    assert main(["--ui", "--port", "70000"]) == 2
    assert "is not a port" in capsys.readouterr().err


# ── The static files ──────────────────────────────────────────────────────────────────────────────

APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP_CSS = (STATIC / "app.css").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "call",
    [
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "createContextualFragment",
        "DOMParser",
        "srcdoc",
        "javascript:",
        'setTimeout("',
        "setTimeout('",
        "setInterval(",
        "localStorage",
    ],
)
def test_the_script_builds_the_dom_as_text_only(call):
    assert call not in APP_JS


def test_the_script_never_sets_an_inline_handler():
    assert not re.search(r"setAttribute\(\s*[\"']on", APP_JS)
    assert 'if (typeof value === "function") el.addEventListener' in APP_JS


@pytest.mark.parametrize("name", ["index.html", "app.css", "app.js"])
def test_the_static_files_load_nothing_remote(name):
    text = (STATIC / name).read_text(encoding="utf-8")
    urls = set(re.findall(r"(?:https?:)?//[\w.-]+\.[a-z]{2,}[^\s\"')]*", text))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls  # an XML namespace, never fetched
    assert "@import" not in text and "url(" not in text


def test_the_page_has_no_inline_script_style_or_handler():
    assert re.findall(r"<script\b[^>]*>", INDEX) == ['<script src="/app.js" defer>']
    assert "</script>" in INDEX and '<script src="/app.js" defer></script>' in INDEX
    assert "<style" not in INDEX and " style=" not in INDEX
    assert not re.search(r"\son\w+=", INDEX)
    assert '<meta name="auditkit-token" content="__AUDITKIT_TOKEN__">' in INDEX


def test_the_stylesheet_has_a_dark_theme_for_every_colour():
    light, dark = APP_CSS.split("@media (prefers-color-scheme: dark)", 1)
    colours = set(re.findall(r"(--[a-z]+): #", light))
    assert colours and colours == set(re.findall(r"(--[a-z]+): #", dark.split("}\n}", 1)[0]))


def test_the_readme_documents_the_local_page():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for flag in ("--ui", "--port", "--no-browser"):
        assert flag in readme, flag


# ── After review ──────────────────────────────────────────────────────────────────────────────────


def test_the_confirmation_focuses_its_question_not_the_paid_button():
    assert 'planBox.querySelector("#confirm-title")' in APP_JS and "title.focus()" in APP_JS
    assert '{ id: "confirm-title", tabindex: "-1" }' in APP_JS
    assert 'button.primary");\n      if (button) button.focus()' not in APP_JS


def test_the_child_uses_the_servers_environment_and_does_not_reread_env(tmp_path):
    (tmp_path / ".env").write_text(f"AGENT_MODEL={OTHER_MODEL}\n")  # written after the start
    env = {k: v for k, v in os.environ.items() if not k.endswith("_MODEL")}
    argv = [sys.executable, "-m", "auditkit.ui.child", "--lint", "toypack"]
    argv += ["--packs-dir", str(FIXTURE_PACKS)]
    done = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    agent = next(line for line in done.stdout.splitlines() if line.strip().startswith("agent"))
    assert "claude-haiku-5-5" in agent and "(default)" in agent


def test_each_job_start_and_end_is_said_in_the_terminal(shared):
    said = []
    jobs = Jobs(FakeSpawn(RUN_LINES), say=said.append)
    app = App(packs_dir=FIXTURE_PACKS, runs_dir=shared, port=PORT, jobs=jobs)
    post(app, "/api/runs", {"pack": "toypack", "cases": ["toy-open"], "confirm": True})
    app.jobs.wait(5)
    assert said[0].startswith("job 1: live started: eval --domain toypack --packs-dir ")
    assert "--cases toy-open" in said[0] and app.token not in said[0]
    assert said[1] == f"job 1: live ended, run {RUN_ID}_2: exit 0, report written"


def test_run_detail_holds_the_whole_manifest(app):
    manifest = get(app, f"/api/runs/{RUN_ID}").json()["manifest"]
    assert manifest["run_id"] == RUN_ID
    assert manifest["command"][:3] == ["eval", "--domain", "toypack"]
    for key in ("harness_version", "harness_git_sha", "pack_sha256", "max_turns", "estimate_usd"):
        assert key in manifest, key


def test_parameters_hold_the_rest_of_pack_yaml(app):
    p = get(app, "/api/packs/toypack").json()
    assert p["depends_on"] == ["toybase"]
    assert list(p["settings"]["side_channel"]) == ["asking_about"]
    assert set(p["settings"]["generic_checks"]) == {
        "core.conversation_completed",
        "core.output_schema_valid",
    }
    assert p["settings"]["data"] == {"catalog": "catalog.yaml"}
    assert p["settings"]["estimate"]["exchanges"] == 3


def test_the_live_progress_reads_the_command_lines_own_output(tmp_path, capsys):
    llm = ScriptedLLM(
        simulator=lambda r: user_says("Where is R-1?", {"asking_about": "R-1"}),
        agent=lambda r: (
            say('Urgent: R-1 is open. {"status": "open"}')
            if r["messages"][-1].blocks[0].type == "tool_result"
            else use("lookup_record", {"record": "R-1"})
        ),
    )
    argv = ["--domain", "toypack", "--packs-dir", str(FIXTURE_PACKS), "--runs-dir", str(tmp_path)]
    assert main([*argv, "--cases", "toy-open"], make_client=lambda: llm) == 0
    job = Job(1, "live", [], FIXED_NOW)
    for line in capsys.readouterr().out.splitlines():
        job.add(line)
    (run,) = tmp_path.iterdir()
    spent = json.loads((run / "execution.json").read_bytes())["spent_usd"]
    assert (job.run_id, job.planned, job.done) == (run.name, 2, 2)
    assert job.spent_usd == pytest.approx(spent, abs=0.0001 * 2)  # the log rounds each trial


def test_a_terminal_that_cannot_be_written_never_loses_a_job(shared):
    def closed(line):
        raise OSError("stdout closed")

    app = App(
        packs_dir=FIXTURE_PACKS, runs_dir=shared, port=PORT, jobs=Jobs(FakeSpawn(), say=closed)
    )
    assert post(app, f"/api/runs/{RUN_ID}/replay").status == 202
    app.jobs.wait(5)
    assert get(app, "/api/jobs").json()["exit_code"] == 0


def test_the_paid_button_turns_off_on_its_first_click():
    assert "event.currentTarget.disabled = true;" in APP_JS


def test_every_summary_keeps_its_disclosure_mark():
    (rule,) = re.findall(r"^details summary \{[^}]*\}", APP_CSS, re.MULTILINE)
    assert "display: list-item" in rule and "min-height: 44px" in rule


def test_a_content_length_that_is_not_ascii_digits_is_a_400(app):
    head = f"POST /api/estimate HTTP/1.1\r\nHost: {HOST}\r\n".encode()
    assert exchange(app, head + b"Content-Length: \xb2\r\n\r\n")[0] == 400  # "²", in latin-1
