# mcp-picoscope — AI instructions (entry point)

A thin entry point. An MCP server that exposes a **PicoScope PS2104** as tools
for a Claude session: open the device, set channel and trigger, capture a block,
measure and export.

**The project's core principle:** the server **measures and reports**. It never
drives the outside world and never touches the thing being measured. It never
answers with raw samples — statistics, a decimated curve and a file path, or a
single capture would blow the context window.

## Base context (read at session start)

- **[SOUL.md](SOUL.md)** — HOW we work (approvals, plan mode, mini-sprint, the
  elegance pause, the lessons loop, hardware, code and git rules).
  **Authoritative.**
- **[LESSONS.md](LESSONS.md)** — lessons from earlier mistakes. Read after SOUL.

## Read on demand

- **[PLAN.md](PLAN.md)** — goals, architecture, steps 0–6, risks, open
  questions. Read when the task touches architecture or what to build next.
  **Note:** PLAN.md describes the target design. The code is the truth —
  `capture_streaming` appears in the plan's tool table but is not built (v2, see
  TODO.md).
- **[TODO.md](TODO.md)** — the active backlog, hand-maintained in this repo.
- **[README.md](README.md)** — installation and getting started.

## Layout

```
mcp_picoscope/server.py          The MCP surface. Thin: it translates, it does not compute.
mcp_picoscope/scope.py           Value types, backend protocol, ScopeSession (the lock)
mcp_picoscope/analysis.py        Vpp/RMS/frequency/duty + min-max decimation
mcp_picoscope/control.py         Actions and the sweep engine — shared by MCP and the page
mcp_picoscope/export.py          CSV / NPZ / PNG under captures/
mcp_picoscope/app.py             Desktop app: owns the scope, display, MCP over HTTP (8090)
mcp_picoscope/ui.py              Local web server + Edge launch (port 8071)
mcp_picoscope/ui.html            The page: trace, readouts, MCP activity
mcp_picoscope/backends/mock.py   Simulated signal source — the tests' ground truth
mcp_picoscope/backends/ps2000.py Real hardware via ps2000.dll. Verified against a PS2104.
tests/test_analysis.py           Measurements against the mock's known signals
tests/test_stdio.py              Smoke test over the real stdio transport (mock)
tests/test_hardware.py           Smoke test against a real scope; skipped without one
tests/test_ui.py                 Window rules, view memory, sweep engine
tests/test_page.py               node --check over the page's script
tools/step0_verify.py            Hardware identity: variant, ranges, timebases
tools/verify_volt_scale.py       The scale against a known voltage (MAX_ADC)
tools/verify_zero.py             Offset, shorted input
tools/verify_trigger.py          The edge trigger against a periodic signal
tools/trigger_stability.py       The same, measured through the display over HTTP
tools/measure_signal.py          One signal across several timebases
tools/ui_session.py              Holds a session open so the display stays live
packaging/build_exe.py           Builds dist/PicoScope.exe; every option explained
tests/test_app.py                The app's close detection
```

**Versions:** `SYSTEM_VERSION` in `mcp_picoscope/__init__.py` mirrors the latest
git tag, `deploy_version.txt` the deploy. Both are shown by `version_line()` —
in `get_server_info()` and in the display header.

## Run and test

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q          # everything
.\.venv\Scripts\python.exe tests\test_stdio.py         # smoke test, starts the server
.\.venv\Scripts\python.exe -m mcp_picoscope.server     # the server by hand (waits on stdio)
```

Register the server with Claude Code by copying `.mcp.json.example` to
`.mcp.json` and adjusting the paths.

## Pitfalls — read before changing anything

**The PS2104 is `ps2000`, not `ps2000a`.** The wrong API family answers "unit
not found", which looks exactly like broken hardware. This is the single most
important technical fact in the project.

**Step 0 is done (2026-09-12).** The device answers: variant `2104`, hardware 4,
driver 3.0.152.6217. Measured, not assumed: voltage ranges **100 mV–20 V** (it
rejects 20 mV and 50 mV), timebases 0–19 (20 ns–10.49 ms), **buffer depth 8092
samples**. Calibration is verified at both ends: the **scale** against a 1.5 V
cell (four ranges within 47 mV, `MAX_ADC = 32767` confirmed) and the **zero**
against a shorted input (worst offset 0.14 LSB). Frequency is verified against an
800 Hz sine (0.03 % error) and the edge trigger by the spread of the starting
point (31 % of Vpp free-running, 0.7 % armed).

**The driver is not found on its own.** `picosdk` resolves the DLL with
`ctypes.util.find_library`, which searches `PATH` on Windows — and nothing puts
Pico's directory there. `_ensure_dll_on_path()` in `backends/ps2000.py` does it,
with `PICOSDK_DIR` overriding. On the development machine `ps2000.dll` lives in
`PicoScope 7 T&M Stable\` rather than `SDK\lib`, because the application was
installed instead of the SDK; both work.

**One window for the whole machine — not one per process.** The hook sits in the
`tool()` decorator because every tool call already passes through it. A **recent
poll** from the page is the proof that a window is watching (`viewer_present()`,
6 s), and that same proof is written to `%TEMP%\mcp-picoscope-ui.json`, which
every server process reads (`window_claim()`). A variable inside one process was
not enough: two server processes — the registered server and a separate
`tools/ui_session.py` — each opened a window, and there is **one** PS2104. The
decision lives in `should_launch()` precisely so it can be tested without a
browser (`tests/test_ui.py`). `PICOSCOPE_UI_BROWSER=0` serves the page without
opening anything.

**The window opens in its own Edge profile** (`%TEMP%\picoscope-edge-profile`).
That costs a cold profile start and buys the only thing that matters: every
process using that directory is ours, so a leftover window can be closed
deterministically without touching the user's own browsing.
`close_stale_windows()` runs **before** every launch (we only get there with no
window watching, so anything still standing is a corpse) and in `stop()` when the
server exits — but only when the claim is ours, or it would be another session's
live window.

**The page cannot close itself.** Chromium refuses `window.close()` for a window
the script did not open, and an `--app` window is one of those — measured, not
assumed. The page therefore shows a clear "the server is gone" panel after ten
seconds without contact, but the server's sweep is the guarantee.

**The display remembers zoom, position and size** in that same file, not in
`localStorage` — that is per origin, and the port changes as soon as another
process already owns 8071. The frame offset (the difference between where we
asked Edge to place the window and where the content landed) is measured on the
first report after a launch; without it the window creeps one title bar down the
screen every time.

**The page is a control surface, under three rules.** It reads the session, and
may run the actions listed in `ui.CONTROLS` — today `autoset`, `trigger`,
`sweep`, `coupling`, `timebase`, `range`. Each one needs: **one implementation** in `control.py` that the MCP tool
uses too, **the session lock** taken there, and **a result that lands in the
session** so `picoscope://state` tells the truth afterwards. Never put anything
in that whitelist that drives the outside world.

**Two servers can hijack the same port on Windows.** `HTTPServer` sets
`allow_reuse_address`, and `SO_REUSEADDR` does not mean the same thing on Windows
as on Unix: there, a new socket can **take over** a live listener. Two sessions
both "owned" 8071 and connections landed on whichever won the race, so the window
showed another session's scope. `_Server.allow_reuse_address = False` makes the
bind fail honestly and the port scan moves on to 8072.

**CSS zoom: measure in one place.** `getBoundingClientRect()` is zoom-scaled,
`clientWidth`/`clientHeight` are not. Measuring the canvas with one and drawing
with the other stretches the trace and pushes its zero line off centre. `fit()`
measures once into `viewW`/`viewH` and `draw()` uses only those. For the same
reason the body height is set in script: `100vh` is computed *before* the zoom,
so at 70 % the page lays out 1/0.7 times too tall.

**A `<canvas>` in a flex column grows by itself.** Writing `canvas.height` sets
the element's *intrinsic* size, so a `flex: 1` canvas pushes the rest of the
column out on every redraw. It therefore sits `position: absolute` inside a
wrapper with `min-height: 0` and takes its size from there. The bug looked like
the readout row "disappearing" and the trace being clipped at the bottom.

**The mock imitates hardware deliberately.** The sample interval snaps to a 2^n
timebase, samples are quantised to 8 bits of the range, and a signal larger than
the range clips. That is not friction — it forces every caller to read the
**actual** sample rate off the capture instead of trusting the one it asked for.

**Frequency comes from level crossings, not an FFT.** A square wave puts most of
its energy in the harmonics and a slow signal may not fit two periods in the
window. Crossings handle both and give duty cycle for free. The level is the
**midpoint between min and max**, not the mean: a 20 % duty square has a mean far
from its own midpoint, and measured against that every such wave reads ~50 %.

**The sweep is a thread, and it has three obligations.** `SweepRunner` in
`control.py` captures on its own until stopped. It must be stoppable
(`threading.Event` + `join(5 s)`), it takes **the session lock per capture and
never across the loop** (otherwise MCP calls starve —
`test_the_lock_is_free_between_sweeps` catches that), and it must survive a
capture failing: **a trigger that never fires is a state, not a fault**, so the
loop reports and continues. `close_device` and server shutdown stop it first.

**The sweep mode maps onto the hardware trigger.** `auto` keeps the auto-trigger
rescue, `normal` clears it so the trigger must really fire — and arms an edge
trigger when the scope is free-running, or the button would do nothing in exactly
the state the device opens in. Neither touches the level or the direction; those
are the user's.

**The app is a second transport, not a second implementation.** `app.py`
imports the same `server` module and runs its tools over streamable HTTP, so
Claude and the window share one session, one lock and one USB handle. It
builds the Starlette app itself and keeps the `uvicorn.Server`, because
`MCPServer.run("streamable-http")` blocks with no way to stop it short of
killing the process around an open device.

**Closing the app, in order.** `ui.freeze()` first: closing the device goes
through a tool, every tool asks for a window, and with the user's window just
closed it got a new one — measured, a window flashed open a second after the
app was closed. Then stop the sweep, then close the device, then the display,
then HTTP. Streamable HTTP holds connections open and uvicorn waits for them by
default, so `timeout_graceful_shutdown` is set; without it the endpoint hung the
full stop timeout.

**Two things the build needed that are not obvious.** `--collect-submodules mcp`
imports every module in the SDK, including `mcp.cli`, which calls `sys.exit(1)`
at import when its optional `typer` dependency is missing and takes the build
down. And the Windows proactor event loop logs a full `ConnectionResetError`
traceback every time an HTTP client hangs up — twelve for one test session —
so `app.py` filters exactly that exception type on the `asyncio` logger and
nothing else.

**A manual timebase belongs to the user.** `SweepRunner._timebase_mode` is `auto` or `manual`; while manual, `_retune` and the widening leave the window alone and only re-check the aliasing warning. `set_time_per_div(None)` and autoset hand it back. This was the trap written into issue #1 — the following undoing a choice two seconds later — and `test_a_manual_timebase_is_not_overwritten_by_the_following` holds it shut.

**The aliasing warning never trusts the capture on screen.** A timebase too slow for the signal measures an alias: a wrong, lower frequency with seemingly plenty of samples per period. Measured on the hardware: 2 206 Hz from a 10 kHz signal at 20 ms/div. `_reference_hz` is updated only while the timebase follows the signal (or from autoset), and the warning is computed against that.

**Both scales are round, in auto too.** Autoset and the following used to label the screen 1.25 V/div and 524 µs/div — true and unreadable. `_clamp_window` rounds the window up to ten 1-2-5 divisions (up only: more periods is safe), and `session.volts_per_div` is a 1-2-5 screen scale on the narrowest range that covers it (`range_for_volts_per_div`). An explicit `range_v` clears it back to range/4. The page draws `screenV`, not the hardware range.

**A manual time/div draws exactly what the label says.** The driver delivers up to 1.7× the requested window, and the first version labelled that honestly — 131 µs/div after clicking to 100 µs/div — which made the buttons look broken. The page draws `min(duration, time_per_div × 10)`; the capture always covers the window.

**A manual capture may raise the alias reference, never lower it.** An alias reads lower than the signal, so a higher frequency with ≥ 10 samples per period is the signal itself. Without this the reference stayed at 10 kHz after the generator went to 1 MHz, and a 562 kHz alias at 0.2 ms/div raised no warning (`_raise_reference`).

**A screenshot is frozen at the click, not at Save.** The sweep keeps running while the name is typed, so the image is composed when the button is pressed. The page renders it (the canvas is what the user saw) and `POST /screenshot` only checks the PNG signature, cleans the name with `export.safe_name` and writes through `unique_path`. It is not in `CONTROLS`: it touches neither the scope nor the sweep.

**The capture note is decided in one place.** The alias warning was set, then wiped by the capture's empty note a few lines further down the same poll. Priority: timebase warning, sweep error, capture note.

**The header wraps rather than clips.** With `flex-wrap: nowrap` and `overflow: hidden`, the time/div controls ran off the right edge of a 941 px window — the default on a 300 %-scaled display. Passive labels (clock, version, device) are dropped first by breakpoints; a second row is the last resort.

**AC coupling has to settle.** It is a capacitor, and after switching to AC the midpoint drifts: measured +0.24 V at 1.08 s, +0.02 V at 1.25 s, settled by 1.42 s. The first version took one capture straight after the switch and put the trigger at 1.258 V on a signal centred at 0 V. `_settled_capture` repeats captures until the midpoint moves by no more than two ADC steps. The mock models the settling with the measured time constant (`AC_SETTLE_TAU_S`); it used to remove the DC instantly, which is how the bug passed every test.

**The captures that find a new trigger level must not wait for the old one.** After AC → DC in NORMAL mode, an edge trigger left at −0.02 V never fires on a 0..3 V signal, so the settling capture waited out its timeout and the switch failed. Those captures run free-running, and the user's mode is given back around the new level.

**Nothing may freeze the window while a trigger waits.** The driver needs the session lock for a whole capture, and a capture waiting on a trigger that never fires held it for 6 s — the display's state read waits on the same lock, so the window froze. The sweep now waits for a trigger in turns of `SWEEP_TRIGGER_WAIT_S` (`capture_block(max_wait_s=...)`), and `_ui_state` takes the lock with a timeout and serves the last frame marked `busy` rather than wait. Measured with an unreachable trigger: the longest state read 291 ms.

**The trigger marker is on the right, the 0 V marker on the left**, as on a bench scope. In AC coupling the trigger level lands near 0 V, where two markers on the same edge would sit on top of each other.

**Who owns the timebase.** A running sweep keeps its own window, so anything that picks a timebase has to hand it over or be undone 150 ms later — that is how autoset came to look broken. `SweepRunner.set_window(seconds, for_hz)` is the handover, and the retune compares **frequencies, not window lengths**: comparing lengths cannot tell "the signal changed" from "somebody deliberately chose a different number of periods", so a deliberate choice was overwritten on the next sweep.

**A sweep window can trap itself.** Too short a window holds fewer than two edges, so no frequency is measured, so the retune that would widen it never fires. Seen live: stuck at the 20 µs minimum with an 800 Hz signal on the probe — 0.066 of a period per capture. After `SWEEP_MISSES_BEFORE_WIDENING` empty sweeps the window widens by `SWEEP_WIDEN_FACTOR`; widening is safe, since a window that is too long only draws more periods while one that is too short shows nothing at all.

**Autoset hunts fast → slow, never the other way.** Too fast a timebase shows too
few edges and is rejected for saying nothing; too slow a one **aliases** and is
rejected for lying. The wrong direction reported an 11.8 kHz sine as a perfectly
steady 406 Hz. A frequency is believed only when the sample rate is at least
`AUTOSET_MIN_SAMPLES_PER_PERIOD` (10) times it.

**Noise is not a frequency.** The amplitude threshold (`MIN_SWING_FRAC`, 2 % of
the range) is not enough — on a narrow range noise clears it easily, and an
unconnected probe was once reported as "456 Hz". Periodicity decides:
`MAX_JITTER_PCT = 20` against a measured 0.06–0.71 % for real waveforms and
58–200 % for noise. With fewer than three periods there are no intervals to
compare (two edges give 0 % jitter by definition), and the shape measure
Vpp/stdev is used instead. The thresholds are **measured**, and the numbers are
in `analysis.py` — do not change them without measuring again.

**Decimation is min/max per bucket.** Every N-th sample drops the spikes, which
is precisely what one buys an oscilloscope to see.
`test_downsample_keeps_the_spike` fails the build if anyone simplifies it.

**An exception that reaches the MCP surface must become a `ToolError`.** The
`tool()` decorator in `server.py` does that. Anything else becomes "Error
executing tool X" in the session, which helps nobody who cannot see the screen.

**`mcp` 2.x, not 1.x.** `FastMCP` has been called `MCPServer` since 2.0
(`from mcp.server.mcpserver import MCPServer`). Use a dedicated virtualenv — do
not install into the global Python.

## Hardware

Built against a Dell XPS 15 9500, Windows 11, Python 3.13 64-bit.
PicoScope PS2104: 1 channel, 8 bits, no signal generator, 50 MS/s, an 8092
sample buffer, 100 mV–20 V. The driver came with **PicoScope 7 T&M** via winget
(`PicoTechnology.Picoscope.T&M`), so PicoSDK as a separate package is not
required — the application carries the same `ps2000.dll`. 64-bit, to match the
Python.
