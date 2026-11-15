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
