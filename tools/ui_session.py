# File version: v0.03
"""Hold an MCP session open so the display stays live.

Usage: ui_session.py [seconds] [backend]   defaults: 3600, auto

The server captures on its own now: this opens the device, autosets, starts a
sweep and then waits. It used to drive the captures itself, which meant two
things wanted to own the instrument as soon as the display got a Run button —
exactly what issue #1 warned about. The sweep engine in control.py is the one
driver, and it follows the signal's frequency by itself.
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
# How often this session pokes the server. The sweep does not need it; a live
# MCP connection just makes the session's own state visible while it runs.
REPORT_S = 30


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

            sweep = payload(await session.call_tool("start_sweep", {"mode": "auto"}))
            print("svep:", sweep["mode"], "igång" if sweep["running"] else "STARTADE INTE",
                  flush=True)

            loop = asyncio.get_event_loop()
            deadline = loop.time() + SECONDS
            while loop.time() < deadline:
                await asyncio.sleep(min(REPORT_S, max(0.0, deadline - loop.time())))
                status = payload(await session.call_tool("get_server_info", {}))
                del status  # keeps the MCP session warm; the sweep needs no help

            print("stoppar svepet", flush=True)
            payload(await session.call_tool("stop_sweep", {}))
            payload(await session.call_tool("close_device", {}))


asyncio.run(run())
