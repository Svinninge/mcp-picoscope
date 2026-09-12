# File version: v0.01
"""PLAN.md step 0 — the gatekeeper. Talk to the real PS2104 or stop.

Run:  .\.venv\Scripts\python.exe tools\step0_verify.py

Checks, in the order where each one makes the next meaningful:
  1. picosdk wrapper imports and ps2000.dll loads (bitness, SDK present)
  2. ps2000_open_unit returns a handle
  3. get_unit_info: driver, hardware, variant, serial, calibration date
  4. which voltage ranges channel A actually accepts
  5. get_timebase across the range: intervals and max samples
  6. one real block capture on the largest range, free running

Prints what it found; asserts nothing about values we have not measured yet.
"""

from __future__ import annotations

import ctypes
import sys
import time

import numpy as np

INFO_LINES = {
    0: "driver version",
    1: "USB version",
    2: "hardware version",
    3: "variant info",
    4: "batch and serial",
    5: "calibration date",
    6: "error code",
    7: "kernel driver version",
}

RANGE_ENUM = {
    0.02: 1, 0.05: 2, 0.1: 3, 0.2: 4, 0.5: 5,
    1.0: 6, 2.0: 7, 5.0: 8, 10.0: 9, 20.0: 10,
}

CHANNEL_A = 0
OVERSAMPLE = 1
SAMPLES = 4096


def info(lib, handle: int, line: int) -> str:
    buf = ctypes.create_string_buffer(80)
    n = lib.ps2000_get_unit_info(handle, buf, ctypes.c_int16(80), ctypes.c_int16(line))
    return buf.value[:n].decode("ascii", errors="replace").strip()


def main() -> int:
    print("[1] importing picosdk / loading ps2000.dll")
    try:
        from picosdk.ps2000 import ps2000 as lib
    except ImportError as exc:
        print(f"    FAIL: picosdk wrapper not installed ({exc}). pip install picosdk")
        return 1
    except OSError as exc:
        print(f"    FAIL: ps2000.dll did not load ({exc}).")
        print("    Either PicoSDK is missing, or its bitness differs from this Python")
        print(f"    (this Python is {ctypes.sizeof(ctypes.c_void_p) * 8}-bit).")
        return 1
    print("    OK")

    print("[2] ps2000_open_unit")
    handle = lib.ps2000_open_unit()
    if handle == 0:
        print("    FAIL: no unit found. Check USB, and close the PicoScope app.")
        return 1
    if handle < 0:
        print("    FAIL: open_unit error. Unplug and replug the scope.")
        return 1
    print(f"    OK, handle={handle}")

    try:
        print("[3] unit info")
        for line, label in INFO_LINES.items():
            try:
                print(f"    {label:22s}: {info(lib, handle, line)}")
            except Exception as exc:  # noqa: BLE001 - probing an unknown line
                print(f"    {label:22s}: <unreadable: {exc}>")

        print("[4] voltage ranges channel A accepts (DC)")
        accepted = []
        for volts, code in RANGE_ENUM.items():
            ok = lib.ps2000_set_channel(handle, CHANNEL_A, 1, 1, code)
            print(f"    {volts:>6} V (enum {code:2d}): {'accepted' if ok else 'rejected'}")
            if ok:
                accepted.append(volts)
        if not accepted:
            print("    FAIL: every range rejected — wrong driver family?")
            return 1

        print("[5] timebases (for %d samples)" % SAMPLES)
        lib.ps2000_set_channel(handle, CHANNEL_A, 1, 1, RANGE_ENUM[max(accepted)])
        fastest = None
        for timebase in range(24):
            interval = ctypes.c_int32()
            units = ctypes.c_int16()
            max_samples = ctypes.c_int32()
            ok = lib.ps2000_get_timebase(
                handle, timebase, SAMPLES,
                ctypes.byref(interval), ctypes.byref(units),
                OVERSAMPLE, ctypes.byref(max_samples),
            )
            if not ok or interval.value <= 0:
                print(f"    tb {timebase:2d}: rejected")
                continue
            rate = 1e9 / interval.value
            if fastest is None:
                fastest = (timebase, interval.value, max_samples.value)
            print(
                f"    tb {timebase:2d}: {interval.value:>10d} ns "
                f"({rate:>12.4g} S/s), units={units.value}, "
                f"max_samples={max_samples.value}"
            )
        if fastest is None:
            print("    FAIL: no usable timebase")
            return 1
        print(
            f"    fastest usable: tb {fastest[0]}, {fastest[1]} ns "
            f"= {1e9 / fastest[1]:.4g} S/s, max_samples {fastest[2]}"
        )

        print("[6] one free-running block capture")
        range_v = max(accepted)
        lib.ps2000_set_channel(handle, CHANNEL_A, 1, 1, RANGE_ENUM[range_v])
        lib.ps2000_set_trigger(handle, 5, 0, 0, 0, 0)  # source NONE = free run
        timebase = fastest[0]
        indisposed = ctypes.c_int32()
        if not lib.ps2000_run_block(
            handle, SAMPLES, timebase, OVERSAMPLE, ctypes.byref(indisposed)
        ):
            print("    FAIL: run_block refused to start")
            return 1
        deadline = time.monotonic() + max(indisposed.value / 1000.0, 0) + 5.0
        while time.monotonic() < deadline and lib.ps2000_ready(handle) == 0:
            time.sleep(0.005)
        buf = (ctypes.c_int16 * SAMPLES)()
        overflow = ctypes.c_int16()
        got = lib.ps2000_get_values(
            handle, ctypes.byref(buf), None, None, None,
            ctypes.byref(overflow), SAMPLES,
        )
        lib.ps2000_stop(handle)
        if got <= 0:
            print("    FAIL: get_values returned nothing")
            return 1
        adc = np.ctypeslib.as_array(buf)[:got].astype(float)
        print(
            f"    OK: {got} samples, raw ADC min {adc.min():.0f} max {adc.max():.0f}, "
            f"overflow flag {overflow.value}"
        )
        print(
            f"    assuming MAX_ADC=32767 on ±{range_v} V: "
            f"{adc.min() / 32767 * range_v:+.4f} V .. {adc.max() / 32767 * range_v:+.4f} V"
        )
        print("\nStep 0 PASSED — the device answers. Values above are the facts to")
        print("carry into backends/ps2000.py; nothing here was assumed.")
        return 0
    finally:
        lib.ps2000_close_unit(handle)
        print("[closed]")


if __name__ == "__main__":
    sys.exit(main())
