import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from choir_solfa import audio, shortcut, tools


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


def test_icons_ship_with_the_app():
    for name in ("icon.icns", "icon.ico", "icon.png"):
        assert (shortcut.ASSETS / name).stat().st_size > 1000


def test_mac_launcher_runs_this_python_in_the_background(tmp_path):
    script = shortcut.mac_launcher("/Users/o'brien/py/bin/python", tmp_path / "app.log")
    assert "nohup '/Users/o'\\''brien/py/bin/python' -m choir_solfa serve --quit-when-closed" in script
    assert script.rstrip().endswith("&")


def test_make_saves_into_a_folder_or_a_named_file(tmp_path):
    from choir_solfa.cli import output_path

    assert output_path(None, "Song") == Path("Song.pdf")
    assert output_path(f"{tmp_path}/new/", "Song") == tmp_path / "new" / "Song.pdf"
    assert output_path(str(tmp_path), "Song") == tmp_path / "Song.pdf"
    assert output_path(f"{tmp_path}/x/mine.pdf", "Song") == tmp_path / "x" / "mine.pdf"


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac installer is built on a Mac")
def test_mac_installer_app_builds(tmp_path):
    """scripts/make_mac_installer.py makes a working applet that runs launch.sh."""
    import importlib.util
    import plistlib

    path = Path(__file__).resolve().parent.parent / "scripts" / "make_mac_installer.py"
    spec = importlib.util.spec_from_file_location("make_mac_installer", path)
    maker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(maker)
    app = maker.build_app(tmp_path)
    info = plistlib.loads((app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == "io.github.d-beloved.choir-solfa.installer"
    resources = app / "Contents" / "Resources"
    assert (resources / "launch.sh").stat().st_mode & 0o111
    assert (resources / "applet.icns").exists()
    assert (app / "Contents" / "MacOS" / "applet").read_bytes()[:4] in (b"\xca\xfe\xba\xbe", b"\xcf\xfa\xed\xfe")
    assert maker.zip_app(app).stat().st_size > 10_000
