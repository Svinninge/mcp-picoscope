# File version: v0.04
"""Real hardware backend for the PicoScope 2104 via the legacy ps2000 driver.

STATUS: verified against the real device 2026-09-12 (PLAN.md step 0). The unit
reports variant "2104", serial <serial>, hardware 4, driver 3.0.152.6217; the
volt scale is measured against a 1.5 V cell, the zero against a shorted input,
the frequency against an 800 Hz sine, and the edge trigger by the spread of the
starting point: free-running captures start anywhere (31 % of Vpp), edge-
triggered ones within 0.7 %, and the direction decides the slope 12 times out
of 12. Both failure paths too: a threshold the signal never reaches times out
with a readable message, or is rescued by auto_trigger_ms. Nothing here is
assumed any more — tools/verify_trigger.py repeats the measurement.

PS2104 belongs to the OLD 2000 series and speaks ps2000.dll. The ps2000a family
answers "unit not found" on this device, and that failure is indistinguishable
from broken hardware if you do not already know. See SOUL.md, Hardware.
"""

from __future__ import annotations

import ctypes
import logging
import os
import time
from pathlib import Path

import numpy as np

from ..analysis import detect_overrange
from ..scope import (
    Capture,
    ChannelConfig,
    DeviceInfo,
    ScopeError,
    TriggerConfig,
    pick_range,
)

log = logging.getLogger(__name__)

# Where ps2000.dll lives. picosdk resolves it with ctypes.util.find_library,
# which on Windows searches PATH — and nothing puts Pico's directory there.
# PicoSDK installs to SDK\lib; the PicoScope 7 application ships the same
# driver DLLs in its own directory, which is what this machine actually has.
DLL_NAME = "ps2000.dll"
DLL_DIR_ENV = "PICOSDK_DIR"
DLL_CANDIDATES = (
    r"C:\Program Files\Pico Technology\SDK\lib",
    r"C:\Program Files\Pico Technology\PicoScope 7 T&M Stable",
    r"C:\Program Files\Pico Technology\PicoScope 7 T&M Early Access",
    r"C:\Program Files\Pico Technology\PicoScope 7 Automotive Stable",
    r"C:\Program Files\Pico Technology\PicoScope 6",
)

# ps2000 voltage range enum. The legacy driver has no call that reports which
# ranges a given variant supports, so this table is the one thing that cannot
# be asked for. Ranges the device rejects are dropped at open time by probing
# set_channel, so the reported list still comes from the hardware — on the
# PS2104 that leaves 100 mV..20 V; it rejects 20 mV and 50 mV (measured
# 2026-09-12).
RANGE_ENUM: dict[float, int] = {
    0.02: 1,
    0.05: 2,
    0.1: 3,
    0.2: 4,
    0.5: 5,
    1.0: 6,
    2.0: 7,
    5.0: 8,
    10.0: 9,
    20.0: 10,
}

CHANNEL_A = 0
# The legacy driver scales to full int16 regardless of the 8-bit front end, and
# offers no maximum_value() call to ask — picosdk's own wrapper falls back to
# the same 2**15-1. Verified 2026-09-12 against a 1.5 V alkaline cell: four
# ranges read 1.593..1.640 V, agreeing within 47 mV. Absolute value is what
# proves this constant; a wrong one is a constant factor on every range at
# once, so cross-range agreement alone would not have caught it.
MAX_ADC = 32767
# Timebases 0..19 are valid on the PS2104 (20 ns .. 10.49 ms, measured
# 2026-09-12); the loop stops at the first rejection anyway.
MAX_TIMEBASE = 32
OVERSAMPLE = 1
READY_POLL_S = 0.005

# Info lines of ps2000_get_unit_info.
INFO_DRIVER_VERSION = 0
INFO_HARDWARE_VERSION = 2
INFO_VARIANT = 3
INFO_BATCH_AND_SERIAL = 4

TRIGGER_DIRECTION = {"rising": 0, "falling": 1}


def _dll_directory() -> Path | None:
    """Find the directory holding ps2000.dll, or None if it is not installed.

    PICOSDK_DIR wins when set, so an unusual install is a setting and not a
    code change.
    """
    override = os.environ.get(DLL_DIR_ENV)
    candidates = [override] if override else list(DLL_CANDIDATES)
    for candidate in candidates:
        if candidate and (Path(candidate) / DLL_NAME).is_file():
            return Path(candidate)
    return None


def _ensure_dll_on_path() -> None:
    """Put the driver directory where picosdk will look.

    picosdk resolves the DLL with ctypes.util.find_library, which searches PATH
    on Windows. Nothing adds Pico's directory to PATH, so a plain import fails
    on a machine where the driver is installed and working — which is every
    machine, until someone prepends it by hand.
    """
    directory = _dll_directory()
    if directory is None:
        return
    if hasattr(os, "add_dll_directory"):  # Windows: dependent DLLs of ps2000
        os.add_dll_directory(str(directory))
    if str(directory) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(directory) + os.pathsep + os.environ.get("PATH", "")
        log.info("added %s to PATH for %s", directory, DLL_NAME)


def _load_driver():
    """Import the picosdk wrapper, translating both ways it can fail."""
    _ensure_dll_on_path()
    try:
        from picosdk.ps2000 import ps2000  # type: ignore
    except ImportError as exc:
        raise ScopeError(
            "The picosdk Python wrapper is not installed. "
            "Install it with: pip install picosdk"
        ) from exc
    except OSError as exc:
        where = _dll_directory()
        raise ScopeError(
            f"{DLL_NAME} could not be loaded"
            + (f" from {where}" if where else " and was not found on this machine")
            + ". Install the 64-bit PicoSDK (or the PicoScope application, which "
            "ships the same driver), or point "
            f"{DLL_DIR_ENV} at the directory holding it. A bitness mismatch looks "
            "the same: 64-bit Python needs the 64-bit driver. "
            f"Original error: {exc}"
        ) from exc
    return ps2000


class PS2000Backend:
    """Implements ScopeBackend against ps2000.dll."""

    name = "ps2000"

    def __init__(self) -> None:
        self._lib = None
        self._handle: int | None = None
        self._info: DeviceInfo | None = None
        self.channel = ChannelConfig()
        self.trigger = TriggerConfig()
        self._ranges: tuple[float, ...] = tuple(RANGE_ENUM)

    # -- lifecycle ---------------------------------------------------------

    def list_devices(self) -> list[dict]:
        """Open, read the nameplate, close. The only enumeration ps2000 offers."""
        if self._handle is not None:
            return [_device_row(self.info())]
        lib = _load_driver()
        handle = lib.ps2000_open_unit()
        if handle <= 0:
            return []
        try:
            row = {
                "backend": "ps2000",
                "model": _unit_info(lib, handle, INFO_VARIANT),
                "serial": _unit_info(lib, handle, INFO_BATCH_AND_SERIAL),
                "note": "Found on USB via ps2000.dll.",
            }
        finally:
            lib.ps2000_close_unit(handle)
        return [row]

    def open(self) -> DeviceInfo:
        if self._handle is not None:
            return self.info()
        lib = _load_driver()
        handle = lib.ps2000_open_unit()
        if handle == 0:
            raise ScopeError(
                "No PicoScope found. Check the USB cable, and close the PicoScope "
                "application if it is running — the device can only be opened by "
                "one process at a time."
            )
        if handle < 0:
            raise ScopeError(
                "ps2000_open_unit failed. The device is present but could not be "
                "opened; a stale handle from a crashed process is the usual cause. "
                "Unplug and replug the scope."
            )
        self._lib, self._handle = lib, handle
        try:
            self._ranges = self._probe_ranges()
            self._info = self._read_info()
            self.set_channel(self.channel)
            self.set_trigger(self.trigger)
        except Exception:
            self.close()  # never leak the USB handle on a half-finished open
            raise
        return self._info

    def close(self) -> None:
        if self._lib is not None and self._handle is not None:
            try:
                self._lib.ps2000_stop(self._handle)
            finally:
                self._lib.ps2000_close_unit(self._handle)
        self._handle = None
        self._lib = None
        self._info = None

    def info(self) -> DeviceInfo:
        if self._info is None:
            raise ScopeError("Device is not open.")
        return self._info

    def _read_info(self) -> DeviceInfo:
        lib, handle = self._require()
        interval_ns, max_samples = self._timebase_limits()
        return DeviceInfo(
            backend="ps2000",
            model=_unit_info(lib, handle, INFO_VARIANT),
            serial=_unit_info(lib, handle, INFO_BATCH_AND_SERIAL),
            driver_version=_unit_info(lib, handle, INFO_DRIVER_VERSION),
            channels=1,
            resolution_bits=8,
            voltage_ranges_v=self._ranges,
            max_sample_rate_hz=1e9 / interval_ns if interval_ns else 0.0,
            max_samples=max_samples,
            note=(
                "Legacy ps2000 driver. Hardware version "
                f"{_unit_info(lib, handle, INFO_HARDWARE_VERSION)}."
            ),
        )

    def _timebase_limits(self) -> tuple[int, int]:
        """Fastest sample interval (ns) and buffer depth, asked of the driver.

        Probed with a small record so the question is about the device rather
        than about the request: on the PS2104 this answers 20 ns and 8092
        samples, and neither number belongs in a constant.
        """
        lib, handle = self._require()
        probe_samples = 1024
        for timebase in range(MAX_TIMEBASE):
            interval_ns = ctypes.c_int32()
            time_units = ctypes.c_int16()
            max_samples = ctypes.c_int32()
            ok = lib.ps2000_get_timebase(
                handle,
                timebase,
                probe_samples,
                ctypes.byref(interval_ns),
                ctypes.byref(time_units),
                OVERSAMPLE,
                ctypes.byref(max_samples),
            )
            if ok and interval_ns.value > 0:
                return interval_ns.value, max_samples.value
        raise ScopeError(
            "The driver rejected every timebase — the device answered open_unit "
            "but cannot be configured to sample."
        )

    def _probe_ranges(self) -> tuple[float, ...]:
        """Keep the ranges the device actually accepts on channel A."""
        lib, handle = self._require()
        accepted = [
            volts
            for volts, code in RANGE_ENUM.items()
            if lib.ps2000_set_channel(handle, CHANNEL_A, 1, 1, code) != 0
        ]
        if not accepted:
            raise ScopeError(
                "The device rejected every voltage range — channel A could not be "
                "configured. This is what a wrong driver family looks like; "
                "confirm the unit really is a 2000-series (ps2000, not ps2000a)."
            )
        return tuple(sorted(accepted))

    # -- configuration -----------------------------------------------------

    def set_channel(self, config: ChannelConfig) -> ChannelConfig:
        lib, handle = self._require()
        config.range_v = pick_range(config.range_v, self._ranges)
        if config.coupling not in ("DC", "AC"):
            raise ScopeError(f"coupling must be 'DC' or 'AC', got {config.coupling!r}.")
        status = lib.ps2000_set_channel(
            handle,
            CHANNEL_A,
            1 if config.enabled else 0,
            1 if config.coupling == "DC" else 0,
            RANGE_ENUM[config.range_v],
        )
        if status == 0:
            raise ScopeError(
                f"ps2000_set_channel failed for range {config.range_v} V "
                f"{config.coupling}."
            )
        self.channel = config
        return config

    def set_trigger(self, config: TriggerConfig) -> TriggerConfig:
        lib, handle = self._require()
        if config.mode not in ("auto", "edge"):
            raise ScopeError(f"trigger mode must be 'auto' or 'edge', got {config.mode!r}.")
        if config.direction not in TRIGGER_DIRECTION:
            raise ScopeError(
                f"trigger direction must be 'rising' or 'falling', got {config.direction!r}."
            )
        if abs(config.threshold_v) > self.channel.range_v:
            raise ScopeError(
                f"threshold_v {config.threshold_v} V is outside the selected "
                f"±{self.channel.range_v} V range — the trigger could never fire. "
                "Widen the range first."
            )

        if config.mode == "auto":
            # source PS2000_NONE (5) disables the trigger; the block returns
            # immediately, free running.
            status = lib.ps2000_set_trigger(handle, 5, 0, 0, 0, 0)
        else:
            status = lib.ps2000_set_trigger(
                handle,
                CHANNEL_A,
                _volts_to_adc(config.threshold_v, self.channel.range_v),
                TRIGGER_DIRECTION[config.direction],
                int(config.delay_pct),
                int(config.auto_trigger_ms),
            )
        if status == 0:
            raise ScopeError("ps2000_set_trigger failed.")
        self.trigger = config
        return config

    # -- acquisition -------------------------------------------------------

    def capture_block(self, duration_s: float, samples: int) -> Capture:
        lib, handle = self._require()
        if duration_s <= 0:
            raise ScopeError("duration_s must be positive.")
        if samples <= 0:
            raise ScopeError("samples must be positive.")

        timebase, interval_ns, max_samples = self._select_timebase(duration_s, samples)
        samples = min(samples, max_samples)

        time_indisposed_ms = ctypes.c_int32()
        if lib.ps2000_run_block(
            handle, samples, timebase, OVERSAMPLE, ctypes.byref(time_indisposed_ms)
        ) == 0:
            raise ScopeError("ps2000_run_block failed to start the capture.")

        self._wait_ready(timeout_s=self._capture_timeout_s(time_indisposed_ms.value))

        buffer = (ctypes.c_int16 * samples)()
        overflow = ctypes.c_int16()
        got = lib.ps2000_get_values(
            handle,
            ctypes.byref(buffer),
            None,
            None,
            None,
            ctypes.byref(overflow),
            samples,
        )
        lib.ps2000_stop(handle)
        if got <= 0:
            raise ScopeError(
                "ps2000_get_values returned no samples. The device may have been "
                "unplugged mid-capture."
            )

        adc = np.ctypeslib.as_array(buffer)[:got].astype(float)
        volts = adc / MAX_ADC * self.channel.range_v
        return Capture(
            capture_id="",  # assigned by the session
            volts=volts,
            dt_s=interval_ns * 1e-9,
            range_v=self.channel.range_v,
            coupling=self.channel.coupling,
            trigger=self.trigger,
            # The driver's overflow flag catches the AFE railing; the sample
            # check also catches a signal that only touches the rail briefly.
            overrange=bool(overflow.value & 1)
            or detect_overrange(volts, self.channel.range_v),
            requested_duration_s=duration_s,
            requested_samples=samples,
        )

    def _wait_ready(self, timeout_s: float) -> None:
        lib, handle = self._require()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ready = lib.ps2000_ready(handle)
            if ready > 0:
                return
            if ready < 0:
                lib.ps2000_stop(handle)
                raise ScopeError("The scope was disconnected during the capture.")
            time.sleep(READY_POLL_S)
        lib.ps2000_stop(handle)
        raise ScopeError(
            f"The capture did not complete within {timeout_s:.1f} s. With "
            f"trigger mode {self.trigger.mode!r} at {self.trigger.threshold_v} V "
            f"({self.trigger.direction}), the likely cause is a trigger that never "
            "fired. Set auto_trigger_ms > 0, or move the threshold inside the signal."
        )

    def _capture_timeout_s(self, time_indisposed_ms: int) -> float:
        """Acquisition time plus the trigger wait, with a floor for USB latency."""
        base = max(time_indisposed_ms, 0) / 1000.0 + 1.0
        if self.trigger.mode == "edge":
            base += (self.trigger.auto_trigger_ms or 5000) / 1000.0
        return base

    def _select_timebase(self, duration_s: float, samples: int) -> tuple[int, int, int]:
        """Fastest timebase whose record still spans `duration_s`.

        Returns (timebase, actual interval in ns, max samples at that timebase).
        The interval is what the driver reports, not what was asked for — the
        two are rarely the same and the caller needs the real one.
        """
        lib, handle = self._require()
        wanted_ns = duration_s / samples * 1e9
        best: tuple[int, int, int] | None = None

        for timebase in range(MAX_TIMEBASE):
            interval_ns = ctypes.c_int32()
            time_units = ctypes.c_int16()
            max_samples = ctypes.c_int32()
            ok = lib.ps2000_get_timebase(
                handle,
                timebase,
                samples,
                ctypes.byref(interval_ns),
                ctypes.byref(time_units),
                OVERSAMPLE,
                ctypes.byref(max_samples),
            )
            if ok == 0 or interval_ns.value <= 0:
                continue
            best = (timebase, interval_ns.value, max_samples.value)
            if interval_ns.value >= wanted_ns:
                return best

        if best is None:
            raise ScopeError(
                "No usable timebase. The driver rejected every setting for "
                f"{samples} samples — ask for fewer samples."
            )
        # Nothing was slow enough: the slowest timebase is the best available.
        return best

    def _require(self):
        if self._lib is None or self._handle is None:
            raise ScopeError("Device is not open.")
        return self._lib, self._handle


def _unit_info(lib, handle: int, line: int) -> str:
    buffer = ctypes.create_string_buffer(64)
    length = lib.ps2000_get_unit_info(handle, buffer, ctypes.c_int16(64), ctypes.c_int16(line))
    return buffer.value[:length].decode("ascii", errors="replace").strip()


def _volts_to_adc(volts: float, range_v: float) -> int:
    return int(round(volts / range_v * MAX_ADC))


def _device_row(info: DeviceInfo) -> dict:
    return {
        "backend": info.backend,
        "model": info.model,
        "serial": info.serial,
        "note": "Already open in this session.",
    }
