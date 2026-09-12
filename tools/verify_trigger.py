# File version: v0.01
"""Verify the edge trigger against a real signal.

A trigger is not verified by a capture succeeding — a free-running capture
succeeds too. It is verified by the record STARTING AT THE SAME PLACE every
time: with an edge trigger at level L rising, every capture should begin near L
on a rising slope, while an untriggered one begins wherever the signal happened
to be. So this measures the spread of the first samples across many captures
and compares the two modes against each other.

It also checks the two failure paths, which are easy to write and easy to get
wrong: a threshold the signal never reaches must time out with a readable
message when auto_trigger_ms is 0, and must fall back to an untriggered capture
when it is not.

Run:  .\\.venv\\Scripts\\python.exe tools\\verify_trigger.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_picoscope import control  # noqa: E402
from mcp_picoscope.backends.ps2000 import PS2000Backend  # noqa: E402
from mcp_picoscope.scope import (  # noqa: E402
    ScopeError,
    ScopeSession,
    TriggerConfig,
)

CAPTURES = 12
PERIODS_ON_SCREEN = 4
# How far into the record the slope is measured. A hundredth of a period is
# short enough to still be on the trigger edge and long enough to beat noise.
SLOPE_FRACTION = 0.01


def start_stats(scope, duration_s: float, samples: int = 4096) -> tuple[list[float], list[float]]:
    """First sample and initial slope of each capture."""
    firsts: list[float] = []
    slopes: list[float] = []
    for _ in range(CAPTURES):
        cap = scope.capture_block(duration_s, samples)
        span = max(2, int(len(cap.volts) * SLOPE_FRACTION))
        firsts.append(float(cap.volts[0]))
        slopes.append(float(cap.volts[span] - cap.volts[0]))
    return firsts, slopes


def report(label: str, firsts: list[float], slopes: list[float]) -> float:
    spread = statistics.pstdev(firsts)
    rising = sum(1 for s in slopes if s > 0)
    print(
        f"{label:26s} start {statistics.mean(firsts):+.3f} V "
        f"±{spread:.3f} V   stigande {rising}/{len(slopes)}   "
        f"min {min(firsts):+.3f} max {max(firsts):+.3f}"
    )
    return spread


def main() -> int:
    scope = PS2000Backend()
    info = scope.open()
    print(f"{info.model} serienr {info.serial}\n")
    try:
        # Find the signal with autoset rather than a guessed window: one fixed
        # survey length only sees one decade, which is the whole reason autoset
        # hunts across timebases in the first place.
        session = ScopeSession()
        session.backend = scope
        session.device = info
        stats = control.autoset(session)["measurements"]
        if not stats["frequency_hz"]:
            print("ingen periodisk signal på kanal A — koppla in generatorn")
            return 1

        freq = stats["frequency_hz"]
        range_v = session.channel.range_v
        window = PERIODS_ON_SCREEN / freq
        mid = (stats["vmin_v"] + stats["vmax_v"]) / 2
        print(
            f"signal: {freq:.6g} Hz, {stats['vmin_v']:+.3f}..{stats['vmax_v']:+.3f} V, "
            f"mitt {mid:+.3f} V, område ±{range_v} V, fönster {window * 1e3:.4g} ms\n"
        )

        scope.set_trigger(TriggerConfig(mode="auto"))
        free = report("auto (fri) ", *start_stats(scope, window))

        scope.set_trigger(
            TriggerConfig("edge", threshold_v=mid, direction="rising", auto_trigger_ms=1000)
        )
        rising_spread = report("edge, stigande flank", *start_stats(scope, window))

        scope.set_trigger(
            TriggerConfig("edge", threshold_v=mid, direction="falling", auto_trigger_ms=1000)
        )
        falling, falling_slopes = start_stats(scope, window)
        report("edge, fallande flank", falling, falling_slopes)

        print()
        amplitude = stats["vmax_v"] - stats["vmin_v"]
        print(f"spridning fritt löpande: {free / amplitude * 100:.1f} % av Vpp")
        print(f"spridning med flanktrigg: {rising_spread / amplitude * 100:.1f} % av Vpp")
        triggered = rising_spread < free / 3
        print("triggen håller startpunkten:", "JA" if triggered else "NEJ")
        falling_ok = sum(1 for s in falling_slopes if s < 0) >= CAPTURES - 1
        print("fallande flank ger fallande start:", "JA" if falling_ok else "NEJ")

        print("\nfelvägar:")
        outside = range_v * 0.95
        scope.set_trigger(
            TriggerConfig("edge", threshold_v=outside, direction="rising", auto_trigger_ms=0)
        )
        try:
            scope.capture_block(window, 4096)
            print("  timeout vid omöjlig nivå: NEJ — fångsten returnerade ändå")
            timeout_ok = False
        except ScopeError as exc:
            print(f"  timeout vid omöjlig nivå: JA — {str(exc)[:80]}...")
            timeout_ok = True

        scope.set_trigger(
            TriggerConfig("edge", threshold_v=outside, direction="rising", auto_trigger_ms=200)
        )
        try:
            cap = scope.capture_block(window, 4096)
            rescue_ok = cap.volts.size > 0
            print(f"  auto_trigger räddar: {'JA' if rescue_ok else 'NEJ'} "
                  f"({cap.volts.size} sampel)")
        except ScopeError as exc:
            print(f"  auto_trigger räddar: NEJ — {exc}")
            rescue_ok = False

        print()
        ok = triggered and falling_ok and timeout_ok and rescue_ok
        print("FLANKTRIGG VERIFIERAD" if ok else "NÅGOT STÄMMER INTE — se ovan")
        return 0 if ok else 1
    except ScopeError as exc:
        print("FEL:", exc)
        return 1
    finally:
        scope.close()


if __name__ == "__main__":
    sys.exit(main())
