#!/usr/bin/env python3
"""Calibrate the scoring constants against the real corpus.

    python3 data/etl/calibrate.py

docs/SCORING.md defines the rules; several of its constants were reasoned about
directionally and marked ⏳ untuned. This measures the distributions they are
supposed to sit inside and prints recommended values.

## Method

Synthetic pitches: a random sample of REAL titles is used as stand-in pitches,
each described by its own genre/tags/platforms/price. That gives hundreds of
plausible, correctly-vocabularied pitches without hand-writing any — and since
the corpus IS the population being scored against, the resulting distributions
are the ones production will actually see.

⚠️ The sampled title is excluded from its own comp set. Leaving it in would let
every pitch match itself at similarity 1.0 and quietly shift every statistic.

## What cannot be calibrated here

The sub-score WEIGHTS. Calibration can show what a metric's distribution looks
like; it cannot say how much market saturation should matter relative to price
fit. That is a judgement about what an acquisitions team values, and there is
no ground truth in this corpus to fit against — no labelled good and bad
greenlight decisions. Those stay a documented editorial choice.
"""

from __future__ import annotations

import random
import math
import statistics
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from shared.config import Config  # noqa: E402

SUCCESS_UNITS = 10_000
SAMPLE_PITCHES = 300
RANDOM_SEED = 20260720  # fixed so a re-run reproduces the same recommendations

# Weights from docs/SCORING.md, step 1.
#
# ⚠️ Rebalanced after the first calibration run. Genre was 0.40 and scored a
# flat 1.0 on any match, so genre alone cleared every floor <= 0.40 regardless
# of tags — the floor never bound, and MAX_COMPS was silently doing all the
# selection. Genres are also enormous ("Action" is 35,470 titles) where tags
# are specific (452 of them), so the discriminating signal now carries the
# weight it earns.
W_GENRE, W_TAGS, W_SUBGENRE, W_PLATFORM = 0.20, 0.55, 0.15, 0.10


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def weighted_tag_overlap(pitch_tags: dict[str, int], cand_tags: dict[str, int]) -> float:
    """Jaccard weighted by how strongly each tag is voted.

    Steam tags carry vote counts, so a title tagged 'Turn-Based Strategy' 86
    times genuinely is one, while a single stray vote is noise. Plain Jaccard
    treats those identically, which is how ubiquitous tags like 'Indie' come to
    dominate a comp set. Weighting by the shared votes fixes that without
    needing a stopword list.
    """
    if not pitch_tags or not cand_tags:
        return 0.0
    shared = set(pitch_tags) & set(cand_tags)
    if not shared:
        return 0.0
    inter = sum(min(pitch_tags[t], cand_tags[t]) for t in shared)
    union = sum(pitch_tags.values()) + sum(cand_tags.values()) - inter
    return inter / union if union else 0.0


def top_n_share(units: list[int], n: int = 10) -> float:
    """Fraction of a niche's units held by its biggest n titles.

    Replaces Gini for competitive_headroom. Gini measured 0.954-0.970 across
    every niche — dominated by the universal long tail of near-zero sellers,
    which exists everywhere and so drowns out the differences. Top-n share
    ignores the tail and asks the decision-relevant question directly: is this
    space open, or held by a handful of incumbents?
    """
    total = sum(units)
    if total <= 0:
        return 0.0
    return sum(sorted(units, reverse=True)[:n]) / total


def similarity(pitch: dict, cand: dict) -> float:
    """Mirrors docs/SCORING.md step 1, minus recency (measured separately)."""
    pg = pitch["primary_genre"]
    cand_genres = cand["genres_set"]
    if pg and pg in cand_genres:
        genre = 1.0
    elif pg and cand_genres:
        genre = 0.0
    else:
        genre = 0.0

    return (
        W_GENRE * genre
        + W_TAGS * weighted_tag_overlap(pitch["tag_votes"], cand["tag_votes"])
        + W_SUBGENRE * jaccard(pitch["genres_set"], cand_genres)
        + W_PLATFORM * jaccard(pitch["platforms_set"], cand["platforms_set"])
    )


def gini(values: list[float]) -> float:
    """0 = perfectly even, 1 = one title takes everything."""
    vals = sorted(v for v in values if v is not None and v >= 0)
    n = len(vals)
    total = sum(vals)
    if n < 2 or total == 0:
        return 0.0
    cumulative = sum((i + 1) * v for i, v in enumerate(vals))
    return (2 * cumulative) / (n * total) - (n + 1) / n


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(p / 100 * len(s)), len(s) - 1)
    return s[idx]


def main() -> int:
    print("📦 Loading corpus...")
    with psycopg.connect(Config.from_env().dsn) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT app_id, genres, tags, platforms, estimated_units, price_usd,
                   coalesce(tag_votes, '{}'::jsonb)
            FROM steam_titles
            WHERE cardinality(tags) > 0
            """
        )
        rows = cur.fetchall()

    corpus = [
        {
            "app_id": r[0],
            "genres_set": set(r[1] or []),
            "tags_set": set(r[2] or []),
            "platforms_set": set(r[3] or []),
            "units": r[4] or 0,
            "price": float(r[5]) if r[5] is not None else None,
            "tag_votes": {k: int(v) for k, v in (r[6] or {}).items()},
        }
        for r in rows
    ]
    print(f"✅ {len(corpus):,} titles with tags\n")

    rng = random.Random(RANDOM_SEED)
    sample = rng.sample(corpus, min(SAMPLE_PITCHES, len(corpus)))

    # ---------------------------------------------------------------- floor
    # How many comps survive each candidate similarity floor? The floor should
    # yield enough comps for the statistics to mean anything, without dragging
    # in titles that merely share a ubiquitous tag like "Indie".
    print("=" * 68)
    print("SIMILARITY_FLOOR — comps retained per pitch")
    print("=" * 68)
    print(f"{'floor':>7} {'median':>9} {'p10':>9} {'p90':>9}  {'% pitches <5 comps':>19}")

    floors = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.50]
    counts_by_floor: dict[float, list[int]] = {f: [] for f in floors}
    all_sims: list[float] = []

    for pitch_src in sample:
        pitch = {
            "primary_genre": next(iter(pitch_src["genres_set"]), None),
            "genres_set": pitch_src["genres_set"],
            "tags_set": pitch_src["tags_set"],
            "tag_votes": pitch_src["tag_votes"],
            "platforms_set": pitch_src["platforms_set"],
        }
        # Prefilter on tag overlap, exactly as production will via the GIN index.
        sims = [
            similarity(pitch, c)
            for c in corpus
            if c["app_id"] != pitch_src["app_id"] and (c["tags_set"] & pitch["tags_set"])
        ]
        all_sims.extend(sims[::37])  # thinned; only the shape is needed
        for f in floors:
            counts_by_floor[f].append(sum(1 for s in sims if s >= f))

    for f in floors:
        c = counts_by_floor[f]
        thin = 100 * sum(1 for x in c if x < 5) / len(c)
        print(f"{f:>7.2f} {statistics.median(c):>9,.0f} {pct(c,10):>9,.0f} "
              f"{pct(c,90):>9,.0f}  {thin:>18.1f}%")

    print(f"\n  similarity distribution: p50={pct(all_sims,50):.3f} "
          f"p90={pct(all_sims,90):.3f} p99={pct(all_sims,99):.3f}")

    # ---------------------------------------------------- real comp sets
    # ⚠️ Everything below is measured on the comp set PRODUCTION will build:
    # similarity >= floor, then top MAX_COMPS. An earlier version used a
    # ">=2 shared tags" proxy, which is thousands of loosely-related titles
    # rather than the 50 closest — a different population with a different
    # unit distribution, so any constant fitted to it would be fitted to the
    # wrong thing.
    RECO_FLOOR, MAX_COMPS = 0.45, 50
    print("\n" + "=" * 68)
    print(f"REAL COMP SETS (floor={RECO_FLOOR}, top {MAX_COMPS})")
    print("=" * 68)

    comp_sets: list[list[dict]] = []
    for pitch_src in sample[:150]:
        pitch = {
            "primary_genre": next(iter(pitch_src["genres_set"]), None),
            "genres_set": pitch_src["genres_set"],
            "tags_set": pitch_src["tags_set"],
            "tag_votes": pitch_src["tag_votes"],
            "platforms_set": pitch_src["platforms_set"],
        }
        scored = [
            (similarity(pitch, c), c)
            for c in corpus
            if c["app_id"] != pitch_src["app_id"] and (c["tags_set"] & pitch["tags_set"])
        ]
        kept = sorted((s for s in scored if s[0] >= RECO_FLOOR), key=lambda x: -x[0])[:MAX_COMPS]
        if len(kept) >= 10:
            comp_sets.append([c for _, c in kept])

    sizes = [len(cs) for cs in comp_sets]
    print(f"  usable comp sets: {len(comp_sets)}/150   median size {statistics.median(sizes):.0f}")

    # ------------------------------------------------------------- headroom
    print("\n" + "=" * 68)
    print("competitive_headroom — top-10 share within the REAL comp set")
    print("=" * 68)
    shares = [top_n_share([c["units"] for c in cs], 10) for cs in comp_sets]
    if shares:
        for q in (5, 25, 50, 75, 95):
            print(f"  p{q:<3} {pct(shares,q):.3f}")
        print(f"  → rescale 100*(1-share) from observed {pct(shares,5):.3f}..{pct(shares,95):.3f}")

    # ------------------------------------------------------------ potential
    print("\n" + "=" * 68)
    print("sales_potential — p90 units among winners in the REAL comp set")
    print("=" * 68)
    p90s = []
    for cs in comp_sets:
        winners = [c["units"] for c in cs if c["units"] >= SUCCESS_UNITS]
        if len(winners) >= 5:
            p90s.append(pct(winners, 90))
    if p90s:
        print(f"  niches measured: {len(p90s)}")
        for q in (5, 25, 50, 75, 95):
            print(f"  p{q:<3} {pct(p90s,q):>12,.0f} units")
        print(f"  → log bracket {pct(p90s,5):,.0f} .. {pct(p90s,95):,.0f} "
              f"({pct(p90s,95)/max(pct(p90s,5),1):.1f}x spread)")

    # ------------------------------------------------------- hit rate spread
    # The core signal — worth confirming it varies across real comp sets.
    print("\n" + "=" * 68)
    print("niche_hit_rate — across REAL comp sets")
    print("=" * 68)
    rates = [100*sum(1 for c in cs if c["units"] >= SUCCESS_UNITS)/len(cs) for cs in comp_sets]
    if rates:
        for q in (5, 25, 50, 75, 95):
            print(f"  p{q:<3} {pct(rates,q):>6.1f}%")

    # ---------------------------------------------------------------- price
    print("\n" + "=" * 68)
    print("price_alignment — price distribution of successful titles")
    print("=" * 68)
    prices = [c["price"] for c in corpus if c["units"] >= SUCCESS_UNITS and c["price"] is not None]
    for p in (25, 50, 75, 90):
        print(f"  p{p:<3} ${pct(prices,p):>7,.2f}")
    print(f"  free-to-play share: {100*sum(1 for p in prices if p == 0)/len(prices):.1f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
