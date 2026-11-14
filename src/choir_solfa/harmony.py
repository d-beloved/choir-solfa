"""Write alto, tenor and bass under a melody (for solo songs).

Two steps:
1. Pick a chord for every beat with a Viterbi search: chord tones should
   match the melody, common progressions score well, and chords change
   mostly on strong beats.
2. Voice each melody note: choose alto, tenor and bass notes from the chord,
   in range, below the melody, moving as little as possible and avoiding
   parallel fifths and octaves.

The result is hymn-style: the lower parts move with the melody's rhythm,
holding a note through a beat when the melody runs quickly.
"""
from __future__ import annotations

from .models import Meter, QNote

# Chord tones as semitones above doh.
CHORDS = {
    "I": (0, 4, 7),
    "ii": (2, 5, 9),
    "iii": (4, 7, 11),
    "IV": (5, 9, 0),
    "V": (7, 11, 2),
    "V7": (7, 11, 2, 5),
    "vi": (9, 0, 4),
    "III": (4, 8, 11),  # major chord on m: the dominant of a minor (lah) key
}

PROGRESSION = {
    ("V", "I"): 1.0, ("V7", "I"): 1.2, ("IV", "V"): 0.6, ("IV", "V7"): 0.6,
    ("ii", "V"): 0.8, ("ii", "V7"): 0.8, ("IV", "I"): 0.4, ("vi", "ii"): 0.5,
    ("vi", "IV"): 0.5, ("I", "IV"): 0.4, ("I", "V"): 0.4, ("I", "vi"): 0.3,
    ("iii", "vi"): 0.5, ("III", "vi"): 1.2, ("V", "vi"): 0.3, ("IV", "ii"): 0.2,
    ("I", "ii"): 0.2, ("vi", "III"): 0.4, ("ii", "III"): 0.3,
    ("V", "IV"): -0.6, ("V7", "IV"): -0.8, ("V7", "ii"): -0.6, ("ii", "I"): -0.4,
    ("V7", "V"): -0.5, ("iii", "IV"): -0.1,
}

ALTO, TENOR, BASS = (55, 74), (48, 67), (40, 60)


def _melody_cells(melody: list[QNote], total: int) -> tuple[list, list]:
    pitch: list = [None] * total
    onset = [False] * total
    for n in melody:
        for c in range(max(0, n.start), min(total, n.end)):
            pitch[c] = n.pitch
        if 0 <= n.start < total:
            onset[n.start] = True
    return pitch, onset


def choose_chords(melody: list[QNote], doh_pc: int, mode: str, meter: Meter, total: int) -> list[str]:
    """One chord name per beat."""
    cpb = meter.cells_per_beat
    beats_per_bar = max(1, meter.cells_per_bar // cpb)
    n_slots = max(1, total // cpb)
    pitch, onset = _melody_cells(melody, total)
    names = [c for c in CHORDS if mode == "minor" or c != "III"]
    tonic = "vi" if mode == "minor" else "I"
    dominants = {"III"} if mode == "minor" else {"V", "V7"}

    ordered = sorted(melody, key=lambda n: n.start)
    first_sung = ordered[0].start // cpb if ordered else 0
    # The cadence lands where the last note starts, not where it ends.
    final = ordered[-1].start // cpb if ordered else n_slots - 1

    def emission(slot: int, chord: str) -> float:
        tones = {(doh_pc + d) % 12 for d in CHORDS[chord]}
        score, weight = 0.0, 0.0
        for c in range(slot * cpb, (slot + 1) * cpb):
            p = pitch[c]
            if p is None:
                continue
            w = 1.0 + (1.0 if c == slot * cpb else 0.0) + (0.5 if onset[c] else 0.0)
            score += w if p % 12 in tones else -1.2 * w
            weight += w
        e = score / weight if weight else 0.0
        if chord == "V7":
            e -= 0.25
        if chord in ("iii", "III"):
            e -= 0.15
        if slot == first_sung and chord == tonic:
            e += 1.0
        if slot >= final and chord == tonic:
            e += 2.0
        if slot == final - 1 and chord in dominants:
            e += 0.5
        return e

    def change_cost(slot: int) -> float:
        if not any(pitch[c] is not None for c in range(slot * cpb, (slot + 1) * cpb)):
            return 1.0
        pos = slot % beats_per_bar
        if pos == 0:
            return 0.0
        if beats_per_bar % 2 == 0 and pos == beats_per_bar // 2:
            return 0.15
        return 0.5

    score = {c: emission(0, c) for c in names}
    back: list[dict] = []
    for s in range(1, n_slots):
        new, ptr = {}, {}
        cc = change_cost(s)
        for c in names:
            best_prev, best_val = None, None
            for p in names:
                v = score[p]
                if p != c:
                    v += PROGRESSION.get((p, c), 0.0) - cc
                if best_val is None or v > best_val:
                    best_prev, best_val = p, v
            new[c] = best_val + emission(s, c)
            ptr[c] = best_prev
        score = new
        back.append(ptr)
    chord = max(score, key=score.get)
    out = [chord]
    for ptr in reversed(back):
        chord = ptr[chord]
        out.append(chord)
    return out[::-1]


def _voicings(sop: int, chord: str, doh_pc: int) -> list[tuple[float, tuple[int, int, int]]]:
    degrees = CHORDS[chord]
    pcs = [(doh_pc + d) % 12 for d in degrees]
    root, third, fifth = pcs[0], pcs[1], pcs[2]
    pcset = set(pcs)
    out = []
    for b in range(BASS[0], BASS[1] + 1):
        if b % 12 not in pcset:
            continue
        for t in range(max(b, TENOR[0]), TENOR[1] + 1):
            if t % 12 not in pcset or t - b > 19:
                continue
            for a in range(max(t, ALTO[0]), min(ALTO[1], sop) + 1):
                if a % 12 not in pcset or a - t > 12 or sop - a > 12:
                    continue
                cost = 0.0
                bpc = b % 12
                if bpc == third:
                    cost += 0.8
                elif bpc != root:
                    cost += 2.5
                voices = [sop % 12, a % 12, t % 12, bpc]
                if third not in voices:
                    cost += 1.5
                if root not in voices:
                    cost += 2.0
                if len(pcs) > 3 and pcs[3] not in voices:
                    cost += 0.7
                if voices.count(third) > 1:
                    cost += 0.6
                if a == sop:
                    cost += 1.0
                if t == b:
                    cost += 1.0  # tenor and bass in unison sounds thin
                cost += 0.15 * max(0, b - 52)  # keep the bass low
                cost += 0.03 * (abs(a - 64) + abs(t - 57) + abs(b - 48))
                out.append((cost, (a, t, b)))
    out.sort()
    return out[:40]


def _motion_cost(prev_s: int, prev: tuple, s: int, cur: tuple) -> float:
    pa, pt, pb = prev
    a, t, b = cur
    cost = 0.25 * abs(a - pa) + 0.25 * abs(t - pt) + 0.12 * abs(b - pb)
    cost += 0.4 * max(0, abs(a - pa) - 5) + 0.4 * max(0, abs(t - pt) - 5)
    before, after = (prev_s, pa, pt, pb), (s, a, t, b)
    for i in range(4):
        for j in range(i + 1, 4):
            moved = before[i] != after[i] and before[j] != after[j]
            same_dir = (after[i] - before[i]) * (after[j] - before[j]) > 0
            if moved and same_dir:
                iv_before = (before[i] - before[j]) % 12
                iv_after = (after[i] - after[j]) % 12
                if iv_before == iv_after and iv_after in (0, 7):
                    cost += 3.0
    return cost


def harmonize(melody: list[QNote], doh_pc: int, mode: str, meter: Meter, total: int) -> dict[str, list[QNote]]:
    if not melody:
        return {"Alto": [], "Tenor": [], "Bass": []}
    cpb = meter.cells_per_beat
    chords = choose_chords(melody, doh_pc, mode, meter, total)

    # Split melody notes wherever the chord changes under them.
    segments = []  # (start, end, sop, chord)
    last_slot = len(chords) - 1
    for n in sorted(melody, key=lambda n: n.start):
        s = n.start
        while s < n.end:
            chord = chords[min(s // cpb, last_slot)]
            e = s
            while e < n.end and chords[min(e // cpb, last_slot)] == chord:
                e = min(n.end, (e // cpb + 1) * cpb)
            segments.append((s, e, n.pitch, chord))
            s = e

    # Viterbi over voicings.
    cands = [_voicings(sop, chord, doh_pc) or [(9.0, (min(sop, ALTO[1]), TENOR[0], BASS[0]))]
             for _, _, sop, chord in segments]
    cost = [c for c, _ in cands[0]]
    back = []
    for k in range(1, len(segments)):
        prev_sop, sop = segments[k - 1][2], segments[k][2]
        new_cost, ptr = [], []
        for c_static, cur in cands[k]:
            best_i, best_v = 0, None
            for i, (_, prev) in enumerate(cands[k - 1]):
                v = cost[i] + _motion_cost(prev_sop, prev, sop, cur)
                if best_v is None or v < best_v:
                    best_i, best_v = i, v
            new_cost.append(best_v + c_static)
            ptr.append(best_i)
        cost = new_cost
        back.append(ptr)
    i = min(range(len(cost)), key=cost.__getitem__)
    chosen = [i]
    for ptr in reversed(back):
        i = ptr[i]
        chosen.append(i)
    chosen.reverse()

    parts: dict[str, list[QNote]] = {"Alto": [], "Tenor": [], "Bass": []}
    for k, ((s, e, _, _), ci) in enumerate(zip(segments, chosen)):
        voicing = cands[k][ci][1]
        for name, p in zip(("Alto", "Tenor", "Bass"), voicing):
            notes = parts[name]
            last = notes[-1] if notes else None
            # Hold through quick melody notes: only re-sing on a beat or when the pitch changes.
            if last and last.end == s and last.pitch == p and s % cpb != 0:
                last.end = e
            else:
                notes.append(QNote(p, s, e))
    return parts
