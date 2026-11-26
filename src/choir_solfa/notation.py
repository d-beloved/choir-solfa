"""Turn grid notes into tonic solfa text, one pulse at a time.

A pulse is written as runs of (text, style), where style is "n" for normal
text, "sup" for a raised octave number and "sub" for a lowered one, so the
PDF and the plain-text output can share the same layout.

Curwen rhythm marks used here:
  d        note fills the pulse
  -        previous note held through the pulse
  (blank)  rest
  d.r      two half pulses
  d.,r     three quarters then a quarter
  d,r.m,f  four quarter pulses
"""
from __future__ import annotations

from .models import Meter, QNote
from .theory import solfa, written_pitch

Run = tuple[str, str]

_SUP = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")
_SUB = str.maketrans("0123456789", "₀₁₂₃₄₅₆₇₈₉")


def cell_states(qnotes: list[QNote], total: int) -> tuple[list, list]:
    """Per cell: the sounding pitch (or None) and whether a new note starts there."""
    pitch: list = [None] * total
    onset = [False] * total
    for n in sorted(qnotes, key=lambda n: n.start):
        s, e = max(0, n.start), min(total, n.end)
        if e <= s:
            continue
        for c in range(s, e):
            pitch[c] = n.pitch
            onset[c] = False
        onset[s] = True
    # A pitch change without an onset (from overlapping notes) is still a new note.
    for c in range(total):
        if pitch[c] is not None and not onset[c]:
            if c == 0 or pitch[c - 1] != pitch[c]:
                onset[c] = True
    return pitch, onset


def note_runs(pitch: int, part: str, doh_midi: int) -> list[Run]:
    syllable, octave = solfa(written_pitch(part, pitch), doh_midi)
    runs: list[Run] = [(syllable, "n")]
    if octave > 0:
        runs.append((str(octave), "sup"))
    elif octave < 0:
        runs.append((str(-octave), "sub"))
    return runs


def render_pulse(pitch: list, onset: list, part: str, doh_midi: int, marks: bool = False) -> list[Run]:
    """One pulse as runs. With `marks`, an empty ("", "@i") run comes before the note that starts
    at cell i of the pulse, so words can be put under it (see `split_marks`)."""
    n = len(pitch)

    def is_event(i: int) -> bool:
        if i == 0 or onset[i]:
            return True
        return pitch[i] is None and pitch[i - 1] is not None

    def tok(i: int) -> list[Run]:
        if pitch[i] is None:
            return []
        if onset[i]:
            return ([("", f"@{i}")] if marks else []) + note_runs(pitch[i], part, doh_midi)
        return [("-", "n")]

    events = {i for i in range(n) if is_event(i)}
    dot, comma = (".", "n"), (",", "n")

    if events == {0}:
        return tok(0)
    if n == 4:
        if events == {0, 3}:
            return tok(0) + [(".,", "n")] + tok(3)
        first = tok(0) + ([comma] + tok(1) if 1 in events else [])
        second = tok(2) if 2 in events else ([("-", "n")] if pitch[2] is not None else [])
        second = second + ([comma] + tok(3) if 3 in events else [])
        return first + [dot] + second
    if n == 2:
        return tok(0) + [dot] + tok(1)
    out: list[Run] = tok(0)
    for i in range(1, n):
        out += [dot] + (tok(i) if i in events else [])
    return out


def render_part(qnotes: list[QNote], part: str, total: int, meter: Meter, doh_midi: int,
                marks: bool = False) -> list[list[list[Run]]]:
    """Return bars -> pulses -> runs for one part."""
    pitch, onset = cell_states(qnotes, total)
    cpp = meter.cells_per_pulse
    bars = []
    for b in range(total // meter.cells_per_bar):
        pulses = []
        for p in range(meter.pulses_per_bar):
            s = b * meter.cells_per_bar + p * cpp
            pulses.append(render_pulse(pitch[s:s + cpp], onset[s:s + cpp], part, doh_midi, marks))
        bars.append(pulses)
    return bars


def join_syllables(texts: list[str]) -> str:
    out = ""
    for t in texts:
        out = out + t if (not out or out.endswith("-")) else f"{out} {t}"
    return out


def split_marks(runs: list[Run]) -> tuple[list[Run], dict[int, int]]:
    """Runs without the "@i" marks, and for each marked cell the number of runs before its note."""
    clean: list[Run] = []
    at: dict[int, int] = {}
    for text, style in runs:
        if style.startswith("@"):
            at[int(style[1:])] = len(clean)
        else:
            clean.append((text, style))
    return clean, at


def render_words(score) -> list[list[list[tuple[int, str]]]]:
    """Bars -> pulses -> (cell in the pulse, syllables sung there) under the words line."""
    m = score.meter
    out = []
    for b in range(score.total_cells // m.cells_per_bar):
        bar = []
        for p in range(m.pulses_per_bar):
            s = b * m.cells_per_bar + p * m.cells_per_pulse
            bar.append([(c - s, score.words[c]) for c in range(s, s + m.cells_per_pulse) if c in score.words])
        out.append(bar)
    return out


def lay_words(syls: list[tuple[int, str]], runs: list[Run], measure, gap: float) -> list[tuple[float, str]]:
    """Where each syllable starts: under its note in `runs` (marked soprano runs), or just
    after the syllable before when that one is in the way. `measure` gives the width of
    runs (a list) or of a syllable (a string)."""
    clean, at = split_marks(runs)
    out: list[tuple[float, str]] = []
    end = None
    for cell, text in syls:
        x = measure(clean[:at[cell]]) if cell in at else 0.0
        if end is not None:
            x = max(x, end + gap)
        out.append((x, text))
        end = x + measure(text)
    return out


def words_line(laid: list[tuple[float, str]]) -> str:
    line = ""
    for x, text in laid:
        line = line.ljust(int(x)) + text
    return line


def _text_measure(item) -> int:
    return len(item) if isinstance(item, str) else len(runs_text(item))


def bar_is_empty(qnotes_by_part: dict, bar: int, meter: Meter) -> bool:
    s, e = bar * meter.cells_per_bar, (bar + 1) * meter.cells_per_bar
    return not any(n.start < e and n.end > s for notes in qnotes_by_part.values() for n in notes)


def runs_text(runs: list[Run]) -> str:
    out = []
    for text, style in runs:
        if style == "sup":
            out.append(text.translate(_SUP))
        elif style == "sub":
            out.append(text.translate(_SUB))
        else:
            out.append(text)
    return "".join(out)


def bar_text(pulses: list[list[Run]], meter: Meter) -> str:
    parts = [runs_text(pulses[0])]
    for sep, pulse in zip(meter.separators, pulses[1:]):
        parts.append(f" {sep}{runs_text(pulse)}")
    return "".join(parts)


def score_text(score, bars_per_line: int = 4) -> str:
    """The whole score as plain text, easy to paste into a chat message.

    Every pulse is padded to the widest part so the beats line up in a
    monospaced font.
    """
    from .theory import LINES, key_label

    names = [p for p in LINES if p in score.parts]
    meter = score.meter
    rendered = {p: render_part(score.parts[p], p, score.total_cells, meter, score.doh_midi) for p in names}
    n_bars = score.total_cells // meter.cells_per_bar
    words = render_words(score) if score.words else None
    if words is not None:
        under = score.words_part
        marked = render_part(score.parts.get(under, []), under, score.total_cells, meter, score.doh_midi, True)
        words = [[words_line(lay_words(words[b][k], marked[b][k], _text_measure, 1)) for k in range(meter.pulses_per_bar)]
                 for b in range(n_bars)]
    lines = [score.title, f"{key_label(score.doh_pc, score.tonic_pc, score.mode)}   {meter.name}   M. {round(score.tempo_bpm)}"]
    lines += score.notes
    for start in range(0, n_bars, bars_per_line):
        bars = list(range(start, min(n_bars, start + bars_per_line)))
        text = {p: [[runs_text(rendered[p][b][k]) for k in range(meter.pulses_per_bar)] for b in bars] for p in names}
        rows = list(names)
        if words is not None:
            text["words"] = [[words[b][k] for k in range(meter.pulses_per_bar)] for b in bars]
            rows.insert(rows.index(under) + 1 if under in rows else 0, "words")
        width = [[max(len(text[p][i][k]) for p in rows) for k in range(meter.pulses_per_bar)] for i in range(len(bars))]
        lines += ["", f"Bar {start + 1}"]
        for p in rows:
            row = []
            for i in range(len(bars)):
                cells = [text[p][i][0].ljust(width[i][0])]
                for k in range(1, meter.pulses_per_bar):
                    cells.append(f"{meter.separators[k - 1]}{text[p][i][k].ljust(width[i][k])}")
                row.append(" ".join(cells))
            end = " ||" if bars[-1] == n_bars - 1 else " |"
            label = " " if p == "words" else p[0]
            lines.append(f"{label}  | " + " | ".join(row) + end)
    lines += ["", "How to read: | bar line   : beat   - hold the note   . half beat   , quarter beat   "
                  "d¹ higher   s₁ lower   (blank) rest"]
    if words is not None:
        lines.append(f"The words are under the {score.words_part.lower()} line. Si- means the word goes on to the next note.")
    return "\n".join(lines) + "\n"
