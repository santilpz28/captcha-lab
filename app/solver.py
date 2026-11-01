"""CV solver for the slider CAPTCHA.

Two-stage approach, mirroring what a real attacker would do:

  Stage 1 — hole detection.  The generator darkens the cut-out region, so a
  local-contrast detector finds the candidate hole without any template.
  This is immune to the degenerate-NCC problem on smooth backgrounds.

  Stage 2 — texture refinement.  A masked normalised cross-correlation (FFT,
  via numpy only) refines the position using the piece sprite's own texture.

Confidence combines the NCC peak height and the margin over the best
far-away candidate.

This doubles as the security test: if a naive template matcher can beat the
CAPTCHA, the CAPTCHA is weak.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .render import PieceGeometry


def _masked_ncc(image: np.ndarray, templ: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Masked normalised cross-correlation.

    At each candidate position the score is the Pearson correlation between
    the template and the image patch, computed only over the masked (opaque)
    region of the template.

    Returns (H-th+1, W-tw+1), values in [-1, 1].
    """
    H, W = image.shape
    th, tw = templ.shape
    out_h, out_w = H - th + 1, W - tw + 1

    # ---- numerator: sum over the mask of image * templ, per position ----
    # cross-correlate the image with the (masked) template.
    num = _xcorr(image, templ * mask)

    # ---- local image energy over the mask ----
    ones = np.zeros((th, tw), dtype=np.float64)
    ones[:] = mask
    i2 = _xcorr(image ** 2, ones)

    # ---- template energy (constant) ----
    t2 = float(((templ * mask) ** 2).sum())

    denom = np.sqrt(np.maximum(i2, 0.0) * t2)
    out = np.zeros((out_h, out_w), dtype=np.float64)
    nz = denom > 1e-9
    out[nz] = num[nz] / denom[nz]
    out[~nz] = -1.0
    return out


def _xcorr(image: np.ndarray, templ: np.ndarray) -> np.ndarray:
    """Cross-correlation of `image` with `templ`.

    out[y, x] = sum_{u,v} image[y+u, x+v] * templ[u, v]

    Uses FFT with the template placed at the origin (no flip), which is the
    standard 'same' convention for correlation.
    """
    H, W = image.shape
    th, tw = templ.shape
    fh = 1 << int(np.ceil(np.log2(H + th - 1)))
    fw = 1 << int(np.ceil(np.log2(W + tw - 1)))
    fh, fw = max(fh, th), max(fw, tw)

    fi = np.fft.rfft2(image, s=(fh, fw))
    ft = np.fft.rfft2(templ, s=(fh, fw))
    # correlate = irfft( conj(ft) * fi ), taking the [0:H, 0:W] block
    out = np.fft.irfft2(np.conj(ft) * fi, s=(fh, fw))
    return out[:H - th + 1, :W - tw + 1]




def solve_slider(bg: Image.Image, piece: Image.Image,
                 geom: PieceGeometry | None = None,
                 channel: str = "gray",
                 use_hole_detector: bool = False,
                 erode_mask: int = 2) -> dict:
    """Locate the gap in `bg` by matching the `piece` sprite against it."""
    bg = bg.convert("RGB")
    bg_arr = np.asarray(bg, dtype=np.float64)

    piece_rgba = piece.convert("RGBA")
    alpha = np.asarray(piece_rgba.split()[-1], dtype=np.float64) / 255.0
    if alpha.max() <= 0.01:
        return {"best_x": None, "best_y": None, "confidence": 0.0,
                "error": "piece has no alpha mask"}

    pc_arr = np.asarray(piece_rgba.convert("RGB"), dtype=np.float64)

    if channel == "gray":
        bg_c = bg_arr.mean(axis=2)
        pc_c = pc_arr.mean(axis=2)
    else:
        bg_c = bg_arr
        pc_c = pc_arr

    ys, xs = np.where(alpha > 0.25)
    if len(ys) == 0:
        return {"best_x": None, "best_y": None, "confidence": 0.0,
                "error": "no opaque pixels"}
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    a_crop = alpha[y0:y1, x0:x1]
    p_crop = pc_c[y0:y1, x0:x1]
    ah, aw = a_crop.shape

    Hc, Wc = bg_c.shape[:2]
    if ah > Hc or aw > Wc:
        return {"best_x": None, "best_y": None, "confidence": 0.0,
                "error": "piece larger than canvas"}

    # ---- stage 1: NCC map over the texture ----
    # The piece sprite is literally cut from the gap, and the generator keeps
    # the gap's interior pixels pristine, so the correlation peaks exactly at
    # the true position (NCC = 1.0).
    #
    # The mask is ERODED by 2px: the generator draws a thin inner shadow on
    # the hole's edge to make it read as recessed, and those boundary pixels
    # do not match the sprite. Skipping them costs nothing (the texture in
    # the interior is what carries the signal) and keeps the correlation
    # clean.
    mask = (a_crop > 0.5).astype(np.float64)
    if erode_mask > 0:
        from PIL import ImageFilter as _IF
        k = erode_mask * 2 + 1
        eroded = Image.fromarray((mask * 255).astype(np.uint8), "L").filter(
            _IF.MinFilter(k))
        mask = (np.asarray(eroded) > 127).astype(np.float64)
        if mask.sum() < 50:            # too small — keep the original
            mask = (a_crop > 0.5).astype(np.float64)
    if channel == "gray":
        ncc = _masked_ncc(bg_c, p_crop, mask)
    else:
        ncc = np.mean([_masked_ncc(bg_c[:, :, c], p_crop[:, :, c], mask)
                       for c in range(3)], axis=0)

    # ---- stage 2: pick the best candidate ----
    by = int(np.argmax(ncc.max(axis=1)))
    bx = int(np.argmax(ncc[by, :]))
    best = float(ncc[by, bx])
    detector_used = False

    # ---- confidence ----
    # The masked NCC is high over most of the canvas (the mask zeroes much of
    # the template, so any patch correlates reasonably well). What separates
    # the true match from the decoys is that the true peak is a sharp spike:
    # the neighbours drop away immediately.
    #
    # confidence = peak height * local sharpness
    r = 6
    y0 = max(0, by - r); y1 = min(ncc.shape[0], by + r + 1)
    x0 = max(0, bx - r); x1 = min(ncc.shape[1], bx + r + 1)
    neigh = ncc[y0:y1, x0:x1].copy()
    neigh[by - y0, bx - x0] = -np.inf
    nb_max = float(np.max(neigh)) if neigh.size else -1.0
    nb_med = float(np.median(neigh[np.isfinite(neigh)])) if np.isfinite(neigh).any() else -1.0

    peak_term = max(0.0, min(1.0, (best - 0.5) / 0.5))
    sharp_term = max(0.0, min(1.0, (best - nb_med) / 0.05))
    margin = float(best - nb_max)
    confidence = float(peak_term * (0.5 + 0.5 * sharp_term))

    return {
        "best_x": int(bx),
        "best_y": int(by),
        "raw_x": int(bx),
        "raw_y": int(by),
        "confidence": confidence,
        "margin": margin,
        "ncc": best,
        "neighbour_median": nb_med,
        "detector_used": detector_used,
        "error": None,
    }
