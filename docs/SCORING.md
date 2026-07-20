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
| Tags | **0.55** | Vote-weighted overlap (below) |
| Primary genre | **0.20** | 1.0 exact match, else 0 |
| Sub-genres | 0.15 | Jaccard overlap |
| Platforms | 0.10 | Jaccard overlap |

⚠️ Genre was originally 0.40 and tags 0.35. Calibration showed the floor **never bound**: a flat `0.40 × 1.0` on any genre match cleared every floor ≤ 0.40 regardless of tags, so `MAX_COMPS` was silently doing all the selection and comp sets held ~33,000 titles at every floor from 0.25 to 0.50. Genres are also enormous — "Action" is 35,470 titles — where tags are specific (452 distinct). The discriminating signal now carries the weight it earns.

**Vote-weighted tag overlap.** Steam tags carry vote counts (`{'Turn-Based Strategy': 86, 'Indie': 2}`), stored in `steam_titles.tag_votes`:

```
inter = Σ min(pitch_votes[t], cand_votes[t])   for t in shared tags
union = Σ pitch_votes + Σ cand_votes - inter
score = inter / union
```

Plain Jaccard treats a tag voted 86 times identically to one voted twice, which is how ubiquitous tags like *Indie* come to dominate a comp set. Weighting by shared votes fixes that without maintaining a stopword list.

Then scaled by **recency**, because an old comparable is weaker evidence about today's market:

| Released within | Factor |
| :--- | :---: |
| 3 years | 1.00 |
| 3–8 years | linear decay 1.00 → 0.50 |
| > 8 years | 0.25 (floor, never 0 — a genre's history still informs it) |

Recency is applied here rather than as a sub-score on purpose: a niche with a high hit rate but nothing successful in five years then scores low automatically, with no extra dimension needed.

- **`SIMILARITY_FLOOR = 0.45`** — measured distribution is p50 0.31, p90 0.43, p99 0.55. At 0.45, 149 of 150 sample pitches produce a full 50-title comp set.
- **`MAX_COMPS = 50`**, top by similarity. Recorded in `FitmentResult.comps_considered`.
- Fewer than 5 comps clear the floor → the result carries an assumption noting the thin comp set.

⚠️ The floor is calibrated on **synthetic pitches derived from real titles**, which carry complete tag sets. A real design document will extract sparser, match worse, and clear fewer comps — so this floor is biased slightly high. Re-check against `samples/` once Component A exists.

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

Each 0–100. Raw crowding is deliberately **not** a signal: 10,000 titles in a genre where most clear the bar is a healthy market, and 200 where almost none do is a graveyard. What matters is the *rate* at which entrants succeed and the *size* of the wins available.

### `niche_hit_rate` — what fraction of entrants succeed

```
score = 100 × (comps with estimated_units >= SUCCESS_UNITS) / total comps
```

`SUCCESS_UNITS = 10_000`. The strongest measured signal: **8.0 for random titles vs 38.0 for known winners.**

⚠️ Reported on an **absolute** scale, not normalised to the corpus. The median real comp set has an 8% hit rate, so most pitches score low here — and that is correct. Most Steam releases do not clear 10,000 units. Rescaling that to make the median pitch look average would be grade inflation, and would destroy exactly the discrimination a publisher needs.

### `sales_potential` — how large the wins are

Rate is not size: a niche where 40% of entrants clear 10k but nothing ever exceeds 30k is a small business. Uses the **p90 of `estimated_units` among the successful comps**, log-scaled.

```
score = 100 × (log10(p90_winner_units) - log10(23_250)) / (log10(1_449_000) - log10(23_250))
```

Clamped 0–100. Separates **17.4 random vs 56.8 winners.**

💡 p90, not median. A median over a set already thresholded at ≥10k is structurally stable — it spread only 1.33× across niches, scoring everything ~55. p90 spreads 62× and asks the better question: *how big can a hit get here?*

⚠️ The bracket is the observed p05..p95 of real comp sets, so roughly 5% of niches clip at 100. Widening it would compress the range where nearly all pitches actually sit.

### `price_alignment` — does the pitch price fit the niche

Scored in **rung distance** on the `PriceTier` ladder, not dollars — Steam prices are charm-pricing rungs, and $9.99 → $11.99 is a psychological step that a $2.00 delta understates.

```
distance = |pitch_tier.index - median(comp_tier.index)|
score    = max(0, 100 - 12.5 × distance)
```

⚠️ **This is a guard rail, not a success predictor, and its silence is correct.** It scores ~87 for both random and winning titles, because real shipped games are priced sensibly for their niches. It fires when a pitch asks $49.99 in a $3.99 niche. Do not "fix" its low variance.

### ⛔ `competitive_headroom` — specified, built, removed

An inverse-concentration score ("is this space locked up by incumbents?") was built and then dropped. Validation against 120 winners vs 120 random titles showed **18.2 vs 19.7** — no separation, marginally inverted.

The reason is structural: measured as top-10 share of a 50-title comp set, and game sales are power-law distributed, so the top 10 hold most of the units in *every* niche. Worse, a winner is frequently the very title concentrating its own niche, so the measure penalised exactly the pitches worth finding.

The question is real; top-10 share does not answer it. It is still **computed and reported** by `data/etl/validate_scoring.py` but carries zero weight, so the decision stays falsifiable.

🛑 **Known blind spot:** nothing in Part 1 now measures competitive lock-in. A niche with a strong hit rate and a high ceiling that is wholly owned by two publishers will score well. A natural job for the Part 2 LLM pass, which can read a comp set and judge whether incumbents are beatable in a way a ratio cannot.

## Step 4 — Weighted total

| Sub-score | Weight |
| :--- | :---: |
| `niche_hit_rate` | **0.45** |
| `sales_potential` | **0.40** |
| `price_alignment` | 0.15 |

Tilted toward `sales_potential` relative to a pure rate measure: the brief is winners who win **big**, and a high-rate niche full of modest successes is the safe-but-small bet a publisher chasing outliers should deprioritise.

⏳ These weights remain an **editorial judgement, not a derived number**. Calibration can show what a metric's distribution looks like; it cannot say how much success rate should matter relative to ceiling. There is no ground truth in this corpus to fit against — no labelled good and bad greenlight decisions. Fitting them to maximise the winners-vs-random gap would be fitting noise.

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
| ≥ 80 | A | `greenlight` |
| ≥ 68 | B | `greenlight` |
| ≥ 55 | C | `conditional` |
| ≥ 40 | D | `de_risk` |
| < 40 | F | `pass` |

**Deliberately harsh.** Anchored to the measured distributions below, not chosen to produce a pleasant spread of grades. Under these thresholds a randomly sampled Steam title lands in D or F about three quarters of the time, and fewer than 10% reach B. That is the intended behaviour for an acquisitions filter: the base rate of good investments is genuinely low, and a tool that says no to most of its inbox is working.

## Validation

`data/etl/validate_scoring.py` scores two cohorts through the full rules — 120 uniformly sampled titles, and 120 from the top units decile — and checks that winners separate from slop.

| Cohort | p10 | p25 | p50 | p75 | p90 | max |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| RANDOM | 12.0 | 15.0 | **24.1** | 42.0 | 56.3 | 85.3 |
| WINNERS | 30.1 | 40.9 | **49.6** | 65.8 | 76.4 | 100.0 |

**+25.5 separation at the median, and the top of the scale is reachable.**

⚠️ What this does and does not prove. It shows the rules rank market opportunity in the right direction. It does **not** show they predict whether a given pitch succeeds — every sub-score is computed from the comp set, so two pitches selecting the same comps score nearly identically regardless of their own merits. **Part 1 grades the niche, not the pitch.** That boundary is deliberate, and it is precisely what the Part 2 LLM pass exists to cross.

## Constants

All live in one module so they are tunable without touching the rules.

| Constant | Value | Basis |
| :--- | :--- | :--- |
| `SIMILARITY_FLOOR` | 0.45 | ✅ calibrated — p90 of observed similarity |
| `MAX_COMPS` | 50 | ✅ 149/150 pitches fill it |
| `MIN_COMPS_WARN` | 5 | judgement |
| `SUCCESS_UNITS` | 10_000 | ✅ your bar; ~$100k-scale at indie pricing |
| `POTENTIAL_UNITS_FLOOR` / `_CEILING` | 23_250 / 1_449_000 | ✅ observed p05..p95 of real comp sets |
| `BOXLEITER_MULTIPLIER` | 30 | ⏳ community rule of thumb, not measured |
| Similarity weights | .55 / .20 / .15 / .10 | ✅ rebalanced after the floor failed to bind |
| Recency full / floor years | 3 / 8 | ⏳ judgement |
| Sub-score weights | .45 / .40 / .15 | ⏳ editorial — no ground truth to fit |
| Grade thresholds | 80 / 68 / 55 / 40 | ✅ anchored to validation distributions |

⏳ items are reasoned but unmeasured. ✅ items were fitted to the loaded corpus via `data/etl/calibrate.py` (fixed `RANDOM_SEED`, so re-running reproduces them).

⚠️ Two standing caveats on every number above:

- **`BOXLEITER_MULTIPLIER` shifts everything together.** It is a community heuristic, not a measurement, so absolute hit rates are soft. Comparisons *between* niches are sound; the claim "8% of entrants succeed" is not precise.
- **The corpus excludes 53,128 review-less records.** What remains is titles that shipped *and got noticed*, so games that launched into silence are underrepresented and every hit rate is biased **upward**. Both belong in the report's disclosed assumptions.
