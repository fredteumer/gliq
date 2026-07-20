#!/usr/bin/env python3
"""Does the scoring actually separate winners from slop?

    python3 data/etl/validate_scoring.py
    python3 data/etl/validate_scoring.py --floor-sweep

⚠️ Imports the REAL rules from components/scoring/rules.py. An earlier version
reimplemented them here, which meant calibration measured a copy rather than
production — and the two had already diverged: a pitch carries no tag votes, so
the production overlap uniform-weights the pitch side and normalises both,
which is a different scale from the raw-vote formula the constants were first
fitted against.

Two cohorts, scored identically:

  * RANDOM   — titles sampled uniformly. Mostly slop, by construction: that is
               what Steam mostly is, and a publisher's inbox looks similar.
  * WINNERS  — titles in the top units decile. What "wins big" looks like.

⚠️ Grade thresholds are NOT fitted to make this distribution look nice. Most
pitches SHOULD fail — the base rate of good acquisitions is genuinely low, and
normalising that away would be grade inflation. The only things being checked
are that winners separate from random, and that the top of the scale is
REACHABLE so exceptional pitches stay distinguishable.
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
from datetime import date
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from components.scoring import rules  # noqa: E402
from shared.config import Config  # noqa: E402
from shared.schemas import PitchProfile, PriceTier  # noqa: E402

COHORT = 120
RANDOM_SEED = 20260720
#: Fixed so recency does not shift the numbers between runs.
AS_OF = date(2026, 7, 20)


def pct(vals, q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(int(q / 100 * len(s)), len(s) - 1)]


def as_profile(row: dict) -> PitchProfile:
    """Use a real title as a stand-in pitch, described in its own terms.

    ⚠️ These carry COMPLETE tag sets. A real design document extracts sparser
    and will match worse, so any floor fitted here is biased high.
    """
    genres = list(row["genres"] or [])
    return PitchProfile(
        title=row["name"],
        primary_genre=genres[0] if genres else None,
        sub_genres=genres[1:],
        tags=list(row["tags"] or []),
        price_tier=PriceTier.from_price(row["price_usd"]),
        target_platforms=list(row["platforms"] or []),
        extracted_by="synthetic",
    )


def load_corpus() -> list[dict]:
    with psycopg.connect(Config.from_env().dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT app_id, name, genres, tags, platforms, release_date, price_usd,
                   review_count, positive_ratio, estimated_units, estimation_method,
                   coalesce(tag_votes, '{}'::jsonb)
            FROM steam_titles WHERE cardinality(tags) > 0
            """
        )
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    for r in rows:
        r["tag_votes"] = {k: int(v) for k, v in (r["coalesce"] or {}).items()}
        r["price_usd"] = float(r["price_usd"]) if r["price_usd"] is not None else None
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--floor-sweep", action="store_true",
                        help="report comps retained at a range of floors and exit")
    args = parser.parse_args()

    print("📦 Loading corpus...")
    corpus = load_corpus()
    print(f"✅ {len(corpus):,} titles\n")

    rng = random.Random(RANDOM_SEED)
    cutoff = pct([c["estimated_units"] or 0 for c in corpus], 90)
    winners_pool = [c for c in corpus if (c["estimated_units"] or 0) >= cutoff]

    if args.floor_sweep:
        # The floor must be re-fitted whenever the similarity formula changes.
        print("SIMILARITY_FLOOR sweep — comps retained per pitch")
        print(f"{'floor':>7} {'median':>9} {'p10':>9}  {'% pitches <5':>13}")
        sample = rng.sample(corpus, 60)
        sims_by_pitch = []
        for src in sample:
            profile = as_profile(src)
            sims_by_pitch.append([
                rules.similarity(profile, c, AS_OF)
                for c in corpus
                if c["app_id"] != src["app_id"] and (set(c["tags"] or []) & set(profile.tags))
            ])
        for floor in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
            counts = [sum(1 for s in sims if s >= floor) for sims in sims_by_pitch]
            thin = 100 * sum(1 for c in counts if c < rules.MIN_COMPS_WARN) / len(counts)
            print(f"{floor:>7.2f} {statistics.median(counts):>9,.0f} "
                  f"{pct(counts,10):>9,.0f}  {thin:>12.1f}%")
        allsims = [s for sims in sims_by_pitch for s in sims[::29]]
        print(f"\n  similarity distribution: p50={pct(allsims,50):.3f} "
              f"p90={pct(allsims,90):.3f} p99={pct(allsims,99):.3f}")
        return 0

    cohorts = {
        "RANDOM": rng.sample(corpus, COHORT),
        "WINNERS": rng.sample(winners_pool, min(COHORT, len(winners_pool))),
    }
    print(f"top-decile cutoff: {cutoff:,.0f} units ({len(winners_pool):,} titles)")
    print(f"floor={rules.SIMILARITY_FLOOR}  max_comps={rules.MAX_COMPS}\n")

    results = {}
    for label, cohort in cohorts.items():
        scored = []
        for src in cohort:
            profile = as_profile(src)
            others = (c for c in corpus if c["app_id"] != src["app_id"])
            scored.append(rules.score_pitch(profile, others, AS_OF))
        results[label] = scored

    print("=" * 74)
    print(f"{'cohort':<9} {'n':>4} {'p10':>7} {'p25':>7} {'p50':>7} {'p75':>7} {'p90':>7} {'max':>7}")
    print("=" * 74)
    for label, scored in results.items():
        s = [r.score for r in scored]
        print(f"{label:<9} {len(s):>4} {pct(s,10):>7.1f} {pct(s,25):>7.1f} {pct(s,50):>7.1f} "
              f"{pct(s,75):>7.1f} {pct(s,90):>7.1f} {max(s):>7.1f}")

    print("\nSub-score medians:")
    print(f"{'cohort':<9} {'hit':>8} {'potential':>10} {'price':>7} {'comps':>7}")
    for label, scored in results.items():
        print(f"{label:<9} "
              f"{statistics.median([r.sub_scores.niche_hit_rate for r in scored]):>8.1f} "
              f"{statistics.median([r.sub_scores.sales_potential for r in scored]):>10.1f} "
              f"{statistics.median([r.sub_scores.price_alignment for r in scored]):>7.1f} "
              f"{statistics.median([r.comps_considered for r in scored]):>7.0f}")

    print("\nGrade distribution:")
    for label, scored in results.items():
        counts = {g: sum(1 for r in scored if r.grade == g) for g in "ABCDF"}
        total = len(scored)
        bars = "  ".join(f"{g}:{counts[g]:>3} ({100*counts[g]/total:>4.1f}%)" for g in "ABCDF")
        print(f"  {label:<8} {bars}")

    rnd = [r.score for r in results["RANDOM"]]
    win = [r.score for r in results["WINNERS"]]
    print(f"\n🎯 separation: winners p50 {pct(win,50):.1f} vs random p50 {pct(rnd,50):.1f} "
          f"(+{pct(win,50)-pct(rnd,50):.1f})")
    print(f"   ceiling: best winner scored {max(win):.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
