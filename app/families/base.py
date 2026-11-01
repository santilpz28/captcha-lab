"""Shared abstractions for the multi-type CAPTCHA test bench.

A "challenge family" is one kind of CAPTCHA: slider, OCR text, image
selection, grid selection… Every family implements the same tiny contract so
the harness can treat them uniformly:

    generate()  -> Challenge      (payload for the browser, truth for the server)
    solve()     -> Attempt        (the attacker's answer)
    score()     -> bool           (did the attack succeed?)

The ground truth never leaves the server. `Challenge.truth` lives on the
server side only; `Challenge.public` is what gets serialised to the client.

This module is deliberately dependency-light: it must import without cv2 or
any OCR engine so the core test bench runs anywhere.
"""
from __future__ import annotations

import base64
import io
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
from PIL import Image


# --------------------------------------------------------------------------
# payload helpers
# --------------------------------------------------------------------------

def to_data_url(img: Image.Image, fmt: str = "PNG") -> str:
    """Serialise a PIL image as a data URL (keeps the widget to one request)."""
    buf = io.BytesIO()
    img.save(buf, format=fmt, optimize=True)
    return "data:image/{};base64,{}".format(
        fmt.lower(), base64.b64encode(buf.getvalue()).decode())


def decode_data_url(url: str) -> Image.Image:
    """Inverse of `to_data_url`."""
    raw = base64.b64decode(url.split(",", 1)[1])
    return Image.open(io.BytesIO(raw)).convert("RGB")


# --------------------------------------------------------------------------
# contracts
# --------------------------------------------------------------------------

@dataclass
class Attempt:
    """What an attacker submits for a challenge."""
    answer: Any
    # Behavioural signals the client would report. The solver fills these in
    # so the behavioural gate has something to score.
    trace: list[dict] = field(default_factory=list)
    duration_ms: float = 0.0
    pointer_events: int = 0
    # Free-form diagnostics (per-cell confidences, OCR text, …)
    meta: dict = field(default_factory=dict)


@dataclass
class Challenge:
    """A generated challenge.

    `truth` never leaves the server. `public` is the client-safe payload.
    """
    family: str
    cid: str
    seed: int
    created: float
    # Client-facing payload.
    public: dict
    # Server-side answer key.
    truth: Any
    # Reference images kept for scoring (e.g. the pristine tiles).
    artifacts: dict = field(default_factory=dict)

    @staticmethod
    def new_id() -> str:
        return secrets.token_urlsafe(12)

    @property
    def age_s(self) -> float:
        return time.time() - self.created


class Family(Protocol):
    """The contract every challenge family implements."""
    name: str

    def generate(self, seed: int, **kwargs) -> Challenge:
        ...

    def solve(self, challenge: Challenge) -> Attempt:
        """Run the attacker. Must not mutate the challenge."""
        ...

    def score(self, challenge: Challenge, attempt: Attempt) -> bool:
        """Did the attempt solve the challenge?"""
        ...


# --------------------------------------------------------------------------
# shared image utilities
# --------------------------------------------------------------------------

def rand_seed() -> int:
    return secrets.randbelow(2 ** 31)


def hsv_jitter(img: Image.Image, hue: float = 0.0, sat: float = 0.0,
               val: float = 0.0) -> Image.Image:
    """Shift hue/saturation/value of an RGB image."""
    arr = np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0
    hsv = np.asarray(Image.fromarray((arr * 255).astype(np.uint8)).convert("HSV"),
                     dtype=np.float32)
    hsv[..., 0] = (hsv[..., 0] + hue * 255) % 256
    hsv[..., 1] = np.clip(hsv[..., 1] * (1 + sat), 0, 255)
    hsv[..., 2] = np.clip(hsv[..., 2] * (1 + val), 0, 255)
    return Image.fromarray(np.asarray(hsv, dtype=np.uint8).astype(np.uint8),
                           "HSV").convert("RGB")


def perturb_image(img: Image.Image, seed: int, strength: float = 1.0) -> Image.Image:
    """Apply the classic image-CAPTCHA distortions: noise, blur, speckle."""
    rng = np.random.default_rng(seed)
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)

    if strength > 0:
        # gaussian noise
        noise = rng.normal(0, 14 * strength, arr.shape[:2])[..., None]
        arr = arr + noise

    if strength > 0.5:
        # salt & pepper speckle
        sp = rng.random(arr.shape[:2])
        arr[sp < 0.004 * strength] = 0
        arr[sp > 1 - 0.004 * strength] = 255

    out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))

    if strength > 0.5:
        from PIL import ImageFilter
        out = out.filter(ImageFilter.GaussianBlur(radius=0.4 * strength))

    # wave distortion: shift each row horizontally by a sine wave
    if strength > 0:
        amp = 3.0 * strength
        freq = 0.02 + 0.02 * rng.random()
        src = np.asarray(out, dtype=np.uint8)
        h, w = src.shape[:2]
        dst = np.zeros_like(src)
        for y in range(h):
            dx = int(amp * np.sin(freq * y + rng.random() * 6.28))
            dst[y] = np.roll(src[y], dx, axis=0)
        out = Image.fromarray(dst)

    return out


def cut_text_image(text: str, size: tuple[int, int], font_fill=(20, 20, 20),
                   bg=(245, 245, 240)) -> Image.Image:
    """Render text with PIL's built-in font at a large-ish size.

    Uses the default bitmap font scaled up, which keeps the lab dependency-free.
    """
    from PIL import ImageDraw, ImageFont
    w, h = size
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=max(14, h // 3))
    except TypeError:                       # older Pillow
        font = ImageFont.load_default()
    # measure and centre
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((w - tw) / 2 - bbox[0], (h - th) / 2 - bbox[1]), text,
              fill=font_fill, font=font)
    return img
