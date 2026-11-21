"""Song words: reading them, finding them online, syllables, and placing them under the soprano."""
from __future__ import annotations

import time

import pretty_midi

from choir_solfa import lyrics
from choir_solfa.lyrics import Line, clean_title, find, parse, place, syllables
from choir_solfa.midi import write_midi
from choir_solfa.models import METERS, QNote, Score
from choir_solfa.notation import score_text
from choir_solfa.pdf import write_pdf

M34 = METERS["3/4"]
CPB = M34.cells_per_beat


def q(pitch, beat, beats):
    return QNote(pitch, int(beat * CPB), int((beat + beats) * CPB))


# Silent Night: s :-.l :s | m :- :- | s :-.l :s | m :- :- | r¹ :- :r¹ | t :- :- |
SILENT_NIGHT = [q(67, 0, 1.5), q(69, 1.5, .5), q(67, 2, 1), q(64, 3, 3), q(67, 6, 1.5), q(69, 7.5, .5), q(67, 8, 1),
                q(64, 9, 3), q(74, 12, 2), q(74, 14, 1), q(71, 15, 3)]


def beats(placed):
    return sorted((c / CPB, text) for c, text in placed.items())


def test_reading_plain_words_and_lrc():
    assert [line.text for line in parse("[Verse 1]\nSilent night (x2)\n\nChorus:\nholy night\n")] == \
        ["Silent night", "holy night"]
    timed = parse("[00:12.30] holy night\n[00:05.00][00:20.00] Silent night")
    assert [(line.text, line.time) for line in timed] == [("Silent night", 5.0), ("holy night", 12.3),
                                                          ("Silent night", 20.0)]
    assert all(line.time is None for line in parse("[00:01.00] one\ntwo"))  # partly timed: times not trusted



def test_section_labels_are_dropped_and_long_lines_stay_quick():
    text = "[Verse 1]\nChorus (x2)\nPre-Chorus:\nCHORUS x2\nAll is calm\nLead me home\n" + "chorus" + "!" * 9000 + "a"
    t = time.perf_counter()
    lines = [line.text for line in parse(text)]
    assert time.perf_counter() - t < 2
    assert lines[:2] == ["All is calm", "Lead me home"]

def test_syllables_in_english_and_yoruba():
    assert [syllables(w) for w in ["Silent", "night,", "holy", "heavenly", "little", "peace.", "loved", "singing"]] == \
        [["Si", "lent"], ["night,"], ["ho", "ly"], ["hea", "ven", "ly"], ["lit", "tle"], ["peace."], ["loved"],
         ["sing", "ing"]]
    assert [syllables(w, english=False) for w in ["Olúwa", "àgbà", "ire"]] == [["O", "lú", "wa"], ["à", "gbà"],
                                                                               ["i", "re"]]


def test_video_titles_are_cleaned_for_the_search():
    assert clean_title("Silent Night (Official Video) | SATB arrangement") == "Silent Night"
    assert clean_title("Sinach - Way Maker [Lyrics]") == "Sinach - Way Maker"


def test_words_go_under_the_soprano_notes():
    placed = place(parse("Silent night, holy night\nAll is calm"), SILENT_NIGHT, 18 * CPB, M34)
    assert beats(placed) == [(0, "Si-"), (1.5, "lent"), (2, "night,"), (6, "ho-"), (7.5, "ly"), (8, "night"),
                             (12, "All"), (14, "is"), (15, "calm")]


def test_notes_before_the_singing_get_no_words():
    intro = [QNote(n.pitch, n.start, n.end) for n in SILENT_NIGHT[:4]]
    song = intro + [QNote(n.pitch, n.start + 24 * CPB, n.end + 24 * CPB) for n in SILENT_NIGHT]
    placed = place(parse("Silent night, holy night\nAll is calm"), song, 42 * CPB, M34)
    assert min(placed) == 24 * CPB and placed[24 * CPB] == "Si-"


def test_line_times_keep_words_near_their_place():
    lines = [Line("Silent night", 0.0), Line("holy night", 6.0), Line("All is calm", 12.0)]
    placed = place(lines, SILENT_NIGHT, 18 * CPB, M34, time_cell=lambda t: int(t * CPB))
    assert placed[6 * CPB] == "ho-" and placed[12 * CPB] == "All"


def test_lines_the_singers_leave_out_are_skipped():
    # Lyrics found online often have more verses (or ad-libs) than the recording sings.
    lines = parse("Silent night, holy night (holy night)\nShepherds quake at the sight, glory streams\n"
                  "All is calm", asides=False)
    assert lines[0].text == "Silent night, holy night"
    placed = place(lines, SILENT_NIGHT, 18 * CPB, M34)
    assert beats(placed)[:3] == [(0, "Si-"), (1.5, "lent"), (2, "night,")]
    assert placed[12 * CPB] == "All" and not any("Shep" in t for t in placed.values())


def test_far_too_many_words_are_not_placed():
    assert place(parse("la " * 60), SILENT_NIGHT[:3], 3 * CPB, M34) == {}  # more than 8 syllables a note


def _lrclib(results):
    def get(url):
        if "/get?" in url:
            raise OSError("404")
        return results
    return get


def _hit(n, artist, words, duration=200.0, synced=None):
    return {"id": n, "trackName": "Silent Night", "artistName": artist, "duration": duration,
            "plainLyrics": words, "syncedLyrics": synced, "instrumental": False}


def test_found_words_prefer_the_same_singer_and_time_lines_only_from_the_same_length():
    synced = "[00:01.00] Silent night\n[00:05.00] Holy night"
    got = find("Silent Night", "Home Free", 130.0, get=_lrclib([
        _hit(1, "Someone Else", "Silent night\nholy night"),
        _hit(2, "Home Free", "Silent night\nHoly night", 131.0, synced)]))
    assert got.found == "Silent Night by Home Free" and got.timed and got.lines[1].time == 5.0
    far = find("Silent Night", "Home Free", 300.0, get=_lrclib([_hit(2, "Home Free", "Silent night\nHoly night",
                                                                    131.0, synced)]))
    assert far.lines and not far.timed


def test_found_words_without_a_singer_need_versions_that_agree():
    carol = "Silent night, holy night\nAll is calm"
    agree = find("Silent Night", "", 0, also="St Mary's Choir", get=_lrclib(
        [_hit(1, "A", carol), _hit(2, "B", carol), _hit(3, "C", "Something else\nentirely")]))
    assert agree and agree.lines[0].text == "Silent night, holy night"
    alone = find("Silent Night", "", 0, get=_lrclib([_hit(1, "A", carol), _hit(3, "C", "Something else\nentirely")]))
    assert alone is None
    assert find("Silent Night", "Home Free", 0, get=_lrclib([])) is None


def test_a_failed_lookup_finds_nothing():
    def broken(url):
        raise OSError("offline")
    assert find("Silent Night", "Home Free", 100.0, get=broken) is None


def test_words_are_printed_in_the_text_pdf_and_midi(tmp_path):
    words = place(parse("Silent night, holy night\nAll is calm"), SILENT_NIGHT, 18 * CPB, M34)
    score = Score("Silent Night", 10, 10, "major", M34, 60, 70, 18 * CPB, {"Soprano": SILENT_NIGHT}, "solo",
                  words=words)
    text = score_text(score)
    soprano, under = [line for line in text.splitlines() if line.startswith(("S  |", "   |"))][:2]
    assert "Si-" in under and "lent" in under and "night," in under
    assert under.index("lent") == soprano.index("t₁")  # each syllable under its own note, even inside a beat
    assert soprano.index("| fe₁") == under.index("|", under.index("night,"))  # bar lines stay in step
    write_pdf(score, str(tmp_path / "s.pdf"))
    assert (tmp_path / "s.pdf").read_bytes()[:4] == b"%PDF"
    write_midi(score, str(tmp_path / "s.mid"))
    assert [ly.text for ly in pretty_midi.PrettyMIDI(str(tmp_path / "s.mid")).lyrics][:3] == ["Si-", "lent", "night,"]


def test_no_words_means_no_words_row():
    score = Score("Tune", 0, 0, "major", M34, 60, 60, 18 * CPB, {"Soprano": SILENT_NIGHT}, "solo")
    assert not any(line.startswith("   |") for line in score_text(score).splitlines())
    assert lyrics.MAX_CHARS == 10_000
