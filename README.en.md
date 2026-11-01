# CAPTCHA Lab — slider puzzle generator & attacker tester

[ES](README.md) · [EN](#captcha-lab--slider-puzzle-generator--attacker-tester)

> **A CAPTCHA is only as strong as its weakest attacker.**
> This project builds the CAPTCHA *and* the attacker that breaks it, then reports
> both numbers honestly.

![The lab UI](docs/images/ui-desktop.png)

A generator and test bench for **slider** CAPTCHAs (drag-to-fit): the challenge used
by nearly every company today — a puzzle piece you have to drag into its gap.

The lab has **two halves**, and that is the whole point:

| Half | What it does | Why it matters |
|---|---|---|
| **Generator** | Emits real challenges: procedural background, jigsaw piece, gap, track, behavioural scoring | The challenge has to be **solvable by a person** and **not trivially solvable by a machine** |
| **Tester** | Runs attacks against the widget and measures what breaks it | A CAPTCHA a bot solves is not a CAPTCHA: it is a decorated form |

---

## What this lab actually proves (with numbers, not opinions)

| Metric | Value | What it means |
|---|---|---|
| CV solver | **40/40 exact, 0 px error** | A naive matcher finds the gap every time → position was never the problem |
| Attacks in production mode | **12/15 blocked** | The 3 that get through are `humanish`: an attack indistinguishable from a person |
| Attribution | **15/15 → AI AGENT, 0 → HUMAN** | The server knows *who* solved it, using signed tokens |
| Solver latency | ~120 ms | Fast enough for production... which is exactly why you need the behavioural gate |

The conclusion is uncomfortable, and it is the point of the whole lab:

> **A CAPTCHA without server-side attestation is a decorated form.**
> This repo contains the evidence for why that is true, measured.

### Differentiator: telling humans and AI apart

![Human vs AI agent](docs/images/sessions-human-vs-ai.png)

Session tokens are **HMAC-SHA256 signed**, and the type (`human` / `ai_agent`) is
**inside the signature**: an agent cannot relabel its own token as human. The
*Auto-solve* button books the resolution as AI even when a person is sitting
right in front of the browser.

---

## Quick start

```bash
git clone https://github.com/santilpz28/captcha-lab.git
cd captcha-lab
pip install -r requirements.txt

python3 -m uvicorn app.server:app --host 127.0.0.1 --port 8099
# → http://127.0.0.1:8099
```

For the bot tests: `playwright install chromium`.

```bash
python3 tests/test_e2e.py                                  # 40 assertions
python3 tests/test_bot.py --runs 3 --json out/bot.json     # 5 attack strategies
```

---

## API

### `GET /api/session?kind=human|ai_agent`

Opens a session and returns a **signed token** — the mechanism that tells a human
resolution apart from an AI one:

```jsonc
{
  "token": "lab_hu…Mm_…",
  "session": {
    "kind": "human", "label": "HUMAN",
    "sid": "44ea8b8c1f3a9d02",
    "issued_at": 1793569070.4, "expires_at": 1793572670.4
  },
  "expires_in": 3600
}
```

- The prefix is human-readable (`lab_human.` / `lab_ai_agent.`).
- The signature is **HMAC-SHA256** over `kind.payload` with the server secret.
- 1 hour TTL.

> **Why this matters:** the type is *inside the signature*. An agent cannot
> relabel its token as `human` — the server rejects the bad signature. If the type
> were just a payload field, the human/AI distinction would be theatre.

Every flow carries the token in `Authorization: Bearer <token>`:
`/api/challenge`, `/api/verify` and `/api/agent/solve`.

### `GET /api/challenge?width=320&height=160&piece_size=46`

Issues a challenge. Returns the background and the piece as data URLs, plus the
geometry the client needs to place the piece:

```jsonc
{
  "challenge_id": "…",
  "background": "data:image/png;base64,…",
  "piece":      "data:image/png;base64,…",
  "width": 320, "height": 160, "piece_size": 46,
  "sprite": {            // geometry of the piece PNG
    "w": 69, "h": 69,    //   natural PNG size (includes halo)
    "opaque_x": 8, "opaque_y": 8,   // opaque box inside the PNG
    "opaque_w": 47, "opaque_h": 54
  },
  "piece_rest": {"x": 8,  "y": 45},   // rest position (canvas px)
  "travel":    {"x": 143, "y": 0},   // travel to the gap
  "shape_centre": {"x": 174, "y": 81},  // shape centre (the verify reference)
  "session": {"kind": "human", "label": "HUMAN", "sid": "44ea8b8c…"},
  "expires_in": 300
}
```

> **The gap never travels in the payload.** The ground truth stays on the server.

### `POST /api/verify`

```jsonc
{
  "challenge_id": "…",
  "x": 174, "y": 81,        // shape centre in canvas pixels
  "trace": [{"x": 12, "y": 300, "t": 0}, …],   // drag trajectory
  "duration_ms": 940,
  "pointer_events": 41      // pointer events reported by the client
}
```

Response:

```jsonc
{
  "ok": true,
  "distance_px": 0.5,
  "tolerance_px": 6,
  "solved_by": "human",           // "human" | "ai_agent" | "unknown"
  "solved_by_label": "HUMAN",
  "session_sid": "44ea8b8c…",
  "behaviour": {
    "wobble_ratio": 1.08,   // path length / straight-line distance
    "path_len": 214.3,
    "samples": 41,
    "duration_ms": 940.0,
    "direction_changes": 3,  // overshoots / reversals
    "pointer_events": 41,     // REAL pointer events the browser saw
    "human_score": 1.0
  }
}
```

A challenge is **single-use**: it is dropped from the store on solve (replay → 404).

If the position is correct but the behavioural score falls below the threshold:

```jsonc
{
  "ok": false,
  "reason": "behaviour_rejected",
  "behaviour": {"human_score": 0.05, "pointer_events": 0, …}
}
```

### `POST /api/solve`

Runs the computer-vision solver against the challenge. **It returns the answer**
— deliberately accessible, because the point of the lab is to measure whether the
challenge is breakable, not to hide it.

It always reports `solved_by: "ai_agent"`: the solver **is** an AI, and the lab
should not report a CV solve as if a person had done it.

### `POST /api/agent/solve`

Shortcut for agents: opens an `ai_agent` session, solves the challenge and books
it as AI, all in one call.

### `GET /api/stats` · `GET /api/health`

Aggregated metrics and health check. The stats split by actor:

```jsonc
{
  "issued": 44, "solved": 17,
  "solved_human": 1,        // solved by a human
  "solved_ai_agent": 11,    // solved by an AI / agent
  "solved_unknown": 5,      // solved without a token (anonymous API)
  "by_actor": {"human": 1, "ai_agent": 11, "unknown": 5},
  "sessions_human": 7, "sessions_ai_agent": 4,
  "solver_calls": 23, "solver_hits": 23, "solver_hit_rate": 1.0,
  "would_block": 12, "blocked": 0
}
```

---

## How it works internally

### Generator (`app/render.py`)

1. **Procedural background** — gradients + colour blobs + 2 octaves of noise +
   smoothstep contrast + saturation. No stock photos: every challenge is distinct
   and nothing depends on the network.
2. **Jigsaw shape** — a rectangle with a **tab** on top and a **notch** on the right.
3. **The gap keeps the original texture** — that is what makes the challenge
   *solvable*: the solver has something to correlate against. A gap with invented
   texture would make the challenge unsolvable (and therefore useless).
4. **The piece is cut exactly from the gap region** → it carries the texture it
   has to match.
5. **Halo and keyline only *outside* the shape** (dilate − shape). Any pixel
   painted inside the shape would corrupt the texture and drop the solver from
   40/40 to 14/40.

> **The sprite and the solver template are two different images.** The sprite the
> user sees carries the keyline and drop shadow (that is what makes it read as a
> "lifted piece"); the solver gets a **pristine crop** of the same region.
> Mixing them was exactly the bug that wrecked the solver's accuracy.

### Solver (`app/solver.py`)

Masked **normalised cross-correlation** over an FFT:

- the mask is the shape, eroded 2 px to skip the inner shadow ring
- the FFT makes the cost independent of canvas size
- the NCC peak gives the gap position

**Measured accuracy: 40/40 exact, 0 px error, ~120 ms.**

### Sessions and attribution (`app/auth.py`)

The lab distinguishes three actors:

| Actor | How it arrives | How it is counted |
|---|---|---|
| **HUMAN** | signed `lab_human.` token | `solved_human` |
| **AI AGENT** | signed `lab_ai_agent.` token | `solved_ai_agent` |
| **UNKNOWN** | no token (anonymous API) | `solved_unknown` |

The token is `lab_<kind>.<payload_b64>.<sig_b64>`, with the signature
`HMAC-SHA256(kind.payload, SECRET)` and `SECRET` persisted in
`app/.session_secret` (or `CAPTCHA_LAB_SECRET` from the environment).

The widget opens a human session on load. The **Auto-solve (CV)** button opens a
temporary agent session, calls `/api/solve` and restores the human session — so a
resolution made by the solver **is reported as AI**, even with a person sitting
there. That is the difference between measuring the challenge and fooling yourself.

### Behaviour profiling (`app/server.py`)

Beyond the position, the verification endpoint scores the **trajectory**:

| Signal | Reading |
|---|---|
| `wobble_ratio` | path length ÷ straight-line distance. Humans go back and forth; bots go in a straight line |
| `samples` | a human generates dozens of samples; a bot, one or two |
| `duration_ms` | under 250 ms is incompatible with a real drag |
| `direction_changes` | overshoots and reversals: the signature of a human finger |
| `pointer_events` | **how many real pointer events** the browser saw. An API-forged trace reports 0 |

`human_score` runs 0 to 1 and **it really blocks**: below `HUMAN_SCORE_MIN`
(0.50) the response is `reason: "behaviour_rejected"`.

> **Honesty about the limit.** `pointer_events` is reported by the client, so an
> attacker controlling the browser can send a fake number. This is not attestation:
> it is a heuristic that raises the cost of the trivial attack. The real lesson is
> at the end: without server-side attestation, a CAPTCHA is a decorated form.

---

## Bot tester (`tests/test_bot.py`)

```bash
python3 tests/test_bot.py --url http://127.0.0.1:8099 --runs 3 --json out/bot-report.json
```

| Strategy | What it simulates | Result |
|---|---|---|
| `teleport` | a single mouse jump to the target (naive CV bot) | **blocked** — human score 0.00 |
| `linear` | constant-speed drag | **blocked** — human score 0.40 |
| `humanish` | drag with easing, overshoot and a pause before release | **solves it** — human score 0.55–1.00 |
| `no_mouse` | calls the verify endpoint with a synthetic trace | **blocked** — human score 0.05 |
| `api_only` | asks `/api/solve` for the answer | **blocked** — human score 0.00 |

Last measurement in **production mode** (`LAB_MODE = False`, 3 runs per strategy):

```
position solved:      3/15
blocked by gate:      12/15
EFFECTIVE bypasses:   3/15
attributed to HUMAN:  0   AI AGENT: 15
```

The 3 bypasses are **all `humanish`** — an attack that behaves like a real person
(easing, overshoot, pause before release). **That is the correct result**: the
CAPTCHA resists automation and only yields to something indistinguishable from a
human.

In **lab mode** (`LAB_MODE = True`) the gate is recorded but does not block, so both
numbers can be measured at once:

```
position solved:      15/15
would-block:          12/15
```

---

## Tests (`tests/test_e2e.py`)

```
40 passed, 0 failed
```

- geometry: the piece always fits inside the canvas (40/40)
- texture: the sprite matches the gap (mad < 2 levels, corr > 0.95)
- solver: exact on 40/40, within tolerance on 40/40, < 500 ms
- API: never leaks the ground truth, a challenge cannot be replayed, a wrong
  offset is rejected, a human drag scores high and a bot-like one low
- gate: an API-forged trace (no pointer events) scores low and is flagged as
  blockable; **the same trace with pointer events is accepted**
- sessions: human and agent tokens are issued with readable prefixes; the type
  **cannot be downgraded** (an agent token relabelled as human is rejected); the
  type round-trips exactly through the token; a solve with a human token is
  attributed to HUMAN and the agent flow to AI AGENT; the stats keep both counters
  apart

---

## Project layout

```
captcha-lab/
├── app/
│   ├── tokens.py     # palette, typography, spacing, canvas constants
│   ├── auth.py       # signed session tokens (human vs ai_agent)
│   ├── render.py     # procedural generator + Pillow compositor
│   ├── solver.py     # masked NCC over FFT
│   └── server.py     # FastAPI + store + behavioural scoring
├── static/           # the widget (HTML/CSS/JS, no framework)
├── tests/
│   ├── test_e2e.py   # 40 assertions
│   └── test_bot.py   # 5 attack strategies with Playwright
├── docs/images/      # screenshots
└── out/              # runtime output (gitignored)
```

---

## Implementation notes (the ones that cost blood)

**The token separator: a one-line bug that broke everything.** The token was
emitted as `lab_<kind>_<payload>.<sig>` and parsed with `body.split("_", 1)`.
Since the type itself contains an underscore (`ai_agent`), the split cut the kind
in half: `ai_agent` → `ai`. The result was that **no token ever validated**. But —
and this is the interesting part — the flow still *looked* fine: the browser sent
the token, the server answered `kind: unknown`, and nobody looked closely because
everything else (position, gate, stats) was already in place. A mis-parsed token
does not break the system; **it silently degrades it until it no longer
distinguishes anything**, which is the exact opposite of what it promises.

The rule that comes out of it: *a classifier that does not classify is worse than
having no classifier*, because people trust the number. That is why the tests
assert the exact round-trip of the type, not just that the token "looks valid".

**The type lives inside the signature, not in the payload.** If the type were a
normal payload field (`{"kind": "human", …}`), an agent could relabel its own
token to `human` without touching the signature. With the type signed, the server
rejects the altered token. That is the difference between *labelling* and
*attesting*: only the second survives a hostile actor.

**`pointer_events` is not attestation.** It is a number the client sends. An
attacker controlling the browser can send whatever it likes. It raises the cost of
the trivial attack; it does not close the door. What *is* dishonest here is the
pretence: the lab documents it instead of selling it as a guarantee.

**One source of truth for the geometry.** `piece_rest` + `travel` +
`sprite.opaque_*` describe the shape's position; the client derives from there the
point that gets verified. When client and server used different references (centre
of the square body vs. centre of the opaque shape), the error was a constant 20 px
— the tell-tale of a structural misalignment, not an arithmetic bug.

**The piece floats above the canvas, not inside the track.** The track is only the
handle. The piece has to share **exactly** the row of the gap, and that is only
guaranteed if it is a sibling element positioned absolutely over the plate.

**A sprite with decoration ≠ the solver's template.** Adding keyline + drop shadow
to the sprite so it would "look like a lifted piece" dropped solver accuracy from
40/40 to **14/40**. The decoration must be **strictly exterior**
(`dilate(mask,k) - mask`), and the solver must correlate against a **pristine
crop** of the same region, not against the decorated sprite.

---

## Security disclaimer

This is a **defensive** research tool: it generates CAPTCHA challenges and measures
how well they withstand automated attacks. The solve endpoint is deliberately
open — the point of the lab is to publish the attack surface, not to protect it.

For a real deployment you would still need: a challenge signed and bound to the
session, client attestation (WebAuthn / Turnstile), and server-side behavioural
signals that do not depend on the client reporting them.

## License

MIT — see [LICENSE](LICENSE).

## Author

Santi (Nexo) — [github.com/santilpz28](https://github.com/santilpz28) ·
founder of **Chain-Cortex** (smart contract audits & Web 2.5 security).
