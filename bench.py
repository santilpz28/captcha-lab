#!/usr/bin/env python3
"""Multi-family CAPTCHA benchmark runner.

Runs every challenge family through its attacker and reports the numbers
that matter for an audit:

    * **exact rate**     — full challenges solved
    * **per-unit rate**  — characters (text) / cells (grid) read correctly
    * **cost**           — milliseconds per attempt

Usage
-----
    python3 bench.py                    # default: 30 runs, seed fixed
    python3 bench.py --runs 50
    python3 bench.py --families text,grid
    python3 bench.py --sweep            # distortion sweep for text
    python3 bench.py --clip             # include the CLIP attacker
    python3 bench.py --json out.json    # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.families.grid_family import GridFamily
from app.families.harness import FamilyResult, format_report, run_family
from app.families.image_family import ImageFamily
from app.families.text_family import TextFamily

DEFAULT_SEED = 20261101


def build_families(include_clip: bool = False) -> dict:
    """The benchmark suite.

    Distortions are calibrated to what production CAPTCHAs actually do:
    noise, mild speckle, gentle wave. The earlier `distortion=1.0` setting
    was far harsher than anything a real site ships — that is documented
    in the README, because a benchmark that flatters the defender is
    worthless.
    """
    fams = {
        "text": TextFamily(),
        "grid": GridFamily(grid=3, target_count=(2, 4),
                           tile_size=96, distortion=0.25),
        "image": ImageFamily(size=220, n_objects=4, distortion=0.25),
    }
    if include_clip:
        from app.families import clip_classifier
        if not clip_classifier.available():
            print("! CLIP model not on disk — run bench.py --download-clip",
                  file=sys.stderr)
        else:
            fams["grid-clip"] = GridFamily(
                grid=3, target_count=(2, 4), tile_size=96, distortion=0.25)
    return fams


def distortion_sweep(runs: int, seed: int) -> list[dict]:
    """Measure text-family accuracy across distortion levels.

    This is the calibration table in the README: it shows where the
    challenge stops being a security control.
    """
    out = []
    for d in (0.0, 0.15, 0.3, 0.45, 0.6, 0.8, 1.0):
        fam = TextFamily()
        res = FamilyResult(family="text", runs=runs, exact_hits=0,
                           unit_hits=0, unit_total=0)
        import time
        for i in range(runs):
            ch = fam.generate(seed=seed + i, distortion=d)
            t0 = time.time()
            att = fam.solve(ch)
            dt = (time.time() - t0) * 1000
            ok = fam.score(ch, att)
            t = ch.truth["text"].upper()
            a = (att.answer or "").upper()
            uh = sum(1 for x, y in zip(t, a) if x == y)
            res.exact_hits += int(ok)
            res.unit_hits += uh
            res.unit_total += max(1, len(t))
            res.durations_ms.append(dt)
        out.append({"distortion": d,
                    "exact_rate": round(res.exact_rate, 4),
                    "unit_rate": round(res.unit_rate, 4),
                    "median_ms": round(res.median_ms, 1),
                    "expected_attempts": (round(res.expected_attempts, 2)
                                          if res.expected_attempts != float("inf")
                                          else None)})
        print(f"  distortion={d:<5} exact={res.exact_rate:>6.1%} "
              f"per-char={res.unit_rate:>6.1%} "
              f"ms={res.median_ms:>7.0f} tries={res.expected_attempts:.1f}")
    return out


def grid_scaling(runs: int, seed: int) -> list[dict]:
    """How the exact rate collapses as more target cells are required.

    This is the combinatorial arithmetic: per-cell accuracy stays roughly
    constant while full-challenge success falls off a cliff.
    """
    out = []
    for n_targets in (1, 2, 3, 4, 5, 6):
        fam = GridFamily(grid=3, target_count=(n_targets, n_targets),
                         tile_size=96, distortion=0.25)
        res = run_family(fam, runs=runs, seed=seed)
        out.append({"targets": n_targets,
                    "exact_rate": round(res.exact_rate, 4),
                    "unit_rate": round(res.unit_rate, 4),
                    "median_ms": round(res.median_ms, 1),
                    "expected_attempts": (round(res.expected_attempts, 2)
                                          if res.expected_attempts != float("inf")
                                          else None)})
        print(f"  targets={n_targets:<3} exact={res.exact_rate:>6.1%} "
              f"per-cell={res.unit_rate:>6.1%} tries={res.expected_attempts:.1f}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=30)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--families", default="text,grid,image")
    ap.add_argument("--sweep", action="store_true",
                    help="run the text distortion sweep")
    ap.add_argument("--scaling", action="store_true",
                    help="run the grid target-count scaling curve")
    ap.add_argument("--clip", action="store_true",
                    help="include the CLIP attacker (slow on CPU)")
    ap.add_argument("--download-clip", action="store_true",
                    help="download the CLIP ONNX model and exit")
    ap.add_argument("--json", metavar="PATH", help="write results as JSON")
    args = ap.parse_args()

    if args.download_clip:
        from app.families import clip_classifier
        print("downloading CLIP ONNX model (~300 MB)…")
        ok = clip_classifier.download(progress=True)
        print("ok" if ok else "download failed")
        return 0 if ok else 1

    payload = {"runs": args.runs, "seed": args.seed, "families": {}}

    if args.sweep:
        print("\n=== text distortion sweep ===")
        payload["sweep"] = distortion_sweep(args.runs, args.seed)
    if args.scaling:
        print("\n=== grid target-count scaling ===")
        payload["scaling"] = grid_scaling(args.runs, args.seed)

    wanted = [f.strip() for f in args.families.split(",") if f.strip()]
    fams = build_families(include_clip=args.clip)
    selected = {k: v for k, v in fams.items() if k in wanted}

    results = {}
    for name, fam in selected.items():
        print(f"\n=== {name} ===")
        res = run_family(fam, runs=args.runs, seed=args.seed, verbose=False)
        print(format_report({name: res}))
        results[name] = res

    payload["families"] = {k: v.to_dict() for k, v in results.items()}
    payload["details"] = {k: v.details for k, v in results.items()}

    if args.json:
        Path(args.json).write_text(json.dumps(payload, indent=2),
                                   encoding="utf-8")
        print(f"\nwrote {args.json}")

    print("\n" + format_report(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
