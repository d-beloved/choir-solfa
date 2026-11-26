import numpy as np

from choir_solfa import audio, practice
from choir_solfa.models import QNote


def test_tone_follows_the_recordings_timing():
    sr = 8000
    times = {0: 0.0, 4: 0.5, 8: 1.25}  # an uneven beat
    y = practice.tone([QNote(69, 0, 4), QNote(81, 4, 8)], times.get, sr * 2, sr)
    first, second, after = y[: sr // 2], y[sr // 2: int(1.25 * sr)], y[int(1.3 * sr):]
    assert np.abs(after).max() == 0  # nothing after the last note
    spectrum = lambda part: np.argmax(np.abs(np.fft.rfft(part))) * sr / len(part)
    assert abs(spectrum(first) - 440) < 10 and abs(spectrum(second) - 880) < 10


def test_tracks_are_written_for_the_parts_that_have_notes(tmp_path):
    sr = practice.SR
    recording = (0.3 * np.sin(2 * np.pi * 220 * np.arange(sr * 2) / sr)).astype(np.float32)
    parts = {"Soprano": [QNote(72, 0, 8)], "Alto": [QNote(67, 0, 8)], "Tenor": [], "Bass": [QNote(48, 0, 8)]}
    written = practice.write_tracks(recording, parts, lambda c: c * 0.2, tmp_path, sr)
    assert list(written) == ["Soprano", "Alto", "Bass", "All parts"]  # no tenor notes, no tenor track
    back = audio.decode(written["Soprano"], sr, 1)[0]
    assert abs(len(back) - len(recording)) < sr // 10 and np.abs(back).max() > 0.1
