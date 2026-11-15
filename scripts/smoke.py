"""Check an installed Choir Solfa end to end (used by CI after running install.sh / install.ps1).

    uv run --no-project scripts/smoke.py

Uses only the standard library, so it runs outside the app's own Python.
"""
from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

PORT = 4799


def step(msg: str) -> None:
    print(f"\n--- {msg}", flush=True)


def app() -> Path:
    bin_dir = subprocess.run(["uv", "tool", "dir", "--bin"], capture_output=True, text=True, check=True).stdout.strip()
    exe = Path(bin_dir) / ("choir-solfa.exe" if os.name == "nt" else "choir-solfa")
    assert exe.exists(), f"{exe} is missing"
    return exe


def chords(path: Path) -> None:
    """16 seconds of four three-note chords (C, F, G, C), like three singers holding parts."""
    sr = 22050
    progression = [(60, 64, 67), (60, 65, 69), (59, 62, 67), (60, 64, 67)]
    frames = bytearray()
    for chord in progression:
        for i in range(sr * 4):
            t = i / sr
            env = min(1.0, t * 20, (4 - t) * 20)
            s = sum(math.sin(2 * math.pi * 440 * 2 ** ((n - 69) / 12) * t * h) / h
                    for n in chord for h in (1, 2, 3))
            frames += struct.pack("<h", int(3000 * env * s))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))


def ping() -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/ping", timeout=2) as r:
            return json.load(r)
    except OSError:
        return None


def main() -> int:
    exe = app()
    work = Path(tempfile.mkdtemp())
    env = {**os.environ, "CHOIR_SOLFA_HOME": str(work / "home"), "BROWSER": "true"}

    step("Making a score from a test recording")
    chords(work / "chords.wav")
    out = work / "out" / "chords.pdf"
    subprocess.run([str(exe), "make", str(work / "chords.wav"), "-o", str(out), "--mode", "choir", "--time", "4/4"],
                   env=env, check=True, timeout=1800)
    assert out.exists() and out.read_bytes()[:5] == b"%PDF-", f"{out} was not made"
    midi = out.with_suffix(".mid")
    assert midi.exists() and midi.read_bytes()[:4] == b"MThd", f"{midi} was not made"
    print("score made:", out)

    step("Starting the page, then starting it again")
    server = subprocess.Popen([str(exe), "serve", "--no-open", "--port", str(PORT), "--quit-when-closed"], env=env)
    try:
        for _ in range(60):
            if ping():
                break
            time.sleep(1)
        assert ping() == {"app": "choir-solfa"}, "the page did not start"
        again = subprocess.run([str(exe), "serve", "--no-open", "--port", str(PORT)], env=env,
                               capture_output=True, text=True, timeout=60)
        assert again.returncode == 0 and "already running" in again.stdout, again.stdout + again.stderr
        print("page answers, and a second start just points at it")
    finally:
        server.terminate()
        server.wait(timeout=30)

    step("Checking the shortcut")
    if sys.platform == "darwin":
        place = Path.home() / "Applications" / "Choir Solfa.app" / "Contents" / "MacOS" / "choir-solfa"
    else:
        place = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Choir Solfa.lnk"
    assert place.exists(), f"{place} is missing"
    print("shortcut:", place)
    print("\nAll good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
