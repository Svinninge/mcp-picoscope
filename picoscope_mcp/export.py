# File version: v0.02
"""Capture export: CSV, NPZ and PNG.

Files land under the capture directory (CAPTURE_DIR, default ./captures). The
LLM never supplies a path — it picks a format and gets a path back.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import numpy as np

from .analysis import measure
from .scope import Capture, ScopeError

FORMATS = ("csv", "npz", "png")


def capture_dir() -> Path:
    """Where exports are written. Created on demand."""
    path = Path(os.environ.get("CAPTURE_DIR", "captures")).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


# Windows refuses these as file names whatever the extension — "con.png" is
# the console, not a picture.
_RESERVED = {"con", "prn", "aux", "nul"} | {f"{p}{i}" for p in ("com", "lpt") for i in range(1, 10)}
MAX_NAME = 80
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def safe_name(name: str) -> str:
    """A user's name for a file, reduced to something that can only be a name.

    The name is input that becomes a path: separators, drive colons, '..' and
    the characters Windows rejects are replaced, so whatever arrives, the file
    lands directly in the capture directory.
    """
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", (name or "").strip())
    stem = re.sub(r"\.(png|csv|npz)$", "", stem, flags=re.IGNORECASE)
    stem = stem.strip(" ._")[:MAX_NAME].strip(" ._")
    if not stem:
        raise ScopeError("The name is empty once path characters are removed.")
    if stem.split(".")[0].lower() in _RESERVED:
        stem = "_" + stem
    return stem


def unique_path(stem: str, ext: str) -> Path:
    """A path in the capture directory that does not exist yet: never overwrite."""
    folder = capture_dir()
    target = folder / f"{stem}.{ext}"
    n = 2
    while target.exists():
        target = folder / f"{stem}-{n}.{ext}"
        n += 1
    if target.resolve().parent != folder:
        raise ScopeError("Refusing to write outside the capture directory.")
    return target


def save_screenshot(png: bytes, name: str) -> Path:
    """Store an image the display rendered — the trace exactly as it was shown."""
    if not png.startswith(PNG_SIGNATURE):
        raise ScopeError("The screenshot is not a PNG image.")
    target = unique_path(safe_name(name), "png")
    target.write_bytes(png)
    return target


def export(capture: Capture, fmt: str, name: str = "") -> Path:
    fmt = fmt.lower()
    if fmt not in FORMATS:
        raise ScopeError(f"Unknown format {fmt!r}. Valid: {', '.join(FORMATS)}.")
    if name:
        target = unique_path(safe_name(name), fmt)
    else:
        target = capture_dir() / f"{capture.capture_id}.{fmt}"
    if fmt == "csv":
        _write_csv(capture, target)
    elif fmt == "npz":
        _write_npz(capture, target)
    else:
        _write_png(capture, target)
    return target


def _write_csv(capture: Capture, target: Path) -> None:
    t = np.arange(capture.volts.size) * capture.dt_s
    rows = np.column_stack((t, capture.volts))
    header = (
        f"capture_id={capture.capture_id} "
        f"sample_rate_hz={capture.sample_rate_hz:.6g} "
        f"range_v={capture.range_v} coupling={capture.coupling}\n"
        "time_s,volt"
    )
    np.savetxt(target, rows, delimiter=",", header=header, comments="# ", fmt="%.9g")


def _write_npz(capture: Capture, target: Path) -> None:
    np.savez_compressed(
        target,
        volts=capture.volts,
        dt_s=capture.dt_s,
        range_v=capture.range_v,
        coupling=capture.coupling,
        capture_id=capture.capture_id,
        overrange=capture.overrange,
    )


def _write_png(capture: Capture, target: Path) -> None:
    # Imported here: matplotlib costs ~1 s to import and most sessions never
    # export a plot. Agg because there is no display on the far side of stdio.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    stats = measure(capture)
    t = np.arange(capture.volts.size) * capture.dt_s
    scale, unit = _time_unit(capture.duration_s)

    fig, ax = plt.subplots(figsize=(10, 4.5), dpi=110)
    ax.plot(t * scale, capture.volts, linewidth=0.8, color="#1f77b4")
    ax.set_xlabel(f"time ({unit})")
    ax.set_ylabel("volt")
    ax.set_ylim(-capture.range_v * 1.05, capture.range_v * 1.05)
    ax.grid(True, alpha=0.3)

    freq = stats["frequency_hz"]
    subtitle = f"Vpp {stats['vpp_v']:.4g} V · RMS {stats['rms_v']:.4g} V"
    if freq:
        subtitle += f" · {freq:.6g} Hz"
    if capture.overrange:
        subtitle += " · CLIPPED"
    ax.set_title(
        f"{capture.capture_id} — {stats['sample_rate_hz']:.4g} S/s, "
        f"±{capture.range_v} V {capture.coupling}\n{subtitle}",
        fontsize=10,
    )
    fig.tight_layout()
    fig.savefig(target)
    plt.close(fig)


def _time_unit(duration_s: float) -> tuple[float, str]:
    for limit, scale, unit in (
        (1e-6, 1e9, "ns"),
        (1e-3, 1e6, "µs"),
        (1.0, 1e3, "ms"),
    ):
        if duration_s < limit:
            return scale, unit
    return 1.0, "s"
