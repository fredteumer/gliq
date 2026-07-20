# Scoring rules

How Component B turns a `PitchProfile` into a `FitmentResult`. This document is the specification; `components/scoring/` implements it and should not diverge without updating this file first.

## 🎯 Design principle: deterministic first, LLM as a swappable upgrade

Every stage where a model could help has a **deterministic implementation** and, later, an **optional LLM implementation behind the same interface** — both emitting the same pydantic model.

| Stage | Deterministic (Part 1) | LLM drop-in (Part 2) |
| :--- | :--- | :--- |
| A — extract | Dictionary match against the corpus's own genre/tag vocabulary | Semantic extraction from prose + images |
| B — comp selection | Weighted overlap similarity | Embedding or model-judged relevance |
| B — scoring | Rule-based (below) | ⛔ stays deterministic — it is the control |
| C — rationale | Template from sub-scores | Generated prose |

The deterministic path is not a placeholder. It is the **reproducible control** the LLM path is measured against: same input, same schema, comparable grades. Scoring itself stays rule-based in both parts so that a change in grade is attributable to extraction or comp selection, never to model drift.

⚠️ This supersedes the older blanket claim that no LLM appears anywhere in the pipeline. The constraint that matters — and that is preserved — is that **the scoring arithmetic is deterministic and re-gradable**.

## Vocabulary

Extraction matches against the tags and genres that actually occur in `steam_titles`. A pitch is therefore always described in the same vocabulary as the corpus, so matching compares like to like rather than fuzzily mapping an invented vocabulary onto Steam's.

## Step 1 — Comp selection

For each candidate title in `steam_titles`, similarity to the pitch:

| Component | Weight | Measure |
| :--- | :---: | :--- |
| Primary genre | 0.40 | 1.0 exact match, 0.5 if it appears in the title's other genres, else 0 |
| Tags | 0.35 | Jaccard overlap of tag sets |
| Sub-genres | 0.15 | Jaccard overlap |
| Platforms | 0.10 | Jaccard overlap |

Then scaled by **recency**, because an old comparable is weaker evidence about today's market:

| Released within | Factor |
| :--- | :---: |
| 3 years | 1.00 |
| 3–8 years | linear decay 1.00 → 0.50 |
| > 8 years | 0.25 (floor, never 0 — a genre's history still informs it) |

Recency is applied here rather than as a sub-score on purpose: a niche with a high hit rate but nothing successful in five years then scores low automatically, with no extra dimension needed.

- **Similarity floor:** 0.25. Below this a title is not a comparable.
- **Cap:** top 50 by similarity. Recorded in `FitmentResult.comps_considered`.
- If fewer than 5 comps clear the floor, the result carries an assumption noting the thin comp set.

## Step 2 — Units, not dollars

**Success is measured in units moved. The system does not compute or report revenue.**

⚠️ `estimated_units` is an estimate and must be disclosed as one. Steam does not publish unit sales.

**The primary estimate is Boxleiter** — `review_count × 30`. SteamSpy owner bands are stored alongside it as a cross-check, not used for scoring.

That ordering was reversed after loading real data. The lowest owner band is `0 - 20000`, so taking its lower bound reads **zero units for everything in it** — including Shadow of the Tomb Raider at 17,861 reviews, and 74% of a 500-row sample. The two methods disagreed by 2× on hit count. Boxleiter wins because `niche_hit_rate` is a **threshold** comparison within a comp set: it is monotonic in review count and degrades smoothly, so nothing falls off a cliff for sitting near a band edge, and its error is a systematic multiplier that largely cancels in a relative within-niche measure. A discontinuous error does not. ➡️ migration `0003`

💡 The owner band is retained precisely because it fails *differently* — it depends on SteamSpy's sampling rather than on review-leaving behaviour, so sharp disagreement between the two is a signal worth surfacing in a report rather than averaging away.

Deriving revenue would mean multiplying that estimate by list price — which ignores that most units sell discounted, plus regional pricing, refunds, and Steam's cut. That stacks a second guess on the first and produces a number with the *appearance* of precision and none of the substance. A units figure carries exactly one layer of estimation error, and that error is documented.

So the success bar is a unit count:

```
is_hit = estimated_units >= SUCCESS_UNITS      # 10_000
```

💡 10,000 units is roughly the $100k-scale outcome the bar is meant to capture, at typical indie pricing — but the arithmetic never performs that conversion, and neither should the report.

**On what units alone omit:** 10,000 units of a $2 game is a different business from 10,000 of a $40 game. That gap is covered by `price_alignment`, which scores the pitch's price against what its niche actually sustains — so pricing is still assessed, just as its own dimension instead of being smuggled into the success bar.

## Step 3 — Sub-scores

Each 0–100. Raw crowding is deliberately **not** a signal: 10,000 titles in a genre where most clear the bar is a healthy market, and 200 where almost none do is a graveyard. What matters is the *rate*, the *size*, and whether the space is *winnable*.

### `niche_hit_rate` — what fraction of entrants succeed

```
hit_rate = comps with estimated_units >= SUCCESS_UNITS / total comps
score    = hit_rate × 100
```

`SUCCESS_UNITS = 10_000` (configurable). The core signal.

### `sales_potential` — how large the wins are

Rate is not size: a 95% hit rate where the winners move 11,000 units each is still a small business. Uses the **median `estimated_units` of the successful comps**, mapped on a log scale, since game sales are power-law distributed and a linear scale would collapse everything beneath the outliers.

```
score = 100 × (log10(median_success_units) - log10(1_000)) / (log10(1_000_000) - log10(1_000))
```

Clamped to 0–100. 1k units → 0, 1M units → 100.

### `competitive_headroom` — is the space winnable

Rate is not winnability: 95% of comps might clear the bar while three titles hold most of the units, leaving a newcomer the tail rather than the average. Measured as the **Gini coefficient of `estimated_units` across the comp set**:

```
score = 100 × (1 - gini)
```

⏳ **Needs calibration against the real corpus.** Game sales are power-law distributed, so a *healthy* niche may sit at Gini 0.6–0.8 — meaning the raw mapping will compress every score into a narrow band. Once the ETL has loaded `steam_titles`, rescale against the observed distribution across genres rather than against the theoretical 0–1 range. Until then this sub-score is directionally right and numerically untuned.

### `price_alignment` — does the pitch price fit the niche

The one place list price is used — and legitimately, because it compares a pitch's asking price against comparable titles' asking prices. No estimation is involved, and no revenue is inferred.

Compares the pitch's `price_tier` midpoint against the `price_usd` distribution of the **successful** comps:

| Pitch price sits | Score |
| :--- | :---: |
| Within [p25, p75] | 100 |
| Between p75 and p90 | linear 100 → 60 |
| Above p90 | 40, decaying to 0 at 2× p90 |
| Below p25 | 80 — underpricing leaves money on the table, but rarely kills a title |

Asymmetric on purpose: overpricing relative to a niche is a materially bigger risk than underpricing.

## Step 4 — Weighted total

| Sub-score | Weight |
| :--- | :---: |
| `niche_hit_rate` | 0.35 |
| `sales_potential` | 0.25 |
| `competitive_headroom` | 0.25 |
| `price_alignment` | 0.15 |

## Step 5 — Completeness cap

The design doc is open-ended, so extraction will often leave fields empty. Completeness sets a **ceiling** rather than averaging in — this keeps *"we cannot tell"* distinct from *"we can tell, and it is mediocre."* Averaging destroys that distinction, and they warrant different recommendations.

**Load-bearing fields:** `primary_genre`, `title`, `price_tier`.

| Condition | Effect |
| :--- | :--- |
| `primary_genre` **and** `tags` both empty | ⛔ **F — insufficient information.** No comp set exists, so there is nothing to score. Reported distinctly from a low grade. |
| Any load-bearing field missing | Hard cap at **65 (D)** |
| Field coverage < 0.40 | Cap at 65 (D) |
| Coverage 0.40–0.60 | Cap at 75 (C) |
| Coverage 0.60–0.80 | Cap at 85 (B) |
| Coverage ≥ 0.80 | No cap |

Coverage counts the scoring-relevant fields — `primary_genre`, `sub_genres`, `tags`, `core_mechanics`, `price_tier`, `target_platforms`. `title`, `summary`, and `art_style` are report-only and do not enter coverage, though `title` remains load-bearing for identity.

When a cap applies, `FitmentResult.assumptions` states which fields were missing and what the grade would have been uncapped — so the report shows the cost of an incomplete submission rather than silently absorbing it.

## Step 6 — Grade and recommendation

| Score | Grade | Investment tier |
| :---: | :---: | :--- |
| ≥ 90 | A | `greenlight` |
| ≥ 80 | B | `greenlight` |
| ≥ 70 | C | `conditional` |
| ≥ 60 | D | `de_risk` |
| < 60 | F | `pass` |

## Constants

All live in one module so they are tunable without touching the rules, and so the write-up can state them in a table.

| Constant | Value | Status |
| :--- | :--- | :--- |
| `SIMILARITY_FLOOR` | 0.25 | ⏳ calibrate |
| `MAX_COMPS` | 50 | |
| `MIN_COMPS_WARN` | 5 | |
| `SUCCESS_UNITS` | 10_000 | configurable |
| `POTENTIAL_UNITS_FLOOR` / `_CEILING` | 1_000 / 1_000_000 | ⏳ calibrate |
| `RECENCY_FULL_YEARS` / `RECENCY_FLOOR_YEARS` | 3 / 8 | |
| Sub-score weights | .35 / .25 / .25 / .15 | ⏳ calibrate |

⏳ Marked constants are **directionally reasoned but numerically untuned**. They are guesses until the corpus is loaded and real pitches are run through. Evaluate outputs against `samples/` (a deliberately strong and a deliberately weak pitch) and adjust — the adjustment is expected, not a failure.
