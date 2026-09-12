# File version: v0.01
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
AUTOSET_SURVEY_S = 0.1
AUTOSET_SURVEY_SAMPLES = 4096
AUTOSET_PERIODS = 5


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
        survey = backend.capture_block(AUTOSET_SURVEY_S, AUTOSET_SURVEY_SAMPLES)
        survey.capture_id = "survey"
        stats = measure(survey)
        steps.append(f"surveyed on ±{ranges[-1]} V: Vpp {stats['vpp_v']:.4g} V")

        duration = AUTOSET_SURVEY_S
        if stats["frequency_hz"]:
            duration = AUTOSET_PERIODS / stats["frequency_hz"]
            steps.append(
                f"measured {stats['frequency_hz']:.6g} Hz → {AUTOSET_PERIODS} "
                f"periods = {duration:.6g} s"
            )
        else:
            steps.append("no periodic signal found; keeping the survey timebase")

        peak = max(abs(stats["vmin_v"]), abs(stats["vmax_v"])) * AUTOSET_HEADROOM
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
