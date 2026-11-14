"""Lay out a Score as a printed tonic solfa sheet.

Every pulse is a column, and the column is as wide as the widest part needs,
so the accent marks (| : ¦) line up down the page for all four parts, the way
printed choir sheets do.
"""
from __future__ import annotations

from reportlab.lib.pagesizes import A4
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from .models import Score
from .notation import bar_is_empty, render_part
from .theory import PARTS, key_label

FONT, BOLD, ITALIC = "Helvetica", "Helvetica-Bold", "Helvetica-Oblique"
SIZE, SMALL = 10.5, 6.5
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


def build_items(score: Score) -> tuple[list, dict]:
    """Bars as layout items; runs of 2+ empty bars become one 'rest' item."""
    names = [p for p in PARTS if p in score.parts]
    rendered = {p: render_part(score.parts[p], p, score.total_cells, score.meter, score.doh_midi) for p in names}
    n_bars = score.total_cells // score.meter.cells_per_bar
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

    system_h = ROW_H * len(names)
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
        for r, name in enumerate(names):
            ry = top - r * ROW_H
            c.setFont(BOLD, SIZE)
            c.drawString(MARGIN, ry, LABELS[name])
        x = MARGIN + LABEL_W
        for item in system:
            for r in range(len(names)):
                c.setFont(FONT, SIZE)
                c.drawString(x + 2, top - r * ROW_H, "|")
            x += _sep_w("|")
            if item[0] == "rest":
                c.setFont(ITALIC, SIZE - 1)
                mid = top - (len(names) - 1) * ROW_H / 2
                c.drawString(x + 8, mid, f"{item[2]} bars rest")
                x += _item_width(item, score) - _sep_w("|")
                continue
            _, b, widths = item
            for p, w in enumerate(widths):
                if p > 0:
                    sep = score.meter.separators[p - 1]
                    for r in range(len(names)):
                        c.setFont(FONT, SIZE)
                        c.drawString(x + 2, top - r * ROW_H, sep)
                    x += _sep_w(sep)
                for r, name in enumerate(names):
                    _draw_runs(c, x + PULSE_PAD, top - r * ROW_H, rendered[name][b][p])
                x += w
        end_mark = "||" if system is systems[-1] else "|"
        for r in range(len(names)):
            c.setFont(FONT, SIZE)
            c.drawString(x + 2, top - r * ROW_H, end_mark)
        y = top - system_h - SYSTEM_GAP

    footer()
    c.save()
