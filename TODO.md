# Work log — mcp-picoscope

Open work lives in [GitHub Issues](https://github.com/Svinninge/mcp-picoscope/issues).
This file is the other half: what was actually done, and why — the reasoning that
a closed issue loses and a commit message only half carries.

Newest first.

---

## 2026-09-13

**AC/DC coupling and a 0 V marker in the display.** Asked as "shouldn't the
trace be centred on 0 V?" — no: in DC coupling the trace shows the true voltage,
exactly as a bench scope does. What was missing was a marker saying where zero
is, and an AC button to centre the signal on purpose. The PS2104 has no analogue
offset (checked in the driver), so AC is also its only way to put more ADC steps
across an offset signal.

Three faults, each found on the hardware rather than in the tests. The trigger
level was taken from a capture made before AC coupling settled: 1.258 V on a
signal centred at 0 V. The captures that find the new level waited for the old
one, so AC → DC in NORMAL mode failed outright. And a capture waiting for a
trigger held the session lock for 6 s, freezing the whole window. After the
fixes, the user's own AC click on a 10 kHz signal set the level to −0.0399 V
against a midpoint of −0.0399 V, and with an unreachable trigger the display's
slowest state read was 291 ms.

**A Windows desktop app, `PicoScope.exe`.** The scope as an application for
manual measurements: it opens the instrument, autosets, sweeps and shows the
display, and closing the window releases everything. It also serves the MCP
tools over HTTP, because the PS2104 opens in one process only — an app owning
it while Claude started a server of its own would have them fight over it.

Three faults found by running it rather than reading it. Shutdown took over
20 s: the close threshold was counted on top of the 6 s grace the viewer check
already gives, and uvicorn waited politely on connections streamable HTTP holds
open. A window flashed open after the user closed the app, because the teardown
closed the device through a tool and every tool asks for a window. And the log
filled with Windows proactor `ConnectionResetError` tracebacks whenever a client
hung up. Then the build: collecting all of the MCP SDK imports `mcp.cli`, which
exits the process when `typer` is missing.

Verified on the frozen exe: exit code 0, no process left behind, and the device
free for the next process to open.

## 2026-09-12

**The display and the tools translated to English**, ahead of making the
repository public. Three faults surfaced while checking the result.

**Autoset looked broken because the sweep undid it.** A running sweep keeps
its own window; autoset picked a good one and the next sweep replaced it a
tenth of a second later. Autoset now hands its window over, and the retune
compares frequencies rather than window lengths — comparing lengths cannot
tell a changed signal from a deliberate choice.

**A sweep window could trap itself.** Too short to hold two edges means no
frequency, and no frequency means nothing widens it again. Found stuck at the
20 µs minimum with an 800 Hz signal present — 0.066 of a period per capture.
The window now widens after a few empty sweeps.

**Autoset now sets the trigger level to half of peak-to-peak**, the midpoint
of the signal, and restores the trigger mode it found. Zero is the wrong
default for anything with an offset: the bench signal sits at 0..3 V and a
trigger at zero would never fire.

**Trigger and sweep in the display** (issue #1, partly). A sweep engine in
`control.py` with its own thread: `auto`, `normal`, `single`, stoppable, the lock
taken per capture. The trigger level is dragged with the mouse on the canvas and
sent on release; the line is always drawn, dimmed when the trigger is not armed.
`normal` arms an edge trigger when the scope is free-running — without that the
button did nothing in exactly the state the device opens in. A trigger that never
fires is reported as a state, not a fault. `tools/ui_session.py` no longer drives
captures of its own; the server owns the acquisition. Remaining from the issue:
time/div.

**Autoset sees the whole frequency range, and the display follows the signal.**
Reported as "cannot show 11.8 kHz resolved". The measurement was right (11.800
kHz), but autoset's survey ran at 41 kS/s — 3.5 samples per period — and answered
"no periodic signal". The first fix made it worse: a ladder running slow to fast
found an **alias** at 406 Hz that looked perfectly stable. The ladder now runs
fast → slow and believes a frequency only when the sample rate is ≥10× it. Mock,
50 Hz–1 MHz: every decade within 0.03 %. Hardware: 11 807 Hz, 530 samples per
period. The capture window now follows the measured frequency (~10 periods)
instead of a fixed 20 ms, which drew 248 periods as a solid green block.

**The edge trigger verified against hardware.** The last unproven hardware path.
Measured on the 800 Hz sine as the spread of the starting point over 12 captures:
free-running ±0.962 V (31.4 % of Vpp, 6/12 rising), rising edge ±0.020 V (0.7 %,
12/12 rising), falling edge ±0.000 V (0/12 rising). Both failure paths too: a
timeout with a readable message at an impossible level, and `auto_trigger_ms` as
the rescue. `tools/verify_trigger.py`.

**An Autoset button in the display.** The first action the page was allowed to
perform, and it set the pattern for issue #1: `control.py` carries the
implementation that both the MCP tool and the page call, the lock is taken there,
and the result lands in the session. The whitelist is `ui.CONTROLS`. `server.py`
got thinner as a side effect.

**Definition of done: frequency to ±1 % met.** Function generator, 800 Hz sine,
amplitude 3.0 V: measured 799.37–800.20 Hz across windows from 2 to 200 ms,
**0.03 %** error on the longer ones and 0.09 % spread. Vpp 3.02 V against 3.0 V —
inside one ADC step (39 mV on ±5 V). `tools/measure_signal.py` repeats it.

**Noise is no longer reported as a frequency.** A measured threshold rather than
a guessed one: real waveforms (sine, square, ramp, triangle, and a sine under
10 % noise) sit at 0.06–0.71 % period jitter; pure noise at 58–73 % in the mock
and 66–200 % on an unconnected PS2104 probe. The gate was set at 20 %, in the
empty middle. The few-cycle case — two edges give one interval and therefore 0 %
jitter by definition, which produced "3756 Hz" from noise — is caught by the
shape measure Vpp/stdev (2.0 square, 2.8 sine, 3.5 ramp, 5–7 noise).

**Windows no longer outlive their server.** Seven had accumulated: each test
session opened one, and the process dying left it on screen with a frozen
measurement. The display now runs in its own Edge profile and
`close_stale_windows()` closes leftovers — on server shutdown and before every
launch. `window.close()` in the page is not enough: Chromium refuses it for a
window the script did not open (measured).

**One window per machine, and it remembers where it was.** Two server processes
each opened a window although there is a single PS2104 — the guard was per
process. The proof that a window is watching is now written to
`%TEMP%/mcp-picoscope-ui.json`, which every process reads (`should_launch()`,
tested in `tests/test_ui.py`). The same file carries zoom, position and size, with
the window frame measured so the window does not creep down the screen on every
launch. `PICOSCOPE_UI_BROWSER=0` serves without opening anything.

**Rounded numbers in everything leaving `analysis.py`.** Statistics to 6
significant digits, curve points to 5. A reply with hardware-shaped values
(`adc/32767`) fell from 7 593 to 4 716 characters — **38 % smaller**, the curve
alone 40 %. An 8-bit ADC resolves one part in 256; eleven digits was precision the
instrument does not have, paid for in the caller's context window.

**The display made scalable.** Zoom buttons (40–200 %, remembered in the browser)
plus breakpoints that collapse the layout down to ~320×260. Window launching is
governed by whether the page has polled in the last 6 seconds rather than by a
process flag — measured at 67 tool calls → 1 window.

**A live display in Edge.** `ui.py` + `ui.html`: a local server on 8071, the trace
on a graticule with V/div, readouts, channel and trigger, and a log of MCP calls.
Opens automatically in an Edge window on the first tool call. `PICOSCOPE_UI=0`
turns it off. Verified against a 1 kHz mock signal: 1.0002 kHz and 50.0 % duty on
screen.

**The zero point verified.** Shorted input, all eight ranges: worst offset 0.14
LSB, under the resolution. The noise floor on ±0.1 V is 0.99 mV Vpp, about 1.3
LSB. `tools/verify_zero.py`.

**The volt scale verified.** `MAX_ADC = 32767` measured against a 1.5 V alkaline
cell: ±2/5/10/20 V read 1.6007 / 1.6120 / 1.5931 / 1.6399 V — agreeing within
47 mV, and within 7 % of the cell's nominal in absolute terms.
`tools/verify_volt_scale.py`.

**Step 3 verified against the real device.** `backends/ps2000.py` opens,
configures, captures and exports through the MCP server. `_ensure_dll_on_path()`
added — without it `picosdk` does not find the driver. `tests/test_hardware.py`,
which refuses the mock fallback, caught a missing `_timebase_limits`; fixed.

**Step 0 PASSED.** PicoScope 7 T&M installed through winget (it carries
`ps2000.dll`; a separate PicoSDK was not needed). The device went from
`Status: Error` to `OK` and answers: variant 2104, hardware 4, driver
3.0.152.6217. Measured: ranges 100 mV–20 V (20/50 mV rejected), timebases 0–19 =
20 ns–10.49 ms, an 8092 sample buffer.

**Steps 1–2 and 4–5 built against the mock backend.** Package structure,
`ScopeSession` with its lock, a mock that imitates timebase snapping, 8-bit
quantisation and clipping, `analysis.py` (hysteresis crossings, midpoint as the
level), `export.py` (CSV/NPZ/PNG), twelve MCP tools and the `picoscope://state`
resource. Unit tests against the mock's ground truth plus a smoke test over the
real stdio transport, all green.

**Working rules inherited from an earlier project.** SOUL.md, CLAUDE.md,
LESSONS.md and this log, adapted for a hardware-facing MCP server.
