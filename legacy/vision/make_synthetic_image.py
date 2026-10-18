#!/usr/bin/env python3
"""Synthetic pictures for the vision request contract -- drawn, never downloaded.

WHY THIS FILE EXISTS.  The image samples that ship with the vision work carried a
`resume_sample.png` that was never in the repository, so the sample could not be
run as written, and the only way to make it run was to reach for a real
document.  A test asset that has to be fetched cannot be run on the box, and a
test asset that has to be a real CV cannot be run at all: resumes are personal
data, and a suite whose fixtures are personal data is a suite nobody runs.

So the pictures here are drawn from constants.  Same bytes on every machine,
every run -- which is what makes them usable as fixtures at all.

WHAT THEY ARE FOR.  Both pictures are 896x896 with one large, unambiguous
central shape and nothing else:

    red_square.png     a red square on white   -- "is the shape red?" -> YES
    blue_circle.png    a blue circle on white  -- the same question   -> NO

They are a pair on purpose.  A single picture answered YES proves that
*something* answered; it does not prove the picture was read.  The pair is what
lets a caller notice the difference, because the same canned answer to both is
exactly what an upstream that never looked at either one produces.  See
`examples/vision/README.md` and the "200 is not accuracy" control in
`legacy/gateway/vision_protocol_selftest.py`.

The other two files exercise the conversion paths a real caller hits:
portrait_exif.jpg carries an EXIF Orientation of 6 (phone portrait), and
photo.webp is a format the encoder's decoder cannot read and which must be
converted to PNG first.

No network.  No model.  No GPU.

    python3 examples/vision/make_synthetic_image.py [--out DIR]
"""

from __future__ import annotations

import argparse
import os
import sys

# The size is not arbitrary: a Qwen-VL style encoder is documented as needing at
# least ~1024 image tokens for grounding, and a thumbnail does not produce them.
# 896x896 is large enough that the shape survives tokenisation, and small enough
# to stay well under every body limit in the path.
SIZE = 896
WHITE = (255, 255, 255)
RED = (220, 20, 20)
BLUE = (20, 60, 200)


def _pillow():
    try:
        from PIL import Image, ImageDraw  # noqa: F401
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "Pillow is needed to draw the fixtures: python3 -m pip install pillow"
            "  (%s)" % exc)
    from PIL import Image, ImageDraw
    return Image, ImageDraw


def draw_red_square(path: str) -> None:
    """A red square centred on white. The 'answer should be YES' picture."""
    Image, ImageDraw = _pillow()
    im = Image.new("RGB", (SIZE, SIZE), WHITE)
    d = ImageDraw.Draw(im)
    m = SIZE // 8
    d.rectangle([m, m, SIZE - m, SIZE - m], fill=RED)
    im.save(path, "PNG")


def draw_blue_circle(path: str) -> None:
    """A blue circle centred on white. The 'answer should be NO' picture.

    Its significance is not that it is blue. It is that it is *different*: if
    the same answer comes back for this and for the red square, nothing read
    either of them.
    """
    Image, ImageDraw = _pillow()
    im = Image.new("RGB", (SIZE, SIZE), WHITE)
    d = ImageDraw.Draw(im)
    m = SIZE // 8
    d.ellipse([m, m, SIZE - m, SIZE - m], fill=BLUE)
    im.save(path, "PNG")


def draw_portrait_exif(path: str) -> str:
    """A JPEG whose EXIF says it is stored rotated.

    Returns a note about what happened, because this one can legitimately not
    be produced: writing EXIF needs Pillow's own writer and older ones differ.
    A fixture that silently is not the fixture is worse than no fixture, so the
    caller gets told.
    """
    Image, ImageDraw = _pillow()
    im = Image.new("RGB", (SIZE, SIZE), WHITE)
    d = ImageDraw.Draw(im)
    m = SIZE // 8
    d.rectangle([m, m, SIZE - m, SIZE - m], fill=RED)
    exif = im.getexif()
    exif[0x0112] = 6                       # Orientation: rotate 90 CW to display
    try:
        im.save(path, "JPEG", exif=exif)
    except (TypeError, ValueError, OSError) as exc:
        return "portrait_exif.jpg NOT written (%s)" % exc
    try:
        from PIL import Image as _I
        with _I.open(path) as back:
            got = back.getexif().get(0x0112)
    except Exception as exc:  # noqa: BLE001
        return "portrait_exif.jpg written but unreadable back (%s)" % exc
    if got != 6:
        return "portrait_exif.jpg written but Orientation reads %r, not 6" % (got,)
    return "portrait_exif.jpg OK (Orientation=6)"


def draw_webp(path: str) -> str:
    """A WebP, i.e. a format the encoder's own decoder cannot read."""
    Image, ImageDraw = _pillow()
    im = Image.new("RGB", (SIZE, SIZE), WHITE)
    d = ImageDraw.Draw(im)
    m = SIZE // 8
    d.ellipse([m, m, SIZE - m, SIZE - m], fill=BLUE)
    try:
        im.save(path, "WEBP")
    except (KeyError, OSError, ValueError) as exc:
        return "photo.webp NOT written (this Pillow has no WebP encoder: %s)" % exc
    return "photo.webp OK"


def make_all(out: str) -> list[str]:
    """Draw every fixture into `out`. Returns human-readable lines, one per file.

    Importable, so the request-contract test builds the same bytes it asserts
    on rather than keeping a second copy of the drawing code.
    """
    os.makedirs(out, exist_ok=True)
    lines = []
    for name, fn in (("red_square.png", draw_red_square),
                     ("blue_circle.png", draw_blue_circle)):
        p = os.path.join(out, name)
        fn(p)
        lines.append("%s OK (%d bytes)" % (name, os.path.getsize(p)))
    p = os.path.join(out, "portrait_exif.jpg")
    lines.append(draw_portrait_exif(p))
    p = os.path.join(out, "photo.webp")
    lines.append(draw_webp(p))
    return lines


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=os.path.dirname(os.path.abspath(__file__)),
                    help="where to draw the fixtures (default: this directory)")
    args = ap.parse_args(argv)
    for line in make_all(args.out):
        print(line)
    print("drawn into %s" % os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
