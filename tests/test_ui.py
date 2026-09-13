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


def test_a_frozen_display_never_launches_a_window(monkeypatch):
    """Shutdown must not open the window the user has just closed."""
    monkeypatch.setattr(ui, "_frozen", False)
    assert ui.should_launch()[0] is True
    ui.freeze()
    assert ui.should_launch() == (False, "shutting down")
    monkeypatch.setattr(ui, "_frozen", False)


# -- AC/DC coupling ---------------------------------------------------------
# Changing the coupling moves the signal's midpoint, so an armed trigger has to
# follow it or it never fires again. One implementation for both surfaces.


def offset_session():
    from mcp_picoscope.backends.mock import MockSignal

    # 0..3 V: the bench signal's shape, midpoint 1.5 V in DC, 0 V in AC.
    return sweep_session(MockSignal("sine", 1000.0, 1.5, offset_v=1.5, noise_v=0.0))


def test_switching_to_ac_moves_the_trigger_level_to_zero():
    from mcp_picoscope import control

    session = offset_session()
    control.set_trigger(session, "edge", 1.5, "rising")
    reply = control.configure_channel(session, coupling="AC")
    assert reply["coupling"] == "AC"
    assert abs(session.trigger.threshold_v) < 0.1, "trigger left at the DC midpoint"
    assert session.trigger.mode == "edge" and session.trigger.direction == "rising"


def test_switching_back_to_dc_moves_it_back_to_the_signal():
    from mcp_picoscope import control

    session = offset_session()
    control.configure_channel(session, coupling="AC")
    control.configure_channel(session, coupling="DC")
    assert session.trigger.threshold_v == pytest.approx(1.5, abs=0.1)


def test_a_range_change_alone_leaves_the_trigger_where_it_was():
    from mcp_picoscope import control

    session = offset_session()
    control.set_trigger(session, "edge", 1.234, "falling")
    reply = control.configure_channel(session, range_v=10.0)
    assert reply["range_v"] == 10.0
    assert session.trigger.threshold_v == pytest.approx(1.234)
    assert "trigger" not in reply


def test_an_unknown_coupling_is_refused_with_the_valid_ones():
    from mcp_picoscope import control
    from mcp_picoscope.scope import ScopeError

    session = offset_session()
    with pytest.raises(ScopeError, match="DC, AC"):
        control.configure_channel(session, coupling="GND")


def test_the_coupling_button_runs_the_same_code_as_the_tool(monkeypatch):
    from mcp_picoscope import control

    calls: list = []
    monkeypatch.setattr(ui, "_session", "the-session")
    monkeypatch.setattr(
        control,
        "configure_channel",
        lambda s, **kw: calls.append((s, kw)) or {"coupling": kw["coupling"]},
    )
    body = ui.run_control("coupling", {"value": ["AC"]})
    assert calls == [("the-session", {"coupling": "AC"})]
    assert body["ok"] and body["coupling"] == "AC"


def test_the_mock_ac_coupling_settles_like_the_hardware():
    """The model the coupling tests depend on: straight after the switch the DC is
    still there. Without this the mock removed it instantly and a trigger set from
    an unsettled capture passed every test, while the hardware put it at 1.258 V
    on a signal centred at 0 V."""
    from mcp_picoscope.analysis import measure
    from mcp_picoscope.scope import ChannelConfig

    session = offset_session()
    backend = session.backend
    backend.set_channel(ChannelConfig(5.0, "AC", True))
    early = measure(backend.capture_block(0.02, 4096))
    time.sleep(1.0)
    late = measure(backend.capture_block(0.02, 4096))
    assert (early["vmin_v"] + early["vmax_v"]) / 2 > 0.8, "no settling modelled"
    assert abs((late["vmin_v"] + late["vmax_v"]) / 2) < 0.1


def test_the_trigger_level_is_taken_after_ac_has_settled():
    from mcp_picoscope import control

    session = offset_session()
    control.set_trigger(session, "edge", 1.5, "rising")
    reply = control.configure_channel(session, coupling="AC")
    assert reply["settled"] is True
    assert abs(session.trigger.threshold_v) < 0.1, (
        f"level {session.trigger.threshold_v} V taken before the input settled"
    )


# -- trigger waits must never freeze anything --------------------------------


def test_switching_coupling_in_normal_mode_does_not_wait_for_the_old_level():
    """AC → DC with an edge trigger left at -0.05 V and no auto-trigger rescue.

    A 0..3 V signal never crosses -0.05 V, so a capture that honoured that
    trigger waited out its timeout and the whole switch failed — measured on the
    hardware. The settling captures run free-running now.
    """
    from mcp_picoscope import control

    session = offset_session()
    control.configure_channel(session, coupling="AC")
    control.set_trigger(session, "edge", -0.05, "rising", auto_trigger_ms=0)
    reply = control.configure_channel(session, coupling="DC")
    assert reply["coupling"] == "DC"
    assert session.trigger.mode == "edge", "the user's trigger mode was lost"
    assert session.trigger.auto_trigger_ms == 0, "NORMAL's no-rescue setting was lost"
    assert session.trigger.threshold_v == pytest.approx(1.5, abs=0.1)


def test_the_sweep_bounds_how_long_a_capture_may_wait_for_a_trigger():
    """Holding the lock for a 6 s trigger wait froze the display."""
    from mcp_picoscope import control

    session = sweep_session()
    seen: list = []
    real = session.backend.capture_block

    def recording(duration_s, samples, max_wait_s=None):
        seen.append(max_wait_s)
        return real(duration_s, samples, max_wait_s=max_wait_s)

    session.backend.capture_block = recording
    try:
        control.start_sweep(session, "single")
        deadline = time.monotonic() + 3
        while control.sweep_status()["running"] and time.monotonic() < deadline:
            time.sleep(0.05)
    finally:
        control.stop_sweep(session)
    assert seen and seen[0] == control.SWEEP_TRIGGER_WAIT_S


def test_the_display_serves_the_last_frame_while_the_device_is_busy(monkeypatch):
    """A capture waiting for its trigger holds the lock; the window must not wait."""
    import threading

    session = sweep_session()
    monkeypatch.setattr(ui, "_session", session)
    monkeypatch.setattr(ui, "_last_state", None)
    first = ui._ui_state()
    assert first["busy"] is False and first["open"] is True

    held = threading.Event()
    release = threading.Event()

    def hold_the_lock():
        with session.lock:
            held.set()
            release.wait(5)

    thread = threading.Thread(target=hold_the_lock)
    thread.start()
    held.wait(2)
    try:
        started = time.monotonic()
        busy = ui._ui_state()
        waited = time.monotonic() - started
    finally:
        release.set()
        thread.join()
    assert busy["busy"] is True
    assert busy["open"] is True, "the busy frame forgot the device was open"
    assert waited < 1.0, f"the state read waited {waited:.2f} s for the device"


# -- time/div ---------------------------------------------------------------
# Two traps from issue #1: the following must not overwrite a manual choice, and
# a manual timebase too slow for the signal must say it may alias.


def test_the_1_2_5_steps_move_one_step_and_stop_at_the_ends():
    from mcp_picoscope import control

    steps = control.TIME_PER_DIV_STEPS
    assert control._next_step(1e-3, +1) == 2e-3
    assert control._next_step(2e-3, -1) == 1e-3
    assert control._next_step(steps[-1], +1) == steps[-1]
    assert control._next_step(steps[0], -1) == steps[0]
    # From between steps (where auto leaves it), one click always changes it.
    assert control._next_step(1.3e-3, +1) == 2e-3
    assert control._next_step(1.3e-3, -1) == 1e-3


def test_a_manual_timebase_is_not_overwritten_by_the_following():
    """The trap from issue #1: set time/div, and two seconds later it is undone."""
    from mcp_picoscope import control

    session = sweep_session()  # 1 kHz: the following would want 10 ms
    try:
        control.start_sweep(session, "auto")
        control.set_time_per_div(session, 200e-6)
        time.sleep(1.2)  # several sweeps
        status = control.sweep_status()
        assert status["timebase_mode"] == "manual"
        assert status["time_per_div_s"] == pytest.approx(200e-6)
        assert status["window_s"] == pytest.approx(2e-3), "the following moved it"
    finally:
        control.stop_sweep(session)


def test_auto_hands_the_timebase_back_to_the_following():
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "auto")
        control.set_time_per_div(session, 20e-3)
        control.set_time_per_div(session, None)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if control.sweep_status()["window_s"] == pytest.approx(10e-3, rel=0.05):
                break
            time.sleep(0.05)
        status = control.sweep_status()
        assert status["timebase_mode"] == "auto"
        assert status["window_s"] == pytest.approx(10e-3, rel=0.05)
    finally:
        control.stop_sweep(session)


def test_autoset_takes_a_manual_timebase_back():
    from mcp_picoscope import control

    session = sweep_session()
    try:
        control.start_sweep(session, "auto")
        control.set_time_per_div(session, 20e-3)
        control.autoset(session)
        assert control.sweep_status()["timebase_mode"] == "auto"
    finally:
        control.stop_sweep(session)


def test_a_manual_timebase_too_slow_for_the_signal_warns_that_it_may_alias():
    """And the warning must come from the trusted frequency, not the alias."""
    from mcp_picoscope import control

    session = sweep_session()  # 1 kHz
    try:
        control.start_sweep(session, "auto")
        deadline = time.monotonic() + 3  # let the following measure and trust it
        while control.runner(session)._reference_hz is None and time.monotonic() < deadline:
            time.sleep(0.05)
        # 20 ms/div → 200 ms over 4096 samples ≈ 20 kS/s ≈ 20 samples per period:
        # still fine. Nothing slower exists in the sequence, so force it past.
        control.set_time_per_div(session, 20e-3)
        time.sleep(0.6)
        assert control.sweep_status()["timebase_warning"] == ""
        control.runner(session)._reference_hz = 5000.0  # a faster trusted signal
        time.sleep(0.6)
        warning = control.sweep_status()["timebase_warning"]
        assert "alias" in warning and "5000 Hz" in warning
    finally:
        control.stop_sweep(session)


def test_the_time_div_buttons_run_the_same_code_as_the_tool(monkeypatch):
    from mcp_picoscope import control

    calls: list = []
    monkeypatch.setattr(ui, "_session", "s")
    monkeypatch.setattr(control, "step_time_per_div", lambda s, d: calls.append(("step", d)) or {})
    monkeypatch.setattr(control, "set_time_per_div", lambda s, v: calls.append(("set", v)) or {})
    ui.run_control("timebase", {"step": ["1"]})
    ui.run_control("timebase", {"step": ["-1"]})
    ui.run_control("timebase", {"value": ["auto"]})
    assert calls == [("step", 1), ("step", -1), ("set", None)]


def test_a_faster_signal_seen_on_a_manual_timebase_raises_the_alias_reference():
    """Measured: autoset trusted 10 kHz, the generator went to 1 MHz, and a
    562 kHz alias at 0.2 ms/div showed no warning. An alias only reads lower, so
    a higher well-resolved frequency is the signal itself — but a lower one must
    never lower the reference, or the alias would vouch for itself."""
    from mcp_picoscope import control

    session = sweep_session()
    r = control.runner(session)
    r._timebase_mode = "manual"
    r._reference_hz = 10_000.0
    r._raise_reference(1_000_000.0, 25_000_000.0)
    assert r._reference_hz == 1_000_000.0
    r._raise_reference(2_206.0, 12_207.0 * 10)  # lower: an alias, ignored
    assert r._reference_hz == 1_000_000.0
    r._raise_reference(5_000_000.0, 25_000_000.0)  # 5 samples/period: not trusted
    assert r._reference_hz == 1_000_000.0
    assert "alias" in r._alias_warning(1_562_500.0)


def test_volt_div_steps_through_the_device_ranges_and_stops_at_the_ends():
    from mcp_picoscope import control

    session = sweep_session()
    ranges = sorted(session.device.voltage_ranges_v)
    control.configure_channel(session, range_v=ranges[2])
    assert control.step_range(session, +1)["range_v"] == ranges[3]
    assert control.step_range(session, -1)["range_v"] == ranges[2]
    control.configure_channel(session, range_v=ranges[-1])
    assert control.step_range(session, +1)["range_v"] == ranges[-1]
    control.configure_channel(session, range_v=ranges[0])
    assert control.step_range(session, -1)["range_v"] == ranges[0]


def test_the_volt_div_buttons_run_the_same_code_as_the_tool_path(monkeypatch):
    from mcp_picoscope import control

    calls: list = []
    monkeypatch.setattr(ui, "_session", "s")
    monkeypatch.setattr(control, "step_range", lambda s, d: calls.append(d) or {})
    ui.run_control("range", {"step": ["1"]})
    ui.run_control("range", {"step": ["-1"]})
    assert calls == [1, -1]


# -- screenshot (issue #6) ------------------------------------------------
# The name is user input that becomes a path. Whatever arrives, the file lands
# directly in the capture directory and never overwrites an earlier one.

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


@pytest.mark.parametrize(
    "name, expected",
    [
        ("scope test", "scope test"),
        ("../../evil", "evil"),
        (r"C:\Windows\x", "C_Windows_x"),
        ("a<b>|c?.PNG", "a_b_c"),
        ("con", "_con"),
        ("lpt1.backup", "_lpt1.backup"),
    ],
)
def test_a_screenshot_name_can_only_be_a_name(name, expected):
    from mcp_picoscope.export import safe_name

    assert safe_name(name) == expected


def test_an_empty_screenshot_name_is_refused():
    from mcp_picoscope.export import safe_name
    from mcp_picoscope.scope import ScopeError

    with pytest.raises(ScopeError):
        safe_name(" /.. ")


def test_a_screenshot_never_overwrites_and_never_leaves_the_folder(tmp_path, monkeypatch):
    from mcp_picoscope.export import save_screenshot

    monkeypatch.setenv("CAPTURE_DIR", str(tmp_path))
    first = save_screenshot(PNG, "../trace")
    second = save_screenshot(PNG, "trace")
    assert first == tmp_path / "trace.png"
    assert second == tmp_path / "trace-2.png"
    assert first.read_bytes() == PNG


def test_a_screenshot_must_be_a_png(tmp_path, monkeypatch):
    from mcp_picoscope.export import save_screenshot
    from mcp_picoscope.scope import ScopeError

    monkeypatch.setenv("CAPTURE_DIR", str(tmp_path))
    with pytest.raises(ScopeError):
        save_screenshot(b"<html>", "x")
    assert not list(tmp_path.iterdir())


def test_the_page_posts_a_screenshot_and_gets_the_path_back(tmp_path, monkeypatch):
    import threading
    import urllib.request
    from http.server import HTTPServer

    monkeypatch.setenv("CAPTURE_DIR", str(tmp_path))
    server = HTTPServer(("127.0.0.1", 0), ui._Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/screenshot?name=my%20trace",
            data=PNG, method="POST", headers={"Content-Type": "image/png"},
        )
        body = json.loads(urllib.request.urlopen(request, timeout=5).read())
    finally:
        server.shutdown()
        server.server_close()
    assert body["ok"] and body["name"] == "my trace.png"
    assert (tmp_path / "my trace.png").read_bytes() == PNG
