# File version: v0.01
"""Scope abstraction: value types, backend protocol and the single owned session.

Shared by server.py and every backend. The driver is not thread safe and the
device can only be opened by one process, so all hardware access goes through
one ScopeSession guarded by a lock.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

# Capture retention. A block is tens of thousands of floats; keeping every
# capture of a long session would quietly grow without bound.
MAX_CAPTURES = 32


class ScopeError(Exception):
    """Any failure a calling session should read as a sentence, not a stack trace."""


@dataclass(frozen=True)
class DeviceInfo:
    backend: str
    model: str
    serial: str
    driver_version: str
    channels: int
    resolution_bits: int
    voltage_ranges_v: tuple[float, ...]
    max_sample_rate_hz: float
    max_samples: int
    note: str = ""


@dataclass
class ChannelConfig:
    range_v: float = 5.0
    coupling: str = "DC"  # "DC" | "AC"
    enabled: bool = True


@dataclass
class TriggerConfig:
    mode: str = "auto"  # "auto" | "edge"
    threshold_v: float = 0.0
    direction: str = "rising"  # "rising" | "falling"
    delay_pct: float = 0.0  # trigger position, -100..100 % of the block
    auto_trigger_ms: int = 1000  # 0 = wait forever (edge mode only)


@dataclass
class Capture:
    """One acquired block. `volts` is the full record; never send it to an LLM."""

    capture_id: str
    volts: np.ndarray
    dt_s: float  # actual sample interval, as reported by the driver
    range_v: float
    coupling: str
    trigger: TriggerConfig
    overrange: bool
    requested_duration_s: float
    requested_samples: int

    @property
    def sample_rate_hz(self) -> float:
        return 1.0 / self.dt_s

    @property
    def duration_s(self) -> float:
        return len(self.volts) * self.dt_s


class ScopeBackend(Protocol):
    """What a backend must provide. Mock and ps2000 both implement this."""

    name: str

    def list_devices(self) -> list[dict]: ...

    def open(self) -> DeviceInfo: ...

    def close(self) -> None: ...

    def info(self) -> DeviceInfo: ...

    def set_channel(self, config: ChannelConfig) -> ChannelConfig: ...

    def set_trigger(self, config: TriggerConfig) -> TriggerConfig: ...

    def capture_block(
        self, duration_s: float, samples: int, max_wait_s: float | None = None
    ) -> Capture: ...


@dataclass
class ScopeSession:
    """The one owned device session. Serialises every call to the backend."""

    backend: ScopeBackend | None = None
    device: DeviceInfo | None = None
    channel: ChannelConfig = field(default_factory=ChannelConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    captures: OrderedDict[str, Capture] = field(default_factory=OrderedDict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    _counter: int = 0

    @property
    def is_open(self) -> bool:
        return self.device is not None

    def require_open(self) -> ScopeBackend:
        if self.backend is None or self.device is None:
            raise ScopeError(
                "No device is open. Call open_device() first "
                "(backend='auto' falls back to the mock when no hardware is found)."
            )
        return self.backend

    def next_capture_id(self) -> str:
        self._counter += 1
        return f"cap{self._counter:04d}"

    def store(self, capture: Capture) -> None:
        self.captures[capture.capture_id] = capture
        while len(self.captures) > MAX_CAPTURES:
            self.captures.popitem(last=False)

    def get_capture(self, capture_id: str) -> Capture:
        try:
            return self.captures[capture_id]
        except KeyError:
            known = ", ".join(self.captures) or "none"
            raise ScopeError(
                f"Unknown capture_id {capture_id!r}. Held captures: {known}. "
                f"Only the {MAX_CAPTURES} most recent are kept."
            ) from None

    def state(self) -> dict:
        """Readable snapshot — backs the picoscope://state resource."""
        return {
            "open": self.is_open,
            "backend": self.backend.name if self.backend else None,
            "device": _device_dict(self.device) if self.device else None,
            "channel": {
                "range_v": self.channel.range_v,
                "coupling": self.channel.coupling,
                "enabled": self.channel.enabled,
            },
            "trigger": {
                "mode": self.trigger.mode,
                "threshold_v": self.trigger.threshold_v,
                "direction": self.trigger.direction,
                "delay_pct": self.trigger.delay_pct,
                "auto_trigger_ms": self.trigger.auto_trigger_ms,
            },
            "captures": [
                {
                    "capture_id": c.capture_id,
                    "samples": int(len(c.volts)),
                    "sample_rate_hz": c.sample_rate_hz,
                    "duration_s": c.duration_s,
                    "range_v": c.range_v,
                    "overrange": c.overrange,
                }
                for c in self.captures.values()
            ],
        }


def _device_dict(info: DeviceInfo) -> dict:
    return {
        "backend": info.backend,
        "model": info.model,
        "serial": info.serial,
        "driver_version": info.driver_version,
        "channels": info.channels,
        "resolution_bits": info.resolution_bits,
        "voltage_ranges_v": list(info.voltage_ranges_v),
        "max_sample_rate_hz": info.max_sample_rate_hz,
        "max_samples": info.max_samples,
        "note": info.note,
    }


def pick_range(requested_v: float, available: tuple[float, ...]) -> float:
    """Smallest available range that still holds `requested_v`.

    Ranges are full-scale +/- volts. Asking for more than the largest range is a
    caller error worth naming: silently clamping would hand back a capture that
    clips without saying so.
    """
    if requested_v <= 0:
        raise ScopeError(f"range_v must be positive, got {requested_v}.")
    for r in sorted(available):
        if r >= requested_v - 1e-12:
            return r
    raise ScopeError(
        f"range_v={requested_v} V exceeds the largest range this device has "
        f"({max(available)} V). Available: {sorted(available)}."
    )
