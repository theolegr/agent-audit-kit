"""One job at a time: `python -m auditkit.ui.child …` as a child process (an argument list, no
shell), its output kept as lines, its progress read from them, its exit code.

The child is the command line itself, so a live run from the page passes the same lint, estimate
and cap refusal as one typed in a terminal, and writes the same run folder. Each job's start and
end is also said in the server's terminal: a run nobody started from the page shows there.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import subprocess
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from auditkit.tools import now

MAX_LINES = 4000  # the log keeps its first and last lines beyond this
RUN_LINE = re.compile(r"^(?:run|resume) (\d{4}-\d{2}-\d{2}_\d{6}(?:_\d+)?): (\d+) trial\(s\)")
TRIAL_LINE = re.compile(r"^  \S+ run \d+ \(attempt \d+\): \w+, \w+, \$([0-9.]+)$")
EXIT_CODES = {  # the command line's own (eval --help)
    0: "report written",
    2: "usage, configuration or lint error: nothing sent",
    3: "budget: the estimate is above the cap, or the cap was reached (partial report written)",
    4: "harness or provider error (partial report written)",
    5: "integrity error: the run folder does not match its own record",
    130: "interrupted: finish it with eval --resume",
}


class Busy(Exception):
    """A job is already running."""


class Process(Protocol):
    stdout: Iterable[str] | None

    def wait(self) -> int: ...


Spawn = Callable[[Sequence[str]], Process]


def popen(cwd: Path | None = None) -> Spawn:
    """The real spawner: unbuffered UTF-8 output, stderr merged into stdout, no stdin."""

    def spawn(argv: Sequence[str]) -> Process:
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        return subprocess.Popen(
            list(argv),
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )

    return spawn


@dataclass
class Job:
    id: int
    kind: str  # "live" or "replay"
    argv: list[str]
    started_at: datetime
    run_id: str | None = None
    lines: list[str] = field(default_factory=list)
    dropped: int = 0  # lines left out of the middle of a long log
    planned: int | None = None
    done: int = 0
    spent_usd: float = 0.0
    exit_code: int | None = None
    finished_at: datetime | None = None

    def add(self, line: str) -> None:
        if match := RUN_LINE.match(line):
            self.run_id = self.run_id or match[1]
            self.planned = int(match[2])
        elif match := TRIAL_LINE.match(line):
            self.done += 1
            self.spent_usd += float(match[1])
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            del self.lines[MAX_LINES // 2]
            self.dropped += 1

    def view(self) -> dict[str, Any]:
        lines = list(self.lines)
        if self.dropped:
            lines.insert(MAX_LINES // 2, f"[{self.dropped} line(s) left out]")
        return {
            "id": self.id,
            "kind": self.kind,
            "run_id": self.run_id,
            "running": self.exit_code is None,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "planned": self.planned,
            "done": self.done,
            "spent_usd": round(self.spent_usd, 6),
            "exit_code": self.exit_code,
            "exit_meaning": None if self.exit_code is None else EXIT_CODES.get(self.exit_code),
            "log": lines,
        }


class Jobs:
    """`say`: where each job's start and end is told (the server's terminal); tests pass none."""

    def __init__(
        self,
        spawn: Spawn | None = None,
        clock: Callable[[], datetime] = now,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._spawn = spawn or popen()
        self._clock = clock
        self._say = say or (lambda line: None)
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._threads: dict[int, threading.Thread] = {}

    def start(self, kind: str, argv: Sequence[str], *, run_id: str | None = None) -> dict[str, Any]:
        """Start a job and return its view; `Busy` while another runs."""
        with self._lock:
            if any(j.exit_code is None for j in self._jobs.values()):
                raise Busy("a job is already running: wait for it to end")
            job = Job(len(self._jobs) + 1, kind, list(argv), self._clock(), run_id=run_id)
            process = self._spawn(job.argv)
            self._jobs[job.id] = job  # before anything else can fail: a child never runs untracked
            thread = threading.Thread(target=self._follow, args=(job, process), daemon=True)
            self._threads[job.id] = thread
            thread.start()
            self._tell(f"job {job.id}: {kind} started: eval {shlex.join(job.argv[3:])}")
            return job.view()

    def _tell(self, line: str) -> None:
        """A line for the terminal; one that cannot be written (a closed stdout) is dropped: the
        page and the run folder hold the record."""
        with contextlib.suppress(OSError, ValueError):
            self._say(line)

    def _follow(self, job: Job, process: Process) -> None:
        code = -1
        try:
            for raw in process.stdout or ():
                with self._lock:
                    job.add(raw.rstrip("\r\n"))
        finally:
            try:
                code = process.wait()
            finally:
                with self._lock:
                    job.exit_code = code
                    job.finished_at = self._clock()
                run = f", run {job.run_id}" if job.run_id else ""
                self._tell(
                    f"job {job.id}: {job.kind} ended{run}: exit {code}, {EXIT_CODES.get(code, '')}"
                )

    def get(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return job.view() if job else None

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs[max(self._jobs)].view() if self._jobs else None

    def running(self) -> bool:
        with self._lock:
            return any(j.exit_code is None for j in self._jobs.values())

    def wait(self, timeout: float | None = None) -> None:
        """Wait for the jobs' followers to end (tests; the server's shutdown)."""
        for thread in list(self._threads.values()):
            thread.join(timeout)
