# File version: v0.01
"""The display's rules: one window per machine, and it remembers where it was.

There is a single PS2104 on this bench. A second window is not a cosmetic
annoyance — it claims a second instrument exists. That rule spans processes, so
it lives in a shared file rather than in a variable, and it is worth a test.
"""

from __future__ import annotations

import json
import time

import pytest

from mcp_picoscope import ui


@pytest.fixture(autouse=True)
def isolated_prefs(tmp_path, monkeypatch):
    """Never touch the real shared file; never launch a real browser."""
    monkeypatch.setattr(ui, "PREFS_FILE", tmp_path / "ui.json")
    monkeypatch.setattr(ui, "_last_poll", 0.0)
    monkeypatch.setattr(ui, "_last_launch", 0.0)
    monkeypatch.setattr(ui, "_last_claim_write", 0.0)
    monkeypatch.setattr(ui, "_pending_launch", None)
    monkeypatch.setenv(ui.BROWSER_ENV, "1")
    yield


def claim(ts_offset: float = 0.0, pid: int = 4242, url: str = "http://127.0.0.1:8071/"):
    ui.write_prefs({"window": {"url": url, "pid": pid, "ts": time.time() + ts_offset}})


def test_no_window_anywhere_means_launch():
    assert ui.should_launch()[0] is True


def test_another_process_with_a_window_blocks_the_launch():
    claim()
    launch, why = ui.should_launch()
    assert launch is False
    assert "4242" in why and "8071" in why


def test_a_stale_claim_does_not_block():
    """A process that died left its claim behind; it must not silence us."""
    claim(ts_offset=-(ui.VIEWER_TIMEOUT_S + 1))
    assert ui.window_claim() is None
    assert ui.should_launch()[0] is True


def test_our_own_viewer_blocks_the_launch(monkeypatch):
    monkeypatch.setattr(ui, "_last_poll", time.monotonic())
    assert ui.should_launch() == (False, "a window in this process is already watching")


def test_cooldown_blocks_a_second_launch_while_one_is_starting(monkeypatch):
    monkeypatch.setattr(ui, "_last_launch", time.monotonic())
    assert ui.should_launch()[0] is False


def test_browser_disabled_never_launches(monkeypatch):
    monkeypatch.setenv(ui.BROWSER_ENV, "0")
    assert ui.should_launch()[0] is False
    assert ui.reopen("http://127.0.0.1:8071/", force=True) is False


def test_reopen_respects_another_process_but_force_overrides(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(ui, "_open_edge", lambda target: launched.append(target))
    claim()
    assert ui.reopen("http://127.0.0.1:8072/") is False
    assert launched == []
    assert ui.reopen("http://127.0.0.1:8072/", force=True) is True
    assert launched == ["http://127.0.0.1:8072/"]


def test_claim_is_written_and_read_back():
    ui._claim_window()
    held = ui.window_claim()
    assert held is not None and held["pid"] == __import__("os").getpid()


def test_view_survives_a_round_trip():
    ui.save_view(zoom=0.7, x=120, y=64, w=900, h=600)
    assert ui.view_prefs() == {"zoom": 0.7, "x": 120, "y": 64, "w": 900, "h": 600}


def test_frame_offset_is_learned_so_the_window_stops_creeping(monkeypatch):
    """Edge puts the content below the title bar; without this the window walks."""
    monkeypatch.setattr(ui, "_pending_launch", (100, 200))
    view = ui.save_view(x=108, y=245, w=900, h=600)
    assert view["offset_x"] == 8 and view["offset_y"] == 45
    # A later report is just a move, not a new measurement of the frame.
    view = ui.save_view(x=300, y=400)
    assert view["offset_x"] == 8 and view["offset_y"] == 45
    assert (view["x"], view["y"]) == (300, 400)


def test_absurd_geometry_is_ignored():
    """A window remembered at -30000 could never be found again."""
    ui.save_view(x=50, y=50, w=900, h=600)
    ui.save_view(x=-99999, y=-99999, w=1, h=1)
    view = ui.view_prefs()
    assert (view["x"], view["y"], view["w"], view["h"]) == (50, 50, 900, 600)


def test_unreadable_prefs_file_is_not_fatal(tmp_path, monkeypatch):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(ui, "PREFS_FILE", broken)
    assert ui.read_prefs() == {}
    assert ui.window_claim() is None
    assert ui.should_launch()[0] is True


def test_launch_arguments_carry_the_remembered_geometry(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ui, "_edge_path", lambda: "msedge.exe")
    # The sweep shells out through subprocess.run, which builds a Popen — stub
    # it too, or stubbing Popen breaks the sweep instead of the launch.
    monkeypatch.setattr(ui, "close_stale_windows", lambda: 0)
    monkeypatch.setattr(
        ui.subprocess, "Popen", lambda args, **kw: seen.update(args=args)
    )
    ui.save_view(zoom=0.8, x=310, y=245, w=840, h=560)
    ui.write_prefs({"view": {**ui.view_prefs(), "offset_x": 8, "offset_y": 45}})
    ui._open_edge("http://127.0.0.1:8071/")
    args = seen["args"]
    assert "--window-size=840,560" in args
    # Asked for the frame position that puts the content back where it was.
    assert "--window-position=302,200" in args


def test_prefs_file_is_json_a_person_can_read(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "PREFS_FILE", tmp_path / "ui.json")
    ui.save_view(zoom=1.0, x=1, y=2, w=800, h=600)
    ui._claim_window()
    data = json.loads((tmp_path / "ui.json").read_text(encoding="utf-8"))
    assert set(data) == {"view", "window"}


def test_shutdown_closes_our_own_window(monkeypatch):
    """A window must not outlive its server: the page cannot close itself."""
    swept: list[int] = []
    monkeypatch.setattr(ui, "_url", "http://127.0.0.1:8071/")
    monkeypatch.setattr(ui, "close_stale_windows", lambda: swept.append(1) or 1)
    ui._claim_window()
    ui.stop()
    assert swept == [1]
    assert ui.window_claim() is None


def test_shutdown_leaves_another_sessions_window_alone(monkeypatch):
    """Its window is still showing a real scope; ours was never opened."""
    swept: list[int] = []
    monkeypatch.setattr(ui, "_url", "http://127.0.0.1:8095/")
    monkeypatch.setattr(ui, "close_stale_windows", lambda: swept.append(1) or 1)
    claim(url="http://127.0.0.1:8071/")
    ui.stop()
    assert swept == []
    assert ui.window_claim() is not None


def test_launch_runs_in_its_own_edge_profile(monkeypatch):
    """The profile is what makes a stale window safe to close."""
    seen: dict = {}
    monkeypatch.setattr(ui, "_edge_path", lambda: "msedge.exe")
    monkeypatch.setattr(ui, "close_stale_windows", lambda: 0)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda args, **kw: seen.update(args=args))
    ui._open_edge("http://127.0.0.1:8071/")
    assert f"--user-data-dir={ui.EDGE_PROFILE_DIR}" in seen["args"]


def test_a_launch_sweeps_stale_windows_first(monkeypatch):
    """We only get here with no window watching, so anything left is a corpse."""
    order: list[str] = []
    monkeypatch.setattr(ui, "_edge_path", lambda: "msedge.exe")
    monkeypatch.setattr(ui, "close_stale_windows", lambda: order.append("sweep") or 0)
    monkeypatch.setattr(
        ui.subprocess, "Popen", lambda args, **kw: order.append("launch")
    )
    ui._open_edge("http://127.0.0.1:8071/")
    assert order == ["sweep", "launch"]


def test_size_offset_is_learned_so_the_window_stops_growing(monkeypatch):
    """Asking for 1000 and being told 1001 is the frame, not a resize."""
    monkeypatch.setattr(ui, "_pending_size", (1000, 680))
    view = ui.save_view(x=10, y=10, w=1001, h=681)
    assert view["offset_w"] == 1 and view["offset_h"] == 1

    seen: dict = {}
    monkeypatch.setattr(ui, "_edge_path", lambda: "msedge.exe")
    monkeypatch.setattr(ui, "close_stale_windows", lambda: 0)
    monkeypatch.setattr(ui.subprocess, "Popen", lambda args, **kw: seen.update(args=args))
    ui._open_edge("http://127.0.0.1:8071/")
    # Asks for 1000 again, so the window comes back the size the user had.
    assert "--window-size=1000,680" in seen["args"]


# -- the page as a control surface, not just a display --------------------


def test_only_whitelisted_actions_run(monkeypatch):
    monkeypatch.setattr(ui, "_session", object())
    with pytest.raises(Exception) as exc:
        ui.run_control("close_device", {})
    assert "Unknown action" in str(exc.value)


def test_autoset_from_the_page_runs_the_same_code_as_the_tool(monkeypatch):
    """One implementation, or the two surfaces drift apart."""
    from mcp_picoscope import control

    calls: list = []
    monkeypatch.setattr(ui, "_session", "the-session")
    monkeypatch.setattr(
        control,
        "autoset",
        lambda s: calls.append(s)
        or {"steps": ["picked ±5 V"], "range_v": 5.0, "capture_id": "cap0007"},
    )
    body = ui.run_control("autoset", {})
    assert calls == ["the-session"]
    assert body["ok"] and body["capture_id"] == "cap0007"


def test_a_control_action_shows_up_in_the_activity_log(monkeypatch):
    """The LLM and the user must see what the other one did."""
    from mcp_picoscope import control

    monkeypatch.setattr(ui, "_session", "s")
    monkeypatch.setattr(
        control, "autoset", lambda s: {"steps": [], "range_v": 1.0, "capture_id": "c"}
    )
    ui.run_control("autoset", {})
    assert ui._activity[0]["tool"] == "autoset"


# -- sweep engine ---------------------------------------------------------
# Who drives the acquisition was the open question in issue #1: a thread that
# captures on its own must be stoppable, must not hold the lock across the
# loop, and must survive a trigger that never fires.


def sweep_session(signal=None):
    from mcp_picoscope.backends.mock import MockBackend, MockSignal
    from mcp_picoscope.scope import ScopeSession

    session = ScopeSession()
    backend = MockBackend(signal or MockSignal("sine", 1000.0, 1.0))
    session.backend = backend
    session.device = backend.open()
    return session


def test_a_sweep_captures_until_stopped():
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "auto")
        deadline = time.monotonic() + 3
        while control.sweep_status()["sweeps"] < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert control.sweep_status()["running"] is True
        assert control.sweep_status()["sweeps"] >= 3
    finally:
        status = control.stop_sweep(session)
    assert status["running"] is False


def test_single_stops_itself_after_one():
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "single")
        deadline = time.monotonic() + 3
        while control.sweep_status()["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
        status = control.sweep_status()
        assert status["running"] is False and status["sweeps"] == 1
    finally:
        control.stop_sweep(session)


def test_the_lock_is_free_between_sweeps():
    """An MCP call must never wait for the loop, only for one capture."""
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "auto")
        time.sleep(0.3)
        got_it = session.lock.acquire(timeout=2.0)
        assert got_it, "the sweep held the session lock across its loop"
        session.lock.release()
    finally:
        control.stop_sweep(session)


def test_a_trigger_that_never_fires_is_a_state_not_a_crash():
    from mcp_picoscope import control
    from mcp_picoscope.scope import TriggerConfig

    session = sweep_session()
    session.backend.set_trigger(
        TriggerConfig("edge", threshold_v=4.0, direction="rising", auto_trigger_ms=0)
    )
    session.trigger = session.backend.trigger
    try:
        control.start_sweep(session, "normal")
        deadline = time.monotonic() + 3
        while not control.sweep_status()["error"] and time.monotonic() < deadline:
            time.sleep(0.05)
        status = control.sweep_status()
        assert status["running"] is True, "the sweep gave up instead of waiting"
        assert "Trigger never fired" in status["error"]
    finally:
        control.stop_sweep(session)


def test_normal_arms_a_trigger_that_was_free_running():
    """Without this the button was a no-op from the state a scope opens in."""
    from mcp_picoscope import control

    session = sweep_session()
    assert session.trigger.mode == "auto"
    try:
        control.start_sweep(session, "normal")
        time.sleep(0.3)
        assert session.trigger.mode == "edge"
        assert session.trigger.auto_trigger_ms == 0
    finally:
        control.stop_sweep(session)


def test_a_level_dragged_with_a_mouse_is_not_stored_to_16_digits():
    from mcp_picoscope import control

    session = sweep_session()
    applied = control.set_trigger(session, "edge", 1.7111404667547336, "rising")
    assert applied["threshold_v"] == 1.7111


def test_a_window_too_short_to_see_the_signal_widens_itself():
    """The sweep could trap itself: seen live, stuck at 20 us with 800 Hz present.

    Too short a window holds fewer than two edges, so no frequency is measured,
    so the retune that would widen it never fires. Widening after a few empty
    sweeps is what breaks the cycle.
    """
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "auto", window_s=control.SWEEP_MIN_WINDOW_S)
        assert control.sweep_status()["window_s"] == control.SWEEP_MIN_WINDOW_S
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = control.sweep_status()
            if status["window_s"] > control.SWEEP_MIN_WINDOW_S * 4:
                break
            time.sleep(0.05)
        widened = control.sweep_status()["window_s"]
        assert widened > control.SWEEP_MIN_WINDOW_S, "the sweep stayed stuck"
    finally:
        control.stop_sweep(session)


def test_autoset_hands_its_window_to_a_running_sweep():
    """Otherwise autoset is invisible: the sweep overwrites it 150 ms later."""
    from mcp_picoscope import control
    from mcp_picoscope.backends.mock import MockSignal

    session = sweep_session(MockSignal("sine", 50.0, 1.0))
    try:
        control.start_sweep(session, "auto", window_s=control.SWEEP_MIN_WINDOW_S)
        result = control.autoset(session)
        chosen = result["measurements"]["duration_s"]
        # The sweep now asks for what autoset chose, not its own stale window.
        assert control.sweep_status()["window_s"] > control.SWEEP_MIN_WINDOW_S
        assert chosen > control.SWEEP_MIN_WINDOW_S
    finally:
        control.stop_sweep(session)


def test_the_sweep_leaves_a_chosen_window_alone_while_the_signal_holds():
    """Autoset picks five periods; the sweep preferring ten must not overrule it."""
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "auto")
        chosen = control.autoset(session)["measurements"]["duration_s"]
        window = control.sweep_status()["window_s"]
        time.sleep(1.0)  # several sweeps
        assert control.sweep_status()["window_s"] == window, "the sweep overrode autoset"
        assert chosen > 0
    finally:
        control.stop_sweep(session)


def test_autoset_puts_the_trigger_level_at_half_of_peak_to_peak():
    """Zero is the wrong default: a 0..3 V signal never crosses it."""
    from mcp_picoscope import control
    from mcp_picoscope.backends.mock import MockSignal

    session = sweep_session(MockSignal("sine", 1000.0, 1.5, offset_v=1.5, noise_v=0.0))
    result = control.autoset(session)
    stats = result["measurements"]
    midpoint = (stats["vmin_v"] + stats["vmax_v"]) / 2
    assert session.trigger.threshold_v == pytest.approx(midpoint, abs=0.05)
    assert session.trigger.threshold_v > 1.0, "still sitting at zero"
    assert "half of peak-to-peak" in " ".join(result["steps"])


def test_autoset_gives_back_the_trigger_mode_it_found():
    """An armed edge trigger must survive an autoset; only the level moves."""
    from mcp_picoscope import control
    from mcp_picoscope.backends.mock import MockSignal

    session = sweep_session(MockSignal("sine", 1000.0, 1.5, offset_v=1.5, noise_v=0.0))
    control.set_trigger(session, "edge", 0.0, "falling", auto_trigger_ms=250)
    control.autoset(session)
    assert session.trigger.mode == "edge"
    assert session.trigger.direction == "falling"
    assert session.trigger.auto_trigger_ms == 250
    assert session.trigger.threshold_v > 1.0, "the level did not move to the signal"
