from pathlib import Path

import numpy as np
import soundfile as sf

from choir_solfa import audio, tools


def test_bundled_ffmpeg_decodes_audio(tmp_path):
    sr = 22050
    t = np.arange(sr * 2) / sr
    sf.write(tmp_path / "a.wav", 0.3 * np.sin(2 * np.pi * 440 * t), sr)
    out = audio.decode(tmp_path / "a.wav", sr, 1)
    assert out.shape == (1, sr * 2)
    assert "imageio_ffmpeg" in tools.ffmpeg() or tools.ffmpeg().endswith(("ffmpeg", "ffmpeg.exe"))


def test_youtube_has_a_javascript_runtime():
    runtimes = tools.js_runtimes()
    assert "deno" in runtimes  # bundled by the deno package, so no Node install is needed


def test_the_icon_ships_with_the_app():
    assert (Path(audio.__file__).parent / "assets" / "icon.png").stat().st_size > 1000


def test_make_saves_into_a_folder_or_a_named_file(tmp_path):
    from choir_solfa.cli import output_path

    assert output_path(None, "Song") == Path("Song.pdf")
    assert output_path(f"{tmp_path}/new/", "Song") == tmp_path / "new" / "Song.pdf"
    assert output_path(str(tmp_path), "Song") == tmp_path / "Song.pdf"
    assert output_path(f"{tmp_path}/x/mine.pdf", "Song") == tmp_path / "x" / "mine.pdf"
