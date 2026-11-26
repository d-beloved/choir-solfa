"""From a song to a solfa PDF, a text version and practice tracks."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

from . import audio, karaoke, lyrics as lyr, practice
from .fetch import Song, get_song, is_url, use_upload
from .harmony import harmonize
from .models import METERS, Score
from .notation import score_text
from .pdf import write_pdf
from .rhythm import (align_to_bars, beat_grid, choose_bar_phase, fold_fast_beats, guess_meter, quantize, refine_beats,
                     tempo_from_beats, time_to_cell)
from .theory import LEAD, LINES, PARTS, choose_doh_midi, detect_key, doh_pc_for, key_label, scale_letters
from .voices import choir_tune, clean_part, drop_overtones, drop_strays, fold_octaves, merge_resung, split_choir

Progress = Callable[[str, float], None]

MIX_SR = 44100
SR = audio.ANALYSIS_SR
CHOIR_POLYPHONY = 0.3
# Backing singers take the choir's part when they sing for at least this share of the song's singing.
BACKING_SHARE = 0.4
# Backing singers quieter than this beside the lead (where both sing) are hard to hear clearly.
QUIET_BACKING = 0.5
MODES = ("auto", "choir", "backing", "solo")


@dataclass
class Result:
    title: str
    kind: str
    key: str
    meter: str
    tempo: int
    bars: int
    pdf: Path
    found_on: str | None = None
    found_url: str | None = None
    letters: list[str] = field(default_factory=list)  # d r m f s l t as letter names
    # The words: "you" (given) or "online" (found) when printed, "missing" (none found for a link),
    # "unplaced" (given or found but they didn't fit the tune), or None (none given for a file).
    words: str | None = None
    words_found: str | None = None  # what was found online, e.g. "Silent Night by Tyler Shaw"
    tracks: dict[str, str] = field(default_factory=dict)  # practice tracks: part name -> file name

    def to_dict(self) -> dict:
        return {
            "title": self.title, "kind": self.kind, "key": self.key, "meter": self.meter,
            "tempo": self.tempo, "bars": self.bars, "pdf": self.pdf.name,
            "found_on": self.found_on, "found_url": self.found_url, "letters": self.letters,
            "words": self.words, "words_found": self.words_found, "tracks": self.tracks,
        }


def run(source: str, workdir: Path, *, mode: str = "auto", meter: str = "auto", title: str | None = None,
        upload_name: str | None = None, progress: Progress = lambda s, f: None,
        max_seconds: float | None = None, lyrics: str | None = None) -> Result:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
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
    kind = mode if mode in ("choir", "solo") else ("choir" if poly >= CHOIR_POLYPHONY else "solo")

    # A lead singer with backing singers: in many songs the lead sings the calls (or over
    # the top) and the backing singers sing the choir's part. Both lines are written.
    backing = None
    if mode == "backing" or (mode == "auto" and not a_cappella and poly < CHOIR_POLYPHONY):
        stage = "Listening for backing singers"
        progress(stage, 0.64)
        backing = find_backing(mix if a_cappella else vocals, workdir,
                               lambda f: progress(stage, 0.64 + 0.08 * f), always=mode == "backing")
        if backing:
            kind = "backing"

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
    if kind in ("choir", "backing"):
        progress("Sorting the notes into soprano, alto, tenor and bass", 0.82)
        source = drop_overtones(backing.notes) if backing else [n for n in heard if n.amp >= 0.2]
        grid = quantize(source, cells, m.cells_per_pulse)
        attacks = audio.onset_cells(backing.mono if backing else voc_m, cells)
        parts = {k: clean_part(merge_resung(v, attacks)) for k, v in split_choir(grid, len(cells)).items()}
        lead_octave = 0
        if backing:
            progress("Following the lead singer", 0.86)
            parts[LEAD], lead_octave = lead_line(backing, cells, m)
        phase = choose_bar_phase(parts["Soprano"] + parts["Bass"], m)
        parts, total, shift = align_to_bars(parts, m, phase)
        if backing:
            # Like a solo song's tune: phrases sung low (a man leading) are written in the treble.
            lead = choir_tune(parts.pop(LEAD), doh_pc, key_mode, m)
            parts, suggested = tidy_backing(parts, doh_pc, key_mode, m, total)
            parts = {LEAD: lead, **parts}
            notes_lines.append("Lead singer with backing singers: L is the lead singer's line, and S, A, T and B "
                               "are the backing singers' parts.")
            if backing.quiet:
                notes_lines.append("The backing singers are quiet beside the lead singer in this recording, "
                                   "so their parts were harder to hear: check them by ear.")
            if suggested:
                names = " and ".join(p.lower() for p in suggested)
                notes_lines.append(f"The backing singers don't sing a {names} part, so "
                                   + ("it is a suggested part." if len(suggested) == 1 else "those are suggested parts."))
            if lead_octave > 0:
                notes_lines.append("The lead singer sings low: the lead line is written an octave above where it is sung.")
            bars = bars_text(backing.lead_alone, cells, shift, m.cells_per_bar, total)
            if bars:
                notes_lines.append(f"The lead singer sings alone ({bars}): only the lead line is written there.")
        else:
            notes_lines.append("Parts as heard in the recording. Voices that overlap are hard to tell apart, "
                               "so check each line by ear.")
    else:
        progress("Following the lead melody", 0.82)
        sung, spoken = audio.melody_on_grid(voc_m, cells)
        # A choir holds through the soloist's breaths and consonants (gaps of up to a beat).
        melody = clean_part(fold_octaves(sung), min_len=2, max_gap=m.cells_per_beat)
        melody = drop_strays(melody, gap=m.cells_per_bar, longest=m.cells_per_beat)
        if not melody:
            raise audio.AudioError("Could not hear a clear melody in this song.")
        median = float(np.median([n.pitch for n in melody]))
        octave = 12 * int(round((67 - median) / 12))
        if octave:
            for n in melody:
                n.pitch += octave
        phase = choose_bar_phase(melody, m)
        aligned, total, shift = align_to_bars({"Soprano": melody}, m, phase)
        tune = choir_tune(aligned["Soprano"], doh_pc, key_mode, m)
        progress("Writing alto, tenor and bass", 0.9)
        parts = {"Soprano": tune, **harmonize(tune, doh_pc, key_mode, m, total)}
        notes_lines.append("Solo song: the soprano line is the tune as a choir would sing it, without the "
                           "soloist's slides and runs. Alto, tenor and bass are a suggested arrangement.")
        if octave > 0:
            notes_lines.append("The melody was sung by a lower voice and has been written an octave up for sopranos.")
        elif octave < 0:
            notes_lines.append("The melody has been written an octave down to suit sopranos.")
        bars = bars_text(spoken, cells, shift, m.cells_per_bar, total)
        if bars:
            notes_lines.append(f"Words spoken rather than sung are left out ({bars}).")

    placed: dict[int, str] = {}
    # With backing singers the lead sings most of the words (the calls and the verses).
    words_part = LEAD if parts.get(LEAD) else "Soprano"
    if words:
        progress("Putting the words under the notes", 0.92)
        placed = lyr.place(words.lines, parts[words_part], total, m,
                           (lambda t: time_to_cell(cells, t) + shift) if words.timed else None)
    if placed:
        notes_lines.append(f"The words were placed under the {words_part.lower()} line automatically. "
                           "Check where each syllable falls.")
    words_state = ("you" if words.source == "you" else "online") if placed else \
        ("unplaced" if words else ("missing" if song is not None else None))

    if guessed:
        notes_lines.append(f"Time {meter} was worked out from the recording. If the bars look wrong, pick the time yourself and run it again.")
    parts = {p: parts.get(p, []) for p in LINES if p in PARTS or parts.get(p)}
    score = Score(
        title=title, doh_pc=doh_pc, tonic_pc=tonic_pc, mode=key_mode, meter=m, tempo_bpm=tempo,
        doh_midi=choose_doh_midi(doh_pc, parts), total_cells=total, parts=parts, kind=kind, notes=notes_lines,
        words=placed, words_part=words_part,
    )

    progress("Making the PDF", 0.95)
    pdf_path = workdir / "score.pdf"
    write_pdf(score, str(pdf_path))
    (workdir / "score.txt").write_text(score_text(score), encoding="utf-8")

    progress("Making the practice tracks", 0.97)
    step = float(np.median(np.diff(cells)))

    def cell_time(c: int) -> float:  # a score cell's time in the recording
        i = c - shift
        if i < 0:
            return float(cells[0]) + i * step
        if i >= len(cells):
            return float(cells[-1]) + (i - len(cells) + 1) * step
        return float(cells[i])

    tracks = practice.write_tracks(mix_m, parts, cell_time, workdir, SR)

    result = Result(title, kind, key_label(doh_pc, tonic_pc, key_mode), meter, int(round(tempo)),
                    total // m.cells_per_bar, pdf_path, found_on, found_url, scale_letters(doh_pc),
                    words_state, words.found if placed and words and words.source == "online" else None,
                    {name: path.name for name, path in tracks.items()})
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


@dataclass
class Backing:
    notes: list  # what the backing singers sing (Note, in seconds)
    mono: np.ndarray  # their track, mono at SR
    lead_alone: list[tuple[float, float]]  # stretches where only the lead sings
    lead_mono: np.ndarray  # the lead singer's track, mono at SR
    lead_on: np.ndarray  # where the lead sings, in steps of HOP seconds
    quiet: bool  # the backing singers are much quieter than the lead
    HOP = 0.1


def find_backing(vocals: np.ndarray, workdir: Path, progress: Callable[[float], None], *,
                 always: bool = False) -> Backing | None:
    """The backing singers, if they sing for a good share of the song (or `always`); else None."""
    lead, back = karaoke.split(vocals, progress)
    lead_m, back_m = audio.to_mono(lead, MIX_SR), audio.to_mono(back, MIX_SR)
    hop = Backing.HOP
    lead_rms, back_rms = audio.singing(lead_m, hop=hop), audio.singing(back_m, hop=hop)
    n = min(len(lead_rms), len(back_rms))
    lead_rms, back_rms = lead_rms[:n], back_rms[:n]
    # Where only the lead sings, what is left in the backing track is about 1% as loud as the
    # loudest singing; where backing singers sing it is 15% or more.
    loud = 0.08 * float(np.percentile(np.r_[lead_rms, back_rms], 95))
    lead_on, back_on = lead_rms > loud, back_rms > loud
    sung = lead_on | back_on
    if not sung.any() or (back_on[sung].mean() < BACKING_SHARE and not always):
        return None
    both = lead_on & back_on
    quiet = bool(both.any() and np.median(back_rms[both] / lead_rms[both]) < QUIET_BACKING)
    path = workdir / "backing.wav"
    audio.write_wav(path, back, MIX_SR)
    heard = audio.polyphonic_notes(path, blended=True)
    # Keep the notes heard while the backing singers sing (not what the lead leaves behind).
    def while_backing(x) -> bool:
        frames = back_on[int(x.start / hop):int(x.end / hop) + 1]
        return frames.size > 0 and frames.mean() >= 0.5
    notes = [x for x in heard if while_backing(x)]
    return Backing(notes, back_m, audio.stretches(lead_on & ~back_on, hop, shortest=2.0), lead_m, lead_on, quiet)


def lead_line(backing: Backing, cells: np.ndarray, meter) -> tuple[list, int]:
    """The lead singer's tune on the grid, and the octaves (in semitones) it was moved to suit a treble line.

    Only notes sung where the lead singer is heard are kept: elsewhere the lead track
    holds what is left of the backing singers.
    """
    sung, _ = audio.melody_on_grid(backing.lead_mono, cells)
    on, hop = backing.lead_on, Backing.HOP

    def while_lead(n) -> bool:
        a = float(cells[min(n.start, len(cells) - 1)])
        b = float(cells[n.end]) if n.end < len(cells) else a + 0.5
        frames = on[int(a / hop):int(b / hop) + 1]
        return frames.size > 0 and frames.mean() >= 0.5

    tune = clean_part(fold_octaves([n for n in sung if while_lead(n)]), min_len=2, max_gap=meter.cells_per_beat)
    tune = drop_strays(tune, gap=meter.cells_per_bar, longest=meter.cells_per_beat)
    if not tune:
        return [], 0
    # A man leading is written in the treble, as the tune of a solo song is.
    octave = 12 if np.median([n.pitch for n in tune]) < 60 else 0
    for n in tune:
        n.pitch += octave
    return tune, octave


def tidy_backing(parts: dict, doh_pc: int, mode: str, meter, total: int) -> tuple[dict, list[str]]:
    """Backing singers' parts as a choir would sing them (in the key, on the beat).

    A part they hardly sing is written by the harmonizer instead; returns the parts
    and the names of those.
    """
    soprano = choir_tune(fold_octaves(parts["Soprano"]), doh_pc, mode, meter)
    out, suggested = {"Soprano": soprano}, []
    made = harmonize(soprano, doh_pc, mode, meter, total)
    sung = sum(n.length for n in soprano)
    for name in PARTS[1:]:
        heard = choir_tune(fold_octaves(parts.get(name, [])), doh_pc, mode, meter, soprano=False)
        if sung and sum(n.length for n in heard) >= 0.4 * sung:
            out[name] = heard
        else:
            out[name] = made[name]
            suggested.append(name)
    return out, suggested


def bars_text(stretches: list[tuple[float, float]], cells: np.ndarray, shift: int, cells_per_bar: int,
              total: int) -> str:
    """The bars the stretches (in seconds) fall in, e.g. 'at the start, bars 1-4 and 52', or '' if none."""
    bars: set[int] = set()
    n_bars = total // cells_per_bar
    start = end = False
    for a, b in stretches:
        first = (time_to_cell(cells, a) + shift) // cells_per_bar
        last = (time_to_cell(cells, b) + shift) // cells_per_bar
        start |= first < 0
        end |= last >= n_bars
        bars.update(range(max(0, first), min(n_bars - 1, last) + 1))
    runs: list[list[int]] = []
    for bar in sorted(bars):
        if runs and bar == runs[-1][1] + 1:
            runs[-1][1] = bar
        else:
            runs.append([bar, bar])
    places = [f"{a + 1}" if a == b else f"{a + 1}-{b + 1}" for a, b in runs]
    if places:
        places[0] = ("bar " if len(bars) == 1 else "bars ") + places[0]
    places = (["at the start"] if start else []) + places + (["at the end"] if end else [])
    if not places:
        return ""
    return places[0] if len(places) == 1 else f"{', '.join(places[:-1])} and {places[-1]}"


def _rms(y: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(y)))) if y.size else 0.0
