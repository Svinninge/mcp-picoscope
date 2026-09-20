# File version: v0.01
"""Simulated single-channel scope. No hardware, known answers.

The mock is the development path for everything except backends/ps2000.py, and
it is the reference the analysis tests measure against: it produces signals
whose frequency, amplitude and duty cycle are known exactly, so a measurement
can be checked rather than eyeballed.

It deliberately imitates three things real hardware does and naive simulators
do not: the sample interval snaps to the driver's timebase grid, samples are
quantised to 8 bits of the selected range, and a signal larger than the range
clips instead of growing.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

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

WAVEFORMS = ("sine", "square", "ramp", "triangle", "noise", "dc")

# Simulated instrument limits. Not PS2104 numbers — the real ones are read off
# the device in PLAN.md step 0 and belong in the ps2000 backend.
BASE_RATE_HZ = 50e6
MAX_SAMPLES = 32768
VOLTAGE_RANGES_V = (0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0)
RESOLUTION_BITS = 8
# AC coupling is a capacitor, and it takes time to charge to the new DC level.
# Measured on the PS2104, 2026-09-13: after switching an 0..3 V sine to AC the
# midpoint read +0.24 V at 1.08 s, +0.02 V at 1.25 s and settled by 1.42 s —
# a time constant near 0.12 s. The mock used to remove the DC instantly, which
# is exactly how a bug that measured an unsettled signal passed every test.
AC_SETTLE_TAU_S = 0.12


@dataclass
class MockSignal:
    waveform: str = "sine"
    frequency_hz: float = 1000.0
    amplitude_v: float = 1.0  # peak, so Vpp is twice this
    offset_v: float = 0.0
    noise_v: float = 0.002
    duty_cycle_pct: float = 50.0

    def validate(self) -> None:
        if self.waveform not in WAVEFORMS:
            raise ScopeError(
                f"Unknown waveform {self.waveform!r}. Valid: {', '.join(WAVEFORMS)}."
            )
        if self.frequency_hz <= 0 and self.waveform not in ("noise", "dc"):
            raise ScopeError("frequency_hz must be positive.")
        if not 0 < self.duty_cycle_pct < 100:
            raise ScopeError("duty_cycle_pct must be between 0 and 100 (exclusive).")

    def at(self, t: np.ndarray) -> np.ndarray:
        """Ideal, noise-free signal at times `t` (seconds)."""
        a, off = self.amplitude_v, self.offset_v
        if self.waveform == "dc":
            return np.full_like(t, off + a)
        if self.waveform == "noise":
            return np.full_like(t, off)

        phase = (t * self.frequency_hz) % 1.0  # 0..1 within the period
        if self.waveform == "sine":
            return off + a * np.sin(2 * math.pi * phase)
        if self.waveform == "square":
            duty = self.duty_cycle_pct / 100.0
            return off + np.where(phase < duty, a, -a)
        if self.waveform == "ramp":
            return off + a * (2 * phase - 1)
        # triangle
        return off + a * (1 - 4 * np.abs(phase - 0.5))


class MockBackend:
    """Implements ScopeBackend against a signal generator in software."""

    name = "mock"

    def __init__(self, signal: MockSignal | None = None, seed: int = 0) -> None:
        self.signal = signal or MockSignal()
        self.channel = ChannelConfig()
        self.trigger = TriggerConfig()
        self._rng = np.random.default_rng(seed)
        self._open = False
        self._ac_since = 0.0

    # -- lifecycle ---------------------------------------------------------

    def list_devices(self) -> list[dict]:
        return [
            {
                "backend": "mock",
                "model": "PS2104-MOCK",
                "serial": "MOCK0001",
                "note": "Simulated device. No hardware required.",
            }
        ]

    def open(self) -> DeviceInfo:
        self._open = True
        return self.info()

    def close(self) -> None:
        self._open = False

    def info(self) -> DeviceInfo:
        return DeviceInfo(
            backend="mock",
            model="PS2104-MOCK",
            serial="MOCK0001",
            driver_version="mock 0.01",
            channels=1,
            resolution_bits=RESOLUTION_BITS,
            voltage_ranges_v=VOLTAGE_RANGES_V,
            max_sample_rate_hz=BASE_RATE_HZ,
            max_samples=MAX_SAMPLES,
            note=(
                "Simulated scope — not a PS2104. Limits and ranges here are the "
                "mock's own; real values come from the device in the ps2000 backend."
            ),
        )

    # -- configuration -----------------------------------------------------

    def set_channel(self, config: ChannelConfig) -> ChannelConfig:
        config.range_v = pick_range(config.range_v, VOLTAGE_RANGES_V)
        if config.coupling not in ("DC", "AC"):
            raise ScopeError(f"coupling must be 'DC' or 'AC', got {config.coupling!r}.")
        if config.coupling == "AC" and self.channel.coupling != "AC":
            self._ac_since = time.monotonic()  # the capacitor starts charging now
        self.channel = config
        return config

    def set_trigger(self, config: TriggerConfig) -> TriggerConfig:
        if config.mode not in ("auto", "edge"):
            raise ScopeError(f"trigger mode must be 'auto' or 'edge', got {config.mode!r}.")
        if config.direction not in ("rising", "falling"):
            raise ScopeError(
                f"trigger direction must be 'rising' or 'falling', got {config.direction!r}."
            )
        self.trigger = config
        return config

    def set_signal(self, signal: MockSignal) -> MockSignal:
        signal.validate()
        self.signal = signal
        return signal

    # -- acquisition -------------------------------------------------------

    def capture_block(
        self, duration_s: float, samples: int, max_wait_s: float | None = None
    ) -> Capture:
        # max_wait_s bounds a trigger wait; the mock answers at once either way.
        if not self._open:
            raise ScopeError("Mock device is not open.")
        if duration_s <= 0:
            raise ScopeError("duration_s must be positive.")
        if samples <= 0:
            raise ScopeError("samples must be positive.")

        samples = min(samples, MAX_SAMPLES)
        dt = _snap_interval(duration_s / samples)
        t_start = self._trigger_time(dt, samples)
        t = t_start + np.arange(samples) * dt

        volts = self.signal.at(t)
        if self.channel.coupling == "AC":
            # The DC level decays away rather than vanishing: what is left of it
            # shrinks with the time since the switch.
            dc = float(np.mean(volts))
            remaining = math.exp(-(time.monotonic() - self._ac_since) / AC_SETTLE_TAU_S)
            volts = volts - dc + dc * remaining
        if self.signal.noise_v > 0:
            volts = volts + self._rng.normal(0.0, self.signal.noise_v, samples)

        volts = _quantise(volts, self.channel.range_v)
        return Capture(
            capture_id="",  # assigned by the session
            volts=volts,
            dt_s=dt,
            range_v=self.channel.range_v,
            coupling=self.channel.coupling,
            trigger=self.trigger,
            overrange=detect_overrange(volts, self.channel.range_v),
            requested_duration_s=duration_s,
            requested_samples=samples,
        )

    def _trigger_time(self, dt: float, samples: int) -> float:
        """Start time of the record, honouring the trigger settings.

        Auto mode starts wherever the signal happens to be. Edge mode finds the
        requested crossing and places it at the trigger position; a threshold
        the signal never reaches times out exactly as real hardware does.
        """
        free_running = self._rng.uniform(0.0, 1.0) / max(self.signal.frequency_hz, 1.0)
        if self.trigger.mode == "auto":
            return free_running

        offset = (self.trigger.delay_pct / 100.0) * samples * dt
        crossing = self._find_crossing(dt)
        if crossing is None:
            if self.trigger.auto_trigger_ms == 0:
                raise ScopeError(
                    f"Trigger never fired: no {self.trigger.direction} crossing of "
                    f"{self.trigger.threshold_v} V in the signal "
                    f"(Vpp {2 * self.signal.amplitude_v} V around "
                    f"{self.signal.offset_v} V). Move the threshold inside the signal "
                    "or set auto_trigger_ms > 0 to capture untriggered."
                )
            return free_running  # auto-trigger rescue, same as the hardware
        return crossing - offset

    def _find_crossing(self, dt: float) -> float | None:
        """First crossing time of the trigger threshold, searched over one period."""
        if self.signal.waveform in ("noise", "dc"):
            return None
        period = 1.0 / self.signal.frequency_hz
        fine = np.linspace(0.0, period, 4096, endpoint=False)
        v = self.signal.at(fine)
        level = self.trigger.threshold_v
        if self.trigger.direction == "rising":
            hits = np.flatnonzero((v[:-1] < level) & (v[1:] >= level))
        else:
            hits = np.flatnonzero((v[:-1] > level) & (v[1:] <= level))
        if hits.size == 0:
            return None
        return float(fine[hits[0]])


def _snap_interval(requested_dt_s: float) -> float:
    """Nearest timebase at or above the request, on the driver's 2^n grid.

    Real drivers do not hand out arbitrary sample rates, and the difference is
    worth simulating: every caller must read the actual rate back off the
    capture instead of trusting what it asked for.
    """
    base = 1.0 / BASE_RATE_HZ
    if requested_dt_s <= base:
        return base
    n = math.ceil(math.log2(requested_dt_s / base))
    return base * (2**n)


def _quantise(volts: np.ndarray, range_v: float) -> np.ndarray:
    """Clip to the range, then round to the ADC's 8-bit grid."""
    clipped = np.clip(volts, -range_v, range_v)
    step = 2 * range_v / (2**RESOLUTION_BITS)
    return np.round(clipped / step) * step
