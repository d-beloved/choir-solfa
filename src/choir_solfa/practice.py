"""Practice tracks: the recording with a part played on top in a soft, voice-like tone.

Singers hear their line inside the real song, in time with it (the notes follow the
recording's own beat, not a steady tempo), and anyone checking the score hears at
once where a note clashes with what was sung.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

import numpy as np

from . import tools
from .models import QNote
from .theory import LINES, PARTS

SR = 22050
TRACKS = {name: f"{name.lower()}.m4a" for name in LINES} | {"All parts": "all.m4a"}
# How loud each part is in the track with all four (the choir's parts, not the lead): the tune leads.
ALL_LEVELS = {"Soprano": 1.0, "Alto": 0.5, "Tenor": 0.5, "Bass": 0.55}


def tone(notes: list[QNote], cell_time: Callable[[int], float], length: int, sr: int = SR) -> np.ndarray:
    """The notes as a soft sung-sounding tone (a few harmonics and a gentle vibrato)."""
    out = np.zeros(length, dtype=np.float32)
    for n in notes:
        a, b = int(cell_time(n.start) * sr), int(cell_time(n.end) * sr)
        b = min(b, length)
        if a < 0 or b - a < sr // 50:
            continue
        t = np.arange(b - a) / sr
        freq = 440.0 * 2 ** ((n.pitch - 69 + 0.12 * np.sin(2 * np.pi * 5.0 * t)) / 12)
        phase = 2 * np.pi * np.cumsum(freq) / sr
        wave = np.sin(phase) + 0.35 * np.sin(2 * phase) + 0.12 * np.sin(3 * phase)
        envelope = np.minimum(1.0, np.minimum(t / 0.03, (t[-1] - t + 1e-9) / 0.08))
        out[a:b] += (wave * envelope).astype(np.float32)
    return out


def _peak(x: np.ndarray) -> np.ndarray:
    top = float(np.abs(x).max()) if x.size else 0.0
    return x / top if top > 0 else x


def write_tracks(recording: np.ndarray, parts: dict[str, list[QNote]], cell_time: Callable[[int], float],
                 folder: Path, sr: int = SR) -> dict[str, Path]:
    """One track per part, and one with all of them, each over the (quieter) recording.

    `recording` is mono at `sr`; `cell_time` gives the time in seconds of a score cell.
    Returns {part name: file} for the tracks written.
    """
    song = _peak(recording.astype(np.float32))
    tones = {name: _peak(tone(parts.get(name) or [], cell_time, len(song), sr)) for name in LINES}
    written = {}
    for name, filename in TRACKS.items():
        if name == "All parts":
            line = _peak(sum(ALL_LEVELS[p] * tones[p] for p in PARTS))
        else:
            line = tones[name]
        if not line.any():
            continue
        mixed = 0.5 * song + 0.5 * line
        mixed /= max(1.0, float(np.abs(mixed).max()))
        path = folder / filename
        _encode(mixed, sr, path)
        written[name] = path
    return written


def _encode(samples: np.ndarray, sr: int, path: Path) -> None:
    pcm = (np.clip(samples, -1, 1) * 32767).astype(np.int16).tobytes()
    subprocess.run([tools.ffmpeg(), "-v", "error", "-nostdin", "-y", "-f", "s16le", "-ar", str(sr), "-ac", "1",
                    "-i", "-", "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(path)],
                   input=pcm, check=True, capture_output=True)
