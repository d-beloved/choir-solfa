"""Makes src/choir_solfa/assets/logo.svg: python3 design/logo.py tile > src/choir_solfa/assets/logo.svg (or "full" for the
square Google and phone versions, which are then saved as PNGs).

Draw the Choir Solfa mark: four voices (S A T B) as four parallel lanes that sweep into one S.

Each lane is a quarter-circle chain: top bowl around (cx, cy1), bottom bowl around (cx, cy2).
A lane with radius r at the top has radius D - r at the bottom, so the outside voice at the top
becomes the inside voice at the bottom, like parts weaving through a score.
"""
import sys

COLOURS = ["#e9483b", "#f2b51f", "#1fa3b6", "#4a6fd8"]  # the site's d, m, s, l chime-bar colours


def lanes(size=512, a=18, p=28, w=20, tail=58, cx=None, scale=1.0):
    D = 2 * a + 3 * p
    rmax = a + 3 * p
    height = 2 * rmax + D
    cx = size / 2 if cx is None else cx
    cy1 = size / 2 - height / 2 + rmax
    cy2 = cy1 + D
    out = []
    for i, colour in enumerate(COLOURS):  # outermost at the top first: soprano on top
        r = rmax - i * p
        s = D - r
        d = (f"M{cx + tail:.1f},{cy1 - r:.1f} L{cx:.1f},{cy1 - r:.1f} "
             f"A{r},{r} 0 0 0 {cx - r:.1f},{cy1:.1f} A{r},{r} 0 0 0 {cx:.1f},{cy1 + r:.1f} "
             f"A{s},{s} 0 0 1 {cx + s:.1f},{cy2:.1f} A{s},{s} 0 0 1 {cx:.1f},{cy2 + s:.1f} "
             f"L{cx - tail:.1f},{cy2 + s:.1f}")
        out.append(f'<path d="{d}" stroke="{colour}"/>')
    g = "\n    ".join(out)
    return (f'<g fill="none" stroke-width="{w}" stroke-linecap="round" stroke-linejoin="round" '
            f'transform="translate({size/2},{size/2}) scale({scale}) translate({-size/2},{-size/2})">\n    {g}\n  </g>')


def svg(kind="tile", size=512):
    tile = {"tile": f'<rect width="{size}" height="{size}" rx="{size*0.225:.0f}" fill="url(#bg)"/>'
                    f'<rect x="1.5" y="1.5" width="{size-3}" height="{size-3}" rx="{size*0.225-1.5:.0f}" fill="none" '
                    f'stroke="#ffffff" stroke-opacity=".08" stroke-width="3"/>',
            "full": f'<rect width="{size}" height="{size}" fill="url(#bg)"/>'}[kind]
    scale = 1.12 if kind == "tile" else 0.96  # Google and iOS crop the corners: keep the mark well inside
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}">
  <title>Choir Solfa</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="#262a52"/>
      <stop offset="1" stop-color="#1a1d3a"/>
    </linearGradient>
  </defs>
  {tile}
  {lanes(size, scale=scale)}
</svg>
'''


if __name__ == "__main__":
    print(svg(sys.argv[1] if len(sys.argv) > 1 else "tile"))
