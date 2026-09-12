# LESSONS — mcp-picoscope

Lessons from mistakes and corrections, newest first. Read at session start after
[SOUL.md](SOUL.md). When a decision resembles a lesson, quote the rule explicitly
(`"Per LESSONS YYYY-MM-DD: ..."`).

Format: a heading with a date, then one or two sentences — the rule, and the
context that produced it.

---

## 2026-09-12 — A hardware fix must be measured in isolation

Seven display windows had accumulated: each test session opened one, and stopping
the process killed the server but left the window on screen with a frozen
measurement. The first fix — `window.close()` in the page — looked like it worked
in a check that killed the whole process tree, but **measured in isolation,
Chromium refuses** to close a window the script did not open; Windows job objects
had taken the window down in that first check. Whoever opens a window is the only
one who can close it, so the display now runs in **its own Edge profile** and its
processes can be identified and ended without touching the user's browsing.
**Always measure a fix in isolation** — the first check proved the wrong thing.

## 2026-09-12 — Never write Windows paths with backslash escapes in a generated script

`"tools\verify_zero.py"` in a non-raw Python string became `tools` + a vertical
tab + `erify_zero.py` in the README — a documented command that could not be run.
Worse, **the repair failed silently** several times: the search string carried the
same escape problem, so `replace()` matched nothing while `in` appeared to find
it. Build such lines from `chr()` or raw strings, or rewrite the whole line rather
than patching it, and **always read the file back** to confirm the change landed.

## 2026-09-12 — A hardware test allowed to fall back to the mock tests nothing

`open_device(backend="auto")` falls back to the mock when no hardware is present —
convenient in use, worthless in a test. `tests/test_hardware.py` therefore asks
for `backend="ps2000"` explicitly and verifies the variant string is "2104". The
first run immediately raised an `AttributeError`: `_read_info` called a
`_timebase_limits` that had never been written. With the auto fallback the test
would have passed green.

## 2026-09-12 — `find_library` searches PATH, and nobody puts Pico there

`picosdk` loads the DLL through `ctypes.util.find_library`, which on Windows
searches `PATH`. Neither the PicoSDK installation nor the PicoScope application
adds its directory there, so the import fails on a machine where the driver is
installed and working. Find the directory in code rather than prepending `PATH` by
hand in a shell — the manual fix does not travel to the next session.

## 2026-09-12 — "Connected" does not mean "available"

The PS2104 was plugged in, but `Get-PnpDevice` showed `Status: Error` for
`VID_0CE9&PID_1007`: the device was powered and enumerated but had no driver,
because PicoSDK was not installed. **Check the device's status in Windows before
drawing conclusions about the DLL call** — a `unit not found` from `ps2000` can
mean "no driver" just as easily as "wrong API family", and the two have completely
different remedies.

## 2026-09-12 — `ps2000`, not `ps2000a`

The PS2104 belongs to the older generation of the 2000 series and is driven by
`ps2000.dll`. The `ps2000a` family answers `unit not found` on a PS2104 and the
error looks like broken hardware. Look the model up in Pico's wrapper repository
before choosing an API family — never guess from the digits in the model number.
