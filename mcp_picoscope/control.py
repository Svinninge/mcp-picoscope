# File version: v0.03
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
import threading
import time

from .analysis import downsample_minmax, measure, round_sig
from .scope import ChannelConfig, ScopeError, ScopeSession, TriggerConfig

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

# -- sweep engine ---------------------------------------------------------
# Who drives the acquisition was the open question in issue #1. Until now
# nothing captured unless a tool call asked for it, so a display could only be
# as live as its caller. A sweep is a thread that captures on its own, which
# means it must answer for itself on three counts: it must be stoppable, it
# must never hold the session lock across a loop (the lock is taken per
# capture, so an MCP call always gets in between), and it must survive a
# capture failing — a trigger that does not fire is a normal state of the
# instrument, not a crash.

SWEEP_MODES = ("single", "auto", "normal")
# A pause between captures. The scope can go faster, but the display redraws at
# 400 ms and the MCP session needs the lock more than the screen needs frames.
SWEEP_INTERVAL_S = 0.15
# After a failed capture, back off before trying again so a scope that has been
# unplugged does not spin.
SWEEP_ERROR_BACKOFF_S = 1.0
SWEEP_SAMPLES = 4096
# Roughly a screen's worth of signal, and the bounds of what a window may be.
SWEEP_PERIODS_ON_SCREEN = 10
SWEEP_MIN_WINDOW_S = 20e-6
SWEEP_MAX_WINDOW_S = 0.2
SWEEP_START_WINDOW_S = 0.02
# Only retune when the window is off by more than this, or the timebase twitches
# on every capture as the last digit of the measured frequency wobbles.
SWEEP_RETUNE_RATIO = 1.5
# A window can trap itself: too short to hold two edges means no frequency,
# and no frequency means nothing ever widens it again. Seen live, stuck at the
# 20 us minimum while an 800 Hz signal sat on the probe — 0.066 of a period per
# capture. After this many sweeps without a frequency, widen and try again.
SWEEP_MISSES_BEFORE_WIDENING = 3
SWEEP_WIDEN_FACTOR = 8.0
# How long one sweep may wait for a trigger while holding the session lock.
# The driver needs the lock for the whole capture, so a capture waiting on a
# trigger that never fires held it for the backend's full 6 s — and the
# display's state read waits on that same lock, so the whole window froze
# (measured). NORMAL mode loses nothing by waiting in short turns: the loop
# simply tries again, and between turns everything else gets the lock.
SWEEP_TRIGGER_WAIT_S = 1.0

# Timebase steps in seconds per division, the 1-2-5 sequence of a bench scope.
# Ten divisions across, so the window is ten times the step. The ends follow
# the sweep's window limits; the display always shows the timebase the driver
# actually delivered, which snaps to its own grid and can be up to 2x wider.
DIVISIONS = 10
TIME_PER_DIV_STEPS = (
    10e-6, 20e-6, 50e-6,
    100e-6, 200e-6, 500e-6,
    1e-3, 2e-3, 5e-3,
    10e-3, 20e-3,
)
# Fewer samples per period than this and the trace may alias — the same bound
# autoset uses before it believes a frequency.
ALIAS_MIN_SAMPLES_PER_PERIOD = 10

# What an edge trigger's rescue is set to when leaving NORMAL for AUTO.
NORMAL_RESCUE_MS = 1000


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

        # The survey must be free-running: an armed trigger that does not fire
        # would time out on a signal autoset has not found yet. The user's
        # setting is put back at the end, with the level moved to the signal.
        incoming = session.trigger
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

        # A running sweep keeps its own window, so autoset has to hand its
        # choice over or the next sweep undoes it a tenth of a second later.
        if _runner is not None and _runner.session is session:
            _runner.set_window(duration, for_hz=frequency)

        capture = backend.capture_block(duration, AUTOSET_SURVEY_SAMPLES)
        capture.capture_id = session.next_capture_id()
        session.store(capture)

        # Half of peak-to-peak: where a signal spends least time and its slope
        # is steepest, which is where a trigger is steadiest. Zero is the wrong
        # default for anything with an offset — a 0..3 V signal would never
        # cross it. The mode the user had is restored around it.
        stats = measure(capture)
        level = round_sig((stats["vmin_v"] + stats["vmax_v"]) / 2, 5)
        applied_trigger = set_trigger(
            session,
            mode=incoming.mode,
            threshold_v=level,
            direction=incoming.direction,
            delay_pct=incoming.delay_pct,
            auto_trigger_ms=incoming.auto_trigger_ms,
        )
        steps.append(f"trigger level {level:.4g} V (half of peak-to-peak)")

        log.info("autoset: %s", "; ".join(steps))
        return {
            "trigger": applied_trigger,
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


COUPLINGS = ("DC", "AC")
# Switching to AC charges the coupling capacitor, and until it settles the
# signal's midpoint drifts. Measured on the PS2104: +0.24 V at 1.08 s, +0.02 V
# at 1.25 s, settled by 1.42 s. The first version took one capture straight
# after the switch and set the trigger to 1.258 V on a signal centred at 0 V —
# it only still fired because that happened to sit below the 1.45 V peak.
# So captures are repeated until the midpoint stops moving: two readings within
# SETTLE_LSB steps of the ADC, spaced far enough apart to see a decay.
SETTLE_LSB = 2
SETTLE_INTERVAL_S = 0.15
SETTLE_TIMEOUT_S = 3.0


def configure_channel(
    session: ScopeSession,
    range_v: float | None = None,
    coupling: str | None = None,
    enabled: bool | None = None,
) -> dict:
    """Set channel A, from either surface. Unset arguments keep their value.

    Changing the coupling moves the signal: a 0..3 V sine has its midpoint at
    1.5 V in DC and at 0 V in AC. An edge trigger armed at 1.5 V would never fire
    after switching to AC, and the sweep would sit "waiting for trigger" on a
    perfectly good signal. So a coupling change takes one capture and moves the
    trigger level to the new midpoint, keeping the mode and the direction.

    The range is deliberately left alone. Switching AC back to DC on a narrow
    range can clip, and the capture's own overrange note says so — silently
    widening the range would hide the choice from the person who made it.
    """
    with session.lock:
        backend = session.require_open()
        current = session.channel
        new_coupling = (coupling or current.coupling).upper()
        if new_coupling not in COUPLINGS:
            raise ScopeError(
                f"coupling must be one of {', '.join(COUPLINGS)}, got {coupling!r}."
            )
        applied = backend.set_channel(
            ChannelConfig(
                range_v if range_v is not None else current.range_v,
                new_coupling,
                enabled if enabled is not None else current.enabled,
            )
        )
        session.channel = applied
        reply = {
            "range_v": applied.range_v,
            "coupling": applied.coupling,
            "enabled": applied.enabled,
            "requested_range_v": range_v,
            "available_ranges_v": list(session.device.voltage_ranges_v),  # type: ignore[union-attr]
        }

        if applied.coupling != current.coupling:
            # The captures that find the new level must not wait for the old
            # one. After AC → DC an edge trigger at -0.02 V never fires on a
            # 0..3 V signal, and in NORMAL mode the capture waited for it and
            # the switch failed (measured). Survey free-running, as autoset does,
            # then give the user's mode back around the new level.
            trigger = session.trigger
            backend.set_trigger(TriggerConfig(mode="auto"))
            try:
                stats, settled = _settled_capture(session, applied.range_v)
            except ScopeError:
                backend.set_trigger(trigger)  # never leave it free-running
                raise
            level = round_sig((stats["vmin_v"] + stats["vmax_v"]) / 2, 5)
            reply["trigger"] = set_trigger(
                session,
                mode=trigger.mode,
                threshold_v=level,
                direction=trigger.direction,
                delay_pct=trigger.delay_pct,
                auto_trigger_ms=trigger.auto_trigger_ms,
            )
            reply["overrange"] = stats["overrange"]
            reply["settled"] = settled
            reply["note"] = (
                f"Coupling {current.coupling} → {applied.coupling}; trigger level "
                f"moved to {level:.4g} V, the new midpoint of the signal."
                + (" The signal now clips on this range — widen it or run autoset."
                   if stats["overrange"] else "")
                + ("" if settled else
                   f" The input had not settled after {SETTLE_TIMEOUT_S:g} s, so the"
                   " level may be off; drag it or run autoset.")
            )
        return reply


def _settled_capture(session: ScopeSession, range_v: float) -> tuple[dict, bool]:
    """Capture until the signal's midpoint stops moving. Returns (stats, settled).

    Called with the session lock held. The tolerance is in ADC steps of the
    current range, because "stopped moving" means "no longer resolvable as
    moving", and that is a property of the range, not a fixed voltage.
    """
    backend = session.require_open()
    tolerance = SETTLE_LSB * (2 * range_v / 256)
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    previous: float | None = None
    while True:
        capture = backend.capture_block(0.02, 4096)
        capture.capture_id = session.next_capture_id()
        session.store(capture)
        stats = measure(capture)
        mid = (stats["vmin_v"] + stats["vmax_v"]) / 2
        if previous is not None and abs(mid - previous) <= tolerance:
            return stats, True
        if time.monotonic() > deadline:
            return stats, False
        previous = mid
        time.sleep(SETTLE_INTERVAL_S)


def stop_for_shutdown() -> None:
    """Stop any sweep before the device is closed or the server exits."""
    if _runner is not None:
        _runner.stop()


def set_trigger(
    session: ScopeSession,
    mode: str = "auto",
    threshold_v: float = 0.0,
    direction: str = "rising",
    delay_pct: float = 0.0,
    auto_trigger_ms: int = 1000,
) -> dict:
    """Set the trigger, from either surface.

    The backend validates: a threshold outside the selected range could never
    fire, and saying so beats a capture that times out for a reason nobody can
    see from the numbers.
    """
    with session.lock:
        backend = session.require_open()
        # A level dragged with a mouse arrives as 1.7111404667547336 V. The
        # instrument resolves one part in 256 of the range; the extra digits are
        # pixel noise, and they read as false precision everywhere they land.
        applied = backend.set_trigger(
            TriggerConfig(
                mode, round_sig(threshold_v, 5), direction, delay_pct, auto_trigger_ms
            )
        )
        session.trigger = applied
        return {
            "mode": applied.mode,
            "threshold_v": applied.threshold_v,
            "direction": applied.direction,
            "delay_pct": applied.delay_pct,
            "auto_trigger_ms": applied.auto_trigger_ms,
        }


def suggested_level(session: ScopeSession) -> float:
    """A trigger level worth arming: the midpoint of the last capture.

    Zero is the wrong default for anything with an offset — the bench signal
    sits at 0..3 V, and a trigger at 0 V would never fire on it.
    """
    capture = next(reversed(session.captures.values()), None)
    if capture is None:
        return 0.0
    stats = measure(capture)
    mid = (stats["vmin_v"] + stats["vmax_v"]) / 2
    return round(mid, 4)


class SweepRunner:
    """Captures on its own until told to stop.

    One thread, one session. The lock is taken inside each capture, never
    around the loop, so a tool call always gets a turn between sweeps.
    """

    def __init__(self, session: ScopeSession) -> None:
        self.session = session
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._mode = "stop"
        self._window_s = SWEEP_START_WINDOW_S
        self._error = ""
        self._sweeps = 0
        self._misses = 0
        # The frequency the current window was chosen for. Retuning compares
        # frequencies, not window lengths: comparing lengths cannot tell "the
        # signal changed" from "somebody deliberately picked a different number
        # of periods", so autoset's choice was overwritten 150 ms later.
        self._tuned_for_hz: float | None = None
        # Who owns the timebase: "auto" follows the signal, "manual" is a
        # time/div somebody chose and nothing else may overwrite.
        self._timebase_mode = "auto"
        self._manual_per_div_s: float | None = None
        # The last frequency measured while the timebase was chosen to resolve
        # it. A manual timebase that is too slow measures an ALIAS — a wrong,
        # low frequency with apparently plenty of samples per period — so the
        # aliasing warning must never be computed from a manual capture.
        self._reference_hz: float | None = None
        self._timebase_warning = ""
        self._guard = threading.Lock()  # guards the fields above, not the device

    # -- control -----------------------------------------------------------

    def start(self, mode: str, window_s: float | None = None) -> dict:
        if mode not in SWEEP_MODES:
            raise ScopeError(
                f"Unknown sweep mode {mode!r}. Valid: {', '.join(SWEEP_MODES)}."
            )
        self.session.require_open()
        self.stop()  # never two threads on one instrument
        self._apply_mode_to_trigger(mode)
        with self._guard:
            self._mode = mode
            self._error = ""
            self._sweeps = 0
            if window_s:
                self._window_s = _clamp_window(window_s)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"picoscope-sweep-{mode}", daemon=True
        )
        self._thread.start()
        log.info("sweep started: %s", mode)
        return self.status()

    def _apply_mode_to_trigger(self, mode: str) -> None:
        """Map the sweep mode onto the hardware trigger, as a bench scope does.

        AUTO sweeps even when nothing triggers, so an edge trigger keeps its
        auto_trigger rescue. NORMAL only sweeps on a real trigger, so the rescue
        is switched off and a capture that never triggers times out, which the
        loop reports as "waiting" instead of treating as a fault. Neither mode
        touches the level or the direction — those are the user's.
        """
        trigger = self.session.trigger
        if mode == "normal" and trigger.mode != "edge":
            # NORMAL means "only on a trigger", so it has to arm one. Without
            # this the button was a no-op from a free-running start, which is
            # the state the scope is in when it opens.
            set_trigger(
                self.session, "edge", suggested_level(self.session),
                trigger.direction, trigger.delay_pct, 0,
            )
            return
        if trigger.mode != "edge":
            return  # a free-running trigger already means "sweep regardless"
        if mode == "normal" and trigger.auto_trigger_ms != 0:
            set_trigger(
                self.session, "edge", trigger.threshold_v, trigger.direction,
                trigger.delay_pct, 0,
            )
        elif mode in ("auto", "single") and trigger.auto_trigger_ms == 0:
            set_trigger(
                self.session, "edge", trigger.threshold_v, trigger.direction,
                trigger.delay_pct, NORMAL_RESCUE_MS,
            )

    def stop(self) -> dict:
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._stop.set()
            # Long enough for a capture in flight to finish and notice; short
            # enough that a wedged driver cannot hang the caller forever.
            thread.join(timeout=5.0)
            if thread.is_alive():
                log.warning("sweep thread did not stop within 5 s")
        self._thread = None
        with self._guard:
            self._mode = "stop"
        return self.status()

    def set_window(self, seconds: float, for_hz: float | None = None) -> None:
        """Adopt a window chosen elsewhere — autoset, or a future time/div.

        Without this, autoset is invisible while a sweep is running: it picks a
        range and a timebase, and the sweep overwrites the timebase with its own
        within 150 ms. Whoever owns the timebase has to be one thing. Passing
        the frequency it was chosen for keeps the choice until the signal itself
        changes.
        """
        with self._guard:
            self._window_s = _clamp_window(seconds)
            self._tuned_for_hz = for_hz
            self._misses = 0
            # Autoset means "choose for me": it takes the timebase back.
            self._timebase_mode = "auto"
            self._manual_per_div_s = None
            self._timebase_warning = ""
            if for_hz:
                self._reference_hz = for_hz

    def set_time_per_div(self, per_div_s: float | None) -> None:
        """Choose the timebase by hand, or hand it back with None."""
        with self._guard:
            if per_div_s is None:
                self._timebase_mode = "auto"
                self._manual_per_div_s = None
                self._timebase_warning = ""
                self._tuned_for_hz = None  # retune on the next capture
                return
            window = _clamp_window(per_div_s * DIVISIONS)
            self._timebase_mode = "manual"
            self._manual_per_div_s = window / DIVISIONS
            self._window_s = window
            self._misses = 0
            self._timebase_warning = self._alias_warning(None)

    def step_time_per_div(self, direction: int) -> float:
        """Move one step along the 1-2-5 sequence from what is on screen now."""
        with self._guard:
            current = (
                self._manual_per_div_s
                if self._manual_per_div_s is not None
                else self._window_s / DIVISIONS
            )
        target = _next_step(current, direction)
        self.set_time_per_div(target)
        return target

    def _raise_reference(self, freq: float | None, sample_rate_hz: float | None) -> None:
        """Let a manual capture raise the trusted frequency, never lower it.

        An alias always reads LOWER than the real signal, so a higher frequency,
        resolved with enough samples per period, cannot be one: the signal itself
        got faster. Without this the reference stayed at the 10 kHz autoset had
        measured after the generator went to 1 MHz, and stepping to 0.2 ms/div
        showed a 562 kHz alias with no warning (measured). Called with the guard.
        """
        if not freq or not sample_rate_hz:
            return
        if sample_rate_hz / freq < ALIAS_MIN_SAMPLES_PER_PERIOD:
            return
        if self._reference_hz is None or freq > self._reference_hz * SWEEP_RETUNE_RATIO:
            self._reference_hz = freq

    def _alias_warning(self, sample_rate_hz: float | None) -> str:
        """Called with the guard held."""
        reference = self._reference_hz
        if self._timebase_mode != "manual" or not reference:
            return ""
        rate = sample_rate_hz or (SWEEP_SAMPLES / self._window_s)
        per_period = rate / reference
        if per_period >= ALIAS_MIN_SAMPLES_PER_PERIOD:
            return ""
        return (
            f"At this timebase the scope samples at {rate:.4g} S/s — about "
            f"{per_period:.1f} samples per period of the {reference:.6g} Hz signal "
            "last measured, fewer than it takes to trust the trace. It may alias: "
            "what you see can be a false, slower waveform. Shorten time/div or "
            "press Auto."
        )

    def status(self) -> dict:
        thread = self._thread
        with self._guard:
            return {
                "running": bool(thread and thread.is_alive()),
                "mode": self._mode,
                "window_s": round_sig(self._window_s, 5),
                "sweeps": self._sweeps,
                "error": self._error,
                "timebase_mode": self._timebase_mode,
                "time_per_div_s": round_sig(
                    self._manual_per_div_s
                    if self._manual_per_div_s is not None
                    else self._window_s / DIVISIONS,
                    5,
                ),
                "timebase_warning": self._timebase_warning,
            }

    # -- the loop ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                stats = self._one_sweep()
            except ScopeError as exc:
                # A trigger that never fires is a state, not a failure: say so
                # and keep sweeping. Anything else backs off before retrying.
                with self._guard:
                    self._error = str(exc)
                log.info("sweep: %s", exc)
                if self._stop.wait(SWEEP_ERROR_BACKOFF_S):
                    break
                continue
            except Exception:  # noqa: BLE001 - a thread that dies silently is worse
                log.exception("sweep loop failed")
                with self._guard:
                    self._error = "unexpected failure; sweep stopped"
                break

            with self._guard:
                self._error = ""
                self._sweeps += 1
                mode = self._mode
            self._retune(stats)
            if mode == "single":
                break
            if self._stop.wait(SWEEP_INTERVAL_S):
                break

        with self._guard:
            if self._mode == "single":
                self._mode = "stop"

    def _one_sweep(self) -> dict:
        with self.session.lock:
            backend = self.session.require_open()
            capture = backend.capture_block(
                self._window_s, SWEEP_SAMPLES, max_wait_s=SWEEP_TRIGGER_WAIT_S
            )
            capture.capture_id = self.session.next_capture_id()
            self.session.store(capture)
            return measure(capture)

    def _retune(self, stats: dict) -> None:
        """Follow the signal, and climb back out when the window is too short.

        With a manual timebase nothing here may move the window — that was the
        trap written into issue #1: the user sets time/div and the following
        undoes it two seconds later. It only re-checks the aliasing warning.
        """
        freq = stats.get("frequency_hz")
        with self._guard:
            if self._timebase_mode == "manual":
                self._raise_reference(freq, stats.get("sample_rate_hz"))
                self._timebase_warning = self._alias_warning(stats.get("sample_rate_hz"))
                return
        if not freq:
            self._widen_after_misses()
            return
        with self._guard:
            self._misses = 0
            reference = self._tuned_for_hz
            if reference and max(freq / reference, reference / freq) <= SWEEP_RETUNE_RATIO:
                return  # same signal; leave the window as somebody chose it
            self._reference_hz = freq  # measured on a timebase chosen for it
            wanted = _clamp_window(SWEEP_PERIODS_ON_SCREEN / freq)
            if reference is None or wanted != self._window_s:
                self._window_s = wanted
                self._tuned_for_hz = freq
                log.info("sweep follows %.6g Hz → %.4g ms window", freq, wanted * 1e3)

    def _widen_after_misses(self) -> None:
        """Widen the window when nothing periodic has been seen for a while.

        Without this the sweep can trap itself: a window too short to hold two
        edges reports no frequency, and no frequency means the window is never
        retuned — so it stays too short forever. Widening is safe: a window too
        long only draws more periods, while one too short shows nothing at all.
        """
        with self._guard:
            self._misses += 1
            if self._misses < SWEEP_MISSES_BEFORE_WIDENING:
                return
            self._misses = 0
            widened = _clamp_window(self._window_s * SWEEP_WIDEN_FACTOR)
            if widened == self._window_s:
                return  # already at the widest; nothing periodic is there
            self._window_s = widened
            log.info("sweep saw nothing periodic → widening to %.4g ms", widened * 1e3)


def _next_step(current_s: float, direction: int) -> float:
    """The neighbouring 1-2-5 step, clamped to the ends of the sequence.

    From a value between steps (the auto timebase rarely sits on one), a step
    up goes to the next step above it and a step down to the next below, so a
    single click always visibly changes the screen.
    """
    steps = TIME_PER_DIV_STEPS
    tolerance = 1e-9
    if direction > 0:
        above = [s for s in steps if s > current_s * (1 + tolerance)]
        return above[0] if above else steps[-1]
    below = [s for s in steps if s < current_s * (1 - tolerance)]
    return below[-1] if below else steps[0]


def step_range(session: ScopeSession, direction: int) -> dict:
    """One range up (+1, more volts per division) or down (-1) from the current.

    The display has eight vertical divisions, so volts/div is the full-scale
    range over four. The steps are the device's own ranges — 100 mV to 20 V on
    the PS2104 — rather than an invented 1-2-5 sequence, because the driver has
    no analogue gain in between: a label between ranges would be a lie.
    """
    with session.lock:
        session.require_open()
        ranges = sorted(session.device.voltage_ranges_v)  # type: ignore[union-attr]
        current = session.channel.range_v
    if direction > 0:
        wider = [r for r in ranges if r > current * (1 + 1e-9)]
        target = wider[0] if wider else ranges[-1]
    else:
        narrower = [r for r in ranges if r < current * (1 - 1e-9)]
        target = narrower[-1] if narrower else ranges[0]
    return configure_channel(session, range_v=target)


def set_time_per_div(session: ScopeSession, per_div_s: float | None) -> dict:
    """The timebase in seconds per division, or None to follow the signal."""
    if per_div_s is not None and per_div_s <= 0:
        raise ScopeError("time_per_div_s must be positive, or 0 for auto.")
    runner(session).set_time_per_div(per_div_s)
    return runner(session).status()


def step_time_per_div(session: ScopeSession, direction: int) -> dict:
    runner(session).step_time_per_div(1 if direction > 0 else -1)
    return runner(session).status()


def _clamp_window(seconds: float) -> float:
    return max(SWEEP_MIN_WINDOW_S, min(SWEEP_MAX_WINDOW_S, seconds))


_runner: SweepRunner | None = None
_runner_lock = threading.Lock()


def runner(session: ScopeSession) -> SweepRunner:
    """The one sweep runner. One instrument, one thread driving it."""
    global _runner
    with _runner_lock:
        if _runner is None or _runner.session is not session:
            _runner = SweepRunner(session)
        return _runner


def start_sweep(session: ScopeSession, mode: str = "auto", window_s: float | None = None) -> dict:
    return runner(session).start(mode, window_s)


def stop_sweep(session: ScopeSession) -> dict:
    return runner(session).stop()


def sweep_status(session: ScopeSession | None = None) -> dict:
    """Status without creating a runner — safe to call from the state handler."""
    if _runner is None:
        return {
            "running": False, "mode": "stop", "window_s": None, "sweeps": 0,
            "error": "", "timebase_mode": "auto", "time_per_div_s": None,
            "timebase_warning": "",
        }
    return _runner.status()
