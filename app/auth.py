"""Session tokens — HUMAN vs AI-AGENT.

The lab needs to tell apart two kinds of solver:

* **human**    — a person dragging the piece in a browser.
* **ai_agent** — a machine (or an AI agent) driving the API: the CV solver,
                 the Playwright bot tester, or anything else that talks to
                 the endpoints directly.

Both are legitimate actors in the lab. The point is not to ban the AI one —
the point is to *measure* it and to be able to report which is which.

Token format
------------
``lab_<kind>.<payload_b64>.<sig_b64>``

* ``kind``            — ``human`` or ``ai_agent``, in cleartext so a human
                        can read it at a glance in the devtools.
* ``payload``         — base64url JSON: ``{"sid":…, "iat":…, "exp":…}``.
* ``sig``             — base64url HMAC-SHA256 over ``kind.payload`` using the
                        server secret.

The separator is a dot, not an underscore: the kind itself contains an
underscore (``ai_agent``), so splitting on ``_`` would cut the kind in
half -- and every token would silently fail to validate.

The signature makes the token unforgeable without the server secret, so an
agent cannot mint itself a ``human`` token. That is the whole point: if the
token were just a label, the distinction would be theatre.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass

# --- constants --------------------------------------------------------------

KIND_HUMAN = "human"
KIND_AGENT = "ai_agent"
VALID_KINDS = (KIND_HUMAN, KIND_AGENT)

TOKEN_TTL_S = 3600          # 1 hour — long enough for a lab session

# Canonical token layout:  lab_<kind>.<payload_b64>.<sig_b64>
#
# The kind segment is separated from the payload by a DOT, never by an
# underscore: "ai_agent" contains an underscore, so underscore-splitting
# would cut the kind in half and every token would silently fail to
# validate. The leading "lab_" prefix is cosmetic (it makes the token
# greppable in logs) and is NOT part of the signed message.
PREFIX = "lab_"
SEP = "."


def _secret() -> bytes:
    """The signing secret.

    Persisted next to the app so tokens survive a restart, and overridable
    through the environment for deployments.
    """
    env = os.environ.get("CAPTCHA_LAB_SECRET")
    if env:
        return env.encode()
    path = os.path.join(os.path.dirname(__file__), ".session_secret")
    try:
        with open(path, "rb") as fh:
            data = fh.read().strip()
        if data:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    try:
        with open(path, "wb") as fh:
            fh.write(data)
        os.chmod(path, 0o600)
    except OSError:
        pass
    return data


SECRET = _secret()


# --- helpers ----------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(kind: str, payload_b64: str) -> str:
    msg = f"{kind}.{payload_b64}".encode()
    return _b64e(hmac.new(SECRET, msg, hashlib.sha256).digest())


# --- public API -------------------------------------------------------------

@dataclass
class Session:
    kind: str
    sid: str
    issued_at: float
    expires_at: float

    @property
    def label(self) -> str:
        return "HUMAN" if self.kind == KIND_HUMAN else "AI AGENT"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "sid": self.sid,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }


def issue_session(kind: str = KIND_HUMAN, ttl_s: int = TOKEN_TTL_S) -> tuple[str, Session]:
    """Mint a signed session token for the given kind."""
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid session kind: {kind!r}")

    now = time.time()
    sess = Session(
        kind=kind,
        sid=secrets.token_hex(8),
        issued_at=now,
        expires_at=now + ttl_s,
    )
    payload = json.dumps(
        {"sid": sess.sid, "iat": int(now), "exp": int(sess.expires_at)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    payload_b64 = _b64e(payload)
    sig = _sign(kind, payload_b64)
    token = f"{PREFIX}{kind}{SEP}{payload_b64}{SEP}{sig}"
    return token, sess


def parse_token(token: str) -> Session | None:
    """Validate a token's signature and expiry. Returns None if invalid."""
    if not token or not token.startswith(PREFIX):
        return None
    body = token[len(PREFIX):]
    # The kind may contain underscores (ai_agent), so split on the DOT
    # separator and require exactly three segments.
    parts = body.split(SEP)
    if len(parts) != 3:
        return None
    kind, payload_b64, sig = parts
    if kind not in VALID_KINDS:
        return None
    # Base64url alphabet only — reject anything that could carry a second
    # separator or path traversal in a header.
    if not payload_b64 or not sig:
        return None
    if not all(c.isalnum() or c in "-_" for c in payload_b64 + sig):
        return None

    expected = _sign(kind, payload_b64)
    # constant-time compare: never leak the signature through timing
    if not hmac.compare_digest(expected, sig):
        return None

    try:
        payload = json.loads(_b64d(payload_b64))
    except (ValueError, TypeError):
        return None

    now = time.time()
    if float(payload.get("exp", 0)) < now:
        return None

    return Session(
        kind=kind,
        sid=str(payload.get("sid", "")),
        issued_at=float(payload.get("iat", now)),
        expires_at=float(payload.get("exp", now)),
    )


def token_from_header(header: str | None) -> str | None:
    """Extract the bearer token from an Authorization header."""
    if not header:
        return None
    header = header.strip()
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return header or None


def describe(token: str | None) -> dict:
    """Best-effort description of a token, for stats and responses."""
    sess = parse_token(token) if token else None
    if sess is None:
        return {"kind": "unknown", "label": "UNKNOWN", "sid": None}
    return sess.to_dict()
