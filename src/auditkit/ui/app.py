"""The local page's server, without a socket: a request in, a response out.

Every request passes the same guards before routing, so that no other site can read the page's
data or start a paid run from the owner's browser:

- the `Host` header must be this server's own (127.0.0.1 or localhost, on its port): a DNS
  rebinding attack names another host;
- a cross-site fetch (`Sec-Fetch-Site`) and a foreign `Origin` are refused; no CORS header is ever
  sent, so a foreign page cannot preflight one in;
- every API call carries the session token the page received in its HTML (`X-Auditkit-Token`),
  compared in constant time; it never appears in a URL or a log;
- a POST has a JSON body of at most 64 KB, and every field is checked.

Paths are a fixed set of routes; ids (runs, packs, cases) are checked by shape, then against the
run's or the packs folder's own lists, never joined to a path as given.
"""

from __future__ import annotations

import hmac
import json
import math
import re
import secrets
import sys
import threading
import traceback
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auditkit import cli
from auditkit.cost import PRICES_AS_OF
from auditkit.lint import lint_pack
from auditkit.ui import views
from auditkit.ui.jobs import Busy, Jobs

MAX_BODY = 64 * 1024
SESSION_HEADER = "x-auditkit-token"
SESSION_SLOT = "__AUDITKIT_TOKEN__"
STATIC = Path(__file__).parent / "static"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)
HEADERS = [
    ("Content-Security-Policy", CSP),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Cache-Control", "no-store"),
]
PATH = re.compile(r"/[A-Za-z0-9._/-]{0,511}")
ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,99}")  # schema.Id, bounded
FIELDS = {"pack", "variant", "target", "cases", "repeats", "max_turns", "budget_usd", "confirm"}
LIMITS = {"repeats": 100, "max_turns": 200}
MAX_BUDGET_USD = 1000.0
MAX_CASES = 1000


@dataclass(frozen=True)
class Request:
    method: str
    target: str  # the request line's target: path and query
    headers: Mapping[str, str]  # lower-case names
    body: bytes = b""


@dataclass
class Response:
    status: int
    body: bytes
    content_type: str
    headers: list[tuple[str, str]] = field(default_factory=lambda: list(HEADERS))

    def json(self) -> Any:
        return json.loads(self.body)


class HTTPError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status, self.message = status, message


@dataclass
class Selection:
    pack: str
    variant: str | None
    target: str | None
    cases: list[str] | None
    repeats: int | None
    max_turns: int | None
    budget_usd: float | None


def _not_a_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a number")


class App:
    """`port` is set once the server is bound (port 0 picks a free one)."""

    def __init__(
        self,
        *,
        packs_dir: Path,
        runs_dir: Path,
        port: int = 0,
        token: str | None = None,
        jobs: Jobs | None = None,
        python: str = sys.executable,
    ) -> None:
        self.packs_dir, self.runs_dir, self.port = Path(packs_dir), Path(runs_dir), port
        self.token = token or secrets.token_urlsafe(32)
        self.jobs = jobs or Jobs()
        self.python = python
        # loading a pack's code swaps the `packs.*` modules: one request at a time
        self._lock = threading.Lock()

    # ── Guards ────────────────────────────────────────────────────────────────────────────────────

    @property
    def hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @property
    def origins(self) -> set[str]:
        return {f"http://{host}" for host in self.hosts}

    def handle(self, request: Request) -> Response:
        try:
            with self._lock:
                return self._handle(request)
        except HTTPError as e:
            return self.error(e.status, e.message)
        except Exception:  # a harness bug: the terminal gets the trace, the page a short line
            traceback.print_exc()
            return self.error(500, "internal error: see the terminal that runs eval --ui")

    def error(self, status: int, message: str) -> Response:
        return self._json({"error": message}, status)

    def _guard(self, request: Request) -> None:
        if request.headers.get("host") not in self.hosts:
            raise HTTPError(403, "unknown Host: this page answers as 127.0.0.1 or localhost only")
        site = request.headers.get("sec-fetch-site")
        if site is not None and site not in ("same-origin", "none"):
            raise HTTPError(403, "a request from another site is refused")
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.origins:
            raise HTTPError(403, "a request from another origin is refused")

    def _token(self, request: Request) -> None:
        given = request.headers.get(SESSION_HEADER, "").encode("utf-8", "replace")
        if not hmac.compare_digest(given, self.token.encode()):
            raise HTTPError(403, "missing or wrong session token: reload the page")

    def _body(self, request: Request) -> dict[str, Any]:
        kind = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if kind != "application/json":
            raise HTTPError(415, "a POST body is JSON (Content-Type: application/json)")
        if len(request.body) > MAX_BODY:
            raise HTTPError(413, f"the body is over {MAX_BODY // 1024} KB")
        try:
            value = json.loads(request.body.decode("utf-8") or "{}", parse_constant=_not_a_constant)
        except (UnicodeDecodeError, ValueError, RecursionError) as e:
            raise HTTPError(400, "the body is not valid JSON") from e
        if not isinstance(value, dict):
            raise HTTPError(400, "the body is a JSON object")
        return value

    # ── Routing ───────────────────────────────────────────────────────────────────────────────────

    def _handle(self, request: Request) -> Response:
        self._guard(request)
        path = request.target.split("?", 1)[0]
        parts = path.split("/")[1:]
        if not PATH.fullmatch(path) or (path != "/" and any(p in ("", ".", "..") for p in parts)):
            raise HTTPError(404, "no such page")
        if path in FILES:
            self._method(request, "GET")
            return self._static(path)
        if parts[0] != "api":
            raise HTTPError(404, "no such page")
        self._token(request)
        return self._api(request, parts[1:])

    @staticmethod
    def _method(request: Request, allowed: str) -> None:
        if request.method != allowed:
            raise HTTPError(405, f"use {allowed} here")

    def _api(self, request: Request, parts: list[str]) -> Response:
        match parts:
            case ["packs"]:
                self._method(request, "GET")
                return self._json(views.list_packs(self.packs_dir))
            case ["packs", name]:
                self._method(request, "GET")
                return self._json(self._view(views.pack_parameters, self.packs_dir, name))
            case ["estimate"]:
                self._method(request, "POST")
                return self._json(self._plan(self._selection(self._body(request))))
            case ["runs"] if request.method == "POST":
                return self._launch(self._body(request))
            case ["runs"]:
                self._method(request, "GET")
                return self._json(views.list_runs(self.runs_dir))
            case ["runs", run_id]:
                self._method(request, "GET")
                return self._json(self._view(views.run_detail, self.runs_dir, run_id))
            case ["runs", run_id, "replay"]:
                self._method(request, "POST")
                return self._replay(run_id, self._body(request))
            case ["runs", run_id, "report"]:
                self._method(request, "GET")
                text = self._view(views.report_text, self.runs_dir, run_id)
                return Response(200, text.encode(), "text/plain; charset=utf-8")
            case ["runs", run_id, "trials", case_id, n] if ID.fullmatch(case_id):
                self._method(request, "GET")
                if not (n.isdigit() and len(n) <= 4):
                    raise HTTPError(404, "no such trial")
                found = self._view(views.transcript, self.runs_dir, run_id, case_id, int(n))
                return self._json(found)
            case ["jobs"]:
                self._method(request, "GET")
                return self._json(self.jobs.latest())
            case ["jobs", job_id] if job_id.isdigit() and len(job_id) <= 9:
                self._method(request, "GET")
                job = self.jobs.get(int(job_id))
                if job is None:
                    raise HTTPError(404, "no such job")
                return self._json(job)
        raise HTTPError(404, "no such page")

    @staticmethod
    def _view(fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except views.NotFound as e:
            raise HTTPError(404, str(e)) from e
        except views.Unreadable as e:
            raise HTTPError(409, str(e)) from e

    # ── Responses ─────────────────────────────────────────────────────────────────────────────────

    def _json(self, value: Any, status: int = 200) -> Response:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
        return Response(status, body, "application/json; charset=utf-8")

    def _static(self, path: str) -> Response:
        name, kind = FILES[path]
        data = (STATIC / name).read_bytes()
        if name == "index.html":
            data = data.replace(SESSION_SLOT.encode(), self.token.encode())
        return Response(200, data, kind)

    # ── Estimate, launch, replay ──────────────────────────────────────────────────────────────────

    def _selection(self, body: dict[str, Any]) -> Selection:
        if unknown := sorted(set(body) - FIELDS):
            raise HTTPError(400, f"unknown field(s): {', '.join(u[:40] for u in unknown[:5])}")
        pack = body.get("pack")
        if not isinstance(pack, str) or pack not in views.pack_names(self.packs_dir):
            raise HTTPError(400, "pack: not a pack of the packs folder")
        cases = body.get("cases")
        if cases is not None:
            if not (isinstance(cases, list) and 0 < len(cases) <= MAX_CASES):
                raise HTTPError(400, f"cases: a list of 1 to {MAX_CASES} case ids, or null")
            if not all(isinstance(c, str) and ID.fullmatch(c) for c in cases):
                raise HTTPError(400, "cases: every item is a case id")
            cases = list(dict.fromkeys(cases))
        return Selection(
            pack=pack,
            variant=self._id(body, "variant"),
            target=self._id(body, "target"),
            cases=cases,
            repeats=self._count(body, "repeats"),
            max_turns=self._count(body, "max_turns"),
            budget_usd=self._usd(body),
        )

    @staticmethod
    def _id(body: dict[str, Any], key: str) -> str | None:
        value = body.get(key)
        if value is None:
            return None
        if not (isinstance(value, str) and ID.fullmatch(value)):
            raise HTTPError(400, f"{key}: not a name")
        return value

    @staticmethod
    def _count(body: dict[str, Any], key: str) -> int | None:
        value = body.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= LIMITS[key]:
            raise HTTPError(400, f"{key}: a whole number from 1 to {LIMITS[key]}, or null")
        return value

    @staticmethod
    def _usd(body: dict[str, Any]) -> float | None:
        value = body.get("budget_usd")
        if value is None:
            return None
        ok = isinstance(value, int | float) and not isinstance(value, bool)
        if not (ok and math.isfinite(value) and 0 < value <= MAX_BUDGET_USD):
            raise HTTPError(400, f"budget_usd: an amount above 0 and up to {MAX_BUDGET_USD:g}")
        return float(value)

    def _plan(self, sel: Selection) -> dict[str, Any]:
        """The lint, then (no error) the selection and the estimate, as `eval --lint` prints
        them. Free: no API call."""
        issues = lint_pack(
            self.packs_dir,
            sel.pack,
            variant=sel.variant or "default",
            target=sel.target,
            repeats=sel.repeats,
            max_turns=sel.max_turns,
        )
        out: dict[str, Any] = {
            "lint": [i.model_dump(mode="json") for i in issues],
            "lint_errors": sum(i.level == "error" for i in issues),
            "estimate": None,
        }
        if out["lint_errors"]:
            return out
        try:
            chosen = cli.select(
                self.packs_dir,
                sel.pack,
                variant=sel.variant,
                target=sel.target,
                cases=",".join(sel.cases) if sel.cases else None,
                repeats=sel.repeats,
                max_turns=sel.max_turns,
            )
            budget = sel.budget_usd if sel.budget_usd is not None else cli.default_budget()
        except (cli.Refused, ValueError) as e:
            raise HTTPError(400, str(e)) from e
        out["estimate"] = {
            "pack": sel.pack,
            "variant": sel.variant or "default",
            "target": chosen.target.name,
            "case_ids": chosen.case_ids,
            "repeats": chosen.repeats,
            "max_turns": chosen.max_turns,
            "trials": len(chosen.case_ids) * chosen.repeats,
            "worst_turns": chosen.worst_turns,
            "roles": [
                {
                    "role": r.role,
                    "model": r.model,
                    "source": chosen.source(r.role),
                    "typical_usd": r.typical_usd,
                    "worst_usd": r.worst_usd,
                }
                for r in chosen.roles
            ],
            "typical_usd": chosen.typical_usd,
            "worst_usd": chosen.worst_usd,
            "budget_usd": budget,
            "refused": chosen.typical_usd > budget,
            "prices_as_of": PRICES_AS_OF,
        }
        return out

    def _launch(self, body: dict[str, Any]) -> Response:
        """A live run: it costs money, so it needs `confirm: true`, a clean lint and an estimate
        under the cap; then the command line runs it, and checks all of it again."""
        sel = self._selection(body)
        if body.get("confirm") is not True:
            raise HTTPError(400, "a live run costs money: it needs confirm: true")
        if self.jobs.running():
            raise HTTPError(409, "a job is already running: wait for it to end")
        plan = self._plan(sel)
        estimate = plan["estimate"]
        if estimate is None:
            raise HTTPError(400, f"lint: {plan['lint_errors']} error(s): nothing run")
        if estimate["refused"]:
            raise HTTPError(400, "refused: the typical estimate is above the cap")
        argv = [
            self.python,
            "-m",
            "auditkit.ui.child",
            "--domain",
            sel.pack,
            "--packs-dir",
            str(self.packs_dir.resolve()),
            "--runs-dir",
            str(self.runs_dir.resolve()),
            "--budget-usd",
            repr(estimate["budget_usd"]),
        ]
        for flag, value in (
            ("--variant", sel.variant),
            ("--target", sel.target),
            ("--cases", ",".join(sel.cases) if sel.cases else None),
            ("--repeats", sel.repeats),
            ("--max-turns", sel.max_turns),
        ):
            if value is not None:
                argv += [flag, str(value)]
        return self._start("live", argv)

    def _replay(self, run_id: str, body: dict[str, Any]) -> Response:
        """Re-grade a run from its snapshot: free, no API call."""
        if body:
            raise HTTPError(400, "a replay takes no field")
        path = self._view(views.run_dir, self.runs_dir, run_id)
        argv = [self.python, "-m", "auditkit.ui.child", "--replay", str(path.resolve())]
        return self._start("replay", argv, run_id=run_id)

    def _start(self, kind: str, argv: list[str], *, run_id: str | None = None) -> Response:
        try:
            return self._json(self.jobs.start(kind, argv, run_id=run_id), 202)
        except Busy as e:
            raise HTTPError(409, str(e)) from e
