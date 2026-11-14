"""Beat grid, quantising notes onto it, and finding where bars start."""
from __future__ import annotations

import math

import numpy as np

from .models import Meter, Note, QNote


def beat_grid(beat_times, duration: float, cells_per_beat: int, fallback_bpm: float = 100.0) -> np.ndarray:
    """Start time of every grid cell, covering 0..duration."""
    beats = sorted(float(b) for b in beat_times)
    if len(beats) >= 2:
        step = float(np.median(np.diff(beats)))
    else:
        step = 60.0 / fallback_bpm
        beats = beats or [0.0]
    step = max(step, 0.15)
    while beats[0] > 0:
        beats.insert(0, beats[0] - step)
    while beats[-1] < duration + step:
        beats.append(beats[-1] + step)
    cells = []
    for a, b in zip(beats[:-1], beats[1:]):
        cells.extend(np.linspace(a, b, cells_per_beat, endpoint=False))
    return np.array(cells)


def refine_beats(beat_times, notes: list[Note]) -> np.ndarray:
    """Shift the beat grid so it lines up with where notes actually start.

    Beat trackers often place beats a little late. Take the weighted circular
    mean of where each note starts within its beat and move every beat by that.
    """
    beats = np.asarray(beat_times, dtype=float)
    if len(beats) < 4 or not notes:
        return beats
    step = float(np.median(np.diff(beats)))
    vec, total = 0j, 0.0
    for n in notes:
        i = int(np.searchsorted(beats, n.start)) - 1
        if 0 <= i < len(beats) - 1:
            frac = (n.start - beats[i]) / (beats[i + 1] - beats[i])
            w = n.amp * min(n.end - n.start, 2 * step)
            vec += w * np.exp(2j * np.pi * frac)
            total += w
    if total == 0:
        return beats
    offset = float(np.angle(vec) / (2 * np.pi))
    if abs(offset) < 0.03:
        return beats
    return beats + offset * step


def tempo_from_beats(beat_times) -> float:
    beats = np.asarray(beat_times, dtype=float)
    if len(beats) < 2:
        return 100.0
    return float(60.0 / np.median(np.diff(beats)))


def time_to_cell(cell_times: np.ndarray, t: float) -> int:
    i = int(np.searchsorted(cell_times, t))
    if i <= 0:
        return 0
    if i >= len(cell_times):
        return len(cell_times) - 1
    return i if (cell_times[i] - t) < (t - cell_times[i - 1]) else i - 1


def _cell_position(cell_times: np.ndarray, t: float) -> float:
    """Where t falls on the grid, in fractional cells."""
    i = int(np.searchsorted(cell_times, t, side="right")) - 1
    if i < 0:
        return 0.0
    if i >= len(cell_times) - 1:
        return float(len(cell_times) - 1)
    return i + (t - cell_times[i]) / (cell_times[i + 1] - cell_times[i])


# Extra cost (in cells) for landing a note edge off the pulse. Sung timing is
# loose, so a note edge only lands on a quarter pulse when it is clearly there.
_OFFBEAT_COST = {4: (0.0, 0.55, 0.15, 0.55), 2: (0.0, 0.3)}


def snap(cell_times: np.ndarray, t: float, cells_per_pulse: int) -> int:
    x = _cell_position(cell_times, t)
    costs = _OFFBEAT_COST.get(cells_per_pulse, (0.0,) * cells_per_pulse)
    lo, hi = int(math.floor(x)) - 1, int(math.ceil(x)) + 1
    best = min(range(max(0, lo), min(len(cell_times) - 1, hi) + 1),
               key=lambda g: abs(x - g) + costs[g % cells_per_pulse])
    return best


def quantize(notes: list[Note], cell_times: np.ndarray, cells_per_pulse: int = 1) -> list[QNote]:
    out = []
    for n in notes:
        s = snap(cell_times, n.start, cells_per_pulse)
        e = max(snap(cell_times, n.end, cells_per_pulse), s + 1)
        out.append(QNote(n.pitch, s, e, n.amp))
    return out


def guess_meter(beat_times, notes: list[Note], chroma=None) -> str:
    """Guess 3/4 or 4/4 from where the accents and the chord changes fall.

    Bars start where notes are longest and loudest and where the harmony moves,
    so look for the grouping of beats (2, 3 or 4) with the clearest such pattern.
    """
    beats = np.asarray(beat_times, dtype=float)
    if len(beats) < 12:
        return "4/4"
    step = float(np.median(np.diff(beats)))
    accent = np.zeros(len(beats))
    for n in notes:
        i = int(np.argmin(np.abs(beats - n.start)))
        if abs(beats[i] - n.start) <= 0.2 * step:
            accent[i] += n.amp * min(n.end - n.start, 2 * step)
    feats = [accent]
    if chroma is not None and chroma.shape[1] == len(beats):
        c = chroma / (np.linalg.norm(chroma, axis=0, keepdims=True) + 1e-9)
        change = np.r_[0.0, 1.0 - np.sum(c[:, 1:] * c[:, :-1], axis=0)]
        feats.append(change)
    f = sum((x - x.mean()) / (x.std() + 1e-9) for x in feats)

    def contrast(g: int) -> float:
        return max(float(f[p::g].mean()) for p in range(g)) - float(f.mean())

    triple, duple = contrast(3), max(contrast(2), contrast(4))
    return "3/4" if triple > 1.15 * duple else "4/4"


def choose_bar_phase(qnotes: list[QNote], meter: Meter) -> int:
    """Which tracked beat (0..beats_per_bar-1) most likely starts each bar.

    Notes that start on the first beat of a bar tend to be longer and louder,
    so score each phase by the weight of notes starting on it.
    """
    cpb = meter.cells_per_beat
    beats_per_bar = meter.cells_per_bar // cpb
    if beats_per_bar <= 1:
        return 0
    scores = np.zeros(beats_per_bar)
    for n in qnotes:
        if n.start % cpb == 0:
            beat = n.start // cpb
            scores[beat % beats_per_bar] += n.length * (0.5 + n.amp)
    return int(np.argmax(scores))


def align_to_bars(parts: dict[str, list[QNote]], meter: Meter, phase_beat: int) -> tuple[dict[str, list[QNote]], int, int]:
    """Shift notes so bars start at the chosen beat, then drop empty bars at the start.

    Returns (parts, total_cells, shift) where shift is the number of cells added
    to every original cell index (may be negative).
    """
    cpbar = meter.cells_per_bar
    shift = (-phase_beat * meter.cells_per_beat) % cpbar
    all_notes = [n for notes in parts.values() for n in notes]
    if not all_notes:
        return {k: [] for k in parts}, cpbar, shift
    first = min(n.start for n in all_notes) + shift
    shift -= (first // cpbar) * cpbar
    moved = {
        name: [QNote(n.pitch, n.start + shift, n.end + shift, n.amp) for n in notes]
        for name, notes in parts.items()
    }
    last = max(n.end for notes in moved.values() for n in notes)
    total = int(math.ceil(last / cpbar)) * cpbar
    return moved, total, shift
