"""Find the helper programs the app runs: ffmpeg, and a JavaScript runtime for YouTube.

Both ship inside Python packages (imageio-ffmpeg and deno), so nobody has to
install them by hand on macOS or Windows. A copy already on PATH is the fallback.
"""
from __future__ import annotations

import functools
import shutil


@functools.cache
def ffmpeg() -> str:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # package missing, or no binary for this platform
        return shutil.which("ffmpeg") or "ffmpeg"


@functools.cache
def js_runtimes() -> dict:
    """The yt-dlp `js_runtimes` option: the bundled deno, plus Node if it is installed."""
    runtimes: dict = {}
    try:
        import deno
        runtimes["deno"] = {"path": deno.find_deno_bin()}
    except Exception:
        if shutil.which("deno"):
            runtimes["deno"] = {}
    if shutil.which("node"):
        runtimes["node"] = {}
    return runtimes
