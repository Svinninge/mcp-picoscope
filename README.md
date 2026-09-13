# mcp-picoscope

An MCP server that lets Claude drive and read a **PicoScope PS2104** USB
oscilloscope.

> **Status: v1, verified against real hardware** (2026-09-12). The whole chain —
> open → configure → capture → measure → export — works against the instrument.
> The volt scale is measured against a 1.5 V cell, the zero against a shorted
> input, and the frequency against an 800 Hz sine to within 0.03 %. The edge
> trigger is verified too: the spread of the starting point falls from 31 % of
> Vpp to 0.7 % when it is armed. Remaining: the timebase control in the display
> (issue #1) and streaming (v2).

## The idea

Expose the oscilloscope as MCP tools, so you can say:

- "Connect to the scope and show me what is on channel A"
- "Trigger on a rising edge at 1.5 V and capture 10 ms"
- "What is the frequency and Vpp of this signal?"
- "Save the measurement as CSV and draw a PNG"

The server never answers with raw samples. A capture is tens of thousands of
points; you get statistics, a decimated curve (min/max per bucket, so spikes
survive) and a path to the file.

## Requirements

| Requirement | Note |
|---|---|
| Python 3.11+ | 64-bit, and it must match the driver's bitness |
| PicoScope PS2104 | 1 channel, 8 bits, legacy `ps2000` driver (**not** `ps2000a`) |
| `ps2000.dll` 64-bit | Ships with the PicoScope application (`winget install PicoTechnology.Picoscope.T&M`) or with PicoSDK |
| `picosdk` | Pico's official Python wrappers, `pip install picosdk` |

The last two are only needed for real hardware. **A mock backend makes the whole
server runnable and testable without a scope.**

## Installation

```powershell
git clone https://github.com/Svinninge/mcp-picoscope.git
cd mcp-picoscope
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

Use a dedicated virtualenv rather than the global Python: `mcp` 2.x pulls in a
newer starlette than some other tooling tolerates.

### Registering with Claude Code

Copy [.mcp.json.example](.mcp.json.example) to `.mcp.json` and adjust the paths
to your checkout, or register the server at user scope:

```powershell
claude mcp add --scope user picoscope -- <checkout>\.venv\Scripts\python.exe -m mcp_picoscope.server
```

### Hardware (step 0)

1. Install the driver. Easiest via winget:
   `winget install PicoTechnology.Picoscope.T&M` — the application carries
   `ps2000.dll`. The 64-bit PicoSDK from Pico Technology works just as well.
2. Plug in the PS2104. Without a driver it shows as `Status: Error` in Device
   Manager; with one, as `PicoScope 2000 series PC Oscilloscope`.
3. `pip install picosdk`.
4. `.\.venv\Scripts\python.exe tools\step0_verify.py` prints the variant, the
   serial, the voltage ranges the device accepts and the whole timebase table.
5. `open_device(backend="ps2000")` should now report model and serial.

The server finds the DLL itself (`_ensure_dll_on_path`). If it lives somewhere
unusual, point `PICOSDK_DIR` at the directory holding it.

Close the PicoScope application before using the server — the device can only be
opened by one process at a time.

## The desktop app

`PicoScope.exe` is the scope as an application, for manual measurements. It opens
the instrument, runs an autoset, starts sweeping and shows the display. **Close
the window and everything goes with it:** the sweep stops, the USB handle is
released, and the process exits.

While it is open it also **serves the same MCP tools over HTTP**, so Claude can
drive the very same instrument. That is not a convenience but a consequence of
the hardware: the PS2104 opens in exactly one process. An app that owned the
device while Claude started a server of its own would leave them fighting over
it. One owner, two ways in.

```powershell
.\.venv\Scripts\python.exe -m pip install -e .[build]
.\.venv\Scripts\python.exe packaging\build_exe.py     # → dist\PicoScope.exe, about 50 MB
```

To let Claude use the app, copy [.mcp.json.app.example](.mcp.json.app.example) to
`.mcp.json` — it points at `http://127.0.0.1:8090/mcp` instead of starting a
server. Use one example or the other, not both.

- The driver is **not** bundled. `ps2000.dll` belongs to the PicoScope or
  PicoSDK installation, and the app finds it there.
- Without a console, the app writes its log to
  `%LOCALAPPDATA%\mcp-picoscope\app.log`. Look there when it will not start.
- Closing is detected from the page going quiet, so the process ends about ten
  seconds after the window does. That is deliberate: a window merely throttled
  in the background must not switch the instrument off under you.
- `PICOSCOPE_MCP_PORT` moves the endpoint off 8090.

## Tools

| Tool | Description |
|---|---|
| `list_devices()` | Look for connected scopes. The mock is always listed. |
| `open_device(backend)` | `auto` \| `ps2000` \| `mock`. `auto` falls back to the mock, saying so loudly. |
| `close_device()` | Close and release the USB device. |
| `get_device_info()` | Model, serial, driver, channels, voltage ranges, limits. |
| `get_server_info()` | Version (`System vX.YY \| Deploy vX.YY`), capture directory and the display URL. |
| `configure_channel(range_v, coupling, enabled)` | Channel A. The device snaps to the nearest range and reports which. |
| `configure_trigger(mode, threshold_v, direction, delay_pct, auto_trigger_ms)` | Auto or edge trigger. |
| `configure_mock_signal(...)` | What the mock generates: sine, square, ramp, triangle, noise, dc. |
| `capture_block(duration_s, samples)` | Capture one block → statistics + decimated curve + capture id. |
| `measure(capture_id)` | Vpp, Vmin/Vmax, mean, RMS, frequency, period, duty cycle. |
| `export_capture(capture_id, format, name)` | `csv` \| `npz` \| `png` → path. `name` is optional; it is cleaned of path characters and never overwrites. |
| `autoset()` | Picks a range and timebase that show the signal, and puts the trigger level at half of peak-to-peak. Hunts across timebases fast → slow, so anything from 50 Hz to 1 MHz is found. |
| `start_sweep(mode, window_s)` | Capture continuously. `auto` sweeps regardless of the trigger, `normal` only on a real trigger, `single` once. |
| `stop_sweep()` | Stop the sweep. |
| `set_time_per_div(time_per_div_s)` | The timebase in seconds per division, or `0` for auto. A manual timebase stays until changed, and the reply warns when it is too slow to resolve the signal. |
| `open_ui(force)` | Show the display; reuses the window already watching. `force=true` opens another. |

**Resource:** `picoscope://state` — backend, device, channel, trigger, sweep and
held captures.

Exports land in `./captures/`, configurable with the `CAPTURE_DIR` environment
variable.

## The display

The first time a tool is called, the server starts a local page on
`http://127.0.0.1:8071/` and opens it in an **Edge app window**. It draws the
trace the way an oscilloscope does — graticule, V/div, ms/div — plus the
measurements, the channel and trigger settings, and a log of which MCP tools
have run. It refreshes every 400 ms.

### Trigger and sweep

The buttons **Run · Normal · Single · Stop** are the three positions on a bench
scope: `Run` sweeps whether or not the trigger fires, `Normal` only on a real
trigger, and `Single` captures once and stops. **The trigger level is dragged
with the mouse** on the trace. The line is always drawn, dimmed when the trigger
is not armed, and dragging it is what arms it. The arrow button toggles rising
and falling edge.

**time/div.** `◀` and `▶` step a 1-2-5 sequence from 10 µs to 20 ms per division; `Auto` hands the timebase back. The screen spans exactly ten of the chosen divisions: the driver only has power-of-two timebases and captures up to 1.7× more than asked, and the extra is not drawn. The label turns orange while the timebase is manual, because that is the one state in which the scope will not adapt if the signal changes. Autoset also takes the timebase back.

A timebase that is too slow for the signal does not fail; it **aliases**, and an alias looks entirely plausible. Stepped up to 20 ms/div on a 10 kHz square wave, the scope measured a steady 2 206 Hz. So a manual timebase that leaves fewer than ten samples per period warns under the trace, and the warning is computed from the last frequency measured on a timebase chosen to resolve it — never from the capture on screen, which may be the alias itself.

**Screenshot.** Freezes the screen at the click and asks for a name, pre-filled with `capture-YYYYMMDD-HHMMSS`; Enter saves, Esc cancels. The image is the trace as shown plus a line of settings (V/div, time/div, range, coupling, trigger) and the readouts, so it explains itself. It lands in `captures/` — the name cannot reach outside it, and an existing file gets a `-2` suffix rather than being overwritten. The sweep keeps running.

**volt/div.** `▼` and `▲` step through the device's own ranges (±100 mV to ±20 V, shown as 25 mV/div to 5 V/div over eight divisions). There is no gain between ranges, so there is no 1-2-5 sequence to pretend to.

**AC / DC and the 0 V marker.** The green pointer at the left edge is 0 V, as on a bench scope; the trigger marker sits at the right edge. In **DC** the trace shows the true voltage, so a 0..3 V signal floats above the marker — that is correct, not an offset error. **AC** removes the DC level and centres the signal on 0 V. On a PS2104 that is also the only way to gain resolution on an offset signal: the ps2000 driver has no analogue offset, so the range cannot follow the signal up. A 0..3 V sine on ±5 V uses about 30 % of the ADC; AC coupled on ±2 V it uses 75 %. The price is the DC level itself, and square waves grow sloping tops through the coupling capacitor — which is why autoset never switches to AC on its own. Switching coupling moves the trigger level to the signal's new midpoint, once the capacitor has settled.

A trigger that never fires is a **state, not a fault**: the sweep keeps waiting
and prints why below the trace instead of stopping.

The server owns the thread that captures; the page only asks. The level is sent
when you **release** — a capture per pixel of travel would queue behind the
session lock and the scope would lag the line by seconds.

The page also has an **Autoset** button that runs exactly the same code as the
MCP tool, under the same lock — the result appears in the activity log, so you
and Claude can each see what the other did. Beyond that the page only reads; it
cannot set the channel, and nothing that drives the outside world will ever be
added to it. A simulated signal is flagged with an orange **SIMULATED** badge so
a mock cannot be mistaken for a measurement.

**One window, for the whole machine.** There is a single PS2104 on the bench, so
a second window would claim a second instrument exists. The page polling is the
proof that a window is watching, and that proof is written to a shared file
(`%TEMP%\mcp-picoscope-ui.json`) which **every** server process reads before
launching anything. Close the window and the polls stop, the claim goes stale
within six seconds, and the next tool call brings it back. `open_ui(force=true)`
is the only way past the rule.

**The window is cleaned up.** The display runs in its own Edge profile, so a
window whose server has exited can be closed without touching your normal
browsing. That happens on server shutdown, and again before any new launch as a
safety net. (The page cannot close itself — Chromium refuses `window.close()`
for a window the script did not open — so it shows a "the server is gone" panel
if it is left alone anyway.)

**The window remembers itself.** The same file carries zoom, position and size
to the next launch. It also measures the window frame — the difference between
where we asked Edge to put the window and where the content landed — so the
window does not creep one title bar down the screen every time.

**Scalable.** The −/100 %/+ buttons zoom the whole page (40–200 %) and the choice
is remembered — useful on a 300 %-scaled Windows display, where a "normal"
window fills the screen. The layout collapses with the window and drops the
least valuable content first: the activity log, then the settings panel, then the
secondary readouts. The trace and its V/div are the last to go, because a curve
without its scale is not a measurement.

| Environment variable | Effect |
|---|---|
| `PICOSCOPE_UI=0` | No server, no window. Set this for unattended runs. |
| `PICOSCOPE_UI_PORT` | A start port other than 8071 (ten are tried upwards). |
| `PICOSCOPE_UI_BROWSER=0` | Serve the page but never open a window — for your own tab, or for tests. |

### The timebase follows the signal

A fixed window cannot work: 20 ms of an 800 Hz sine is 16 periods and reads
nicely, while the same 20 ms of 11.8 kHz is 248 periods and draws as a solid
block — three pixels per period, which neither this display nor a bench scope
can resolve. The sweep therefore sets the window from the measured frequency, at
about ten periods, and retunes only when it is off by more than 1.5× so the
timebase does not twitch on the last digit.

**Autoset owns what it sets.** It hands its window to a running sweep and puts the trigger level at half of peak-to-peak — the midpoint of the signal, where the slope is steepest and a trigger is steadiest. Zero would be the wrong default for anything with an offset: a 0..3 V signal never crosses it. The trigger mode you had is restored afterwards; only the level moves.

**Autoset hunts fast → slow.** Too fast a window shows too few edges and is
rejected for saying nothing; too slow a window **aliases** and is rejected for
lying — the wrong direction once reported an 11.8 kHz sine as a steady 406 Hz. A
frequency is believed only when the sample rate is at least ten times it.

The display lives exactly as long as the MCP server does. To watch the scope
without a Claude session running, hold a session open yourself:

```powershell
.\.venv\Scripts\python.exe tools\ui_session.py 3600 auto
```

## Example dialogue

> **You:** Open the scope and run an autoset.
>
> **Claude:** *(open_device → autoset)* Device open. Autoset surveyed on ±20 V
> over 0.02 and 0.2 ms, measured 11.807 kHz, picked ±5 V and captured five
> periods: Vpp 3.02 V, duty 49.8 %, RMS 1.78 V.
>
> **You:** Save it as a PNG.
>
> **Claude:** *(export_capture)* `captures\cap0002.png`

## Development

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q        # everything (the hardware test skips without a scope)
.\.venv\Scripts\python.exe tests\test_stdio.py       # smoke test against the mock, with output
.\.venv\Scripts\python.exe tests\test_hardware.py    # smoke test against a real scope
```

The mock carries the ground truth: the analysis functions are tested against
signals of known frequency, amplitude and duty cycle. The hardware test asks for
`backend="ps2000"` explicitly — it must not fall back to the mock, or a broken
driver path would pass as green.

### Verification tools

Against real hardware, each runnable on its own:

```powershell
.\.venv\Scripts\python.exe tools\step0_verify.py        # variant, ranges, timebase table
.\.venv\Scripts\python.exe tools\verify_volt_scale.py   # the scale against a known voltage
.\.venv\Scripts\python.exe tools\verify_zero.py         # offset, shorted input
.\.venv\Scripts\python.exe tools\verify_trigger.py      # the edge trigger against a periodic signal
.\.venv\Scripts\python.exe tools\trigger_stability.py   # the same, measured through the display
```

Working rules and pitfalls: [SOUL.md](SOUL.md), [CLAUDE.md](CLAUDE.md),
[LESSONS.md](LESSONS.md). Backlog: [TODO.md](TODO.md).
