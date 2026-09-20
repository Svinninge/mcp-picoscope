# File version: v0.01
"""Entry point for the frozen PicoScope.exe.

PyInstaller needs a plain script to start from, and picoscope_mcp.app uses
relative imports, so it must be imported as a package rather than run directly.
"""

import sys

from picoscope_mcp.app import main

if __name__ == "__main__":
    sys.exit(main())
