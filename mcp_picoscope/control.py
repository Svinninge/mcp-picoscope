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

    def status(self) -> dict:
        thread = self._thread
        with self._guard:
            return {
                "running": bool(thread and thread.is_alive()),
                "mode": self._mode,
                "window_s": round_sig(self._window_s, 5),
                "sweeps": self._sweeps,
                "error": self._error,
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
            capture = backend.capture_block(self._window_s, SWEEP_SAMPLES)
            capture.capture_id = self.session.next_capture_id()
            self.session.store(capture)
            return measure(capture)

    def _retune(self, stats: dict) -> None:
        """Follow the signal, so a faster one does not draw as a green block."""
        freq = stats.get("frequency_hz")
        if not freq:
            return
        wanted = _clamp_window(SWEEP_PERIODS_ON_SCREEN / freq)
        with self._guard:
            current = self._window_s
            if max(wanted / current, current / wanted) > SWEEP_RETUNE_RATIO:
                self._window_s = wanted
                log.info("sweep follows %.6g Hz → %.4g ms window", freq, wanted * 1e3)


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
        return {"running": False, "mode": "stop", "window_s": None, "sweeps": 0, "error": ""}
    return _runner.status()
