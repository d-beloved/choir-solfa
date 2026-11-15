"""Keys, solfa syllables and voice ranges."""
from __future__ import annotations

import numpy as np

PC_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

# Syllable for each semitone above doh (movable doh, Curwen style).
SYLLABLES = ["d", "de", "r", "ma", "m", "f", "fe", "s", "se", "l", "ta", "t"]

PARTS = ("Soprano", "Alto", "Tenor", "Bass")
MALE = frozenset({"Tenor", "Bass"})

# Comfortable sounding ranges as MIDI numbers.
RANGES = {
    "Soprano": (60, 81),
    "Alto": (53, 74),
    "Tenor": (48, 69),
    "Bass": (40, 64),
}
CENTERS = {"Soprano": 69, "Alto": 62, "Tenor": 57, "Bass": 50}

# Krumhansl-Kessler key profiles.
_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def detect_key(weights) -> tuple[int, str, float]:
    """Return (tonic pitch class, mode, confidence) for 12 pitch-class weights."""
    w = np.asarray(weights, dtype=float)
    if w.shape != (12,) or w.sum() <= 0 or np.allclose(w, w[0]):
        return 0, "major", 0.0
    scored = []
    for tonic in range(12):
        for mode, profile in (("major", _MAJOR), ("minor", _MINOR)):
            r = float(np.corrcoef(w, np.roll(profile, tonic))[0, 1])
            scored.append((r, tonic, mode))
    scored.sort(reverse=True)
    r, tonic, mode = scored[0]
    return tonic, mode, r - scored[1][0]


def doh_pc_for(tonic_pc: int, mode: str) -> int:
    """In tonic solfa a minor key is sung in the lah mode, so doh is the relative major."""
    return tonic_pc % 12 if mode == "major" else (tonic_pc + 3) % 12


def written_pitch(part: str, pitch: int) -> int:
    """Tenor and bass are written an octave above where they sound."""
    return pitch + 12 if part in MALE else pitch


def solfa(pitch: int, doh_midi: int) -> tuple[str, int]:
    """Syllable and octave mark (0 = unmarked, 1 = d¹, -1 = d₁) for a written pitch."""
    rel = pitch - doh_midi
    return SYLLABLES[rel % 12], rel // 12


def choose_doh_midi(doh_pc: int, parts: dict) -> int:
    """Pick the octave of doh that leaves the fewest octave marks on the page.

    Soprano and alto count most: by custom their lines sit mostly unmarked.
    """
    candidates = [m for m in range(48, 72) if m % 12 == doh_pc % 12]
    weight = {"Soprano": 2.0, "Alto": 1.5, "Tenor": 1.0, "Bass": 0.5}

    def cost(m: int) -> float:
        total = 0.0
        for name, notes in parts.items():
            for n in notes:
                octave = (written_pitch(name, n.pitch) - m) // 12
                total += weight.get(name, 1.0) * abs(octave) * max(1, n.end - n.start)
        return total + abs(m - 60) * 1e-3

    return min(candidates, key=cost)


_LETTERS = "CDEFGAB"
_NATURAL = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
_MAJOR_STEPS = (0, 2, 4, 5, 7, 9, 11)
_ACCIDENTAL = {0: "", 1: "#", 2: "##", -1: "b", -2: "bb"}


def scale_letters(doh_pc: int) -> list[str]:
    """Letter names of d r m f s l t for this doh, spelled as in the key (doh F# gives E#, not F)."""
    start = _LETTERS.index(PC_NAMES[doh_pc % 12][0])
    out = []
    for i, step in enumerate(_MAJOR_STEPS):
        letter = _LETTERS[(start + i) % 7]
        diff = (doh_pc + step - _NATURAL[letter]) % 12
        out.append(letter + _ACCIDENTAL[diff - 12 if diff > 6 else diff])
    return out


def key_label(doh_pc: int, tonic_pc: int, mode: str) -> str:
    label = f"Doh is {PC_NAMES[doh_pc]}"
    if mode == "minor":
        label += f" (minor: lah is {PC_NAMES[tonic_pc]})"
    return label
