"""Get the song: download it from a link, or use an uploaded file."""
from __future__ import annotations

import html
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from . import tools

MAX_MINUTES = 15
MAX_BYTES = 300 * 1024 * 1024
DOWNLOAD_SECONDS = 15 * 60  # a download slower than this is given up (a song is at most MAX_BYTES)


class FetchError(Exception):
    pass


@dataclass
class Song:
    path: Path
    title: str
    found_on: str | None = None   # what was actually downloaded, when it differs from the link
    found_url: str | None = None
    track: str = ""               # what the song is called and who sings it, when known (to find the words)
    artist: str = ""
    channel: str = ""


def get_song(url: str, dest: Path, progress: Callable[[str], None] = lambda s: None) -> Song:
    """Download the song behind any supported link (Spotify links are matched on YouTube)."""
    url = url.strip()
    if is_spotify(url):
        progress("Looking up the song on Spotify")
        track = spotify_track(url)
        progress(f"Finding \u201c{track.title}\u201d to download")
        video = find_on_youtube(track)
        progress("Downloading the song")
        path, _, about = download(video["url"], dest)
        return Song(path, track.title, found_on=video["title"], found_url=video["url"], track=track.title,
                    artist=track.artist, channel=about.get("channel", ""))
    progress("Downloading the song")
    path, title, about = download(url, dest)
    return Song(path, title, **about)


def is_url(text: str) -> bool:
    text = text.strip()
    if text.startswith("spotify:"):
        return True
    parsed = urlparse(text)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


# ---- Spotify -------------------------------------------------------------
# Spotify audio is DRM-protected and can't be downloaded. A track's public
# page does give its title, artist and exact length, which is enough to find
# the same recording on YouTube (official "Artist - Topic" uploads use the
# same master as Spotify).

_SPOTIFY_HOSTS = {"open.spotify.com", "play.spotify.com", "spotify.link", "spoti.fi"}
_SPOTIFY_PATH = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?(track|album|playlist|artist|episode|show)/([A-Za-z0-9]{22})")
# A plain client name: Spotify sends full browser user agents a script-only page without the song details.
_UA = "choir-solfa/0.1"


@dataclass
class SpotifyTrack:
    id: str
    title: str
    artist: str
    duration: int  # seconds


def is_spotify(url: str) -> bool:
    url = url.strip()
    return url.startswith("spotify:") or (urlparse(url).hostname or "").lower() in _SPOTIFY_HOSTS


def spotify_kind_and_id(url: str) -> tuple[str, str]:
    """('track', id) etc. Short links (spotify.link, spoti.fi) are followed first."""
    url = url.strip()
    if url.startswith("spotify:"):
        bits = url.split(":")
        if len(bits) >= 3:
            return bits[1], bits[2]
        raise FetchError("That Spotify link looks incomplete. In Spotify, use Share \u2192 Copy song link.")
    host = (urlparse(url).hostname or "").lower()
    if host in ("spotify.link", "spoti.fi"):
        url = _resolve_redirect(url)
    m = _SPOTIFY_PATH.match(urlparse(url).path)
    if not m:
        raise FetchError("That Spotify link doesn't point to a song. In Spotify, use Share \u2192 Copy song link.")
    return m.group(1), m.group(2)


def spotify_track(url: str) -> SpotifyTrack:
    kind, track_id = spotify_kind_and_id(url)
    if kind != "track":
        name = {"album": "an album", "playlist": "a playlist", "artist": "an artist page",
                "episode": "a podcast episode", "show": "a podcast"}.get(kind, "not a song")
        raise FetchError(f"That Spotify link is {name}. Open one song, then use Share \u2192 Copy song link.")
    link = f"https://open.spotify.com/track/{track_id}"
    try:
        return parse_spotify_page(_get(link), track_id)
    except FetchError:
        # Fall back to Spotify's oEmbed endpoint: title only, so matching relies on the search.
        import json
        data = json.loads(_get(f"https://open.spotify.com/oembed?url={link}") or "{}")
        if not data.get("title"):
            raise
        return SpotifyTrack(track_id, data["title"], "", 0)


def parse_spotify_page(page: str, track_id: str = "") -> SpotifyTrack:
    def meta(name: str) -> str:
        m = re.search(r'<meta[^>]+(?:property|name)="' + re.escape(name) + r'"[^>]+content="([^"]*)"', page)
        return html.unescape(m.group(1)).strip() if m else ""

    title = meta("og:title")
    artist = meta("music:musician_description") or meta("og:description").split(" \u00b7 ")[0]
    try:
        duration = int(meta("music:duration") or 0)
    except ValueError:
        duration = 0
    if not title:
        raise FetchError("Couldn't read the song details from Spotify. Try again, or upload the song file instead.")
    return SpotifyTrack(track_id, title, artist, duration)


_BAD_WORDS = ("karaoke", "instrumental", "backing track", "minus one", "8d audio", "slowed", "sped up",
              "reverb", "nightcore", "remix", "cover", "tutorial", "lesson", "reaction")


def score_candidate(track: SpotifyTrack, entry: dict) -> float:
    """Lower is better. Length match matters most; official audio is preferred."""
    name = (entry.get("title") or "").lower()
    channel = (entry.get("channel") or entry.get("uploader") or "").lower()
    duration = entry.get("duration")
    if not duration:
        return float("inf")
    cost = abs(float(duration) - track.duration) if track.duration else 0.0
    if channel.endswith(" - topic"):
        cost -= 3.0  # YouTube Music upload of the same master recording
    wanted = f"{track.title} {track.artist}".lower()
    for word in _BAD_WORDS:
        if word in name and word not in wanted:
            cost += 30.0
    if "live" in name.split() and "live" not in wanted.split():
        cost += 8.0
    if track.title.lower() not in name and not channel.endswith(" - topic"):
        cost += 4.0
    if track.artist and not channel.endswith(" - topic") and not _mentions_artist(track.artist, f"{name} {channel}"):
        cost += 4.0  # same length but maybe someone else's recording
    return cost


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower().replace("'", "")) if len(w) >= 4}


def _mentions_artist(artists: str, text: str) -> bool:
    """True if any listed artist's name (most of its longer words) appears in the text."""
    have = _words(text)
    squashed = re.sub(r"[^a-z0-9]", "", text.lower())
    for artist in artists.split(","):
        words = _words(artist)
        if not words:
            continue
        hits = sum(1 for w in words if w in have or w in squashed)
        if hits * 2 >= len(words):
            return True
    return False


def find_on_youtube(track: SpotifyTrack, max_off: float = 8.0) -> dict:
    import yt_dlp

    query = f"ytsearch10:{track.artist} - {track.title}" if track.artist else f"ytsearch10:{track.title}"
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "js_runtimes": tools.js_runtimes(), **_proxy()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            entries = list((ydl.extract_info(query, download=False) or {}).get("entries") or [])
    except yt_dlp.utils.DownloadError as e:
        if _refused(e):
            raise FetchError(REFUSED) from e
        raise FetchError(f"Couldn't search YouTube for this song. {_short(e)}") from e
    ranked = sorted((score_candidate(track, e), i, e) for i, e in enumerate(entries))
    for cost, _, entry in ranked:
        off = abs(float(entry.get("duration") or 0) - track.duration) if track.duration else 0.0
        if cost != float("inf") and off <= max_off and cost < 30:
            vid = entry.get("id")
            return {"url": entry.get("url") or f"https://www.youtube.com/watch?v={vid}",
                    "title": entry.get("title") or track.title}
    who = f"\u201c{track.title}\u201d by {track.artist}" if track.artist else f"\u201c{track.title}\u201d"
    raise FetchError(f"Couldn't find the same recording of {who} to download. "
                     "Upload the song file instead, or paste a YouTube link to it.")


class _SpotifyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to Spotify's own https addresses (short links go via spotify.app.link)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlparse(newurl)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not (host in _SPOTIFY_HOSTS or host.endswith((".spotify.com", ".spotify.link"))
                                           or host == "spotify.app.link"):
            raise urllib.error.HTTPError(newurl, code, "redirect away from Spotify", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req: urllib.request.Request, timeout: float = 20):
    """Open a Spotify address, through the WARP tunnel on the website (like every other fetch)."""
    proxy = os.environ.get("CHOIR_SOLFA_PROXY")
    handlers = [_SpotifyRedirects()]
    if proxy:  # otherwise the computer's own proxy settings, as before
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers).open(req, timeout=timeout)


def _get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept-Language": "en"})
    try:
        with _open(req) as r:
            return r.read(2_000_000).decode("utf-8", "replace")
    except OSError as e:
        raise FetchError("Couldn't reach Spotify. Check your internet connection and try again.") from e


def _resolve_redirect(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _open(req) as r:
            final = r.geturl()
            if (urlparse(final).hostname or "") == "open.spotify.com":
                return final
            # Some short links land on an app-launch page; the real link is in it.
            m = re.search(r'https://open\.spotify\.com/[^"\'<> ]+', r.read(500_000).decode("utf-8", "replace"))
            return html.unescape(m.group(0)) if m else final
    except OSError as e:
        raise FetchError("Couldn't open that Spotify short link. Try copying the song link again.") from e


def download(url: str, dest: Path) -> tuple[Path, str, dict]:
    """Download the audio behind a link. Returns (file path, title, {track, artist, channel})."""
    import yt_dlp

    if not is_url(url):
        raise FetchError("That does not look like a web link. It should start with http:// or https://.")
    started = time.monotonic()

    def too_slow(status: dict) -> None:
        if status.get("status") == "downloading" and time.monotonic() - started > DOWNLOAD_SECONDS:
            raise yt_dlp.utils.DownloadError("too slow")

    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(dest / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "socket_timeout": 30,
        "progress_hooks": [too_slow],
        "js_runtimes": tools.js_runtimes(),  # YouTube needs a JavaScript runtime
        "ffmpeg_location": tools.ffmpeg(),
        **_proxy(),
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
        if _refused(e):
            raise FetchError(REFUSED) from e
        if time.monotonic() - started > DOWNLOAD_SECONDS:
            raise FetchError("That link was far too slow to download. Try another link, or upload the song file.") from e
        raise FetchError(f"Could not download that link. {_short(e)}") from e
    if not path.exists():
        found = sorted(dest.glob("source.*"))
        if not found:
            raise FetchError("The download finished but no audio file was saved.")
        path = found[0]
    about = {"track": str(info.get("track") or ""), "artist": str(info.get("artist") or info.get("creator") or ""),
             "channel": str(info.get("channel") or info.get("uploader") or "")}
    return path, info.get("title") or "Untitled song", about


def use_upload(src: Path, dest: Path, original_name: str) -> tuple[Path, str]:
    suffix = Path(original_name).suffix.lower()[:8] or ".audio"
    target = dest / f"source{suffix}"
    shutil.copyfile(src, target)
    return target, Path(original_name).stem or "Untitled song"


def _proxy() -> dict:
    """On the website, links go through a Cloudflare WARP tunnel (see cloud_worker), since YouTube refuses AWS."""
    proxy = os.environ.get("CHOIR_SOLFA_PROXY")
    return {"proxy": proxy} if proxy else {}


REFUSED = ("YouTube would not let us download this song. Save the song on your phone or computer, "
           "then use Choose an audio file instead.")


def _refused(e: Exception) -> bool:
    """YouTube asks servers (such as the website's) to sign in to prove they aren't robots."""
    text = str(e).lower()
    return "confirm you" in text and "not a bot" in text or "sign in to confirm" in text


def _short(e: Exception) -> str:
    text = str(e).replace("ERROR: ", "").strip()
    return text.splitlines()[0][:200] if text else ""
