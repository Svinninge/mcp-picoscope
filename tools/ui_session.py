# File version: v0.02
"""Hold an MCP session open so the display stays live.

Usage: ui_session.py [seconds] [backend]   defaults: 3600, auto

Captures continuously so the page has something current to draw. The MCP
server, and therefore the UI server, lives exactly as long as this session.

The window length follows the signal. A fixed one cannot work: 20 ms of an
800 Hz sine is 16 periods and reads nicely, while the same 20 ms of an 11.8 kHz
sine is 248 periods and draws as a solid block — three pixels per period, which
no screen can resolve and no bench scope would show either. So after each
capture the measured frequency sets the next window to about ten periods.
"""

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from tests.test_stdio import payload  # noqa: E402

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
BACKEND = sys.argv[2] if len(sys.argv) > 2 else "auto"
PERIOD_S = 2

# Roughly a screen's worth of signal.
PERIODS_ON_SCREEN = 10
MIN_WINDOW_S = 20e-6
MAX_WINDOW_S = 0.2
START_WINDOW_S = 0.02
# Only retune when the window is off by more than this, or the timebase would
# twitch on every capture as the measured frequency wobbles in its last digit.
RETUNE_RATIO = 1.5
SAMPLES = 4096


def clamp(value: float) -> float:
    return max(MIN_WINDOW_S, min(MAX_WINDOW_S, value))


async def run() -> None:
    env = {k: v for k, v in os.environ.items() if k != "PICOSCOPE_UI"}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "mcp_picoscope.server"],
        cwd=str(ROOT),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            info = payload(await session.call_tool("get_server_info", {}))
            print("UI:", info["ui_url"], flush=True)

            opened = payload(await session.call_tool("open_device", {"backend": BACKEND}))
            if opened.get("warning"):
                print(opened["warning"], flush=True)
            print("backend:", opened["backend"], opened["device"]["model"], flush=True)

            auto = payload(await session.call_tool("autoset", {}))
            print("autoset:", "; ".join(auto["steps"]), flush=True)

            window = START_WINDOW_S
            freq = auto["measurements"]["frequency_hz"]
            if freq:
                window = clamp(PERIODS_ON_SCREEN / freq)

            loop = asyncio.get_event_loop()
            deadline = loop.time() + SECONDS
            captures = 0
            while loop.time() < deadline:
                block = payload(await session.call_tool("capture_block", {
                    "duration_s": window, "samples": SAMPLES,
                }))
                captures += 1
                freq = block["measurements"]["frequency_hz"]
                if freq:
                    wanted = clamp(PERIODS_ON_SCREEN / freq)
                    if max(wanted / window, window / wanted) > RETUNE_RATIO:
                        window = wanted
                        print(
                            f"följer signalen: {freq:.6g} Hz → {window * 1e3:.4g} ms "
                            f"fönster ({PERIODS_ON_SCREEN} perioder)",
                            flush=True,
                        )
                await asyncio.sleep(PERIOD_S)

            print(f"done after {captures} captures", flush=True)
            payload(await session.call_tool("close_device", {}))


asyncio.run(run())
