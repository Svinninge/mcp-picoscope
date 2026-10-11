# File version: v0.02
# Modified: 2026-10-11 - node fallback for macOS (Homebrew) beside the Windows one (Claude)
"""The display page must at least parse.

A stray quote in ui.html killed the whole script at parse time, and nothing in
the Python test suite could tell: the server happily served a page that did
nothing, and the window looked alive. Node is on this machine, so the page's
script can be checked for real rather than eyeballed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parents[1] / "picoscope_mcp" / "ui.html"
# Where node lands when its directory is not on PATH (an MCP or IDE process
# does not always inherit the login shell's PATH): the Windows installer and
# Homebrew on Apple Silicon / Intel.
NODE_FALLBACKS = (
    r"C:\Program Files\nodejs\node.exe",
    "/opt/homebrew/bin/node",
    "/usr/local/bin/node",
)


def find_node() -> str | None:
    found = shutil.which("node")
    if found:
        return found
    return next((p for p in NODE_FALLBACKS if Path(p).exists()), None)


NODE = find_node()


def page_script() -> str:
    html = PAGE.read_text(encoding="utf-8")
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "ui.html has no script block"
    return "\n".join(blocks)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_page_script_parses(tmp_path):
    js = tmp_path / "page.js"
    js.write_text(page_script(), encoding="utf-8")
    done = subprocess.run(
        [NODE, "--check", str(js)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr


def test_page_has_the_pieces_the_server_depends_on():
    """The server's contract with the page, in one place."""
    script = page_script()
    for needed in ("/state", "/view", "giveUp", "reportView", "applyScale"):
        assert needed in script, f"ui.html no longer mentions {needed}"
