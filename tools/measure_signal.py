# File version: v0.01
"""Measure whatever is on channel A properly: autoset, then several timebases.

The definition-of-done check wants frequency to +/-1 % against a known source,
so this reports the same signal measured over different window lengths. A real
signal gives the same answer every time; a wrong one wanders.

Run:  .\\.venv\\Scripts\\python.exe tools\\measure_signal.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_picoscope.analysis import measure  # noqa: E402
from mcp_picoscope.backends.ps2000 import PS2000Backend  # noqa: E402
from mcp_picoscope.export import export  # noqa: E402
from mcp_picoscope.scope import ChannelConfig, ScopeError, TriggerConfig  # noqa: E402

WINDOWS_S = (0.002, 0.005, 0.02, 0.05, 0.2)
HEADROOM = 1.2


def show(label: str, stats: dict) -> None:
    freq = stats["frequency_hz"]
    freq_text = f"{freq:.6g} Hz" if freq else "ingen"
    duty = stats["duty_cycle_pct"]
    duty_text = f"{duty:.1f} %" if duty is not None else "—"
    print(
        f"{label:20s} Vpp {stats['vpp_v']:8.4f} V   f {freq_text:>12}   "
        f"duty {duty_text:>7}   jitter {stats['period_jitter_pct']:7.3f} %   "
        f"cykler {stats['cycles_in_record']}"
    )
    if stats["note"]:
        print(f"{'':20s} {stats['note']}")


def main() -> int:
    scope = PS2000Backend()
    info = scope.open()
    print(f"{info.model} serienr {info.serial}\n")
    try:
        scope.set_trigger(TriggerConfig(mode="auto"))

        # Widest range first: the signal may be larger than whatever the last
        # session left configured, and a clipped capture measures short.
        scope.set_channel(ChannelConfig(max(info.voltage_ranges_v), "DC", True))
        survey = scope.capture_block(0.05, 4096)
        survey.capture_id = "survey"
        stats = measure(survey)
        show("survey +/-20 V", stats)

        peak = max(abs(stats["vmin_v"]), abs(stats["vmax_v"]))
        chosen = next(
            (r for r in sorted(info.voltage_ranges_v) if r >= peak * HEADROOM),
            max(info.voltage_ranges_v),
        )
        applied = scope.set_channel(ChannelConfig(chosen, "DC", True))
        print(f"\nvalt omrade: +/-{applied.range_v} V (topp {peak:.4f} V + 20 %)\n")

        freqs: list[float] = []
        for duration in WINDOWS_S:
            cap = scope.capture_block(duration, 4096)
            cap.capture_id = f"win{duration}"
            stats = measure(cap)
            show(f"{duration * 1000:g} ms fonster", stats)
            if stats["frequency_hz"]:
                freqs.append(stats["frequency_hz"])

        if len(freqs) >= 2:
            lo, hi = min(freqs), max(freqs)
            spread = 100 * (hi - lo) / lo
            print(f"\nspridning over fonsterlangder: {lo:.6g} .. {hi:.6g} Hz ({spread:.2f} %)")

        cap = scope.capture_block(5 / freqs[0] if freqs else 0.01, 4096)
        cap.capture_id = "signal"
        stats = measure(cap)
        show("5 perioder", stats)
        print("bild:", export(cap, "png"))
        return 0
    except ScopeError as exc:
        print("FEL:", exc)
        return 1
    finally:
        scope.close()


if __name__ == "__main__":
    sys.exit(main())
