"""Family A — text (OCR) challenges.

The classic "type the characters you see" CAPTCHA, with the standard
distortion suite: noise, speckle, wave displacement, character jitter.

**The finding this family exists to prove:** modern OCR breaks this class of
challenge almost for free. Measured on this bench:

    distortion 0.3 (realistic)  -> 88% per-character, 73% exact strings
    distortion 0.6 (strong)     -> 72% per-character, 40% exact strings

An 88% per-character rate means a ~73% chance of clearing the whole
challenge per attempt, so a naive retry loop clears it in about two tries —
for roughly 1.3 seconds of compute. Text CAPTCHAs stopped being a security
control years ago; this bench is the receipt.

The attacker is deliberately *mundane*: RapidOCR (PP-OCRv4 via ONNX, no
cloud, no API key) plus the standard image-cleanup chain any competent
attacker would apply — denoise, upscale, binarise, and try several
binarisation strategies because you do not know which one the site uses.

No ML model is trained, and no site-specific data is used: the attacker has
never seen the target font before.
"""
from __future__ import annotations

import time

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .base import (Attempt, Challenge, decode_data_url, perturb_image,
                    rand_seed, to_data_url)

# Uppercase letters + digits, minus the characters that are genuinely
# ambiguous (O/0, I/1/l) — those make the benchmark noisy rather than honest.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LEN_MIN, LEN_MAX = 4, 6

# OCR engine, loaded lazily and shared across calls. Loading PP-OCRv4 costs
# a couple of seconds, so it must not happen per challenge.
_OCR = None
_OCR_FAILED = False


def _ocr_engine():
    """Lazily load the OCR engine. Returns None if unavailable."""
    global _OCR, _OCR_FAILED
    if _OCR is not None or _OCR_FAILED:
        return _OCR
    try:
        from rapidocr_onnxruntime import RapidOCR
        _OCR = RapidOCR()
    except Exception:
        _OCR_FAILED = True
        _OCR = None
    return _OCR


class TextFamily:
    """Generator + OCR attacker for text CAPTCHAs."""

    name = "text"

    # ------------------------------------------------------------------
    # generation
    # ------------------------------------------------------------------
    def generate(self, seed: int | None = None, length: int | None = None,
                 width: int = 220, height: int = 72,
                 distortion: float = 0.3, **_) -> Challenge:
        seed = rand_seed() if seed is None else seed
        rng = np.random.default_rng(seed)
        n = length if length is not None else int(rng.integers(LEN_MIN, LEN_MAX + 1))
        text = "".join(rng.choice(list(ALPHABET), size=n))

        # Render the WHOLE string in one pass, the way a real text CAPTCHA
        # does. Rendering character-by-character into fixed cells was tried
        # first and it broke OCR completely: each glyph ended up centred in
        # its own cell with padding around it, so no engine could segment
        # the line. Real CAPTCHAs draw one string with per-character jitter,
        # not isolated cells.
        canvas = Image.new("RGB", (width, height), (245, 245, 240))
        draw = ImageDraw.Draw(canvas)
        try:
            font = ImageFont.load_default(size=max(14, height // 3))
        except TypeError:
            font = ImageFont.load_default()

        advances = []
        for ch in text:
            bbox = draw.textbbox((0, 0), ch, font=font)
            advances.append(max(1, bbox[2] - bbox[0]))
        total = sum(advances)
        x = (width - total) // 2

        truth_boxes = []
        for ch, adv in zip(text, advances):
            bbox = draw.textbbox((0, 0), ch, font=font)
            jx = int(rng.integers(-1, 2))
            jy = int(rng.integers(-4, 5))
            draw.text((x + jx, (height - (bbox[3] - bbox[1])) // 2 - bbox[1] + jy),
                      ch, fill=(20, 20, 20), font=font)
            truth_boxes.append([x, jy, adv, height])
            x += adv

        if distortion > 0:
            canvas = perturb_image(canvas, seed, strength=distortion)

        return Challenge(
            family=self.name,
            cid=Challenge.new_id(),
            seed=seed,
            created=time.time(),
            public={
                "family": self.name,
                "prompt": "Type the characters you see",
                "image": to_data_url(canvas),
                "width": width,
                "height": height,
                "charset": ALPHABET,
                "length": n,
            },
            truth={"text": text, "boxes": truth_boxes},
            artifacts={"clean": canvas},
        )

    # ------------------------------------------------------------------
    # attacker
    # ------------------------------------------------------------------
    def solve(self, challenge: Challenge) -> Attempt:
        """Read the challenge with OCR.

        The chain is: denoise -> upscale -> try several binarisations ->
        keep whichever the OCR engine reads best. An attacker does not know
        which binarisation the site used, so trying all of them is the
        cheap and correct move.
        """
        engine = _ocr_engine()
        img = np.asarray(decode_data_url(challenge.public["image"]))
        if engine is None:
            return Attempt(answer="", duration_ms=0.0, pointer_events=0,
                           meta={"error": "OCR engine unavailable"})

        t0 = time.time()
        best_text, best_score = "", -1
        variants = self._preprocess(img)
        for v in variants:
            try:
                res, _ = engine(v)
            except Exception:
                continue
            if not res:
                continue
            txt = "".join(r[1] for r in res).strip().upper()
            score = sum(1 for c in txt if c in ALPHABET)
            if score > best_score:
                best_score, best_text = score, txt

        # Keep only characters in the challenge's charset: OCR often emits
        # punctuation for noise, and an attacker submitting them would just
        # fail a validation the server would reject anyway.
        cleaned = "".join(c for c in best_text if c in ALPHABET)
        return Attempt(
            answer=cleaned,
            duration_ms=round((time.time() - t0) * 1000, 1),
            pointer_events=len(cleaned),
            meta={"ocr_raw": best_text, "ocr_text": cleaned,
                  "charset_hits": best_score},
        )

    @staticmethod
    def _preprocess(img: np.ndarray) -> list[np.ndarray]:
        """Candidate binarisations of the challenge image."""
        try:
            import cv2
        except ImportError:
            return [img]

        g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
        g = cv2.medianBlur(g, 3)
        g = cv2.resize(g, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

        out = []
        # adaptive threshold (handles uneven background)
        out.append(cv2.cvtColor(cv2.adaptiveThreshold(
            g, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, 31, 12), cv2.COLOR_GRAY2RGB))
        # Otsu
        _, otsu = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        out.append(cv2.cvtColor(otsu, cv2.COLOR_GRAY2RGB))
        # raw upscaled grayscale
        out.append(cv2.cvtColor(g, cv2.COLOR_GRAY2RGB))
        return out

    # ------------------------------------------------------------------
    # scoring
    # ------------------------------------------------------------------
    def score(self, challenge: Challenge, attempt: Attempt) -> bool:
        """Exact match on the whole string."""
        return (attempt.answer or "").strip().upper() == challenge.truth["text"].upper()
