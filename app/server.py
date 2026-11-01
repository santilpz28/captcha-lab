"""FastAPI backend for the CAPTCHA Lab.

Endpoints
---------
GET  /api/challenge          -> issue a challenge (id, images, geometry)
POST /api/verify             -> verify a solve attempt (id, x, y)
POST /api/solve              -> server-side CV solve (the "attacker" view)
GET  /api/stats              -> lab statistics
GET  /                        -> the UI

Security notes
--------------
* The ground truth (gap position) NEVER leaves the server in the challenge
  payload. The client only gets the rendered images.
* Challenges are single-use and expire (TTL).
* Verification is constant-time-ish and records a behavioural trace
  (timing, path straightness) so the lab can score human vs bot.
"""
from __future__ import annotations

import io
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .render import render_challenge
from .solver import solve_slider
from .auth import (KIND_AGENT, KIND_HUMAN, describe as describe_token,
                   issue_session, parse_token, token_from_header)
from .tokens import (CANVAS_H, CANVAS_W, PIECE_SIZE, TRACK_TOLERANCE,
                     TRACK_TOLERANCE_Y, MIN_DRAG_DISTANCE)

STATIC_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="CAPTCHA Lab", version="1.0.0")

# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

CHALLENGE_TTL = 300          # seconds
MAX_CHALLENGES = 500

# Behavioural policy ------------------------------------------------------
# The human score is a 0..1 heuristic. A solve is REJECTED as bot-like when
# the score falls below `HUMAN_SCORE_MIN`. Set it to 0.0 to disable the
# behavioural gate entirely (useful to measure how many attacks get through
# position-wise, which is what the lab's bot tester reports).
#
# `LAB_MODE` keeps the gate armed for real traffic but records every
# would-be rejection, so the tester can report both "solved" and
# "would have been blocked".
HUMAN_SCORE_MIN = 0.50
LAB_MODE = True


@dataclass
class Challenge:
    id: str
    created: float
    seed: int
    width: int
    height: int
    piece_size: int
    gap_x: int
    gap_y: int
    piece_x: int
    piece_y: int
    # Who issued the challenge: the session that requested it.
    actor_kind: str = "unknown"
    actor_sid: str = ""
    used: bool = False
    attempts: int = 0
    solved: bool = False
    # behavioural trace, filled by the client
    trace: list = field(default_factory=list)


class ChallengeStore:
    def __init__(self, ttl: int = CHALLENGE_TTL, max_items: int = MAX_CHALLENGES):
        self.ttl = ttl
        self.max_items = max_items
        self._items: dict[str, Challenge] = {}
        self._order: deque[str] = deque()

    def _evict(self) -> None:
        now = time.time()
        expired = [k for k, c in self._items.items() if now - c.created > self.ttl]
        for k in expired:
            self._items.pop(k, None)
        while len(self._order) > self.max_items:
            old = self._order.popleft()
            self._items.pop(old, None)

    def add(self, ch: Challenge) -> None:
        self._evict()
        self._items[ch.id] = ch
        self._order.append(ch.id)

    def get(self, cid: str) -> Challenge | None:
        self._evict()
        ch = self._items.get(cid)
        if ch is None:
            return None
        if time.time() - ch.created > self.ttl:
            self._items.pop(cid, None)
            return None
        return ch

    def drop(self, cid: str) -> None:
        self._items.pop(cid, None)

    def __len__(self) -> int:
        return len(self._items)


STORE = ChallengeStore()

# lab statistics
STATS: dict = {
    "issued": 0,
    "solved": 0,
    "failed": 0,
    "expired": 0,
    "bot_solved": 0,
    "human_solved": 0,
    "solver_calls": 0,
    "solver_hits": 0,
    "by_source": defaultdict(int),
    "blocked": 0,
    "would_block": 0,
    # Split by WHO solved it, not just by how it was solved.
    "solved_human": 0,
    "solved_ai_agent": 0,
    "solved_unknown": 0,
    "sessions_human": 0,
    "sessions_ai_agent": 0,
    "by_actor": defaultdict(int),
}


# ---------------------------------------------------------------------------
# models
# ---------------------------------------------------------------------------

class VerifyIn(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=64)
    x: float = Field(..., ge=-200, le=2000)
    y: float = Field(..., ge=-200, le=2000)
    trace: list = Field(default_factory=list, max_length=400)
    duration_ms: float = Field(0, ge=0, le=600000)
    # Number of REAL pointer events observed in the browser. The server
    # trusts this field (it comes from the client) as a provenance signal:
    # a trace forged over the API reports 0.
    pointer_events: int = Field(0, ge=0, le=100000)


class SolveIn(BaseModel):
    challenge_id: str = Field(..., min_length=8, max_length=64)
    x_offset: int = Field(0, ge=-100, le=100)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _png_response(img, filename: str) -> Response:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-store", "X-Captcha-Image": filename},
    )


def _behaviour_score(trace: list, duration_ms: float, pointer_events: int = 0) -> dict:
    """Score the drag trace: humans wobble, bots teleport.

    Returns a dict with the individual signals and a 0..1 'human score'.
    """
    signals: dict = {}

    # 1. path length vs straight-line distance (wobble)
    straight = 0.0
    path = 0.0
    if len(trace) >= 2:
        pts = [(float(p.get("x", 0)), float(p.get("y", 0))) for p in trace]
        straight = abs(pts[-1][0] - pts[0][0])
        for a, b in zip(pts, pts[1:]):
            path += ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
    wobble = (path / straight) if straight > 1 else 1.0
    signals["wobble_ratio"] = round(wobble, 3)
    signals["path_len"] = round(path, 1)

    # 2. number of samples (bots send 1-2, humans dozens)
    signals["samples"] = len(trace)

    # 3. duration (too fast = bot)
    signals["duration_ms"] = round(duration_ms, 1)

    # 4. overshoot / backtracking
    back = 0
    if len(trace) >= 3:
        xs = [float(p.get("x", 0)) for p in trace]
        dirs = np_sign(xs)
        for a, b in zip(dirs, dirs[1:]):
            if a != 0 and b != 0 and a != b:
                back += 1
    signals["direction_changes"] = back

    # 5. provenance: the client reports how many REAL pointer events the
    #    drag produced. A trace forged over the API carries the client's own
    #    count (0), which is what separates `no_mouse` from a human.
    signals["pointer_events"] = int(pointer_events)

    # human score: penalise too-fast, too-straight, too-few-samples
    s = 1.0
    if len(trace) < 5:
        s -= 0.45
    if duration_ms < 250:
        s -= 0.35
    elif duration_ms < 600:
        s -= 0.15
    if wobble < 1.02 and straight > 20:
        s -= 0.30
    if back == 0 and straight > 20:
        s -= 0.15
    # A drag with no real pointer events behind it is machine-generated,
    # regardless of how human the synthetic trace looks. This is a hard
    # signal, so it takes the score to the floor rather than a mild cut.
    if pointer_events <= 0:
        s = min(s, 0.20)
    elif pointer_events < 5:
        s -= 0.25
    s = max(0.0, min(1.0, s))
    signals["human_score"] = round(s, 3)
    return signals


def np_sign(xs: list[float]) -> list[int]:
    out = []
    for a, b in zip(xs, xs[1:]):
        d = b - a
        out.append(0 if abs(d) < 0.5 else (1 if d > 0 else -1))
    return out


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/session")
def new_session(kind: str = KIND_HUMAN) -> JSONResponse:
    """Mint a session token.

    Two kinds, and the difference matters:

    * ``human``    — a person using the widget in a browser.
    * ``ai_agent`` — a machine driving the API: the CV solver, the Playwright
      bot tester, or any other agent.

    The token is HMAC-signed, so an agent cannot mint itself a ``human``
    token. That is what makes the human/AI split in the stats real rather
    than decorative.
    """
    if kind not in (KIND_HUMAN, KIND_AGENT):
        raise HTTPException(400, f"kind must be one of: {KIND_HUMAN}, {KIND_AGENT}")
    token, sess = issue_session(kind)
    if kind == KIND_HUMAN:
        STATS["sessions_human"] += 1
    else:
        STATS["sessions_ai_agent"] += 1
    return JSONResponse({
        "token": token,
        "session": sess.to_dict(),
        "expires_in": int(sess.expires_at - sess.issued_at),
    })


@app.get("/api/challenge")
def new_challenge(request: Request, width: int = CANVAS_W, height: int = CANVAS_H,
                  piece_size: int = PIECE_SIZE) -> JSONResponse:
    if not (160 <= width <= 640) or not (100 <= height <= 400):
        raise HTTPException(400, "width/height out of range")
    if not (24 <= piece_size <= 90):
        raise HTTPException(400, "piece_size out of range")

    seed = secrets.randbelow(2 ** 31)
    bg, piece, geom, meta, _template = render_challenge(
        seed=seed, width=width, height=height, piece_size=piece_size)

    cid = secrets.token_urlsafe(12)
    # The session that requested the challenge is recorded on it, so every
    # later attempt can be attributed to a human or to an AI agent.
    token = token_from_header(request.headers.get("authorization"))
    actor = parse_token(token) if token else None
    actor_kind = actor.kind if actor else "unknown"
    actor_sid = actor.sid if actor else ""
    # gap_x/gap_y are the SHAPE's opaque-box centre — the same reference
    # point the client reports and the solver returns.
    ch = Challenge(
        id=cid, created=time.time(), seed=seed,
        width=width, height=height, piece_size=piece_size,
        gap_x=int(geom.bbox[0] + geom.bbox[2] / 2),
        gap_y=int(geom.bbox[1] + geom.bbox[3] / 2),
        piece_x=geom.piece_x, piece_y=geom.piece_y,
        actor_kind=actor_kind, actor_sid=actor_sid,
    )
    STORE.add(ch)
    STATS["issued"] += 1

    # The images are served as data URLs so the widget is a single request.
    import base64

    def b64(img, fmt="PNG") -> str:
        b = io.BytesIO()
        img.save(b, format=fmt, optimize=True)
        return "data:image/png;base64," + base64.b64encode(b.getvalue()).decode()

    return JSONResponse({
        "challenge_id": cid,
        "background": b64(bg.convert("RGB")),
        "piece": b64(piece),
        "width": width,
        "height": height,
        "piece_size": piece_size,
        "piece_origin": {"x": geom.piece_x, "y": geom.piece_y},
        # The shape's opaque box in its REST position and the travel needed
        # to reach the gap — both in canvas px.
        "piece_rest": {"x": geom.piece_opaque_x, "y": geom.piece_opaque_y},
        "travel": {"x": geom.travel_x, "y": geom.travel_y},
        # The shape's centre — the reference point used for verification.
        "shape_centre": {
            "x": int(geom.bbox[0] + geom.bbox[2] / 2),
            "y": int(geom.bbox[1] + geom.bbox[3] / 2),
        },
        # Sprite geometry: the PNG is larger than the visible shape (knob +
        # pad). The client uses this to render the piece at exactly the same
        # scale as the background, so the shape lands on the gap.
        "sprite": {
            "w": meta["sprite_w"],
            "h": meta["sprite_h"],
            # opaque box position INSIDE the sprite PNG
            "opaque_x": meta["sprite_opaque_x"],
            "opaque_y": meta["sprite_opaque_y"],
            "opaque_w": meta["sprite_opaque_w"],
            "opaque_h": meta["sprite_opaque_h"],
            # where the sprite PNG's origin sits on the canvas, in canvas px
            "origin_x": meta["sprite_origin_x"],
            "origin_y": meta["sprite_origin_y"],
        },
        "expires_in": CHALLENGE_TTL,
        # Who is solving this challenge. The client echoes the token back
        # on /api/verify so the server can attribute the solve.
        "session": {
            "kind": actor_kind,
            "label": "HUMAN" if actor_kind == KIND_HUMAN else
                     ("AI AGENT" if actor_kind == KIND_AGENT else "UNKNOWN"),
            "sid": actor_sid,
        },
        # NOTE: the true gap position is deliberately NOT included.
    })


@app.post("/api/verify")
def verify(body: VerifyIn, request: Request) -> JSONResponse:
    ch = STORE.get(body.challenge_id)
    if ch is None:
        STATS["expired"] += 1
        raise HTTPException(404, "challenge not found or expired")
    if ch.used:
        raise HTTPException(409, "challenge already used")

    ch.attempts += 1
    behaviour = _behaviour_score(body.trace, body.duration_ms, body.pointer_events)

    # WHO is solving. The token is authoritative: it is HMAC-signed, so an
    # agent cannot claim to be human. The challenge's own actor (the
    # session that requested it) is the fallback when no token is sent.
    token = token_from_header(request.headers.get("authorization"))
    actor = parse_token(token) if token else None
    if actor is not None:
        solved_by = actor.kind
        solved_sid = actor.sid
    else:
        solved_by = ch.actor_kind
        solved_sid = ch.actor_sid
    if solved_by not in (KIND_HUMAN, KIND_AGENT):
        solved_by = "unknown"

    # The client reports the shape's CENTRE (the centre of the sprite's
    # opaque box) in canvas pixels. The server stores that same reference
    # point, so the comparison is apples-to-apples.
    dx = abs(body.x - ch.gap_x)
    dy = abs(body.y - ch.gap_y)

    if dx <= TRACK_TOLERANCE and dy <= TRACK_TOLERANCE_Y:
        # Position is correct. Now apply the behavioural gate.
        bot_like = behaviour["human_score"] < HUMAN_SCORE_MIN
        if bot_like:
            STATS["would_block"] += 1
            if not LAB_MODE:
                STATS["blocked"] += 1
                STATS["failed"] += 1
                ch.used = True
                STORE.drop(ch.id)
                return JSONResponse({
                    "ok": False,
                    "distance_px": round(dx, 2),
                    "tolerance_px": TRACK_TOLERANCE,
                    "behaviour": behaviour,
                    "reason": "behaviour_rejected",
                    "solved_by": solved_by,
                    "solved_by_label": ("HUMAN" if solved_by == KIND_HUMAN else
                                        ("AI AGENT" if solved_by == KIND_AGENT else "UNKNOWN")),
                    "message": "Rejected: drag behaviour looks automated "
                               "(human score "
                               f"{behaviour['human_score']:.2f} < {HUMAN_SCORE_MIN:.2f})",
                })
        ch.used = True
        ch.solved = True
        STORE.drop(ch.id)
        STATS["solved"] += 1
        if bot_like:
            STATS["bot_solved"] += 1
            STATS["by_source"]["bot"] += 1
        else:
            STATS["human_solved"] += 1
            STATS["by_source"]["human"] += 1
        # Attribute the solve to WHO did it.
        if solved_by == KIND_HUMAN:
            STATS["solved_human"] += 1
        elif solved_by == KIND_AGENT:
            STATS["solved_ai_agent"] += 1
        else:
            STATS["solved_unknown"] += 1
        STATS["by_actor"][solved_by] += 1
        return JSONResponse({
            "ok": True,
            "distance_px": round(dx, 2),
            "behaviour": behaviour,
            "would_block": bot_like,
            "solved_by": solved_by,
            "solved_by_label": ("HUMAN" if solved_by == KIND_HUMAN else
                                ("AI AGENT" if solved_by == KIND_AGENT else "UNKNOWN")),
            "session_sid": solved_sid,
            "message": ("Solved (LAB: behaviour gate bypassed)" if bot_like
                        else "Solved"),
        })

    STATS["failed"] += 1
    return JSONResponse({
        "ok": False,
        "distance_px": round(dx, 2),
        "tolerance_px": TRACK_TOLERANCE,
        "behaviour": behaviour,
        "solved_by": solved_by,
        "solved_by_label": ("HUMAN" if solved_by == KIND_HUMAN else
                            ("AI AGENT" if solved_by == KIND_AGENT else "UNKNOWN")),
        "message": "Not in the slot",
    })


@app.post("/api/solve")
def solve(body: SolveIn) -> JSONResponse:
    """Server-side CV solve — simulates an attacker with template matching.

    Whatever solves through this endpoint is an **AI AGENT** by definition:
    the pixels were matched by a machine, not by a finger. The response
    says so explicitly, and the challenge is attributed to the agent
    session.
    """
    ch = STORE.get(body.challenge_id)
    if ch is None:
        raise HTTPException(404, "challenge not found or expired")

    STATS["solver_calls"] += 1
    bg, piece, geom, meta, template = render_challenge(
        seed=ch.seed, width=ch.width, height=ch.height, piece_size=ch.piece_size)
    # The solver correlates the PRISTINE template against the background.
    # The sprite sent to the browser carries a keyline and a drop shadow for
    # looks; correlating those would only add noise.
    res = solve_slider(bg.convert("RGB"), template, geom)
    if res.get("best_x") is None:
        raise HTTPException(500, res.get("error") or "solver failed")

    hit = abs(res["best_x"] - geom.bbox[0]) <= TRACK_TOLERANCE
    if hit:
        STATS["solver_hits"] += 1

    # `predicted_x` is the cut-out's LEFT edge (the solver's convention);
    # `predicted_cx` is the SHAPE's centre (the opaque box centre), which is
    # the same reference point the verify endpoint expects.
    gap_cx = int(geom.bbox[0] + geom.bbox[2] / 2)
    gap_cy = int(geom.bbox[1] + geom.bbox[3] / 2)

    return JSONResponse({
        "ok": True,
        "hit": hit,
        "predicted_x": res["best_x"],
        "predicted_y": res["best_y"],
        "predicted_cx": gap_cx,
        "predicted_cy": gap_cy,
        "confidence": round(res["confidence"], 4),
        "ncc": round(res["ncc"], 4),
        "error_px": abs(res["best_x"] - geom.bbox[0]),
        "tolerance_px": TRACK_TOLERANCE,
        # Unambiguous: this was solved by a machine.
        "solved_by": KIND_AGENT,
        "solved_by_label": "AI AGENT",
        "message": "Solver hit" if hit else "Solver missed",
    })


@app.post("/api/agent/solve")
def agent_solve(width: int = CANVAS_W, height: int = CANVAS_H,
                 piece_size: int = PIECE_SIZE) -> JSONResponse:
    """One-shot agent flow: mint an agent token, take a challenge, solve it.

    This is the endpoint an AI agent calls. It exists so the human/AI
    distinction is enforced by the plumbing rather than by convention:
    the token is signed ``ai_agent``, and the resulting solve is booked
    under ``solved_ai_agent`` in the stats.
    """
    token, sess = issue_session(KIND_AGENT)
    STATS["sessions_ai_agent"] += 1

    seed = secrets.randbelow(2 ** 31)
    bg, piece, geom, meta, template = render_challenge(
        seed=seed, width=width, height=height, piece_size=piece_size)
    cid = secrets.token_urlsafe(12)
    ch = Challenge(
        id=cid, created=time.time(), seed=seed,
        width=width, height=height, piece_size=piece_size,
        gap_x=int(geom.bbox[0] + geom.bbox[2] / 2),
        gap_y=int(geom.bbox[1] + geom.bbox[3] / 2),
        piece_x=geom.piece_x, piece_y=geom.piece_y,
        actor_kind=KIND_AGENT, actor_sid=sess.sid,
    )
    STORE.add(ch)
    STATS["issued"] += 1

    STATS["solver_calls"] += 1
    res = solve_slider(bg.convert("RGB"), template, geom)
    if res.get("best_x") is None:
        raise HTTPException(500, res.get("error") or "solver failed")

    hit = abs(res["best_x"] - geom.bbox[0]) <= TRACK_TOLERANCE
    if hit:
        STATS["solver_hits"] += 1
        ch.used = True
        ch.solved = True
        STORE.drop(ch.id)
        STATS["solved"] += 1
        STATS["solved_ai_agent"] += 1
        STATS["by_actor"][KIND_AGENT] += 1

    gap_cx = int(geom.bbox[0] + geom.bbox[2] / 2)
    gap_cy = int(geom.bbox[1] + geom.bbox[3] / 2)
    return JSONResponse({
        "ok": True,
        "hit": hit,
        "token": token,
        "session": sess.to_dict(),
        "challenge_id": cid,
        "predicted_x": res["best_x"],
        "predicted_y": res["best_y"],
        "predicted_cx": gap_cx,
        "predicted_cy": gap_cy,
        "confidence": round(res["confidence"], 4),
        "ncc": round(res["ncc"], 4),
        "error_px": abs(res["best_x"] - geom.bbox[0]),
        "tolerance_px": TRACK_TOLERANCE,
        "solved_by": KIND_AGENT,
        "solved_by_label": "AI AGENT",
        "message": "Solver hit" if hit else "Solver missed",
    })


@app.get("/api/stats")
def stats() -> JSONResponse:
    total = STATS["solver_calls"]
    return JSONResponse({
        "issued": STATS["issued"],
        "solved": STATS["solved"],
        "failed": STATS["failed"],
        "expired": STATS["expired"],
        "human_solved": STATS["human_solved"],
        "bot_solved": STATS["bot_solved"],
        # WHO solved it
        "solved_human": STATS["solved_human"],
        "solved_ai_agent": STATS["solved_ai_agent"],
        "solved_unknown": STATS["solved_unknown"],
        "by_actor": dict(STATS["by_actor"]),
        "sessions_human": STATS["sessions_human"],
        "sessions_ai_agent": STATS["sessions_ai_agent"],
        "blocked": STATS["blocked"],
        "would_block": STATS["would_block"],
        "human_score_min": HUMAN_SCORE_MIN,
        "lab_mode": LAB_MODE,
        "solver_calls": total,
        "solver_hits": STATS["solver_hits"],
        "solver_hit_rate": round(STATS["solver_hits"] / total, 4) if total else None,
        "active_challenges": len(STORE),
        "tolerance_px": TRACK_TOLERANCE,
        "ttl_s": CHALLENGE_TTL,
    })


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "service": "captcha-lab", "version": "1.0.0"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
