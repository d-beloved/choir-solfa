"""Draw the app icon (src/choir_solfa/assets/icon.png, .icns and .ico). macOS only: uses Georgia and iconutil.

    .venv/bin/python scripts/make_icon.py
"""
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

S = Path(tempfile.mkdtemp())
A = Path(__file__).resolve().parent.parent / "src" / "choir_solfa" / "assets"
N, climb, gap = 1024, 95, 18
font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Georgia Bold.ttf", 270)
letters = ["d", "r", "m"]
bbox = {c: font.getbbox(c, anchor="ls") for c in letters}
top_y = min(bbox["d"][1], bbox["m"][1] - 2 * climb)
base = 512 - top_y / 2  # centres the group (from the top of m to the d's baseline)
grad = Image.new("RGBA", (N, N))
top, bot = (0xF2, 0x6B, 0x4A), (0xC0, 0x3E, 0x27)
gd = ImageDraw.Draw(grad)
for y in range(N):
    t = y / (N - 1)
    gd.line([(0, y), (N, y)], fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bot)) + (255,))
lines = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ld = ImageDraw.Draw(lines)
for i in range(-1, 4):  # each letter sits on a line, one more above and below
    y = base - i * climb
    ld.line([(180, y), (844, y)], fill=(255, 255, 255, 46), width=5)
grad = Image.alpha_composite(grad, lines)
img = Image.new("RGBA", (N, N), (0, 0, 0, 0))
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle([100, 100, 924, 924], radius=185, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)
widths = [bbox[c][2] - bbox[c][0] for c in letters]
x = (N - sum(widths) - gap * 2) / 2
for i, c in enumerate(letters):
    d.text((x - bbox[c][0], base - i * climb), c, font=font, fill=(255, 250, 244, 255), anchor="ls")
    x += widths[i] + gap
img.save(A / "icon.png")
iconset = S / "icon.iconset"; shutil.rmtree(iconset, ignore_errors=True); iconset.mkdir()
for size in (16, 32, 128, 256, 512):
    img.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
    img.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(A / "icon.icns")], check=True)
img.crop((100, 100, 924, 924)).save(A / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
