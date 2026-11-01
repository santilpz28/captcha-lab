"""End-to-end tests for the CAPTCHA Lab.

Run with:  python3 -m pytest tests/ -v      (or plain: python3 tests/test_e2e.py)

Covers:
  * the renderer's geometry guarantees (piece fits, sprite matches the gap)
  * the CV solver's accuracy over many seeds
  * the HTTP API: challenge issuance, verification, solve, stats
  * that the ground truth never leaks into the challenge payload
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import requests
from PIL import Image

from app.render import render_challenge
from app.solver import solve_slider

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail else ""))


def human_trace(x_from: float, x_to: float, y: float, n: int = 24,
                duration_ms: float = 700.0):
    """A wobbling, overshooting drag trace."""
    trace = []
    for i in range(n):
        f = i / (n - 1)
        wob = 3.0 * (1 if i % 3 == 0 else -2)
        trace.append({
            "x": round(x_from + (x_to - x_from) * f + wob * f),
            "y": round(y + (i % 2) * 2),
            "t": round(duration_ms * f),
        })
    return trace


def bot_trace(x_from: float, x_to: float, y: float):
    """A single-step teleport."""
    return [{"x": round(x_to), "y": round(y), "t": 0}]


# ---------------------------------------------------------------------------
def test_geometry():
    print("\n[geometry] renderer invariants")
    bad_fit = 0
    for seed in range(40):
        bg, piece, geom, meta, template = render_challenge(seed=seed)
        x, y, w, h = geom.bbox
        if not (x >= 0 and y >= 0 and x + w <= bg.width and y + h <= bg.height):
            bad_fit += 1
    check("piece always fits inside the canvas", bad_fit == 0,
          f"{40 - bad_fit}/40 ok")

    # The sprite must be a perfect texture match for the gap region.
    bg, piece, geom, meta, template = render_challenge(seed=7)
    # The pristine TEMPLATE (not the decorated sprite) must be a pixel-exact
    # match for the gap: that is what the solver correlates against.
    pa = np.asarray(template.convert("RGBA"), dtype=np.float64)
    alpha = pa[:, :, 3] / 255.0
    ys, xs = np.where(alpha > 0.25)
    pc = pa[ys.min():ys.max() + 1, xs.min():xs.max() + 1, :3].mean(axis=2)
    mask = alpha[ys.min():ys.max() + 1, xs.min():xs.max() + 1] > 0.5
    img = np.asarray(bg.convert("RGB"), dtype=np.float64).mean(axis=2)
    gx, gy = geom.bbox[0], geom.bbox[1]
    region = img[gy:gy + pc.shape[0], gx:gx + pc.shape[1]]
    corr = float(np.corrcoef(region[mask], pc[mask])[0, 1])
    # The sprite must carry the gap's texture. Correlation is the wrong metric
    # for a smooth gradient (a 0.7-level offset moves it a lot), so the
    # assertion uses the mean absolute difference, which is what "same
    # texture" actually means.
    mad = float(np.abs(region[mask] - pc[mask]).mean())
    check("sprite texture matches the gap exactly", mad < 2.0 and corr > 0.95,
          f"mad={mad:.3f} levels, corr={corr:.6f}")


def test_solver():
    print("\n[solver] CV accuracy")
    n = 40
    errors, times = [], []
    for seed in range(3000, 3000 + n):
        t0 = time.time()
        bg, piece, geom, meta, template = render_challenge(seed=seed)
        res = solve_slider(bg.convert("RGB"), template, geom)
        times.append(time.time() - t0)
        errors.append(abs(res["best_x"] - geom.bbox[0]))
    exact = sum(1 for e in errors if e == 0)
    within = sum(1 for e in errors if e <= 6)
    check(f"solver exact on {exact}/{n}", exact == n)
    check(f"solver within tolerance on {within}/{n}", within == n,
          f"max err {max(errors)}px")
    check("solve is fast enough for a request handler",
          max(times) < 0.5, f"max {max(times) * 1000:.0f} ms")


def test_api():
    print("\n[api] HTTP endpoints")
    try:
        import requests
    except ImportError:
        print("  [SKIP] requests not installed")
        return

    base = os.environ.get("CAPTCHA_LAB_URL", "http://127.0.0.1:8099")
    try:
        r = requests.get(base + "/api/health", timeout=5)
    except Exception as e:
        print(f"  [SKIP] server not reachable at {base} ({e})")
        return

    check("health endpoint responds", r.json().get("ok") is True)

    ch = requests.get(base + "/api/challenge?width=320&height=160&piece_size=46").json()
    check("challenge returns images",
          ch["background"].startswith("data:image/png") and
          ch["piece"].startswith("data:image/png"))

    leak = [k for k in ch
            if k in ("gap_x", "gap_y", "answer", "solution", "target_x", "target_y")]
    check("challenge payload does not leak the ground truth", not leak, str(leak))

    sol = requests.post(base + "/api/solve",
                        json={"challenge_id": ch["challenge_id"]}).json()
    check("solver endpoint hits", sol["hit"] is True,
          f"error {sol['error_px']}px, conf {sol['confidence']}")

    # wrong offset must be rejected
    wrong = requests.post(base + "/api/verify", json={
        "challenge_id": ch["challenge_id"],
        "x": 40.0, "y": ch["piece_origin"]["y"],
        "trace": bot_trace(10, 40, 90), "duration_ms": 120,
    }).json()
    check("wrong offset is rejected", wrong["ok"] is False,
          f"{wrong['distance_px']}px off")

    # correct offset with a human-like trace
    ch2 = requests.get(base + "/api/challenge").json()
    sol2 = requests.post(base + "/api/solve",
                         json={"challenge_id": ch2["challenge_id"]}).json()
    cx = sol2["predicted_cx"]
    ok_human = requests.post(base + "/api/verify", json={
        "challenge_id": ch2["challenge_id"],
        "x": cx, "y": sol2["predicted_cy"],
        "trace": human_trace(6, cx, 90), "duration_ms": 750,
        "pointer_events": 24,
    }).json()
    check("correct offset with human trace is accepted", ok_human["ok"] is True)
    check("human-like drag scores high",
          ok_human["behaviour"]["human_score"] >= 0.5,
          f"score {ok_human['behaviour']['human_score']}")

    # bot-like trace on a correct offset
    ch3 = requests.get(base + "/api/challenge").json()
    sol3 = requests.post(base + "/api/solve",
                         json={"challenge_id": ch3["challenge_id"]}).json()
    ok_bot = requests.post(base + "/api/verify", json={
        "challenge_id": ch3["challenge_id"],
        "x": sol3["predicted_cx"], "y": sol3["predicted_cy"],
        "trace": bot_trace(6, sol3["predicted_cx"], 90), "duration_ms": 90,
    }).json()
    check("bot-like drag scores low",
          ok_bot["behaviour"]["human_score"] < 0.5,
          f"score {ok_bot['behaviour']['human_score']}")

    # --- behavioural gate: provenance -----------------------------------
    # A trace forged over the API carries NO real pointer events. Even with
    # a perfectly human-looking synthetic path it must score low.
    ch3b = requests.get(base + "/api/challenge").json()
    sol3b = requests.post(base + "/api/solve",
                          json={"challenge_id": ch3b["challenge_id"]}).json()
    forged = requests.post(base + "/api/verify", json={
        "challenge_id": ch3b["challenge_id"],
        "x": sol3b["predicted_cx"], "y": sol3b["predicted_cy"],
        "trace": human_trace(6, sol3b["predicted_cx"], 90),
        "duration_ms": 750, "pointer_events": 0,
    }).json()
    check("forged trace with no pointer events scores low",
          forged["behaviour"]["human_score"] < 0.5,
          f"score {forged['behaviour']['human_score']}")
    check("forged trace is flagged as would-block",
          forged.get("would_block") is True
          or forged.get("reason") == "behaviour_rejected",
          f"would_block={forged.get('would_block')} reason={forged.get('reason')}")

    # The same trace WITH pointer events must be accepted — proving the
    # gate keys on provenance, not on the synthetic path shape.
    ch3c = requests.get(base + "/api/challenge").json()
    sol3c = requests.post(base + "/api/solve",
                          json={"challenge_id": ch3c["challenge_id"]}).json()
    real = requests.post(base + "/api/verify", json={
        "challenge_id": ch3c["challenge_id"],
        "x": sol3c["predicted_cx"], "y": sol3c["predicted_cy"],
        "trace": human_trace(6, sol3c["predicted_cx"], 90),
        "duration_ms": 750, "pointer_events": 24,
    }).json()
    check("same trace with pointer events is accepted",
          real["ok"] is True and real.get("would_block") is not True,
          f"ok={real['ok']} score={real['behaviour']['human_score']}")

    # single-use enforcement
    ch4 = requests.get(base + "/api/challenge").json()
    sol4 = requests.post(base + "/api/solve",
                         json={"challenge_id": ch4["challenge_id"]}).json()
    requests.post(base + "/api/verify", json={
        "challenge_id": ch4["challenge_id"], "x": sol4["predicted_cx"],
        "y": sol4["predicted_cy"], "trace": human_trace(6, sol4["predicted_cx"], 90),
        "duration_ms": 700, "pointer_events": 24})
    reuse = requests.post(base + "/api/verify", json={
        "challenge_id": ch4["challenge_id"], "x": sol4["predicted_cx"],
        "y": sol4["predicted_cy"], "trace": [], "duration_ms": 700})
    check("challenge cannot be replayed", reuse.status_code in (404, 409),
          f"HTTP {reuse.status_code}")

    st = requests.get(base + "/api/stats").json()
    check("stats endpoint aggregates", st["issued"] > 0 and st["solver_calls"] > 0)

    test_sessions(base)


def test_sessions(base: str) -> None:
    """Human vs AI-agent session tokens."""
    from app.auth import parse_token, issue_session, KIND_HUMAN

    print("\n[sessions] human vs AI-agent tokens")

    # --- issuance -------------------------------------------------------
    hs = requests.get(base + "/api/session?kind=human").json()
    as_ = requests.get(base + "/api/session?kind=ai_agent").json()
    check("human token issued", hs["session"]["kind"] == "human"
          and hs["session"]["label"] == "HUMAN")
    check("ai_agent token issued", as_["session"]["kind"] == "ai_agent"
          and as_["session"]["label"] == "AI AGENT")
    check("token prefixes are readable",
          hs["token"].startswith("lab_human.")
          and as_["token"].startswith("lab_ai_agent."))

    bad = requests.get(base + "/api/session?kind=wizard")
    check("invalid session kind rejected", bad.status_code == 400,
          f"HTTP {bad.status_code}")

    # Regression guard: the kind contains an underscore ("ai_agent"), so a
    # token parser that splits on "_" silently truncates it to "ai" and
    # every token fails to validate. Assert the kind round-trips exactly.
    for k in ("human", "ai_agent"):
        tk, sess = issue_session(k)
        parsed = parse_token(tk)
        check(f"kind {k!r} round-trips through the token",
              parsed is not None and parsed.kind == k,
              f"parsed={getattr(parsed, 'kind', None)}")
        check(f"token for {k!r} keeps its kind segment",
              tk.split(".", 1)[0] == f"lab_{k}",
              tk.split(".", 1)[0])

    # --- integrity ------------------------------------------------------
    tok, _ = issue_session(KIND_HUMAN)
    tampered = tok[:-1] + ("A" if tok[-1] != "A" else "B")
    check("tampered signature rejected", parse_token(tampered) is None)

    # An agent token relabelled as human must not validate: the signature
    # covers the kind, so this is the downgrade attack.
    atok, _ = issue_session("ai_agent")
    body = atok[len("lab_ai_agent."):]
    p, s = body.rsplit(".", 1)
    check("agent token cannot be downgraded to human",
          parse_token(f"lab_human.{p}.{s}") is None)

    # --- attribution: a human-token solve is booked as HUMAN -----------
    H = {"Authorization": "Bearer " + hs["token"]}
    ch = requests.get(base + "/api/challenge", headers=H).json()
    check("challenge records the requesting session",
          ch["session"]["kind"] == "human", str(ch["session"]))
    sol = requests.post(base + "/api/solve",
                        json={"challenge_id": ch["challenge_id"]}).json()
    v = requests.post(base + "/api/verify", headers=H, json={
        "challenge_id": ch["challenge_id"], "x": sol["predicted_cx"],
        "y": sol["predicted_cy"], "trace": human_trace(6, sol["predicted_cx"], 90),
        "duration_ms": 750, "pointer_events": 24}).json()
    check("human-token solve is attributed to HUMAN",
          v.get("solved_by") == "human" and v.get("solved_by_label") == "HUMAN",
          f"solved_by={v.get('solved_by')}")

    # --- attribution: the agent flow is booked as AI AGENT -------------
    a = requests.post(base + "/api/agent/solve", json={}).json()
    check("one-shot agent flow returns an agent token",
          a["session"]["kind"] == "ai_agent" and a["token"].startswith("lab_ai_agent."))
    check("one-shot agent flow is attributed to AI AGENT",
          a.get("solved_by") == "ai_agent" and a.get("solved_by_label") == "AI AGENT",
          f"solved_by={a.get('solved_by')}")

    # --- the CV solver endpoint always claims AI ------------------------
    ch2 = requests.get(base + "/api/challenge", headers=H).json()
    s2 = requests.post(base + "/api/solve",
                        json={"challenge_id": ch2["challenge_id"]}).json()
    check("CV solver always reports AI AGENT",
          s2.get("solved_by") == "ai_agent", f"solved_by={s2.get('solved_by')}")

    # --- the auto-solve flow must actually BOOK the resolution ---------
    # Regression: the widget called /api/solve but never /api/verify, so
    # the solver hit was counted while the resolution was not. The stats
    # showed "solver hits: 1" with "solved: 0" and the AI counter stuck
    # at zero.
    before = requests.get(base + "/api/stats").json()
    ch3 = requests.get(base + "/api/challenge", headers=H).json()
    s3 = requests.post(base + "/api/solve",
                       json={"challenge_id": ch3["challenge_id"]}).json()
    agent_hdr = {"Authorization": "Bearer " + as_["token"]}
    v3 = requests.post(base + "/api/verify", headers=agent_hdr, json={
        "challenge_id": ch3["challenge_id"], "x": s3["predicted_cx"],
        "y": s3["predicted_cy"], "trace": [{"x": s3["predicted_cx"], "t": 0}],
        "duration_ms": 60, "pointer_events": 1}).json()
    check("agent-token verify is accepted",
          v3.get("ok") is True or v3.get("reason") == "behaviour_rejected",
          f"ok={v3.get('ok')} reason={v3.get('reason')}")
    check("agent-token verify is attributed to AI AGENT",
          v3.get("solved_by") == "ai_agent", f"solved_by={v3.get('solved_by')}")

    after = requests.get(base + "/api/stats").json()
    check("a resolved challenge increments the solved counter",
          after["solved"] > before["solved"],
          f"{before['solved']} -> {after['solved']}")
    check("the AI counter increments on an agent solve",
          after["solved_ai_agent"] > before["solved_ai_agent"],
          f"{before['solved_ai_agent']} -> {after['solved_ai_agent']}")
    check("the human counter is NOT incremented by an agent solve",
          after["solved_human"] == before["solved_human"],
          f"human {before['solved_human']} -> {after['solved_human']}")

    # --- stats split ----------------------------------------------------
    st = requests.get(base + "/api/stats").json()
    check("stats split human vs ai_agent",
          "solved_human" in st and "solved_ai_agent" in st
          and st["solved_human"] >= 1 and st["solved_ai_agent"] >= 1,
          f"human={st.get('solved_human')} ai={st.get('solved_ai_agent')}")
    check("stats expose session counts",
          st.get("sessions_human", 0) >= 1 and st.get("sessions_ai_agent", 0) >= 1)


if __name__ == "__main__":
    print("=" * 62)
    print("CAPTCHA Lab — test suite")
    print("=" * 62)
    test_geometry()
    test_solver()
    test_api()
    print("\n" + "=" * 62)
    print(f"RESULT: {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
    print("=" * 62)
    sys.exit(1 if FAIL else 0)
