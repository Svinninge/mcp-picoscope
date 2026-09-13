# File version: v0.05
"""Local scope display, opened in an Edge app window when the server is used.

The MCP session sees numbers; a person wants to see the waveform. This serves
one page on 127.0.0.1 that polls the live session state, and opens Edge at it
when no window is already showing one.

Deliberately separate from the MCP surface: the page reads the same
ScopeSession the tools write, and can never drive the hardware. A display that
could also push buttons would need the session lock and a permission story;
this needs neither.

ONE WINDOW PER MACHINE, and it is a machine-wide rule, not a per-process one.
There is a single PS2104 on this desk, so a second window is always a lie about
how many instruments exist. Two mechanisms, in order:

  * within a process: the page polls continuously, so a recent poll proves a
    window is watching;
  * across processes: that same proof is written to a small file in the temp
    directory, which every server process reads before launching anything.

The file also remembers the view — zoom, position and size — because the port
can differ between runs and localStorage is per-origin, so the browser's own
memory of the zoom is lost exactly when a second process starts.

Set PICOSCOPE_UI=0 to disable it entirely — the test suite does, and so should
anything running unattended.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import version_line
from .analysis import downsample_minmax, measure
from .scope import ScopeError

log = logging.getLogger(__name__)

DEFAULT_PORT = 8071
PORT_ATTEMPTS = 10
ENABLED_ENV = "PICOSCOPE_UI"
PORT_ENV = "PICOSCOPE_UI_PORT"
# Serve the page but never launch a browser. For tests, and for anyone who
# would rather keep the page open in a tab of their own.
BROWSER_ENV = "PICOSCOPE_UI_BROWSER"

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
# How often the window claim is rewritten. Every poll would mean two disk
# writes a second for a file nobody reads that often.
CLAIM_WRITE_INTERVAL_S = 1.0

# Shared between every server process for this user. Per-user rather than
# system-wide: the scope belongs to whoever is logged in.
PREFS_FILE = Path(tempfile.gettempdir()) / "mcp-picoscope-ui.json"

# Sanity bounds for remembered geometry. A window left at -30000 because a
# screen was unplugged must not be restored there, where it cannot be found.
MIN_SIZE, MAX_SIZE = 240, 10000
MAX_COORD = 20000
DEFAULT_SIZE = (1000, 680)

PAGE = Path(__file__).with_name("ui.html")

# How long a state read may wait for the session lock. The lock serialises the
# driver, so a capture holds it for as long as the capture takes. The page
# polls every 400 ms and would rather draw the last frame again than freeze;
# it gets the previous snapshot, marked busy.
STATE_LOCK_WAIT_S = 0.25

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

# The display runs in its own Edge profile. That costs a cold start, and buys
# the one thing worth paying for: every process using this directory is ours,
# so a leftover window can be closed deterministically without ever touching
# the user's own browsing. Without it, a window whose server has exited sits
# there forever showing a scope that no longer exists — and they accumulate,
# one per session, each claiming to be an instrument.
EDGE_PROFILE_DIR = Path(tempfile.gettempdir()) / "picoscope-edge-profile"
SWEEP_TIMEOUT_S = 10

_activity: deque[dict] = deque(maxlen=ACTIVITY_MAX)
_last_state: dict | None = None
_state_lock = threading.Lock()
_prefs_lock = threading.Lock()
_server: ThreadingHTTPServer | None = None
_session: Any = None
_url: str | None = None
_last_poll = 0.0
_last_launch = 0.0
_last_claim_write = 0.0
# What we asked Edge for, until the page reports what it actually got. The
# differences are the window frame, and subtracting them next time is what
# stops a window creeping down the screen and growing a pixel every session.
_pending_launch: tuple[int, int] | None = None
_pending_size: tuple[int, int] | None = None
# Set when the owner is shutting down. Every tool call passes ensure_started(),
# so without this the teardown's own close_device() saw no window watching and
# opened a new one — measured, a window flashed up a second after the user had
# closed the app.
_frozen = False


def _flag(name: str) -> bool:
    return os.environ.get(name, "1") not in ("0", "false", "no")


def enabled() -> bool:
    return _flag(ENABLED_ENV)


def browser_enabled() -> bool:
    return _flag(BROWSER_ENV)


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


# -- shared preferences and the window claim ------------------------------


def read_prefs() -> dict:
    """The shared file, or an empty dict. Never raises, never blocks a tool."""
    try:
        with PREFS_FILE.open(encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_prefs(patch: dict) -> None:
    """Merge `patch` into the shared file, atomically.

    Last writer wins. Two processes racing here can only disagree about which
    of them owns a window, and the next poll a second later settles it — cheap
    enough not to warrant a real lock.
    """
    with _prefs_lock:
        data = read_prefs()
        data.update(patch)
        tmp = PREFS_FILE.with_suffix(".tmp")
        try:
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, PREFS_FILE)
        except OSError as exc:
            log.debug("could not write %s: %s", PREFS_FILE, exc)


def window_claim() -> dict | None:
    """The live window claim, if any process still holds one."""
    claim = read_prefs().get("window")
    if not isinstance(claim, dict):
        return None
    try:
        fresh = time.time() - float(claim.get("ts", 0)) <= VIEWER_TIMEOUT_S
    except (TypeError, ValueError):
        return None
    return claim if fresh else None  # stale: the owner stopped polling


def _claim_window() -> None:
    """Say that a window is watching us — the proof other processes read."""
    global _last_claim_write
    now = time.monotonic()
    if now - _last_claim_write < CLAIM_WRITE_INTERVAL_S:
        return
    _last_claim_write = now
    write_prefs({"window": {"url": _url, "pid": os.getpid(), "ts": time.time()}})


def viewer_present() -> bool:
    """True when a page polled *this* server recently."""
    return (time.monotonic() - _last_poll) < VIEWER_TIMEOUT_S


def view_prefs() -> dict:
    """Remembered zoom, position and size."""
    view = read_prefs().get("view")
    return view if isinstance(view, dict) else {}


def save_view(zoom=None, x=None, y=None, w=None, h=None) -> dict:
    """Store what the page reports about itself, learning the frame offset.

    The first report after a launch says how far Edge put the content from the
    position we asked for — the title bar and borders. Remembering that
    difference is what separates reopening where the user left the window from
    creeping one title bar further down the screen each time.
    """
    global _pending_launch, _pending_size
    view = view_prefs()
    if zoom is not None and 0.2 <= zoom <= 4:
        view["zoom"] = round(float(zoom), 2)
    if x is not None and y is not None and abs(x) < MAX_COORD and abs(y) < MAX_COORD:
        view["x"], view["y"] = int(x), int(y)
        if _pending_launch is not None:
            view["offset_x"] = int(x) - _pending_launch[0]
            view["offset_y"] = int(y) - _pending_launch[1]
            _pending_launch = None
    if (
        w is not None
        and h is not None
        and MIN_SIZE <= w <= MAX_SIZE
        and MIN_SIZE <= h <= MAX_SIZE
    ):
        view["w"], view["h"] = int(w), int(h)
        if _pending_size is not None:
            # Asking for 1000 and being told 1001 is the frame, not a resize.
            # Storing the reported number verbatim grows the window by a pixel
            # every single launch.
            view["offset_w"] = int(w) - _pending_size[0]
            view["offset_h"] = int(h) - _pending_size[1]
            _pending_size = None
    write_prefs({"view": view})
    return view


# -- lifecycle ------------------------------------------------------------


def freeze() -> None:
    """Never launch a window again in this process. For shutdown."""
    global _frozen
    _frozen = True


def should_launch() -> tuple[bool, str]:
    """Whether to open a window now, and why not when the answer is no.

    Pure decision, kept out of ensure_started so it can be tested without
    spawning a browser.
    """
    if _frozen:
        return False, "shutting down"
    if not browser_enabled():
        return False, f"{BROWSER_ENV}=0"
    if viewer_present():
        return False, "a window in this process is already watching"
    claim = window_claim()
    if claim is not None:
        # There is one scope on this bench, so a second window would be a lie
        # about how many instruments exist.
        return False, (
            f"another process (pid {claim.get('pid')}) has a window at "
            f"{claim.get('url')}"
        )
    if time.monotonic() - _last_launch < LAUNCH_COOLDOWN_S:
        return False, "a window is already starting"
    return True, ""


def ensure_started(session: Any) -> str | None:
    """Start the UI server, and open a window only if none exists anywhere.

    Failing to show a window must never fail a measurement, so every error here
    is logged and swallowed.
    """
    global _last_launch
    if not enabled():
        return None
    try:
        url = _ensure_server(session)
        if url is None:
            return None
        launch, why_not = should_launch()
        if not launch:
            log.debug("not opening a window: %s", why_not)
            return url
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

    Without `force` this respects any window already open, in this process or
    another — "show me the display" must not mean "give me a second scope".
    """
    global _last_launch
    if not force and not should_launch()[0]:
        return False
    if force and not browser_enabled():
        return False
    _last_launch = time.monotonic()
    _open_edge(target)
    return True


def stop(close_window: bool = True) -> None:
    """Shut the display down, and take our window with us.

    A window outlives its server otherwise: the page cannot close itself —
    Chromium refuses window.close() for a window the script did not open
    (measured, not assumed) — so the process that opened it has to. Only our
    own window is closed: if the live claim belongs to another session, its
    window is still showing a real scope.
    """
    global _server, _url, _last_poll, _last_launch, _last_claim_write
    ours = _url is not None and (window_claim() or {}).get("url") == _url
    if _server is not None:
        _server.shutdown()
        _server.server_close()
        _server = None
    if close_window and ours:
        write_prefs({"window": {}})  # release the claim before the sweep
        close_stale_windows()
    _url = None
    _last_poll = 0.0
    _last_launch = 0.0
    _last_claim_write = 0.0


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


def close_stale_windows() -> int:
    """Close display windows left over from servers that have exited.

    Only processes using our own Edge profile are touched, so this can never
    close the user's browsing. Returns how many were asked to stop; failures
    are logged and swallowed, because a display is never worth a failed
    measurement.
    """
    if not EDGE_PROFILE_DIR.exists():
        return 0
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
        f"Where-Object {{ $_.CommandLine -like '*{EDGE_PROFILE_DIR.name}*' }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction "
        "SilentlyContinue; $_.ProcessId } | Measure-Object | "
        "Select-Object -ExpandProperty Count"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=SWEEP_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        count = int((done.stdout or "0").strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.debug("could not sweep stale display windows: %s", exc)
        return 0
    if count:
        log.info("closed %d stale display window process(es)", count)
    return count


def _open_edge(target: str) -> None:
    """Open the page in an Edge app window — no tabs, no address bar.

    Sweeps first: we only get here when no window is watching, so anything
    still on screen from our profile belongs to a server that has exited.
    Restores the size and position the window had when it was last seen, minus
    the frame offset measured then.
    """
    global _pending_launch, _pending_size
    edge = _edge_path()
    if edge is None:
        log.warning("Edge not found; open %s yourself", target)
        return

    close_stale_windows()
    view = view_prefs()
    width = int(view.get("w") or DEFAULT_SIZE[0]) - int(view.get("offset_w", 0))
    height = int(view.get("h") or DEFAULT_SIZE[1]) - int(view.get("offset_h", 0))
    width = max(MIN_SIZE, min(MAX_SIZE, width))
    height = max(MIN_SIZE, min(MAX_SIZE, height))
    _pending_size = (width, height)
    args = [
        edge,
        f"--user-data-dir={EDGE_PROFILE_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        f"--app={target}",
        f"--window-size={width},{height}",
    ]
    if "x" in view and "y" in view:
        x = int(view["x"]) - int(view.get("offset_x", 0))
        y = int(view["y"]) - int(view.get("offset_y", 0))
        args.append(f"--window-position={x},{y}")
        _pending_launch = (x, y)

    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        # Detach: the window must outlive a single tool call, and must not hold
        # the stdio pipes the MCP protocol runs on.
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    log.info("opened Edge at %s (%dx%d)", target, width, height)


# -- HTTP -----------------------------------------------------------------


def _ui_state() -> dict:
    """Everything the page draws, in one request."""
    session = _session
    from . import control  # late, same reason as in run_control

    base = {
        "version": version_line(),
        "activity": list(_activity),
        "view": view_prefs(),
        "sweep": control.sweep_status(),
    }
    if session is None:
        return {"open": False, **base}

    global _last_state
    if not session.lock.acquire(timeout=STATE_LOCK_WAIT_S):
        # The device is busy — most likely a capture waiting for its trigger.
        # Serve the last frame rather than make the window wait for the device.
        if _last_state is not None:
            return {**_last_state, **base, "busy": True}
        return {"open": session.is_open, **base, "busy": True}
    try:
        state = session.state()
        latest = next(reversed(session.captures.values()), None)
        if latest is not None:
            state["latest"] = {
                "capture_id": latest.capture_id,
                "measurements": measure(latest),
                "curve": downsample_minmax(latest.volts, latest.dt_s, UI_CURVE_POINTS),
            }
    finally:
        session.lock.release()
    state["simulated"] = state.get("backend") == "mock"
    _last_state = state
    return {**state, **base, "busy": False}


# What the page is allowed to do, as opposed to watch. Autoset only changes the
# range and timebase — a scope is a passive listener and the PS2104 has no
# signal generator — and it runs the same code the MCP tool runs, under the
# same lock. Anything added here needs the same three answers: one
# implementation, one lock, and a result the session can report afterwards.
CONTROLS = ("autoset", "trigger", "sweep", "coupling")


def run_control(action: str, values: dict) -> dict:
    """Perform a control action on behalf of the page."""
    from . import control  # imported late: control imports analysis, we import it too

    if action not in CONTROLS:
        raise ScopeError(
            f"Unknown action {action!r}. This display can run: {', '.join(CONTROLS)}."
        )
    if _session is None:
        raise ScopeError("No session is attached to this display.")

    if action == "autoset":
        result = control.autoset(_session)
        body = {
            "steps": result["steps"],
            "range_v": result["range_v"],
            "capture_id": result["capture_id"],
        }
    elif action == "trigger":
        level = _number(values, "level_v")
        if level is None:
            raise ScopeError("trigger needs level_v.")
        current = _session.trigger
        direction = (values.get("direction") or [current.direction])[0]
        mode = (values.get("mode") or ["edge"])[0]
        body = control.set_trigger(
            _session,
            mode=mode,
            threshold_v=level,
            direction=direction,
            delay_pct=current.delay_pct,
            auto_trigger_ms=current.auto_trigger_ms,
        )
    elif action == "coupling":
        value = (values.get("value") or [""])[0]
        body = control.configure_channel(_session, coupling=value)
    else:  # sweep
        mode = (values.get("mode") or ["auto"])[0]
        body = (
            control.stop_sweep(_session)
            if mode == "stop"
            else control.start_sweep(_session, mode)
        )

    record(action, "ok")
    return {"ok": True, "action": action, **body}


def _number(values: dict, key: str) -> float | None:
    try:
        return float(values[key][0])
    except (KeyError, IndexError, ValueError, TypeError):
        return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        global _last_poll
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
        elif parsed.path == "/state":
            _last_poll = time.monotonic()  # proof that a window is watching
            _claim_window()  # ... and the proof other processes read
            self._send(
                json.dumps(_ui_state()).encode("utf-8"), "application/json", cache=False
            )
        elif parsed.path == "/control":
            self._control(parse_qs(parsed.query))
        elif parsed.path == "/view":
            q = parse_qs(parsed.query)
            view = save_view(
                zoom=_number(q, "zoom"),
                x=_number(q, "x"),
                y=_number(q, "y"),
                w=_number(q, "w"),
                h=_number(q, "h"),
            )
            self._send(
                json.dumps(view).encode("utf-8"), "application/json", cache=False
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        parsed = urlparse(self.path)
        if parsed.path == "/control":
            self._control(parse_qs(parsed.query))
        else:
            self.send_error(404)

    def _control(self, query: dict) -> None:
        """Run an action for the page, answering with why not rather than 500."""
        action = (query.get("action") or [""])[0]
        try:
            body = run_control(action, query)
        except ScopeError as exc:
            log.warning("control %s: %s", action, exc)
            record(action or "control", "fel", str(exc))
            body = {"ok": False, "action": action, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the page must still get an answer
            log.exception("control %s failed", action)
            record(action or "control", "fel", str(exc))
            body = {"ok": False, "action": action, "error": f"{type(exc).__name__}: {exc}"}
        self._send(json.dumps(body).encode("utf-8"), "application/json", cache=False)

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
