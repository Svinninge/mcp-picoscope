# File version: v0.02
"""Local scope display, opened in an Edge app window when the server is used.

The MCP session sees numbers; a person wants to see the waveform. This serves
one page on 127.0.0.1 that polls the live session state, and opens Edge at it
the first time a tool is called.

Deliberately separate from the MCP surface: the page reads the same
ScopeSession the tools write, and can never drive the hardware. A display that
could also push buttons would need the session lock and a permission story;
this needs neither.

One window, not one per call. The page polls /state continuously, so a recent
poll IS the proof that a window is already watching: no launch then. When the
window is closed the polls stop, and the next tool call brings it back. A
process-lifetime flag could not do that — it would leave you with no display
for the rest of the session the moment you closed the window once.

Set PICOSCOPE_UI=0 to disable it entirely — the test suite does, and so should
anything running unattended.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import version_line
from .analysis import downsample_minmax, measure

log = logging.getLogger(__name__)

DEFAULT_PORT = 8071
PORT_ATTEMPTS = 10
ENABLED_ENV = "PICOSCOPE_UI"
PORT_ENV = "PICOSCOPE_UI_PORT"

# The page is a picture, not a context window: it can afford far more points
# than an MCP reply, but still not the whole record over HTTP every 400 ms.
UI_CURVE_POINTS = 1600
ACTIVITY_MAX = 60

# A poll this recent means a window is open and watching. The page polls every
# 400 ms, so this tolerates a dozen missed polls — a page that is merely busy
# or throttled in a background tab must not be mistaken for a closed window.
VIEWER_TIMEOUT_S = 6.0
# Edge needs a moment to start and load the page before its first poll. Without
# a cooldown, the tool calls in that gap would each launch another window.
LAUNCH_COOLDOWN_S = 15.0

PAGE = Path(__file__).with_name("ui.html")

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_activity: deque[dict] = deque(maxlen=ACTIVITY_MAX)
_state_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_session: Any = None
_url: str | None = None
_last_poll = 0.0
_last_launch = 0.0


def enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "1") not in ("0", "false", "no")


def record(tool: str, status: str, detail: str = "") -> None:
    """Log one tool call for the activity list. Never raises."""
    _activity.appendleft(
        {
            "time": datetime.now().strftime("%H:%M:%S"),
            "tool": tool,
            "status": status,
            "detail": detail[:200],
        }
    )


def viewer_present() -> bool:
    """True when a page polled us recently — i.e. a window is open."""
    return (time.monotonic() - _last_poll) < VIEWER_TIMEOUT_S


def ensure_started(session: Any) -> str | None:
    """Start the UI server, and open a window only if none is watching.

    Failing to show a window must never fail a measurement, so every error here
    is logged and swallowed.
    """
    global _last_launch
    if not enabled():
        return None
    try:
        url = _ensure_server(session)
        if url is None or viewer_present():
            return url
        if time.monotonic() - _last_launch < LAUNCH_COOLDOWN_S:
            return url  # one is already on its way up
        _last_launch = time.monotonic()
        _open_edge(url)
        return url
    except Exception:  # noqa: BLE001 - the UI is never worth a failed tool call
        log.exception("could not start the UI")
        return None


def url() -> str | None:
    return _url


def reopen(target: str, force: bool = False) -> bool:
    """Bring up a window on demand. Returns True if one was launched.

    Without `force` this still respects a window that is already watching —
    "show me the display" should not mean "give me a second copy of it".
    """
    global _last_launch
    if viewer_present() and not force:
        return False
    _last_launch = time.monotonic()
    _open_edge(target)
    return True


def stop() -> None:
    global _server, _url, _last_poll, _last_launch
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    _url = None
    _last_poll = 0.0
    _last_launch = 0.0


def _ensure_server(session: Any) -> str | None:
    global _server, _session, _url
    with _state_lock:
        _session = session
        if _server is not None:
            return _url
        port = int(os.environ.get(PORT_ENV, DEFAULT_PORT))
        for candidate in range(port, port + PORT_ATTEMPTS):
            try:
                _server = _Server(("127.0.0.1", candidate), _Handler)
            except OSError:
                continue  # port taken — another session of ours owns it
            threading.Thread(
                target=_server.serve_forever, name="picoscope-ui", daemon=True
            ).start()
            _url = f"http://127.0.0.1:{candidate}/"
            log.info("UI on %s", _url)
            return _url
        log.warning("no free port in %d..%d for the UI", port, port + PORT_ATTEMPTS)
        return None


def _edge_path() -> str | None:
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("msedge")


def _open_edge(target: str) -> None:
    """Open the page in an Edge app window — no tabs, no address bar.

    A separate user-data-dir would isolate it from the user's own browsing but
    costs a cold profile every start; --app on the normal profile opens fast and
    keeps its own window.
    """
    edge = _edge_path()
    if edge is None:
        log.warning("Edge not found; open %s yourself", target)
        return
    subprocess.Popen(
        # Modest default size: this machine runs Windows at 300 % scaling, where
        # a "normal" window fills the screen. The page carries its own zoom and
        # remembers it, so the user only sets this once.
        [edge, f"--app={target}", "--window-size=1000,680"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Detach: the window must outlive a single tool call, and must not hold
        # the stdio pipes the MCP protocol runs on.
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    log.info("opened Edge at %s", target)


def _ui_state() -> dict:
    """Everything the page draws, in one request."""
    session = _session
    if session is None:
        return {
            "open": False,
            "activity": list(_activity),
            "version": version_line(),
        }

    with session.lock:
        state = session.state()
        latest = next(reversed(session.captures.values()), None)
        if latest is not None:
            state["latest"] = {
                "capture_id": latest.capture_id,
                "measurements": measure(latest),
                "curve": downsample_minmax(
                    latest.volts, latest.dt_s, UI_CURVE_POINTS
                ),
            }
    state["activity"] = list(_activity)
    state["version"] = version_line()
    state["simulated"] = state.get("backend") == "mock"
    return state


class _Server(ThreadingHTTPServer):
    """HTTP server that refuses to share its port.

    ThreadingHTTPServer sets allow_reuse_address, which on Windows means
    SO_REUSEADDR — and there that does not merely permit rebinding a socket in
    TIME_WAIT, it lets a second process HIJACK a live listener. Two sessions
    then both "own" 8071, connections land on whichever socket wins the race,
    and the window you opened shows another session's scope. Refusing reuse
    makes the bind fail honestly so the port scan moves to the next one.
    """

    allow_reuse_address = False
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        route = self.path.split("?")[0]
        if route == "/":
            self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
        elif route == "/state":
            global _last_poll
            _last_poll = time.monotonic()  # proof that a window is watching
            body = json.dumps(_ui_state()).encode("utf-8")
            self._send(body, "application/json", cache=False)
        else:
            self.send_error(404)

    def _send(self, body: bytes, content_type: str, cache: bool = True) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if not cache:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        # stderr belongs to the MCP session log; one line per poll would bury it.
        log.debug("ui %s", fmt % args)
