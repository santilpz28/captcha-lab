"""Playwright bot-attack tester for the CAPTCHA Lab.

Simulates realistic bot behaviours against the running UI and reports whether
each one defeats the challenge. This is the security-testing half of the lab:
if a strategy wins, the CAPTCHA is not doing its job.

Strategies
----------
teleport      one mouse jump straight to the target (naive CV bot)
linear        constant-velocity drag
humanish      eased drag with overshoot and a pause before release
no_mouse      calls the verify endpoint directly with a synthetic trace
api_only      asks /api/solve for the answer and never touches the UI

Usage:
    python3 tests/test_bot.py --url http://127.0.0.1:8099 --runs 3
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field, asdict

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("playwright missing: pip install playwright && python3 -m playwright install chromium")


@dataclass
class AttackResult:
    strategy: str
    solved: bool
    duration_ms: float
    human_score: float | None = None
    error_px: float | None = None
    note: str = ""
    # Who the SERVER attributed this solve to (human / ai_agent / unknown).
    solved_by: str | None = None
    # True when the position was correct but the behavioural gate would
    # have rejected the attempt in production mode.
    would_block: bool = False
    blocked: bool = False

    def to_dict(self):
        return asdict(self)

    @property
    def effective_success(self) -> bool:
        """Did the attack actually defeat the CAPTCHA?

        In production mode the behavioural gate is armed, so a solve that
        would have been blocked does NOT count as a win. In lab mode the
        gate is bypassed, and we report both numbers.
        """
        return self.solved and not self.blocked


def _ease(t: float) -> float:
    """Ease-out cubic — fast at first, slowing to a stop."""
    return 1 - (1 - t) ** 3


def _humanish_path(x0: float, x1: float, steps: int = 26):
    """Points along a drag that starts fast, overshoots slightly, comes back."""
    overshoot = random.uniform(2.0, 7.0)
    pts = []
    for i in range(steps):
        t = i / (steps - 1)
        e = _ease(t)
        # overshoot in the last 25%
        if t > 0.75:
            over = overshoot * math.sin((t - 0.75) / 0.25 * math.pi)
        else:
            over = 0.0
        pts.append((x0 + (x1 - x0 + over) * e, t))
    return pts


class Attacker:
    def __init__(self, url: str, seed: int = 0):
        self.url = url
        self.rng = random.Random(seed)
        # The tester IS an AI agent — it must present an agent token, not a
        # human one. The server would (correctly) treat a human token here
        # as a lie.
        self.token = ""

    def agent_token(self, page=None) -> str:
        """Mint an AI-agent session token.

        Fetched over plain HTTP rather than from the page: a relative
        `fetch('/api/session')` only resolves once the page has a base URL,
        and the token is needed before the first navigation.
        """
        if not self.token:
            import urllib.request
            with urllib.request.urlopen(
                self.url.rstrip("/") + "/api/session?kind=ai_agent", timeout=10
            ) as resp:
                self.token = json.loads(resp.read().decode())["token"]
        return self.token

    # ---------------------------------------------------------------- setup
    def prepare(self, page) -> None:
        page.goto(self.url, wait_until="networkidle")
        page.wait_for_function(
            "() => document.getElementById('challenge-id').textContent.includes('id')",
            timeout=15000,
        )
        self.agent_token(page)

    def _inject_agent_token(self, page) -> None:
        """Make the widget use the agent token instead of a human one.

        The widget mints its own human session on load. A bot driving that
        widget must present the agent token, otherwise the server books a
        machine-driven solve as a human — and the whole human/AI split
        becomes theatre.

        The token has to be injected BEFORE the page's own script runs, so
        the widget picks it up on its first `openSession()` call. We add an
        init script and reload.
        """
        token = self.agent_token(page)
        # add_init_script takes no arguments in this Playwright version, so
        # the token is interpolated into the script source.
        page.add_init_script(
            f"window.__lab_agent_token = {json.dumps(token)};")
        page.reload(wait_until="networkidle")
        page.wait_for_function(
            "() => document.getElementById('challenge-id').textContent.includes('id')",
            timeout=15000,
        )
        page.evaluate(
            """() => {
                // Belt and braces: also override fetch so every API call
                // carries the agent token even if the widget re-mints.
                const orig = window.fetch;
                window.fetch = function (url, opts) {
                    opts = opts || {};
                    opts.headers = opts.headers || {};
                    if (opts.headers['Authorization'] === undefined) {
                        opts.headers['Authorization'] =
                            'Bearer ' + window.__lab_agent_token;
                    }
                    return orig.call(window, url, opts);
                };
            }""")

    def _target(self, page) -> tuple[float, float, float]:
        """Ask the server where the gap is (the attacker's oracle)."""
        return page.evaluate(
            """async () => {
                const id = window.__lab_challenge_id;
                const r = await fetch('/api/solve', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({challenge_id:id})
                });
                return await r.json();
            }""")

    # ------------------------------------------------------------ strategies
    def _drag(self, page, strategy: str, target_left: float,
              oracle: dict | None = None) -> tuple[float, float]:
        """Perform a drag on the track. Returns (duration_ms, final_x)."""
        track = page.locator("#track")
        slider = page.locator("#slider")
        tb = track.bounding_box()
        sb = slider.bounding_box()
        if not tb or not sb:
            return 0.0, 0.0

        # The piece SPRITE floats over the canvas; the TRACK HANDLE is what
        # the user actually grabs. The handle's travel maps 1:1 to the piece's
        # offset from its rest position.
        piece = oracle["piece"]
        scale = page.evaluate(
            """() => {
                const bg = document.getElementById('bg-img');
                const ch = window.__lab_challenge;
                return bg.getBoundingClientRect().width / ch.width;
            }""")
        sprite = page.evaluate(
            """() => {
                const ch = window.__lab_challenge;
                const r = document.getElementById('piece-img').getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height, sp: ch.sprite,
                        centre: ch.shape_centre};
            }""")
        handle = page.evaluate(
            """() => {
                const r = document.getElementById('slider').getBoundingClientRect();
                return {x: r.x, y: r.y, w: r.width, h: r.height};
            }""")

        start_x = handle["x"] + handle["w"] / 2
        y = handle["y"] + handle["h"] / 2

        # Travel (canvas px) from the piece's rest position to the gap.
        # The handle must travel exactly this far.
        # The travel the handle must make is published by the server.
        target_offset = page.evaluate(
            """() => window.__lab_challenge.travel.x""")
        end_x = start_x + target_offset * scale
        self._target_offset = target_offset

        t0 = time.time()

        if strategy == "teleport":
            # A teleport is a single huge jump WHILE dragging. The pointer
            # must be down before the jump, otherwise it is a click and the
            # client never records a drag.
            page.mouse.move(start_x, y)
            page.mouse.down()
            page.mouse.move(end_x, y)
            page.mouse.up()

        elif strategy == "linear":
            page.mouse.move(start_x, y)
            page.mouse.down()
            steps = 22
            for i in range(1, steps + 1):
                x = start_x + (end_x - start_x) * (i / steps)
                page.mouse.move(x, y)
                page.wait_for_timeout(8)
            page.mouse.up()

        elif strategy == "humanish":
            page.mouse.move(start_x, y)
            page.mouse.down()
            for (x, t) in _humanish_path(start_x, end_x):
                page.mouse.move(x, y)
                page.wait_for_timeout(random.uniform(8, 26))
            page.wait_for_timeout(random.uniform(80, 260))   # "thinking" pause
            page.mouse.up()
        else:
            raise ValueError(strategy)

        return (time.time() - t0) * 1000.0, end_x - start_x

    # --------------------------------------------------------------- runner
    def run(self, strategy: str, runs: int = 1) -> list[AttackResult]:
        out: list[AttackResult] = []
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            # capture the verify response
            last_resp = {}
            def on_response(resp):
                if resp.url.endswith("/api/verify"):
                    try:
                        last_resp.clear()
                        last_resp.update(resp.json())
                    except Exception:
                        pass
            page.on("response", on_response)

            for _ in range(runs):
                # Navigate first, then swap the widget's human session for
                # the agent token (which requires a reload), so the server
                # attributes the solve to AI rather than to a human.
                self.prepare(page)
                self._inject_agent_token(page)
                page.click("#btn-new")
                page.wait_for_timeout(700)

                if strategy == "api_only":
                    r = self._api_only(page)
                    out.append(r)
                    continue

                if strategy == "no_mouse":
                    r = self._no_mouse(page)
                    out.append(r)
                    continue

                # oracle: the bot's CV knows the answer. It asks the
                # server-side solver, exactly like a real attacker would.
                oracle = page.evaluate(
                    """async () => {
                        const id = window.__lab_challenge_id;
                        const s = await fetch('/api/solve', {
                            method:'POST',
                            headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({challenge_id:id})
                        });
                        const sd = await s.json();
                        const ch = window.__lab_challenge;
                        return {cx: sd.predicted_cx, cy: sd.predicted_cy,
                                piece: ch.piece_size, origin: ch.piece_origin};
                    }""")
                target_left = oracle["cx"] - oracle["piece"] / 2

                dur, _ = self._drag(page, strategy, target_left, oracle)
                page.wait_for_timeout(900)

                res = AttackResult(
                    strategy=strategy,
                    solved=bool(last_resp.get("ok")),
                    duration_ms=round(dur, 1),
                    human_score=last_resp.get("behaviour", {}).get("human_score"),
                    error_px=last_resp.get("distance_px"),
                    note=last_resp.get("message", ""),
                    solved_by=last_resp.get("solved_by"),
                    would_block=bool(last_resp.get("would_block")),
                    blocked=bool(last_resp.get("reason") == "behaviour_rejected"),
                )
                out.append(res)

            browser.close()
        return out

    def _api_only(self, page) -> AttackResult:
        """Use /api/solve then POST /api/verify with a machine trace."""
        t0 = time.time()
        res = page.evaluate(
            """async (tok) => {
                const c = await fetch('/api/challenge?width=320&height=160&piece_size=46',
                    {headers:{Authorization:'Bearer '+tok}});
                const d = await c.json();
                const s = await fetch('/api/solve', {
                    method:'POST',
                    headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
                    body: JSON.stringify({challenge_id:d.challenge_id})
                });
                const sd = await s.json();
                const v = await fetch('/api/verify', {
                    method:'POST',
                    headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
                    body: JSON.stringify({
                        challenge_id: d.challenge_id,
                        x: sd.predicted_cx, y: sd.predicted_cy,
                        trace: [{x: sd.predicted_cx, y: sd.predicted_cy, t: 0}],
                        duration_ms: 80
                    })
                });
                return {solve: sd, verify: await v.json()};
            }""", self.agent_token(page))
        dur = (time.time() - t0) * 1000.0
        v = res["verify"]
        return AttackResult(
            strategy="api_only",
            solved=bool(v.get("ok")),
            duration_ms=round(dur, 1),
            human_score=v.get("behaviour", {}).get("human_score"),
            error_px=v.get("distance_px"),
            note=v.get("message", ""),
            solved_by=v.get("solved_by"),
            would_block=bool(v.get("would_block")),
            blocked=bool(v.get("reason") == "behaviour_rejected"),
        )

    def _no_mouse(self, page) -> AttackResult:
        """Send a synthetic 'human' trace via the API without any pointer events."""
        res = page.evaluate(
            """async (tok) => {
                const c = await fetch('/api/challenge?width=320&height=160&piece_size=46',
                    {headers:{Authorization:'Bearer '+tok}});
                const d = await c.json();
                const s = await fetch('/api/solve', {
                    method:'POST',
                    headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
                    body: JSON.stringify({challenge_id:d.challenge_id})
                });
                const sd = await s.json();
                const trace = [];
                for (let i=0;i<28;i++){
                    const t=i/27;
                    trace.push({x: 6 + (sd.predicted_cx-6)*t + (i%3===0?3:-2)*t,
                                y: d.piece_origin.y, t: Math.round(700*t)});
                }
                const v = await fetch('/api/verify', {
                    method:'POST',
                    headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
                    body: JSON.stringify({
                        challenge_id: d.challenge_id,
                        x: sd.predicted_cx, y: sd.predicted_cy,
                        trace, duration_ms: 740
                    })
                });
                return await v.json();
            }""", self.agent_token(page))
        return AttackResult(
            strategy="no_mouse",
            solved=bool(res.get("ok")),
            duration_ms=740.0,
            human_score=res.get("behaviour", {}).get("human_score"),
            error_px=res.get("distance_px"),
            note=res.get("message", ""),
            solved_by=res.get("solved_by"),
            would_block=bool(res.get("would_block")),
            blocked=bool(res.get("reason") == "behaviour_rejected"),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--strategies", default="teleport,linear,humanish,no_mouse,api_only")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    atk = Attacker(args.url, seed=args.seed)
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    print("=" * 74)
    print("CAPTCHA Lab — bot attack tester   ({})".format(args.url))
    print("=" * 74)
    print(f"{'strategy':<12} {'solved':<8} {'by':<11} {'blocked':<9} {'err px':<8} {'human':<7} {'ms':<7}")
    print("-" * 74)

    all_results = []
    for s in strategies:
        try:
            results = atk.run(s, runs=args.runs)
        except Exception as e:
            print(f"{s:<12} ERROR: {e}")
            continue
        for r in results:
            all_results.append(r.to_dict())
            hs = f"{r.human_score:.2f}" if r.human_score is not None else "—"
            err = f"{r.error_px:.0f}" if r.error_px is not None else "—"
            blk = "YES" if r.blocked else ("would" if r.would_block else "—")
            who = {"human": "HUMAN", "ai_agent": "AI AGENT"}.get(r.solved_by or "", "—")
            print(f"{r.strategy:<12} {str(r.solved):<8} {who:<11} {blk:<9} {err:<8} {hs:<7} {r.duration_ms:<7.0f}")

    print("-" * 74)
    solved = sum(1 for r in all_results if r["solved"])
    blocked = sum(1 for r in all_results if r["blocked"])
    effective = sum(1 for r in all_results
                    if r["solved"] and not r["blocked"])
    total = len(all_results)
    by_human = sum(1 for r in all_results if r.get("solved_by") == "human")
    by_ai = sum(1 for r in all_results if r.get("solved_by") == "ai_agent")
    print(f"position solved:      {solved}/{total}")
    print(f"blocked by gate:      {blocked}/{total}")
    print(f"EFFECTIVE bypasses:   {effective}/{total}")
    print(f"attributed to HUMAN:  {by_human}   AI AGENT: {by_ai}")
    print("=" * 74)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(all_results, f, indent=2)
        print(f"report written to {args.json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
