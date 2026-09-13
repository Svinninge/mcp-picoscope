# File version: v0.01
"""The desktop app's one rule: the window closing is what ends everything.

It is detected from silence â€” the page stops polling â€” so the rule has two ways
to go wrong, and both are tested: shutting the instrument off while the window is
merely slow to open, and never noticing that it closed.
"""

from __future__ import annotations

from mcp_picoscope.app import wait_for_window_close


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def run(timeline, **kwargs):
    """timeline(t) -> whether a page has polled recently at time t."""
    clock = FakeClock()
    return wait_for_window_close(
        lambda: timeline(clock.now),
        clock=clock,
        sleep=clock.sleep,
        interval_s=0.5,
        **kwargs,
    ), clock.now


def test_a_window_that_opens_and_closes_ends_the_app():
    reason, when = run(lambda t: 2.0 <= t < 20.0, close_after_s=10.0)
    assert reason == "closed"
    assert 30.0 <= when <= 31.0, "should end close_after_s after the last poll"


def test_a_slow_start_is_not_mistaken_for_a_closed_window():
    """Edge on a cold profile can take a while; silence before the first poll is not closing."""
    reason, when = run(
        lambda t: 25.0 <= t < 40.0, close_after_s=10.0, first_poll_timeout_s=60.0
    )
    assert reason == "closed"
    assert when >= 50.0, "gave up during the slow start"


def test_a_brief_gap_in_polling_does_not_shut_the_instrument_off():
    """A page throttled in the background skips a few polls; that is not a close."""
    polling = lambda t: (1.0 <= t < 10.0) or (14.0 <= t < 30.0)  # noqa: E731 - 4 s gap
    reason, when = run(polling, close_after_s=10.0)
    assert reason == "closed"
    assert when >= 40.0, "a 4 s gap was treated as the window closing"


def test_a_window_that_never_opens_gives_up_rather_than_hanging():
    reason, when = run(lambda t: False, first_poll_timeout_s=60.0)
    assert reason == "never opened"
    assert 60.0 <= when <= 61.0


def test_the_wait_can_be_stopped_from_outside():
    import threading

    stop = threading.Event()
    stop.set()
    reason, _ = run(lambda t: True, stop=stop)
    assert reason == "stopped"


def test_a_client_hanging_up_is_filtered_but_real_errors_are_not():
    import logging

    from mcp_picoscope.app import _IgnoreProactorResets

    def record(exc):
        try:
            raise exc
        except Exception:  # noqa: BLE001
            import sys

            return logging.LogRecord("asyncio", logging.ERROR, "", 0, "boom", None, sys.exc_info())

    keep = _IgnoreProactorResets().filter
    assert keep(record(ConnectionResetError(10054, "reset"))) is False
    assert keep(record(RuntimeError("a real problem"))) is True
    assert keep(logging.LogRecord("asyncio", logging.ERROR, "", 0, "no exc", None, None)) is True


def test_the_http_server_starts_without_a_console(monkeypatch):
    """The windowed exe has sys.stdout = None; uvicorn's default log config
    called isatty() on it and the app died before the window opened."""
    import socket
    import sys

    from mcp_picoscope import server
    from mcp_picoscope.app import McpHttpServer

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    http = McpHttpServer(server.server, "127.0.0.1", port, "/mcp")
    http.start()
    http.stop()
