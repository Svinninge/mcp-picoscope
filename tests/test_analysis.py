# File version: v0.02
"""Measurements against the mock's known answers.

The mock is the ground truth here: it knows the frequency, amplitude and duty
cycle it generated, so every assertion below compares a measurement to a fact
rather than to a previous run.
"""

from __future__ import annotations

import numpy as np
import pytest

from mcp_picoscope import analysis
from mcp_picoscope.analysis import downsample_minmax, measure
from mcp_picoscope.backends.mock import MockBackend, MockSignal
from mcp_picoscope.scope import (
    Capture,
    ChannelConfig,
    ScopeError,
    TriggerConfig,
    pick_range,
)


def capture(signal: MockSignal, *, range_v: float = 5.0, duration_s: float = 0.01,
            samples: int = 8192, coupling: str = "DC"):
    backend = MockBackend(signal)
    backend.open()
    backend.set_channel(ChannelConfig(range_v, coupling, True))
    cap = backend.capture_block(duration_s, samples)
    cap.capture_id = "test"
    return cap


@pytest.mark.parametrize("frequency", [50.0, 1000.0, 12345.0])
def test_sine_frequency_within_one_percent(frequency):
    cap = capture(MockSignal("sine", frequency, 2.0), duration_s=20 / frequency)
    stats = measure(cap)
    assert stats["frequency_hz"] == pytest.approx(frequency, rel=0.01)


def test_sine_amplitude_and_rms():
    cap = capture(MockSignal("sine", 1000.0, 2.0, noise_v=0.0), range_v=5.0)
    stats = measure(cap)
    # 8-bit quantisation on a 5 V range is ~39 mV per step; tolerate two steps.
    assert stats["vpp_v"] == pytest.approx(4.0, abs=0.08)
    assert stats["rms_v"] == pytest.approx(2.0 / np.sqrt(2), abs=0.08)
    assert stats["mean_v"] == pytest.approx(0.0, abs=0.08)


@pytest.mark.parametrize("duty", [25.0, 50.0, 75.0])
def test_square_duty_cycle(duty):
    cap = capture(
        MockSignal("square", 1000.0, 1.0, noise_v=0.0, duty_cycle_pct=duty),
        duration_s=0.02,
        samples=16384,
    )
    stats = measure(cap)
    assert stats["frequency_hz"] == pytest.approx(1000.0, rel=0.01)
    assert stats["duty_cycle_pct"] == pytest.approx(duty, abs=1.0)


def test_offset_square_measures_duty_against_its_own_midpoint():
    """A 20 % duty wave has a mean far from its midpoint; duty must not follow it."""
    cap = capture(
        MockSignal("square", 1000.0, 1.0, offset_v=1.0, noise_v=0.0, duty_cycle_pct=20.0),
        duration_s=0.02,
        samples=16384,
    )
    assert measure(cap)["duty_cycle_pct"] == pytest.approx(20.0, abs=1.5)


def test_dc_reports_no_frequency():
    cap = capture(MockSignal("dc", amplitude_v=1.0, noise_v=0.001))
    stats = measure(cap)
    assert stats["frequency_hz"] is None
    assert "No periodic signal" in stats["note"]


def test_noise_is_not_mistaken_for_a_signal():
    cap = capture(MockSignal("noise", noise_v=0.01), range_v=5.0)
    stats = measure(cap)
    assert stats["frequency_hz"] is None


def test_clipping_is_reported_not_hidden():
    cap = capture(MockSignal("sine", 1000.0, 4.0, noise_v=0.0), range_v=2.0)
    stats = measure(cap)
    assert stats["overrange"] is True
    assert "clips" in stats["note"]


def test_ac_coupling_removes_the_offset():
    cap = capture(MockSignal("sine", 1000.0, 1.0, offset_v=2.0, noise_v=0.0),
                  coupling="AC")
    assert measure(cap)["mean_v"] == pytest.approx(0.0, abs=0.05)


def test_sample_rate_is_the_actual_one_not_the_requested_one():
    """The mock snaps to the timebase grid exactly as a real driver does."""
    cap = capture(MockSignal("sine", 1000.0, 1.0), duration_s=0.01, samples=1000)
    requested = 1000 / 0.01
    assert cap.sample_rate_hz != requested
    assert cap.sample_rate_hz == pytest.approx(1 / cap.dt_s)


def test_single_period_reports_too_few_edges():
    cap = capture(MockSignal("sine", 1000.0, 1.0), duration_s=0.0008)
    stats = measure(cap)
    assert stats["frequency_hz"] is None
    assert "two rising edges" in stats["note"]


def test_edge_trigger_that_can_never_fire_times_out():
    backend = MockBackend(MockSignal("sine", 1000.0, 1.0))
    backend.open()
    backend.set_channel(ChannelConfig(5.0, "DC", True))
    backend.set_trigger(TriggerConfig("edge", threshold_v=3.0, auto_trigger_ms=0))
    with pytest.raises(ScopeError, match="Trigger never fired"):
        backend.capture_block(0.01, 4096)


def test_auto_trigger_rescues_a_threshold_that_never_fires():
    backend = MockBackend(MockSignal("sine", 1000.0, 1.0))
    backend.open()
    backend.set_channel(ChannelConfig(5.0, "DC", True))
    backend.set_trigger(TriggerConfig("edge", threshold_v=3.0, auto_trigger_ms=100))
    assert backend.capture_block(0.01, 4096).volts.size == 4096


def test_downsample_keeps_the_spike():
    volts = np.zeros(10000)
    volts[4321] = 3.0  # a single-sample spike that every-Nth would drop
    curve = downsample_minmax(volts, 1e-6, 200)
    assert max(v for _, v in curve) == pytest.approx(3.0)
    assert len(curve) <= 220


def test_pick_range_refuses_to_clamp_silently():
    with pytest.raises(ScopeError, match="exceeds the largest range"):
        pick_range(50.0, (1.0, 2.0, 5.0))
    assert pick_range(1.5, (1.0, 2.0, 5.0)) == 2.0


# -- noise must never be reported as a frequency --------------------------
# An unconnected probe on ±0.5 V once came back as "456 Hz". Amplitude alone
# cannot tell a signal from noise on a narrow range; periodicity can.


def test_noise_reports_no_frequency_however_loud():
    for noise in (0.02, 0.05, 0.2):
        cap = capture(MockSignal("noise", noise_v=noise), range_v=0.5)
        stats = measure(cap)
        assert stats["frequency_hz"] is None, f"noise {noise} V became a frequency"
        assert "not periodic" in stats["note"] or "noise" in stats["note"]


def test_a_signal_buried_in_noise_is_refused_not_guessed():
    """30 % noise on a 1 kHz sine measured 3368 Hz before this; now it says no."""
    cap = capture(MockSignal("sine", 1000.0, 1.0, noise_v=0.30))
    stats = measure(cap)
    assert stats["frequency_hz"] is None
    assert stats["period_jitter_pct"] > analysis.MAX_JITTER_PCT


@pytest.mark.parametrize(
    "signal",
    [
        MockSignal("sine", 1000.0, 1.0, noise_v=0.10),
        MockSignal("square", 1000.0, 1.0, noise_v=0.01, duty_cycle_pct=30.0),
        MockSignal("ramp", 500.0, 1.0, noise_v=0.01),
        MockSignal("triangle", 2000.0, 1.0, noise_v=0.01),
    ],
)
def test_real_waveforms_keep_their_frequency(signal):
    """The noise gate must not cost us the signals it exists to protect."""
    stats = measure(capture(signal, duration_s=0.02, samples=16384))
    assert stats["frequency_hz"] == pytest.approx(signal.frequency_hz, rel=0.01)
    assert stats["period_jitter_pct"] < analysis.MAX_JITTER_PCT


def test_two_edges_of_noise_do_not_become_a_frequency():
    """One interval has zero jitter by definition — the shape test catches it."""
    rng = np.random.default_rng(3)
    volts = np.round(rng.normal(0, 1.2, 600)) / 32767 * 0.2
    cap = Capture("c", volts, 1 / 195312.5, 0.2, "DC", TriggerConfig(), False, 0.003, 600)
    stats = measure(cap)
    assert stats["frequency_hz"] is None


def test_a_slow_signal_with_few_cycles_is_still_measured():
    """Three periods is thin, but it is a signal and must not be thrown away."""
    stats = measure(capture(MockSignal("sine", 50.0, 1.0, noise_v=0.01), duration_s=0.06))
    assert stats["frequency_hz"] == pytest.approx(50.0, rel=0.01)
