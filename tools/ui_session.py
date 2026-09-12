# File version: v0.01
"""Hold an MCP session open so the display stays live.

Usage: ui_session.py [seconds] [backend]   defaults: 3600, auto

Captures continuously so the page has something current to draw. The MCP
server, and therefore the UI server, lives exactly as long as this session.
"""
import asyncio, os, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tests.test_stdio import payload

SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 3600
BACKEND = sys.argv[2] if len(sys.argv) > 2 else "auto"
PERIOD_S = 2

async def run():
    env = {k: v for k, v in os.environ.items() if k != "PICOSCOPE_UI"}
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "mcp_picoscope.server"],
        cwd=str(ROOT), env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            info = payload(await s.call_tool("get_server_info", {}))
            print("UI:", info["ui_url"], flush=True)
            opened = payload(await s.call_tool("open_device", {"backend": BACKEND}))
            if opened.get("warning"):
                print(opened["warning"], flush=True)
            print("backend:", opened["backend"], opened["device"]["model"], flush=True)
            auto = payload(await s.call_tool("autoset", {}))
            print("autoset:", "; ".join(auto["steps"]), flush=True)
            deadline = asyncio.get_event_loop().time() + SECONDS
            n = 0
            while asyncio.get_event_loop().time() < deadline:
                payload(await s.call_tool("capture_block",
                                          {"duration_s": 0.02, "samples": 4096}))
                n += 1
                await asyncio.sleep(PERIOD_S)
            print(f"done after {n} captures", flush=True)
            payload(await s.call_tool("close_device", {}))
asyncio.run(run())
