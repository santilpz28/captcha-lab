"""Classical shape features — the fallback classifier.

When CLIP is unavailable (no model on disk, or the target is synthetic
clip-art where CLIP is unreliable) the attacker falls back to a feature
vector computed from the object's contour:

  * fill ratio — how much of the bounding box the object covers
  * aspect ratio
  * number of connected components
  * Hu moments — rotation/scale invariant shape descriptors

Hu moments are the workhorse: they describe shape *topology*, so a bus
(rectangle) and a boat (triangular hull) stay apart even when their
colours are nearly identical.

The reference signatures (one per class) are supplied by the caller.
Keeping them out of this module is what lets the attacker run without
the generator: with no references, only the generic colour signature
below is available.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

# The synthetic palette the classical classifier knows. The colours are
# deliberately compressed across classes so colour alone cannot separate
# them: a colour-nearest-neighbour classifier scored 100% on the first
# version of the bench, which meant it was measuring colour separation,
# not difficulty.
CLASSES = {
    "bus":  {"color": (188, 92, 58),  "shape": "rect"},
    "car":  {"color": (196, 104, 66), "shape": "rounded"},
    "boat": {"color": (176, 100, 72), "shape": "hull"},
    "bike": {"color": (204, 118, 70), "shape": "wheels"},
    "tree": {"color": (150, 104, 66), "shape": "triangle"},
    "bird": {"color": (186, 110, 74), "shape": "wings"},
}


def draw_shape(label: str, size: int = 72) -> Image.Image:
    """Draw one object shape on a TRANSPARENT canvas.

    This is the shape as it appears in a scene: no background, no grain,
    no gradient. It is what template matching searches for.

    The scene draws each object on its own RGBA layer and pastes it onto
    the background, so the object pixels are the raw palette colour. A
    template with a background would never correlate.
    """
    from PIL import ImageDraw
    spec = CLASSES[label]
    c = tuple(int(v) for v in spec["color"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = size // 2
    shape = spec["shape"]
    if shape == "rect":
        d.rectangle([m - 26, m - 18, m + 26, m + 18], fill=c + (255,))
    elif shape == "rounded":
        d.rounded_rectangle([m - 26, m - 16, m + 26, m + 18], radius=9,
                            fill=c + (255,))
    elif shape == "hull":
        d.polygon([(m - 30, m + 14), (m + 30, m + 14), (m + 18, m - 6),
                   (m - 18, m - 6)], fill=c + (255,))
        d.rectangle([m - 4, m - 24, m + 6, m - 6], fill=c + (255,))
    elif shape == "wheels":
        d.line([(m - 24, m + 16), (m, m - 8), (m + 24, m + 16)],
               fill=c + (255,), width=5)
        d.ellipse([m - 32, m + 8, m - 14, m + 26], outline=c + (255,), width=5)
        d.ellipse([m + 14, m + 8, m + 32, m + 26], outline=c + (255,), width=5)
    elif shape == "triangle":
        d.polygon([(m, m - 26), (m + 26, m + 20), (m - 26, m + 20)],
                  fill=c + (255,))
        d.rectangle([m - 4, m + 18, m + 4, m + 28], fill=c + (255,))
    elif shape == "wings":
        d.ellipse([m - 28, m - 14, m + 2, m + 12], fill=c + (255,))
        d.ellipse([m - 2, m - 14, m + 28, m + 12], fill=c + (255,))
        d.ellipse([m - 6, m - 6, m + 6, m + 6], fill=c + (255,))
    return img


def shape_template(label: str, size: int = 72) -> np.ndarray:
    """The shape as a float RGB array, for template matching.

    Pixels outside the shape are set to NaN so the correlation ignores
    them: a scene where a bus overlaps a tree must still match the bus
    template on the visible part of the bus.
    """
    tpl = draw_shape(label, size).convert("RGB")
    arr = np.asarray(tpl, dtype=np.float32)
    alpha = np.asarray(draw_shape(label, size))[:, :, 3]
    mask = alpha > 128
    out = arr.copy()
    out[~mask] = np.nan
    return out


def draw_tile(label: str, size: int = 96, seed: int = 0) -> Image.Image:
    """Draw one synthetic tile containing the given class.

    The background varies per tile (hue, brightness, gradient) so the
    classifier cannot cheat by keying on the background.

    This lives in the attacker so the attacker can build its own
    reference signatures without importing the challenge generator.
    """
    rng = np.random.default_rng(seed)
    spec = CLASSES[label]
    base = np.array(spec["color"], dtype=np.float32)

    bg = np.zeros((size, size, 3), dtype=np.float32)
    bg[:, :] = 232 - rng.normal(0, 20, (size, size, 1))
    grad = np.linspace(-18, 18, size)[None, :, None]
    bg += grad + rng.normal(0, 14, (size, size, 3))
    img = Image.fromarray(np.clip(bg, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(img)

    c = tuple(int(v) for v in base)
    m = size // 2
    shape = spec["shape"]
    if shape == "rect":
        d.rectangle([m - 26, m - 18, m + 26, m + 18], fill=c)
        d.rectangle([m - 26, m - 18, m + 26, m - 8], fill=tuple(
            max(0, v - 40) for v in c))
    elif shape == "rounded":
        d.rounded_rectangle([m - 26, m - 16, m + 26, m + 18], radius=9, fill=c)
        d.ellipse([m - 22, m - 8, m + 2, m + 14], fill=tuple(
            max(0, v - 45) for v in c))
    elif shape == "hull":
        d.polygon([(m - 30, m + 14), (m + 30, m + 14), (m + 18, m - 6),
                   (m - 18, m - 6)], fill=c)
        d.rectangle([m - 4, m - 24, m + 6, m - 6], fill=c)
    elif shape == "wheels":
        d.line([(m - 24, m + 16), (m, m - 8), (m + 24, m + 16)],
               fill=c, width=5)
        d.ellipse([m - 32, m + 8, m - 14, m + 26], outline=c, width=5)
        d.ellipse([m + 14, m + 8, m + 32, m + 26], outline=c, width=5)
    elif shape == "triangle":
        d.polygon([(m, m - 26), (m + 26, m + 20), (m - 26, m + 20)], fill=c)
        d.rectangle([m - 4, m + 18, m + 4, m + 28], fill=tuple(
            max(0, v - 40) for v in c))
    elif shape == "wings":
        d.ellipse([m - 28, m - 14, m + 2, m + 12], fill=c)
        d.ellipse([m - 2, m - 14, m + 28, m + 12], fill=c)
        d.ellipse([m - 6, m - 6, m + 6, m + 6], fill=tuple(
            max(0, v - 50) for v in c))

    arr = np.asarray(img, dtype=np.float32) + rng.normal(0, 7, (size, size, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


def object_mask(tile: Image.Image) -> np.ndarray:
    """Binary mask of the object inside a tile.

    The tile background is bright and desaturated; the object is a
    saturated blob. Saturation alone is enough and needs no knowledge of
    the palette.
    """
    import cv2
    rgb = np.asarray(tile.convert("RGB"))
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1].astype(np.float32)
    mask = (sat > 90).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    return mask


def features_from_mask(mask: np.ndarray) -> np.ndarray | None:
    """Feature vector for the largest contour in a mask."""
    import cv2
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                               cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    cnt = max(cnts, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    if area < 40:
        return None
    x, y, w, h = cv2.boundingRect(cnt)
    moments = cv2.moments(cnt)
    hu = cv2.HuMoments(moments).flatten()
    hu_n = -np.sign(hu) * np.log10(np.abs(hu) + 1e-30)
    return np.array([
        area / (w * h + 1e-6),      # fill ratio
        w / (h + 1e-6),             # aspect ratio
        float(len(cnts)),           # component count
        *hu_n,
    ], dtype=np.float32)


def features(tile: Image.Image) -> np.ndarray | None:
    """Feature vector for the object inside a tile, or None."""
    try:
        import cv2  # noqa: F401
    except ImportError:
        return None
    try:
        return features_from_mask(object_mask(tile))
    except Exception:
        return None


def colour_signature(tile: Image.Image) -> tuple[str, float]:
    """Generic fallback: describe the object by its dominant hue.

    Returns a coarse label ("warm" / "cool" / "green" / "neutral") and a
    confidence. It is not a real classifier — it exists so the solver
    degrades to *something* instead of crashing when OpenCV is missing.
    """
    import colorsys
    rgb = np.asarray(tile.convert("RGB"), dtype=np.float32) / 255.0
    if rgb.size == 0:
        return "neutral", 0.0
    # Ignore near-white pixels (the tile background).
    mask = rgb.sum(axis=2) < 2.55
    if mask.sum() < 20:
        return "neutral", 0.0
    px = rgb[mask]
    r, g, b = px[:, 0].mean(), px[:, 1].mean(), px[:, 2].mean()
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    if s < 0.15:
        return "neutral", 0.2
    if 0.08 < h < 0.45:
        return "green", float(min(0.4 + s, 0.95))
    if h < 0.08 or h > 0.92:
        return "warm", float(min(0.4 + s, 0.95))
    if 0.45 <= h <= 0.75:
        return "cool", float(min(0.4 + s, 0.95))
    return "warm", float(min(0.3 + s, 0.9))


_REF_CACHE: dict = {}


def reference_features(label: str) -> np.ndarray | None:
    """Shape signature of a class, computed from its canonical drawing."""
    if label in _REF_CACHE:
        return _REF_CACHE[label]
    try:
        feats = features(draw_tile(label, 96, seed=0))
    except Exception:
        return None
    if feats is not None:
        _REF_CACHE[label] = feats
    return feats


def reference_signatures(labels=None) -> dict[str, np.ndarray]:
    """Reference feature vectors for the known classes."""
    labels = labels or list(CLASSES.keys())
    out = {}
    for l in labels:
        f = reference_features(l)
        if f is not None:
            out[l] = f
    return out


def classify(tile: Image.Image,
             references: dict[str, np.ndarray] | None = None
             ) -> tuple[str, float]:
    """Classify a tile by shape against the reference signatures.

    ``references`` maps a label to its feature vector. When it is None
    the built-in synthetic signatures are used; if those are unavailable
    the generic colour signature is returned instead.

    The returned score is the **relative margin** between the best and
    the runner-up distance: 1.0 means "unambiguous", 0.0 means "the
    classifier is guessing between two equally good matches". That is
    what makes it usable as a confidence.
    """
    if references is None:
        references = reference_signatures()
    if not references:
        return colour_signature(tile)

    feats = features(tile)
    if feats is None:
        return colour_signature(tile)

    weights = np.array([1.0, 0.25, 0.4] + [2.0] * (len(feats) - 3),
                        dtype=np.float32)

    def _dist(label: str) -> float:
        ref = references[label]
        return float(np.linalg.norm((feats - ref[:len(feats)]) * weights))

    ordered = sorted(((l, _dist(l)) for l in references),
                      key=lambda kv: kv[1])
    best = ordered[0][0]
    margin = ((ordered[1][1] - ordered[0][1]) / (ordered[0][1] + 1e-6)
              if len(ordered) > 1 and ordered[0][1] > 1e-9 else 1.0)
    return best, float(margin)
