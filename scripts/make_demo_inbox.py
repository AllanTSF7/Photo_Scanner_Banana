"""Fill an inbox with fake FastFoto-style scans for trying the UI.

    python scripts/make_demo_inbox.py /path/to/inbox

Creates fronts with simple shapes, backs with "handwriting", a blank back, a front with no back,
and a slightly resized rescan of the first photo (to exercise the duplicate flag).
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

W, H = 1800, 1200


def front(seed: int) -> Image.Image:
    rnd = random.Random(seed)
    sky = tuple(rnd.randint(60, 220) for _ in range(3))
    img = Image.new("RGB", (W, H), sky)
    d = ImageDraw.Draw(img)
    d.rectangle((0, int(H * 0.62), W, H), fill=tuple(rnd.randint(40, 140) for _ in range(3)))
    for _ in range(rnd.randint(2, 5)):
        x, y, r = rnd.randint(100, W - 300), rnd.randint(200, H - 400), rnd.randint(80, 260)
        d.ellipse((x, y, x + r, y + r * 1.4), fill=tuple(rnd.randint(0, 255) for _ in range(3)))
    d.rectangle((0, 0, W - 1, H - 1), outline=(250, 250, 245), width=36)  # white print border
    return img.filter(ImageFilter.GaussianBlur(2))


def back(lines: list[str]) -> Image.Image:
    img = Image.new("RGB", (W, H), (243, 239, 229))
    d = ImageDraw.Draw(img)
    for i, text in enumerate(lines):
        d.text((160, 220 + i * 140), text, fill=(35, 40, 95), font_size=96)
    return img.rotate(1.5, fillcolor=(243, 239, 229))


def main(inbox: Path) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    photos = [
        ("Attic3_0001", 11, ["Xmas '84", "Grandma & John"]),
        ("Attic3_0002", 12, ["Summer of 1979", "Lake Erie"]),
        ("Attic3_0003", 13, []),  # blank back
        ("Attic3_0004", 14, None),  # no back scanned
        ("Attic3_0005", 15, ["Mary's 5th birthday", "June 12, 1988"]),
        ("Attic3_0006", 16, ["the 1960s?"]),
    ]
    for base, seed, lines in photos:
        front(seed).save(inbox / f"{base}.jpg", quality=92)
        if lines is not None:
            back(lines).save(inbox / f"{base}_b.jpg", quality=92)

    rescan = front(11).resize((1760, 1173)).crop((0, 0, 1760, 1173))
    rescan.save(inbox / "Attic3_0007.jpg", quality=85)
    back(["Xmas '84", "Grandma & John"]).save(inbox / "Attic3_0007_b.jpg", quality=85)
    print(f"wrote {len(list(inbox.iterdir()))} files to {inbox}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
