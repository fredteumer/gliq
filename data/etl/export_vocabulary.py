#!/usr/bin/env python3
"""Export the extraction vocabulary from the corpus.

    python3 data/etl/export_vocabulary.py
    python3 data/etl/export_vocabulary.py --min-count 25
    python3 data/etl/export_vocabulary.py --dry-run

Component A extracts a PitchProfile by matching document text against the tags
and genres that ACTUALLY OCCUR in `steam_titles`. This writes that vocabulary
out to a JSON artifact so the extractor needs neither a database nor a network
at runtime — which is what keeps the intake VM free of psycopg and free of a
route to Cloud SQL.

## Why the strings are copied byte-for-byte

Every comparison downstream is exact, case-sensitive string equality:
`rules.tag_overlap` intersects raw `set[str]`, and `corpus.fetch_candidates`
prefilters with the Postgres `&&` array-overlap operator. A tag that differs
from the corpus by one character does not merely score low — it fails to
RETRIEVE, so the pitch is scored against the wrong comp set or none at all.

⚠️ So this script canonicalises nothing. Any normalisation belongs in
`components/intake/vocabulary.py`, where it is applied to the INCOMING document
text to find a match, and the value emitted is always the corpus's own spelling.
Normalising here instead would silently diverge the two vocabularies and zero
out `tag_overlap` with no error anywhere.

## On --min-count

A tag applied to a handful of titles cannot support a comp set, and the long
tail is where false matches live (a stray tag that happens to be a common
English word). The default keeps everything; raise it if extraction turns out
noisy. Counts ship in the artifact either way, so the extractor can rank by
corpus frequency — that is how `primary_genre` is chosen from several matches.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import psycopg

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from shared.config import Config  # noqa: E402

DEFAULT_TARGET = REPO_ROOT / "components" / "intake" / "vocabulary.json"

#: Distinct tags with their corpus frequency. Ordered most-common-first so the
#: artifact is readable and the extractor can break ties without re-sorting.
TAGS_SQL = """
    SELECT t AS value, count(*) AS n
    FROM steam_titles, unnest(tags) t
    GROUP BY t
    HAVING count(*) >= %(min_count)s
    ORDER BY n DESC, value
"""

GENRES_SQL = """
    SELECT g AS value, count(*) AS n
    FROM steam_titles, unnest(genres) g
    GROUP BY g
    HAVING count(*) >= %(min_count)s
    ORDER BY n DESC, value
"""

#: Per-tag genre affinity: of the titles carrying tag T, what share sit in
#: genre G.
#:
#: This exists because genre words seldom appear literally in a design
#: document. "A free-to-play 100-player battle royale shooter" names no Steam
#: genre at all — `Shooter` and `Battle Royale` are TAGS — so a literal genre
#: match finds nothing and `primary_genre` comes back empty, which is a
#: load-bearing field and hard-caps the grade at D.
#:
#: The corpus already knows the answer: 92% of `Shooter` titles are filed under
#: Action, 83% of `Battle Royale` titles likewise. Baking that mapping in here
#: keeps the inference DERIVED FROM DATA rather than a hand-written table of
#: someone's assumptions, and keeps it out of the runtime — the extractor needs
#: no database to use it.
AFFINITY_SQL = """
    WITH tag_totals AS (
        SELECT t AS tag, count(*) AS n
        FROM steam_titles, unnest(tags) t
        GROUP BY t
        HAVING count(*) >= %(min_count)s
    ),
    pairs AS (
        SELECT t AS tag, g AS genre, count(*) AS n
        FROM steam_titles, unnest(tags) t, unnest(genres) g
        GROUP BY t, g
    )
    SELECT p.tag, p.genre, p.n::float / tt.n AS share
    FROM pairs p JOIN tag_totals tt USING (tag)
    WHERE p.n::float / tt.n >= %(floor)s
    ORDER BY p.tag, share DESC
"""

#: Below this share the association is noise rather than a signal. 0.25 keeps
#: `Metroidvania -> RPG` (26%) and drops the long tail of incidental genres.
AFFINITY_FLOOR = 0.25

#: How many genres to keep per tag. Corpus titles carry 2-3 genres, so four
#: candidates is already more than any single title has.
AFFINITY_TOP_N = 4

#: Steam reports exactly these three. Not a query: `platforms` is a fixed set
#: written by load_corpus.py, and a pitch naming "Nintendo Switch" or "consoles"
#: has nowhere to land — the extractor maps what it can onto these and drops the
#: rest rather than emitting a value no corpus row can match.
PLATFORMS_SQL = """
    SELECT p AS value, count(*) AS n
    FROM steam_titles, unnest(platforms) p
    GROUP BY p
    ORDER BY n DESC, value
"""


def fetch(conn: psycopg.Connection, sql: str, min_count: int) -> list[dict[str, object]]:
    with conn.cursor() as cur:
        cur.execute(sql, {"min_count": min_count})
        return [{"value": value, "count": n} for value, n in cur.fetchall()]


def fetch_affinity(conn: psycopg.Connection, min_count: int) -> dict[str, list[dict[str, object]]]:
    """Tag -> its top genres by co-occurrence share, most associated first."""
    affinity: dict[str, list[dict[str, object]]] = {}
    with conn.cursor() as cur:
        cur.execute(AFFINITY_SQL, {"min_count": min_count, "floor": AFFINITY_FLOOR})
        for tag, genre, share in cur.fetchall():
            entries = affinity.setdefault(tag, [])
            if len(entries) < AFFINITY_TOP_N:
                entries.append({"genre": genre, "share": round(float(share), 3)})
    return affinity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument(
        "--min-count",
        type=int,
        default=1,
        help="drop vocabulary entries occurring in fewer than N titles",
    )
    parser.add_argument("--dry-run", action="store_true", help="report but do not write")
    args = parser.parse_args()

    config = Config.from_env()

    print("📦 Exporting extraction vocabulary...")
    try:
        with psycopg.connect(config.dsn, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM steam_titles")
                titles = cur.fetchone()[0]
            tags = fetch(conn, TAGS_SQL, args.min_count)
            genres = fetch(conn, GENRES_SQL, args.min_count)
            platforms = fetch(conn, PLATFORMS_SQL, 1)
            affinity = fetch_affinity(conn, args.min_count)
    except psycopg.OperationalError as exc:
        print(f"❌ could not reach Cloud SQL at {config.db_host}: {exc}", file=sys.stderr)
        print("   Reaching it from a laptop needs `tailscale up --accept-routes`.", file=sys.stderr)
        return 1

    if not titles:
        print("❌ steam_titles is empty — run data/etl/load_corpus.py first", file=sys.stderr)
        return 1

    print(f"✅ {titles:,} titles")
    print(f"✅ {len(tags):,} tags, {len(genres):,} genres, {len(platforms)} platforms")
    print(f"✅ genre affinity for {len(affinity):,} tags (share >= {AFFINITY_FLOOR:.0%})")
    if args.min_count > 1:
        print(f"⚠️ entries occurring in fewer than {args.min_count} titles were dropped")

    artifact = {
        "_comment": (
            "Generated by data/etl/export_vocabulary.py — do not hand-edit. "
            "Strings are the corpus's own spelling and are matched with exact, "
            "case-sensitive equality downstream."
        ),
        "corpus_titles": titles,
        "min_count": args.min_count,
        "affinity_floor": AFFINITY_FLOOR,
        "tags": tags,
        "genres": genres,
        "platforms": platforms,
        "genre_affinity": affinity,
    }

    if args.dry_run:
        print("🧪 Dry run — nothing written")
        for label, rows in (("tags", tags), ("genres", genres)):
            head = ", ".join(f"{r['value']} ({r['count']:,})" for r in rows[:5])
            print(f"   {label}: {head} ...")
        return 0

    args.target.write_text(json.dumps(artifact, indent=2, ensure_ascii=False) + "\n")
    size_kb = args.target.stat().st_size / 1024
    print(f"✅ wrote {args.target.relative_to(REPO_ROOT)} ({size_kb:.0f} KB)")
    print("📝 This artifact IS committed — the extractor must work with no DB and no network.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
