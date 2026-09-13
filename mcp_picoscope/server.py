# File version: v0.04
"""MCP surface for the PicoScope. Thin: it translates, it does not compute.

Every tool answers with a summary — statistics, a decimated curve, a file path —
and never with a raw sample array. A block capture is tens of thousands of
points; sending one would blow the caller's context window for no gain.
"""

from __future__ import annotations

import functools
import logging
import sys
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import control, ui, version_line
from .analysis import measure as measure_capture
from .backends.mock import MockBackend, MockSignal
from .export import export as export_file
from .scope import ChannelConfig, ScopeError, ScopeSession, TriggerConfig

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mcp_picoscope")

server = MCPServer(
    name="picoscope",
    instructions=(
        "Controls a PicoScope PS2104 USB oscilloscope, or a simulated one when no "
        "hardware is present. Call open_device first, then autoset for a quick look "
        "or configure_channel/configure_trigger/capture_block for a deliberate "
        "measurement. Captures stay on the server: tools answer with statistics, a "
        "decimated curve and file paths, never with raw samples. A live display "
        "opens in a browser window on the first call, showing the trace, the "
        "measurements and which tools have run; mention it to the user rather "
        "than describing the waveform in words."
    ),
)
session = ScopeSession()


def tool(fn: Callable) -> Callable:
    """Register a tool, turning ScopeError into a message the caller can read.

    Anything else would surface as "Error executing tool X", which helps nobody
    on the other side of a stdio pipe.

    This is also where the display hangs: every tool call passes through here
    exactly once, so the window opens and the activity list fills in one place
    instead of in twelve.
    """

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        ui.ensure_started(session)
        try:
            with session.lock:
                result = fn(*args, **kwargs)
        except ScopeError as exc:
            log.warning("%s: %s", fn.__name__, exc)
            ui.record(fn.__name__, "fel", str(exc))
            raise ToolError(str(exc)) from exc
        except Exception as exc:  # unexpected, but still must be readable
            log.exception("%s failed", fn.__name__)
            ui.record(fn.__name__, "fel", str(exc))
            raise ToolError(f"{type(exc).__name__} in {fn.__name__}: {exc}") from exc
        ui.record(fn.__name__, "ok")
        return result

    return server.tool()(wrapper)


# -- device ---------------------------------------------------------------


@tool
def list_devices() -> dict:
    """List connected PicoScopes. Always reports the mock as available too.

    Opens and closes each device briefly — ps2000 has no other way to enumerate.
    """
    devices: list[dict] = []
    hardware_note = ""
    try:
        from .backends.ps2000 import PS2000Backend

        devices.extend(PS2000Backend().list_devices())
    except ScopeError as exc:
        hardware_note = str(exc)
    devices.extend(MockBackend().list_devices())
    return {"devices": devices, "hardware_note": hardware_note}


@tool
def open_device(backend: str = "auto") -> dict:
    """Open the scope. backend: 'auto' | 'ps2000' | 'mock'.

    'auto' tries the real device and falls back to the mock, saying so loudly in
    the reply. Opening an already-open device just reports its state.
    """
    if session.is_open:
        return {"already_open": True, **session.state()}
    if backend not in ("auto", "ps2000", "mock"):
        raise ScopeError(f"backend must be 'auto', 'ps2000' or 'mock', got {backend!r}.")

    warning = ""
    chosen: Any = None
    if backend in ("auto", "ps2000"):
        try:
            from .backends.ps2000 import PS2000Backend

            chosen = PS2000Backend()
            info = chosen.open()
        except ScopeError as exc:
            if backend == "ps2000":
                raise
            chosen = None
            warning = (
                "No hardware: " + str(exc) + " Falling back to the MOCK backend — "
                "every measurement below is simulated, not measured."
            )
    if chosen is None:
        chosen = MockBackend()
        info = chosen.open()

    session.backend = chosen
    session.device = info
    session.channel = chosen.channel
    session.trigger = chosen.trigger
    log.info("opened %s (%s)", info.backend, info.model)
    return {"warning": warning, **session.state()}


@tool
def close_device() -> dict:
    """Close the device and release the USB handle."""
    control.stop_for_shutdown()  # no thread may keep capturing from it
    if not session.is_open:
        return {"open": False, "note": "No device was open."}
    session.backend.close()  # type: ignore[union-attr]
    session.backend = None
    session.device = None
    session.captures.clear()
    return {"open": False, "note": "Device closed; held captures were dropped."}


@tool
def get_device_info() -> dict:
    """Model, serial, driver version, channels, ranges and sampling limits."""
    session.require_open()
    return session.state()["device"]


@tool
def get_server_info() -> dict:
    """Server version, capture directory and the URL of the live display."""
    from .export import capture_dir

    return {
        "version": version_line(),
        "capture_dir": str(capture_dir()),
        "ui_url": ui.url(),
        "ui_enabled": ui.enabled(),
        "tools": sorted(t.name for t in server._tool_manager.list_tools()),
    }


@tool
def open_ui(force: bool = False) -> dict:
    """Show the live display and return its URL.

    A window opens by itself when one is needed, so this is for bringing it
    back after you closed it. It reuses the window that is already watching;
    pass force=true to open a second one, for another screen.
    """
    if not ui.enabled():
        raise ScopeError(
            f"The display is switched off ({ui.ENABLED_ENV}=0). Unset that "
            "environment variable and restart the server to use it."
        )
    target = ui.ensure_started(session) or ui.url()
    if target is None:
        raise ScopeError(
            "The display server could not start — no free port was available. "
            f"Set {ui.PORT_ENV} to pick another one."
        )
    launched = ui.reopen(target, force=force)
    return {
        "url": target,
        "launched": launched,
        "note": (
            "Opened a new window."
            if launched
            else "A window is already showing the display; reused it."
        ),
    }


# -- configuration --------------------------------------------------------


@tool
def configure_channel(
    range_v: float = 5.0, coupling: str = "DC", enabled: bool = True
) -> dict:
    """Set channel A. range_v is full scale in volts (±), coupling 'DC' or 'AC'.

    The device snaps to the smallest range that holds range_v; the reply says
    which one it picked. Changing the coupling moves the signal's midpoint (a
    0..3 V sine sits at 1.5 V in DC and 0 V in AC), so the trigger level follows
    it — otherwise an armed trigger would never fire after the switch.
    """
    # The display's AC/DC button runs this same function, so a coupling change
    # moves the trigger level the same way whoever asks for it.
    return control.configure_channel(session, range_v, coupling, enabled)


@tool
def configure_trigger(
    mode: str = "auto",
    threshold_v: float = 0.0,
    direction: str = "rising",
    delay_pct: float = 0.0,
    auto_trigger_ms: int = 1000,
) -> dict:
    """Set the trigger.

    mode 'auto' free-runs; 'edge' waits for threshold_v crossed in `direction`.
    delay_pct places the trigger in the record (0 = at the start, 50 = middle).
    auto_trigger_ms is how long an edge trigger waits before capturing anyway;
    0 means wait forever, which can time out the capture.
    """
    return control.set_trigger(
        session, mode, threshold_v, direction, delay_pct, auto_trigger_ms
    )


@tool
def configure_mock_signal(
    waveform: str = "sine",
    frequency_hz: float = 1000.0,
    amplitude_v: float = 1.0,
    offset_v: float = 0.0,
    noise_v: float = 0.002,
    duty_cycle_pct: float = 50.0,
) -> dict:
    """Set what the MOCK backend generates. No effect on real hardware.

    waveform: sine | square | ramp | triangle | noise | dc.
    amplitude_v is the peak, so Vpp is twice it.
    """
    backend = session.require_open()
    if not isinstance(backend, MockBackend):
        raise ScopeError(
            "The open device is real hardware — its signal comes from the probes, "
            "not from a setting."
        )
    applied = backend.set_signal(
        MockSignal(waveform, frequency_hz, amplitude_v, offset_v, noise_v, duty_cycle_pct)
    )
    return applied.__dict__


# -- acquisition ----------------------------------------------------------


@tool
def capture_block(duration_s: float = 0.01, samples: int = 4096) -> dict:
    """Capture one block and return statistics plus a decimated curve.

    The actual sample rate is rarely the one implied by duration_s/samples — the
    driver snaps to its own timebase grid, and the reply reports what it got.
    The full record stays on the server; use export_capture to get the samples.
    """
    return control.capture_block(session, duration_s, samples)


@tool
def measure(capture_id: str) -> dict:
    """Vpp, Vmin/Vmax, mean, RMS, frequency, period and duty cycle of a capture."""
    return measure_capture(session.get_capture(capture_id))


@tool
def export_capture(capture_id: str, format: str = "csv") -> dict:
    """Write a capture to disk. format: 'csv' | 'npz' | 'png'. Returns the path."""
    capture = session.get_capture(capture_id)
    path = export_file(capture, format)
    return {
        "capture_id": capture_id,
        "format": format,
        "path": str(path),
        "samples": int(capture.volts.size),
    }


@tool
def autoset() -> dict:
    """Find a range and timebase that show the signal — the AutoSetup button.

    Surveys on the widest range, measures the frequency, then re-captures about
    five periods on the smallest range that holds the peaks with headroom. The
    display's Autoset button runs this same code, under the same lock.
    """
    return control.autoset(session)


@tool
def start_sweep(mode: str = "auto", window_s: float = 0.0) -> dict:
    """Capture continuously until stopped. mode: 'auto' | 'normal' | 'single'.

    'auto' sweeps whether or not the trigger fires, 'normal' only on a real
    trigger, and 'single' captures once and stops — the three positions on a
    bench scope. The window follows the measured frequency unless window_s
    says otherwise. The display's Run/Single buttons call this same engine.
    """
    return control.start_sweep(session, mode, window_s or None)


@tool
def stop_sweep() -> dict:
    """Stop the continuous capture started by start_sweep."""
    return control.stop_sweep(session)


# -- resource -------------------------------------------------------------


@server.resource("picoscope://state")
def state_resource() -> dict:
    """Current backend, device, channel, trigger, sweep and held captures."""
    with session.lock:
        state = session.state()
    state["sweep"] = control.sweep_status()
    return state


def main() -> None:
    """Entry point for `python -m mcp_picoscope.server` and the console script."""
    try:
        server.run(transport="stdio")
    finally:
        control.stop_for_shutdown()
        ui.stop()
        if session.backend is not None:
            session.backend.close()  # never leave the USB handle open


if __name__ == "__main__":
    main()
