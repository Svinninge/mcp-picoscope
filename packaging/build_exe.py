# File version: v0.01
"""Build dist/PicoScope.exe — a reproducible PyInstaller invocation.

Run:  .\\.venv\\Scripts\\python.exe packaging\\build_exe.py

The command lives in a script rather than a README line because every option
here was needed for a reason, and a reason written next to the option survives
the next person "tidying" the command.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEP = ";" if sys.platform == "win32" else ":"

ARGS = [
    sys.executable, "-m", "PyInstaller",
    str(ROOT / "packaging" / "picoscope_app.py"),
    "--name", "PicoScope",
    "--onefile",
    # No console window: this is a desktop app. Its log goes to
    # %LOCALAPPDATA%\mcp-picoscope\app.log instead (app.configure_logging).
    "--windowed",
    "--noconfirm",
    "--clean",
    "--distpath", str(ROOT / "dist"),
    "--workpath", str(ROOT / "build"),
    "--specpath", str(ROOT / "build"),
    # ui.py reads the page from next to itself, so it must land in the package
    # directory inside the bundle, not at the root.
    "--add-data", f"{ROOT / 'mcp_picoscope' / 'ui.html'}{SEP}mcp_picoscope",
    # version_line() reads this from the package's parent directory.
    "--add-data", f"{ROOT / 'deploy_version.txt'}{SEP}.",
    # uvicorn picks its loop, protocol and lifespan implementations by name at
    # runtime; PyInstaller's static analysis cannot see those imports.
    "--collect-submodules", "uvicorn",
    # NOT --collect-submodules mcp: that imports every module in the SDK,
    # including mcp.cli, which calls sys.exit(1) at import time when its optional
    # typer dependency is absent — and takes the build down with it. Static
    # analysis already follows the imports the server really makes, including
    # the ones inside functions.
    "--exclude-module", "mcp.cli",
    # The ps2000 wrapper is imported only when hardware is opened. ps2000.dll is
    # deliberately NOT bundled: it belongs to the driver installation, and
    # _ensure_dll_on_path() finds it there.
    "--collect-submodules", "picosdk",
    # matplotlib is only needed for PNG export and draws with Agg; the GUI
    # backends would add megabytes for a window nothing ever opens.
    "--exclude-module", "tkinter",
    "--exclude-module", "PyQt5",
    "--exclude-module", "PyQt6",
    "--exclude-module", "PySide6",
    # Not used at runtime; only the build and the tests need them.
    "--exclude-module", "pytest",
    "--exclude-module", "PyInstaller",
]


def main() -> int:
    print("building dist/PicoScope.exe ...")
    done = subprocess.run(ARGS, cwd=ROOT)
    if done.returncode != 0:
        return done.returncode
    exe = ROOT / "dist" / "PicoScope.exe"
    print(f"\n{exe}  ({exe.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
