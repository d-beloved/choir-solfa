"""Lay out a Score as a printed tonic solfa sheet.

Every pulse is a column, and the column is as wide as the widest part needs,
so the accent marks (| : ¦) line up down the page for all four parts, the way
printed choir sheets do.
"""
from __future__ import annotations

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from .models import Score
from .notation import bar_is_empty, lay_words, render_part, render_words
from .theory import PARTS, key_label, scale_letters

FONT, BOLD, ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
SIZE, SMALL, WORD_SIZE = 10.5, 6.5, 9
WORDS_H = 13
MARGIN = 40
LABEL_W = 20
ROW_H = 15
SYSTEM_GAP = 16
PULSE_PAD = 3
MIN_PULSE = 9
LABELS = {"Soprano": "S", "Alto": "A", "Tenor": "T", "Bass": "B"}


def _runs_width(runs) -> float:
    return sum(stringWidth(t, FONT, SIZE if s == "n" else SMALL) for t, s in runs)


def _draw_runs(c: canvas.Canvas, x: float, y: float, runs) -> None:
    for text, style in runs:
        if style == "n":
            c.setFont(FONT, SIZE)
            c.drawString(x, y, text)
            x += stringWidth(text, FONT, SIZE)
        else:
            c.setFont(FONT, SMALL)
            c.drawString(x, y + (4.5 if style == "sup" else -2.5), text)
            x += stringWidth(text, FONT, SMALL)


def _sep_w(sep: str) -> float:
    return stringWidth(sep, FONT, SIZE) + 4


def _words_measure(item) -> float:
    return stringWidth(item, ITALIC, WORD_SIZE) if isinstance(item, str) else _runs_width(item)


def _words_width(laid) -> float:
    """Width of one pulse of laid-out words: (x, syllable) pairs."""
    return max((x + stringWidth(t, ITALIC, WORD_SIZE) + 3 for x, t in laid), default=0.0)


def build_items(score: Score) -> tuple[list, dict]:
    """Bars as layout items; runs of 2+ empty bars become one 'rest' item."""
    names = [p for p in PARTS if p in score.parts]
    rendered = {p: render_part(score.parts[p], p, score.total_cells, score.meter, score.doh_midi) for p in names}
    n_bars = score.total_cells // score.meter.cells_per_bar
    words = render_words(score) if score.words else None
    if words is not None:
        m = score.meter
        marked = render_part(score.parts.get("Soprano", []), "Soprano", score.total_cells, m, score.doh_midi, True)
        gap = stringWidth(" ", ITALIC, WORD_SIZE) + 1
        words = [[lay_words(words[b][p], marked[b][p], _words_measure, gap) for p in range(m.pulses_per_bar)]
                 for b in range(n_bars)]
        rendered["words"] = words
    items: list = []
    b = 0
    while b < n_bars:
        if bar_is_empty(score.parts, b, score.meter):
            run = b
            while run < n_bars and bar_is_empty(score.parts, run, score.meter):
                run += 1
            if run - b >= 2:
                items.append(("rest", b, run - b))
                b = run
                continue
        widths = []
        for p in range(score.meter.pulses_per_bar):
            w = max(_runs_width(rendered[name][b][p]) for name in names)
            if words is not None:
                w = max(w, _words_width(words[b][p]))
            widths.append(max(MIN_PULSE, w) + 2 * PULSE_PAD)
        items.append(("bar", b, widths))
        b += 1
    return items, rendered


def _item_width(item, score: Score) -> float:
    bar_w = _sep_w("|")
    if item[0] == "rest":
        return bar_w + stringWidth(f"{item[2]} bars rest", ITALIC, SIZE - 1) + 16
    return bar_w + sum(item[2]) + sum(_sep_w(s) for s in score.meter.separators)


def write_pdf(score: Score, path: str) -> None:
    page_w, page_h = A4
    c = canvas.Canvas(path, pagesize=A4)
    c.setTitle(score.title)
    c.setAuthor("choir-solfa")
    names = [p for p in PARTS if p in score.parts]
    items, rendered = build_items(score)
    avail = page_w - 2 * MARGIN - LABEL_W - _sep_w("||")

    systems, current, width = [], [], 0.0
    for item in items:
        w = _item_width(item, score)
        if current and width + w > avail:
            systems.append(current)
            current, width = [], 0.0
        current.append(item)
        width += w
    if current:
        systems.append(current)

    page = 1

    def footer() -> None:
        c.setFont(FONT, 7.5)
        c.setFillGray(0.4)
        c.drawString(MARGIN, 22, "Made with choir-solfa. Automatic transcription: check it by ear before teaching.")
        c.drawRightString(page_w - MARGIN, 22, f"Page {page}")
        c.setFillGray(0)

    # Header
    y = page_h - MARGIN - 6
    c.setFont(BOLD, 16)
    c.drawString(MARGIN, y, score.title[:80])
    y -= 18
    c.setFont(FONT, 10)
    bpm = int(round(score.tempo_bpm))
    c.drawString(MARGIN, y, f"{key_label(score.doh_pc, score.tonic_pc, score.mode)}     {score.meter.name}     M. {bpm}")
    for line in score.notes:
        y -= 13
        c.setFont(ITALIC, 8.5)
        c.drawString(MARGIN, y, line[:140])
    y -= 26

    # One row per part, with the words under the soprano.
    rows, row_y, offset = [], [], 0.0
    for name in names:
        rows.append(name)
        row_y.append(offset)
        offset += ROW_H
        if name == "Soprano" and "words" in rendered:
            rows.append("words")
            row_y.append(offset - 1)
            offset += WORDS_H
    system_h = offset
    part_rows = [r for r, name in enumerate(rows) if name != "words"]
    for system in systems:
        if y - system_h < MARGIN + 10:
            footer()
            c.showPage()
            page += 1
            y = page_h - MARGIN - 10
        top = y
        # bar number above the first bar
        c.setFont(FONT, 6.5)
        c.setFillGray(0.45)
        c.drawString(MARGIN + LABEL_W, top + 9, str(system[0][1] + 1))
        c.setFillGray(0)
        for r in part_rows:
            c.setFont(BOLD, SIZE)
            c.drawString(MARGIN, top - row_y[r], LABELS[rows[r]])
        x = MARGIN + LABEL_W
        for item in system:
            for r in part_rows:
                c.setFont(FONT, SIZE)
                c.drawString(x + 2, top - row_y[r], "|")
            x += _sep_w("|")
            if item[0] == "rest":
                c.setFont(ITALIC, SIZE - 1)
                mid = top - (row_y[part_rows[0]] + row_y[part_rows[-1]]) / 2
                c.drawString(x + 8, mid, f"{item[2]} bars rest")
                x += _item_width(item, score) - _sep_w("|")
                continue
            _, b, widths = item
            for p, w in enumerate(widths):
                if p > 0:
                    sep = score.meter.separators[p - 1]
                    for r in part_rows:
                        c.setFont(FONT, SIZE)
                        c.drawString(x + 2, top - row_y[r], sep)
                    x += _sep_w(sep)
                for r, name in enumerate(rows):
                    if name == "words":
                        c.setFont(ITALIC, WORD_SIZE)
                        c.setFillGray(0.2)
                        for dx, text in rendered["words"][b][p]:
                            c.drawString(x + PULSE_PAD + dx, top - row_y[r], text)
                        c.setFillGray(0)
                    else:
                        _draw_runs(c, x + PULSE_PAD, top - row_y[r], rendered[name][b][p])
                x += w
        end_mark = "||" if system is systems[-1] else "|"
        for r in part_rows:
            c.setFont(FONT, SIZE)
            c.drawString(x + 2, top - row_y[r], end_mark)
        y = top - system_h - SYSTEM_GAP

    footer()
    c.showPage()
    page += 1
    _write_key_page(c, score, page_w, page_h)
    footer()
    c.save()


def _bar_line(c: canvas.Canvas, x: float, y: float) -> None:
    """A ruled bar line, taller than the letters so it can't be read as l."""
    c.setLineWidth(1.4)
    c.line(x, y - 4, x, y + 15)
    c.setLineWidth(1)


NOTE_GREYS = (0.93, 0.95, 0.93, 0.95, 0.93, 0.95, 0.93, 0.93)


def _write_key_page(c: canvas.Canvas, score: Score, page_w: float, page_h: float) -> None:
    """A last page that explains the marks, so the printout can be handed to singers on its own."""
    width = page_w - 2 * MARGIN
    letters = scale_letters(score.doh_pc)
    y = page_h - MARGIN - 6

    def heading(text: str) -> None:
        nonlocal y
        y -= 26
        c.setFont(BOLD, 12)
        c.drawString(MARGIN, y, text)
        y -= 6

    def para(text: str, indent: float = 0, size: float = 9.5, bullet: bool = False) -> None:
        nonlocal y
        lines = simpleSplit(text, FONT, size, width - indent - (10 if bullet else 0))
        for i, line in enumerate(lines):
            y -= size + 3.5
            c.setFont(FONT, size)
            if bullet and i == 0:
                c.drawString(MARGIN + indent, y, "\u2022")
            c.drawString(MARGIN + indent + (10 if bullet else 0), y, line)

    c.setFont(BOLD, 16)
    c.drawString(MARGIN, y, "How to read this score")
    para("Tonic solfa writes each note as a short name instead of a dot on lines. "
         "This page explains every mark on the score.", size=9.5)

    # The notes, with this song's letter names
    heading("The notes")
    box_w, box_h, gap, half_gap = 50, 36, 8, 2
    x, top = MARGIN, y - 4
    syllables = ["d", "r", "m", "f", "s", "l", "t", "d"]
    for i, name in enumerate(syllables):
        c.setFillGray(NOTE_GREYS[i])
        c.roundRect(x, top - box_h, box_w, box_h, 6, stroke=0, fill=1)
        c.setFillGray(0)
        c.setFont(BOLD, 14)
        tw = stringWidth(name, BOLD, 14) + (stringWidth("1", FONT, SMALL) if i == 7 else 0)
        c.drawString(x + (box_w - tw) / 2, top - 16, name)
        if i == 7:
            c.setFont(FONT, SMALL + 1)
            c.drawString(x + (box_w - tw) / 2 + stringWidth(name, BOLD, 14), top - 11, "1")
        c.setFont(FONT, 9)
        c.setFillGray(0.35)
        c.drawCentredString(x + box_w / 2, top - 30, letters[i % 7])
        c.setFillGray(0)
        x += box_w + (half_gap if i in (2, 6) else gap)
    y = top - box_h
    para(f"In this song doh is {letters[0]}; the letters are the keyboard notes for each name. "
         "Play doh, sing up d r m f s l t d, then find your first note.", size=9)
    y -= 4
    para("m to f and t to the top d are a half step apart (close together). All the other neighbours are a whole step.",
         bullet=True)
    # higher and lower, drawn with real octave marks
    y -= 15
    c.setFont(FONT, 9.5)
    c.drawString(MARGIN, y, "\u2022")
    c.drawString(MARGIN + 10, y, "Higher notes have a small 1 above:")
    xx = MARGIN + 10 + stringWidth("Higher notes have a small 1 above:", FONT, 9.5) + 8
    for runs in ([("d", "n"), ("1", "sup")], [("r", "n"), ("1", "sup")], [("m", "n"), ("1", "sup")]):
        _draw_runs(c, xx, y, runs)
        xx += _runs_width(runs) + 10
    c.setFont(FONT, 9.5)
    label = "Lower notes have a small 1 below:"
    xx += 14
    c.drawString(xx, y, label)
    xx += stringWidth(label, FONT, 9.5) + 8
    for runs in ([("t", "n"), ("1", "sub")], [("l", "n"), ("1", "sub")], [("s", "n"), ("1", "sub")]):
        _draw_runs(c, xx, y, runs)
        xx += _runs_width(runs) + 10
    para("Tenor and bass are written an octave higher than they sound, as is usual in solfa. Sing them in your normal voice.",
         bullet=True)
    para("fe, se and de are a little above f, s and d. ta and ma are a little below t and m.", bullet=True)
    para("A minor song says, for example, \"minor: lah is A\". The tune centres on lah instead of doh; "
         "read the notes the same way.", bullet=True)

    # The rhythm marks
    heading("The rhythm")
    rows = [
        ("|", "Bar line: a new bar starts."),
        (":", "Beat mark. Each space between marks is one beat (count 1, 2, 3...)."),
        ("\u00a6", "The middle of a 4-beat bar, or of a 6/8 bar. Count it like any other beat."),
        ("-", "Keep holding the note before it through this beat."),
        ("(empty)", "A rest: be silent for that beat."),
        ("d.r", "Two notes in one beat, half a beat each."),
        ("d,r.m,f", "Four quick notes in one beat."),
        ("d.,r", "Long then short in one beat: three quarters, then a quarter."),
        ("4 bars rest", "That part is silent for 4 bars."),
        (f"M. {int(round(score.tempo_bpm))}", f"The speed: {int(round(score.tempo_bpm))} beats a minute."),
    ]
    for mark, meaning in rows:
        y -= 15
        c.setFont(BOLD, 10.5)
        c.drawString(MARGIN + 4, y, mark)
        c.setFont(FONT, 9.5)
        c.drawString(MARGIN + 86, y, meaning)
        c.setStrokeGray(0.85)
        c.setLineWidth(0.5)
        c.line(MARGIN, y - 4.5, MARGIN + width, y - 4.5)
    c.setStrokeGray(0)

    # A worked example
    heading("Try it: Silent Night (3/4, three beats in each bar)")
    beats = [("|", "s", "1", "Si-"), (":", "-.l", "2 and", "-i-"), (":", "s", "3", "lent"),
             ("|", "m", "1", "night"), (":", "-", "2", ""), (":", "-", "3", "")]
    col = 62
    y -= 22
    for i, (sep, mark, count, word) in enumerate(beats):
        x = MARGIN + 6 + i * col
        c.setFont(BOLD, 15)
        if sep == "|":
            _bar_line(c, x + 2, y)
        else:
            c.drawString(x, y, sep)
        c.drawString(x + 14, y, mark)
        c.setFont(BOLD, 7.5)
        c.setFillGray(0.4)
        c.drawString(x + 14, y - 12, count)
        c.setFont(ITALIC, 8.5)
        c.drawString(x + 14, y - 23, word)
        c.setFillGray(0)
    _bar_line(c, MARGIN + 8 + 6 * col, y)
    y -= 26
    para("Sing s on beat 1. On beat 2 keep holding s for the first half, then sing l for the second half. "
         "Sing s on beat 3. After the bar line, m is held for all three beats. Count \"1 and 2 and 3 and\" as you sing.")

    # Practising
    heading("Practise your part")
    para("Find your line: S is soprano, A alto, T tenor, B bass. The marks line up down the page, "
         "so all four parts move together.", bullet=True)
    para("The practice audio (MIDI file) opens in GarageBand, MuseScore (free) or any music app that plays MIDI. "
         "Each part is its own track: mute the others to hear just yours, or slow it down.", bullet=True)
    if score.words:
        para("The words are printed under the soprano line. A syllable ending in a hyphen (Si-) means the word "
             "goes on to the next note. A syllable held over several notes is printed once, under the first.",
             bullet=True)
    para("This is an automatic transcription. Sing each part through and check it by ear before teaching it.",
         bullet=True)
