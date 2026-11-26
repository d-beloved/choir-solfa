"""Scoring the backing singers: splitting them from the lead, and tidying their parts."""
import struct

import numpy as np
import pytest

from choir_solfa import audio, karaoke, pipeline
from choir_solfa.models import METERS, Note, QNote, Score
from choir_solfa.voices import choir_tune, drop_overtones


def test_drop_overtones_keeps_one_line():
    tune = Note(60, 0.0, 1.0)
    octave = Note(72, 0.1, 0.9)       # the same line an octave up
    twelfth = Note(79, 0.0, 1.0)      # an overtone a twelfth up
    later = Note(72, 2.0, 3.0)        # the octave note on its own, later on
    brief = Note(84, 0.0, 1.0)        # two octaves above the tune
    passing = Note(72, 0.8, 2.0)      # overlaps the tune only briefly
    kept = drop_overtones([tune, octave, twelfth, later, brief, passing])
    assert kept == [tune, later, passing]


def test_stretches_joins_short_gaps_and_skips_short_runs():
    mask = np.array([1, 1, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0], bool)
    assert audio.stretches(mask, 0.5, shortest=1.0, join=1.0) == [(0.0, 3.5)]
    assert audio.stretches(mask, 0.5, shortest=0.5, join=1.0) == [(0.0, 3.5), (6.5, 7.0)]
    assert audio.stretches(np.zeros(5, bool), 0.5) == []


def test_choir_tune_moves_only_the_soprano_by_octaves():
    m = METERS["4/4"]
    low = [QNote(48, 0, 4), QNote(50, 4, 8), QNote(52, 8, 16)]  # c-d-e, low in a bass voice
    assert [n.pitch for n in choir_tune(low, 0, "major", m, soprano=False)] == [48, 50, 52]
    assert min(n.pitch for n in choir_tune(low, 0, "major", m)) >= 55


def test_tidy_backing_suggests_the_parts_nobody_sings():
    m = METERS["4/4"]
    soprano = [QNote(64 + (i % 3), 16 * i, 16 * i + 16) for i in range(8)]
    alto = [QNote(60, 16 * i, 16 * i + 16) for i in range(8)]
    bass = [QNote(48, 0, 4)]  # hardly sung
    parts, suggested = pipeline.tidy_backing({"Soprano": soprano, "Alto": alto, "Tenor": [], "Bass": bass},
                                             0, "major", m, 16 * 8)
    assert suggested == ["Tenor", "Bass"]
    assert [n.pitch for n in parts["Alto"]] == [60] * len(parts["Alto"])
    assert parts["Tenor"] and parts["Bass"]


def _tone(start: float, end: float, total: float, sr: int, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(total * sr)) / sr
    y = 0.3 * np.sin(2 * np.pi * hz * t) * ((t >= start) & (t < end))
    return np.stack([y, y]).astype(np.float32)


def test_find_backing_when_backing_singers_carry_the_song(monkeypatch, tmp_path):
    sr = pipeline.MIX_SR
    lead, back = _tone(0, 10, 10, sr), _tone(4, 10, 10, sr, 330.0)
    monkeypatch.setattr(karaoke, "split", lambda vocals, progress: (lead, back))
    heard = [Note(64, 1.0, 2.0), Note(64, 5.0, 7.0)]
    monkeypatch.setattr(audio, "polyphonic_notes", lambda path, blended=False: heard)
    found = pipeline.find_backing(lead + back, tmp_path, lambda f: None)
    assert found is not None
    assert found.notes == [heard[1]]  # the note heard while only the lead sang is left out
    assert len(found.lead_alone) == 1
    a, b = found.lead_alone[0]
    assert a < 0.3 and 3.7 < b < 4.3
    assert (tmp_path / "backing.wav").exists()
    assert found.quiet is False  # as loud as the lead where both sing
    assert found.lead_on[:30].all() and found.lead_on[40:].all()


def test_quiet_backing_singers_are_noted(monkeypatch, tmp_path):
    sr = pipeline.MIX_SR
    lead, back = _tone(0, 10, 10, sr), 0.3 * _tone(4, 10, 10, sr, 330.0)
    monkeypatch.setattr(karaoke, "split", lambda vocals, progress: (lead, back))
    monkeypatch.setattr(audio, "polyphonic_notes", lambda path, blended=False: [])
    found = pipeline.find_backing(lead + back, tmp_path, lambda f: None)
    assert found is not None and found.quiet


def test_backing_can_be_asked_for_even_when_quiet_for_most_of_the_song(monkeypatch, tmp_path):
    sr = pipeline.MIX_SR
    lead, back = _tone(0, 10, 10, sr), _tone(8, 10, 10, sr, 330.0)  # backing only at the end
    monkeypatch.setattr(karaoke, "split", lambda vocals, progress: (lead, back))
    monkeypatch.setattr(audio, "polyphonic_notes", lambda path, blended=False: [])
    assert pipeline.find_backing(lead + back, tmp_path, lambda f: None) is None
    assert pipeline.find_backing(lead + back, tmp_path, lambda f: None, always=True) is not None


def test_find_backing_none_for_a_solo_singer(monkeypatch, tmp_path):
    sr = pipeline.MIX_SR
    lead, back = _tone(0, 10, 10, sr), 0.01 * _tone(0, 10, 10, sr)  # what a solo voice leaves behind
    monkeypatch.setattr(karaoke, "split", lambda vocals, progress: (lead, back))
    monkeypatch.setattr(audio, "polyphonic_notes", lambda *a, **k: pytest.fail("should not look for notes"))
    assert pipeline.find_backing(lead, tmp_path, lambda f: None) is None


def test_lead_line_keeps_only_what_the_lead_sings_and_writes_a_man_in_the_treble(monkeypatch):
    m = METERS["4/4"]
    cells = np.arange(0, 12, 0.125)  # 8 cells a second
    lead_on = np.zeros(120, bool)
    lead_on[:60] = True  # the lead sings for the first 6 seconds
    sung = [QNote(55, 0, 16), QNote(57, 16, 32), QNote(55, 32, 40), QNote(64, 56, 72)]  # the last at 7-9 s
    monkeypatch.setattr(audio, "melody_on_grid", lambda y, c: ([QNote(n.pitch, n.start, n.end) for n in sung], []))
    backing = pipeline.Backing([], np.zeros(1), [], np.zeros(1), lead_on, False)
    tune, octave = pipeline.lead_line(backing, cells, m)
    assert octave == 12
    assert [(n.pitch, n.start) for n in tune] == [(67, 0), (69, 16), (67, 32)]


def _backing_score() -> Score:
    m = METERS["4/4"]
    lead = [QNote(67, 0, 8), QNote(69, 8, 16)]
    soprano = [QNote(64, 16, 32)]
    parts = {"Lead": lead, "Soprano": soprano, "Alto": [QNote(60, 16, 32)], "Tenor": [QNote(55, 16, 32)],
             "Bass": [QNote(48, 16, 32)]}
    return Score("Call and response", 0, 0, "major", m, 96, 60, 32, parts, "backing", [],
                 words={0: "Call", 8: "me"}, words_part="Lead")


def test_text_has_the_lead_line_with_the_words_under_it():
    from choir_solfa.notation import score_text
    text = score_text(_backing_score())
    rows = [line for line in text.splitlines() if line.startswith(("L ", "S ", "A ", "T ", "B ", "   "))]
    assert [r[0] for r in rows[:6]] == ["L", " ", "S", "A", "T", "B"]
    assert "Call" in rows[1]
    assert "The words are under the lead line." in text


def test_pdf_and_tracks_with_a_lead_line(tmp_path):
    from choir_solfa import practice
    from choir_solfa.pdf import write_pdf
    score = _backing_score()
    write_pdf(score, str(tmp_path / "s.pdf"))
    assert (tmp_path / "s.pdf").read_bytes()[:4] == b"%PDF"
    tracks = practice.write_tracks(np.zeros(practice.SR * 5, np.float32), score.parts,
                                   lambda c: c * 0.125, tmp_path)
    assert list(tracks) == ["Lead", "Soprano", "Alto", "Tenor", "Bass", "All parts"]


def test_website_offers_every_practice_track():
    import importlib.util
    from pathlib import Path
    from choir_solfa import practice
    spec = importlib.util.spec_from_file_location("handler_tracks", Path(__file__).parents[1] / "cloud/api/handler.py")
    source = spec.loader.get_source("handler_tracks")
    assert all(f'"{name}": "{file}"' in source for name, file in practice.TRACKS.items())


# --- A tiny ONNX file, written by hand, run by the reader and checked against torch ---

def _varint(n: int) -> bytes:
    n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _len(num: int, payload: bytes | str) -> bytes:
    payload = payload.encode() if isinstance(payload, str) else payload
    return _varint(num << 3 | 2) + _varint(len(payload)) + payload


def _int(num: int, value: int) -> bytes:
    return _varint(num << 3) + _varint(value)


def _attr(name: str, value) -> bytes:
    if isinstance(value, float):
        return _len(5, _len(1, name) + _varint(2 << 3 | 5) + struct.pack("<f", value))
    if isinstance(value, int):
        return _len(5, _len(1, name) + _int(3, value))
    return _len(5, _len(1, name) + _len(8, b"".join(_varint(v) for v in value)))  # packed ints


def _node(op: str, inputs: list[str], output: str, **attrs) -> bytes:
    body = b"".join(_len(1, i) for i in inputs) + _len(2, output) + _len(4, op)
    return _len(1, body + b"".join(_attr(k, v) for k, v in attrs.items()))


def _weight(name: str, array: np.ndarray) -> bytes:
    body = b"".join(_int(1, d) for d in array.shape) + _int(2, 1) + _len(8, name)
    return _len(5, body + _len(9, array.astype("<f4").tobytes()))


def test_onnx_reader_runs_the_graph_like_torch(tmp_path):
    import torch
    import torch.nn.functional as F

    rng = np.random.default_rng(0)
    w = {
        "conv.w": rng.normal(size=(3, 2, 3, 3)), "conv.b": rng.normal(size=3),
        "bn.s": rng.uniform(0.5, 1.5, 3), "bn.b": rng.normal(size=3),
        "bn.m": rng.normal(size=3), "bn.v": rng.uniform(0.5, 1.5, 3),
        "up.w": rng.normal(size=(3, 2, 2, 2)), "mm": rng.normal(size=(8, 8)), "half": np.array([0.5]),
    }
    w = {k: v.astype(np.float32) for k, v in w.items()}
    graph = b"".join([
        _node("Conv", ["x", "conv.w", "conv.b"], "c", pads=[1, 1, 1, 1], strides=[1, 1]),
        _node("BatchNormalization", ["c", "bn.s", "bn.b", "bn.m", "bn.v"], "n", epsilon=1e-5),
        _node("Relu", ["n"], "r"),
        _node("ConvTranspose", ["r", "up.w"], "u", strides=[2, 2]),
        _node("Transpose", ["u"], "t", perm=[0, 1, 3, 2]),
        _node("MatMul", ["t", "mm"], "p"),
        _node("Add", ["p", "t"], "a"),
        _node("Mul", ["a", "half"], "y"),
        *[_weight(k, v) for k, v in w.items()],
        _len(11, _len(1, "x")), _len(11, _len(1, "conv.w")),  # older files list weights as inputs too
        _len(12, _len(1, "y")),
    ])
    path = tmp_path / "tiny.onnx"
    path.write_bytes(_int(1, 7) + _len(7, graph))

    x = torch.from_numpy(rng.normal(size=(1, 2, 4, 4)).astype(np.float32))
    got = karaoke.OnnxNet(path)(x)

    t = {k: torch.from_numpy(v) for k, v in w.items()}
    c = F.conv2d(x, t["conv.w"], t["conv.b"], padding=1)
    n = F.batch_norm(c, t["bn.m"], t["bn.v"], t["bn.s"], t["bn.b"], False, 0.0, 1e-5)
    u = F.conv_transpose2d(F.relu(n), t["up.w"], stride=2).permute(0, 1, 3, 2)
    want = (u @ t["mm"] + u) * 0.5
    assert got.shape == (1, 2, 8, 8)
    assert torch.allclose(got, want, atol=1e-5)


def test_onnx_reader_refuses_what_it_cannot_run(tmp_path):
    import torch

    path = tmp_path / "odd.onnx"
    path.write_bytes(_len(7, _node("Softmax", ["x"], "y") + _len(11, _len(1, "x")) + _len(12, _len(1, "y"))))
    with pytest.raises(ValueError, match="Softmax"):
        karaoke.OnnxNet(path)(torch.zeros(1))
