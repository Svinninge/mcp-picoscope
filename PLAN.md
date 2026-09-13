# PLAN — an MCP server for the PicoScope 2104

The plan for building an MCP (Model Context Protocol) server that lets Claude
drive and read a PicoScope PS2104 USB oscilloscope.

Status: **v1, done and verified against hardware** (2026-09-12). Steps 0–6 are
complete and the definition of done in §7 is met: the device identifies itself as
variant 2104, the volt scale is measured against a 1.5 V cell, the zero against a
shorted input, and the frequency against an 800 Hz sine to 0.03 %. What remains
are extensions, not foundations: streaming (v2). The interactive control
surface in the display is complete
([issue #1](https://github.com/Svinninge/mcp-picoscope/issues/1)). See
[TODO.md](TODO.md) and the open issues.

---

## 1. Goal

An MCP server that exposes the oscilloscope as tools to an LLM client (Claude
Code / Claude Desktop), so one can say things like:

- "Connect to the scope and show me what is on channel A"
- "Trigger on a rising edge at 1.5 V and capture 10 ms"
- "What is the frequency and Vpp of this signal?"
- "Save the measurement as CSV and draw a PNG"

Non-goals (v1): multi-channel scopes, protocol decoding, real-time streaming to a
GUI, signal generation (the PS2104 has none).

---

## 2. Hardware assumptions — PS2104

The PS2104 belongs to the **older generation of the PicoScope 2000 series** and
uses the old `ps2000` driver — **not** `ps2000a`. This is the single most
important technical detail in the project; the wrong API gives "unit not found".

| Property | Value (verified against the device at step 0) |
|---|---|
| Channels | 1 (channel A) |
| Resolution | 8 bits |
| API / driver | `ps2000.dll` (legacy) |
| Connection | USB, USB-powered |
| Signal generator | none |

**To do in step 0:** verify bandwidth, maximum sample rate, buffer depth and
available voltage ranges against Pico's datasheet and against
`ps2000_get_unit_info()` on the actual device. Hardcode nothing the driver can be
asked about.

### Dependencies on the machine
- **`ps2000.dll` (64-bit)** is required. *Installed 2026-09-12* — but not through
  PicoSDK: `winget install PicoTechnology.Picoscope.T&M` installs the PicoScope 7
  application, which carries the same driver DLLs in its program directory. That
  is enough.
- The `picosdk` Python wrapper (Pico's official `picosdk-python-wrappers`), which
  binds to the DLL through ctypes.
- Python 3.13, 64-bit.

---

## 3. Architecture

```
Claude (MCP client)
        │  stdio, MCP protocol
        ▼
  mcp_picoscope/server.py        ← MCPServer, tool definitions
        │
        ▼
  mcp_picoscope/scope.py         ← abstract scope interface
        ├── backends/ps2000.py   ← real hardware through picosdk
        └── backends/mock.py     ← simulated signal source
        │
        ▼
  mcp_picoscope/analysis.py      ← Vpp, RMS, frequency, duty cycle
  mcp_picoscope/export.py        ← CSV / NPZ / PNG
        │
        ▼
  mcp_picoscope/control.py       ← actions and the sweep engine (shared by both surfaces)
        │
        ▼
  mcp_picoscope/ui.py            ← local display (HTTP 8071) + Edge window
  mcp_picoscope/ui.html          ← the page: trace, readouts, MCP activity
```

**Load-bearing design decisions**

1. **Mock backend first.** The whole server must be developable and testable
   without hardware. The mock generates sine/square/noise with known frequency
   and amplitude, so the analysis functions can be unit-tested against the truth.
2. **One owned session.** The driver is not thread safe and the device can only
   be opened by one process. The server holds a `ScopeSession` and serialises
   every call.
3. **Never raw sample arrays in a reply.** A block capture can be tens of
   thousands of points — that blows the context window. Tools return a summary
   (statistics, a decimated curve, a file path), never the whole array.
4. **Explicit state.** Channel and trigger settings are set with their own tools
   and can be read back, so the LLM can reason about the current state.
5. **The display reads, and may do a small enumerated set of things** *(revised
   2026-09-12, when the first button was needed)*. The original principle was
   that the page never drives. It held until the first button, and rather than
   bending it quietly, every action the page may perform now has to meet three
   requirements: **one implementation** (`control.py`, the same code the MCP tool
   runs — two copies would drift and the two surfaces would then disagree about
   what the scope is doing), **one lock** (`ScopeSession.lock`, so a click and a
   tool call cannot interleave in the middle of a sequence of captures), and **a
   visible result** (the change lands in the session, so `picoscope://state`
   tells the truth afterwards and the LLM is not reasoning about a range somebody
   else just changed). The whitelist is `ui.CONTROLS` — today `autoset`,
   `trigger`, `sweep`. Nothing that drives the outside world may ever go in it;
   the scope is a passive listener and the PS2104 has no generator. **A fourth
   requirement arrived with the sweep** (issue #1): whatever drives the
   acquisition must be stoppable, must take the lock per capture, and must
   survive a capture failing.

---

## 4. MCP tools (v1)

| Tool | Description |
|---|---|
| `list_devices()` | Look for connected scopes. Returns serial and model. |
| `open_device(backend="auto")` | Open the device. `auto` → ps2000, falling back to the mock when none is found (with a clear warning in the reply). |
| `close_device()` | Close and release the USB device. |
| `get_device_info()` | Model, serial, driver version, channels, voltage ranges. |
| `configure_channel(range_v, coupling, enabled)` | Set voltage range and AC/DC on channel A. |
| `configure_trigger(mode, threshold_v, direction, delay, auto_trigger_ms)` | Auto or single edge trigger. |
| `capture_block(duration_s, samples)` | Capture one block. Returns statistics + decimated curve + capture id. |
| `capture_streaming(duration_s, rate)` | Slow continuous acquisition to file. *(Not built — v2.)* |
| `measure(capture_id)` | Vpp, Vmin, Vmax, mean, RMS, frequency, period, duty cycle. |
| `export_capture(capture_id, format, name)` | `csv` \| `npz` \| `png`, optionally under a chosen name. Returns the path. |
| `start_sweep(mode, window_s)` / `stop_sweep()` | Continuous acquisition in its own thread: `auto`, `normal`, `single`. The lock is taken per capture, never across the loop. |
| `set_time_per_div(time_per_div_s)` | Manual timebase in seconds per division, `0` for auto. Warns when a manual timebase may alias. |
| `autoset()` | Tries voltage ranges and timebases until the signal fills the screen sensibly — what the "AutoSetup" button does. The ladder runs **fast → slow** and a frequency is believed only at ≥10 samples per period; the opposite direction produced an alias (11.8 kHz reported as 406 Hz). It also hands its window to a running sweep and sets the trigger level to half of peak-to-peak. Also a button in the display. |

**Resource:** `picoscope://state` — current configuration and the latest capture
as a readable resource.

---

## 5. Implementation — step by step

**Step 0 — Groundwork (hardware)** ✅ 2026-09-12
- Install the 64-bit driver. Verify that `ps2000.dll` exists.
- Plug in the PS2104, run Pico's own application and confirm it sees the device.
- Run a minimal Python script that opens the device and prints
  `ps2000_get_unit_info()`. **This is the gatekeeper** — if this does not work,
  nothing else matters.

**Step 1 — Skeleton** ✅ 2026-09-12
- `pyproject.toml`, package structure, the `mcp` dependency.
- A server that starts and exposes `list_devices` against the mock backend.
- Verify that Claude Code sees the server through `.mcp.json`.

**Step 2 — Mock backend + analysis** ✅ 2026-09-12
- A signal generator in the mock: sine, square, ramp, noise, with selectable
  frequency and amplitude.
- `analysis.py` with Vpp/RMS/frequency. Frequency from level crossings with
  hysteresis rather than an FFT peak — more robust for square waves and low
  frequencies.
- Unit tests that measure the mock's known signals and compare against the truth.

**Step 3 — The real ps2000 backend** ✅ 2026-09-12 (including the edge trigger)
- `open_unit`, `set_channel`, `set_trigger`, `get_timebase`, `run_block`,
  `ready` polling, `get_values`.
- ADC counts → volts through `max_adc` scaling per voltage range.
- Timebase selection: pick the fastest timebase that covers the requested
  `duration_s` with the requested sample count; report the actual sample rate
  back (it is rarely exactly what was asked for).

**Step 3b — A timebase choice that holds** ✅ 2026-09-12 *(found in use)*
- `_select_timebase` picks the fastest timebase that covers the requested
  duration; the device reports the actual rate back.
- `autoset` hunts across a ladder of window lengths, fast → slow, and demands
  ≥10 samples per period before believing a frequency. Verified 50 Hz–1 MHz in
  the mock (0.03 %) and 11.8 kHz on hardware (530 samples per period).
- The sweep sets its window from the measured frequency, about ten periods.

**Step 4 — Export and presentation** ✅ 2026-09-12
- CSV (time, volts), NPZ for further analysis, PNG through matplotlib.
- Decimation for the reply: min/max per bucket, not every N-th point — otherwise
  spikes disappear.

**Step 5 — Robustness** ✅ 2026-09-12
- Clear errors: device busy, USB removed mid-capture, overrange (the signal clips
  against the range limit → suggest a larger range).
- A timeout on a trigger that never fires.
- Clean up the USB handle on shutdown.

**Step 6b — Live display** ✅ 2026-09-12 *(arrived during the build; it was not in
the original plan)*
- A local HTTP server inside the server process, a page on `127.0.0.1:8071`,
  opened in an Edge app window on the **first** tool call.
- The hook sits in the `tool()` decorator — the one point every call passes.
- Draws the trace on a graticule with V/div and ms/div, plus readouts, channel
  and trigger settings, a version banner and a log of MCP calls. Refreshes every
  400 ms.
- Simulated captures are marked **SIMULATED** in orange, so a mock can never be
  mistaken for a measurement.
- `PICOSCOPE_UI=0` turns it off (the tests set it), `PICOSCOPE_UI_PORT` moves it.
- **One window per machine**, not per process: the proof that a window is
  watching is written to a shared file in the temp directory that every server
  process reads. The same file remembers zoom, position and size for the next
  launch.
- Trigger and sweep controls: Run / Normal / Single / Stop, and a trigger level
  dragged with the mouse (issue #1).
- AC/DC coupling and a 0 V marker.
- time/div: a 1-2-5 sequence with an explicit auto/manual owner and a warning
  when a manual timebase may alias (issue #1, now complete).

**Step 7 — The desktop app** ✅ 2026-09-13 *(requested after v1)*
- `PicoScope.exe`: one process owns the scope, shows the display for manual
  measurements, and serves the MCP tools over streamable HTTP on 8090.
- Closing the window ends everything. Detected from the page going quiet, using
  the same poll heartbeat that already decides whether a window is watching.
- Built with PyInstaller (`packaging/build_exe.py`), about 50 MB; the driver DLL
  is not bundled.
- Verified on the frozen exe against the hardware: page and version served from
  the bundle, 15 tools over HTTP, a client measuring through the app, and on
  closing the window exit code 0, no process left and the device free to open.

**Step 6 — Documentation** ✅ 2026-09-12
- README covering driver installation, an `.mcp.json` example and a sample
  dialogue.

---

## 6. Risks

| Risk | Handling |
|---|---|
| The wrong driver family (`ps2000a` instead of `ps2000`) | ~~Step 0 verifies~~ — verified 2026-09-12: `ps2000` answers, variant "2104". |
| PicoSDK missing on the machine | ~~The mock backend makes development possible anyway~~ — solved 2026-09-12: the PicoScope 7 application carries `ps2000.dll`, and `_ensure_dll_on_path()` finds it. |
| A 32/64-bit DLL mismatch with Python | Use the 64-bit driver with 64-bit Python. Checked in step 0. |
| Large datasets blowing the LLM context | Tools never return raw arrays — principle 3 above. |
| The driver is not thread safe | One session, serialised calls. |

---

## 7. Definition of done (v1)

- [x] `open_device` finds and opens a real PS2104. *(2026-09-12, variant 2104)*
- [x] `capture_block` on a known signal gives the right frequency to ±1 %.
      *(2026-09-12: function generator, 800 Hz sine, amplitude 3.0 V. Measured
      799.37–800.20 Hz across five window lengths from 2 to 200 ms — **0.03 %**
      error on the longer ones, 0.09 % spread. Vpp read 3.02 V against 3.0 V,
      inside one ADC step on the ±5 V range. Tool: `tools/measure_signal.py`.)*
- [x] `export_capture` produces a PNG that looks right to the eye.
- [x] The whole tool set works against the mock backend without hardware.
- [x] The README is enough to set the server up on a new machine.
- [ ] Tagged `v0.01` per the versioning rules.

---

## 7b. Environment variables

| Variable | Default | Effect |
|---|---|---|
| `CAPTURE_DIR` | `./captures` | Where exported files land. |
| `PICOSCOPE_UI` | `1` | `0` turns off the display and the Edge window. |
| `PICOSCOPE_UI_PORT` | `8071` | Start port; ten are tried upwards. |
| `PICOSCOPE_UI_BROWSER` | `1` | `0` serves the page but never opens a window. |
| `PICOSDK_DIR` | *(auto)* | The directory holding `ps2000.dll` when the search misses. |
| `PICOSCOPE_MCP_PORT` | `8090` | Where the desktop app serves MCP over HTTP. |

---

## 8. Open questions

1. Should the server run over `stdio` (local, simplest) or also be exposed over
   the network so the scope can sit on another machine?
   **Decided 2026-09-12: stdio in v1.**
2. Is continuous streaming needed in v1, or is block capture enough?
   **Decided 2026-09-12: block capture in v1, streaming in v2.**
3. Where should exported files go? **Decided 2026-09-12: `./captures/` in the
   project root, configurable through `CAPTURE_DIR`.**
