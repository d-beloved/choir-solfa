"""Split the notes heard in a choir recording into soprano, alto, tenor and bass."""
from __future__ import annotations

import itertools

from .models import Meter, Note, QNote
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


def fold_octaves(notes: list[QNote], reach: int = 6, limit: int = 7) -> list[QNote]:
    """Move notes heard in the wrong octave back next to their neighbours.

    Pitch trackers often jump an octave (or two) up or down for a note or two.
    A note further than `limit` semitones from the middle of the notes around
    it (`reach` either side, longer notes counting more) is moved by whole
    octaves towards them. Real leaps that wide are rare in a sung tune.
    """
    notes = [QNote(n.pitch, n.start, n.end, n.amp) for n in sorted(notes, key=lambda n: n.start)]
    for _ in range(2):
        for i, n in enumerate(notes):
            around = notes[max(0, i - reach):i] + notes[i + 1:i + 1 + reach]
            if not around:
                continue
            middle = _weighted_median([m.pitch for m in around], [m.length for m in around])
            if abs(n.pitch - middle) > limit:
                n.pitch += 12 * round((middle - n.pitch) / 12)
    return notes


def drop_strays(notes: list[QNote], gap: int, longest: int) -> list[QNote]:
    """Leave out short notes (up to `longest` cells) with at least `gap` empty cells on both sides.

    These are leftovers from talking, breathing or the band, not part of the tune.
    """
    notes = sorted(notes, key=lambda n: n.start)
    out = []
    for i, n in enumerate(notes):
        before = n.start - notes[i - 1].end if i > 0 else gap
        after = notes[i + 1].start - n.end if i + 1 < len(notes) else gap
        if n.length <= longest and before >= gap and after >= gap:
            continue
        out.append(n)
    return out


MAJOR_STEPS = (0, 2, 4, 5, 7, 9, 11)
SOPRANO_MIDDLE = (61, 76)  # where the middle of a soprano phrase sits comfortably


def choir_tune(notes: list[QNote], doh_pc: int, mode: str, meter: Meter, *, soprano: bool = True) -> list[QNote]:
    """The tune as a choir would sing it, from what a soloist sang.

    Soloists slide between notes, bend them (blue notes) and sing a little
    ahead of or behind the beat. A choir sings the plain tune, so:
    notes outside the key move to the nearest note in it (joining the note a
    slide came from or went to), quick flicks to a neighbouring note and back
    are smoothed over, and notes start on the eighth-note grid. With `soprano`,
    phrases sung too low or too high for sopranos move by an octave.
    """
    scale = {(doh_pc + d) % 12 for d in MAJOR_STEPS}
    if mode == "minor":
        scale.add((doh_pc + 8) % 12)  # se, the raised seventh of the lah mode
    notes = [QNote(n.pitch, n.start, n.end, n.amp) for n in sorted(notes, key=lambda n: n.start)]

    def touching(a: QNote | None, b: QNote | None) -> bool:
        return a is not None and b is not None and a.end == b.start

    # 0. Soloists drop a phrase low (or take it high); sopranos sing it in their range.
    phrases: list[list[QNote]] = []
    for n in notes:
        if phrases and n.start - phrases[-1][-1].end < meter.cells_per_beat:
            phrases[-1].append(n)
        else:
            phrases.append([n])
    for phrase in phrases if soprano else []:
        middle = _weighted_median([n.pitch for n in phrase], [n.length for n in phrase])
        move = 12 if middle < SOPRANO_MIDDLE[0] else (-12 if middle > SOPRANO_MIDDLE[1] else 0)
        for n in phrase:
            n.pitch += move

    # 1. Into the key.
    moved = set()
    for i, n in enumerate(notes):
        if n.pitch % 12 in scale:
            continue
        prev = notes[i - 1] if i > 0 and touching(notes[i - 1], n) else None
        nxt = notes[i + 1] if i + 1 < len(notes) and touching(n, notes[i + 1]) else None
        options = [p for p in (n.pitch - 1, n.pitch + 1) if p % 12 in scale]
        if not options:
            continue
        joins = [m for m in (prev, nxt) if m is not None and m.pitch in options]
        if joins:
            n.pitch = max(joins, key=lambda m: m.length).pitch
        else:
            around = [m.pitch for m in (notes[i - 1] if i > 0 else None,
                                        notes[i + 1] if i + 1 < len(notes) else None) if m is not None]
            aim = sum(around) / len(around) if around else n.pitch
            n.pitch = min(options, key=lambda p: (abs(p - aim), p))
        moved.add(id(n))

    # 2. A moved note that lands on its neighbour's pitch was a slide: join them.
    # So is a quick flick away and back (A B A with a short B).
    out: list[QNote] = []
    for n in notes:
        last = out[-1] if out else None
        if touching(last, n) and last.pitch == n.pitch and (id(n) in moved or id(last) in moved):
            last.end = n.end
            continue
        if (len(out) >= 2 and touching(out[-2], last) and touching(last, n) and out[-2].pitch == n.pitch
                and last.length <= meter.cells_per_pulse // 2 and abs(last.pitch - n.pitch) <= 2):
            out.pop()
            out[-1].end = n.end
            continue
        out.append(n)

    # 3. Onto the eighth-note grid (half a pulse in simple time, a pulse in 6/8).
    step = meter.cells_per_pulse // 2 if meter.cells_per_pulse >= 4 else meter.cells_per_pulse

    def strength(c: int) -> int:
        return (c % meter.cells_per_bar == 0) + (c % meter.cells_per_beat == 0) + (c % meter.cells_per_pulse == 0)

    def snap(c: int) -> int:
        lo = c - c % step
        if lo == c:
            return c
        hi = lo + step
        if c - lo != hi - c:
            return lo if c - lo < hi - c else hi
        return hi if strength(hi) > strength(lo) else lo

    snapped: list[QNote] = []
    for n in out:
        start = snap(n.start)
        end = max(start + step, snap(n.end))
        if snapped and start < snapped[-1].end:
            prev = snapped[-1]
            if start <= prev.start:
                # Both land on the same spot: keep the longer of the two.
                if n.length > prev.end - prev.start:
                    snapped[-1] = QNote(n.pitch, prev.start, max(end, prev.end), n.amp)
                continue
            prev.end = start
        snapped.append(QNote(n.pitch, start, end, n.amp))
    return snapped


def drop_overtones(notes: list[Note], intervals: tuple[int, ...] = (12, 19, 24)) -> list[Note]:
    """Leave out notes an octave (or a twelfth, or two octaves) above another note sounding with them.

    In a group of backing singers the note finder often hears the same line twice: the
    voices' overtones, or men and women singing the tune an octave apart. Either way it is
    one line, written once.
    """
    by_pitch: dict[int, list[Note]] = {}
    for m in notes:
        by_pitch.setdefault(m.pitch, []).append(m)
    out = []
    for n in notes:
        least = 0.5 * (n.end - n.start)
        below = (m for i in intervals for m in by_pitch.get(n.pitch - i, ()))
        if not any(min(n.end, m.end) - max(n.start, m.start) >= least for m in below):
            out.append(n)
    return out


def _weighted_median(values: list[int], weights: list[int]) -> float:
    pairs = sorted(zip(values, weights))
    half, run = sum(w for _, w in pairs) / 2, 0
    for v, w in pairs:
        run += w
        if run >= half:
            return float(v)
    return float(pairs[-1][0])


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
