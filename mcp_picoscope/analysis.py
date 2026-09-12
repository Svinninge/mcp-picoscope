# File version: v0.02
"""Waveform measurements: amplitude statistics, frequency, duty cycle.

Frequency comes from hysteresis mid-level crossings, not an FFT peak. A square
wave puts most of its energy in harmonics and a low-frequency signal may not
complete two periods in the window; crossings handle both, and they give duty
cycle for free. The hysteresis band is what keeps noise from counting as edges.
"""

from __future__ import annotations

import math

import numpy as np

from .scope import Capture

# Crossing detector. The signal must swing past +/- HYSTERESIS_FRAC of the
# half-amplitude before the opposite edge counts, and must swing at least
# MIN_SWING_FRAC of the voltage range at all to be called a signal rather than
# a flat line with noise on it.
HYSTERESIS_FRAC = 0.25
MIN_SWING_FRAC = 0.02

# Samples this close to the full-scale rail are treated as clipped.
OVERRANGE_FRAC = 0.995

# Significant digits in anything that leaves this module. An 8-bit ADC resolves
# one part in 256, and a frequency from a few hundred edges is good to maybe
# five figures — so 0.008636738181707206 V claims eleven digits of precision
# the instrument does not have, and costs a caller's context window for the
# privilege. Curves get one digit less than statistics: they are drawn, not read.
STAT_DIGITS = 6
CURVE_DIGITS = 5


def round_sig(value: float | None, digits: int = STAT_DIGITS) -> float | None:
    """Round to significant digits, spanning µV to 20 V and ns to seconds.

    Decimal places cannot do this job: the same measurement set holds 2.5e-5 V
    and 1.9e+4 Hz, and any fixed number of decimals mangles one end or the other.
    """
    if value is None:
        return None
    if not math.isfinite(value):
        return float(value)
    if value == 0:
        return 0.0  # float(), not the numpy scalar that walked in
    return float(f"{value:.{digits}g}")


def crossings(volts: np.ndarray, dt_s: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Rising and falling mid-level crossing times, plus the level used.

    The level is the midpoint between min and max, not the mean: a 10 % duty
    cycle square wave has a mean far from its own midpoint, and measuring duty
    against the mean would report every such wave as roughly 50 %.
    """
    if volts.size < 2:
        return np.empty(0), np.empty(0), 0.0

    vmin, vmax = float(volts.min()), float(volts.max())
    level = 0.5 * (vmin + vmax)
    half = 0.5 * (vmax - vmin)
    hysteresis = HYSTERESIS_FRAC * half
    high_gate, low_gate = level + hysteresis, level - hysteresis

    rising: list[float] = []
    falling: list[float] = []
    state = "high" if volts[0] > level else "low"

    for i in range(1, volts.size):
        v = volts[i]
        if state == "low" and v >= high_gate:
            t = _interpolate_crossing(volts, i, level, rising_edge=True)
            if t is not None:
                rising.append(t * dt_s)
            state = "high"
        elif state == "high" and v <= low_gate:
            t = _interpolate_crossing(volts, i, level, rising_edge=False)
            if t is not None:
                falling.append(t * dt_s)
            state = "low"

    return np.array(rising), np.array(falling), level


def _interpolate_crossing(
    volts: np.ndarray, i: int, level: float, *, rising_edge: bool
) -> float | None:
    """Fractional sample index where the run ending at `i` crossed `level`.

    Walks back from the gate crossing to the sample pair that straddles the
    level, so the reported time is the edge itself and not the point where the
    signal happened to clear the hysteresis band.
    """
    k = i
    while k > 0:
        prev, cur = volts[k - 1], volts[k]
        if rising_edge and prev < level <= cur:
            break
        if not rising_edge and prev > level >= cur:
            break
        k -= 1
    else:
        return None

    prev, cur = volts[k - 1], volts[k]
    span = cur - prev
    frac = 0.0 if span == 0 else (level - prev) / span
    return (k - 1) + float(frac)


def measure(capture: Capture) -> dict:
    """Vpp, Vmin/Vmax, mean, RMS, frequency, period and duty cycle.

    Frequency, period and duty are None when the record holds no usable edges
    (a DC level, or a swing below the noise floor) — an honest None beats a
    number derived from noise.
    """
    v = np.asarray(capture.volts, dtype=float)
    if v.size == 0:
        raise ValueError("capture holds no samples")

    vmin, vmax = float(v.min()), float(v.max())
    vpp = vmax - vmin
    result: dict = {
        "samples": int(v.size),
        "sample_rate_hz": capture.sample_rate_hz,
        "duration_s": capture.duration_s,
        "range_v": capture.range_v,
        "vmin_v": vmin,
        "vmax_v": vmax,
        "vpp_v": vpp,
        "mean_v": float(v.mean()),
        "rms_v": float(np.sqrt(np.mean(v**2))),
        "stdev_v": float(v.std()),
        "overrange": capture.overrange,
        "frequency_hz": None,
        "period_s": None,
        "duty_cycle_pct": None,
        "cycles_in_record": 0,
        "note": "",
    }

    if vpp < MIN_SWING_FRAC * capture.range_v:
        result["note"] = (
            "No periodic signal detected: the swing is under "
            f"{MIN_SWING_FRAC:.0%} of the {capture.range_v} V range. "
            "Looks like DC or noise; try a smaller range_v."
        )
        return _rounded(result)

    rising, falling, _level = crossings(v, capture.dt_s)
    if rising.size < 2:
        result["note"] = (
            "Fewer than two rising edges in the record — capture a longer "
            "duration_s to measure frequency."
        )
        return _rounded(result)

    periods = np.diff(rising)
    period = float(periods.mean())
    result["period_s"] = period
    result["frequency_hz"] = 1.0 / period
    result["cycles_in_record"] = int(periods.size)
    result["period_jitter_pct"] = float(100.0 * periods.std() / period)

    # Duty cycle: high time from each rising edge to the next falling edge.
    highs = [
        float(falling[falling > r][0] - r) for r in rising if np.any(falling > r)
    ]
    if highs:
        result["duty_cycle_pct"] = float(100.0 * np.mean(highs) / period)

    if capture.overrange:
        result["note"] = (
            "Signal clips against the range limit — measurements are "
            f"understated. Re-capture on a range above {capture.range_v} V."
        )
    return _rounded(result)


def _rounded(result: dict) -> dict:
    """Round every float in a measurement dict; leave ints, bools and text."""
    return {
        k: round_sig(v) if isinstance(v, float) else v
        for k, v in result.items()
    }


def detect_overrange(volts: np.ndarray, range_v: float) -> bool:
    """True when any sample sits at the full-scale rail."""
    return bool(np.any(np.abs(volts) >= OVERRANGE_FRAC * range_v))


def downsample_minmax(
    volts: np.ndarray, dt_s: float, points: int, digits: int = CURVE_DIGITS
) -> list[list[float]]:
    """Decimate to ~`points` [time, volt] pairs, keeping the extremes.

    Every N-th sample would drop the spikes, which is the one thing an
    oscilloscope exists to show. Each bucket contributes its min and its max in
    the order they occur, so an envelope survives the trip through the context
    window.
    """
    n = volts.size
    if points <= 0:
        raise ValueError("points must be positive")
    r = lambda x: round_sig(x, digits)  # noqa: E731 - local shorthand, used 6x
    if n <= points:
        return [[r(i * dt_s), r(float(v))] for i, v in enumerate(volts)]

    buckets = max(1, points // 2)
    edges = np.linspace(0, n, buckets + 1, dtype=int)
    out: list[list[float]] = []
    for start, stop in zip(edges[:-1], edges[1:]):
        if stop <= start:
            continue
        chunk = volts[start:stop]
        i_min = start + int(chunk.argmin())
        i_max = start + int(chunk.argmax())
        first, second = sorted((i_min, i_max))
        out.append([r(first * dt_s), r(float(volts[first]))])
        if second != first:
            out.append([r(second * dt_s), r(float(volts[second]))])
    return out
