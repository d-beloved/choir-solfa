"""From a song to a solfa PDF and a MIDI file."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, lyrics as lyr
from .fetch import Song, get_song, is_url, use_upload
from .harmony import harmonize
from .midi import write_midi
from .models import METERS, Score
from .notation import score_text
from .pdf import write_pdf
from .rhythm import (align_to_bars, beat_grid, choose_bar_phase, fold_fast_beats, guess_meter, quantize, refine_beats,
                     tempo_from_beats, time_to_cell)
from .theory import PARTS, choose_doh_midi, detect_key, doh_pc_for, key_label, scale_letters
from .voices import clean_part, merge_resung, split_choir

Progress = Callable[[str, float], None]

MIX_SR = 44100
SR = audio.ANALYSIS_SR
CHOIR_POLYPHONY = 0.3


@dataclass
class Result:
    title: str
    kind: str
    key: str
    meter: str
    tempo: int
    bars: int
    pdf: Path
    midi: Path
    found_on: str | None = None
    found_url: str | None = None
    letters: list[str] = field(default_factory=list)  # d r m f s l t as letter names
    # The words: "you" (given) or "online" (found) when printed, "missing" (none found for a link),
    # "unplaced" (given or found but they didn't fit the tune), or None (none given for a file).
    words: str | None = None
    words_found: str | None = None  # what was found online, e.g. "Silent Night by Tyler Shaw"

    def to_dict(self) -> dict:
        return {
            "title": self.title, "kind": self.kind, "key": self.key, "meter": self.meter,
            "tempo": self.tempo, "bars": self.bars, "pdf": self.pdf.name, "midi": self.midi.name,
            "found_on": self.found_on, "found_url": self.found_url, "letters": self.letters,
            "words": self.words, "words_found": self.words_found,
        }


def run(source: str, workdir: Path, *, mode: str = "auto", meter: str = "auto", title: str | None = None,
        upload_name: str | None = None, progress: Progress = lambda s, f: None,
        max_seconds: float | None = None, lyrics: str | None = None) -> Result:
    if mode not in ("auto", "choir", "solo"):
        raise ValueError("mode must be auto, choir or solo")
    if meter != "auto" and meter not in METERS:
        raise ValueError(f"time signature must be auto or one of {', '.join(METERS)}")
    workdir.mkdir(parents=True, exist_ok=True)

    progress("Getting the song", 0.02)
    if is_url(source):
        song = get_song(source, workdir, lambda stage: progress(stage, 0.04))
        path, found_title = song.path, song.title
        found_on, found_url = song.found_on, song.found_url
    else:
        path, found_title = use_upload(Path(source), workdir, upload_name or Path(source).name)
        found_on = found_url = None
        song = None
    title = (title or found_title).strip() or "Untitled song"

    progress("Reading the audio", 0.08)
    mix = audio.decode(path, MIX_SR, 2)
    duration = mix.shape[1] / MIX_SR
    if max_seconds and duration > max_seconds + 1:
        raise audio.AudioError(f"This recording is {duration / 60:.0f} minutes long. Songs can be up to "
                               f"{max_seconds / 60:.0f} minutes: trim it, or choose a shorter recording.")

    words = None
    if lyrics and lyrics.strip():
        lines = lyr.parse(lyrics)
        words = lyr.Words(lines) if lines else None
    elif song is not None:
        progress("Looking for the words", 0.09)
        words = find_words(song, duration)

    progress("Separating the voices from the instruments", 0.1)
    vocals, accomp = audio.separate_vocals(mix, lambda f: progress("Separating the voices from the instruments", 0.1 + 0.5 * f))
    vocals_path = workdir / "vocals.wav"
    audio.write_wav(vocals_path, vocals, MIX_SR)

    mix_m = audio.to_mono(mix, MIX_SR)
    voc_m = audio.to_mono(vocals, MIX_SR)
    acc_m = audio.to_mono(accomp, MIX_SR)

    voice_found = _rms(voc_m) >= 0.05 * _rms(mix_m)
    a_cappella = not voice_found or _rms(acc_m) < 0.15 * _rms(voc_m)
    if a_cappella:
        # Nothing much but voices (or separation found none): listen to the whole
        # mix, since separation tends to drop the lowest voices into the "bass" stem.
        vocals_path, voc_m = workdir / "mix.wav", mix_m
        audio.write_wav(vocals_path, mix, MIX_SR)

    progress("Listening for the notes", 0.62)
    heard = audio.polyphonic_notes(vocals_path)
    poly = audio.polyphony(heard, duration)
    kind = mode if mode != "auto" else ("choir" if poly >= CHOIR_POLYPHONY else "solo")

    progress("Finding the beat", 0.74)
    beats = audio.find_beats(mix_m)
    if not a_cappella:
        # Instruments usually keep steadier time than voices, but only use them
        # if they play through most of the song (not just an intro).
        acc_beats = audio.find_beats(acc_m)
        if len(beats) and len(acc_beats) >= 0.85 * len(beats):
            beats = acc_beats
    strong = [n for n in heard if n.amp >= 0.3]
    beats = refine_beats(fold_fast_beats(beats, strong), strong)
    guessed = meter == "auto"
    if guessed:
        meter = guess_meter(beats, [n for n in heard if n.amp >= 0.3], audio.beat_chroma(mix_m, beats))
    m = METERS[meter]
    cells = beat_grid(beats, duration, m.cells_per_beat)
    tempo = tempo_from_beats(beats)

    progress("Working out the key", 0.78)
    weights = audio.key_weights(mix_m)
    hist = np.zeros(12)
    for n in heard:
        hist[n.pitch % 12] += (n.end - n.start) * n.amp
    if hist.sum() > 0:
        weights = weights + 0.5 * hist / hist.sum()
    tonic_pc, key_mode, _ = detect_key(weights)
    doh_pc = doh_pc_for(tonic_pc, key_mode)

    notes_lines: list[str] = []
    if kind == "choir":
        progress("Sorting the notes into soprano, alto, tenor and bass", 0.82)
        grid = quantize([n for n in heard if n.amp >= 0.2], cells, m.cells_per_pulse)
        attacks = audio.onset_cells(voc_m, cells)
        parts = {k: clean_part(merge_resung(v, attacks)) for k, v in split_choir(grid, len(cells)).items()}
        phase = choose_bar_phase(parts["Soprano"] + parts["Bass"], m)
        parts, total, shift = align_to_bars(parts, m, phase)
        notes_lines.append("Parts as heard in the recording. Voices that overlap are hard to tell apart, so check each line by ear.")
    else:
        progress("Following the lead melody", 0.82)
        melody = clean_part(audio.melody_on_grid(voc_m, cells), min_len=2)
        if not melody:
            raise audio.AudioError("Could not hear a clear melody in this song.")
        median = float(np.median([n.pitch for n in melody]))
        shift = 12 * int(round((67 - median) / 12))
        if shift:
            for n in melody:
                n.pitch += shift
        phase = choose_bar_phase(melody, m)
        aligned, total, shift = align_to_bars({"Soprano": melody}, m, phase)
        progress("Writing alto, tenor and bass", 0.9)
        parts = {"Soprano": aligned["Soprano"], **harmonize(aligned["Soprano"], doh_pc, key_mode, m, total)}
        notes_lines.append("Solo song: the melody is the soprano line. Alto, tenor and bass are a suggested arrangement.")
        if shift > 0:
            notes_lines.append("The melody was sung by a lower voice and has been written an octave up for sopranos.")
        elif shift < 0:
            notes_lines.append("The melody has been written an octave down to suit sopranos.")

    placed: dict[int, str] = {}
    if words:
        progress("Putting the words under the notes", 0.92)
        placed = lyr.place(words.lines, parts["Soprano"], total, m,
                           (lambda t: time_to_cell(cells, t) + shift) if words.timed else None)
    if placed:
        notes_lines.append("The words were placed under the soprano automatically. Check where each syllable falls.")
    words_state = ("you" if words.source == "you" else "online") if placed else \
        ("unplaced" if words else ("missing" if song is not None else None))

    if guessed:
        notes_lines.append(f"Time {meter} was worked out from the recording. If the bars look wrong, pick the time yourself and run it again.")
    parts = {p: parts.get(p, []) for p in PARTS}
    score = Score(
        title=title, doh_pc=doh_pc, tonic_pc=tonic_pc, mode=key_mode, meter=m, tempo_bpm=tempo,
        doh_midi=choose_doh_midi(doh_pc, parts), total_cells=total, parts=parts, kind=kind, notes=notes_lines,
        words=placed,
    )

    progress("Making the PDF", 0.95)
    pdf_path, midi_path = workdir / "score.pdf", workdir / "score.mid"
    write_pdf(score, str(pdf_path))
    write_midi(score, str(midi_path))
    (workdir / "score.txt").write_text(score_text(score), encoding="utf-8")

    result = Result(title, kind, key_label(doh_pc, tonic_pc, key_mode), meter, int(round(tempo)),
                    total // m.cells_per_bar, pdf_path, midi_path, found_on, found_url, scale_letters(doh_pc),
                    words_state, words.found if placed and words and words.source == "online" else None)
    (workdir / "result.json").write_text(json.dumps({**result.to_dict(), "polyphony": round(poly, 3)}, indent=2))
    progress("Done", 1.0)
    return result


def find_words(song: Song, duration: float) -> lyr.Words | None:
    """The song's words from the lyrics library, or None. Never fails the song."""
    title = song.track or lyr.clean_title(song.title)
    try:
        return lyr.find(title, song.artist, duration, also=f"{song.title} {song.channel} {song.found_on or ''}")
    except Exception:
        return None


def _rms(y: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(y)))) if y.size else 0.0
