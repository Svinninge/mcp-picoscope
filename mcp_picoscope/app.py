# File version: v0.01
"""PicoScope as a desktop app: one process that owns the instrument.

Opens the scope, shows the display for manual measurements, and serves the same
MCP tools over HTTP so Claude can drive the very same instrument while the app is
open. Close the window and everything goes: the sweep stops, the USB handle is
released, and the MCP endpoint shuts down.

Why one process serves both. The PS2104 can be opened by exactly one process.
An app that owned the device while Claude started a server of its own would make
them fight over it, and whichever lost would see "device busy". Serving MCP from
the process that owns the device means there is one owner and two ways in — and
"when the app closes, MCP closes" is simply true rather than arranged.

How closing is detected. The display is an Edge app window, which tells nobody
when it closes. But the page polls the server every 400 ms, and a recent poll is
already how the rest of the code decides a window is watching. When the polls
stop for CLOSE_AFTER_S, the window is gone. Nothing new to trust.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("mcp_picoscope.app")

MCP_HOST = "127.0.0.1"
MCP_PORT_ENV = "PICOSCOPE_MCP_PORT"
# Outside the display's port scan (8071-8080), so the two never collide.
DEFAULT_MCP_PORT = 8090
MCP_PATH = "/mcp"

# Edge needs time to start a cold profile and load the page; until the first
# poll arrives, silence means "not open yet", not "closed".
FIRST_POLL_TIMEOUT_S = 60.0
# Silence this long after the window stopped counting as present means it was
# closed. ui.viewer_present() already forgives 6 s without a poll, which is what
# keeps a page throttled in the background from reading as closed — so this only
# adds a margin on top. It was 10 s at first; with the 6 s in front of it, a
# closed window took over 20 s to end the process, and nothing on screen said so.
CLOSE_AFTER_S = 4.0
WATCH_INTERVAL_S = 0.5
HTTP_STOP_TIMEOUT_S = 5.0
# Streamable HTTP holds client connections open, and uvicorn by default waits for
# every one of them to finish before it stops — measured, the endpoint hung the
# full stop timeout. A short grace, then connections are closed regardless.
HTTP_GRACEFUL_S = 2


def log_path() -> Path:
    """Where a windowed app writes what a console app would have printed."""
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "mcp-picoscope"
    base.mkdir(parents=True, exist_ok=True)
    return base / "app.log"


class _IgnoreProactorResets(logging.Filter):
    """Drop the traceback Windows asyncio logs when an HTTP client hangs up.

    The proactor event loop reports every reset connection as an unhandled
    ConnectionResetError from _call_connection_lost — a known CPython quirk on
    Windows, not a fault. Measured: twelve full tracebacks for one client session
    and one closed window, and one more every time Claude disconnects. Filtered by
    exception type, so a real asyncio error still gets through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not isinstance(exc, ConnectionResetError)


def configure_logging() -> Path:
    path = log_path()
    handlers: list[logging.Handler] = [logging.FileHandler(path, encoding="utf-8")]
    if sys.stderr is not None:  # None when frozen without a console
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("asyncio").addFilter(_IgnoreProactorResets())
    # Every MCP request opens and terminates a transport session at INFO. With
    # Claude attached that is two lines per call and buries everything else.
    for chatty in ("mcp.server.streamable_http", "mcp.server.streamable_http_manager"):
        logging.getLogger(chatty).setLevel(logging.WARNING)
    return path


def wait_for_window_close(
    viewer_present: Callable[[], bool],
    *,
    first_poll_timeout_s: float = FIRST_POLL_TIMEOUT_S,
    close_after_s: float = CLOSE_AFTER_S,
    interval_s: float = WATCH_INTERVAL_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    stop: threading.Event | None = None,
) -> str:
    """Block until the display window has been opened and then closed.

    Returns why it stopped waiting: "closed", "never opened" or "stopped".
    Kept free of the UI module so the rule can be tested without a browser.
    """
    started = clock()
    seen = False
    silent_since: float | None = None
    while True:
        if stop is not None and stop.is_set():
            return "stopped"
        now = clock()
        if viewer_present():
            seen = True
            silent_since = None
        elif not seen:
            if now - started > first_poll_timeout_s:
                return "never opened"
        else:
            silent_since = silent_since if silent_since is not None else now
            if now - silent_since >= close_after_s:
                return "closed"
        sleep(interval_s)


class McpHttpServer:
    """The MCP tools over streamable HTTP, with a handle to stop them.

    MCPServer.run(transport="streamable-http") blocks and offers no way out. The
    same Starlette app is built here and run under a uvicorn.Server we keep, so
    shutdown is a request (should_exit) rather than killing the process around
    an open USB handle.
    """

    def __init__(self, server, host: str, port: int, path: str) -> None:
        import uvicorn

        app = server.streamable_http_app(streamable_http_path=path, host=host)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
            # uvicorn's own log config builds a formatter that calls
            # sys.stdout.isatty(); in the windowed exe there is no console and
            # stdout is None, so it crashed at start. Log through the app log.
            log_config=None,
            timeout_graceful_shutdown=HTTP_GRACEFUL_S,
        )
        self._uvicorn = uvicorn.Server(config)
        self._thread = threading.Thread(
            target=self._uvicorn.run, name="picoscope-mcp-http", daemon=True
        )
        self.url = f"http://{host}:{port}{path}"

    def start(self, timeout_s: float = 10.0) -> None:
        self._thread.start()
        deadline = time.monotonic() + timeout_s
        while not self._uvicorn.started:
            if not self._thread.is_alive():
                raise RuntimeError(
                    f"The MCP endpoint could not start on {self.url} — is another "
                    f"PicoScope app already running? Set {MCP_PORT_ENV} to use "
                    "another port."
                )
            if time.monotonic() > deadline:
                raise RuntimeError(f"The MCP endpoint did not start within {timeout_s} s.")
            time.sleep(0.05)

    def stop(self) -> None:
        self._uvicorn.should_exit = True
        self._thread.join(timeout=HTTP_STOP_TIMEOUT_S)
        if self._thread.is_alive():
            self._uvicorn.force_exit = True
            self._thread.join(timeout=HTTP_STOP_TIMEOUT_S)
        if self._thread.is_alive():
            log.warning("MCP endpoint did not stop within %.0f s", 2 * HTTP_STOP_TIMEOUT_S)


def main() -> int:
    logfile = configure_logging()
    log.info("PicoScope app starting; log at %s", logfile)

    # The same module the stdio server runs: the same tools, the same session,
    # the same lock. The app is a second transport, not a second implementation.
    from . import control, ui
    from . import server as mcp

    port = int(os.environ.get(MCP_PORT_ENV, DEFAULT_MCP_PORT))
    http = McpHttpServer(mcp.server, MCP_HOST, port, MCP_PATH)
    try:
        http.start()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    log.info("MCP tools served at %s", http.url)

    try:
        # Through the tool functions rather than around them, so the activity
        # log shows what the app did exactly as it shows what Claude does.
        opened = mcp.open_device("auto")
        if opened.get("warning"):
            log.warning("%s", opened["warning"])
        mcp.autoset()
        mcp.start_sweep("auto")
        if ui.url() is None:
            log.error("the display did not start; see the log above")
            return 1
        log.info("display at %s", ui.url())

        reason = wait_for_window_close(ui.viewer_present)
        log.info("window %s; shutting down", reason)
        return 0 if reason == "closed" else 1
    except Exception:  # noqa: BLE001 - a windowed app has nobody to show a traceback to
        log.exception("PicoScope app failed")
        return 1
    finally:
        # Order matters. Freeze the display first: closing the device goes
        # through a tool, every tool asks for a window, and with the user's window
        # just closed it would get a fresh one. Then nothing may still be
        # capturing when the device closes, and the device must be closed before
        # the process that owns it ends.
        ui.freeze()
        control.stop_for_shutdown()
        try:
            if mcp.session.is_open:
                mcp.close_device()
        except Exception:  # noqa: BLE001 - keep tearing down whatever remains
            log.exception("closing the device failed")
        ui.stop()
        http.stop()
        log.info("PicoScope app stopped")


if __name__ == "__main__":
    sys.exit(main())
