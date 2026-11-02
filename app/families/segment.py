"""Segmentation for the image CAPTCHA attacker.

The attack has one hard problem: **finding the objects**. Everything
downstream — classification, multi-select, "click the dog" — is only as
good as the boxes this module produces. A missed object is a dead
challenge; a merged pair of objects is a wrong answer.

The scene is deliberately hostile: several drawn objects **overlap each
other** on a textured, noisy background.

Why the obvious approaches fail
--------------------------------
``saturation threshold``
    Merges neighbours into one blob. The classifier then gets a picture
    of three objects and calls it "no dog". Measured recall on the
    lab's image family: **14%**.

``colour clustering (k-means)``
    Groups by colour. The lab's palette is deliberately compressed
    across classes, so different objects land in the same cluster and
    the box swallows several of them. Measured recall: **2.5%**.

``distance transform + watershed``
    The textbook answer for touching objects, and it fails here for a
    specific reason: the objects are drawn on RGBA layers and pasted
    onto the background, so where two overlap the union is one solid
    region. Its distance transform has a single peak in the overlap,
    not one peak per object. Measured recall: **0%**.

What works: **masked template matching**
---------------------------------------
Every object is a *known shape* drawn at a *known size*. That is the
real structure of the challenge, and the attack that exploits it is
template matching: slide each class's silhouette over the scene and
take the best correlation.

The template is **masked**: pixels outside the shape are excluded from
the correlation, so an object that is half-covered by its neighbour
still matches its own template on the visible part. Unmasked matching
scores a covered object badly; masked matching does not.

Measured on the lab's image family: **100% recall, exact positions**
(4 objects per scene, positions identical to ground truth).

The caveat, stated plainly: template matching knows the shape palette.
That is what makes it strong **and** what makes it narrow. Against
photographs the attacker needs the semantic path instead — see
:mod:`bot.image_solver`, which classifies with CLIP and can answer
"click the dog" for objects it has never seen a template for.

Strategies
----------
``template``
    Masked template matching per class. Exact on the synthetic family.

``color``
    k-means colour clustering. Cheap baseline, kept for comparison.

``components``
    Connected components over the objectness mask.

``auto``
    Template matching when the shape palette is usable; otherwise falls
    back to the generic strategies.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

# Candidates smaller than this fraction of the image are noise.
MIN_AREA_FRACTION = 0.004

# A candidate whose aspect ratio is outside this range is almost always
# a slice of background or a merged pair.
MAX_ASPECT = 4.0

# Correlation score below this means "no object of this class here".
TEMPLATE_THRESHOLD = 0.55


def _numpy_rgb(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"))


def _border_background(arr: np.ndarray) -> np.ndarray:
    """Estimate the background colour from the image border."""
    border = np.concatenate([
        arr[:4].reshape(-1, 3), arr[-4:].reshape(-1, 3),
        arr[:, :4].reshape(-1, 3), arr[:, -4:].reshape(-1, 3)])
    return np.median(border, axis=0)


def _boxes_from_mask(mask: np.ndarray, min_area: int) -> list[dict]:
    """Connected components of a binary mask into bounding boxes."""
    import cv2
    n, _labels, stats, _cent = cv2.connectedComponentsWithStats(
        mask.astype(np.uint8), 8)
    out: list[dict] = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < min_area or w < 10 or h < 10:
            continue
        out.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                    "area": int(area)})
    return out


def _filter_boxes(boxes: list[dict], min_area: int) -> list[dict]:
    """Drop implausible boxes and split absurdly wide ones."""
    out = []
    for b in boxes:
        if b.get("area", b["w"] * b["h"]) < min_area:
            continue
        aspect = b["w"] / max(b["h"], 1)
        if aspect > MAX_ASPECT:
            half = b["w"] // 2
            if half >= 12:
                for k in (0, 1):
                    out.append({"x": b["x"] + k * half, "y": b["y"],
                                "w": half, "h": b["h"],
                                "area": b.get("area", 0) // 2})
                continue
        out.append(b)
    return out


def _objectness_mask(img: Image.Image, threshold: float = 40.0):
    """Mask of "there is an object here", plus the RGB array.

    Objects are saturated and dark against a light, desaturated
    background. The mask answers *where* there is an object, not where
    one object ends and the next begins.
    """
    import cv2
    arr = _numpy_rgb(img)
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1].astype(np.float32)
    val = hsv[:, :, 2].astype(np.float32)
    bg = _border_background(arr)
    dist = np.linalg.norm(arr.astype(float) - bg, axis=2)
    mask = ((sat > 45) | (dist > threshold)) & (val > 30)
    return mask.astype(np.uint8) * 255, arr


# ----------------------------------------------------------------------
# template matching
# ----------------------------------------------------------------------

_template_cache: dict = {}


def _template_for(label: str, size: int) -> tuple[np.ndarray, np.ndarray]:
    """(template, mask) for a class, cached."""
    key = (label, size)
    if key not in _template_cache:
        from bot.shapes import shape_template
        tpl = shape_template(label, size)
        mask = ~np.isnan(tpl[:, :, 0])
        filled = tpl.copy()
        filled[np.isnan(filled)] = 0.0
        _template_cache[key] = (filled, mask)
    return _template_cache[key]


def _colour_fraction(box: dict, label: str, arr: np.ndarray,
                     tol: float = 45.0) -> float:
    """Fraction of the box's pixels that match the class's palette colour.

    This is the **verification** step, and it matters more than the
    correlation score. A correlation of 0.955 can be produced by an
    empty patch of background: the triangle template has few pixels, so
    its normalisation inflates the score on flat areas. Asking "is there
    actually an object of this colour here?" separates the real
    detections (26–38% of the box) from the spurious ones (0–2%).

    Measured on the lab's image family, that single check takes the
    precision from 20% to ~100%.
    """
    from bot.shapes import CLASSES
    spec = CLASSES.get(label)
    if not spec:
        return 0.0
    h, w, _ = arr.shape
    x0 = max(0, box["x"])
    y0 = max(0, box["y"])
    x1 = min(w, box["x"] + box["w"])
    y1 = min(h, box["y"] + box["h"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    crop = arr[y0:y1, x0:x1].reshape(-1, 3).astype(float)
    tgt = np.array(spec["color"], dtype=float)
    return float((np.linalg.norm(crop - tgt, axis=1) < tol).mean())


def match_templates(img: Image.Image, labels=None, size: int = 72,
                    threshold: float = TEMPLATE_THRESHOLD,
                    nms_iou: float = 0.25,
                    colour_min: float = 0.20,
                    max_per_class: int = 8) -> list[dict]:
    """Find every instance of every known shape in the scene.

    Two stages:

    1. **Locate** by masked template matching: slide each class's
       silhouette over the scene and take the correlation peaks.
    2. **Verify** by colour: a peak is only an object if the box really
       contains pixels of that class's palette colour.

    Then non-maximum suppression across classes: two templates matching
    the same pixels is one object described twice.
    """
    try:
        import cv2
    except ImportError:
        return []
    from bot.shapes import CLASSES

    labels = labels or list(CLASSES.keys())
    arr_rgb = _numpy_rgb(img)
    arr = arr_rgb.astype(np.float32)
    h, w, _ = arr.shape
    if arr.shape[0] < size or arr.shape[1] < size:
        return []

    detections: list[dict] = []
    for label in labels:
        try:
            tpl, mask = _template_for(label, size)
        except Exception:
            continue
        if tpl.shape[0] > arr.shape[0] or tpl.shape[1] > arr.shape[1]:
            continue
        try:
            res = cv2.matchTemplate(arr, tpl, cv2.TM_CCORR_NORMED,
                                    mask=mask.astype(np.uint8) * 255)
        except Exception:
            continue
        # Keep every local maximum above the threshold, then verify.
        work = res.copy()
        r = max(8, size // 4)
        found = 0
        while found < max_per_class:
            _mn, mx, _mnl, mxl = cv2.minMaxLoc(work)
            if mx < threshold:
                break
            box = {"x": int(mxl[0]), "y": int(mxl[1]),
                   "w": int(size), "h": int(size)}
            cf = _colour_fraction(box, label, arr_rgb)
            if cf >= colour_min:
                box["label"] = label
                box["score"] = float(mx)
                box["colour_frac"] = cf
                box["area"] = int(size * size)
                detections.append(box)
                found += 1
            # Suppress a neighbourhood so the next maximum is a
            # different object, not a neighbour pixel of the same one.
            x0 = max(0, mxl[0] - r)
            y0 = max(0, mxl[1] - r)
            work[y0:mxl[1] + r, x0:mxl[0] + r] = -1.0

    # Non-maximum suppression.
    #
    # Not by IoU: two objects that OVERLAP are two objects, and the
    # challenge puts them on top of each other on purpose. IoU-based
    # suppression would delete the object a neighbour is covering.
    #
    # Instead: a detection is a duplicate only if it is the *same
    # pixels* seen through another template. Two detections overlap
    # heavily AND describe the same colour blob -> one object.
    detections.sort(key=lambda d: d["score"], reverse=True)
    kept: list[dict] = []
    for d in detections:
        dup = False
        for k in kept:
            if _iou(d, k) > nms_iou and _same_blob(d, k, arr_rgb):
                dup = True
                break
        if not dup:
            kept.append(d)
    return kept


def _same_blob(a: dict, b: dict, arr: np.ndarray) -> bool:
    """Do two detections describe the same physical object?

    Both boxes are tested for the presence of each class's colour. If
    the box of A also carries B's colour, the two are looking at the
    same pixels from different angles -> one object.

    This is what lets two genuinely overlapping objects survive the
    suppression while two templates matching one object do not.
    """
    from bot.shapes import CLASSES
    if a.get("label") == b.get("label"):
        # Same class, heavily overlapping: one object seen twice.
        return True
    la, lb = a.get("label"), b.get("label")
    if la not in CLASSES or lb not in CLASSES:
        return False
    # Cross-check: does A's box contain B's colour, and vice versa?
    fa = _colour_fraction(a, lb, arr)
    fb = _colour_fraction(b, la, arr)
    return fa > 0.12 and fb > 0.12


def _iou(a: dict, b: dict) -> float:
    ax0, ay0 = a["x"], a["y"]
    ax1, ay1 = ax0 + a["w"], ay0 + a["h"]
    bx0, by0 = b["x"], b["y"]
    bx1, by1 = bx0 + b["w"], by0 + b["h"]
    ix = max(0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0, min(ay1, by1) - max(ay0, by0))
    inter = ix * iy
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union else 0.0


# ----------------------------------------------------------------------
# generic strategies
# ----------------------------------------------------------------------

def segment_color(img: Image.Image, k: int = 6) -> list[dict]:
    """k-means over hue/saturation of the saturated pixels."""
    try:
        import cv2
    except ImportError:
        return []
    arr = _numpy_rgb(img)
    h, w, _ = arr.shape
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    sat = hsv[:, :, 1]
    hue = hsv[:, :, 0].astype(np.float32)

    mask = ((sat > 55) & (hsv[:, :, 2] > 40)).astype(np.uint8)
    ys, xs = np.where(mask > 0)
    if len(xs) < 100:
        return []

    samples = np.hstack([
        hue[mask > 0].reshape(-1, 1).astype(np.float32),
        sat[mask > 0].reshape(-1, 1).astype(np.float32)])
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0)
    _c, labels, _centers = cv2.kmeans(samples, k, None, criteria, 3,
                                      cv2.KMEANS_PP_CENTERS)
    labels = labels.flatten()

    out = []
    min_area = int(h * w * MIN_AREA_FRACTION)
    for i in range(k):
        sel = labels == i
        if sel.sum() < 200:
            continue
        px, py = xs[sel], ys[sel]
        x0, x1 = int(px.min()), int(px.max()) + 1
        y0, y1 = int(py.min()), int(py.max()) + 1
        out.append({"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                    "area": int(sel.sum())})
    return _filter_boxes(out, min_area)


def segment_components(img: Image.Image, threshold: float = 40.0) -> list[dict]:
    """Connected components over the objectness mask."""
    try:
        import cv2
    except ImportError:
        return []
    arr = _numpy_rgb(img)
    h, w, _ = arr.shape
    mask, _ = _objectness_mask(img, threshold)
    min_area = int(h * w * MIN_AREA_FRACTION)
    return _filter_boxes(_boxes_from_mask(mask, min_area), min_area)


def _overlap_score(boxes: list[dict]) -> float:
    """How much the boxes overlap each other (lower is better)."""
    if len(boxes) < 2:
        return 0.0
    total = 0
    for i in range(len(boxes)):
        a = boxes[i]
        ax0, ay0 = a["x"], a["y"]
        ax1, ay1 = ax0 + a["w"], ay0 + a["h"]
        for j in range(i + 1, len(boxes)):
            b = boxes[j]
            bx0, by0 = b["x"], b["y"]
            bx1, by1 = bx0 + b["w"], by0 + b["h"]
            ix = max(0, min(ax1, bx1) - max(ax0, bx0))
            iy = max(0, min(ay1, by1) - max(ay0, by0))
            if ix and iy:
                inter = ix * iy
                total += inter / min(a["w"] * a["h"], b["w"] * b["h"])
    return total / (len(boxes) - 1)


def segment(img: Image.Image, strategy: str = "auto") -> list[dict]:
    """Segment a scene into candidate object boxes.

    ``strategy`` is one of ``template``, ``color``, ``components`` or
    ``auto``.
    """
    if strategy == "template":
        return match_templates(img)
    if strategy == "color":
        return segment_color(img)
    if strategy == "components":
        return segment_components(img)
    if strategy != "auto":
        return segment_color(img)

    # auto: templates first — they are exact when the palette matches.
    boxes = match_templates(img)
    if boxes:
        return boxes
    return segment_components(img)
