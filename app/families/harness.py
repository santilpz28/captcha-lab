"""Multi-family test harness.

Runs every challenge family through its attacker and reports the numbers
that matter for an audit:

    * **exact rate**     — full challenges solved
    * **per-unit rate**  — characters (text) / cells (grid) correct
    * **cost**           — milliseconds per attempt

The headline metric is `exact rate`: the probability that one attempt
clears the challenge. Multiply by the retry budget and you get the real
cost of breaking it.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from .base import Attempt, Challenge, Family


@dataclass
class FamilyResult:
    family: str
    runs: int
    exact_hits: int
    unit_hits: int
    unit_total: int
    durations_ms: list[float] = field(default_factory=list)
    # Per-run details for drill-down.
    details: list[dict] = field(default_factory=list)

    @property
    def exact_rate(self) -> float:
        return self.exact_hits / self.runs if self.runs else 0.0

    @property
    def unit_rate(self) -> float:
        return self.unit_hits / self.unit_total if self.unit_total else 0.0

    @property
    def median_ms(self) -> float:
        return statistics.median(self.durations_ms) if self.durations_ms else 0.0

    @property
    def expected_attempts(self) -> float:
        """Attempts needed to clear one challenge, geometric with retries."""
        p = self.exact_rate
        if p <= 0:
            return float("inf")
        if p >= 1:
            return 1.0
        return 1.0 / p

    def to_dict(self) -> dict:
        return {
            "family": self.family,
            "runs": self.runs,
            "exact_hits": self.exact_hits,
            "exact_rate": round(self.exact_rate, 4),
            "unit_rate": round(self.unit_rate, 4),
            "unit_hits": self.unit_hits,
            "unit_total": self.unit_total,
            "median_ms": round(self.median_ms, 1),
            "expected_attempts": (round(self.expected_attempts, 2)
                                  if self.expected_attempts != float("inf")
                                  else None),
        }


def _truth_summary(truth: object) -> object:
    """A compact, human-readable view of the answer key for drill-down.

    Each family stores its truth differently (a text string, a list of
    cell indices, a label plus a bounding box), so the summary is
    family-agnostic: it pulls the few keys that actually matter and drops
    the bulky ones.
    """
    if not isinstance(truth, dict):
        return truth
    out = {}
    if "text" in truth:
        out["text"] = truth["text"]
    if "cells" in truth:
        out["cells"] = truth["cells"]
    if "target" in truth:
        out["target"] = truth["target"]
    if "label" in truth:
        out["label"] = truth["label"]
    if "box" in truth:
        box = truth["box"]
        out["box"] = [box.get(k) for k in ("x", "y", "w", "h")] \
            if isinstance(box, dict) else box
    return out or truth


def _unit_score(family: Family, challenge: Challenge,
                attempt: Attempt) -> tuple[int, int]:
    """Per-unit correctness: how many characters/cells were right.

    Families that do not expose units report the whole challenge as one
    unit, which makes the metric identical to the exact rate.
    """
    truth = challenge.truth
    if family.name == "text":
        t = truth["text"].upper()
        a = (attempt.answer or "").upper()
        return sum(1 for x, y in zip(t, a) if x == y), max(1, len(t))
    if family.name == "grid":
        want = set(truth["cells"])
        got = set(attempt.answer or [])
        return len(want & got), max(1, len(want))
    if family.name == "image":
        # `attempt.answer` is a bounding box, not a label string, so the
        # comparison has to go through the family's own scoring (which
        # checks box overlap). Comparing the box to truth["label"] was
        # always False — the per-unit column read 0% while the exact rate
        # read 33%.
        return (1 if family.score(challenge, attempt) else 0), 1
    return (1 if family.score(challenge, attempt) else 0), 1


def run_family(family: Family, runs: int = 20, seed: int | None = None,
               verbose: bool = False) -> FamilyResult:
    """Generate `runs` challenges and attack each one."""
    res = FamilyResult(family=family.name, runs=runs, exact_hits=0,
                       unit_hits=0, unit_total=0)
    for i in range(runs):
        ch = family.generate(seed=None if seed is None else seed + i)
        t0 = time.time()
        attempt = family.solve(ch)
        dt = (time.time() - t0) * 1000
        ok = family.score(ch, attempt)
        uh, ut = _unit_score(family, ch, attempt)

        res.exact_hits += int(ok)
        res.unit_hits += uh
        res.unit_total += ut
        res.durations_ms.append(dt)
        res.details.append({
            "seed": ch.seed,
            "exact": bool(ok),
            "units": [uh, ut],
            "ms": round(dt, 1),
            "answer": attempt.answer if isinstance(attempt.answer, str)
                      else attempt.answer,
            "truth": _truth_summary(ch.truth),
            "meta": {k: v for k, v in attempt.meta.items()
                     if k in ("ocr_raw", "confidence", "cells_scored")},
        })
        if verbose:
            mark = "OK " if ok else "-- "
            print(f"  [{mark}] seed={ch.seed:>10} {dt:7.0f}ms  "
                  f"units {uh}/{ut}  {res.details[-1]['truth']!r} -> "
                  f"{res.details[-1]['answer']!r}")
    return res


def run_all(families: dict[str, Family], runs: int = 20,
            seed: int | None = None, verbose: bool = False) -> dict[str, FamilyResult]:
    """Run every family and return the results keyed by family name."""
    out = {}
    for name, fam in families.items():
        if verbose:
            print(f"\n=== {name} ===")
        out[name] = run_family(fam, runs=runs, seed=seed, verbose=verbose)
    return out


def format_report(results: dict[str, FamilyResult]) -> str:
    """A compact, scannable table."""
    lines = []
    lines.append("=" * 78)
    lines.append(f"{'family':<10} {'exact':>10} {'per-unit':>10} "
                 f"{'ms':>8} {'tries':>8}")
    lines.append("-" * 78)
    for name, r in results.items():
        tries = (f"{r.expected_attempts:.1f}"
                 if r.expected_attempts != float("inf") else "∞")
        lines.append(
            f"{name:<10} {r.exact_hits:>3}/{r.runs:<3} {r.exact_rate:>6.0%}"
            f" {r.unit_rate:>9.0%} {r.median_ms:>8.0f} {tries:>8}")
    lines.append("-" * 78)
    lines.append("exact    = whole challenges cleared")
    lines.append("per-unit = characters (text) / cells (grid) read correctly")
    lines.append("tries    = expected attempts to clear one challenge")
    lines.append("=" * 78)
    return "\n".join(lines)
