# File version: v0.02
"""Hardware smoke test over stdio. Requires a real PicoScope.

Skipped automatically when no device answers, so the suite still runs on a
machine without hardware. This is the test that would have caught the driver
path problem: it refuses the mock fallback and fails if 'ps2000' is not the
backend that opened.

Run:  .\\.venv\\Scripts\\python.exe tests\\test_hardware.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.test_stdio import payload  # noqa: E402


def hardware_present() -> bool:
    from mcp_picoscope.backends.ps2000 import PS2000Backend
    from mcp_picoscope.scope import ScopeError

    try:
        return bool(PS2000Backend().list_devices())
    except ScopeError:
        return False


async def run() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_picoscope.server"],
        cwd=str(ROOT),
        env={**os.environ, "PICOSCOPE_UI": "0"},  # no window from a test run
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            found = payload(await session.call_tool("list_devices", {}))
            hardware = [d for d in found["devices"] if d["backend"] == "ps2000"]
            assert hardware, f"no ps2000 device listed: {found}"
            print(f"[OK] found {hardware[0]['model']} serial {hardware[0]['serial']}")

            # backend='ps2000' refuses the mock fallback: a silent fallback is
            # exactly how a broken driver path would pass as a green test.
            opened = payload(await session.call_tool("open_device", {"backend": "ps2000"}))
            assert opened["backend"] == "ps2000", opened
            device = opened["device"]
            assert device["model"] == "2104", f"unexpected variant: {device['model']}"
            print(
                f"[OK] opened variant {device['model']} serial {device['serial']}, "
                f"driver {device['driver_version']}, "
                f"{device['max_sample_rate_hz']:.4g} S/s, "
                f"{device['max_samples']} samples, ranges {device['voltage_ranges_v']}"
            )
            assert 0.02 not in device["voltage_ranges_v"], (
                "20 mV was probed as supported; the PS2104 rejects it"
            )

            channel = payload(await session.call_tool("configure_channel", {
                "range_v": 5.0, "coupling": "DC",
            }))
            print(f"[OK] channel on ±{channel['range_v']} V {channel['coupling']}")

            payload(await session.call_tool("configure_trigger", {"mode": "auto"}))
            block = payload(await session.call_tool("capture_block", {
                "duration_s": 0.02, "samples": 4096,
            }))
            stats = block["measurements"]
            assert stats["samples"] > 0
            print(
                f"[OK] {block['capture_id']}: {stats['samples']} samples at "
                f"{stats['sample_rate_hz']:.6g} S/s, "
                f"{stats['vmin_v']:+.4f}..{stats['vmax_v']:+.4f} V, "
                f"Vpp {stats['vpp_v']:.4f} V, "
                f"f={stats['frequency_hz'] and f'{stats['frequency_hz']:.6g} Hz' or 'none'}"
            )
            if stats["note"]:
                print(f"     note: {stats['note']}")

            out = payload(await session.call_tool("export_capture", {
                "capture_id": block["capture_id"], "format": "png",
            }))
            print(f"[OK] exported {out['path']}")

            auto = payload(await session.call_tool("autoset", {}))
            print(f"[OK] autoset: {'; '.join(auto['steps'])}")

            payload(await session.call_tool("close_device", {}))
            print("[OK] closed — USB handle released")


def test_hardware_roundtrip():
    import pytest

    if not hardware_present():
        pytest.skip("no PicoScope connected")
    asyncio.run(run())


if __name__ == "__main__":
    if not hardware_present():
        print("No PicoScope found — connect one, or run tests/test_stdio.py instead.")
        raise SystemExit(1)
    asyncio.run(run())
    print("\nHardware checks passed.")
