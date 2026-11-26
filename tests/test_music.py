import numpy as np

from choir_solfa.harmony import CHORDS, choose_chords, harmonize
from choir_solfa.models import METERS, QNote, Score
from choir_solfa.notation import bar_text, cell_states, render_part, render_pulse, runs_text
from choir_solfa.pdf import write_pdf
from choir_solfa.rhythm import align_to_bars, beat_grid, choose_bar_phase, fold_fast_beats, quantize
from choir_solfa.theory import choose_doh_midi, detect_key, doh_pc_for, scale_letters, solfa
from choir_solfa.voices import split_choir

M44 = METERS["4/4"]


def q(pitch, start_beat, beats, cpb=4):
    return QNote(pitch, int(start_beat * cpb), int((start_beat + beats) * cpb))


# --- theory ---------------------------------------------------------------

def test_solfa_syllables_and_octaves():
    assert solfa(60, 60) == ("d", 0)
    assert solfa(67, 60) == ("s", 0)
    assert solfa(72, 60) == ("d", 1)
    assert solfa(55, 60) == ("s", -1)
    assert solfa(66, 60) == ("fe", 0)
    assert solfa(70, 60) == ("ta", 0)


def test_detect_key_major_and_minor():
    c_major = np.zeros(12)
    for pc, w in zip([0, 2, 4, 5, 7, 9, 11], [5, 2, 3, 2, 4, 2, 1]):
        c_major[pc] = w
    assert detect_key(c_major)[:2] == (0, "major")
    a_minor = np.zeros(12)
    for pc, w in zip([9, 11, 0, 2, 4, 5, 8], [5, 1, 3, 2, 4, 1, 2]):
        a_minor[pc] = w
    tonic, mode, _ = detect_key(a_minor)
    assert (tonic, mode) == (9, "minor")
    assert doh_pc_for(9, "minor") == 0  # A minor is sung with doh = C


def test_choose_doh_keeps_most_notes_unmarked():
    parts = {"Soprano": [QNote(p, 0, 4) for p in (67, 69, 71, 72, 74)]}  # G major tune
    assert choose_doh_midi(7, parts) == 67


# --- notation -------------------------------------------------------------

def _pulse(cells):
    """cells: list of (pitch|None, onset)"""
    return runs_text(render_pulse([c[0] for c in cells], [c[1] for c in cells], "Soprano", 60))


def test_pulse_patterns():
    assert _pulse([(60, True), (60, False), (60, False), (60, False)]) == "d"
    assert _pulse([(60, False)] * 4) == "-"
    assert _pulse([(None, False)] * 4) == ""
    assert _pulse([(60, True), (60, False), (62, True), (62, False)]) == "d.r"
    assert _pulse([(60, True), (60, False), (60, False), (62, True)]) == "d.,r"
    assert _pulse([(60, True), (62, True), (64, True), (65, True)]) == "d,r.m,f"
    assert _pulse([(60, False), (60, False), (62, True), (62, False)]) == "-.r"
    assert _pulse([(60, True), (60, False), (None, False), (None, False)]) == "d."
    assert _pulse([(72, True)] + [(72, False)] * 3) == "d¹"
    assert _pulse([(55, True)] + [(55, False)] * 3) == "s₁"


def test_bar_rendering_with_holds():
    notes = [q(60, 0, 2), q(64, 2, 1), q(67, 3, 0.5), q(65, 3.5, 0.5)]
    bars = render_part(notes, "Soprano", 16, M44, 60)
    assert bar_text(bars[0], M44) == "d :- ¦m :s.f"


def test_tenor_written_an_octave_up():
    bars = render_part([q(48, 0, 4)], "Tenor", 16, M44, 60)
    assert bar_text(bars[0], M44) == "d :- ¦- :-"


def test_overlapping_notes_become_new_onsets():
    pitch, onset = cell_states([QNote(60, 0, 8), QNote(62, 2, 4)], 8)
    assert pitch == [60, 60, 62, 62, 60, 60, 60, 60]
    assert onset == [True, False, True, False, True, False, False, False]


# --- rhythm ---------------------------------------------------------------

def test_beat_grid_and_quantize():
    beats = [0.5, 1.0, 1.5, 2.0]
    cells = beat_grid(beats, 2.2, 4)
    assert cells[0] <= 0 and cells[-1] >= 2.2
    from choir_solfa.models import Note
    qn = quantize([Note(60, 0.51, 0.99)], cells)[0]
    start = np.searchsorted(cells, 0.5 - 1e-9)
    assert qn.start == start and qn.length == 4


def test_bar_phase_and_alignment():
    # strong long notes on beats 1, 5, 9 (phase 1 of 4) with short ones between
    notes = []
    for bar in range(3):
        b = 1 + bar * 4
        notes += [q(60, b, 2), q(62, b + 2, 1), q(64, b + 3, 1)]
    phase = choose_bar_phase(notes, M44)
    assert phase == 1
    moved, total, _ = align_to_bars({"Soprano": notes}, M44, phase)
    assert moved["Soprano"][0].start == 0 and total == 48


# --- voices and harmony -----------------------------------------------------

def test_split_choir_four_parts():
    chord1 = [q(72, 0, 4), q(64, 0, 4), q(55, 0, 4), q(48, 0, 4)]
    chord2 = [q(71, 4, 4), q(62, 4, 4), q(55, 4, 4), q(43, 4, 4)]
    parts = split_choir(chord1 + chord2, 32)
    assert [n.pitch for n in parts["Soprano"]] == [72, 71]
    assert [n.pitch for n in parts["Alto"]] == [64, 62]
    assert [n.pitch for n in parts["Tenor"]] == [55, 55]  # re-sung on the second chord
    assert [n.pitch for n in parts["Bass"]] == [48, 43]


def test_chords_end_with_cadence():
    # C major: m r d (mi re do) with the last note on a downbeat
    melody = [q(64, 0, 2), q(62, 2, 2), q(60, 4, 4)]
    chords = choose_chords(melody, 0, "major", M44, 32)
    assert chords[4] == "I"
    assert chords[3] in ("V", "V7")  # ii-V7-I is fine: the beat before the tonic is a dominant


def test_harmony_is_in_range_and_below_melody():
    tune = [67, 67, 69, 67, 72, 71, 67, 67, 69, 67, 74, 72]
    melody = [q(p, i, 1) for i, p in enumerate(tune)]
    parts = harmonize(melody, 0, "major", M44, 64)
    for name, (lo, hi) in {"Alto": (55, 74), "Tenor": (48, 67), "Bass": (40, 60)}.items():
        assert parts[name], name
        assert all(lo <= n.pitch <= hi for n in parts[name]), name
    for a in parts["Alto"]:
        sop = next(m for m in melody if m.start <= a.start < m.end)
        assert a.pitch <= sop.pitch
    # every chord tone belongs to the chosen chord
    chords = choose_chords(melody, 0, "major", M44, 64)
    for name in ("Alto", "Tenor", "Bass"):
        for n in parts[name]:
            assert n.pitch % 12 in CHORDS[chords[n.start // 4]]


def test_fold_octaves_moves_stray_notes_back():
    from choir_solfa.voices import fold_octaves
    tune = [67, 69, 55, 71, 72, 84, 71, 69, 43, 67]  # 55, 84 and 43 are octave slips
    out = fold_octaves([q(p, i, 1) for i, p in enumerate(tune)])
    assert [n.pitch for n in out] == [67, 69, 67, 71, 72, 72, 71, 69, 67, 67]


def test_fold_octaves_keeps_a_real_leap():
    from choir_solfa.voices import fold_octaves
    tune = [60, 62, 64, 65, 67, 72, 71, 69, 67, 65]  # an octave from d to d' over a scale
    assert [n.pitch for n in fold_octaves([q(p, i, 1) for i, p in enumerate(tune)])] == tune


def test_choir_tune_moves_slides_into_the_key():
    from choir_solfa.voices import choir_tune
    # C major. A slide 63 into 64 joins the 64; a blue 70 between 67 and 72 sits on 69, nearer the line.
    melody = [QNote(60, 0, 4), QNote(63, 4, 6), QNote(64, 6, 12), QNote(67, 12, 16), QNote(70, 16, 20), QNote(72, 20, 24)]
    out = choir_tune(melody, 0, "major", M44)
    assert [(n.pitch, n.start, n.end) for n in out] == [(60, 0, 4), (64, 4, 12), (67, 12, 16), (69, 16, 20), (72, 20, 24)]
    assert all(n.pitch % 12 in (0, 2, 4, 5, 7, 9, 11) for n in out)


def test_choir_tune_keeps_the_raised_seventh_in_minor():
    from choir_solfa.voices import choir_tune
    melody = [QNote(57, 0, 4), QNote(68, 4, 8), QNote(69, 8, 16)]  # l se l in A minor (doh C)
    assert [n.pitch for n in choir_tune(melody, 0, "minor", M44)] == [57, 68, 69]


def test_choir_tune_smooths_a_flick_and_puts_notes_on_eighths():
    from choir_solfa.voices import choir_tune
    # 67 with a quick flick to 69 and back; then notes a sixteenth off the beat.
    melody = [QNote(67, 0, 3), QNote(69, 3, 5), QNote(67, 5, 8), QNote(64, 9, 12), QNote(62, 15, 18)]
    out = choir_tune(melody, 0, "major", M44)
    assert [(n.pitch, n.start, n.end) for n in out] == [(67, 0, 8), (64, 8, 12), (62, 16, 18)]
    assert all(n.start % 2 == 0 and n.end % 2 == 0 for n in out)


def test_spoken_phrases_are_found_and_sung_ones_kept():
    from choir_solfa.audio import unsteady_phrases
    rng = np.random.default_rng(0)
    t = np.arange(150) * 0.02
    sung = 67 + 0.4 * np.sin(2 * np.pi * 5.5 * t)  # a held note with vibrato
    sung[75:] = 69 + 0.4 * np.sin(2 * np.pi * 5.5 * t[75:])
    talk = 55 + np.cumsum(rng.normal(0, 0.6, 150))  # pitch wandering all the time
    gap = np.full(30, np.nan)
    midi = np.concatenate([sung, gap, talk, gap, sung])
    phrases = unsteady_phrases(midi)
    assert phrases == [(180, 330)]


def test_bars_text_text():
    from choir_solfa.pipeline import bars_text
    cells = np.arange(0, 16 * 12) * 0.125  # 16 cells (2 s) a bar
    assert bars_text([], cells, 0, 16, 16 * 12) == ""
    assert bars_text([(2.0, 3.9)], cells, 0, 16, 16 * 12) == "bar 2"
    assert bars_text([(2.0, 6.1), (16.0, 17.0)], cells, 0, 16, 16 * 12) == "bars 2-4 and 9"
    # Talking before the first sung bar (bars were moved back by 4) and after the last one.
    assert bars_text([(1.0, 7.0), (10.0, 11.0), (23.0, 23.9)], cells, -64, 16, 16 * 6) == "at the start, bar 2 and at the end"


def test_harmony_under_a_very_low_note_still_uses_the_chord():
    # A melody note too low to write parts under must not leave fixed notes outside the chord.
    melody = [q(67, 0, 1), q(64, 1, 1), q(43, 2, 1), q(41, 3, 1), q(60, 4, 4)]
    chords = choose_chords(melody, 6, "major", M44, 32)
    parts = harmonize(melody, 6, "major", M44, 32)
    for name in ("Alto", "Tenor", "Bass"):
        for n in parts[name]:
            assert (n.pitch - 6) % 12 in CHORDS[chords[n.start // 4]], name


def test_pdf_writes(tmp_path):
    tune = [67, 67, 69, 67, 72, 71, 67, 67, 69, 67, 74, 72]
    melody = [q(p, i, 1) for i, p in enumerate(tune)] + [q(72, 20, 4)]
    parts = {"Soprano": melody, **harmonize(melody, 0, "major", M44, 96)}
    score = Score("Test song", 0, 0, "major", M44, 96, choose_doh_midi(0, parts), 96, parts, "solo",
                  ["Solo song: alto, tenor and bass are a suggested arrangement."])
    out = tmp_path / "s.pdf"
    write_pdf(score, str(out))
    data = out.read_bytes()
    assert data[:4] == b"%PDF"
    assert data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") >= 2  # the score, then the how-to-read page


def test_scale_letters_are_spelled_in_the_key():
    assert scale_letters(0) == ["C", "D", "E", "F", "G", "A", "B"]
    assert scale_letters(10) == ["Bb", "C", "D", "Eb", "F", "G", "A"]
    assert scale_letters(6) == ["F#", "G#", "A#", "B", "C#", "D#", "E#"]
    assert scale_letters(1) == ["Db", "Eb", "F", "Gb", "Ab", "Bb", "C"]


def test_text_version_ends_with_a_reading_key():
    from choir_solfa.notation import score_text
    melody = [q(67, i, 1) for i in range(8)]
    score = Score("Key test", 0, 0, "major", M44, 96, 60, 32, {"Soprano": melody}, "solo", [])
    assert score_text(score).rstrip().splitlines()[-1].startswith("How to read:")


def test_snap_prefers_pulse_and_half_pulse():
    import numpy as np
    from choir_solfa.models import Note
    from choir_solfa.rhythm import quantize
    cells = np.arange(0, 64) * 0.25  # 4 cells per one-second pulse
    # Slightly late start (0.3 cells) and an end a bit past the half pulse.
    q = quantize([Note(60, 0.075, 0.65, 0.8)], cells, cells_per_pulse=4)[0]
    assert (q.start, q.end) == (0, 2)
    # A clearly placed quarter-pulse note still lands on the quarter.
    q = quantize([Note(60, 1.75, 2.0, 0.8)], cells, cells_per_pulse=4)[0]
    assert (q.start, q.end) == (7, 8)


def test_guess_meter_triple_and_duple():
    import numpy as np
    from choir_solfa.models import Note
    from choir_solfa.rhythm import guess_meter
    beats = np.arange(48) * 0.6

    def song(group):
        notes = []
        for i, b in enumerate(beats):
            strong = i % group == 0
            notes.append(Note(60, b, b + (1.1 if strong else 0.5), 0.9 if strong else 0.4))
        return notes

    assert guess_meter(beats, song(3)) == "3/4"
    assert guess_meter(beats, song(4)) == "4/4"


def test_merge_resung_joins_split_notes_without_an_attack():
    from choir_solfa.models import QNote
    from choir_solfa.voices import merge_resung
    notes = [QNote(60, 0, 2), QNote(60, 2, 6), QNote(60, 8, 10)]
    merged = merge_resung(notes, onset_cells={0, 8})
    assert [(n.start, n.end) for n in merged] == [(0, 6), (8, 10)]
    kept = merge_resung(notes, onset_cells={0, 2, 8})
    assert len(kept) == 3


def test_fold_fast_beats_keeps_the_half_the_notes_start_on():
    from choir_solfa.models import Note
    fast = np.arange(0, 20, 0.2)  # 300 a minute
    notes = [Note(pitch=60, start=0.2 + 0.8 * k, end=0.2 + 0.8 * k + 0.6, amp=0.8) for k in range(24)]
    beats = fold_fast_beats(fast, notes)
    assert 60 / np.median(np.diff(beats)) <= 160
    assert np.isclose(beats[0], 0.2)
    slow = np.arange(0, 20, 0.6)
    assert np.array_equal(fold_fast_beats(slow, notes), slow)


def test_drop_strays_leaves_out_lone_blips():
    from choir_solfa.voices import drop_strays
    notes = [QNote(60, 0, 2), QNote(64, 40, 48), QNote(65, 48, 50), QNote(67, 80, 82)]
    assert [n.pitch for n in drop_strays(notes, gap=16, longest=4)] == [64, 65]


def test_choir_tune_lifts_a_low_phrase_into_the_soprano_range():
    from choir_solfa.voices import choir_tune
    low = [QNote(p, 4 * i, 4 * i + 4) for i, p in enumerate([55, 57, 59, 60])]  # s l t d an octave down
    high = [QNote(p, 32 + 4 * i, 36 + 4 * i) for i, p in enumerate([67, 69, 71, 72])]
    out = choir_tune(low + high, 0, "major", M44)
    assert [n.pitch for n in out] == [67, 69, 71, 72, 67, 69, 71, 72]
