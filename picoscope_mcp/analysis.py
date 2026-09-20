# File version: v0.03
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

# Amplitude alone cannot tell a signal from noise: on a narrow range, noise
# clears MIN_SWING_FRAC easily — an unconnected probe on ±0.5 V once came back
# as "456 Hz". Periodicity is what separates them, and the gap is wide.
# Measured 2026-09-12, period jitter (std/mean of the intervals between rising
# edges): clean sine, square, ramp and triangle 0.06-0.34 %; a sine under 10 %
# noise 0.71 %; pure noise from the mock 58-73 %; an unconnected PS2104 probe
# 82-105 %. Twenty per cent sits in the empty middle, closer to the noise.
MAX_JITTER_PCT = 20.0
# Jitter needs intervals to compare. Two rising edges give one interval and a
# standard deviation of zero, which would read as a perfectly steady signal —
# and an unconnected probe did exactly that, reporting 3756 Hz at 0 % jitter.
MIN_CYCLES_FOR_JITTER = 3

# Fallback for those few-cycle records: the shape of the amplitude
# distribution. Vpp/stdev is 2.0 for a square, 2.83 for a sine, 3.5 for a ramp
# or triangle, and 5-7 for noise, whose extremes over thousands of samples
# reach far past one standard deviation (measured 2026-09-12, same survey).
# A narrow pulse train also scores high, so this is used ONLY when there are
# too few cycles to judge periodicity properly — capture longer and jitter,
# which handles pulse trains correctly, takes over.
MAX_NOISE_CREST = 4.5

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
    jitter = float(100.0 * periods.std() / period)
    result["cycles_in_record"] = int(periods.size)
    result["period_jitter_pct"] = jitter

    if periods.size >= MIN_CYCLES_FOR_JITTER and jitter > MAX_JITTER_PCT:
        # The edges are there but they are not periodic. Reporting their mean
        # as a frequency would invent a number: an instrument that makes one up
        # is worse than one that says it does not know.
        result["note"] = (
            f"Edges are not periodic — {jitter:.0f} % variation between them, "
            f"against {MAX_JITTER_PCT:.0f} % allowed. This is noise, not a "
            f"signal. Vpp is only {result['vpp_v']:.4g} V on a "
            f"{capture.range_v} V range."
        )
        return _rounded(result)

    result["period_s"] = period
    result["frequency_hz"] = 1.0 / period

    # Duty cycle: high time from each rising edge to the next falling edge.
    highs = [
        float(falling[falling > r][0] - r) for r in rising if np.any(falling > r)
    ]
    if highs:
        result["duty_cycle_pct"] = float(100.0 * np.mean(highs) / period)

    if periods.size < MIN_CYCLES_FOR_JITTER:
        crest = result["vpp_v"] / result["stdev_v"] if result["stdev_v"] else 0.0
        if crest > MAX_NOISE_CREST:
            result["period_s"] = None
            result["frequency_hz"] = None
            result["duty_cycle_pct"] = None
            result["note"] = (
                f"Too few periods ({periods.size}) to check that the signal "
                f"repeats, and its shape says noise: Vpp is {crest:.1f}x the "
                f"standard deviation, where a real waveform is 2-3.5x. No "
                "frequency reported. Capture a longer duration_s if there is a "
                "slow signal here."
            )
        else:
            result["note"] = (
                f"Only {periods.size} period(s) in the record, too few to check "
                "that the signal is periodic — the frequency could be noise. "
                "Capture a longer duration_s to confirm it."
            )

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
