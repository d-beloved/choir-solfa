"""Get the song: download it from a link, or use an uploaded file."""
from __future__ import annotations

import shutil
from pathlib import Path
from urllib.parse import urlparse

MAX_MINUTES = 15
MAX_BYTES = 300 * 1024 * 1024


class FetchError(Exception):
    pass


def is_url(text: str) -> bool:
    parsed = urlparse(text.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def download(url: str, dest: Path) -> tuple[Path, str]:
    """Download the audio behind a link. Returns (file path, song title)."""
    import yt_dlp

    if not is_url(url):
        raise FetchError("That does not look like a web link. It should start with http:// or https://.")
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "js_runtimes": {"node": {}},  # YouTube needs a JavaScript runtime; Node is common on dev machines
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info.get("_type") == "playlist":
                raise FetchError("That link is a playlist. Paste the link to one song.")
            duration = info.get("duration") or 0
            if duration > MAX_MINUTES * 60:
                raise FetchError(f"That song is longer than {MAX_MINUTES} minutes. Try a shorter one.")
            info = ydl.extract_info(url, download=True)
            path = Path(ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        raise FetchError(f"Could not download that link. {_short(e)}") from e
    if not path.exists():
        found = sorted(dest.glob("source.*"))
        if not found:
            raise FetchError("The download finished but no audio file was saved.")
        path = found[0]
    return path, info.get("title") or "Untitled song"


def use_upload(src: Path, dest: Path, original_name: str) -> tuple[Path, str]:
    suffix = Path(original_name).suffix.lower()[:8] or ".audio"
    target = dest / f"source{suffix}"
    shutil.copyfile(src, target)
    return target, Path(original_name).stem or "Untitled song"


def _short(e: Exception) -> str:
    text = str(e).replace("ERROR: ", "").strip()
    return text.splitlines()[0][:200] if text else ""
