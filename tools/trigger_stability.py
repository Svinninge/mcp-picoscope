# File version: v0.01
"""Measure how much the trigger steadies the trace, through the display.

Reads the running display's state over HTTP and collects the start of each new
capture. It touches no hardware of its own — the point is to measure the path a
click actually takes: page to server to instrument and back.

A triggered trace starts in the same place every sweep; an untriggered one
starts wherever the signal happened to be. That difference is the measurement.

Run:  .\\.venv\\Scripts\\python.exe tools\\trigger_stability.py [url] [seconds]
"""

from __future__ import annotations

import json
import statistics
import sys
import time
import urllib.parse
import urllib.request

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8072/"
SECONDS = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0
POLL_S = 0.2


def get(path: str) -> dict:
    with urllib.request.urlopen(URL.rstrip("/") + path, timeout=5) as response:
        return json.load(response)


def post(path: str) -> dict:
    request = urllib.request.Request(URL.rstrip("/") + path, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def collect(seconds: float) -> tuple[list[float], dict]:
    """First sample of every new capture the display shows."""
    starts: list[float] = []
    seen: set[str] = set()
    state: dict = {}
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        state = get("/state")
        latest = state.get("latest")
        if latest and latest["capture_id"] not in seen:
            seen.add(latest["capture_id"])
            starts.append(latest["curve"][0][1])
        time.sleep(POLL_S)
    return starts, state


def describe(label: str, starts: list[float], vpp: float) -> float:
    if len(starts) < 2:
        print(f"{label:22s} för få fångster ({len(starts)})")
        return float("nan")
    spread = statistics.pstdev(starts)
    print(
        f"{label:22s} {len(starts):2d} fångster   start {statistics.mean(starts):+.3f} V "
        f"±{spread:.3f} V   = {spread / vpp * 100:5.1f} % av Vpp   "
        f"min {min(starts):+.3f} max {max(starts):+.3f}"
    )
    return spread


def main() -> int:
    state = get("/state")
    if not state.get("open"):
        print("ingen enhet öppen i displayen")
        return 1
    latest = state.get("latest")
    if not latest:
        print("displayen har ingen fångst än")
        return 1

    stats = latest["measurements"]
    vpp = stats["vpp_v"]
    mid = round((stats["vmin_v"] + stats["vmax_v"]) / 2, 4)
    print(
        f"signal: {stats['frequency_hz']} Hz, {stats['vmin_v']:+.3f}..{stats['vmax_v']:+.3f} V, "
        f"mitt {mid:+.3f} V\n"
    )

    post("/control?" + urllib.parse.urlencode({"action": "sweep", "mode": "auto"}))
    post("/control?" + urllib.parse.urlencode(
        {"action": "trigger", "level_v": mid, "direction": "rising", "mode": "auto"}
    ))
    free = describe("fritt löpande", *collect(SECONDS)[:1], vpp)

    post("/control?" + urllib.parse.urlencode(
        {"action": "trigger", "level_v": mid, "direction": "rising", "mode": "edge"}
    ))
    post("/control?" + urllib.parse.urlencode({"action": "sweep", "mode": "normal"}))
    starts, state = collect(SECONDS)
    triggered = describe("edge, stigande", starts, vpp)
    if state.get("sweep", {}).get("error"):
        print("   svep säger:", state["sweep"]["error"][:90])

    post("/control?" + urllib.parse.urlencode(
        {"action": "trigger", "level_v": mid, "direction": "falling", "mode": "edge"}
    ))
    falling, _ = collect(SECONDS)
    describe("edge, fallande", falling, vpp)

    print()
    if triggered == triggered and free == free:  # not NaN
        print(f"stabilisering: {free / triggered:.0f}× mindre spridning med trigg")
    post("/control?" + urllib.parse.urlencode({"action": "sweep", "mode": "auto"}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
