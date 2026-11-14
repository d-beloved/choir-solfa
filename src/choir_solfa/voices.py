"""Split the notes heard in a choir recording into soprano, alto, tenor and bass."""
from __future__ import annotations

import itertools

from .models import QNote
from .theory import CENTERS, PARTS, RANGES


def _choose(pitches: list[int], prev: list) -> tuple:
    """Best voice assignment for one cell. `pitches` is sorted high to low.

    Returns a 4-tuple of indexes into `pitches` (or None for a resting voice).
    Voices may not cross: soprano >= alto >= tenor >= bass.
    """
    options = list(range(len(pitches))) + [None]
    best_cost, best = None, (None,) * 4
    for combo in itertools.product(options, repeat=4):
        idx = [i for i in combo if i is not None]
        if any(idx[k] > idx[k + 1] for k in range(len(idx) - 1)):
            continue
        used = set(idx)
        cost = 3.0 * (len(pitches) - len(used)) + 0.6 * (len(idx) - len(used))
        for v, i in enumerate(combo):
            name = PARTS[v]
            if i is None:
                cost += 1.5
                continue
            p = pitches[i]
            lo, hi = RANGES[name]
            cost += max(0, lo - p) * 0.8 + max(0, p - hi) * 0.8
            cost += abs(p - CENTERS[name]) * 0.05
            if prev[v] is not None:
                cost += min(abs(p - prev[v]), 12) * 0.15
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, combo
    return best


def split_choir(qnotes: list[QNote], total: int, max_notes: int = 5) -> dict[str, list[QNote]]:
    # Gather what sounds in each cell: pitch -> (loudest amp, starts here?)
    active: list[dict] = [dict() for _ in range(total)]
    for n in qnotes:
        for c in range(max(0, n.start), min(total, n.end)):
            amp, on = active[c].get(n.pitch, (0.0, False))
            active[c][n.pitch] = (max(amp, n.amp), on or c == n.start)

    voice_pitch = [[None] * total for _ in range(4)]
    voice_onset = [[False] * total for _ in range(4)]
    prev: list = [None] * 4
    cache_key, cache_val = None, None
    for c in range(total):
        heard = active[c]
        if not heard:
            continue
        loudest = sorted(heard, key=lambda p: -heard[p][0])[:max_notes]
        pitches = sorted(loudest, reverse=True)
        key = (tuple(pitches), tuple(prev))
        if key != cache_key:
            cache_key, cache_val = key, _choose(pitches, prev)
        for v, i in enumerate(cache_val):
            if i is None:
                continue
            p = pitches[i]
            voice_pitch[v][c] = p
            restarted = heard[p][1]
            voice_onset[v][c] = restarted or c == 0 or voice_pitch[v][c - 1] != p
            prev[v] = p

    parts = {}
    for v, name in enumerate(PARTS):
        parts[name] = clean_part(_runs(voice_pitch[v], voice_onset[v]))
    return parts


def _runs(pitch: list, onset: list) -> list[QNote]:
    notes: list[QNote] = []
    for c, p in enumerate(pitch):
        if p is None:
            continue
        if onset[c] or not notes or notes[-1].end != c or notes[-1].pitch != p:
            notes.append(QNote(p, c, c + 1))
        else:
            notes[-1].end = c + 1
    return notes


def merge_resung(notes: list[QNote], onset_cells: set[int] | None, tol: int = 1) -> list[QNote]:
    """Join a note to the same pitch straight after it unless the audio has a new attack there.

    Note detectors often split one held note in two (a wobble in loudness or
    vibrato); a real re-sung note almost always has a clear onset.
    """
    if onset_cells is None:
        return notes
    out: list[QNote] = []
    for n in sorted(notes, key=lambda n: n.start):
        last = out[-1] if out else None
        if (last and last.pitch == n.pitch and 0 <= n.start - last.end <= 1
                and not any(c in onset_cells for c in range(n.start - tol, n.start + tol + 1))):
            last.end = max(last.end, n.end)
        else:
            out.append(QNote(n.pitch, n.start, n.end, n.amp))
    return out


def clean_part(notes: list[QNote], min_len: int = 2, max_gap: int = 2) -> list[QNote]:
    """Tidy one part for the page.

    Very short notes are folded into a neighbour, and tiny gaps (a singer
    letting go early, or a breath) are filled by holding the note before.
    """
    notes = [QNote(n.pitch, n.start, n.end, n.amp) for n in sorted(notes, key=lambda n: n.start)]
    for a, b in zip(notes, notes[1:]):
        if 0 < b.start - a.end <= max_gap:
            a.end = b.start
    out: list[QNote] = []
    for i, n in enumerate(notes):
        if n.length >= min_len:
            out.append(n)
            continue
        if out and out[-1].end == n.start:
            out[-1].end = n.end
        elif i + 1 < len(notes) and notes[i + 1].start == n.end:
            notes[i + 1].start = n.start
        # otherwise an isolated blip: drop it
    return out
