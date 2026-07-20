#!/usr/bin/env python3
"""Load the Steam corpus into `steam_titles`.

Reads the Kaggle export at data/raw/kaggle-games.json — a single ~863MB JSON
object keyed by appid — and streams it into Cloud SQL.

    python3 data/etl/load_corpus.py                    # load everything
    python3 data/etl/load_corpus.py --limit 500        # quick smoke test
    python3 data/etl/load_corpus.py --dry-run          # parse + report, no writes

⚠️ The dataset is NOT committed: it carries its own licence terms, separate
from this repository's AGPLv3. data/raw/ is gitignored; fetch it yourself.

## Streaming

The file is parsed incrementally with ijson rather than json.load(). Loading it
whole needs several GB of resident memory for a file we only ever walk once,
and it would rule out running this anywhere smaller than the dev laptop — the
component VMs are e2-smalls with 2GB.

## Estimates

⚠️ Every unit figure here is an ESTIMATE. Steam does not publish sales.

Two independent ones are stored, because they fail differently:

  * `estimated_units` — PRIMARY, read by scoring. review_count x
    BOXLEITER_MULTIPLIER. Chosen over the owner band because niche_hit_rate is
    a THRESHOLD comparison: this is monotonic in review count and degrades
    smoothly, so no title falls off a cliff for sitting near a band edge, and
    its error is a systematic multiplier that largely cancels in a relative
    within-niche measure.

  * `estimated_units_owner_band` — cross-check. The LOWER BOUND of SteamSpy's
    owner band. ⚠️ The lowest band is '0 - 20000', so this reads ZERO for
    everything in it — including titles with five figures of reviews. It is
    kept because it depends on SteamSpy's sampling rather than on review-
    leaving behaviour, so sharp disagreement is worth disclosing. It must NOT
    be used as a units figure on its own. See migration 0003.

Neither is revenue. The system reports units moved. See docs/SCORING.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

import ijson
import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from shared.config import Config  # noqa: E402

DEFAULT_SOURCE = REPO_ROOT / "data" / "raw" / "kaggle-games.json"

#: Reviews -> owners. The Boxleiter number is a rule of thumb from the indie
#: analytics community, not a measured constant, and it has drifted upward over
#: the years as review rates fell. 30 is a conservative mid-range choice.
BOXLEITER_MULTIPLIER = 30

#: Records below this are dropped. A game nobody reviewed is either unreleased,
#: a playtest, or so obscure that its owner band is noise — and every one of
#: them is a guaranteed non-hit that was never really a product, so leaving
#: them in would drag niche_hit_rate down for reasons unrelated to the market.
MIN_REVIEWS = 1

INSERT_SQL = """
    INSERT INTO steam_titles (
        app_id, name, release_date, price_usd,
        genres, tags, platforms, tag_votes,
        review_count, positive_ratio,
        estimated_units, estimated_units_owner_band, estimation_method
    ) VALUES (
        %(app_id)s, %(name)s, %(release_date)s, %(price_usd)s,
        %(genres)s, %(tags)s, %(platforms)s, %(tag_votes)s,
        %(review_count)s, %(positive_ratio)s,
        %(estimated_units)s, %(estimated_units_owner_band)s, %(estimation_method)s
    )
    ON CONFLICT (app_id) DO UPDATE SET
        name = EXCLUDED.name,
        release_date = EXCLUDED.release_date,
        price_usd = EXCLUDED.price_usd,
        genres = EXCLUDED.genres,
        tags = EXCLUDED.tags,
        platforms = EXCLUDED.platforms,
        tag_votes = EXCLUDED.tag_votes,
        review_count = EXCLUDED.review_count,
        positive_ratio = EXCLUDED.positive_ratio,
        estimated_units = EXCLUDED.estimated_units,
        estimated_units_owner_band = EXCLUDED.estimated_units_owner_band,
        estimation_method = EXCLUDED.estimation_method
"""


def parse_release_date(raw: str) -> date | None:
    """The export is inconsistent: 'Sep 5, 2019', 'Sep 2019', '2019', ''."""
    raw = (raw or "").strip()
    if not raw:
        return None
    for fmt in ("%b %d, %Y", "%d %b, %Y", "%b %Y", "%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_owner_band(raw: str) -> int | None:
    """'100000 - 200000' -> 100000. Lower bound; see the module docstring."""
    raw = (raw or "").strip()
    if not raw:
        return None
    low = raw.split("-")[0].strip().replace(",", "")
    try:
        return int(low)
    except ValueError:
        return None


def is_probably_not_a_game(name: str) -> bool:
    """Playtests and demos are separate store entries, not products.

    Matched on a trailing suffix rather than a substring: plenty of real games
    legitimately contain 'demo' somewhere in the title.
    """
    lowered = (name or "").strip().lower()
    return lowered.endswith(("playtest", " demo", "- demo", "beta test"))


def transform(app_id_raw: str, rec: dict[str, Any]) -> dict[str, Any] | None:
    """One source record -> one row, or None if it should be dropped."""
    try:
        app_id = int(app_id_raw)
    except (TypeError, ValueError):
        return None

    name = (rec.get("name") or "").strip()
    if not name or is_probably_not_a_game(name):
        return None

    positive = rec.get("positive") or 0
    negative = rec.get("negative") or 0
    review_count = positive + negative
    if review_count < MIN_REVIEWS:
        return None

    platforms = [
        label
        for key, label in (("windows", "Windows"), ("mac", "macOS"), ("linux", "Linux"))
        if rec.get(key)
    ]

    # tags arrives as {tag: votes}. The array powers GIN containment filtering;
    # the JSONB preserves the weights for ranking whatever survives the filter.
    tag_votes = rec.get("tags") or {}
    if isinstance(tag_votes, list):  # a few records use a flat list
        tag_votes = {t: 0 for t in tag_votes}

    owners = parse_owner_band(rec.get("estimated_owners", ""))

    return {
        "app_id": app_id,
        "name": name,
        "release_date": parse_release_date(rec.get("release_date", "")),
        "price_usd": rec.get("price"),
        "genres": rec.get("genres") or [],
        "tags": list(tag_votes.keys()),
        "platforms": platforms,
        "tag_votes": json.dumps(tag_votes) if tag_votes else None,
        "review_count": review_count,
        "positive_ratio": round(positive / review_count, 3),
        "estimated_units": review_count * BOXLEITER_MULTIPLIER,
        "estimated_units_owner_band": owners,
        "estimation_method": (
            f"boxleiter-{BOXLEITER_MULTIPLIER}x (primary); "
            "steamspy-owners-band-lower (cross-check)"
        ),
    }


def stream_records(path: Path) -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (appid, record) without holding the whole file in memory."""
    with path.open("rb") as fh:
        yield from ijson.kvitems(fh, "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--limit", type=int, help="stop after N loaded rows")
    parser.add_argument("--batch", type=int, default=1000)
    parser.add_argument(
        "--dry-run", action="store_true", help="parse and report; write nothing"
    )
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"❌ {args.source} not found", file=sys.stderr)
        print("   The dataset is not committed — see the module docstring.", file=sys.stderr)
        return 1

    print(f"📦 Reading {args.source} ({args.source.stat().st_size / 1e6:.0f} MB)")
    if args.dry_run:
        print("🧪 Dry run — nothing will be written")

    seen = loaded = 0
    # Counted rather than silently discarded: a corpus that quietly shrank is
    # indistinguishable from one that loaded correctly.
    dropped = {"no_reviews_or_junk": 0, "unparseable": 0}
    batch: list[dict[str, Any]] = []

    conn = None if args.dry_run else psycopg.connect(Config.from_env().dsn)
    try:
        cur = conn.cursor() if conn else None

        for app_id_raw, rec in stream_records(args.source):
            seen += 1
            if not isinstance(rec, dict):
                dropped["unparseable"] += 1
                continue

            row = transform(app_id_raw, rec)
            if row is None:
                dropped["no_reviews_or_junk"] += 1
                continue

            batch.append(row)
            loaded += 1

            if len(batch) >= args.batch:
                if cur:
                    cur.executemany(INSERT_SQL, batch)
                    conn.commit()
                batch.clear()
                print(f"   … {seen:,} read / {loaded:,} loaded", end="\r", flush=True)

            if args.limit and loaded >= args.limit:
                break

        if batch and cur:
            cur.executemany(INSERT_SQL, batch)
            conn.commit()
    finally:
        if conn:
            conn.close()

    total_dropped = sum(dropped.values())
    print(f"\n✅ {loaded:,} titles loaded from {seen:,} records")
    print(f"⚠️ {total_dropped:,} dropped — {dropped['no_reviews_or_junk']:,} no reviews "
          f"/ playtest / demo, {dropped['unparseable']:,} unparseable")
    print(f"📝 Units are ESTIMATES (boxleiter-{BOXLEITER_MULTIPLIER}x primary, "
          "owner band as cross-check), never audited sales.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
