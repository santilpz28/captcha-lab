"""Family C — image grid (multi-select) challenges.

The "select all the images containing X" CAPTCHA: a 3×3 grid of tiles, some
of which contain the target object, and the user must click every matching
one.

**Why this family is the interesting one:** this is where the arithmetic
finally bites the attacker.

Let `p` be the classifier's per-cell accuracy and `k` the number of target
cells. A correct submission needs *every* target cell picked AND *no* false
positive. With per-cell accuracy p, the chance of getting a cell right is
not simply p — the attacker must both recognise the object and make the
right click decision, so the effective per-cell rate is lower.

The practical consequence: a classifier that is 95% accurate per cell still
produces a full-challenge success rate that collapses. That is the
trigonometry that makes grid CAPTCHAs meaningfully harder than text ones —
and it is exactly what this family measures.

The images here are **synthetic**: procedurally drawn objects with clear
visual identity. That keeps the bench dependency-free and reproducible,
while still exercising the whole pipeline (detect → classify → select).
A real-world audit would swap the tile source for scraped imagery.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image, ImageDraw

from .base import (Attempt, Challenge, decode_data_url, perturb_image,
                    rand_seed, to_data_url)

# --- the taxonomy ---------------------------------------------------------
# Each class has a distinctive SHAPE and texture, but the classes share a
# similar colour palette on purpose.
#
# This matters for honesty: the first version of this bench gave every
# class its own saturated hue and the attacker scored 100%, which made the
# bench worthless — it measured colour separation, not the difficulty of
# the selection problem. Real image grids (reCAPTCHA "select all traffic
# lights") show photographs of the same kind of thing with wildly varying
# colour, so the classifier must key on shape and context, not hue.
#
# The palette below is deliberately compressed: all classes sit within a
# narrow hue band and differ mostly in luminance and saturation.

CLASSES = {
    "bus":     {"color": (188, 92, 58),  "shape": "rect"},
    "car":     {"color": (196, 104, 66), "shape": "rounded"},
    "boat":    {"color": (176, 100, 72), "shape": "hull"},
    "bike":    {"color": (204, 118, 70), "shape": "wheels"},
    "tree":    {"color": (150, 104, 66), "shape": "triangle"},
    "bird":    {"color": (186, 110, 74), "shape": "wings"},
}

TARGETS = list(CLASSES.keys())


def draw_tile(label: str, size: int = 96, seed: int = 0) -> Image.Image:
    """Draw one synthetic tile containing the given class.

    The background varies per tile (hue, brightness, gradient) so the
    classifier cannot cheat by key on the background.
    """
    rng = np.random.default_rng(seed)
    spec = CLASSES[label]
    base = np.array(spec["color"], dtype=np.float32)

    # Background: a textured field with a per-tile gradient, so the tile is
    # not a flat colour and the classifier has to look at the shape.
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

    # Photographic-ish grain.
    arr = np.asarray(img, dtype=np.float32) + rng.normal(0, 7, (size, size, 3))
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")


class GridFamily:
    """Generator + feature-based classifier attacker for image grids."""

    name = "grid"

    def __init__(self, grid: int = 3, target_count: tuple[int, int] = (2, 4),
                 tile_size: int = 96, distortion: float = 0.25):
        self.grid = grid
        self.target_count = target_count
        self.tile_size = tile_size
        self.distortion = distortion

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def generate(self, seed: int | None = None, target: str | None = None,
                 **_) -> Challenge:
        seed = rand_seed() if seed is None else seed
        rng = np.random.default_rng(seed)
        g = self.grid
        n_cells = g * g

        target = target or TARGETS[int(rng.integers(len(TARGETS)))]
        lo, hi = self.target_count
        n_target = int(rng.integers(lo, hi + 1))
        n_target = min(n_target, n_cells - 1)

        # Choose which cells hold the target, and which hold distractors.
        positions = list(range(n_cells))
        rng.shuffle(positions)
        target_positions = sorted(positions[:n_target])
        other_positions = sorted(positions[n_target:])

        labels = [""] * n_cells
        for p in target_positions:
            labels[p] = target
        pool = [c for c in TARGETS if c != target]
        for p in other_positions:
            labels[p] = pool[int(rng.integers(len(pool)))]

        # Render the tiles.
        tiles = []
        for i, lab in enumerate(labels):
            t = draw_tile(lab, self.tile_size, seed=seed * 100 + i)
            if self.distortion > 0:
                t = perturb_image(t, seed * 1000 + i, strength=self.distortion)
            tiles.append(t)

        # Compose the grid with a gap, like a real CAPTCHA.
        gap = 6
        cell = self.tile_size
        W = g * cell + (g - 1) * gap
        canvas = Image.new("RGB", (W, W), (250, 250, 250))
        for i, t in enumerate(tiles):
            r, c = divmod(i, g)
            canvas.paste(t, (c * (cell + gap), r * (cell + gap)))

        return Challenge(
            family=self.name,
            cid=Challenge.new_id(),
            seed=seed,
            created=time.time(),
            public={
                "family": self.name,
                "prompt": "Select all images containing: {}".format(target),
                "image": to_data_url(canvas),
                "grid": g,
                "tile_size": cell,
                "gap": gap,
                "size": W,
                "target": target,
            },
            truth={"cells": target_positions, "labels": labels,
                   "target": target},
            artifacts={"tiles": tiles},
        )

    # ------------------------------------------------------------------
    # attacker
    # ------------------------------------------------------------------
    def solve(self, challenge: Challenge) -> Attempt:
        """Classify every cell and select the ones that look like the target.

        The classifier is a colour-signature matcher: it compares the mean
        hue of a cell against the known palette and picks the closest class.
        That is a legitimate, simple baseline — and a deliberately modest
        one. A modern CNN would beat it easily; the point of the bench is
        the *selection arithmetic*, not the classifier's ceiling.
        """
        t0 = time.time()
        img = decode_data_url(challenge.public["image"])
        g = challenge.public["grid"]
        cell = challenge.public["tile_size"]
        gap = challenge.public["gap"]
        target = challenge.public["target"]

        picked = []
        scores = {}
        for i in range(g * g):
            r, c = divmod(i, g)
            x0 = c * (cell + gap)
            y0 = r * (cell + gap)
            tile = img.crop((x0, y0, x0 + cell, y0 + cell))
            label, score = self._classify(tile)
            scores[i] = (label, round(score, 4))
            if label == target:
                picked.append(i)

        return Attempt(
            answer=sorted(picked),
            duration_ms=round((time.time() - t0) * 1000, 1),
            pointer_events=len(picked),
            meta={"cells_scored": scores,
                  "target": target,
                  "n_picked": len(picked)},
        )

    @staticmethod
    def _classify(tile: Image.Image) -> tuple[str, float]:
        """Classify a tile by the SHAPE of the object in it.

        The palette is deliberately compressed across classes (see CLASSES),
        so colour alone cannot separate them — a colour-nearest-neighbour
        classifier scored 100% on the first version of this bench, which
        meant it was measuring colour separation, not difficulty.

        The feature vector is deliberately simple and generic:
          * the shape's relative area,
          * its aspect ratio and fill ratio (how much of its bounding box
            it actually covers),
          * the number of connected components,
          * Hu moments (rotation/scale invariant shape descriptors).

        Hu moments are the workhorse: they describe shape topology, so a
        bus (rectangle) and a boat (triangle-ish hull) stay apart even when
        their colours are nearly identical.
        """
        arr = np.asarray(tile.convert("RGB"), dtype=np.float32)
        try:
            import cv2
        except ImportError:
            return TextFallback._classify_colour(tile)

        # Segment the object: the tile background is bright and desaturated,
        # the object is a saturated blob. Use the distance from the local
        # background colour.
        hsv = cv2.cvtColor(np.asarray(tile.convert("RGB")), cv2.COLOR_RGB2HSV)
        sat = hsv[:, :, 1].astype(np.float32)
        mask = (sat > 90).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))

        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        if not cnts:
            return TextFallback._classify_colour(tile)
        cnt = max(cnts, key=cv2.contourArea)
        area = cv2.contourArea(cnt)
        if area < 40:
            return TextFallback._classify_colour(tile)

        x, y, w, h = cv2.boundingRect(cnt)
        moments = cv2.moments(cnt)
        hu = cv2.HuMoments(moments).flatten()
        # Normalise Hu moments so they are scale/translation invariant and
        # comparable across classes.
        hu_n = -np.sign(hu) * np.log10(np.abs(hu) + 1e-30)

        feats = np.array([
            area / (w * h + 1e-6),                       # fill ratio
            w / (h + 1e-6),                             # aspect ratio
            float(len(cnts)),                           # component count
            *hu_n,
        ], dtype=np.float32)
        weights = np.array([1.0, 0.25, 0.4] + [2.0] * len(hu_n),
                            dtype=np.float32)

        def _dist(label: str) -> float:
            ref = GridFamily._reference_features(label)
            return float(np.linalg.norm((feats - ref[:len(feats)]) * weights))

        best = min(CLASSES.keys(), key=_dist)
        ordered = sorted(((l, _dist(l)) for l in CLASSES), key=lambda kv: kv[1])
        margin = ((ordered[1][1] - ordered[0][1]) / (ordered[0][1] + 1e-6)
                  if ordered[0][1] > 1e-9 else 1.0)
        return best, margin

    _REF_CACHE: dict = {}

    @classmethod
    def _reference_features(cls, label: str) -> np.ndarray:
        """Shape signature of a class, computed from its canonical drawing."""
        if label in cls._REF_CACHE:
            return cls._REF_CACHE[label]
        import cv2
        tile = draw_tile(label, 96, seed=0)
        hsv = cv2.cvtColor(np.asarray(tile.convert("RGB")), cv2.COLOR_RGB2HSV)
        sat = hsv[:, :, 1].astype(np.float32)
        mask = (sat > 90).astype(np.uint8) * 255
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
        cnt = max(cnts, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(cnt)
        area = cv2.contourArea(cnt)
        moments = cv2.moments(cnt)
        hu = cv2.HuMoments(moments).flatten()
        hu_n = -np.sign(hu) * np.log10(np.abs(hu) + 1e-30)
        feats = np.array([area / (w * h + 1e-6), w / (h + 1e-6),
                          float(len(cnts)), *hu_n], dtype=np.float32)
        cls._REF_CACHE[label] = feats
        return feats


    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------
    def score(self, challenge: Challenge, attempt: Attempt) -> bool:
        """Must pick exactly the right set: no misses, no false positives."""
        want = set(challenge.truth["cells"])
        got = set(attempt.answer or [])
        return want == got


class TextFallback:
    """Colour-nearest-neighbour fallback, used only if OpenCV is missing."""
    _PALETTE = None

    @staticmethod
    def _classify_colour(tile: Image.Image) -> tuple[str, float]:
        arr = np.asarray(tile.convert("RGB"), dtype=np.float32).reshape(-1, 3)
        mx = arr.max(axis=1)
        mn = arr.min(axis=1)
        sat = (mx - mn) / (mx + 1e-6)
        weights = np.clip(sat - 0.12, 0, None) ** 2
        if weights.sum() < 1e-6:
            weights = np.ones(len(arr))
        mean = (arr * weights[:, None]).sum(axis=0) / weights.sum()
        dists = {label: float(np.linalg.norm(
            mean - np.array(spec["color"], dtype=np.float32)))
            for label, spec in CLASSES.items()}
        ordered = sorted(dists.items(), key=lambda kv: kv[1])
        margin = ((ordered[1][1] - ordered[0][1]) / (ordered[0][1] + 1e-6)
                  if ordered[0][1] > 1e-9 else 1.0)
        return ordered[0][0], margin

    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------
    def score(self, challenge: Challenge, attempt: Attempt) -> bool:
        """Must pick exactly the right set: no misses, no false positives."""
        want = set(challenge.truth["cells"])
        got = set(attempt.answer or [])
        return want == got
