"""Rendering the evidence report to Markdown.

⚠️ Pure functions. No database, no network — hand it the three models and it
returns a string, which is what lets the whole report be reviewed in a test
without Pub/Sub or Postgres.

## Why Markdown and not HTML

Content and presentation are kept apart. The rendered document is stored in
`pitches.report_md` (migration 0004) and a review UI turns it into HTML at
display time, so changing how a report LOOKS never means regenerating what a
report SAYS.

## ⚠️ Untrusted text

A pitch title and summary come from whatever a producer posted, and comparable
titles are Steam names the project does not control. Both flow into Markdown
that a UI will later render as HTML. `escape_md` neutralises the characters
that would either break the document's structure (a `|` silently destroys a
table row) or survive into HTML (`<script>`). This is done HERE rather than
left to the UI because the stored artifact should be safe on its own — a second
consumer of `report_md` should not have to rediscover the requirement.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from components.scoring.rules import CROWDING_SAMPLE
from shared.schemas import FitmentResult, PitchProfile, Recommendation

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: How many comparables to table. A comp set can hold MAX_COMPS (50) titles;
#: past the first handful they stop being evidence anyone reads. The total is
#: always stated, so truncation is visible rather than silent.
COMPS_SHOWN = 10

#: ⚠️ Standing caveats that apply to EVERY report, required by docs/SCORING.md.
#: These are corpus-level properties, so B does not emit them per-result — but
#: the spec says they "belong in the report's disclosed assumptions", and this
#: is the report. They are appended to `result.assumptions`, never substituted
#: for them.
STANDING_CAVEATS = (
    "The Boxleiter multiplier is a community heuristic, not a measurement, so "
    "absolute hit rates are soft. Comparisons between niches are sound; a claim "
    "like '8% of entrants succeed' is not precise.",
    "The corpus excludes 53,128 review-less records. What remains is titles that "
    "shipped AND got noticed, so games that launched into silence are "
    "underrepresented and every hit rate is biased upward.",
    "Figures are units moved, never revenue. Deriving dollars would multiply an "
    "already-estimated unit count by list price, ignoring discounting, regional "
    "pricing, refunds and Steam's cut — a second guess stacked on the first.",
)

_MD_UNSAFE = re.compile(r"[|<>`*_\[\]\\]")


def escape_md(value: object) -> str:
    """Neutralise characters that break Markdown structure or survive into HTML."""
    if value is None:
        return ""
    return _MD_UNSAFE.sub(" ", str(value)).strip()


def thousands(value: object) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def usd(value: object) -> str:
    if value is None:
        return "—"
    return f"${float(value):,.2f}"


def percent(value: object) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.0f}%"


def _environment() -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # ⚠️ Not HTML. Autoescaping would inject &amp; entities into Markdown
        # prose; `escape_md` is the deliberate defence instead, applied to every
        # untrusted field in the template.
        autoescape=False,
        # A typo in a field name should fail the render, not silently produce a
        # report with a blank where evidence belongs.
        undefined=StrictUndefined,
    )
    env.filters["md"] = escape_md
    env.filters["thousands"] = thousands
    env.filters["usd"] = usd
    env.filters["percent"] = percent
    return env


def mean_closest_similarity(result: FitmentResult) -> float | None:
    """Mean similarity of the closest comps — the claim the report actually makes.

    ⚠️ Computed from the comparables directly, NOT by un-inverting
    `sub_scores.differentiation`. That score is normalised against
    CROWDING_FLOOR/CEILING, so `1 - differentiation/100` is not the crowding
    figure and reversing it produces a number that looks precise and is wrong.
    An earlier draft of this template did exactly that and reported 0.49 where
    the true mean was 0.55.

    The report calls this "a checkable statement", so it had better be one.
    """
    closest = result.comparables[:CROWDING_SAMPLE]
    if not closest:
        return None
    return sum(c.similarity for c in closest) / len(closest)


def render_report(
    pitch_id: str,
    profile: PitchProfile,
    result: FitmentResult,
    recommendation: Recommendation,
) -> str:
    """The evidence report, as Markdown."""
    template = _environment().get_template("report.md.j2")
    return template.render(
        pitch_id=pitch_id,
        profile=profile,
        result=result,
        recommendation=recommendation,
        comps=result.comparables[:COMPS_SHOWN],
        comps_total=len(result.comparables),
        comps_shown=COMPS_SHOWN,
        closest_similarity=mean_closest_similarity(result),
        closest_sample=CROWDING_SAMPLE,
        assumptions=list(result.assumptions) + list(STANDING_CAVEATS),
    )
