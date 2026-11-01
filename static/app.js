/* CAPTCHA Lab — client

   Architecture
   ------------
   * The piece SPRITE lives over the canvas (.plate-piece), absolutely
     positioned in canvas pixels scaled by the CSS factor. It is what the user
     actually moves into the gap, and its position maps 1:1 onto the canvas.
   * The TRACK below is the drag handle. Its horizontal position drives the
     sprite's horizontal position; the sprite's vertical position is fixed to
     the gap's row, which is how every real slider CAPTCHA works.

   All positions reported to the server are in CANVAS pixels.
*/
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  const els = {
    plate: $("plate"),
    bg: $("bg-img"), piece: $("piece-img"), track: $("track"),
    slider: $("slider"), fill: $("track-fill"), hint: $("track-hint"),
    result: $("result"), resultText: $("result-text"), resultExtra: $("result-extra"),
    log: $("log"), readout: $("readout"), challengeId: $("challenge-id"),
    plateSize: $("plate-size"), btnNew: $("btn-new"), btnSolve: $("btn-solve"),
    btnFake: $("btn-fake"), btnStats: $("btn-stats"),
    selSize: $("sel-size"), selPiece: $("sel-piece"),
  };

  const state = {
    challenge: null,
    trace: [],
    startedAt: 0,
    dragging: false,
    startX: 0,
    baseLeft: 0,
    solved: false,
    busy: false,
    // Count of REAL pointer events observed in this drag. The server uses
    // it as a provenance signal: a trace forged over the API reports 0.
    pointerEvents: 0,
    // The signed session token. This widget acts as a HUMAN; the token
    // proves it to the server. The "Auto-solve (CV)" button is an AI
    // AGENT path and uses a different token.
    token: null,
    sessionKind: "human",
  };

  /* ------------------------------------------------------------ geometry -- */

  /* CSS pixels per canvas pixel. */
  function canvasScale() {
    if (!state.challenge) return 1;
    const w = els.bg.getBoundingClientRect().width;
    return w ? w / state.challenge.width : 1;
  }

  /* The sprite's natural canvas size (the PNG includes the knob + pad). */
  function spriteCanvasSize() {
    const sp = state.challenge.sprite;
    return { w: sp.w, h: sp.h };
  }

  /* Where the sprite's PNG origin sits on the canvas, in canvas px.
     `offset` is the piece's horizontal travel from its REST position (in
     canvas px). At offset 0 the shape's opaque box sits at
     `piece_rest`; the shape travels with the sprite, so its top-left is
     `piece_rest + offset`. */
  function spriteOriginCanvas(offset) {
    const sp = state.challenge.sprite;
    const shapeLeft = state.challenge.piece_rest.x + (offset || 0);
    const shapeTop = state.challenge.piece_rest.y;
    return {
      // back out the PNG's opaque-box offset to find the PNG origin
      x: shapeLeft - sp.opaque_x,
      y: shapeTop - sp.opaque_y,
    };
  }

  /* The shape's centre on the canvas, in canvas px. */
  function shapeCentreCanvas(offset) {
    const o = spriteOriginCanvas(offset);
    const sp = state.challenge.sprite;
    return {
      x: o.x + sp.opaque_x + sp.opaque_w / 2,
      y: o.y + sp.opaque_y + sp.opaque_h / 2,
    };
  }

  /* The travel needed to bring the shape onto the gap, in canvas px. */
  function travelToGap() {
    return state.challenge.travel.x;
  }

  /* Maximum travel (the shape must stay on the canvas). */
  function maxTravel() {
    const sp = state.challenge.sprite;
    const shapeW = sp.opaque_w;
    return Math.max(0, state.challenge.width - state.challenge.piece_rest.x - shapeW - 8);
  }

  /* Paint the sprite at the given offset (canvas px from the rest position). */
  function paintSprite(offsetCanvas) {
    const scale = canvasScale();
    const sp = state.challenge.sprite;
    const plateRect = els.plate.getBoundingClientRect();
    const origin = spriteOriginCanvas(offsetCanvas);

    els.piece.style.width = (sp.w * scale) + "px";
    els.piece.style.height = (sp.h * scale) + "px";
    els.piece.style.left = (origin.x * scale) + "px";
    els.piece.style.top = (origin.y * scale) + "px";
    void plateRect;
  }

  /* ---------------------------------------------------------------- log -- */
  function log(msg, kind) {
    const t = new Date().toLocaleTimeString("en-GB", { hour12: false });
    const div = document.createElement("div");
    div.className = "line";
    div.innerHTML = '<span class="t">' + t + "</span> " +
      '<span class="' + (kind || "") + '">' + escapeHtml(msg) + "</span>";
    els.log.appendChild(div);
    els.log.scrollTop = els.log.scrollHeight;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  /* --------------------------------------------------------------- http -- */
  async function api(path, opts) {
    const headers = Object.assign({
      "Content-Type": "application/json",
    }, (opts && opts.headers) || {});
    // Attach the session token when we have one.
    if (state.token) headers["Authorization"] = "Bearer " + state.token;
    const res = await fetch(path, Object.assign({}, opts || {}, { headers }));
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (e) {}
      throw new Error(detail);
    }
    return res.json();
  }

  /* ------------------------------------------------------------ session -- */
  /* Mint a signed session token. `human` for the widget, `ai_agent` for
     the CV solver button. The server signs these, so the distinction is
     cryptographically enforced rather than self-declared. */
  async function openSession(kind) {
    // A test harness may inject an agent token (window.__lab_agent_token).
    // If so, honour it instead of minting our own: the server's
    // human/AI attribution must reflect who is actually driving.
    if (window.__lab_agent_token && kind === "human") {
      state.token = window.__lab_agent_token;
      state.sessionKind = "ai_agent";
      return { session: { kind: "ai_agent", label: "AI AGENT",
                          sid: "(injected)" } };
    }
    const data = await api("/api/session?kind=" + encodeURIComponent(kind));
    state.token = data.token;
    state.sessionKind = data.session.kind;
    return data;
  }

  /* ---------------------------------------------------------- challenge -- */
  async function newChallenge() {
    if (state.busy) return;
    state.busy = true;
    els.btnNew.disabled = true;
    els.btnSolve.disabled = true;
    els.btnFake.disabled = true;
    setResult("Requesting challenge…", null);

    try {
      const [w, h] = els.selSize.value.split("x").map(Number);
      const pieceSize = Number(els.selPiece.value);
      const data = await api("/api/challenge?width=" + w + "&height=" + h +
                             "&piece_size=" + pieceSize);

      state.challenge = data;
      state.trace = [];
      state.solved = false;
      state.pieceOffset = 0;
      state.pointerEvents = 0;

      // Expose the full id so external test harnesses can address the
      // challenge directly.
      window.__lab_challenge_id = data.challenge_id;
      window.__lab_challenge = data;

      els.bg.src = data.background;
      els.bg.width = data.width;
      els.bg.height = data.height;
      els.piece.src = data.piece;
      els.plateSize.textContent = data.width + " × " + data.height;
      els.challengeId.textContent = "id " + data.challenge_id.slice(0, 10);

      els.slider.style.left = "6px";
      els.fill.style.width = "0px";
      els.track.classList.remove("is-touched");

      paintSprite(0);

      const maxOffset = maxTravel();
      els.track.setAttribute("aria-valuemax", String(Math.max(0, Math.round(maxOffset))));
      els.track.setAttribute("aria-valuenow", "0");

      setResult("Challenge ready — drag the piece into the slot.", null);
      log("challenge issued id=" + data.challenge_id.slice(0, 8) +
          " size=" + data.width + "x" + data.height +
          " piece=" + data.piece_size, "info");

      els.btnSolve.disabled = false;
      els.btnFake.disabled = false;
      await refreshStats();
    } catch (e) {
      setResult("Failed to get challenge: " + e.message, "err");
      log("challenge error: " + e.message, "err");
    } finally {
      state.busy = false;
      els.btnNew.disabled = false;
    }
  }

  /* ------------------------------------------------------------- result -- */
  function setResult(msg, kind, extra) {
    els.result.className = "result" + (kind ? " " + kind : "");
    els.resultText.textContent = msg;
    els.resultExtra.textContent = extra || "";
  }

  /* ------------------------------------------------------------ dragging -- */
  function pointerX(e) {
    return e.touches ? e.touches[0].clientX : e.clientX;
  }

  function beginDrag(e) {
    if (!state.challenge || state.solved || state.busy) return;
    state.dragging = true;
    state.trace = [];
    state.pointerEvents = 1;
    state.startedAt = performance.now();
    state.startX = pointerX(e);
    state.baseOffset = state.pieceOffset || 0;
    // Seed the trace with the grab point. A single-jump drag then still
    // produces a valid two-point trace (grab -> release).
    state.trace.push({
      x: Math.round(state.pieceOffset),
      y: Math.round(e.touches ? e.touches[0].clientY : e.clientY),
      t: 0,
    });
    els.track.classList.add("is-touched", "is-dragging");
    if (els.track.setPointerCapture) {
      try { els.track.setPointerCapture(e.pointerId); } catch (err) {}
    }
    e.preventDefault();
  }

  function moveDrag(e) {
    if (!state.dragging) return;
    state.pointerEvents += 1;
    const scale = canvasScale();
    const dxCanvas = (pointerX(e) - state.startX) / scale;
    const maxOffset = maxTravel();

    let offset = state.baseOffset + dxCanvas;
    // the piece starts at the left; it may not go further left than rest
    offset = Math.max(0, Math.min(maxOffset, offset));
    state.pieceOffset = offset;

    paintSprite(offset);

    // the track handle mirrors the piece's travel
    const handleLeft = 6 + offset * scale;
    els.slider.style.left = handleLeft + "px";
    els.fill.style.width = (handleLeft - 6 + state.challenge.sprite.w * scale) + "px";
    els.track.setAttribute("aria-valuenow", String(Math.round(offset)));

    // behavioural trace (throttled)
    if (state.trace.length === 0 ||
        Math.abs(offset - state.trace[state.trace.length - 1].x) > 1.5) {
      state.trace.push({
        x: Math.round(offset),
        y: Math.round(e.touches ? e.touches[0].clientY : e.clientY),
        t: Math.round(performance.now() - state.startedAt),
      });
    }
    e.preventDefault();
  }

  function endDrag(e) {
    if (!state.dragging) return;
    state.dragging = false;
    els.track.classList.remove("is-dragging");
    const duration = performance.now() - state.startedAt;

    // Record the release point so a single-jump drag still yields two
    // trace samples (grab + release).
    const last = state.trace[state.trace.length - 1];
    if (!last || last.x !== Math.round(state.pieceOffset)) {
      state.trace.push({
        x: Math.round(state.pieceOffset),
        y: Math.round(e.touches ? e.touches[0].clientY : e.clientY),
        t: Math.round(duration),
      });
    }

    if (state.trace.length < 2) {
      setResult("Drag the piece across the image before releasing.", "err");
      return;
    }

    const centre = shapeCentreCanvas(state.pieceOffset);
    submit(centre.x, centre.y, duration);
  }

  function onKey(e) {
    if (!state.challenge || state.solved) return;
    const step = (e.shiftKey ? 20 : 6);
    const maxOffset = maxTravel();
    let offset = state.pieceOffset || 0;
    if (e.key === "ArrowRight") offset += step;
    else if (e.key === "ArrowLeft") offset -= step;
    else return;
    e.preventDefault();
    offset = Math.max(0, Math.min(maxOffset, offset));
    state.pieceOffset = offset;
    paintSprite(offset);
    const scale = canvasScale();
    const handleLeft = 6 + offset * scale;
    els.slider.style.left = handleLeft + "px";
    els.fill.style.width = (handleLeft - 6 + state.challenge.sprite.w * scale) + "px";
    els.track.setAttribute("aria-valuenow", String(Math.round(offset)));
    state.trace.push({ x: Math.round(offset), y: 0, t: 0 });
  }

  /* ------------------------------------------------------------- submit -- */
  async function submit(cx, cy, duration) {
    if (!state.challenge) return;
    state.busy = true;
    els.btnSolve.disabled = true;
    els.btnFake.disabled = true;
    setResult("Verifying…", null);

    try {
      const res = await api("/api/verify", {
        method: "POST",
        body: JSON.stringify({
          challenge_id: state.challenge.challenge_id,
          x: cx, y: cy,
          trace: state.trace.slice(-200),
          duration_ms: Math.round(duration),
          pointer_events: state.pointerEvents,
        }),
      });
      const b = res.behaviour || {};
      const who = res.solved_by_label || (b.human_score >= 0.5 ? "HUMAN" : "AI AGENT");
      if (res.ok) {
        state.solved = true;
        setResult("Solved by " + who + " — " + res.distance_px + " px from target.",
                  "ok",
                  "human score " + (b.human_score != null ? b.human_score.toFixed(2) : "—"));
        log("SOLVED by " + who +
            " (distance " + res.distance_px + "px, human score " +
            (b.human_score != null ? b.human_score.toFixed(2) : "—") + ")",
            who === "HUMAN" ? "ok" : "warn");
      } else {
        setResult("Rejected — " + res.distance_px + " px off (tolerance ±" +
                  res.tolerance_px + " px).", "err",
                  who + " · human score " + (b.human_score != null ? b.human_score.toFixed(2) : "—"));
        log("rejected (" + who + "): " + res.distance_px + "px off target", "warn");
      }
      await refreshStats();
    } catch (e) {
      setResult("Verify error: " + e.message, "err");
      log("verify error: " + e.message, "err");
    } finally {
      state.busy = false;
      els.btnSolve.disabled = false;
      els.btnFake.disabled = false;
    }
  }

  /* --------------------------------------------------------------- solve -- */
  async function autoSolve() {
    if (!state.challenge || state.busy) return;
    state.busy = true;
    els.btnSolve.disabled = true;
    setResult("Running CV solver…", null);
    try {
      // The CV solver is an AI AGENT, not a human. Swap to an agent-signed
      // token for the call so the server books this solve as AI.
      const prevToken = state.token;
      const prevKind = state.sessionKind;
      await openSession("ai_agent");
      let res;
      let verified = null;
      try {
        res = await api("/api/solve", {
          method: "POST",
          body: JSON.stringify({ challenge_id: state.challenge.challenge_id }),
        });
        // Submit the solution through the normal verification path, exactly
        // as a real attacker would. Without this the solve is never
        // *booked* — the stats would show a solver hit but no resolution,
        // and the HUMAN/AI counters would stay at zero.
        if (res.hit) {
          verified = await api("/api/verify", {
            method: "POST",
            body: JSON.stringify({
              challenge_id: state.challenge.challenge_id,
              x: res.predicted_cx,
              y: res.predicted_cy,
              trace: [{x: res.predicted_cx, y: res.predicted_cy, t: 0}],
              duration_ms: 60,
              pointer_events: 1,
            }),
          });
        }
      } finally {
        // Restore the human session for subsequent interactions.
        state.token = prevToken;
        state.sessionKind = prevKind;
      }

      const bookedAs = (verified && verified.solved_by_label) || res.solved_by_label || "AI AGENT";
      els.readout.textContent = [
        "solved by      " + bookedAs,
        "predicted x   " + res.predicted_x,
        "predicted y   " + res.predicted_y,
        "ncc peak      " + res.ncc,
        "confidence    " + res.confidence,
        "error         " + res.error_px + " px (tolerance ±" + res.tolerance_px + " px)",
        "result        " + (res.hit ? "HIT — machine-solvable" : "MISS"),
        "booked as     " + bookedAs,
      ].join("\n");
      log("AI AGENT solver: x=" + res.predicted_x + " err=" + res.error_px + "px " +
          (res.hit ? "HIT" : "MISS"), res.hit ? "ok" : "warn");

      // Drive the piece to the solved position so the user sees it work.
      const sp = state.challenge.sprite;
      const targetOffset = travelToGap();
      state.pieceOffset = Math.max(0, targetOffset);
      paintSprite(state.pieceOffset);
      const scale = canvasScale();
      const handleLeft = 6 + state.pieceOffset * scale;
      els.slider.style.left = handleLeft + "px";
      els.fill.style.width = (handleLeft - 6 + sp.w * scale) + "px";
      els.track.classList.add("is-touched");

      const verdict = verified
        ? (verified.ok ? "verified" : ("rejected: " + (verified.reason || "unknown")))
        : "not submitted";
      setResult("AI AGENT solved — " + res.message + " (error " +
                res.error_px + " px).", res.hit ? "ok" : "err",
                "booked as " + bookedAs + " · confidence " + res.confidence.toFixed(2) +
                " · " + verdict +
                " (your browser session stays " +
                (prevKind === "human" ? "HUMAN" : prevKind) + ")");
      await refreshStats();
    } catch (e) {
      setResult("Solver error: " + e.message, "err");
      log("solver error: " + e.message, "err");
    } finally {
      state.busy = false;
      els.btnSolve.disabled = false;
    }
  }

  async function submitWrong() {
    if (!state.challenge) return;
    const off = 40 + Math.floor(Math.random() * 60);
    const centre = shapeCentreCanvas(off);
    log("submitting deliberately wrong offset +" + off + "px", "info");
    await submit(centre.x, centre.y, 900);
  }

  /* --------------------------------------------------------------- stats -- */
  async function refreshStats() {
    try {
      const s = await api("/api/stats");
      $("s-issued").textContent = s.issued;
      $("s-solved").textContent = s.solved;
      $("s-failed").textContent = s.failed;
      $("s-calls").textContent = s.solver_calls;
      $("s-hits").textContent = s.solver_hits;
      const rate = $("s-rate");
      if (s.solver_hit_rate == null) {
        rate.textContent = "—";
        rate.className = "value";
      } else {
        rate.textContent = (s.solver_hit_rate * 100).toFixed(0) + "%";
        rate.className = "value " +
          (s.solver_hit_rate > 0.8 ? "bad" : s.solver_hit_rate > 0.4 ? "warn" : "good");
      }
      $("k-tol").textContent = "±" + s.tolerance_px + " px";
      $("k-ttl").textContent = s.ttl_s + " s";
      $("k-active").textContent = s.active_challenges;
      $("k-session").textContent = state.sessionKind === "human"
        ? "HUMAN (signed)" : (state.sessionKind === "ai_agent" ? "AI AGENT (signed)" : "—");

      // Human vs AI agent split — the metric this lab exists for.
      $("s-human").textContent = s.solved_human != null ? s.solved_human : 0;
      $("s-ai").textContent = s.solved_ai_agent != null ? s.solved_ai_agent : 0;
      $("s-human-sub").textContent =
        (s.sessions_human || 0) + " browser session(s)";
      $("s-ai-sub").textContent =
        (s.sessions_ai_agent || 0) + " agent session(s)";
      $("stats-updated").textContent =
        "updated " + new Date().toLocaleTimeString("en-GB", { hour12: false });
    } catch (e) {
      log("stats error: " + e.message, "err");
    }
  }

  /* --------------------------------------------------------------- wire -- */
  els.track.addEventListener("pointerdown", beginDrag);
  window.addEventListener("pointermove", moveDrag);
  window.addEventListener("pointerup", endDrag);
  window.addEventListener("pointercancel", endDrag);
  els.track.addEventListener("keydown", onKey);
  window.addEventListener("resize", () => { if (state.challenge) paintSprite(state.pieceOffset); });

  els.btnNew.addEventListener("click", newChallenge);
  els.btnSolve.addEventListener("click", autoSolve);
  els.btnFake.addEventListener("click", submitWrong);
  els.btnStats.addEventListener("click", refreshStats);
  els.selSize.addEventListener("change", newChallenge);
  els.selPiece.addEventListener("change", newChallenge);

  log("lab ready", "info");
  // Open a HUMAN session first: the widget is a person dragging a piece.
  openSession("human")
    .then((d) => {
      log("session opened: " + d.session.label + " (sid " +
          d.session.sid.slice(0, 8) + ")", "info");
      return newChallenge();
    })
    .catch((e) => {
      log("session error: " + e.message, "err");
      newChallenge();
    });
})();
