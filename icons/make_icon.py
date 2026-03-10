#!/usr/bin/env python3
"""Generates Airzone-style app icon: blue cloud with white 'A'."""

from PIL import Image, ImageDraw, ImageFilter, ImageFont
import math, os, subprocess
from pathlib import Path

SIZE = 1024
OUT_PNG = "icon.png"

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

# ── Rounded-rect background (dark navy → very dark blue) ─────────────────────
def rounded_rect(draw, xy, radius, fill):
    x0, y0, x1, y1 = xy
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill)

bg_margin = 20
rounded_rect(draw, [bg_margin, bg_margin, SIZE-bg_margin, SIZE-bg_margin],
             radius=200, fill=(18, 25, 48, 255))

# ── Cloud shape (circles composited) ─────────────────────────────────────────
cloud_layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
cd = ImageDraw.Draw(cloud_layer)

# Gradient-ish blue: draw layered circles for cloud body
cloud_color_dark  = (30, 100, 200, 255)
cloud_color_mid   = (55, 140, 235, 255)
cloud_color_light = (90, 175, 255, 255)

cx, cy = SIZE // 2, SIZE // 2 + 40   # cloud centre (shifted down slightly)

def ellipse(d, cx, cy, rx, ry, fill):
    d.ellipse([cx-rx, cy-ry, cx+rx, cy+ry], fill=fill)

# Main body — three overlapping circles
ellipse(cd, cx,        cy+10,  310, 230, cloud_color_dark)   # body
ellipse(cd, cx-180,    cy+30,  190, 165, cloud_color_dark)   # left bump
ellipse(cd, cx+185,    cy+30,  200, 170, cloud_color_dark)   # right bump
ellipse(cd, cx-30,     cy-90,  220, 195, cloud_color_dark)   # top dome

# Highlight pass (lighter tones on top)
ellipse(cd, cx,        cy,     295, 215, cloud_color_mid)
ellipse(cd, cx-175,    cy+15,  178, 152, cloud_color_mid)
ellipse(cd, cx+180,    cy+15,  188, 158, cloud_color_mid)
ellipse(cd, cx-25,     cy-100, 208, 182, cloud_color_mid)

# Specular highlight (top-left glow)
ellipse(cd, cx-80,     cy-120, 145, 125, cloud_color_light)

# Slight soft blur to smooth the cloud edges
cloud_layer = cloud_layer.filter(ImageFilter.GaussianBlur(radius=3))
img = Image.alpha_composite(img, cloud_layer)
draw = ImageDraw.Draw(img)

# ── White "A" letter ──────────────────────────────────────────────────────────
# Try system fonts; fall back to default
font_candidates = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Arial.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/SFNSDisplay.ttf",
]
font = None
for fc in font_candidates:
    if os.path.exists(fc):
        try:
            font = ImageFont.truetype(fc, size=440)
            break
        except Exception:
            pass
if font is None:
    font = ImageFont.load_default()

letter = "A"
# Measure text
bbox = draw.textbbox((0, 0), letter, font=font)
tw = bbox[2] - bbox[0]
th = bbox[3] - bbox[1]
tx = (SIZE - tw) // 2 - bbox[0]
ty = (SIZE - th) // 2 - bbox[1] + 30   # slightly below centre

# Drop shadow
draw.text((tx+6, ty+8), letter, font=font, fill=(0, 40, 120, 160))
# Main white letter
draw.text((tx, ty), letter, font=font, fill=(255, 255, 255, 255))

# ── Save PNG ──────────────────────────────────────────────────────────────────
img.save(OUT_PNG)
print(f"Saved {OUT_PNG}")

# ── Build .icns for macOS ─────────────────────────────────────────────────────
iconset = "Airzone.iconset"
os.makedirs(iconset, exist_ok=True)

sizes = [16, 32, 64, 128, 256, 512, 1024]
for s in sizes:
    resized = img.resize((s, s), Image.LANCZOS)
    resized.save(f"{iconset}/icon_{s}x{s}.png")
    if s <= 512:
        resized2 = img.resize((s*2, s*2), Image.LANCZOS)
        resized2.save(f"{iconset}/icon_{s}x{s}@2x.png")

result = subprocess.run(
    ["iconutil", "-c", "icns", iconset, "-o", str(Path(__file__).parent / "Airzone.icns")],
    capture_output=True, text=True
)
if result.returncode == 0:
    print("Saved icons/Airzone.icns")
else:
    print("iconutil failed:", result.stderr)
