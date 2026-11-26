"""Audio analysis: decode, separate voices, detect notes, beat and key.

Heavy libraries (torch, demucs, basic-pitch, librosa) are imported inside the
functions that need them so the rest of the package stays quick to import.
"""
from __future__ import annotations

import contextlib
import io
import subprocess
import warnings
from pathlib import Path
from typing import Callable

import numpy as np

from . import tools
from .models import Note, QNote

Progress = Callable[[float], None]

ANALYSIS_SR = 22050


class AudioError(Exception):
    pass


def decode(path: Path, sr: int, channels: int) -> np.ndarray:
    """Decode any audio/video file with ffmpeg. Returns float32 array (channels, samples)."""
    cmd = [tools.ffmpeg(), "-v", "error", "-nostdin", "-i", str(path),
           "-f", "f32le", "-ac", str(channels), "-ar", str(sr), "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, check=True).stdout
    except FileNotFoundError as e:
        raise AudioError("The audio reader (ffmpeg) is missing. Reinstall Choir Solfa to get it back.") from e
    except subprocess.CalledProcessError as e:
        msg = e.stderr.decode(errors="ignore").strip().splitlines()
        raise AudioError("Could not read that file as audio. " + (msg[-1] if msg else "")) from e
    audio = np.frombuffer(out, dtype=np.float32)
    if audio.size < sr * channels:
        raise AudioError("The audio is shorter than one second.")
    return audio.reshape(-1, channels).T.copy()


def separate_vocals(mix: np.ndarray, progress: Progress = lambda f: None) -> tuple[np.ndarray, np.ndarray]:
    """Split a 44.1 kHz stereo mix into (vocals, accompaniment) with Demucs."""
    import inspect

    import torch
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    model = get_model("htdemucs")
    model.eval()
    wav = torch.from_numpy(mix)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    wav = (wav - mean) / std

    def callback(d: dict) -> None:
        length = d.get("audio_length") or 1
        progress(min(1.0, d.get("segment_offset", 0) / length))

    # demucs 4.0.1 (used on Intel Macs) has no progress callback.
    extra = {"callback": callback} if "callback" in inspect.signature(apply_model).parameters else {}

    devices = ["mps", "cpu"] if torch.backends.mps.is_available() else ["cpu"]
    last_error = None
    for device in devices:
        try:
            with torch.no_grad():
                sources = apply_model(model, wav[None], device=device, split=True, overlap=0.25,
                                      progress=False, **extra)[0]
            break
        except Exception as e:  # MPS can fail on some ops; fall back to the CPU
            last_error = e
    else:
        raise AudioError(f"Could not separate the voices: {last_error}")
    sources = (sources * std + mean).cpu().numpy()
    vocals = sources[model.sources.index("vocals")]
    accomp = sum(sources[i] for i, name in enumerate(model.sources) if name != "vocals")
    return vocals.astype(np.float32), accomp.astype(np.float32)


def to_mono(stereo: np.ndarray, sr_in: int, sr_out: int = ANALYSIS_SR) -> np.ndarray:
    import librosa
    mono = stereo.mean(axis=0) if stereo.ndim == 2 else stereo
    return librosa.resample(mono, orig_sr=sr_in, target_sr=sr_out).astype(np.float32)


def find_beats(y: np.ndarray, sr: int = ANALYSIS_SR) -> np.ndarray:
    import librosa
    _, beats = librosa.beat.beat_track(y=y, sr=sr, units="time")
    return np.asarray(beats, dtype=float)


def beat_chroma(y: np.ndarray, beat_times, sr: int = ANALYSIS_SR) -> np.ndarray:
    """Average chroma between consecutive beats: 12 x len(beat_times)."""
    import librosa
    hop = 512
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
    frames = librosa.time_to_frames(np.asarray(beat_times), sr=sr, hop_length=hop)
    frames = np.clip(frames, 0, chroma.shape[1] - 1)
    bounds = list(frames) + [chroma.shape[1]]
    return np.stack([chroma[:, a:max(b, a + 1)].mean(axis=1) for a, b in zip(bounds[:-1], bounds[1:])], axis=1)


def onset_cells(y: np.ndarray, cell_times: np.ndarray, sr: int = ANALYSIS_SR) -> set[int]:
    """Grid cells where a new sound starts."""
    import librosa
    times = librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=False)
    return {int(np.argmin(np.abs(cell_times - t))) for t in times}


def key_weights(y: np.ndarray, sr: int = ANALYSIS_SR) -> np.ndarray:
    import librosa
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=4096)
    w = chroma.sum(axis=1)
    return w / (w.sum() + 1e-9)


def polyphonic_notes(wav_path: Path, *, blended: bool = False) -> list[Note]:
    """All notes heard in a vocal track (several at once), via Spotify's Basic Pitch.

    `blended` is for a group of backing singers: their voices blend into one
    soft sound, so quieter notes are kept.
    """
    settings = (dict(onset_threshold=0.3, frame_threshold=0.2, minimum_note_length=80, melodia_trick=False) if blended
                else dict(onset_threshold=0.5, frame_threshold=0.3, minimum_note_length=100, melodia_trick=True))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from basic_pitch import ICASSP_2022_MODEL_PATH
        from basic_pitch.inference import predict
        with contextlib.redirect_stdout(io.StringIO()):  # basic-pitch prints debug lines
            _, _, events = predict(str(wav_path), ICASSP_2022_MODEL_PATH, minimum_frequency=70,
                                   maximum_frequency=1100, **settings)
    return [Note(int(p), float(s), float(e), float(a)) for s, e, p, a, _ in events]


def polyphony(notes: list[Note], duration: float, min_amp: float = 0.3, step: float = 0.05) -> float:
    """Share of sung moments where three or more notes sound together."""
    n = int(duration / step) + 1
    counts = np.zeros(n, dtype=int)
    for note in notes:
        if note.amp < min_amp:
            continue
        counts[int(note.start / step):int(note.end / step) + 1] += 1
    sung = counts > 0
    return float((counts[sung] >= 3).mean()) if sung.any() else 0.0


def singing(y: np.ndarray, sr: int = ANALYSIS_SR, hop: float = 0.1) -> np.ndarray:
    """Loudness of a voice track in steps of `hop` seconds."""
    import librosa
    h = int(sr * hop)
    return librosa.feature.rms(y=y, frame_length=2 * h, hop_length=h)[0]


def stretches(mask: np.ndarray, hop: float, shortest: float = 1.0, join: float = 0.5) -> list[tuple[float, float]]:
    """(start, end) in seconds of the runs of True in `mask`, joined across short gaps."""
    out: list[list[float]] = []
    for i in np.flatnonzero(mask):
        t = i * hop
        if out and t - out[-1][1] <= join:
            out[-1][1] = t + hop
        else:
            out.append([t, t + hop])
    return [(a, b) for a, b in out if b - a >= shortest]


def unsteady_phrases(midi: np.ndarray, *, join: int = 15, shortest: int = 10, smooth: int = 5,
                     window: int = 10, spread: float = 0.8, held_share: float = 0.4) -> list[tuple[int, int]]:
    """Phrases (frame ranges) where the voice talks rather than sings.

    `midi` is a pitch per 20 ms frame, NaN where there's no voice. Sung notes
    hold a pitch: after smoothing out vibrato, the pitch stays within `spread`
    semitones for `window` frames most of the time. Speech (and riffs with no
    clear tune) glide all the time. A phrase is voiced frames joined across
    gaps of up to `join` frames; one with at least `shortest` voiced frames
    that holds a pitch less than `held_share` of the time is returned.
    """
    voiced = ~np.isnan(midi)
    vals = np.where(voiced, midi, 0.0)
    kernel = np.ones(smooth)
    sums = np.convolve(vals, kernel, mode="same")
    counts = np.convolve(voiced.astype(float), kernel, mode="same")
    smoothed = np.where(voiced, sums / np.maximum(counts, 1), np.nan)
    held = np.zeros(len(midi), dtype=bool)
    if len(midi) >= window:
        wins = np.lib.stride_tricks.sliding_window_view(smoothed, window)
        ok = ~np.isnan(wins).any(axis=1)
        steady = np.zeros(len(wins), dtype=bool)
        steady[ok] = (wins[ok].max(axis=1) - wins[ok].min(axis=1)) < spread
        for i in np.flatnonzero(steady):
            held[i:i + window] = True

    out = []
    frames = np.flatnonzero(voiced)
    if not frames.size:
        return out
    breaks = np.flatnonzero(np.diff(frames) > join + 1)
    for a, b in zip(np.r_[0, breaks + 1], np.r_[breaks, frames.size - 1]):
        start, end = int(frames[a]), int(frames[b]) + 1
        v = voiced[start:end]
        if v.sum() >= shortest and held[start:end][v].mean() < held_share:
            out.append((start, end))
    return out


def melody_on_grid(y: np.ndarray, cell_times: np.ndarray,
                   sr: int = ANALYSIS_SR) -> tuple[list[QNote], list[tuple[float, float]]]:
    """Follow a single lead voice and place its notes on the grid.

    Returns the notes and the spoken stretches (start and end in seconds, at
    least a second of voice each) that were left out.
    """
    import librosa

    sr_p = 16000
    y16 = librosa.resample(y, orig_sr=sr, target_sr=sr_p)
    hop = 320  # 20 ms
    f0, voiced, _ = librosa.pyin(y16, fmin=70, fmax=1000, sr=sr_p, frame_length=1280, hop_length=hop)
    times = librosa.times_like(f0, sr=sr_p, hop_length=hop)
    midi = librosa.hz_to_midi(np.where(voiced, f0, np.nan))
    good = ~np.isnan(midi)
    if good.sum() < 10:
        return [], []
    # Singers and recordings are rarely tuned exactly to A=440; centre on the song's own tuning.
    frac = midi[good] - np.round(midi[good])
    tuning = float(np.angle(np.mean(np.exp(2j * np.pi * frac))) / (2 * np.pi))
    midi = midi - tuning

    # Leave out words that are spoken between the sung lines.
    spoken = []
    frame_s = hop / sr_p
    for a, b in unsteady_phrases(midi):
        if np.count_nonzero(~np.isnan(midi[a:b])) * frame_s >= 1.0:
            spoken.append((float(times[a]), float(times[b - 1]) + frame_s))
        midi[a:b] = np.nan

    onset_times = librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=False)
    onset_cells = {int(np.argmin(np.abs(cell_times - t))) for t in onset_times}

    n_cells = len(cell_times)
    edges = np.append(cell_times, cell_times[-1] + (cell_times[-1] - cell_times[-2]))
    idx = np.searchsorted(times, edges)
    pitch: list = [None] * n_cells
    for c in range(n_cells):
        seg = midi[idx[c]:idx[c + 1]]
        if seg.size == 0:
            continue
        ok = seg[~np.isnan(seg)]
        if ok.size >= max(1, seg.size // 2):
            pitch[c] = int(np.round(np.median(ok)))

    notes: list[QNote] = []
    for c, p in enumerate(pitch):
        if p is None:
            continue
        if notes and notes[-1].end == c and notes[-1].pitch == p and c not in onset_cells:
            notes[-1].end = c + 1
        else:
            notes.append(QNote(p, c, c + 1))
    return notes, spoken


def write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    import soundfile as sf
    sf.write(str(path), audio.T if audio.ndim == 2 else audio, sr)
