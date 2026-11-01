"""Slider-puzzle image compositor.

Renders a challenge image plus a puzzle-piece sprite with a matching cut-out,
using Pillow only (no network, no external assets).

The piece shape is a jigsaw-ish polygon with a knob, generated once per
challenge and reused for: the cut-out mask in the background image and the
piece sprite the user drags.

All geometry is computed in canvas pixels and returned in the challenge
payload so the verifier can work in the same coordinate space.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field, asdict

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .tokens import CANVAS_W, CANVAS_H, PIECE_SIZE, PIECE_MARGIN_X


@dataclass
class PieceGeometry:
    """The puzzle cut-out polygon, in canvas coordinates."""

    points: list[tuple[float, float]] = field(default_factory=list)
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # x, y, w, h of the piece
    gap_x: int = 0          # true target x (canvas px)
    gap_y: int = 0          # true target y (canvas px)
    piece_x: int = 0        # where the piece starts (left edge of track)
    piece_y: int = 0        # where the piece starts (top edge)
    # The REST position of the shape's opaque box, in canvas px.
    piece_opaque_x: int = 0
    piece_opaque_y: int = 0
    # Travel (canvas px) from the rest position to the gap.
    travel_x: int = 0
    travel_y: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def _build_outline(size: float, knob: float,
                   rng: random.Random) -> list[tuple[float, float]]:
    """A jigsaw-ish square with a knob on the top edge and a notch on the right.

    Coordinates are relative to the piece's top-left corner and the shape is
    guaranteed to fit inside (0, 0)..(size, size) so that the sprite crop and
    the alpha mask always line up.
    """
    s = float(size)
    k = float(knob)
    steps = 16

    # The knob circle sits ON the top edge; its centre is at y = 0 so the
    # bulge reaches y = -k. To keep the shape inside the box we shift the
    # whole thing down by k and record the offset.
    top_inset = s * 0.34
    knob_r = min(k, s * 0.16)
    knob_cx = s * 0.50

    pts: list[tuple[float, float]] = []

    # --- top edge: left -> right, with an upward knob ---
    pts.append((0.0, 0.0))
    pts.append((top_inset, 0.0))
    for i in range(steps + 1):
        t = i / steps
        ang = math.pi + t * math.pi          # pi -> 2pi sweeps the upper half
        pts.append((knob_cx + knob_r * math.cos(ang),
                    knob_r * math.sin(ang)))
    pts.append((s - top_inset, 0.0))
    pts.append((s, 0.0))

    # --- right edge: top -> bottom, with an inward notch ---
    notch_r = min(s * 0.14, k)
    notch_cy = s * 0.48
    pts.append((s, notch_cy - notch_r - s * 0.08))
    for i in range(steps + 1):
        t = i / steps
        ang = -math.pi / 2 + t * math.pi       # sweeps the left half of the circle
        pts.append((s - notch_r + notch_r * math.cos(ang),
                    notch_cy + notch_r * math.sin(ang)))
    pts.append((s, s))

    # --- bottom edge: right -> left ---
    pts.append((s * 0.60, s))
    pts.append((0.0, s))

    # --- left edge: bottom -> top ---
    pts.append((0.0, s * 0.55))
    pts.append((0.0, 0.0))

    return [(round(x, 2), round(y, 2)) for x, y in pts]


def _offset_polygon(points: list[tuple[float, float]], dx: float, dy: float):
    return [(x + dx, y + dy) for x, y in points]


def _synthetic_background(w: int, h: int, rng: random.Random,
                          palette: tuple) -> Image.Image:
    """Build a plausible 'photo-like' background procedurally.

    Layered gradients + soft blobs + high-frequency detail. The high-
    frequency component is what makes template matching meaningful: on a
    smooth gradient every window correlates with every other one, which is
    both unrealistic and useless as a CAPTCHA.
    """
    top, bot, blob_cols = palette
    img = Image.new("RGB", (w, h), tuple(top))
    d = ImageDraw.Draw(img)

    # vertical gradient base
    top = tuple(top)
    bot = tuple(bot)
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=c)

    # soft colour blobs (blurred ellipses) — creates local structure
    blob = Image.new("RGB", (w, h), (0, 0, 0))
    bd = ImageDraw.Draw(blob)
    for _ in range(rng.randint(5, 8)):
        cx = rng.randint(-30, w + 30)
        cy = rng.randint(-20, h + 20)
        r = rng.randint(int(w * 0.12), int(w * 0.42))
        col = tuple(blob_cols[rng.randrange(len(blob_cols))])
        bd.ellipse([cx - r, cy - r * 0.7, cx + r, cy + r * 0.7], fill=col)
    blob = blob.filter(ImageFilter.GaussianBlur(radius=max(6, w // 22)))
    img = Image.blend(img, blob, 0.55)

    # diagonal streaks — directional texture for the solver to key on
    d = ImageDraw.Draw(img)
    for _ in range(rng.randint(3, 6)):
        x0 = rng.randint(-w // 2, w)
        y0 = rng.randint(-h // 2, h)
        length = rng.randint(int(w * 0.4), int(w * 1.3))
        ang = rng.uniform(-0.5, 0.5)
        col = tuple(blob_cols[rng.randrange(len(blob_cols))])
        d.line([(x0, y0), (x0 + length * math.cos(ang), y0 + length * math.sin(ang))],
               fill=col, width=rng.randint(2, 7))

    # fine grain to defeat flat-region matching
    px = img.load()
    for _ in range(int(w * h * 0.06)):
        x, y = rng.randrange(w), rng.randrange(h)
        r, g, b = px[x, y]
        n = rng.randint(-16, 16)
        px[x, y] = (max(0, min(255, r + n)), max(0, min(255, g + n)), max(0, min(255, b + n)))

    # ---- high-frequency texture layer ----
    # Without this the background is a smooth gradient and template matching
    # is ambiguous: nearly every window matches nearly every other one.
    # Real CAPTCHA backgrounds are photos, i.e. rich in local detail.
    tex = Image.new("L", (w, h))
    tp = tex.load()
    for y in range(h):
        for x in range(w):
            tp[x, y] = rng.randint(0, 255)
    # A short blur keeps the detail crisp (photos, not fog) while removing
    # the pure salt-and-pepper look.
    tex = tex.filter(ImageFilter.GaussianBlur(radius=0.7))
    tex = Image.blend(tex, Image.new("L", (w, h), 128), 0.30)

    # multiply the texture into the colour image, modulating brightness
    base_arr = np.asarray(img, dtype=np.float64)
    tex_arr = np.asarray(tex, dtype=np.float64) / 255.0
    # remap the texture to a narrow band around 1.0 so it adds local detail
    # without shifting the overall exposure
    mod = 0.86 + 0.28 * tex_arr
    base_arr *= mod[:, :, None]

    # a second, larger-scale texture layer (blobby light/shadow)
    coarse = Image.new("L", (w, h))
    cp = coarse.load()
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            cp[x, y] = rng.randint(0, 255)
    coarse = coarse.resize((w, h), Image.BILINEAR).filter(
        ImageFilter.GaussianBlur(radius=max(4, w // 40)))
    coarse_arr = np.asarray(coarse, dtype=np.float64) / 255.0
    base_arr *= (0.90 + 0.20 * coarse_arr)[:, :, None]

    # S-curve contrast so the result reads as a photograph rather than a fog.
    x = np.clip(base_arr / 255.0, 0, 1)
    x = x * x * (3.0 - 2.0 * x)          # smoothstep
    base_arr = x * 255.0

    # Gentle saturation lift — the noise layers desaturate the result.
    lum = base_arr.mean(axis=2, keepdims=True)
    base_arr = lum + (base_arr - lum) * 1.22

    img = Image.fromarray(np.clip(base_arr, 0, 255).astype(np.uint8), "RGB")
    return img


# Palettes: (base_top, base_bottom, blob_colours). Muted but chromatic, so
# the noise layers add detail without turning everything into grey mush.
PALETTES = [
    # dusk blue / slate
    ((176, 196, 214), (58, 82, 112), [(126, 158, 188), (88, 122, 158), (154, 182, 206)]),
    # warm sand / rust
    ((224, 198, 164), (118, 82, 58), [(196, 160, 118), (158, 112, 74), (214, 186, 150)]),
    # forest / moss
    ((168, 188, 160), (54, 84, 62), [(118, 146, 116), (82, 114, 88), (146, 168, 140)]),
    # plum / mauve
    ((196, 172, 200), (72, 56, 88), [(150, 122, 162), (104, 80, 122), (176, 152, 184)]),
    # terracotta / cream
    ((226, 190, 158), (122, 74, 52), [(202, 152, 112), (166, 108, 78), (216, 176, 140)]),
    # teal / petrol
    ((158, 196, 198), (40, 88, 96), [(110, 156, 164), (70, 116, 128), (140, 176, 180)]),
    # olive / khaki
    ((206, 200, 158), (96, 92, 52), [(168, 164, 112), (128, 124, 76), (190, 184, 136)]),
    # slate / indigo
    ((168, 172, 206), (48, 54, 96), [(116, 124, 172), (78, 84, 140), (144, 148, 190)]),
]


def render_challenge(seed: int | None = None,
                     width: int = CANVAS_W,
                     height: int = CANVAS_H,
                     piece_size: int = PIECE_SIZE,
                     jitter: int = 0,
                     ) -> tuple[Image.Image, Image.Image, PieceGeometry, dict]:
    """Render (background_with_gap, piece_sprite, geometry, meta).

    The background has the piece knocked out of it (semi-transparent dark cut),
    and the sprite is the piece itself, cut from the *same* underlying image so
    that the solver can match them by texture.
    """
    rng = random.Random(seed)
    palette = PALETTES[rng.randrange(len(PALETTES))]
    base = _synthetic_background(width, height, rng, palette)

    outline = _build_outline(piece_size, piece_size * 0.18, rng)
    # The knob bulges above y=0, so shift the whole shape down by its radius
    # and widen the sprite to keep the protrusion inside the crop.
    knob_r = min(piece_size * 0.18, piece_size * 0.16)
    outline = _offset_polygon(outline, 0.0, knob_r)
    shape_w = piece_size + knob_r
    shape_h = piece_size + knob_r

    # --- choose the gap position so the WHOLE shape fits on the canvas ---
    # The shape's top-left is (gap_x - half, gap_y - half) and it is
    # shape_h = piece_size + knob_r tall, so the bottom edge lands at
    # gap_y - half + shape_h = gap_y + knob_r + half.
    half = piece_size / 2
    # The gap must be far enough from the piece's rest position that the
    # challenge is not trivially "the piece is already next to the hole".
    min_gap_x = int(PIECE_MARGIN_X + shape_w + 40)
    gap_x = rng.randint(min_gap_x, int(width - PIECE_MARGIN_X - half))
    if gap_x <= min_gap_x:
        gap_x = min_gap_x
    y_min = int(PIECE_MARGIN_X + knob_r + half)
    y_max = int(height - PIECE_MARGIN_X - knob_r - half)
    if y_max < y_min:
        y_max = y_min
    gap_y = rng.randint(y_min, y_max)

    # piece starts at the far left, vertically aligned with the gap (that's how
    # every real slider CAPTCHA works: the piece rests in the track).
    piece_x = PIECE_MARGIN_X
    piece_y = gap_y

    gap_pts = _offset_polygon(outline, gap_x - half, gap_y - half)
    piece_pts = _offset_polygon(outline, piece_x, piece_y)

    # --- background with the cut-out ---
    # IMPORTANT: the cut-out must not alter the interior texture, otherwise a
    # template matcher has nothing to correlate against (and the CAPTCHA
    # becomes unsolvable rather than merely hard).
    #
    # Everything is built from the shape MASK (not Pillow's polygon stroking,
    # which strokes inward on concave shapes and would corrupt the texture):
    #   ring   = dilate(shape, 3) - shape          -> soft shadow outside
    #   keyline= dilate(shape, 1) - erode(shape, 1) -> 1px boundary outside
    # so the pixels strictly inside the shape keep their original texture.
    bg = base.convert("RGBA")

    shape_mask = Image.new("L", (width, height), 0)
    ImageDraw.Draw(shape_mask).polygon(gap_pts, fill=255)
    shape_arr = np.asarray(shape_mask, dtype=np.int16)

    def dilate(m: np.ndarray, k: int) -> np.ndarray:
        return np.asarray(Image.fromarray(m.astype(np.uint8), "L")
                          .filter(ImageFilter.MaxFilter(k)), dtype=np.int16)

    def erode(m: np.ndarray, k: int) -> np.ndarray:
        return np.asarray(Image.fromarray(m.astype(np.uint8), "L")
                          .filter(ImageFilter.MinFilter(k)), dtype=np.int16)

    # Soft shadow ring, strictly outside the shape.
    ring_raw = np.clip(dilate(shape_arr, 7) - shape_arr, 0, 255)
    ring_mask = Image.fromarray(ring_raw.astype(np.uint8), "L")
    ring_mask = ring_mask.filter(ImageFilter.GaussianBlur(1.1))
    # `paste` with a mask REPLACES the alpha channel — compose instead.
    ring_tint = Image.new("RGBA", (width, height), (18, 21, 27, 0))
    ring_tint.putalpha(ring_mask.point(lambda v: int(v * 0.68)))
    bg.alpha_composite(ring_tint)

    # 1px keyline on the boundary, strictly outside the shape.
    key_raw = np.clip(dilate(shape_arr, 3) - erode(shape_arr, 3), 0, 255)
    keyline = Image.new("RGBA", (width, height), (12, 14, 19, 230))
    keyline.putalpha(Image.fromarray(key_raw.astype(np.uint8), "L"))
    bg.alpha_composite(keyline)

    # Rim highlight on the OUTSIDE of the lower-right edge: the far wall of
    # the recess catching light. Makes the slot read as a hole, not a sticker.
    shifted = np.roll(np.roll(shape_arr, 2, axis=0), 2, axis=1)
    rim_raw = np.clip(shifted.astype(np.int16) - dilate(shape_arr, 3), 0, 255)
    rim = Image.fromarray(rim_raw.astype(np.uint8), "L")
    rim = rim.filter(ImageFilter.GaussianBlur(0.8))
    highlight = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    highlight.putalpha(rim.point(lambda v: int(v * 0.30)))
    bg.alpha_composite(highlight)

    # --- make the slot UNAMBIGUOUS ---------------------------------------
    # Measured over 10 seeds, the luminance contrast between the hole and
    # its surroundings was often under 4/255: the slot was effectively
    # invisible on flat or dark palettes. A hole must read as a hole.
    #
    # Two extra cues, both OUTSIDE the shape so the texture the solver
    # correlates against stays pristine:
    #   * a thicker, darker keyline hugging the boundary
    #   * a bright specular rim on the lower-right lip of the recess
    dark_raw = np.clip(dilate(shape_arr, 5) - erode(shape_arr, 3), 0, 255)
    dark_edge = Image.new("RGBA", (width, height), (8, 10, 14, 0))
    dark_edge.putalpha(Image.fromarray(dark_raw.astype(np.uint8), "L")
                       .point(lambda v: int(v * 0.85)))
    bg.alpha_composite(dark_edge)

    lip = np.roll(np.roll(shape_arr, 3, axis=0), 3, axis=1)
    lip_raw = np.clip(lip.astype(np.int16) - dilate(shape_arr, 5), 0, 255)
    lip_mask = Image.fromarray(lip_raw.astype(np.uint8), "L")
    lip_mask = lip_mask.filter(ImageFilter.GaussianBlur(0.7))
    spec = Image.new("RGBA", (width, height), (255, 255, 255, 0))
    spec.putalpha(lip_mask.point(lambda v: int(v * 0.55)))
    bg.alpha_composite(spec)

    # Restore the pristine texture inside the shape. Belt and braces: whatever
    # the filter stack did at the boundary, the interior is byte-identical to
    # the source image, so a template matcher always finds the true match.
    #
    # The first 2px ring inside the shape is kept OUT of the restore, so an
    # inner shadow can be drawn there to make the hole read as recessed
    # without touching the texture the solver correlates against.
    inner_ring = np.clip(erode(shape_arr, 5) - erode(shape_arr, 7), 0, 255)
    bg_arr = np.asarray(bg, dtype=np.uint8).copy()
    base_arr = np.asarray(base.convert("RGBA"), dtype=np.uint8)
    interior = (shape_arr > 0) & (inner_ring == 0)
    bg_arr[interior] = base_arr[interior]

    # Inner shadow: darker at the top/left of the hole, like a recess.
    shadow = Image.new("L", (width, height), 0)
    ImageDraw.Draw(shadow).polygon(gap_pts, fill=255)
    shadow = shadow.filter(ImageFilter.GaussianBlur(2.2))
    sh_arr = np.asarray(shadow, dtype=np.float64)
    # amount of shadow = how close the pixel is to the hole's top edge
    depth = np.clip((sh_arr.max() - sh_arr) / max(sh_arr.max(), 1), 0, 1)
    edge_only = (shape_arr > 0) & (inner_ring > 0)
    if edge_only.any():
        factor = 1.0 - 0.62 * depth[edge_only]
        region = bg_arr[edge_only].astype(np.float64)
        bg_arr[edge_only] = np.clip(region * factor[:, None], 0, 255).astype(np.uint8)

    bg = Image.fromarray(bg_arr, "RGBA")

    # --- the piece sprite, cut from the ORIGINAL background at the GAP ---
    # The sprite must carry the texture of the hole it has to fill, otherwise
    # a solver has nothing to match against. That is the whole point.
    #
    # The sprite canvas is (shape_w + 2*pad) square and the shape is drawn at
    # offset (pad, pad) inside it, so crop origin = gap top-left - pad.
    #
    # `pad` must be large enough to hold the halo the gap draws around the
    # shape (the 1px keyline plus the soft shadow ring). The sprite carries
    # its own copy of that halo, so when the user drops the piece into the
    # slot the two haloes coincide and the joint disappears.
    pad = 8
    sw = int(shape_w) + 2 * pad
    sh = int(shape_h) + 2 * pad
    sx = int(gap_x - half)
    sy = int(gap_y - half)
    piece_mask = Image.new("L", (sw, sh), 0)
    ImageDraw.Draw(piece_mask).polygon(
        _offset_polygon(outline, pad, pad), fill=255)
    piece_arr = np.asarray(piece_mask, dtype=np.int16)

    # The sprite's texture comes from the ORIGINAL background (pristine
    # pixels), not from `bg` — the gap's keyline and shadow must not bleed
    # into the texture the solver correlates against.
    piece = base.crop((sx - pad, sy - pad, sx - pad + sw, sy - pad + sh))
    piece = piece.convert("RGBA")
    # Only the shape itself is opaque; the surrounding margin is transparent
    # so the sprite can be dropped over the slot without a visible seam.
    piece.putalpha(piece_mask)

    # The sprite's halo: a soft shadow ring that matches the gap's, so the
    # piece reads as a tile lifted out of the image. It is deliberately
    # lighter than the gap's own ring — the gap is empty and dark, the piece
    # is lit.
    ring_raw = np.clip(dilate(piece_arr, 7) - piece_arr, 0, 255)
    ring_mask = Image.fromarray(ring_raw.astype(np.uint8), "L")
    ring_mask = ring_mask.filter(ImageFilter.GaussianBlur(2.0))
    # NOTE: `paste` with a mask REPLACES the alpha channel, so the tint has
    # to be composited with alpha_composite instead, or the ring's mask is
    # lost and the sprite becomes an opaque square.
    ring_tint = Image.new("RGBA", (sw, sh), (18, 21, 27, 0))
    ring_tint.putalpha(ring_mask.point(lambda v: int(v * 0.30)))
    piece = Image.alpha_composite(ring_tint, piece)

    # A 1px light keyline on the piece's own boundary — a lifted tile needs
    # an edge to read as separate from the surface below it.
    #
    # The keyline is drawn STRICTLY OUTSIDE the shape (dilate - shape), never
    # straddling the boundary: any pixel inside the shape would corrupt the
    # texture the solver correlates against.
    key_raw = np.clip(dilate(piece_arr, 3) - piece_arr, 0, 255)
    keyline = Image.new("RGBA", (sw, sh), (240, 238, 234, 0))
    keyline.putalpha(Image.fromarray(key_raw.astype(np.uint8), "L")
                     .point(lambda v: int(v * 0.38)))
    piece = Image.alpha_composite(piece, keyline)

    # The sprite's OPAQUE bbox starts at the first opaque pixel, which is not
    # necessarily (pad, pad) because the knob reaches the top edge. We need
    # the exact canvas position of the sprite's opaque top-left so the solver
    # can map a template match back to canvas coordinates.
    _ys, _xs = np.where(np.asarray(piece_mask) > 0)
    opaque_dx = int(_xs.min())          # offset inside the sprite PNG
    opaque_dy = int(_ys.min())
    opaque_w = int(_xs.max() - _xs.min() + 1)
    opaque_h = int(_ys.max() - _ys.min() + 1)
    # canvas position of the opaque bbox's top-left, RELATIVE TO THE GAP
    opaque_canvas_x = sx - pad + opaque_dx
    opaque_canvas_y = sy - pad + opaque_dy

    # The REST position of the same shape: the sprite PNG is cropped at the
    # gap, but the client paints it at the rest position. The opaque box
    # moves with it.
    #
    # Vertically the piece stays on the SAME ROW as the gap (that is how a
    # real slider CAPTCHA works: the piece slides horizontally into the slot),
    # so the travel is purely horizontal.
    rest_opaque_x = int(PIECE_MARGIN_X)
    rest_opaque_y = int(opaque_canvas_y)

    # Drop shadow under the piece so it reads as a lifted tile. It is
    # OFFSET (down-right) and blurred, so it falls OUTSIDE the shape and
    # never darkens the texture the solver correlates against.
    sh_mask = Image.new("L", piece.size, 0)
    ImageDraw.Draw(sh_mask).polygon(
        _offset_polygon(outline, pad + 2, pad + 3), fill=255)
    sh_mask = sh_mask.filter(ImageFilter.GaussianBlur(3.5))
    # keep the shadow strictly outside the shape's own pixels
    sh_mask = Image.fromarray(
        (np.asarray(sh_mask, dtype=np.int16) * (255 - piece_arr) // 255
         ).astype(np.uint8), "L")
    sh_img = Image.new("RGBA", piece.size, (0, 0, 0, 0))
    sh_img.putalpha(sh_mask.point(lambda v: int(v * 0.55)))
    piece = Image.alpha_composite(sh_img, piece)

    geom = PieceGeometry(
        points=[list(p) for p in outline],
        bbox=(opaque_canvas_x, opaque_canvas_y, opaque_w, opaque_h),
        gap_x=int(gap_x),
        gap_y=int(gap_y),
        piece_x=int(piece_x),
        piece_y=int(piece_y),
        # The REST position of the shape's OPAQUE box (the shape travels from
        # here to the gap). The client's offset is measured from this point,
        # so it must be in the same reference frame as `bbox`.
        piece_opaque_x=int(rest_opaque_x),
        piece_opaque_y=int(rest_opaque_y),
        # Travel distance between the rest position and the gap.
        travel_x=int(opaque_canvas_x - rest_opaque_x),
        travel_y=int(opaque_canvas_y - rest_opaque_y),
    )

    meta = {
        "seed": seed,
        "width": width,
        "height": height,
        "piece_size": piece_size,
        "palette": palette,
        "jitter": jitter,
        # The sprite's source size, so the client can scale it 1:1 with the
        # canvas (the shape must land on the gap pixel-for-pixel).
        "sprite_w": int(sw),
        "sprite_h": int(sh),
        # The sprite's own opaque box inside its PNG.
        "sprite_opaque_x": int(opaque_dx),
        "sprite_opaque_y": int(opaque_dy),
        "sprite_opaque_w": int(opaque_w),
        "sprite_opaque_h": int(opaque_h),
        # Where the sprite PNG's top-left sits on the canvas, in canvas px.
        "sprite_origin_x": int(sx - pad),
        "sprite_origin_y": int(sy - pad),
    }
    # --- the CLEAN TEMPLATE for the solver ---
    # The piece sprite carries a keyline and a shadow so it reads as a lifted
    # tile, but those decorations would corrupt a template match. The solver
    # therefore gets a separate, PRISTINE crop of the same region: the
    # original texture under the shape's mask, nothing else.
    template = base.crop((sx - pad, sy - pad, sx - pad + sw, sy - pad + sh))
    template = template.convert("RGBA")
    template.putalpha(piece_mask)

    return bg, piece, geom, meta, template
