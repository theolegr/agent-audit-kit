"""The HTTP adapter of the local page: a stdlib server bound to 127.0.0.1, passing each request to
`App.handle`. No access log: the terminal shows the address, each job's start and end, errors."""

from __future__ import annotations

import re
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from auditkit.ui.app import MAX_BODY, App, Request, Response
from auditkit.ui.jobs import Jobs

SINGLE = ("Host", "Origin", "Sec-Fetch-Site", "X-Auditkit-Token", "Content-Type", "Content-Length")
SHUTDOWN_WAIT_S = 30  # a running child gets Ctrl+C too: time to write its folder and exit
LENGTH = re.compile(r"[0-9]{1,12}")  # ASCII digits only: str.isdigit() takes "²"


def handler_for(app: App) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "auditkit"
        sys_version = ""

        def do_GET(self) -> None:
            self._serve()

        def do_POST(self) -> None:
            self._serve()

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _serve(self) -> None:
            if any(len(self.headers.get_all(name) or []) > 1 for name in SINGLE):
                return self._send(app.error(400, "a header is given twice"))
            if self.headers.get("Transfer-Encoding"):
                return self._send(app.error(411, "send a Content-Length, not a chunked body"))
            length = self.headers.get("Content-Length", "0").strip()
            if not LENGTH.fullmatch(length):
                return self._send(app.error(400, "a bad Content-Length"))
            if int(length) > MAX_BODY:
                self.close_connection = True  # the body is never read
                return self._send(app.error(413, f"the body is over {MAX_BODY // 1024} KB"))
            body = self.rfile.read(int(length))
            headers = {k.lower(): v for k, v in self.headers.items()}
            self._send(app.handle(Request(self.command, self.path, headers, body)))

        def _send(self, response: Response) -> None:
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(response.body)

    return Handler


def serve(*, packs_dir: Path, runs_dir: Path, port: int, open_browser: bool) -> int:
    """Serve until Ctrl+C; 2 when the port is taken."""
    jobs = Jobs(say=lambda line: print(line, flush=True))
    app = App(packs_dir=packs_dir, runs_dir=runs_dir, jobs=jobs)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    except OSError as e:
        print(
            f"eval: cannot listen on 127.0.0.1:{port}: {e.strerror or e} (another eval --ui? "
            "try --port 0)",
            file=sys.stderr,
        )
        return 2
    app.port = httpd.server_address[1]
    url = f"http://127.0.0.1:{app.port}/"
    print(f"local page: {url}  (127.0.0.1 only; Ctrl+C stops it)", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if app.jobs.running():
            print("waiting for the running job to stop…", flush=True)
            app.jobs.wait(SHUTDOWN_WAIT_S)
        job = app.jobs.latest()
        if job is not None and job["exit_code"] not in (None, 0):
            print("\n".join(job["log"][-5:]))
    print("local page stopped")
    return 0
