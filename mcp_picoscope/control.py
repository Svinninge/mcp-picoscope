# File version: v0.02
"""Actions that change the instrument, shared by the MCP tools and the display.

Autoset is the first thing the page is allowed to do rather than just watch,
and it sets the pattern for whatever follows (issue #1). Three rules make that
safe:

  * one implementation, called by both surfaces — a second copy would drift,
    and the two callers would disagree about what the scope is doing;
  * the session lock is taken here, so a click and a tool call cannot interleave
    halfway through a sequence of captures;
  * whatever is changed lands in the session, so picoscope://state tells the
    truth afterwards and the LLM is not reasoning about a range somebody else
    just changed.

Nothing here may drive the outside world. A scope is a passive listener, and
the PS2104 has no signal generator to misuse.
"""

from __future__ import annotations

import logging

from .analysis import downsample_minmax, measure
from .scope import ChannelConfig, ScopeSession, TriggerConfig

log = logging.getLogger(__name__)

# Points in the decimated curve returned with a capture. Two per bucket, so
# this is ~100 buckets: enough to see the shape, small enough to read.
CURVE_POINTS = 200

# Autoset headroom: the range must hold the peak with margin, or the next
# capture clips the moment the signal drifts.
AUTOSET_HEADROOM = 1.2
AUTOSET_SURVEY_SAMPLES = 4096
AUTOSET_PERIODS = 5

# Autoset hunts across timebases the way the button on a bench scope does. One
# survey window cannot work: 0.1 s over 4096 samples is 41 kS/s, which aliases
# anything above a few kHz — an 11.8 kHz sine first came back as "no periodic
# signal", and then, from a slower window, as a confident 406 Hz.
#
# The ladder therefore runs FAST TO SLOW, and that direction matters more than
# the numbers in it. Too fast a window shows too few edges and is rejected for
# saying nothing; too slow a window aliases and is rejected for lying, which is
# far worse. Going fast first means the first window that resolves the signal
# wins, and a frequency is only believed when the sample rate is well above it.
AUTOSET_SURVEY_WINDOWS_S = (2e-5, 2e-4, 0.002, 0.02, 0.2)
AUTOSET_SURVEY_S = AUTOSET_SURVEY_WINDOWS_S[-1]
# Below this the record is aliased, whatever the crossings claim. Ten samples
# per period is already coarse for drawing; it is plenty for believing.
AUTOSET_MIN_SAMPLES_PER_PERIOD = 10


def autoset(session: ScopeSession) -> dict:
    """Find a range and timebase that show the signal — the AutoSetup button.

    Surveys on the widest range, measures the frequency, then re-captures about
    five periods on the smallest range that holds the peaks with headroom.
    Surveying wide first is not politeness: a capture that clips measures short,
    and a range chosen from a clipped survey would stay too small forever.
    """
    with session.lock:
        backend = session.require_open()
        ranges = sorted(session.device.voltage_ranges_v)  # type: ignore[union-attr]
        steps: list[str] = []

        backend.set_channel(ChannelConfig(ranges[-1], session.channel.coupling, True))
        backend.set_trigger(TriggerConfig(mode="auto"))

        frequency = None
        peak_v = 0.0
        tried = []
        for window in AUTOSET_SURVEY_WINDOWS_S:
            survey = backend.capture_block(window, AUTOSET_SURVEY_SAMPLES)
            survey.capture_id = "survey"
            stats = measure(survey)
            # Amplitude from every survey, not only the one that found the
            # frequency: an aliased record still samples the peaks.
            peak_v = max(peak_v, abs(stats["vmin_v"]), abs(stats["vmax_v"]))
            tried.append(f"{window * 1e3:.4g} ms")
            candidate = stats["frequency_hz"]
            if candidate and (
                stats["sample_rate_hz"] / candidate >= AUTOSET_MIN_SAMPLES_PER_PERIOD
            ):
                frequency = candidate
                break

        steps.append(
            f"surveyed on ±{ranges[-1]} V over {', '.join(tried)}: "
            f"peak {peak_v:.4g} V"
        )

        duration = AUTOSET_SURVEY_S
        if frequency:
            duration = AUTOSET_PERIODS / frequency
            steps.append(
                f"measured {frequency:.6g} Hz → {AUTOSET_PERIODS} "
                f"periods = {duration:.6g} s"
            )
        else:
            steps.append(
                "no periodic signal at any timebase; keeping the slowest one"
            )

        peak = peak_v * AUTOSET_HEADROOM
        chosen = next((r for r in ranges if r >= peak), ranges[-1])
        applied = backend.set_channel(
            ChannelConfig(chosen, session.channel.coupling, True)
        )
        session.channel = applied
        steps.append(f"picked ±{applied.range_v} V ({AUTOSET_HEADROOM:g}× headroom)")

        capture = backend.capture_block(duration, AUTOSET_SURVEY_SAMPLES)
        capture.capture_id = session.next_capture_id()
        session.store(capture)
        log.info("autoset: %s", "; ".join(steps))
        return {
            "capture_id": capture.capture_id,
            "steps": steps,
            "range_v": applied.range_v,
            "measurements": measure(capture),
            "curve": downsample_minmax(capture.volts, capture.dt_s, CURVE_POINTS),
        }


def capture_block(session: ScopeSession, duration_s: float, samples: int) -> dict:
    """Capture one block and return statistics plus a decimated curve."""
    with session.lock:
        backend = session.require_open()
        capture = backend.capture_block(duration_s, samples)
        capture.capture_id = session.next_capture_id()
        session.store(capture)
        log.info(
            "%s: %d samples at %.6g S/s",
            capture.capture_id,
            capture.volts.size,
            capture.sample_rate_hz,
        )
        return {
            "capture_id": capture.capture_id,
            "measurements": measure(capture),
            "curve": downsample_minmax(capture.volts, capture.dt_s, CURVE_POINTS),
            "curve_note": (
                f"{len(capture.volts)} samples decimated to ~{CURVE_POINTS} "
                "[time_s, volt] pairs, min/max per bucket so spikes survive."
            ),
        }
