"""Command line: `choir-solfa` starts the page; `choir-solfa make SONG` writes a PDF directly."""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

from .models import METERS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="choir-solfa", description="Turn a song into a tonic solfa score for SATB.")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="start the page in your browser (default)")
    serve.add_argument("--port", type=int, default=int(os.environ.get("CHOIR_SOLFA_PORT", 4790)))
    serve.add_argument("--no-open", action="store_true", help="do not open the browser")
    # Accepted and ignored, so shortcuts made by the old installers still start the page.
    for old in ("--quit-when-closed", "--restart-when-idle"):
        serve.add_argument(old, action="store_true", help=argparse.SUPPRESS)
    serve.add_argument("--public-host", action="append", help=argparse.SUPPRESS)

    make = sub.add_parser("make", help="make a PDF from a file or link without the page")
    make.add_argument("song", help="audio/video file or a link to the song")
    make.add_argument("-o", "--out", default=None, help="PDF path (default: <title>.pdf here)")
    make.add_argument("--mode", choices=["auto", "choir", "backing", "solo"], default="auto")
    make.add_argument("--time", choices=["auto", *METERS], default="auto")
    make.add_argument("--title", default=None)
    make.add_argument("--lyrics", default=None, metavar="FILE",
                      help="a text file with the words (found online for links if left out)")

    args = parser.parse_args(argv)
    if args.cmd == "make":
        return _make(args)
    from .server import serve as run_server
    return run_server(port=getattr(args, "port", int(os.environ.get("CHOIR_SOLFA_PORT", 4790))), open_browser=not getattr(args, "no_open", False))


def output_path(out: str | None, name: str) -> Path:
    """Where `make -o` saves the PDF: a file name, or a folder (ending in / or already there)."""
    if not out:
        return Path(f"{name}.pdf")
    if out.endswith(("/", "\\")) or Path(out).is_dir():
        return Path(out) / f"{name}.pdf"
    return Path(out)


def _make(args) -> int:
    from .pipeline import run

    def progress(stage: str, frac: float) -> None:
        print(f"\r{int(frac * 100):3d}%  {stage:<60}", end="", file=sys.stderr, flush=True)

    words = None
    if args.lyrics:
        try:
            words = Path(args.lyrics).read_text(encoding="utf-8")
        except OSError as e:
            print(f"Could not read the words file: {e}", file=sys.stderr)
            return 1
    with tempfile.TemporaryDirectory(prefix="choir-solfa-") as tmp:
        try:
            result = run(args.song, Path(tmp), mode=args.mode, meter=args.time, title=args.title, progress=progress,
                         lyrics=words)
        except Exception as e:
            print(f"\nCould not make the score: {e}", file=sys.stderr)
            return 1
        print(file=sys.stderr)
        safe = "".join(ch for ch in result.title if ch.isalnum() or ch in " -_").strip() or "score"
        out = output_path(args.out, safe)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result.pdf, out)
        shutil.copyfile(result.midi, out.with_suffix(".mid"))
        tracks = []
        for part, name in result.tracks.items():
            tracks.append(out.with_name(f"{out.stem} - {part}.m4a"))
            shutil.copyfile(result.pdf.parent / name, tracks[-1])
    print(f"{result.title}: {result.key}, {result.meter}, M. {result.tempo}, {result.bars} bars ({result.kind})")
    if result.found_on:
        print(f"Audio taken from: {result.found_on} ({result.found_url})")
    if result.words_found:
        print(f"Words found online: {result.words_found}")
    elif result.words == "missing":
        print("No words found online. Add them with --lyrics FILE.")
    elif result.words == "unplaced":
        print("The words didn't fit the tune, so the score has none.")
    print(f"Saved {out} and {out.with_suffix('.mid')}")
    if tracks:
        print(f"Practice tracks: {', '.join(t.name for t in tracks)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
