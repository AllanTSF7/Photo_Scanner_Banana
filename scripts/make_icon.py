"""Turn the operator's scanner picture (scripts/icon-source.png) into the app icon.

Writes banana/web/static/icon.ico (16-256 px, used by the packaged exe and the native window) and
banana/web/static/icon.png (256 px, used by the web UI's favicon and brand mark). The source has a white
background; it becomes transparent so the icon sits cleanly on the taskbar, the desktop and the dark UI.

Run from the repo root: .venv\\Scripts\\python.exe scripts\\make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "scripts" / "icon-source.png"
STATIC = ROOT / "banana" / "web" / "static"
SIZES = [16, 24, 32, 48, 64, 128, 256]
PAD = 0.06  # of the icon's edge, around the drawing
WHITE_FROM = 232  # channels at or above this read as background; the ramp up to 255 keeps the outline's anti-aliasing


def _transparent_background(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    pixels = rgba.load()
    width, height = rgba.size
    ramp = 255 - WHITE_FROM
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            lightest = min(r, g, b)
            if lightest >= WHITE_FROM:
                alpha = int(round((255 - lightest) / ramp * 255))
                pixels[x, y] = (r, g, b, min(a, alpha))
    return rgba


def _square_crop(image: Image.Image) -> Image.Image:
    box = image.getchannel("A").getbbox()
    if box is None:
        raise SystemExit(f"{SOURCE} is blank after removing the background")
    left, top, right, bottom = box
    side = max(right - left, bottom - top)
    side = int(side * (1 + 2 * PAD))
    cx, cy = (left + right) / 2, (top + bottom) / 2
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(image, (int(round(side / 2 - cx)), int(round(side / 2 - cy))), image)
    return canvas


def build() -> Image.Image:
    return _square_crop(_transparent_background(Image.open(SOURCE)))


def main() -> None:
    big = build()
    big.resize((256, 256), Image.LANCZOS).save(STATIC / "icon.png")
    frames = [big.resize((n, n), Image.LANCZOS) for n in SIZES]
    frames[-1].save(STATIC / "icon.ico", format="ICO", sizes=[(n, n) for n in SIZES], append_images=frames[:-1])
    print("wrote", STATIC / "icon.ico", "and", STATIC / "icon.png")


if __name__ == "__main__":
    main()
