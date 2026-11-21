"""Export the four parts as a MIDI file for practice."""
from __future__ import annotations

import pretty_midi

from .models import Score
from .theory import PARTS


def write_midi(score: Score, path: str) -> None:
    pm = pretty_midi.PrettyMIDI(initial_tempo=score.tempo_bpm)
    seconds_per_cell = 60.0 / score.tempo_bpm / score.meter.cells_per_beat
    for name in PARTS:
        notes = score.parts.get(name)
        if notes is None:
            continue
        inst = pretty_midi.Instrument(program=52, name=name)  # Choir Aahs
        for n in notes:
            inst.notes.append(pretty_midi.Note(
                velocity=90, pitch=int(n.pitch),
                start=n.start * seconds_per_cell, end=n.end * seconds_per_cell,
            ))
        pm.instruments.append(inst)
    for cell, text in sorted(score.words.items()):
        pm.lyrics.append(pretty_midi.Lyric(text=text, time=cell * seconds_per_cell))
    pm.write(path)
