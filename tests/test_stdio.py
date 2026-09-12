# File version: v0.02
"""Smoke test over the real stdio transport.

Tools existing in a registry is not the same as tools being callable: argument
schemas, return serialisation and the resource URI only get exercised when a
client actually speaks the protocol to a spawned server process.

Run directly (no hardware needed, it drives the mock backend):
    .\\.venv\\Scripts\\python.exe tests\\test_stdio.py
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
SIGNAL_HZ = 2000.0
SIGNAL_AMPLITUDE_V = 1.5
DUTY_PCT = 30.0


def payload(result) -> dict:
    """Unwrap a FastMCP tool result into the dict the tool returned."""
    if getattr(result, "is_error", False):
        raise AssertionError(result.content[0].text)
    return json.loads(result.content[0].text)


async def run() -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_picoscope.server"],
        cwd=str(ROOT),
        # No Edge window from a test run: the display opens on the first tool
        # call, and a suite makes dozens of them.
        env={**os.environ, "PICOSCOPE_UI": "0"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            names = {t.name for t in (await session.list_tools()).tools}
            expected = {
                "list_devices", "open_device", "close_device", "get_device_info",
                "get_server_info", "configure_channel", "configure_trigger",
                "configure_mock_signal", "capture_block", "measure",
                "export_capture", "autoset",
            }
            missing = expected - names
            assert not missing, f"tools missing from the server: {sorted(missing)}"
            print(f"[OK] {len(names)} tools exposed")

            opened = payload(await session.call_tool("open_device", {"backend": "mock"}))
            assert opened["open"] is True and opened["backend"] == "mock"
            print(f"[OK] opened {opened['device']['model']}")

            payload(await session.call_tool("configure_mock_signal", {
                "waveform": "square",
                "frequency_hz": SIGNAL_HZ,
                "amplitude_v": SIGNAL_AMPLITUDE_V,
                "duty_cycle_pct": DUTY_PCT,
                "noise_v": 0.001,
            }))
            payload(await session.call_tool("configure_channel", {"range_v": 2.0}))
            payload(await session.call_tool("configure_trigger", {
                "mode": "edge", "threshold_v": 0.0, "direction": "rising",
            }))

            block = payload(await session.call_tool("capture_block", {
                "duration_s": 0.005, "samples": 8192,
            }))
            stats = block["measurements"]
            assert abs(stats["frequency_hz"] - SIGNAL_HZ) / SIGNAL_HZ < 0.01, stats
            assert abs(stats["duty_cycle_pct"] - DUTY_PCT) < 2.0, stats
            assert abs(stats["vpp_v"] - 2 * SIGNAL_AMPLITUDE_V) < 0.05, stats
            assert len(block["curve"]) <= 220, "curve was not decimated"
            print(
                f"[OK] {block['capture_id']}: {stats['frequency_hz']:.6g} Hz, "
                f"duty {stats['duty_cycle_pct']:.3g} %, Vpp {stats['vpp_v']:.4g} V, "
                f"{len(block['curve'])} curve points"
            )

            again = payload(await session.call_tool("measure", {
                "capture_id": block["capture_id"],
            }))
            assert again["frequency_hz"] == stats["frequency_hz"]

            for fmt in ("csv", "npz", "png"):
                out = payload(await session.call_tool("export_capture", {
                    "capture_id": block["capture_id"], "format": fmt,
                }))
                assert Path(out["path"]).stat().st_size > 0
                print(f"[OK] exported {fmt}: {out['path']}")

            auto = payload(await session.call_tool("autoset", {}))
            assert abs(auto["measurements"]["frequency_hz"] - SIGNAL_HZ) / SIGNAL_HZ < 0.01
            print(f"[OK] autoset picked ±{auto['range_v']} V: {'; '.join(auto['steps'])}")

            state = json.loads(
                (await session.read_resource("picoscope://state")).contents[0].text
            )
            assert state["open"] is True and len(state["captures"]) >= 2
            print(f"[OK] picoscope://state lists {len(state['captures'])} captures")

            bad = await session.call_tool("measure", {"capture_id": "nope"})
            assert bad.is_error and "Unknown capture_id" in bad.content[0].text
            print("[OK] unknown capture_id answers with a readable error")

            payload(await session.call_tool("close_device", {}))
            print("[OK] closed")


def test_stdio_roundtrip():
    """Pytest entry point; the module also runs standalone."""
    asyncio.run(run())


if __name__ == "__main__":
    asyncio.run(run())
    print("\nAll stdio checks passed.")
