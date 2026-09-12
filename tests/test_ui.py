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
        ui.run_control("close_device")
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
    body = ui.run_control("autoset")
    assert calls == ["the-session"]
    assert body["ok"] and body["capture_id"] == "cap0007"


def test_a_control_action_shows_up_in_the_activity_log(monkeypatch):
    """The LLM and the user must see what the other one did."""
    from mcp_picoscope import control

    monkeypatch.setattr(ui, "_session", "s")
    monkeypatch.setattr(
        control, "autoset", lambda s: {"steps": [], "range_v": 1.0, "capture_id": "c"}
    )
    ui.run_control("autoset")
    assert ui._activity[0]["tool"] == "autoset"
