"""CLIP-based semantic classifier — the modern attacker.

Everything else in this bench uses hand-rolled feature matching, which is
the wrong benchmark: a real attacker in 2026 does not hand-roll Hu moments
when a vision-language model is one `pip install` away.

This module wraps OpenCLIP ViT-B/32 (ONNX, fp16) so the attacker can answer
"does this image contain a bus?" the way a modern bot would: embed the image,
embed the text prompts, compare.

It lives in ``bot/`` because it is part of the **attacker**, not part of the
challenge generator: the lab may use it to label ground truth, but the bot
uses it to answer the challenge.

The model is **not vendored** — it is downloaded on first use into
``~/.cache/captcha-lab/models`` (~300 MB). The repo stays small, and the
bench degrades gracefully: without the model, the image/grid families fall
back to their classical classifiers.

Why it matters for the audit conclusion: the classical classifier on the
image-grid family scored ~92% per cell, which already broke the challenge.
CLIP does considerably better. The bench reports both so the numbers are
not accidentally flattering.
"""
from __future__ import annotations

import os
import urllib.request
from pathlib import Path

import numpy as np
from PIL import Image

MODEL_DIR = Path(os.path.expanduser("~/.cache/captcha-lab/models"))
HF_REPO = "Marqo/onnx-open_clip-ViT-B-32"
FILES = {
    "visual": "onnx16-open_clip-ViT-B-32-openai-visual.onnx",
    "textual": "onnx16-open_clip-ViT-B-32-openai-textual.onnx",
}

# CLIP's normalisation constants (OpenAI CLIP / OpenCLIP convention).
_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

# CLIP's input resolution.
_SIZE = 224

# The BPE vocabulary that OpenCLIP uses. Shipping the 1.3 MB vocab file in
# the repo would be silly; it is fetched once and cached.
_VOCAB_URL = ("https://raw.githubusercontent.com/openai/CLIP/main/"
              "clip/bpe_simple_vocab_16e6.txt.gz")

# The raw URL serves the ALREADY-decompressed text; GitHub does not gzip
# .gz paths. Saving it as .gz and then gunzipping was a double-decompress
# bug that produced "Not a gzipped file (b'"b')".

_SESSION = None


def available() -> bool:
    """True when the model files are already on disk."""
    return all((MODEL_DIR / f).exists() for f in FILES.values())


def download(progress: bool = False) -> bool:
    """Fetch the ONNX model files if they are missing. Returns success."""
    try:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        for fname in FILES.values():
            dest = MODEL_DIR / fname
            if dest.exists():
                continue
            url = "https://huggingface.co/{}/resolve/main/{}".format(
                HF_REPO, fname)
            if progress:
                print("  downloading", fname)
            tmp = dest.with_suffix(dest.suffix + ".part")
            urllib.request.urlretrieve(url, tmp)
            tmp.rename(dest)
        return True
    except Exception as exc:                                  # noqa: BLE001
        if progress:
            print("  download failed:", exc)
        return False


def _load_bpe_merges() -> dict:
    """Load CLIP's BPE merge table.

    The vocabulary file is NOT a `word|rank` list — it is the *merge table*:
    an ordered list of token pairs, each line meaning "merge these two
    tokens into one". The vocabulary is derived by applying the merges in
    order. Treating the file as a `word|rank` mapping (the obvious guess)
    produced garbage and a crash on the first line that had no `|`.

    Two sources exist and they are NOT interchangeable:
      * `merges.txt` from `openai/clip-vit-base-patch32` — the standard
        48,896-line format: `#version` header, then `a b` pairs. This is
        the one used here.
      * `bpe_simple_vocab_16e6.txt` from the OpenAI repo — 262,143 pairs,
        a different (larger) table used by the original CLIP release.
    Mixing them made the merge ranks meaningless.

    The merge ranks matter: BPE always applies the lowest-ranked pair
    first, which is what makes the encoding deterministic.
    """
    path = MODEL_DIR / "merges.txt"
    if not path.exists():
        urlretrieve_merges(path)
    lines = path.read_text(encoding="utf-8").split("\n")
    merges = {}
    # Skip the "#version" header.
    start = 0 if not lines or not lines[0].startswith("#") else 1
    for line in lines[start:]:
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 2:
            continue
        merges[(parts[0], parts[1])] = len(merges)
    return merges


def urlretrieve_merges(path: Path) -> None:
    """Fetch CLIP's merge table."""
    from huggingface_hub import hf_hub_download
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    src = hf_hub_download("openai/clip-vit-base-patch32", "merges.txt",
                          local_dir=str(MODEL_DIR))
    path.write_bytes(Path(src).read_bytes())


def _bytes_to_unicode():
    """CLIP's reversible byte-to-unicode mapping."""
    bs = (list(range(ord("!"), ord("~") + 1)) +
          list(range(ord("\u00a1"), ord("\u00ac") + 1)) +
          list(range(ord("\u00ae"), ord("\u00ff") + 1)))
    cs = bs[:]
    n = 0
    for b in range(2 ** 8):
        if b not in bs:
            bs.append(b)
            cs.append(2 ** 8 + n)
            n += 1
    return dict(zip(bs, [chr(c) for c in cs]))


_BPE_MERGES = None
_BYTE_ENCODER = None


def _bpe(word: str) -> str:
    """Apply the merge table to a single pre-tokenised word.

    The word is treated as a LIST OF TOKENS, not a string. That distinction
    is the whole ballgame: merging the pair ('b','u') turns the token list
    ['b','u','s'] into ['bu','s']. Doing the merge on the raw string
    instead produces 'bus' again — the same string — so the loop never
    terminates. The first implementation did exactly that and hung on every
    input.
    """
    global _BPE_MERGES, _BYTE_ENCODER
    if _BPE_MERGES is None:
        _BPE_MERGES = _load_bpe_merges()
        _BYTE_ENCODER = _bytes_to_unicode()

    tokens = [_BYTE_ENCODER[b] for b in word.encode("utf-8")]
    if len(tokens) < 2:
        return " ".join(tokens)

    while True:
        pairs = [(tokens[i], tokens[i + 1]) for i in range(len(tokens) - 1)]
        best_pair = None
        best_rank = None
        for p in pairs:
            rank = _BPE_MERGES.get(p)
            if rank is not None and (best_rank is None or rank < best_rank):
                best_rank, best_pair = rank, p
        if best_pair is None:
            break
        first, second = best_pair
        # Rebuild the token list, merging the chosen pair.
        new_tokens = []
        i = 0
        while i < len(tokens):
            if (i < len(tokens) - 1 and tokens[i] == first
                    and tokens[i + 1] == second):
                new_tokens.append(first + second)
                i += 2
            else:
                new_tokens.append(tokens[i])
                i += 1
        tokens = new_tokens
        if len(tokens) < 2:
            break
    return " ".join(tokens)


def _simple_tokenize(text: str) -> list[str]:
    """CLIP's whitespace/punctuation pre-tokenisation."""
    import re
    pattern = re.compile(
        r"<\|startoftext\|>|<\|endoftext\|>|'s|'t|'re|'ve|'m|'ll|'d|"
        r"[a-zA-Z]+|[0-9]|[^\sa-zA-Z0-9]+")
    return pattern.findall(text.lower())


_VOCAB = None


def _clip_vocab() -> dict:
    """CLIP's token -> id table.

    Built the way the original CLIP does it: the 256 byte tokens occupy
    ids 0..255 (ordered by the byte-encoder alphabet), then every merged
    token follows in alphabetical order. The two specials are pinned to
    49406 / 49407.
    """
    global _VOCAB
    if _VOCAB is not None:
        return _VOCAB
    merges = _load_bpe_merges()
    byte_encoder = _bytes_to_unicode()

    vocab = {}
    for i, ch in enumerate(sorted(byte_encoder.values())):
        vocab[ch] = i
    tokens = set()
    for (a, b) in merges:
        tokens.add(a + b)
    for tok in sorted(tokens):
        if tok not in vocab:
            vocab[tok] = len(vocab)
    vocab["<|startoftext|>"] = 49406
    vocab["<|endoftext|>"] = 49407
    _VOCAB = vocab
    return vocab


def _bpe_tokenize(text: str) -> list[int]:
    """Full CLIP tokenisation -> token ids (max 77)."""
    import ftfy
    import regex as regex_mod

    text = ftfy.fix_text(text)
    text = regex_mod.sub(r"\s+", " ", text).strip().lower()

    bpe_tokens = []
    for token in _simple_tokenize(text):
        bpe_tokens.extend(_bpe(token).split(" "))

    vocab = _clip_vocab()
    ids = [vocab["<|startoftext|>"]]
    for tok in bpe_tokens:
        ids.append(vocab.get(tok, vocab["<|endoftext|>"] - 1))
    ids.append(vocab["<|endoftext|>"])
    return ids[:77]


def _session():
    """Lazily create the ONNX inference session (visual + textual)."""
    global _SESSION
    if _SESSION is not None:
        return _SESSION
    if not available():
        return None
    try:
        import onnxruntime as ort
    except ImportError:
        return None
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 4
    vis = ort.InferenceSession(str(MODEL_DIR / FILES["visual"]),
                               providers=["CPUExecutionProvider"], sess_options=opts)
    txt = ort.InferenceSession(str(MODEL_DIR / FILES["textual"]),
                               providers=["CPUExecutionProvider"], sess_options=opts)
    _SESSION = (vis, txt)
    return _SESSION


class ClipClassifier:
    """Semantic classifier over a fixed label set."""

    def __init__(self, labels: list[str]):
        self.labels = labels
        self._text_emb = None

    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return _session() is not None

    def _embed_text(self) -> np.ndarray:
        if self._text_emb is not None:
            return self._text_emb
        sess = _session()
        if sess is None:
            return None
        _vis, txt = sess
        toks = [_bpe_tokenize("a photo of a {}".format(l))
                for l in self.labels]
        maxlen = 77
        ids = np.zeros((len(toks), maxlen), dtype=np.int64)
        for i, t in enumerate(toks):
            ids[i, :len(t)] = t
        # The OpenCLIP textual export takes only the token ids — no
        # attention mask. Feeding one anyway is an INVALID_ARGUMENT.
        feed = {txt.get_inputs()[0].name: ids}
        for extra in txt.get_inputs()[1:]:
            feed[extra.name] = np.zeros_like(ids)
        out = txt.run(None, feed)[0]
        emb = out[0] if isinstance(out, (list, tuple)) else out
        emb = np.asarray(emb, dtype=np.float32)
        emb = emb / (np.linalg.norm(emb, axis=-1, keepdims=True) + 1e-8)
        self._text_emb = emb
        return emb

    def _embed_image(self, img: Image.Image) -> np.ndarray:
        sess = _session()
        if sess is None:
            return None
        vis, _ = sess
        arr = np.asarray(img.convert("RGB").resize((_SIZE, _SIZE),
                                                   Image.BICUBIC),
                         dtype=np.float32) / 255.0
        arr = (arr - _MEAN) / _STD
        arr = arr.transpose(2, 0, 1)[None, ...].astype(np.float32)
        # The ONNX export is fp16 — the runtime rejects float32 input.
        name = vis.get_inputs()[0].name
        out = vis.run(None, {name: arr.astype(np.float16)})[0]
        emb = np.asarray(out, dtype=np.float32)
        emb = emb / (np.linalg.norm(emb, axis=-1, keepdims=True) + 1e-8)
        return emb

    # ------------------------------------------------------------------
    def classify(self, img: Image.Image) -> tuple[str, float]:
        """Return (label, confidence) for the best-matching label."""
        temb = self._embed_text()
        iemb = self._embed_image(img)
        if temb is None or iemb is None:
            return "", 0.0
        sims = (iemb @ temb.T)[0]
        idx = int(np.argmax(sims))
        # Confidence: softmax over the similarities (scaled like CLIP does).
        probs = np.exp((sims - sims.max()) * 100.0)
        probs = probs / probs.sum()
        return self.labels[idx], float(probs[idx])

    def classify_batch(self, imgs: list[Image.Image]) -> list[tuple[str, float]]:
        """Classify several crops with one model call."""
        temb = self._embed_text()
        if temb is None:
            return [("", 0.0)] * len(imgs)
        sess = _session()
        vis, _ = sess
        batch = []
        for img in imgs:
            arr = np.asarray(img.convert("RGB").resize((_SIZE, _SIZE),
                                                       Image.BICUBIC),
                             dtype=np.float32) / 255.0
            arr = (arr - _MEAN) / _STD
            batch.append(arr.transpose(2, 0, 1))
        x = np.stack(batch).astype(np.float16)
        name = vis.get_inputs()[0].name
        out = vis.run(None, {name: x})[0]
        emb = np.asarray(out, dtype=np.float32)
        emb = emb / (np.linalg.norm(emb, axis=-1, keepdims=True) + 1e-8)
        sims = emb @ temb.T
        results = []
        for row in sims:
            probs = np.exp((row - row.max()) * 100.0)
            probs = probs / probs.sum()
            i = int(np.argmax(row))
            results.append((self.labels[i], float(probs[i])))
        return results

    def score_labels(self, img: Image.Image) -> list[float]:
        """Full probability distribution over the label set.

        Needed for multi-select challenges: the attacker must decide
        whether EACH object matches the target, so it needs the target's
        own probability — not the argmax label. A scene of dogs must
        return every dog even when "cat" happens to outrank "dog" on one
        of them.
        """
        rows = self.score_labels_batch([img])
        return rows[0] if rows else [0.0] * len(self.labels)

    def score_labels_batch(self, imgs: list[Image.Image]) -> list[list[float]]:
        """Probability distributions for several images in one model call.

        This is the hot path for grid challenges: a 3x3 grid is nine
        tiles, and one batched forward pass is far cheaper than nine
        separate ones.
        """
        temb = self._embed_text()
        if temb is None or not imgs:
            return [[0.0] * len(self.labels) for _ in imgs]
        sess = _session()
        vis, _ = sess
        batch = []
        for img in imgs:
            arr = np.asarray(img.convert("RGB").resize((_SIZE, _SIZE),
                                                       Image.BICUBIC),
                             dtype=np.float32) / 255.0
            arr = (arr - _MEAN) / _STD
            batch.append(arr.transpose(2, 0, 1))
        x = np.stack(batch).astype(np.float16)
        name = vis.get_inputs()[0].name
        out = vis.run(None, {name: x})[0]
        emb = np.asarray(out, dtype=np.float32)
        emb = emb / (np.linalg.norm(emb, axis=-1, keepdims=True) + 1e-8)
        sims = emb @ temb.T
        rows = []
        for row in sims:
            probs = np.exp((row - row.max()) * 100.0)
            probs = probs / probs.sum()
            rows.append([float(v) for v in probs])
        return rows
