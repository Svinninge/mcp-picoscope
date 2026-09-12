# File version: v0.01
"""Measure the zero point on every range. Expects a shorted/0 V input.

Offset is the other half of a calibration: the volt scale says how big a step
is, the zero says where the steps start from. A constant offset survives every
amplitude measurement untouched and shows up only here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_picoscope.analysis import measure  # noqa: E402
from mcp_picoscope.backends.ps2000 import PS2000Backend  # noqa: E402
from mcp_picoscope.scope import ChannelConfig, TriggerConfig  # noqa: E402


def main() -> int:
    scope = PS2000Backend()
    info = scope.open()
    print(f"{info.model} serial {info.serial} — zero check\n")
    try:
        scope.set_trigger(TriggerConfig(mode="auto"))
        worst = 0.0
        for range_v in info.voltage_ranges_v:
            scope.set_channel(ChannelConfig(range_v, "DC", True))
            capture = scope.capture_block(0.02, 4096)
            capture.capture_id = f"zero{range_v}"
            stats = measure(capture)
            lsb = 2 * range_v / 256  # 8-bit step on this range
            counts = stats["mean_v"] / lsb
            worst = max(worst, abs(counts))
            print(
                f"  ±{range_v:>5} V: mean {stats['mean_v']:+.5f} V "
                f"({counts:+.2f} LSB), Vpp {stats['vpp_v']:.5f} V, "
                f"LSB = {lsb * 1000:.2f} mV"
            )
        print(
            f"\nworst offset {worst:.2f} LSB — anything under ~1 LSB is the ADC "
            "grid, not an error."
        )
        return 0
    finally:
        scope.close()
        print("[closed]")


if __name__ == "__main__":
    sys.exit(main())
