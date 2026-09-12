# File version: v0.01
"""Verify the volt scale against a known DC source (e.g. a 1.5 V AA cell).

Run:  .\\.venv\\Scripts\\python.exe scratch\\verify_volt_scale.py

Measures the same DC level on several ranges. Two different questions:

  * Do the ranges AGREE with each other? That checks the range table — a wrong
    enum mapping shows up as one range disagreeing with the rest.
  * Does the absolute value match the cell? That is the only thing that checks
    MAX_ADC. A wrong MAX_ADC is a constant factor on every range at once, so
    cross-range agreement alone would never reveal it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_picoscope.analysis import measure  # noqa: E402
from mcp_picoscope.backends.ps2000 import MAX_ADC, PS2000Backend  # noqa: E402
from mcp_picoscope.scope import ChannelConfig, TriggerConfig  # noqa: E402

# A fresh alkaline AA sits near 1.6 V unloaded and a used one near 1.3 V, so
# anything in this window is "the cell"; a wrong MAX_ADC would be off by a
# factor of two or more, far outside it.
NOMINAL_V = 1.5
PLAUSIBLE = (1.1, 1.7)


def main() -> int:
    scope = PS2000Backend()
    info = scope.open()
    print(f"{info.model} serial {info.serial}, MAX_ADC assumed {MAX_ADC}")
    try:
        scope.set_trigger(TriggerConfig(mode="auto"))
        readings: list[tuple[float, float, float]] = []
        for range_v in info.voltage_ranges_v:
            if range_v < NOMINAL_V:
                continue  # the cell would clip; nothing to learn
            scope.set_channel(ChannelConfig(range_v, "DC", True))
            capture = scope.capture_block(0.02, 4096)
            capture.capture_id = f"range{range_v}"
            stats = measure(capture)
            readings.append((range_v, stats["mean_v"], stats["vpp_v"]))
            print(
                f"  ±{range_v:>5} V: mean {stats['mean_v']:+.4f} V, "
                f"ripple Vpp {stats['vpp_v']:.4f} V"
                + ("  [CLIPPED]" if stats["overrange"] else "")
            )

        if not readings:
            print("FAIL: no range large enough for the source")
            return 1

        means = np.array([m for _, m, _ in readings])
        spread = float(means.max() - means.min())
        best_range, best_mean, _ = min(readings, key=lambda r: r[0])

        print(f"\nranges agree to within {spread * 1000:.1f} mV")
        print(f"smallest usable range ±{best_range} V reads {best_mean:+.4f} V")
        low, high = PLAUSIBLE
        if low <= abs(best_mean) <= high:
            print(
                f"OK: that is a {NOMINAL_V} V cell. MAX_ADC = {MAX_ADC} is correct "
                "— a wrong one would be off by a factor, not by millivolts."
            )
            return 0
        factor = NOMINAL_V / abs(best_mean) if best_mean else float("inf")
        print(
            f"FAIL: expected roughly {NOMINAL_V} V, read {best_mean:+.4f} V. "
            f"That is a factor of {factor:.3f} — check MAX_ADC "
            f"(the true value may be {MAX_ADC / factor:.0f})."
        )
        return 1
    finally:
        scope.close()
        print("[closed]")


if __name__ == "__main__":
    sys.exit(main())
