#!/usr/bin/env python3
"""Generates packaging/app.ico (and a PNG for docs) from code.

Kept as a script rather than a checked-in binary so the icon can be
regenerated at any size, reviewed as a diff, and rebuilt if the brand colour
changes. Run it after editing, and commit the result — the build does not
run this, because CI should not need Pillow.

    python packaging/make_icon.py

Produces the sizes Windows actually asks for: 16 and 32 for the taskbar and
Explorer lists, 48 for medium icons, 256 for the large view and the
installer header. Each is rendered independently at its final size rather
than downsampled from one large image, so the small ones stay legible.
"""
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - dev-only tool
    sys.exit("Pillow is required: pip install pillow")

OUTPUT = Path(__file__).resolve().parent / "app.ico"
PNG_OUTPUT = Path(__file__).resolve().parent / "app.png"

# app/ui/theme.py's ACCENT / ACCENT_DARK, so the icon matches the product.
ACCENT = (37, 99, 235, 255)
ACCENT_DARK = (29, 78, 216, 255)
WHITE = (255, 255, 255, 255)
SIZES = [16, 24, 32, 48, 64, 128, 256]


def render(size: int) -> Image.Image:
    """A box seen in three-quarter view: a lid parallelogram over two body
    faces, which reads as "inventory" even at 16px where an outline drawing
    would collapse into mush."""
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    radius = max(2, round(size * 0.22))
    draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=ACCENT)

    # Box geometry, in fractions of the canvas.
    left, right = size * 0.22, size * 0.78
    top, bottom = size * 0.28, size * 0.74
    middle = size * 0.42          # where the lid meets the body
    centre = size * 0.5
    inset = size * 0.14           # how far the lid overhangs

    # Lid: two quadrilaterals meeting at the centre line.
    draw.polygon([(left, middle), (left + inset, top), (centre, middle * 0.92),
                  (centre, middle)], fill=WHITE)
    draw.polygon([(right, middle), (right - inset, top), (centre, middle * 0.92),
                  (centre, middle)], fill=(235, 240, 255, 255))

    # Body: left face lighter than right, to read as a solid.
    draw.polygon([(left, middle), (centre, middle), (centre, bottom),
                  (left, bottom - size * 0.06)], fill=WHITE)
    draw.polygon([(right, middle), (centre, middle), (centre, bottom),
                  (right, bottom - size * 0.06)], fill=(214, 225, 253, 255))

    # A seam down the centre, only where there are enough pixels for it.
    if size >= 48:
        draw.line([(centre, middle), (centre, bottom)], fill=ACCENT_DARK,
                  width=max(1, round(size * 0.012)))
    return image


def main() -> int:
    frames = [render(size) for size in SIZES]
    # Pillow writes every supplied size into the .ico when sizes= is given.
    frames[-1].save(OUTPUT, format="ICO",
                    sizes=[(size, size) for size in SIZES])
    frames[-1].save(PNG_OUTPUT, format="PNG")
    print(f"Wrote {OUTPUT} ({', '.join(str(s) for s in SIZES)})")
    print(f"Wrote {PNG_OUTPUT} (256x256)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
