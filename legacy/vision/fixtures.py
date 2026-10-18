#!/usr/bin/env python3
"""Pictures for the consumer tests: distinct, sized, and drawn from constants.

Three properties the tests need and a directory of committed images could not
give all of at once:

  * **distinct.** A request with four pictures has to be checkable as "four
    different pictures arrived, in this order". Four copies of one file would
    pass a count and prove nothing about ordering.
  * **sized on demand.** The gateway's 8 MiB body limit is a boundary, and a
    boundary needs a picture just under it and one just over. Committing an
    8 MiB PNG to a git repository to test a limit is a poor trade.
  * **not anybody's document.** Every picture here is a shape on white or
    deterministic noise. No fixture is, or is derived from, a real CV, and
    nothing is downloaded.

The noise pictures use a fixed seed, so the bytes are the same from run to run
and a failure can be reproduced. `noise_png` returns a picture of *about* the
requested size; callers assert on the size they measured rather than the size
they asked for, because PNG compresses and the exact figure is not predictable
from the pixel count.
"""

from __future__ import annotations

import io
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from make_synthetic_image import (  # noqa: E402
    draw_blue_circle, draw_portrait_exif, draw_red_square, draw_webp,
)

# Distinct colours, so `image_set(4)` gives four pictures that differ in a way
# a test can name. The first two match the shapes make_synthetic_image draws.
SHAPES = ("red_square", "blue_circle", "green_bar", "orange_ring")
_FILL = {"red_square": (220, 20, 20), "blue_circle": (20, 60, 200),
         "green_bar": (20, 160, 60), "orange_ring": (230, 140, 20)}

NOISE_SEED = 20261013      # fixed: a fixture that changes between runs is not one


def _pillow():
    from PIL import Image, ImageDraw
    return Image, ImageDraw


def shape_png(shape: str, size: int = 896) -> bytes:
    """One of SHAPES as a PNG, on white, with one large central mark."""
    Image, ImageDraw = _pillow()
    im = Image.new("RGB", (size, size), (255, 255, 255))
    d = ImageDraw.Draw(im)
    m = size // 8
    fill = _FILL[shape]
    if shape.endswith("square"):
        d.rectangle([m, m, size - m, size - m], fill=fill)
    elif shape.endswith("circle"):
        d.ellipse([m, m, size - m, size - m], fill=fill)
    elif shape.endswith("bar"):
        d.rectangle([size // 2 - m, m, size // 2 + m, size - m], fill=fill)
    else:
        d.ellipse([m, m, size - m, size - m], outline=fill, width=size // 12)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def image_set(count: int, size: int = 896) -> list[tuple[str, bytes]]:
    """`count` distinct pictures, as (name, png bytes).

    Names are the shape, so a failure says which picture went wrong rather
    than which index. Past the fourth, the shape repeats at a smaller canvas,
    which keeps every picture distinct -- a set that silently contained four
    files when six were asked for would pass a count it had not earned.
    """
    if count < 1:
        raise ValueError("a picture request needs at least one picture")
    out = []
    for i in range(count):
        shape = SHAPES[i % len(SHAPES)]
        repeat = i // len(SHAPES)
        canvas = max(64, size - 32 * repeat)
        name = shape if repeat == 0 else "%s-%d" % (shape, i)
        out.append((name, shape_png(shape, canvas)))
    return out


def as_format(png: bytes, fmt: str) -> bytes:
    """The same picture re-encoded. Used to check the conversion paths."""
    Image, _ = _pillow()
    with Image.open(io.BytesIO(png)) as im:
        buf = io.BytesIO()
        im.convert("RGB").save(buf, fmt)
    return buf.getvalue()


def noise_png(approx_bytes: int, seed: int = NOISE_SEED) -> bytes:
    """A PNG of roughly `approx_bytes` raw bytes.

    Random pixels rather than a shape, because a shape compresses to almost
    nothing and would make "a picture just under the limit" impossible to
    build. The size is approximate by nature; the caller measures it.
    """
    if approx_bytes < 256:
        approx_bytes = 256
    # A pixel is three bytes (RGB), so a canvas of `sqrt(n)` pixels is three
    # times the size asked for. Dividing by three first is the difference
    # between "about n bytes" and "about 3n bytes", and the growth loop below
    # can only correct upwards.
    side = max(8, int(((approx_bytes / 3.0) ** 0.5)))
    Image, _ = _pillow()
    rng = random.Random(seed)
    for _ in range(40):                      # grow until it is close enough
        pixels = bytes(rng.getrandbits(8) for _ in range(side * side * 3))
        im = Image.frombytes("RGB", (side, side), pixels)
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=0)
        data = buf.getvalue()
        if len(data) >= approx_bytes:
            return data
        side = int(side * max(1.15, (approx_bytes / len(data)) ** 0.5))
    return data


def draw_all(out: str) -> list[str]:
    """The on-disk fixture set, for the samples and for a person looking at
    what the tests use. Delegates to make_synthetic_image so there is one
    drawing routine rather than two."""
    os.makedirs(out, exist_ok=True)
    lines = []
    for name, fn in (("red_square.png", draw_red_square),
                     ("blue_circle.png", draw_blue_circle)):
        p = os.path.join(out, name)
        fn(p)
        lines.append("%s (%d bytes)" % (name, os.path.getsize(p)))
    p = os.path.join(out, "portrait_exif.jpg")
    lines.append(draw_portrait_exif(p))
    p = os.path.join(out, "photo.webp")
    lines.append(draw_webp(p))
    return lines


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else HERE
    for line in draw_all(target):
        print(line)
