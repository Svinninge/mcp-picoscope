# File version: v0.03
# Description: Package version helpers — system version (git tag) and deploy version
# Author: Per Norrfors
# Created: 2026-09-12
# Modified: 2026-09-20 - SYSTEM_VERSION synced to the v0.05 tag; package renamed (Claude)
"""picoscope-mcp — MCP server for the PicoScope PS2104.

Two independent versions, per the global versioning rules: SYSTEM_VERSION
mirrors the latest git tag and moves with a significant push; DEPLOY_VERSION
lives in deploy_version.txt and moves with each run in anger. Both are shown at
runtime, in get_server_info() and in the display header.
"""

from pathlib import Path

SYSTEM_VERSION = "0.05"
__version__ = SYSTEM_VERSION

_DEPLOY_FILE = Path(__file__).resolve().parents[1] / "deploy_version.txt"


def deploy_version() -> str:
    """The deploy version, or '?' when the file is missing (installed package)."""
    try:
        return _DEPLOY_FILE.read_text(encoding="utf-8").strip() or "?"
    except OSError:
        return "?"


def version_line() -> str:
    """The runtime banner: 'System vX.YY | Deploy vX.YY'."""
    return f"System v{SYSTEM_VERSION} | Deploy v{deploy_version()}"
