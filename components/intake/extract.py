"""Deterministic extraction of a PitchProfile from a design document.

⚠️ Pure functions only. No database, no network, no Pub/Sub — the vocabulary is
a committed artifact, so this is reachable and testable without any of the
transport above it, exactly like `components/scoring/rules.py`.

This is the Part 1 extractor. It is NOT a placeholder for the LLM path: per
AGENTS.md it is the reproducible CONTROL that the Part 2 LLM implementation gets
measured against. Both emit the same `PitchProfile`.

## What it does

Matches document text against the 452 tags / 33 genres that actually occur in
`steam_titles` (➡️ `vocabulary.py`), then reads price, platforms, title and
mechanics out of the document's structure.

## What it cannot do

It matches vocabulary; it does not understand prose. It cannot tell a mechanic
the pitch HAS from one it says it avoids — "no microtransactions" contributes
`Microtransactions` if that were a tag, because negation is invisible to a
dictionary match. It cannot judge whether a claimed genre is plausible. Those
are Part 2 jobs, and the gap between this and the LLM path is the measurement
the project is set up to make.

⚠️ Extraction must never raise on a thin document. `PitchProfile` is optional by
design and decisiveness is enforced in scoring — a required field here would
mean a sparse pitch dies in Component A and never gets a report at all.
"""

from __future__ import annotations

import re

from components.intake import vocabulary as vocab
from shared.schemas import PitchProfile, PriceTier

#: Genre-column values that are not genres. They are business-model or
#: lifecycle labels, and Steam files them under genres anyway. Any of them can
#: legitimately land in `sub_genres` — "this is a free-to-play title" is real
#: information — but as a PRIMARY genre they say nothing about what the game is,
#: and `Indie` alone matches 59,379 corpus titles, which is a comp set so broad
#: it cannot discriminate.
NON_GENRE_LABELS = frozenset({"Indie", "Early Access", "Free To Play"})

#: A genre the document names outright outweighs one inferred from tags. Set
#: above 1.0 (the maximum any single affinity share can contribute) so a single
#: explicit "this is an RPG" beats a pile of weak tag associations, but not so
#: high that one incidental word overrides the whole tag set.
LITERAL_GENRE_WEIGHT = 1.5

#: Corpus titles carry 2-3 genres. Emitting more than this makes the sub-genre
#: Jaccard in `rules.similarity` worse, not better — the denominator is the
#: UNION, so an over-broad pitch is penalised against every comp.
MAX_SUB_GENRES = 3

#: Below this, a lone number near a currency symbol is a price rather than a
#: budget. Design documents quote funding asks ("$850,000", "$4,200,000") in the
#: same prose as the price, and the funding figure usually comes FIRST — a naive
#: "first $-amount" rule reads the budget as the price on both sample pitches.
MAX_PLAUSIBLE_PRICE = 200.0

#: Words whose presence near a figure marks it as the asking price.
PRICE_CONTEXT = re.compile(
    r"(price|priced|pricing|msrp|retail|costs?|sells?|launch(?:es)? at|\bat\b)",
    re.IGNORECASE,
)

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_FREE = re.compile(r"free[-\s]?to[-\s]?play|\bf2p\b|\bfree\b", re.IGNORECASE)

#: Headings whose bullets are the game's mechanics. `core_mechanics` never
#: enters the scoring arithmetic, but it is one of the six COVERAGE_FIELDS, so
#: an empty list costs the pitch a sixth of its completeness for no reason.
MECHANIC_HEADINGS = re.compile(r"^#{1,6}\s*.*\b(mechanic|gameplay|feature|system)", re.IGNORECASE)

_H1 = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_HEADING = re.compile(r"^#{1,6}\s+")

#: ⚠️ The trailing space is load-bearing. Without it `*Hollow Reef* is a
#: hand-drawn metroidvania...` reads as a bullet, and the concept paragraph —
#: the one paragraph that actually describes the game — is skipped in favour of
#: whatever prose comes next. Both sample pitches open their concept section
#: with an italicised title, so this fired on 2 of 2.
_BULLET = re.compile(r"^\s*(?:[-+]|\*(?!\*))\s+(.+)$", re.MULTILINE)

#: A bolded key/value line: "**Studio:** Tidewrack Games".
_METADATA_LINE = re.compile(r"^\*\*[^*]+:\*\*")

#: Shorter than this is a caption or a stray fragment, not a summary.
MIN_SUMMARY_CHARS = 80

#: Markdown emphasis around a run of text. Stripped from the summary because it
#: is rendered into a report, not back into markdown. Asterisks only —
#: underscore emphasis is rare in these documents and the pattern would eat
#: snake_case identifiers.
_EMPHASIS = re.compile(r"\*{1,2}([^*]+)\*{1,2}")

#: Boilerplate to strip off an H1 so the title is the game's name alone.
_TITLE_NOISE = re.compile(
    r"\s*[—–\-:]\s*(game\s+)?design\s+document\s*$|\s*[—–\-:]\s*gdd\s*$|\s*[—–\-:]\s*pitch\s*$",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------
# Vocabulary matching
# --------------------------------------------------------------------------


def _phrases(tokens: list[str]) -> list[str]:
    """Every 1..MAX_PHRASE_WORDS run of consecutive tokens.

    ⚠️ Overlapping and non-consuming: "action adventure" yields `action`,
    `adventure` AND `action adventure`, so all three can match.

    The obvious alternative — longest-match-wins, consuming the tokens it used —
    was rejected on evidence. Corpus titles tagged with a compound overwhelmingly
    carry its constituents too: `Action-Adventure` also carries `Action` 78% and
    `Adventure` 77% of the time, `Turn-Based Strategy` carries `Strategy` 85%,
    `Roguelike Deckbuilder` carries `Deckbuilding` 87%. Consuming the tokens
    would emit only the compound and lose the overlap with the very comps the
    pitch most resembles.
    """
    out: list[str] = []
    for size in range(1, vocab.MAX_PHRASE_WORDS + 1):
        for start in range(len(tokens) - size + 1):
            out.append(" ".join(tokens[start : start + size]))
    return out


def _matches(phrases: list[str], lookup) -> dict[str, int]:
    """Corpus values found, mapped to how many times the document mentioned them."""
    found: dict[str, int] = {}
    for phrase in phrases:
        value = lookup(phrase)
        if value is not None:
            found[value] = found.get(value, 0) + 1
    return found


# --------------------------------------------------------------------------
# Structural fields
# --------------------------------------------------------------------------


def extract_title(text: str) -> str | None:
    """The game's name, from the first H1.

    `title` is load-bearing — a missing one hard-caps the grade at D — so the
    boilerplate suffix is stripped rather than left to make "Hollow Reef — Game
    Design Document" the title of record on the report.
    """
    match = _H1.search(text)
    if not match:
        return None
    title = _TITLE_NOISE.sub("", match.group(1).strip()).strip(" *_#")
    return title or None


def extract_price_tier(text: str) -> PriceTier | None:
    """The asking price as a ladder rung.

    Reuses `PriceTier.from_price` rather than reimplementing the ladder.

    ⚠️ Free-to-play is checked FIRST and independently of any figure: a F2P
    pitch still quotes a funding ask, and on the sample weak pitch the only
    dollar figure in the document is the $4.2M budget.
    """
    for line in text.splitlines():
        if _FREE.search(line) and not _MONEY.search(line):
            return PriceTier.FREE

    priced: list[float] = []
    fallback: list[float] = []
    for line in text.splitlines():
        for raw in _MONEY.findall(line):
            value = float(raw.replace(",", ""))
            if value > MAX_PLAUSIBLE_PRICE:
                continue  # a funding ask, not a price
            (priced if PRICE_CONTEXT.search(line) else fallback).append(value)

    candidates = priced or fallback
    if not candidates:
        # No figure survived, but the document may still say it is free.
        return PriceTier.FREE if _FREE.search(text) else None
    return PriceTier.from_price(min(candidates))


def extract_core_mechanics(text: str) -> list[str]:
    """Bullets under a mechanics/gameplay/features heading.

    Free text, not vocabulary — these are reported, never matched against the
    corpus, so they carry the pitch's own words.
    """
    lines = text.splitlines()
    mechanics: list[str] = []
    collecting = False

    for line in lines:
        if _HEADING.match(line):
            collecting = bool(MECHANIC_HEADINGS.match(line))
            continue
        if collecting:
            bullet = _BULLET.match(line)
            if bullet:
                mechanics.append(bullet.group(1).strip().rstrip("."))

    if not mechanics:
        # No labelled section — take the document's first bullet list rather
        # than nothing, since this field is pure coverage.
        mechanics = [m.strip().rstrip(".") for m in _BULLET.findall(text)[:8]]
    return mechanics


def extract_summary(text: str) -> str | None:
    """First substantial prose paragraph, for the report's header."""
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block or _BULLET.match(block) or block.startswith(("#", "|", ">")):
            continue
        if _METADATA_LINE.match(block):
            continue  # "**Studio:** Tidewrack Games"
        collapsed = _EMPHASIS.sub(r"\1", " ".join(block.split()))
        if len(collapsed) >= MIN_SUMMARY_CHARS:
            return collapsed
    return None


# --------------------------------------------------------------------------
# The extractor
# --------------------------------------------------------------------------


def extract(text: str, extracted_by: str = "deterministic") -> PitchProfile:
    """Document text to a PitchProfile. Never raises on thin input."""
    tokens = vocab.tokenise(text)
    phrases = _phrases(tokens)

    tags = _matches(phrases, vocab.lookup_tag)
    genres = _matches(phrases, vocab.lookup_genre)
    platforms = _matches(phrases, vocab.lookup_platform)

    primary, subs = _rank_genres(genres, tags)

    profile = PitchProfile(
        title=extract_title(text),
        primary_genre=primary,
        sub_genres=subs,
        tags=sorted(tags, key=lambda t: (-tags[t], t)),
        core_mechanics=extract_core_mechanics(text),
        art_style=None,
        price_tier=extract_price_tier(text),
        target_platforms=sorted(platforms, key=lambda p: -vocab.PLATFORM_COUNTS[p]),
        summary=extract_summary(text),
        extracted_by=extracted_by,
        extraction_confidence=None,
    )
    profile.extraction_confidence = _confidence(profile)
    return profile


def _rank_genres(genres: dict[str, int], tags: dict[str, int]) -> tuple[str | None, list[str]]:
    """Pick the primary genre and sub-genres, from literal matches AND tags.

    ⚠️ Literal genre matching alone does not work, and the sample pitches are
    the proof. A design document describes a game; it does not file it. "A
    free-to-play 100-player battle royale shooter" names no Steam genre at all —
    `Shooter` and `Battle Royale` are tags — so literal matching returns nothing
    but `Free To Play`, a NON_GENRE_LABEL, and `primary_genre` ends up empty.
    That field is load-bearing: empty hard-caps the whole pitch at D.

    So genres are VOTED FOR by the matched tags, using the corpus's own
    co-occurrence (91.6% of `Shooter` titles are Action). Each tag contributes
    its affinity share; a literal genre mention contributes LITERAL_GENRE_WEIGHT
    on top, so a document that does name its genre still wins on it.

    ⚠️ This INFERS genres the document never stated. That is the correct
    behaviour for a market-benchmarking tool — it is how the pitch would be
    filed on the storefront it is being compared against — but it is inference,
    and the report must present it as such rather than as something the producer
    claimed.

    ⚠️ Ranking by corpus frequency alone was the first idea and is wrong: it
    elects `Indie` (59,379 titles) for essentially every indie pitch, which is
    true and useless. ➡️ NON_GENRE_LABELS.
    """
    votes: dict[str, float] = {}
    for genre, mentions in genres.items():
        votes[genre] = votes.get(genre, 0.0) + LITERAL_GENRE_WEIGHT * mentions
    for tag in tags:
        for genre, share in vocab.genre_affinity(tag):
            votes[genre] = votes.get(genre, 0.0) + share

    if not votes:
        return None, []

    ranked = sorted(votes, key=lambda g: (-votes[g], -vocab.GENRE_COUNTS[g], g))
    eligible = [g for g in ranked if g not in NON_GENRE_LABELS]

    if not eligible:
        # Nothing but business-model labels. Better a weak primary genre than
        # none: an empty one is load-bearing and hard-caps the grade at D.
        return ranked[0], ranked[1:MAX_SUB_GENRES + 1]

    primary = eligible[0]
    subs = [g for g in ranked if g != primary][:MAX_SUB_GENRES]
    return primary, subs


def _confidence(profile: PitchProfile) -> float:
    """How much of the profile got populated, 0-1.

    ⚠️ STRUCTURAL, not semantic. A dictionary match cannot know whether it read
    the document correctly, only whether it found things — so this reports fill
    rate and must not be presented as a belief about accuracy. The LLM path can
    report a real confidence; this one would be inventing it.
    """
    from shared.schemas import COVERAGE_FIELDS

    populated = sum(1 for field in COVERAGE_FIELDS if getattr(profile, field))
    return round(populated / len(COVERAGE_FIELDS), 3)
