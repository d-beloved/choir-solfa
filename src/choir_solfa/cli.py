"""Command line: `choir-solfa` starts the page; `choir-solfa make SONG` writes a PDF directly."""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

from .models import METERS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="choir-solfa", description="Turn a song into a tonic solfa score for SATB.")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="start the page in your browser (default)")
    serve.add_argument("--port", type=int, default=4790)
    serve.add_argument("--no-open", action="store_true", help="do not open the browser")

    make = sub.add_parser("make", help="make a PDF from a file or link without the page")
    make.add_argument("song", help="audio/video file or a link to the song")
    make.add_argument("-o", "--out", default=None, help="PDF path (default: <title>.pdf here)")
    make.add_argument("--mode", choices=["auto", "choir", "solo"], default="auto")
    make.add_argument("--time", choices=["auto", *METERS], default="auto")
    make.add_argument("--title", default=None)

    args = parser.parse_args(argv)
    if args.cmd == "make":
        return _make(args)
    from .server import serve as run_server
    return run_server(port=getattr(args, "port", 4790), open_browser=not getattr(args, "no_open", False))


def _make(args) -> int:
    from .pipeline import run

    def progress(stage: str, frac: float) -> None:
        print(f"\r{int(frac * 100):3d}%  {stage:<60}", end="", file=sys.stderr, flush=True)

    with tempfile.TemporaryDirectory(prefix="choir-solfa-") as tmp:
        try:
            result = run(args.song, Path(tmp), mode=args.mode, meter=args.time, title=args.title, progress=progress)
        except Exception as e:
            print(f"\nCould not make the score: {e}", file=sys.stderr)
            return 1
        print(file=sys.stderr)
        safe = "".join(ch for ch in result.title if ch.isalnum() or ch in " -_").strip() or "score"
        out = Path(args.out) if args.out else Path(f"{safe}.pdf")
        shutil.copyfile(result.pdf, out)
        shutil.copyfile(result.midi, out.with_suffix(".mid"))
    print(f"{result.title}: {result.key}, {result.meter}, M. {result.tempo}, {result.bars} bars ({result.kind})")
    if result.found_on:
        print(f"Audio taken from: {result.found_on} ({result.found_url})")
    print(f"Saved {out} and {out.with_suffix('.mid')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
