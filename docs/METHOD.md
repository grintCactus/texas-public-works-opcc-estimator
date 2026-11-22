# METHOD: how the estimator prices a bid schedule, and how its accuracy is measured

This file describes the method of the Texas public-works OPCC estimator: how a price is computed, why each step was chosen, how accuracy is measured, and the mathematics behind it. It is written for an engineer who takes the project over and must be able to change the code safely.

- **State described.** Production as of 2026-11-21: parser .34b, after pricing round 18 and round 20 arm G. Algorithm work stopped after that round.
- **Authority.** The code is the final authority on what production does. Where a report or a code comment disagrees with it, this file says what the code does. Section 2.13 lists the stale texts.
- **Numbers.** Every number comes from the project's reports, its public README or the code, with what it was measured on. Fitted values are recomputed at every data rebuild. Those given here are of the 2026-11-21 build unless dated otherwise.
- **"(model-computed)"** marks a figure that the project's own write-up derived from a textbook formula to explain a measured number. It is not a measurement.
- **Words.** "The estimator" is the software (`src/estimate_low_bid.py` and what it calls). "The preparer" is the person who uses it to prepare an OPCC. "Owner" is the public agency that lets a job. "The project's owner" is the person who set the project's goals and rules.
- **Paths** are relative to the repository root. `reports/` and `handoff/` exist in the working copy only.

Sections: 1 the task and the target; 2 the pipeline; 3 why this model and not another; 4 how accuracy is measured; 5 mathematical notes M1–M14; 6 current accuracy and limits; 7 glossary.

---

## 1. The task and the target

### 1.1 Input and output

**Input.** A bid schedule: one line per pay item, with a description, a unit and a quantity. With it: the market (`municipal` or `txdot`), the expected number of bidders and the bid date. Optional: the owner and the work type (the comparables use them), the amounts of owner-set allowances, and vendor quotes for lines that are not covered.

**Output.**

| Output | Meaning |
|---|---|
| **P50** (`expected_low`) | The median estimate of the low bid. |
| **P80** (`expected_low_p80`) | A "not-under" figure: P50 ÷ a calibrated divisor, set so that the actual low bid is at or below it on at least 80% of lettings. It is the number meant to be used as the OPCC. |
| Bands | `p50_band` = P50 ÷ p75 to P50 ÷ p25; `p80_band` = P50 ÷ p90 to P50 ÷ p10. The p's are quantiles of the centred estimate ÷ actual ratio of earlier lettings. |
| Evidence | A per-line trace (the cell that priced the line, the lettings behind it, every fallback), warnings, the not-covered lines, the low-confidence lump sums and the comparable lettings. |

**Terms.** A *letting* is one bid opening. A *bid tabulation* is the table published after it, with every bidder's unit price on every line. The *low bid* is the total of the bidder identified as low. It is not always the award: a low bid that was later disqualified still counts, because it shows a price a contractor was willing to take. The *median bidder* is the bidder whose total sits in the middle. *OPCC* means opinion of probable construction cost.

### 1.2 The goal

The estimate is used as an OPCC, where under-estimating is the costly mistake.

| Layer | Rule |
|---|---|
| **Floor** | P80 is below the actual low bid on **at most 20%** of lettings (`NOT_UNDER_FLOOR = 0.20`). |
| **Score** | Within the floor, P80 should overshoot the low bid **as little as possible**. The score is the *median overshoot*: the median of P80 ÷ low bid − 1 over the lettings where P80 ≥ low bid. 13% is an aspiration, not a gate. |
| **Yardstick** | At a matched not-under rate, the overshoot is compared with engineers' own published estimates. Engineer estimates are never an input: not a feature, not a target. |

On 2026-11-20 the project's owner clarified that 13% is a hope, and that within the floor "the less over, the better". Since then the *in-band share* (P80 ÷ low bid within 1.00–1.13) is reported but no longer used to judge.

How far the target is: with every unit price known exactly, this design's overshoot at an 80% not-under rate is about 13% (M12). So 13% is roughly the design's limit. At that rate engineers reach about 25% on municipal work and 23% on TxDOT. The estimator is at 34% and 30% (section 6).

### 1.3 Markets and standing rules

- **Markets.** *Municipal*: city and county work in the Dallas–Fort Worth (DFW) area. It is the main market, with a focus on creek erosion repair, drainage and streambank stabilisation. *TxDOT*: Texas Department of Transportation lettings in the DFW counties, the second market.
- **Public data only.** Every fitted quantity admits rows with `source_set == "external"` and nothing else.
- **A private company test set exists.** Its rows carry `source_set == "internal"`. They never train or fit anything. Results on them stay local and are not in this file.
- **A locked public test set exists** (`source_set == "public_test"`, section 4.4).
- **Engineer estimates are a yardstick only.**
- **Forward in time.** Every reported figure prices each letting from lettings dated before it (section 4.2).
- **Pre-registration.** A pricing change is written down with its pass/fail rule before it is scored (section 4.6).
- **Guards.** A build fails if any test data reaches a fitted number (section 4.3).

---

## 2. The pipeline

### 2.0 One picture

```
public bid tabulations (PDF / XLSX / scans)
   |  parse: text layer, coordinate reader, OCR, arithmetic checks
   v
panel: one row per bidder x line item                                  2.1
   |  only rows with source_set == "external" go further
   v
reference tables (token cells, band cells, family cells)               2.2
price index by market                                                  2.3
competition discount by number of bidders                              2.4
   v
estimate(schedule)
   each covered line -> reference price: tokens -> band -> family      2.2
        (F1 size fallback, F2 removal identity, abandonment identity)
        escalated to the bid year                                      2.3
   sum of the lines = estimate of the MEDIAN bidder's total
   x competition discount f(number of bidders)                         2.4
        = line-priced bid part
   geometric mean with CMP, the price from the 5 most similar
        earlier lettings (V2)  = bid part                              2.5
        (TxDOT only: T1)                                               2.6
   x centring factor lc                                                2.7
   + owner-set money at face value                                     2.9
   = P50
   / divisor p  (forward calibration, adaptive guard, B80)             2.8
   = P80
```

This is the order of `estimate()` in `src/estimate_low_bid.py`.

### 2.1 The panel

`data/clean/master_panel.csv`, built by `src/build_master.py`, has **one row per bidder × line item** of a letting.

| Columns | What they are |
|---|---|
| `source`, `source_set`, `project_key`, `bid_date`, `date_precision`, `year` | The source, the train/test gate, the letting and its date. |
| `bidder_norm`, `is_low_bidder`, `n_bidders`, `competition_class` | The bidder; whether it is the identified low bidder; the bidder count and its class: `competitive` (3 or more), `thin` (2), `sole` (1), `unknown`. |
| `is_engineer_estimate`, `project_kind`, `price_basis` | Filters. Engineer-estimate rows never enter a fitted quantity. `price_basis` is `all_bids` for bid tabulations; `uneven_proposal` marks a letting whose bidders did not price the same job. |
| `desc_norm`, `item_key`, `item_family` | The normalised description; the item identity the index pairs on; the item family (the web app says "item type"), such as `storm_pipe`. `other`, blank, `trm` and `needs_review` mean "not classified". |
| `unit`, `quantity`, `unit_price` | The pay unit, the quantity, the bidder's unit price. |
| `size_in`, `size_bucket` | The parsed size in inches and its bucket: 0–6, 6–9, 9–13, 13–19, 19–25, 25–37, 37+. Blank: the size did not parse. |
| `owner`, `work_type` | The public agency; the kind of work (erosion, drainage, pavement, utility, …). |

- **Quantity band.** `qty_shape.band_of()` derives it from the quantity: 0–10, 10–50, 50–200, 200–1,000, 1,000–5,000, 5,000–25,000, 25,000+ (the lower edge is included).
- **`source_set`** is the train/test gate. `external`: public tabulations, the only rows that may train. `internal`: the private company test files. `public_test`: the locked public test set.
- **Market.** `municipal` when `source` is `city_bidtab`; every other source is `txdot`.
- **Availability date.** A letting counts as known from the latest bid date it carries. A year-only date counts as 1 July. A letting with no date is never a reference and never scored.

### 2.2 Line prices

**The price pool** (`_reference_pool()`). Rows with `source_set == "external"`, not an engineer estimate, `competition_class == "competitive"`, `project_kind == "construction"`, `price_basis != "uneven_proposal"`, and a unit price above 0.

**The two-stage median.** Every reference price is (1) the median unit price across the bidders of one letting, then (2) the median of those per-letting medians across lettings. One letting has one vote. A 13-bidder letting does not outweigh a 3-bidder one, and one contractor loading a single line is absorbed inside its letting. The result is the price of the **median bidder**. The step down to the low bidder is taken once, on the total (section 2.4). `assert_all_bids_basis()` checks at load time that no contract or winning price is in the pool, because such a price must never be discounted again.

Each cell carries the number of lettings behind it (`n_projects`), its median quantity, a dollar-weighted vintage year (it can be fractional) and a `usable` flag: `yes` with 5 or more lettings, `thin` with 3–4, `no` with fewer, `unclassified` for the unclassified families.

**Three tiers.** `item_market_price()` tries three tables in order. The unit is a hard key in all of them.

| Tier | Table and key | Minimum | What is taken |
|---|---|---|---|
| 0. Token cells | `price_reference_tokens.csv`: (token set of the description, unit, quantity band). Not keyed on family. | 2 lettings | All cells of the same unit and band whose token set is *similar* to the line's. The price is the median of their prices, weighted by similarity. |
| 1. Band cells | `price_reference_band.csv`: (family key, unit, size bucket, quantity band). | 2 lettings | The cell's price. If the sized cell is missing: the unsized cell of the same family, unit and band, unless F1 applies. No quantity adjustment. |
| 2. Family cells | `price_reference.csv`: (family key, unit, size bucket). | None. A one-letting cell prices, flagged `no`. | The cell's price × a quantity-shape factor. If the line's bucket has no cell: the best-evidenced sized bucket with `usable = yes`, else the unsized cell. |

The *family key* is the family, plus `#rm` for a removal-only description (F2). After any tier the price is escalated from the cell's vintage to the bid year (section 2.3). No discount is applied to a line.

Measured gains (leave one letting out; median error of the total; municipal / TxDOT):

- family + unit + size 32.6% / 50.1%; with the quantity band 28.6% / 30.8%. The band costs coverage (fully priceable municipal lettings 121 to 88), which is why it is a tier and not a replacement.
- band only 28.5% / 30.9%; tokens first about 23% / 22–25%. Two code comments disagree here (22.9% / 21.7% and 23.9% / 24.5%), and the original measurement was not archived. The token tier adds coverage (96 to 120 lettings).

**Tokens.** `item_tokens.tokens()` lower-cases a description, splits it at non-alphanumerics, splits glued dimensions (`3ft` gives `3` and `ft`), keeps numbers (they are the dimensions), drops words under three characters and stop words (furnish, install, remove, existing, complete, …, and unit words), and folds plurals. `item_tokens.similar(a, b)` is the Jaccard overlap (intersection ÷ union) when it is at least `THRESHOLD = 0.35`, else 0. Three hard constraints return 0 first:

- **dimensions**: when both sets carry numbers, at least one must agree. "18 in RCP storm sewer pipe" is not "24 in RCP storm sewer pipe", although their overlap is 0.667;
- **removal flag** (F2) and **abandonment flag** (AB): both sides carry it, or neither does.

Matching is deliberately not restricted to the family: an `other` line worded "gabion basket pvc" should be priced from gabion rows. The threshold came from a leave-one-letting-out sweep. The TxDOT curve is flat from 0.25 to 0.60, the noisier municipal curve only rules out the ends, and 0.35 is the middle of what survives both.

**F1, the size fallback** (`SIZE_FALLBACK`, class `SizeCells`). A size bucket is a range. A sized line whose bucket had no band cell used to fall to the unsized cell, which holds rows whose size did not parse (about $45/LF, mostly trench repair and paving). F1 triggers for a line with a parsed size whose family and unit have no usable band cell for its bucket in any quantity band. Then:

1. the unsized cell is never used;
2. the line is priced in the family tier from the nearest sized cell of its family key and unit: the smallest |ln(size ÷ size_ref)|, where size_ref is the median size of the cell's lettings;
3. that price is multiplied by (size ÷ size_ref)^k, then quantity shape and escalation apply;
4. k is the least-squares slope of ln(price) on ln(size) over the sized cells of that family and unit, when they span at least 5 sizes and 20 lettings. Otherwise k = 1 for `water_pipe`, `sanitary_pipe` and `storm_pipe`, and k = 0 for everything else;
5. with no sized cell at all, the line stays unpriced.

Example: a 48-inch main went from $47/LF to $721/LF (the median bid on that line was $1,287), and its letting's P80 ÷ low bid from 0.938 to 1.135. Known side effect: on non-pipe families F1 reads a width as a size. Curb 24–36 inches wide was priced at $4,300–6,800/LF against bids of $35–52/LF (258 lines in 95 lettings). A pipe-only version (F1P) is written and off.

**F2, the removal identity.** `removal_only(desc)` is true when a description has a removal word (`remov*`, `rem`, `demo`, `demol*`, `demolition`) and no rebuild word (`replac*`, `reconstruct*`, `install*`, `reset*`, `relocat*`, `new`, `rebuild*`, `reinstall*`).

- Token table: a removal-only description carries the flag token `zzremoval`.
- Band and family tables: a removal-only row is filed under family + `#rm`, so removal and new work never share a cell. The quantity-shape table is still read by the plain family.
- A removal-only line with no `#rm` cell is never priced as new work. It gets the schedule's **typical line amount**: the median amount of the schedule's directly priced lines that are neither lump sums nor owner-set.

Why: "remove" and "existing" are stop words, so a removal price of $14/LF could price new pipe bid at $82–155/LF. In the municipal 2024–26 back-test, removal-only lines summed to 1.42× their own median bids before F2 and 0.82× after.

**The abandonment identity (AB, live since round 18).** For pipes, removing and abandoning are different work. Abandonment leaves the pipe in place and plugs or grouts it, which costs far less. `abandon_only(desc)` is true for a description that is not removal-only, has no rebuild word, and either has an abandon word or has plug / grout / flowable-fill wording about an existing pipe, main, manhole or similar. It carries the flag token `zzabandon` (`ABANDON_IDENTITY = True`). The flag acts in the token tier only. Splitting the band and family tables too (`#ab`, arm AB2) failed its test and is off.

- Example: "abandon existing 8-inch sanitary sewer pipe in place" went from $141.75/LF to $29.02/LF. The public median for abandonment up to 12 inches is $29.38/LF.
- Known weakness: size is not a hard constraint when only one side states it. A 42-inch abandonment line went to $23.50/LF against bids of $225/LF.

**Quantity shape.** The family tier has no band in its key, so it scales the price by shape[band of this quantity] ÷ shape[band of the cell's median quantity], from `price_by_quantity_band_family.csv`. `qty_shape.fit_shape()` fits the shape per (family, unit) with two-way fixed effects on log price: log p(item i, band j) = alpha_i + beta_j + e. alpha_i absorbs each item's own level; beta_j is the common movement across bands.

- A family needs at least 3 items seen in two or more bands.
- Only the largest connected component of the item–band graph is used.
- The multiplier is 1.0 on the best-supported band.
- A shape whose span is smaller than its own residual spread is refused. An absent shape applies 1.0, with a warning.
- In forward scoring this table is the published one. It is the one known non-forward input.

**When nothing matches.**

- A line with no unit is not priced. A line with no family is refused by the family tier.
- A line with no cell in any tier is not priced ("no price_reference entry").
- A line of an unclassified family that the token tier cannot match is priced from that family's catch-all cell, flagged `unclassified`, with a warning that this is an order of magnitude and not a bid.
- Unpriced lines are counted, and the result warns that the total covers only the rest.
- If no line can be priced there is no estimate. A total of 0.00 is never printed as a bid.
- Every fallback reaches the caller as a warning with its dollar share: rows not `usable = yes`, no quantity adjustment, no index path, a carried or borrowed index, another size bucket. In one earlier back-test, 1,972 of 2,034 lettings had returned no warning while 37.9% of the dollars rested on flagged rows.

**Two public examples.**

- Good: a 3,123 CY trail-grading line fell to a sidewalk family cell at $580/CY. The token tier matched 2 references at $45/CY, and the letting went from 2.505× its low bid to 1.316×.
- Bad: a 10 EA street-light foundation line was priced from a 2-letting family cell in which one reference was a parse error (a junction box at $323,609.50). The median of $164,182/EA pushed the letting to 9.6× its low bid.

### 2.3 Escalation: the price index by market

Reference prices are moved from their vintage to the bid year with an index built from the same bids (`src/index_by_market.py`; the forward refit is `_ForwardIndex`).

- **Build.** Per item and year, take the two-stage median price. For two adjacent years, take the ratio of each item priced in both. The year's move is the **median of those ratios**, and the index is chained. A year pair with fewer than 10 matched items leaves the level unchanged (`FORWARD_MIN_PAIRS`). Only exactly dated lettings enter; engineer-estimate rows do not.
- **Arms.** `Municipal`, `TxDOT`, and a pooled fallback `_default`.
- **Apply** (`escalation()`). Factor = index(bid year) ÷ index(vintage). A fractional vintage is interpolated geometrically; truncating it cost a median +4.06% per row. The market's own arm is used when it has both years. Otherwise `_default` is used and the line says "BORROWED". With no path, the price stays at its vintage, with a warning.
- **Narrow carry.** Only when no arm at all has the bid year (every schedule priced to 2027 before a 2027 row exists), each arm's last level is carried flat to that year, and the line says so.

| Year | TxDOT arm (2016 = 100) | Municipal arm (2020 = 100) |
|---|---|---|
| 2021 | 110.3 | 102.3 |
| 2022 | 137.0 (+24.2%) | 142.7 (+39.5%) |
| 2023 | 155.7 (+13.6%) | 143.4 |
| 2024 | 167.3 (+7.5%) | 155.7 |
| 2025 | 167.3 (0.0%) | 164.0 |
| 2026 | 142.2 (−15.0%) | 153.3 (−6.5%) |

The municipal arm rests on 28–229 matched items a year, the TxDOT arm on roughly 1,400–1,700. Part of the municipal 2026 move is seasonal mix (about −3.0% without it); the estimator reads the raw column.

### 2.4 From the median bidder to the low bid: the competition discount

The line prices add up to an estimate of the **median bidder's total** (`market_total`). The low bid is the minimum of N bids, so the estimator multiplies the total by a discount f(N).

- **Fit** (`src/competition_and_crosswalk.py`; forward refit in `_ForwardDiscount`). Per letting: low bid ÷ median bidder total. Pooled table: the median ratio per bidder count with at least 10 lettings. Municipal table: pooled × one factor, which is the median over municipal lettings of the letting's ratio ÷ the pooled f at its bidder count. The factor is refitted at every build: 0.9752 on 608 municipal lettings. One bidder: f = 1.
- **Data.** 3,345 lettings of 2002–2026: TxDOT archive 2,024, TxDOT current 705, municipal 616.

| Bidders | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 | 13 | 14 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Pooled | 1.000 | 0.935 | 0.898 | 0.868 | 0.856 | 0.837 | 0.847 | 0.846 | 0.845 | 0.833 | 0.782 | 0.801 | 0.871 | 0.847 |
| Municipal | 1.000 | 0.912 | 0.876 | 0.846 | 0.835 | 0.816 | 0.826 | 0.825 | 0.824 | 0.812 | 0.762 | 0.781 | 0.850 | 0.826 |

Example: a municipal job with 4 bidders whose median bidder total is estimated at $1,000,000 has an expected low bid of about $846,000.

- **Apply** (`discount()`). Municipal schedules use the municipal table, TxDOT schedules the pooled one. A count without a bin uses the nearest bin if it is at most 2 away (`MAX_BIN_DISTANCE`). A count **above the top bin** uses the top bin (`CLAMP_ABOVE_TOP_BIN`; round 16's arm C, live since round 18). Before that, a 17-bidder letting got no estimate, because the table ends at 14. Any other gap is refused: the result then carries the market total, a warning and no P50.
- **Stability.** For 3–9 bidders the yearly discount stayed within 0.849–0.884 over 2016–2026, so it is treated as a structural constant.
- **Bidder count.** The back-tests use each letting's actual count, which is not known when quoting. A forecast count left municipal results unchanged and raised the TxDOT overshoot from 31.8% to 34.1% (previous data build).

### 2.5 The comparables blend (V2)

Engineers estimate from a few similar past jobs, scaled by quantity. The comparables method (`src/comparables.py`) does the same. Its price is called CMP, and V2 blends it with the line-priced bid part:

> bid part = sqrt(CMP × line-priced bid part). Owner-set money is added last.

- **Candidates.** Lettings of the same market that are scorable (one identified low bidder, at least two bidders, at least 3 lines, a date) and dated strictly before the bid date.
- **Overlap O.** For each schedule line, the best token similarity to any line of the candidate, in the same unit. The identity used here has no removal or abandonment flag. O is the average over lines, weighted by each line's estimated amount. A candidate needs O ≥ 0.10 (`O_MIN`).
- **Similarity** S = O × size × owner × work type × age. Size = exp(−0.5 × |ln(S_T ÷ S_C)|) on the two totals without lump sums; 4 times larger or smaller gives 0.5. Same owner: × 1.5. Same work type: × 1.5. Age = 2^(−years ÷ 3). The **5 best** candidates are used (`K_MAIN`).
- **Transfer.** For each line and comparable, take the best-matching line's **low bidder** unit price u. A unit-priced line gets u × clip((q ÷ q_C)^beta, 0.5, 2) × escalation × f(n_T) ÷ f(n_C). A lump sum gets u × clip(R^gamma, 1/3, 3) × escalation × f(n_T) ÷ f(n_C), where R is the ratio of the two jobs' amounts without lump sums. Across comparables the median is taken, weighted by S × the line's similarity. An unmatched line keeps its line price × f(n_T).
- **beta and gamma.** Within-item slopes (fixed effects) of ln(unit price) on ln(quantity), and of ln(lump-sum amount) on ln(the job's amount without lump sums). They are fitted on lettings dated before min(bid date, 2023-01-01) and clamped to [−0.5, 0] and [0, 1.2]. Reported values: municipal −0.18 and 0.67; TxDOT −0.19 and 1.2 (at its cap).
- **No candidate.** CMP then equals the line-priced bid part, so the blend changes nothing, and a warning says so.
- **Fragility.** On an unusual schedule CMP swings when one or two of the five comparables change. The blend passes on the square root of that swing (M7). The web app lists the similar lettings for a person to judge.

### 2.6 T1, for TxDOT schedules only

| Part | Rule | Threshold |
|---|---|---|
| C4, exact cell | In the token tier, a cell whose token set equals the line's (Jaccard 1) prices the line by itself, instead of the weighted median of all matches. | `C4_MIN_N = 5` lettings |
| C4, mobilization as a share | Mobilization = r × S, split evenly over the schedule's mobilization lines. S is the schedule's priced lines other than mobilization, owner-set and uncovered lines, before the discount. r is the median, over every bid of every earlier letting, of that bid's mobilization amount ÷ its amount on all other lines (`MobRatio`); about 0.097 for TxDOT. | `MOB_MIN_LETTINGS = 20` |
| D1, size-aware discount | f(n) is replaced by the median low ÷ median-bidder ratio of earlier scored TxDOT lettings in the same cell: bidder class (1–2, 3–4, 5–6, 7+) × class of the model's own pre-discount total (under $250k, $250k–1M, $1M–5M, $5M and over). The same rule gives a comparable's f(n_C) inside V2. | `D1_MIN = 10` lettings per cell, else plain f(n) |

- For a new schedule the D1 records are read from `data/clean/estimator_t1_txdot.csv`, written by the last forward calibration.
- Round 6 (TxDOT 2023–26, 796 lettings): median overshoot 37.2% to 31.3% (−5.9 points, 90% interval −8.0 to −3.0), under rate 16.7% to 16.0%, P50 error 20.2% to 16.6%. Municipal results were identical letting by letting.
- Risks written down at adoption: a 2026 under rate of 22.5%, and bridge maintenance (72 lettings) going from 20.8% to 26.4% under.
- T1 has no locked-set confirmation, because the locked set is municipal only. It carries a prospective check (section 4.9).
- Municipal schedules do not price mobilization as a share: the lines got closer, the job totals did not (M13).

### 2.7 Centring

One factor lc per market multiplies the bid part so that, over earlier forward-scored lettings, the median of (bid part × lc + owner-set money) ÷ actual low bid is exactly 1 (`_centre()`).

- Owner-set money is not multiplied. Without it, lc = 1 ÷ median(bid part ÷ low bid).
- Forward: a letting dated D gets the lc of its market's lettings dated before D, and needs at least 40 of them (`FORWARD_MIN_PRIOR`). A new schedule gets `level_correction` from `estimator_calibration.csv`.
- Values: municipal 1.043585, TxDOT 1.013033.
- Centring makes P50 unbiased. It cannot tighten P80: a factor shared by all lettings cancels out of P80 (M4).

### 2.8 P80

- **Rule.** P80 = P50 ÷ p. p is the alpha-quantile of the centred ratios of the market's earlier lettings: element `int(alpha × (n − 1))` of the n sorted ratios, counting from 0.
- **Guard** (`_not_under_guard()`). alpha starts at `GUARD_TARGET = 0.16`. After each letting date closes, it moves down by gamma × 0.84 for every letting whose P80 came in under, and up by gamma × 0.16 for every other. It is kept between 0.02 (`GUARD_ALPHA_MIN`) and 0.20 (`NOT_UNDER_FLOOR`). gamma is 0.02 for municipal and 0.004 for TxDOT (`GUARD_GAMMA`), about 1 ÷ the lettings scored per year. So the guard aims at 16% and never goes past the plain 20th percentile: it only widens the fixed rule, and only after misses.
- **Forward calibration** (`calibrate_forward()`, `forward_scores()`). Every public letting is priced by `estimate()` on `ForwardTables` at its own date. Reference cells, index arms, discount tables, size cells, comparables and T1's records hold only lettings dated strictly before it. Each letting then gets the centring and divisor of the earlier lettings of its market, with alpha as the guard stood when its date opened. The same pass writes `estimator_calibration.csv` (centring, quantiles p10–p90, `p_not_under`, `not_under_level`) for new schedules.
- **Narrow carry.** The forward calibration runs with the index's narrow carry (section 2.3). Carrying every arm instead cost in-band share with no gain on the floor (section 3.7).
- **B80** (`P80_FROM_B`, live since round 18). The divisor and the guard's level come from "production B": production with every round-10/11/13/16/17 switch off (`production_b()`), run as a nested pass. P50 and its centring stay the actual pass's own. Reason: a change that tightens the forward residuals also narrows its own margin, and unseen lettings do not tighten as much. That is how the arm CORE failed on the locked set. B80 lets a price improvement through without shrinking the margin.

| | Divisor p (`p_not_under`) | P80 ÷ P50 | Guard level alpha | Guard-scored lettings | Under share among them |
|---|---|---|---|---|---|
| Municipal | 0.7342 | 1.362 | 0.148 | 358 | 15.36% |
| TxDOT | 0.784635 | 1.2745 | 0.14448 | 1,778 | 15.80% |

Example: a municipal P50 of $1,000,000 gives a P80 of about $1,362,000.

### 2.9 Coverage scope, low-confidence lump sums, owner-set money, contingency

**Not covered** (`COVERAGE_SCOPE`; the rule is `src/coverage.py`). Specialty items with almost no bid history are not priced.

- A line is uncovered when its description names one of seven classes, it reads as the equipment or structure itself (a catch-all or equipment family, or priced per LS or EA), and no exemption applies.
- Classes: pumps and pump / lift / booster station equipment; generators and electrical gear; prefabricated or pedestrian bridges; tanks and process equipment; specialty buildings; synthetic turf and play equipment; meter vaults.
- Exemptions: ordinary civil work at such a facility (pad, fence, paving, pipe, …); removals; temporary bypass pumping or dewatering; a temporary or rented generator; relocating existing equipment; owner-set money wording; railing or paint on a bridge; HVAC in a building.
- The rule reads only description, family and unit, never the bids. It was fixed before any scoring under it.
- An uncovered line is out of the line total, the comparables match and T1's mobilization base. It is listed with the status "Not covered -- get a separate quote". P50 and P80 cover the other lines. A vendor quote is added at face value. Once every uncovered line has a quote, a complete total is shown.
- When more than half of the lines are uncovered, there is no P50 or P80 at all. A second rule, which voided a schedule for one whole-facility lump sum, is off (`WHOLE_FACILITY_RULE = False`).
- Back-tests score the covered estimate against the low bid minus the low bidder's own amounts on the uncovered lines.

Why: by dollars, about one sixth of the low-tail gap of the worst municipal misses sat in novel specialty items, such as a 2000 kW generator priced at $33k against $1.78M. The rule flags 58 of 847 municipal lettings. On data snapshot 5 it moved the municipal under rate from 18.6% to 17.4% and the overshoot from 51.3% to 48.9%.

**Low-confidence lump sums** (`LOWCONF`; display only). Mobilization and four lump-sum classes are shown apart: traffic control, site / right-of-way preparation, SWPPP / erosion control, bonds / insurance. For each class the result gives:

- our amount at the P50 level;
- the share of their own bid that the low bidders of earlier lettings gave that class (quartiles), by the size of the rest of the bid: under $0.5M, $0.5–2M, over $2M. A size class needs at least 15 lettings, else all sizes are used;
- the dollar range that share implies for this schedule.

Nothing is priced from it: a forward re-run of 2,129 lettings showed 0 differences. The range is a guide. The low bidder's own amount fell inside it in about a third of lettings. It exists because a mobilization line's price is nearly blind to job size, while the low bidders' share of their own bid is steady (median 4.8%, quartiles 3.6–5.8%).

**Owner-set money.** Allowances, contingencies and force accounts: the owner prints the amount and every bidder copies it. Of 224 allowance-worded public items quoted by two or more bidders, 200 carry one identical amount.

- A line is allowance-like by family (`contingency`), unit (`ALLOW`, `ALW`, `DOL`, …) or wording.
- When the schedule gives its amount, it is taken at face value: not estimated, not discounted, not centred, added last.
- Without an amount it is priced like any line, and flagged. A quantity is never read as dollars.
- In back-tests it is recognised from the bids: two or more bidders quoting one identical amount on an allowance-like line.

**Contingency.** A lump sum worded as a contingency is owner-set money. The project's OPCC convention is a separate construction contingency below the subtotal, 10% by default, which the web app implements. For scale: in cities, P50 + 10% came in under the low bid on about 30% of past jobs, and P80 on about 14%.

### 2.10 What the web app does with the estimator

`ui/engine_production.py` calls the same estimator and changes no switch, constant or fitted number:

```
tab = Tables(); tab.attach_v2f1f2()            # once, at start
r = estimate(tab, items, n_bidders, market, to_year,
             target={"date": ..., "owner": ..., "work_type": ...})
```

- **Classification.** Each line's family, unit and size come from the parser's own classifier. The preparer may override the family.
- **Bidder count.** When none is given, rule N1 supplies the median count of earlier lettings of the same market, work type and size class. The first cell with at least 10 lettings wins; otherwise 4.
- **Forbidden inputs.** A request carrying an OPCC or engineer's-estimate field is rejected (`FORBIDDEN_INPUTS`).
- **`MARKET_ACCURACY`**, the accuracy quoted beside a result, is a hand-kept copy of the README table. **`UNDER_CURVE`** says how often m × P50 came in under the low bid, for m from 0.80 to 2.00 (municipal: 0.4213 at 1.00, 0.2936 at 1.10). It drives the contingency guide. Both must be regenerated after every data build.
- **Display.** The page shows up to four of the five comparables (`MAX_SIMILAR = 4`), flags a line that carries at least 15% of P50 on fewer than 3 earlier lettings, and offers converted-unit prices as hints. None of this changes P50 or P80.

### 2.11 A worked public example, with its lesson

A public municipal letting of 2026-04-16: a water-line crossing with 7 bidders. The actual low bid was $110,550. The low bidder's mobilization was $5,000.

| Quantity | Value |
|---|---|
| P50 and P80 on the published tables | $109,075 and $154,594 (× 1.417, the multiplier of 2026-11-20) |
| Our mobilization inside P50 | $66,964, which is 61.4% of P50 |
| Earlier low bidders' mobilization share (77 lettings, rest of job under $0.5M) | quartiles 4.7%–9.1%, which implies $2,098–4,231 here; ours is 27.3× its median |
| P50 of the same letting scored forward in time | $242,724, which is 2.20× the low bid |
| Low-confidence range for mobilization in the forward run | $4.2k–8.5k; the actual $5,000 is inside |

1. The published tables contain this letting's own bids. So the first P50 is optimistic and is no evidence of accuracy. Forward in time the same letting is estimated at 2.20× its low bid.
2. Mobilization on small jobs is a real problem: the line price does not scale with the job.
3. The low-confidence block points at exactly that line.

### 2.12 Switches and presets

`pricing_rule(...)` runs a block under another combination of switches. Its defaults are production's. Each short name is defined in the glossary.

- **On:** `size_fallback` (F1), `removal_identity` (F2), `comparables` (V2), `txdot_c4d1` (T1), `coverage_scope`, `abandon_identity` (AB), `clamp_high_bidders` (C), `p80_from_b` (B80).
- **Off** (written; waiting for a check, or rejected): `muni_cells` (MC), `cmp_removal_identity` (E), `f1_pipe_only` (F1P), `family_cell_t2` (T2), `identical_bid_owner_set` (D), `muni_txdot_level` (P_L), `abandon_family_key` (AB2).
- **Presets:** `round10_core` = MC + E + F1P + T2 + D (the arm CORE); `round11_core_not2` = the same without T2; `round13_l` = P_L; `round18_x1_b80` = clamp + abandonment flag + B80, which round 18 installed.
- Module constants that are not arguments: `LOWCONF = True`, `WHOLE_FACILITY_RULE = False`.

### 2.13 Stale texts in the code and reports

- **Discount shift.** The docstring of `estimate_low_bid.py` quotes a factor of 0.9775 on 278 lettings, "9 of 9 bins", and sigma 0.151 against 0.143. That is an earlier fit. On 2026-11-21 it is 0.9752 on 608 lettings, 11 of 11 bins, and sigma 0.160 against 0.143. The out-of-fold comparison of nine bins against one shift (section 3.7) was measured on those earlier 278 lettings.
- **Index moves.** Two comments say the statewide index fell 12.5% and the municipal one 7.3% from 2025 to 2026. On the current data the TxDOT arm fell 15.0% and the municipal arm 6.5%.
- **"OFF is production".** Comments above `CLAMP_ABOVE_TOP_BIN`, `P80_FROM_B` and `ABANDON_IDENTITY`, and parts of the `pricing_rule` docstring, still say these are off. They are `True` since round 18.
- **Decoy.** The docstring of `holdout_eval.py` describes the decoy as 60 file groups, 12 per year, half erosion/drainage. That was version 1's design. The code draws the decoy like version 2.
- **Similar projects.** The project summary says the page lists the five comparables. The code shows up to four.
- **Adoption rule file.** Its "Consequences" section says round 14's arms qualify under track U. That line predates the clarified reading of condition (a) (section 4.8).

---

## 3. Why this model and not another

Only reasons found in the project's records are given. Where something was never tested, this is said.

### 3.1 Why not average past low bids?

- The low bid is the minimum of N bids, and it falls as N grows. Averaging past low bids mixes the price level with how many bidders came that day. That biases the estimate on a job with another bidder count. Hence: market median × f(N). Leaving the discount out costs a systematic +15.6% (2,040 lettings).
- Engineers show the same effect. On 2,729 TxDOT lettings their estimate ÷ the median bid stays within 0.91–1.08 at every bidder count. Their estimate ÷ the low bid rises: 0.915 with 1 bidder, 0.998 with 2, 1.180 with 8, 1.306 with 12.
- The lowest price on every line adds up to only 21%–83% of the low bid. The low bid is one contractor's whole offer, and contractors unbalance their bids.
- Unit price depends on quantity: within one exact item it is higher at small quantities by a median factor of 1.7 (quartiles 1.2–3.0). Wording varies by city: 84.4% of municipal descriptions occur in one letting only (41.7% for TxDOT). Prices move by year: the TxDOT index rose 24% in 2022 and fell 15% in 2026. Hence bands, tokens and escalation.
- No formal test used "the average of all past bids" as a control arm. The nearest control is the median by family + unit + size (section 2.2).

### 3.2 Why not a regression or machine-learning model?

- A gradient-boosted tree model of unit prices was tried. Under random 5-fold it looked very good. In a rolling back-test that uses only the past, the municipal error went from 24.8% to 23.7% (90% interval −11.9 to +4.2 points, so no gain can be claimed). TxDOT went from 25.1% to 15.8%, but its price level did not carry forward. The model was not connected.
- The modelling recommendation of 2026-11-18 argued against trees and neural networks. The effective sample is about 2,000 projects with about 4,000 distinct items, not the row count. A tree memorises item identity, while real use is new wording. And an estimate must be explained to owners and reviewers, coefficient by coefficient.
- The recommended next model, a hierarchical log-linear mixed model (partial pooling), was never built. The nearest thing tried is the thin-cell shrinkage of round 13 (M9).
- Regression-style corrections failed forward in time: ridge centring by size, project type or title words (error 21.1% to 25.2–27.8%; type effects flip between years), owner effects, dispersion-based margins. The common cause: the municipal sample is small and effects change by year.
- Even with every median unit price right, the design leaves about 8.5% error on municipal work (M12). Municipal is at about 20%. The gap is in item pricing, not in the model form.

### 3.3 Why not use engineer estimates as an input or a target?

As a feature, the model would learn what the engineer would guess, feed back the bias it is meant to correct, and inflate the back-tests. Engineers are not exact either (section 4.10). And estimates are rare in public data: only 72 municipal lettings parsed with an estimate column.

### 3.4 Why not train on the private company files?

They are the test set. The project exists to check whether a benchmark built from public data predicts the company's own projects. If even one parameter were partly fitted on those files, testing on them would mean nothing. That such a leak would move a parameter only slightly is no defence.

### 3.5 Why not read drawings?

Engineers are more accurate mainly because they know the engineering conditions: drawings, the site, the current market. But full plan sets can be downloaded for only 14.5% of public lettings, and the project decided to use text only. Reading dimensions out of descriptions works (306 of 309 values right). But the family classifier is right on only 78.6% of unseen wording, so this needs a forward test before it is connected.

### 3.6 Why line pricing plus comparables, and not one of them?

Pure CMP breaks the floor: its P80 came in under on 22.0% of lettings. Its quality follows coverage. Where comparables cover 80% or more of the dollars, its error is 12.8% (line pricing 17.9%, engineers 13.3%). Where they cover less than half, it is 36.1%. Line pricing is steadier on new wording. The equal-weight geometric mean was the registered rule and was confirmed (section 4.4). No other weight was ever tested (M7).

### 3.7 Alternatives considered at each step

**Line prices**

| Alternative | Deciding numbers | Decision |
|---|---|---|
| Eight "thin reference" variants | Pre-registered, leave-one-out with a paired bootstrap: none qualified. Shrinking every tier made TxDOT worse (M9). | Not adopted |
| A price factor per city | 23.8% to 24.6%: worse. | Not adopted |
| `other` lines priced from the most similar descriptions (O1) | Lines much closer, but the letting overshoot rose from 51.3% to 54.0%. | Not adopted |
| Municipal-only cells (MC, CORE) | Development overshoot fell. On the locked set the under rate went from 18.8% to 29.2%. | Off, waiting for new lettings |
| Municipal price level for TxDOT-dominated cells (P_L) | Overshoot 43.1% to 38.0%, mostly from a tighter margin (multiplier 1.309 to 1.288). | Off, waiting for new lettings |
| Statewide erosion/drainage prices (S1) | Passed the rule, but the 0.9-point gain is inside the noise and the 2024 under rate rose 2 points. | Not installed |
| Lump sums as a share of the job; printed mobilization caps | The lines got much closer; the totals did not (M13). | Not adopted |
| Unit conversion | A rarer unit is often another product (M14). | Hint only |

**Escalation**

| Alternative | Deciding numbers | Decision |
|---|---|---|
| No escalation | With the index, the then-current estimator went from 26.8% to 24.8% (municipal) and from 27.2% to 25.1% (TxDOT). | Required |
| Dollar-weighted index | Fit to TxDOT's official Highway Cost Index (HCI) got worse: mean absolute deviation 4.66 to 9.40 points, correlation 0.81 to 0.59 (M8). | Not adopted |
| The official HCI itself | The TxDOT arm follows its shape (correlation 0.84) but not its level (142.2 against 154.9). | Cross-check only |
| Input-cost indices | Not transaction prices. | Excluded by rule |
| An erosion-basket index | Ten-year cumulative 163.75 against 139.82 for all items (2016 = 100), but unstable turning points, and statewide. | Not used |
| Carrying a missing last year for every arm | Cost 4.0 points of in-band share (90% interval −8.0 to −0.8) with no gain on the floor. | Narrow carry |

**Competition discount**

| Alternative | Deciding numbers | Decision |
|---|---|---|
| Discount on each unit price | Same 831 lettings and price level. On the total: estimate ÷ actual 0.9861, median error 6.5%. On unit prices: 0.9956, 8.4%. The total wins on 517 lettings (sign test p = 1.9e-12). The unit route can fully price only 41% of lettings. | On the total |
| Nine municipal bins instead of one shift | Out of fold: nine bins 0.9975 / 7.09%; pooled × one shift 1.0016 / 7.59%; pooled unshifted 1.0230 / 6.41%. Over 400 half-splits the shift won exactly 200. | One shift |
| One table for both markets | At matched bidder counts the municipal ratio is lower in 11 of 11 bins (sign test p = 0.0010; stratified test p = 1.45e-14). | Municipal table |
| Size-dependent discount for municipal work (D1) | Accuracy unchanged. It moves where P80 lands, not how accurate P50 is. | TxDOT only (T1) |
| Refusing bidder counts above the top bin | With the clamp, 35 of 36 refused lettings are priced, and none of the 10 municipal lettings with 16–21 bidders came in under. | Clamp |

**Level corrections.** A shared level cancels out of P80 (M4). Seven were tried and none lowered the overshoot:

- ridge centring by size (round 2): the coefficients drift and change sign;
- a 36-month calibration window (round 4);
- a separate correction for facility jobs (round 7): identical to the baseline;
- a correction per owner (round 9): an owner's past residual does not predict the next one (correlation +0.035);
- size-classed centring (round 12): it removed P50's size bias (1.26 / 1.07 / 0.94 to 1.10 / 1.05 / 1.00) and left the overshoot unchanged;
- recency-weighted centring (round 15): the level change is a step from 2023 to 2024, not a trend;
- a separate factor for erosion/drainage.

**The P80 rule**

| Alternative | Deciding numbers | Decision |
|---|---|---|
| A multiplier from the last two years' errors | Only 74.3% of municipal lettings not under: the window chases the last period's error. | Dropped |
| Leave-one-out calibration | Looked clean, but forward the 2025–26 lettings came in under about 24% of the time (section 4.2). | Forward calibration |
| Fixed 20th percentile, no guard | 125 new municipal lettings: 22.4% under, 13.6% in band. With the guard: 19.2%, 8.8%. | Guard |
| Guard aimed at 20% | 21.0% under (2026: 25.0%). Aimed at 16%, cap 20%: 17.0% under, 14.8% in band. Cap at 16% too: 16.5%, 8.0%. | Target 16%, cap 20% |
| Margins by predictable dispersion, by size or by thin evidence | Municipal: no significant gain, or the wrong direction. | Not adopted |

---

## 4. How accuracy is measured

### 4.1 Metrics

All ratios are to the letting's actual low bid. Under the coverage scope that is the low bid minus the low bidder's own amounts on the uncovered lines.

| Metric | Exact definition | Role |
|---|---|---|
| P80 under rate | Share of lettings with P80 ÷ low bid below 1. | The floor: at most 20%. |
| Median overshoot | Median of P80 ÷ low bid − 1 over the lettings with P80 ÷ low bid at or above 1. | The score. |
| In band | Share of lettings with P80 ÷ low bid within 1.00–1.13. | Reported only. |
| P50 median error | Median of the absolute value of P50 ÷ low bid − 1. | Reported. |
| Mean abs log error | Mean of the absolute value of ln(P50 ÷ low bid). 0.41 is a typical error of about 50%. | Decision rules. |
| P80 multiplier | Median of P80 ÷ P50, which is 1 ÷ divisor. | Reported. |
| Overshoot at a matched not-under rate | Each method gets its own single multiplier so that exactly 70%, 75% or 80% of its lettings are not under. The median overshoot of those lettings is reported. In sample. | Yardstick comparison (4.11). |
| Line error sum | Sum over lines of the absolute difference between our amount and the median bidder's. Lines: schedule lines of the scored municipal lettings with at least 3 bidders, not owner-set, not uncovered, priced by both sides. "Our amount" is price × quantity before the discount, the blend and the centring. | Track U (4.8). |
| Line log error | Absolute value of ln(our unit price ÷ the median bidder's), per line. | Track U. |

In the public line tables (section 6), "half of lines within X%" means X = exp(median line log error) − 1, and "within ±20%" means a ratio between 0.833 and 1.2.

### 4.2 Forward in time

- **Rule.** A letting dated D is priced with everything refitted on lettings dated strictly before D: reference tables, index arms, discount tables, comparables, centring, the P80 divisor and the guard. Lettings that share a date do not see each other. Constants need at least 40 earlier forward-scored lettings of the same market.
- **Scoring population.** Exactly one identified low bidder, at least two bidders, at least 3 schedule lines (`MIN_SCHEDULE_ITEMS`), a date, at least 90% of the covered lines priced, and not "not covered as a whole".
- **Windows.** Municipal lettings dated 2024-01-01 or later; TxDOT lettings dated 2023-01-01 or later. Results are also reported per year.
- **One difference from real use.** The back-test uses the actual number of bidders.

Why so strict: on 2026-11-19 a correction looked clean under leave-one-out (230 municipal lettings, 20.0% under, in-band share up from 7.4% to 17.1%). Split by year, the median P50 ÷ low bid drifted down: about 1.04–1.06 in 2021–23, 0.97 in 2024, 0.88 in 2025, 0.95 in 2026. With constants set from earlier years only, the under rate was 24.6% (24.1% in 2025–26), over the floor. Leave-one-out lets later lettings into the tables, the index and the constants. Since then, any change that moves the price level or the P80 margin is scored forward and reported by year.

### 4.3 Leak guards

- **`train_test_guard`** proves the gate by a counterfactual, not by reading a label. For every fitted quantity it compares the value on disk, the value recomputed from external rows only, and the value recomputed with one test set admitted. Disk equal to external-only and different from admitted: the gate held. Disk equal to admitted: a leak. External-only equal to admitted: the test rows could not have moved this number, so the guard reports "no evidence", not a pass.
- **`provenance_check`** guards the input side: which files are test files, and whether the same solicitation sits on both sides.
- **`company_twin_scan`** looks for public copies of private test projects before a rebuild. A copy is recognised by the overlap of its (quantity, unit price) pairs, which is complete for a true copy and far lower for an unrelated letting. Copies are quarantined.

### 4.4 The locked public test set

**How it was drawn** (`src/public_holdout.py`, version 2):

- population: public municipal lettings dated 2022–2026 that production could score forward. Only keys, dates and work types were read, never a price or an error;
- a stratified random draw: 22% (`FRACTION = 0.22`) of every year × work-type stratum (erosion/drainage against other), with `random.Random(SEED + year)` and `SEED = 20261119`;
- the unit drawn is the source file with every copy of it, so no twin can carry a held-out letting back into training;
- result: 59 groups, 9 of them erosion/drainage (34 such groups stay in training); 71 files; 59 scorable lettings. `--select` refuses to draw again.

Version 1 was retired before anything was scored. Half of it was erosion/drainage: it locked away 30 of about 43 such groups and left none in training for 2023 and 2026. The lesson: do not over-sample a scarce, important stratum into the test set.

**How it is kept out.** The parser tags these files `source_set = "public_test"`. Every fitted quantity admits `external` only, and the train/test guard proves it cell by cell.

**When it may be opened.** Only to confirm a decision registered beforehand in `data/test_holdout/REGISTRATIONS.md`. Never to choose between options, and never to tune. `src/holdout_eval.py --confirm "<decision; where registered>"` writes a row to `access_log.csv` before it scores and another when it ends. Development uses a decoy (`--decoy SEED`): public lettings drawn with the same design and held out in process. A held-out letting is scored exactly as a new letting in production, and never moves the guard or another test letting's constants.

**The four openings.** The set holds municipal lettings only.

| # | When | Decision | Lettings | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 2026-11-19 22:47 | Does the production rule hold the floor? | 58 | Under 8 of 58 = 13.8% (90% interval 6.9–20.8%); overshoot 38.2%; P50 error 22.4% | Confirmed. "Half clean": the 16% guard target had been set on data that still held these lettings. |
| 2 | 2026-11-20 02:38 | V2 | 57 | Current rule 14.0% under, 46.3% overshoot. V2: 17.5%, 47.4%. P50 error 21.2% to 15.6% | Not confirmed: the overshoot did not shrink. |
| 3 | 2026-11-20 04:22 | V2+F1+F2 | 56 | Under 16.1% to 19.6% (11 of 56); overshoot 35.9% to 31.0%; P50 error 18.4% to 13.6%; multiplier 1.396 to 1.311 | Confirmed; installed. |
| 4 | 2026-11-20 11:23 | CORE | 48 | Under 18.8% to 29.2% (+10.4 points, 90% interval +4.2 to +18.8); overshoot 30.4% to 33.4%; multiplier 1.391 to 1.310 | Not confirmed; switches stay off. |

**Limits.**

- With about 59 lettings, the 90% interval of the under rate is about ±8 points.
- The four openings ran on different data builds, and the current rule's own result moved with the data (overshoot 38.2%, 46.3%, 35.9%, 30.4%). They are not one comparable series.
- Every opening makes the set a little staler. After the fourth, municipal changes are confirmed on new lettings dated after 2026-11-20 (section 4.9). The set has not been opened since.

### 4.5 The private company test set

A private set of company bid files is kept as a second test set. Its rows carry `source_set == "internal"`. Nothing is fitted, tuned or selected on it, and the guards check this at every build. Its results stay local.

### 4.6 Pre-registration

Before a round is scored, a file fixes the arms to try, the md5 of the data and the code, the pass/fail rule and the tie rule. The file's md5 and time are recorded. It is not edited afterwards; an addition is a dated addendum that says whether it came before the scoring. Two mechanical checks precede every round. The baseline B must reproduce production letting by letting and field by field (round 6: 2,063 lettings × 17 fields, 0 differences). And the scorer, run with B standing in for an arm, must report no change.

"B" always means the production rule of the time. An "arm" is a variant under test. Results read "B to arm".

### 4.7 The paired bootstrap

B and the arm are scored on the lettings that both can score. Lettings are resampled with replacement 2,000 times (numpy seed 20261119). On each resample both sides' metric is recomputed (a median is recomputed, not averaged) and the difference taken. The 5th and 95th percentiles of the differences give the 90% interval. The locked-set scorer does the same over file groups (`BOOT_N = 2000`, `BOOT_SEED = 20261120`). Why paired, and why lettings: M10.

### 4.8 The adoption rule

**History.** Rounds 1–4 required a significant gain in municipal mean abs log error, the floor, and an in-band share no more than 1 point below B's. From round 5 the in-band gate gave way to conditions (a)–(c) below. Round 11 added (d) and two scoring modes: *own* (the arm relearns its own P80 margin) and *B80* (it uses B's margin). Arms within 2.0 points of the lowest overshoot are tied; a tie prefers B80, then the simpler arm. Round 14 added a line track. Since 2026-11-21 the rule is "adoption rule v2" (`handoff/scratchpad/p50fix/ADOPTION_RULE_2026-11-21.md`).

**Adoption rule v2.** The tool is a working aid for preparers, who look at unit prices and pick them. So more accurate line prices are worth installing on their own, as long as the totals do not get worse. A change goes live when it passes track T, or track U (or U+).

| Track | Conditions, all required |
|---|---|
| **T**: the totals get better | **(a)** Floor: municipal under rate at most 20.0% overall, and in each of 2024, 2025 and 2026 at most max(20.0%, B's rate on that year's same lettings + 3.0 points). **(b)** Municipal median overshoot strictly below B's on the same lettings, and TxDOT not worse. **(c)** Paired 90% interval of the difference in municipal mean abs log error of P50: its lower end must not be above 0. **(d)** Paired 90% interval of the difference in municipal median overshoot: its upper end must be below 0. |
| **U**: the unit prices get better and nothing else gets worse | **(a)** The P80 under rate is not higher than B's, overall and in each registered year. Hard. **(L)** The line error sum falls, with the upper end of its 90% interval below 0, and the median line log error does not rise. **(U0)** For both the median P50 error and the median overshoot: the 90% interval of arm − B has its lower end at or below 0, and the point estimate is at most +0.5 point (P50 error) and +1.0 point (overshoot). **(c)** TxDOT not worse. |
| **U+**: a large unit-price gain buys a small loss on the totals | If the line error sum falls by at least 5% relative to B (upper end of its interval below 0) and the median line log error does not rise, (U0) is replaced: the point estimates of the median P50 error and of the median overshoot may each be up to +1.0 point worse than B, with no significance test. (a) and (c) stay. |

- Ties between passing arms: the larger line improvement, then the simpler change.
- **Reading of (a) in tracks U and U+**: "not higher than B", overall and per year. It is not "at most 20%". Round 18 applied it. Two arms improved unit prices by 11.6% and 6.1% but raised the municipal under rate from 14.29% to 15.62% and 15.18%, so they failed. The arm X1 (clamp + abandonment flag, with B80) passed tracks T and U and was installed.
- The order is lexicographic: first the floor, then accuracy inside it.
- Parser changes are judged by their own rule, with one precedent: lettings whose parsed low bid the change itself corrected are looked at separately.

### 4.9 Prospective checks

The locked set is spent for further municipal decisions, so changes are confirmed or revoked on new lettings. Every check is paired and forward, with the code and data of that time.

| Item | State | Trigger | Rule |
|---|---|---|---|
| T1 (TxDOT) | on | 100 DFW TxDOT lettings bid after 2026-11-20, or 2027-03-31, whichever comes first | With T1 on: under rate at most 20%, and median overshoot below T1 off. If either fails, T1 is switched off. |
| `round11_core_not2` | off | At least 40 scorable DFW municipal lettings dated after 2026-11-20 | Switched on only if its under rate is at most 20% and its median overshoot is strictly below production's on the same lettings. |
| `round13_l` | off | The same 40 | The same. Jobs of $2M and more are reported separately. |
| Round 18 (clamp + abandonment + B80) | on | The same 40 | Switched off in both markets if the under rate is above 20%, or the median overshoot is more than 1.0 point above production B's. |
| Round 20 arm G (parser .34b) | on | The same 40 | The parser goes back to .33, with a rebuild, if the under rate is above 20%, or the median overshoot is more than 1.0 point above the .33 run's. |
| Guard target 16% | on | Lettings of 2027 | Under rate above 20%: lower the target by 2 points. Under rate at most 18%, with an in-band share 3 or more points below the 20% target's: consider 18%. |
| Narrow carry | on | Lettings from 2027-01 | Registered for an out-of-sample check. |

Open point: rounds 11 and 13 were selected in *own* mode, and production now runs B80 by default. The mode in which they are scored at the check must be written down before the check.

### 4.10 Why engineer estimates are only a yardstick

The rule is in section 3.3. The engineers' own record sets what "accurate enough" means. All figures are engineer estimates on public lettings.

| Set | Lettings | Median error | Below the low bid | In band | Median overshoot when over | Overshoot lifted to 80% not under |
|---|---|---|---|---|---|---|
| DFW municipal, checked against the originals | 140 | 15.2% | 32.9% | 22.9% | 18.7% | 25.1% (× 1.089) |
| The first 12 municipal lettings, corrected | 12 | 13.3% | 25% | 42% | 11.5% | — |
| TxDOT DFW, earlier benchmark | 1,754 | 13.7% | 27.1% | 28.9% | 16.5% | 20.5% (× 1.049) |
| TxDOT DFW, official totals (README, current) | 451 | 21% | 14% | — | — | 23% |
| TxDOT streambank, statewide | 355 | 14.1% | 26% | 32% | 15.1% | — |

- **The two TxDOT rows do not agree, and the records do not reconcile them.** The records do say that an early TxDOT comparison summed the engineer's line items from the panel, that about a tenth of lettings then carried only about half of the official total, and that the comparison was redone with official totals. The README row is the current one.
- **The first target was above the engineers' own level.** "Half in band, overshoot at most 13%" came from the first 12 lettings. After a check against the originals, 5 of 12 are in band (42%, not half), and none of the 12 is erosion or drainage work. On 140 lettings the engineers' in-band share is 22.9%.
- **Engineers have a structural bias too.** With 1–2 bidders, 64% of municipal estimates are below the low bid; with 6 or more, 16.7%. Their error is 23.6% on jobs under $250k and 10.4% on jobs over $5M.
- **Same lettings, both sides.** On the 451 TxDOT lettings with an official estimate, the estimator's median error is 17% against the engineers' 21%. At 80% not under, the engineers overshoot 23% against 30%. On municipal work P50 is at about 20% against about 15%.

### 4.11 Comparing at a matched not-under rate

A more cautious estimate has fewer unders and more overshoot. So two methods with different under rates cannot be compared on overshoot directly. Each method is given its own single multiplier that makes exactly 70%, 75% or 80% of its lettings not under, and the median overshoot is read there (derivation in M5).

| | 70% | 75% | 80% |
|---|---|---|---|
| Municipal, estimator (235 lettings) | 30.4% | 32.1% | 33.5% |
| Municipal, engineers (140 lettings) | 19.8% | 21.3% | 24.9% |
| TxDOT, estimator (807 lettings) | 26% | 27% | 30% |
| TxDOT, engineers (451 lettings) | 19% | 22% | 23% |

"Caught up with the engineers" was defined on 2026-11-20 as: at 80% not under, an overshoot of 20–25% or less; or, at the engineers' own not-under rate (about 67–70%), an overshoot below 19–20%.

---

## 5. Mathematical notes

Each note gives the claim, the derivation, the project's numbers and the naive alternative. Numbers already tabled in sections 2–4 are referenced, not repeated.

Notation: $r = P50/\text{low}$; $\ln$ is the natural logarithm; $q_\alpha(\cdot)$ is the α-quantile; $\Phi$ and $\phi$ are the standard normal distribution function and density; $z_p = \Phi^{-1}(p)$, with $z_{0.2} = -0.8416$ and $z_{0.6} = 0.2533$; n is the number of bidders; $\mathbf{1}[\cdot]$ is 1 when the condition holds and 0 otherwise.

### M1. Forward-in-time evaluation and leak guards

**Claim.** Test the way the tool is used: with the future unknown. Random folds and leave-one-out measure a quantity that does not exist in use.

**Derivation.** Split the log low bid of letting j into a market level at its date and a job part:

$$\ln \text{low}_j = L_{t_j} + u_j$$

An estimator trained on a set $\mathcal T$ has the log error

$$e_j = \ln\frac{P50_j}{\text{low}_j} = \left(\hat L_{\mathcal T} - L_{t_j}\right) + \left(\hat u_j - u_j\right)$$

which is a level error plus a job error. With random folds, $\mathcal T$ holds lettings of j's own period, and the level error is nearly 0. In use, $\mathcal T$ holds only lettings with $t_i \lt t_j$, and the level is extrapolated. With uncorrelated parts,

$$E_{\text{forward}}[e^2] \approx E_{\text{random fold}}[e^2] + E\left[(L_t - \hat L_{\lt t})^2\right]$$

The level does jump: the TxDOT index rose 24.2% in 2022 and fell 15.0% in 2026. And the P80 constants set the low tail, where a level error shows first.

A leak is tested the same way, by a counterfactual: a fitted $\theta$ on disk must equal $\theta_{\text{ext}}$ (external rows only) and differ from $\theta_{\text{ext+test}}$ (section 4.3).

**Numbers.** The same three-tier estimator scored 19.9% under random 5-fold and 26.8% forward (municipal). The final v2 unit-price model scored 12.7% against 15.8% (TxDOT). Leave-one-out gave 20.0% under where forward gave 24.6% (section 4.2). For the locked set, 59 lettings at an under rate near 16% give a 90% half-width of $h = 1.645\sqrt{0.16 \times 0.84 / 59} \approx 7.8$ points (model-computed).

**Naive alternatives.** Random folds and leave-one-out, as above. Choosing on development data and reporting on it: CORE passed in development and missed the floor on the locked set. Over-sampling a scarce stratum into the test set: version 1 of the locked set.

### M2. Error metrics: ratios, logs, medians

**Claim.** Cost errors are multiplicative. So accuracy is measured on ratios, in logs, with medians.

**Definitions.** With $r_j = P50_j / \text{low}_j$ over N lettings:

$$\text{P50 median error} = \underset{j}{\operatorname{median}} \lvert r_j - 1 \rvert, \qquad \text{mean abs log error} = \frac{1}{N}\sum_j \lvert \ln r_j \rvert$$

$$\text{under rate} = \frac{1}{N}\sum_j \mathbf{1}[P80_j \lt \text{low}_j], \qquad \text{median overshoot} = \operatorname{median}\lbrace P80_j/\text{low}_j - 1 : P80_j \ge \text{low}_j \rbrace$$

For line i, with our unit price $\hat p_i$ and the median bidder's $\tilde p_i$, the line log error is $\ell_i = \lvert \ln(\hat p_i / \tilde p_i) \rvert$. With amounts a = quantity × unit price, the line error sum is $S = \sum_i \lvert \hat a_i - \tilde a_i \rvert$. In the public tables, "half of lines within X" means $X = e^{m} - 1$ with m the median of the $\ell_i$, and "within ±20%" means $\ell_i \le \ln 1.2$. In band means $P80/\text{low} \in [1.00, 1.13]$.

**Why.**

1. Costs multiply. Without the blend, bid part × centring is $\left(\sum_i q_i \hat p_i\right) \times f \times lc$. Level, discount and centring errors hit the whole job and add in logs: $\ln \sum_i q_i \hat p_i + \ln f + \ln lc$. M11 relies on this.
2. Logs are symmetric. Twice and half are equally far: $\lvert \ln 2 \rvert = \lvert \ln \tfrac12 \rvert = 0.693$, where $\lvert r - 1 \rvert$ gives 1.0 and 0.5.
3. Ratios are scale-free.
4. Medians resist the tail. One letting at 9.6× its low bid adds 8.6 to a mean of $\lvert r - 1 \rvert$.
5. By the triangle inequality, $\lvert \sum_i (\hat a_i - \tilde a_i) \rvert \le S$. So S bounds the money, while the median of the $\ell_i$ gives each line one vote. Either one alone can be improved at the other's expense.

**Numbers.** A mean abs log error of 0.331 is a typical factor of $e^{0.331} \approx 1.39$ (model-computed). Round 20's baseline: a line error sum of 340.17 million dollars over 10,236 lines, and a median line log error of 0.3766, which is the "46%" of the public tables.

**Naive alternatives.** The P50 error alone says nothing about the floor. The under rate alone is met by any huge multiplier. A mean absolute percentage error is ruled by the high tail: an under-estimate is at most 100%, an over-estimate can be 900%.

### M3. P80 is a calibrated quantile

**Claim.** P80 is a calibrated quantile, not a chosen margin. Its coverage needs no assumption about the error distribution, and a guard corrects it when the market drifts.

**Formula.** With bid part $c_i$, owner-set money $F_i$ and low bid $w_i$ of the n earlier lettings:

$$\tilde r_i = \frac{c_i \cdot lc + F_i}{w_i}, \qquad p = \tilde r_{(k)}, \qquad k = \lfloor \alpha (n - 1) \rfloor + 1, \qquad P80 = \frac{P50}{p}$$

"Under" means $\tilde r_{\text{new}} \lt p$.

**Coverage.** If the new ratio is exchangeable with the n earlier ones, its rank among all n + 1 values is uniform, and "under" is "rank at most k":

$$\Pr(\text{under}) = \frac{k}{n + 1} \approx \alpha$$

This is the conformal-prediction argument (standard result). Example: n = 400 and α = 0.148 give k = 60 and 15.0% (model-computed).

**Why a quantile.** Let a dollar under cost $\tau/(1-\tau)$ times a dollar over:

$$L_\tau(y, \hat y) = \tau \max(y - \hat y, 0) + (1 - \tau) \max(\hat y - y, 0)$$

With F the predictive distribution of the low bid, the expected loss is smallest where

$$-\tau \left[1 - F(\hat y)\right] + (1 - \tau) F(\hat y) = 0 \quad \Rightarrow \quad F(\hat y) = \tau$$

That is the τ-quantile (standard result). τ = 0.8 says a dollar under costs four times a dollar over. P50 is τ = 0.5.

**The guard.** Drift breaks exchangeability. The code uses a one-sided form of adaptive conformal inference. Let $u_j = 1$ when letting j's P80 came in under, else 0. After each letting date:

$$\alpha \leftarrow \operatorname{clip}\Big(\alpha + \gamma \sum_{j \in \text{date}} (0.16 - u_j), 0.02, 0.20\Big)$$

- Fixed point: $E[\Delta\alpha] = \gamma (0.16 - \Pr(\text{under})) = 0$, an under rate of 16%.
- Unclipped, the rule guarantees (standard result) that after T lettings the average under rate is within $\frac{\max(\alpha_1, 1 - \alpha_1) + \gamma}{\gamma T}$ of the target, whatever the drift. With the clip, this holds while α stays off its bounds.
- The cap at 0.20 makes the rule one-sided. Uncapped, the low-miss year 2023 narrowed the margin and 2024 then missed 25.5%.
- Why 16% and not 20%: with about 60 lettings a year, the count of unders is roughly Binomial(60, p), and "over 20%" means 13 or more. Aimed at 0.20 that happens in about 42% of years; aimed at 0.16, in about 15% (model-computed).

**Numbers.** Section 2.8. With a multiplier of 1.362, P80 lands in band (1.00–1.13) only when P50 ÷ low bid is between 0.734 and 0.830 (model-computed). That narrow window is why the in-band share is hard.

**Naive alternatives** (numbers in section 3.7). A multiplier from recent errors chases the last period. A fixed 20th percentile ignores drift. A normal assumption, $P50 \times e^{0.84\sigma}$, fails on a tail that is heavier than normal and not symmetric. "P50 + 10%" comes in under on about 30% of city jobs.

### M4. Why centring cancels in P80

**Claim.** A factor shared by all lettings cancels out of P80 ÷ low bid. The overshoot depends on dispersion only.

**Derivation** (own mode). Let $x_i = c_i / w_i$ for the earlier lettings and x for the new one, without owner-set money. Centring gives $lc = 1/\operatorname{median}(x_i)$. Quantiles are homogeneous: $q_\alpha(cX) = c \cdot q_\alpha(X)$ for a positive c. So

$$\frac{P80}{\text{low}} = \frac{lc \cdot x}{q_\alpha(lc \cdot x_i)} = \frac{lc \cdot x}{lc \cdot q_\alpha(x_i)} = \frac{x}{q_\alpha(x_i)}$$

lc is gone, and so is any common factor. In steady state the under rate is α. The lettings that are not under are the top 1 − α share of x, and their median is the $(1+\alpha)/2$ quantile. The median overshoot is therefore

$$\frac{q_{(1+\alpha)/2}(x)}{q_\alpha(x)} - 1$$

a ratio of two quantiles. If $\ln x$ is normal with standard deviation s, then $P80/\text{low} = \exp\lbrace s (Z - z_\alpha) \rbrace$ with Z standard normal. The median overshoot is $\exp\lbrace s (z_{(1+\alpha)/2} - z_\alpha) \rbrace - 1$, which is $e^{1.095 s} - 1$ at α = 0.2 (model-computed). The level does not appear.

**Two exceptions.**

- Owner-set money is added after centring, $P50 = c \cdot lc + F$, so a little leaks through. 165 of 397 municipal lettings carry it, at a median 4.3% of the estimate.
- Under B80 the divisor is B's. A pure level change by a factor k is still absorbed by the arm's refitted centring, which becomes lc ÷ k. But a change of the centring rule shifts P80: round 15's recency weighting under B80 moved the overshoot from 46.1% to 47.2%.

**Numbers.** Median P50 ÷ low bid by year: 0.857 (2021), 0.957, 0.923, 1.084 (2024), 1.011, 1.056 (2026). That is a step, not a trend. Year means explain 0.2% of the variance of ln(P50 ÷ low). Centring every year exactly, after the fact, moved the matched-80% overshoot the wrong way, from 36.2% to 37.8%.

**Naive alternative.** Tuning the level: seven attempts (section 3.7). Centring keeps P50 unbiased. The overshoot falls only when single jobs are priced more accurately.

### M5. Comparing overshoot at a matched not-under rate

**Claim.** A method traces a curve of overshoot against not-under rate, like a ROC curve. Methods can only be compared at the same not-under rate, and that comparison measures dispersion alone.

**Derivation.** Multiply an estimate E by m, and let r = E ÷ low. Both

$$c(m) = \Pr(m r \ge 1) \qquad \text{and} \qquad o(m) = \operatorname{median}\lbrace m r - 1 : m r \ge 1 \rbrace$$

rise with m. The procedure, in sample: sort low ÷ E ascending, take $k = \lceil cN \rceil$, set m to the k-th smallest value so that exactly k lettings are not under, and take the median of m r − 1 over those k. For large N,

$$o(c) = \frac{q_{1 - c/2}(r)}{q_{1 - c}(r)} - 1, \qquad o(0.8) = \frac{q_{0.6}(r)}{q_{0.2}(r)} - 1$$

m has cancelled. The result is the distance between the 20th and the 60th percentile of ln r. Under a log-normal model $o(0.8) = e^{1.095 s} - 1$. Then 13% means s ≈ 0.11, 25% (engineers) means s ≈ 0.20, and 33.5% (the estimator on municipal work) means s ≈ 0.26 (model-computed).

**Numbers.** Section 4.11. An early log-normal calculation said the spread had to fall from about 0.37 to about 0.11 to reach the target. The overshoot at that time, 51.7%, corresponds to ln 1.517 ÷ 1.095 ≈ 0.38 (model-computed).

**Naive alternative.** Comparing the engineers' raw 18.7% overshoot with the estimator's 45%, or their 33% under rate with its 14%. These are points at different places on two curves.

### M6. The competition discount is an order statistic

**Claim.** The low bid is the first order statistic of N bids. That fixes the discount's form: deeper with more bidders, deeper with more dispersion, and applied to the total.

**Derivation.** Let the totals be $B_b = M e^{\sigma Z_b}$ with independent standard normal $Z_b$. A shock common to all bidders sits in M and cancels. Since $E[Z_{\text{med}}] = 0$ by symmetry,

$$\ln\frac{\text{low}}{\text{med}} = \sigma \left(Z_{(1:n)} - Z_{\text{med}}\right), \qquad E\left[\ln\frac{\text{low}}{\text{med}}\right] = \sigma E[Z_{(1:n)}]$$

$$E[Z_{(1:n)}] = \int_{-\infty}^{\infty} z \cdot n \cdot \phi(z) \left[1 - \Phi(z)\right]^{n-1} dz$$

For n = 2 this is $-1/\sqrt{\pi} = -0.564$. So the discount deepens with n at a falling rate (the tenth bidder adds 0.054σ, about 0.8% at σ = 0.147; model-computed), its depth is proportional to σ, and n = 1 has no order statistic.

| n | Expected minimum (model-computed) | Model: exp(0.147 × expected minimum) | Measured, pooled |
|---|---|---|---|
| 2 | −0.564 | 0.920 | 0.935 |
| 3 | −0.846 | 0.883 | 0.898 |
| 4 | −1.029 | 0.860 | 0.868 |
| 5 | −1.163 | 0.843 | 0.856 |
| 6 | −1.267 | 0.830 | 0.837 |
| 8 | −1.424 | 0.811 | 0.846 |
| 10 | −1.539 | 0.798 | 0.833 |
| 12 | −1.629 | 0.787 | 0.801 |

0.147 is the median within-letting standard deviation of log bids. Up to 6 bidders the model and the data differ by 1–1.5 points. At 8 and 10 bidders the data sit clearly above the model; the records do not test why. **Production uses the measured table. The model only explains its shape.**

**Why municipal is deeper.** Municipal bids are more dispersed: σ is 0.160 against 0.143. That alone predicts a ratio between the markets of $\exp\lbrace (0.160 - 0.143) E[Z_{(1:n)}] \rbrace$, which is 0.976–0.986 for n from 3 to 8 (model-computed). The measured factor is 0.9752.

**Why on the total.** The low bid is the minimum of totals, not the sum of per-line minima:

$$\text{low} = \min_b \sum_i a_{bi} \ge \sum_i \min_b a_{bi}$$

The winner is not lowest on every line. f is measured on totals, so it is the same quantity only when it is applied to a total. In the test of section 3.7, 517 wins out of 831 is 7.0 standard deviations above a fair coin (model-computed).

**Why one shift.** A bin's f is the median of the very ratios it is scored against, so its in-sample score of 1.0000 is forced by arithmetic. Out of fold, nine bins bought nothing. The unshifted pooled table has the lowest error but runs 2.3% high. The project chose an unbiased centre, on the written judgement that an estimate running systematically high loses jobs it would have won.

**Naive alternatives.** Averaging past low bids mixes the price level with the bidder count. No discount at all runs 15.6% high.

### M7. The comparables blend is an average in log space

**Claim.** Two imperfect estimates of one quantity should be averaged in log space. The equal-weight geometric mean beats the better one when their errors are not too correlated.

**Derivation.** With LP the line-priced bid part,

$$\text{bid part} = \sqrt{\text{CMP} \times \text{LP}}, \qquad \ln(\text{bid part}) = \tfrac12 \ln \text{CMP} + \tfrac12 \ln \text{LP}$$

Let $e_1 = \ln(\text{LP}/y)$ and $e_2 = \ln(\text{CMP}/y)$ have standard deviations $\sigma_1$ and $\sigma_2$ and correlation ρ. The blend's error $e = \tfrac12 (e_1 + e_2)$ has

$$\operatorname{Var}(e) = \tfrac14 \left(\sigma_1^2 + \sigma_2^2 + 2 \rho \sigma_1 \sigma_2\right)$$

With equal σ this is $\sigma^2 (1 + \rho)/2$: better than either one. In general, with $k = \sigma_{\max}/\sigma_{\min}$, the blend beats the better estimate if and only if $\rho \lt (3 - k^2)/(2k)$. The optimal weight on $e_1$ is (standard result)

$$w^{\ast} = \frac{\sigma_2^2 - \rho \sigma_1 \sigma_2}{\sigma_1^2 + \sigma_2^2 - 2 \rho \sigma_1 \sigma_2}$$

**Numbers** (the check is model-computed). Mean abs log errors in the pre-registered test: municipal (159 lettings) line pricing 0.380, comparables 0.348, blend 0.342; TxDOT (798 lettings) 0.389, 0.248, 0.277. As proxies for σ they imply ρ ≈ 0.77 and ρ ≈ 0.49.

- Municipal: k = 1.09, threshold 0.83. The blend should beat both, and it does. The optimal weight is about 0.32 on line pricing, for a gain of about 1%.
- TxDOT: k = 1.57, threshold 0.17. The blend should be worse than comparables alone, and it is. The optimal weight is about 0.12 (0.277 to 0.244).

The check is rough, because a mean abs log error also carries bias. No other weight was tested: V2 was registered and confirmed on municipal work only.

**Why geometric.** In logs, twice and half average to exactly 1; arithmetically they give 1.25. Since $(a + b)/2 \ge \sqrt{ab}$, the arithmetic mean runs high. The geometric mean's elasticity to CMP is ½ whatever the sizes. The arithmetic mean's is CMP ÷ (CMP + LP), so the larger one rules. If CMP doubles, P50 moves by $\sqrt 2 \approx 1.41$.

**Inside CMP** (rules in section 2.5). With O the overlap, $S_T$ and $S_C$ the two sizes without lump sums, and age in years, the similarity and the transferred unit price are

$$S = O \cdot e^{-\frac12 \lvert \ln(S_T / S_C) \rvert} \cdot g_{\text{owner}} \cdot g_{\text{type}} \cdot 2^{-\text{age}/3}, \qquad u \cdot \operatorname{clip}\left((q/q_C)^{\beta}, 0.5, 2\right) \cdot \text{esc} \cdot \frac{f(n_T)}{f(n_C)}$$

A lump sum takes $\operatorname{clip}(R^{\gamma}, 1/3, 3)$ in place of the quantity factor. β comes from the within-item regression $\ln u = \alpha_{\text{item}} + \beta \ln q + \varepsilon$. A β of −0.18 means that doubling the quantity multiplies the unit price by 0.88 (model-computed).

**Naive alternatives.** Comparables alone (section 3.6). An arithmetic mean, ruled by a swinging CMP.

### M8. The price index: matched, chained, median of ratios

**Claim.** A price index must compare the same item in two years, chain adjacent years, and take the median of the ratios.

**Formulas.** With $p_{itj}$ the median bid on item i in letting j of year t:

$$P_{it} = \underset{j}{\operatorname{median}} p_{itj}, \qquad M_t = \lbrace i : P_{i,t-1} \gt 0, P_{it} \gt 0 \rbrace$$

$$R_t = \underset{i \in M_t}{\operatorname{median}} \frac{P_{it}}{P_{i,t-1}}, \qquad I_t = I_{t-1} R_t, \qquad I(y + f) = I(y) \left[\frac{I(y + 1)}{I(y)}\right]^{f}$$

The last formula interpolates a fractional vintage. Item identity is source / owner / normalised description for municipal rows, and normalised description / unit for TxDOT.

**Why matched.** An unmatched yearly mean $\bar P_t = \sum_i s_{it} P_{it}$ mixes price moves with moves of the item shares $s_{it}$. The matched ratio holds the item fixed. What is not controlled leaks in. The raw yearly series for gabions rose 5.3% a year, and 3.3% once quantity was controlled. Gabions per CY went from +2.2% to −0.1%: all of the apparent rise (103% of it) was project-size drift.

**Why chained.** Items come and go, and adjacent years overlap most. Error accumulates along a chain, so non-adjacent years are not chained and thin pairs do not move the level.

**Why the median.** One item, one vote, and robust to outlier ratios from parse errors. It is time-reversible: $\operatorname{median}(p_0/p_1) = 1/\operatorname{median}(p_1/p_0)$ for an odd count. So is a geometric mean of ratios (the Jevons index). An arithmetic mean (the Carli index) is not: forward times backward is at least 1 (standard result).

**Why not dollar weights.** Weights $w_a = q_a p_a$ on the ratios r give a Laspeyres index, $\sum w_a r / \sum w_a$. With normalised weights the effective sample size is $n_{\text{eff}} = 1/\sum_i w_i^2$. Of 560–1,599 matched items a year, only 29–51 are effective: a few dozen bulk items would stand for the whole market.

**Numbers.** Sections 2.3 and 3.7. Engineer-estimate rows once leaked into the index and moved the TxDOT 2025–2026 step from −14.29% to −12.52% (values at the time of that fix).

**Naive alternatives.** No escalation; dollar weights; the official index as is; input-cost indices (section 3.7).

### M9. Layered reference prices and shrinkage

**Claim.** How specific a reference cell to use is a bias–variance trade. The tiers are an ordered back-off, and quantity belongs in the key.

**Derivation.** Pricing item i from the median of a cell C has roughly

$$\text{MSE}(C) \approx (\mu_C - \mu_i)^2 + \tau_C^2 / n_C$$

That is bias (the cell holds other things) plus variance (few lettings). A specific cell has little bias and few lettings; a coarse cell the reverse. "Most specific first, fall back when support is thin" is the back-off, and the minimum letting counts (2, 2, 1) are the test of "thin". Token similarity is $J(A, B) = \lvert A \cap B \rvert / \lvert A \cup B \rvert$, required to be at least 0.35.

**Shrinkage.** The alternative to back-off pulls a cell toward its parent:

$$\ln \hat p = w \ln p_{\text{cell}} + (1 - w) \ln p_{\text{parent}}, \qquad w = \frac{n}{n + k}$$

In levels this is $\hat p = p_{\text{cell}}^{w} p_{\text{parent}}^{1-w}$. The optimal k is the within-cell variance ÷ the between-cell variance (Bühlmann credibility; standard result). It was tried twice, with k fixed at 3 beforehand.

- Every tier: the TxDOT median error rose by 1.18 points (interval +0.52 to +2.11). A token cell is already better than its parent, so shrinking pulls toward bias.
- Last tier only (round 13, arm P_T), where one letting gives w = 0.25: the error of the 140 shrunk lines fell from $24.7M to $9.5M, but the job-total overshoot fell only 0.9 point, with an interval across 0 (M13).

Thin cells hurt only in the last tier. A family cell with one letting has a mean abs log error of 1.1–1.3. A token cell with 2 lettings has 0.52, and with 10 or more 0.47.

**Quantity.** The cross-project elasticity $\beta = \partial \ln p / \partial \ln q$ has a median of −0.141 over 547 thousand bids: twice the quantity gives × 0.907 (model-computed). One large Texas city's official tiered unit prices imply −0.154. But inside one bid form, the same item at different quantities (up to about 90 times apart) is hardly re-priced. So the cross-project figure is probably in part a composition effect of large against small projects. Hence the band is a key, and not a global elasticity. Where a shape is needed, it comes from two-way fixed effects, $\ln p_{ij} = \alpha_i + \beta_j + \varepsilon$ (section 2.2). F1 scales by size as $p = p_{\text{ref}} (s / s_{\text{ref}})^{k}$.

**Numbers.** Section 2.2.

**Naive alternatives.** Exact description matching finds nothing in cities. A family average mixes unlike items, and a median per family and band reports item mix as a quantity effect. Ignoring quantity lets a time trend absorb size drift (M8).

### M10. Paired bootstrap, pre-registration, the adoption rule

**Claim.** A change that "looks better" must be judged on paired differences, by a rule fixed beforehand. Otherwise the best of several useless arms looks like progress.

**Paired bootstrap** (section 4.7). $\operatorname{Var}(\bar A - \bar B) = (\sigma_A^2 + \sigma_B^2 - 2 \rho \sigma_A \sigma_B)/N$. A letting is hard or easy for both methods, so ρ is large and the paired difference has a much smaller variance (standard result). Lettings are resampled, not lines, because the lines of a letting share its level, centring and comparables. "Upper end below 0" is roughly a one-sided test at 5%.

**Multiple comparisons.** Suppose m arms are useless, each with an estimated gain $\hat d \sim N(0, \sigma_d^2)$.

- A better point estimate alone (condition (b)): each arm passes with probability ½, and at least one of 10 with probability $p = 1 - 0.5^{10}$, which is 99.9%.
- The interval's upper end below 0 (condition (d)): each passes with at most 5%, and at least one of 10 independent arms with $p = 1 - 0.95^{10}$, which is 40%.
- The winner's curse: the best arm's apparent gain has expectation $\sigma_d E[Z_{(m:m)}]$. That is 1.16 $\sigma_d$ for m = 5 and 1.54 $\sigma_d$ for m = 10 (model-computed). It is M6's order-statistic table with the sign flipped.

Hence pre-registration (section 4.6), and the locked set and the prospective checks as data the choice has not seen. The adoption rule itself is a lexicographic objective: the floor first, then accuracy inside it.

**Numbers: a post hoc arm, round 20 G.** The registered arms F and F+C failed. After the per-rule results had been seen, an addendum kept three parser family rules as arm G. On the common 234 municipal lettings:

- the under rate was unchanged in every year (8, 12 and 12 lettings);
- the median overshoot went from 45.25% to 44.56% (−0.68, interval −1.24 to +0.51), and the P50 error from 19.67% to 19.43%;
- the line error sum fell 0.25% (90% interval −$1.80M to −$0.19M).

G passes track U and fails track T at (d). Its make-up was chosen after seeing the data, so its result carries an upward selection bias. It was installed with a switch-off check on the first 40 new municipal lettings (section 4.9).

**Naive alternative.** Try many arms, keep the best, report its number (CORE, section 4.4).

### M11. The two-stage error decomposition

**Claim.** In logs the P50 error splits into "estimating the median bidder's total" and "stepping from the median to the low bid". Almost all of it is the first stage, and the stages are negatively correlated.

**Formula.** Let Med be the median bidder's total, $\rho = \text{low}/\text{Med}$, $\hat C$ the pre-discount estimate centred on Med, and $\hat f$ the discount used. Ignoring owner-set money, $P50 = \hat C \hat f$ and

$$\ln\frac{P50}{\text{low}} = e_1 + e_2, \qquad e_1 = \ln\frac{\hat C}{\text{Med}}, \qquad e_2 = \ln\frac{\hat f}{\rho}$$

**Variance.** $\operatorname{Var}(e) = \operatorname{Var}(e_1) + \operatorname{Var}(e_2) + 2 \operatorname{Cov}(e_1, e_2)$. On municipal 2024–26 (184 lettings, data of 2026-11-20) Var(e) is 0.231: stage one 102%, stage two 12%, twice the covariance −14%. Splitting the covariance evenly gives 95% against 5%.

**Shapley split of the mean abs error.** Let v(S) be the mean abs error when only the stages in S keep their error. Then

$$\varphi_1 = \tfrac12 \left[v(\lbrace 1 \rbrace) - v(\varnothing)\right] + \tfrac12 \left[v(\lbrace 1, 2 \rbrace) - v(\lbrace 2 \rbrace)\right] = \tfrac12 (0.308) + \tfrac12 (0.331 - 0.121) = 0.259$$

and $\varphi_2 = \tfrac12 (0.121) + \tfrac12 (0.331 - 0.308) = 0.072$. That is 78% against 22%. The shares add up to 0.331 whatever the order.

**Why negative.** Med is itself noisy. When it happens to be high, $e_1$ falls and $e_2$ rises: one noise with opposite signs. The correlation is −0.29 with 3 bidders or fewer and −0.41 with 7 or more.

**Numbers.**

- Stage one perfect (the median total known, the discount still f(n)): the P50 error falls from 22.9% to 8.4%, and the median overshoot is 15.0%.
- Stage two perfect (the discount equal to the letting's own low ÷ median): the P50 error is 21.5%, and the overshoot rises from 44.7% to 46.7%, because the part that cancelled stage one is gone.
- Stage two is hardly predictable. The mean of $\lvert \ln(\hat f/\rho) \rvert$ is 0.120 with the actual bidder count, 0.126 with a forecast and 0.127 with a constant. The standard deviation of ln ρ is 0.17, mostly single-letting noise. The best after-the-fact corrections move the overshoot by only −1.6 to +5.5 points.
- Stage one by size: the median ratio is 1.126 on jobs under 0.5 million dollars and 0.949 on jobs over 2 million. Stage-one error: TxDOT 14.5%, municipal 21.5%.

**Naive alternative.** Tuning the discount by size, work type or bidder count. There is no room, and fixing stage two alone raises the overshoot. The work is in stage one.

### M12. The noise floor of this design

**Claim.** This design has an error floor of about 8.5% on municipal work, even with perfect unit prices. "Overshoot at most 13% at 80% not under" is roughly that floor.

**What was computed** (rolling forward; 101 municipal lettings from 2020 Q1; data of 2026-11-19). Every line price was replaced by the letting's own median-bidder unit price. Everything else stayed: × f(n), with centring and multiplier from the past only.

| Case | P50 error | σ | P80 in band |
|---|---|---|---|
| Municipal, pre-bid bidder count | 8.5% | 0.122 | 37.6% |
| Municipal, actual bidder count | 8.4% | 0.125 | 31.7% |
| Municipal, the median bidder's total given directly | 9.1% | 0.136 | 40.6% |
| TxDOT | 6.1% | 0.102 | 49.6% |

σ is robust: $\sigma = \text{IQR}(\ln r)/1.349$. The overshoot with unit prices known was 13.3%.

**Derivation.** For a log-normal ratio with spread σ and the multiplier set at 80% not under,

$$\text{in band} = \Phi\left(\frac{\ln 1.13}{\sigma} + z_{0.2}\right) - 0.2$$

σ = 0.122 gives 36.4% (measured 37.6%). σ = 0.112 gives 40%. Half in band needs σ ≈ 0.089 (model-computed). An identity: if the median overshoot of the 80% that are not under is exactly 13%, half of them are in band, so the in-band share is 0.8 × 0.5 = 40%. So "overshoot at most 13% at 80% not under" and "40% in band" are one target, and "half in band" needs a σ below that of perfect unit prices.

**Naive readings.** One is "a better model can get arbitrarily close". The other is to call the floor a mathematical impossibility. It is the floor of this design, measured on 101 lettings. Another discount model, or pre-bid knowledge of who will bid, would move it. Nothing of that kind was built.

### M13. Errors that cancel: "fix one, expose another"

**Claim.** Line errors partly cancel inside a job. Fixing one line class can remove a cancellation and make totals worse. And the P80 margin is shared, so one group's new low tail widens everyone's multiplier.

**Derivation.** With dollar shares $w_i$, the job's relative error is $e_T \approx \sum_i w_i e_i$, and

$$\operatorname{Var}(e_T) = \sum_i w_i^2 \sigma_i^2 + \sum_{i \ne k} w_i w_k \operatorname{Cov}(e_i, e_k)$$

Independent line errors shrink in proportion to $n_{\text{eff}}^{-1/2}$, with $n_{\text{eff}} = 1/\sum_i w_i^2$. This is why half of the lines are off by 46% while totals are off by about 20%. A negative covariance makes the sum smaller than its parts. Remove one part and the cancellation goes.

Medians are not additive either. A catch-all cell is priced at $p_{\text{other}} = \operatorname{median}(S)$. Moving a class A out gives $\operatorname{median}(S \setminus A)$, which differs in general. Every remaining line changes price.

**Numbers.**

- Small jobs (43 lettings with our line total under $0.5M). Lump sums add about +28% to the total, and unit-priced lines run at 0.81× (about −12%). Fixing mobilization alone leaves the negative part: the small-job under rate goes from 9% to 17%. Because the divisor is a quantile over all lettings, the overall overshoot goes from 46.8% to 51.0%.
- Round 12, lump sums as a share of the job. The lump-sum lines' log error fell from 0.85 to 0.52. The total's mean abs log error stayed at 0.331, and the 2026 floor broke by one letting.
- Round 13, arm P_T. A 48-inch water line went from 3.70× to 0.77× its bids. Its letting's P80 ÷ low bid went from 1.52 to 0.95: now an under.
- Round 20, arm F. A pipe-bursting line went from $641K to $205K against a median bid of $199K. The letting's comparables price was low and had been propped up by that line. P80 ÷ low bid fell from 1.434 to 0.938.
- Round 20. When sound walls left the `other` cell for SF, an unrelated crack-repair line fell from $6.40/SF to $1.35/SF, and its letting became an under.

**Naive alternative.** Fix the worst lines one at a time and expect totals to follow. The lesson: fix in pairs (small-job lump sums together with the low unit-priced lines). Track U exists so that a line gain that leaves totals no worse can still be installed.

### M14. Unit conversion within one dimension

**Claim.** A unit may be converted only inside one physical dimension, and even an exact conversion is not harmless.

**Derivation.** For an exact factor κ, where one unit of $U_1$ equals κ units of $U_2$:

$$q_2 = \kappa q_1, \qquad p_2 = p_1 / \kappa, \qquad q_2 p_2 = q_1 p_1$$

The amount is invariant. Exact factors: 1 CY = 27 CF; 1 SY = 9 SF; 1 AC = 4,840 SY; 1 TON = 2,000 LB = 20 CWT; 1 MG (a thousand gallons) = 1,000 GAL.

Area and volume do not convert: $q_{\text{CY}} = q_{\text{SY}} \times t / 36$ needs a thickness t in inches, which the unit does not carry. Unit price is not linear in t (fixed costs per square), and sometimes the structure differs: a gabion mattress is bid by SY, a gabion wall by CY.

A threshold carries a unit. The quantity-band edges are numbers in the line's own unit, so converting thousand-gallons to gallons, or acres to square yards, moves quantities across bands. Of 297 TxDOT watering lines, 245 landed in the top band.

**Numbers.**

- With pooling switched on, every one of 188 municipal and 100 TxDOT schedules priced the same in either unit spelling (relative difference at most 2e-12).
- Without it, rewriting the units of the 188 municipal schedules moved P50 by more than 1% on 157 of them and left 624 lines unpriceable.
- Pooling still failed the line track in both markets. On TxDOT the line error sum rose by $67.8M. In 7 of 15 unit pairs the prices do not agree: TxDOT seeding by the acre is about 0.24× the SY price, and TxDOT sidewalk by SF is 1.5× the SY price.

**Naive alternatives.** Pool everything into one unit per dimension (UNIT_POOL); convert only when the line's own unit is thin (U3); leave thin rare-unit lines unpriced (U4). None passed. The conversion survives as a hint beside the line, and P50 and P80 never see it.

---

## 6. Current accuracy and limits

All figures are forward in time, on the 2026-11-21 build (README, "Where it stands"). Municipal figures use the covered scope. The discount uses each letting's actual number of bidders.

### 6.1 By market

| | Lettings | P50 median error | P80 under the low bid | Median overshoot | Overshoot at 70% / 75% / 80% not under |
|---|---|---|---|---|---|
| TxDOT (DFW counties), 2023–26 | 807 | 16% | 15% | 31% | 26% / 27% / 30% |
| TxDOT engineers' estimates (same 451 lettings, official totals) | 451 | 21% | 14% | — | 19% / 22% / 23% |
| Municipal (DFW), 2024–26 | 235 | 20% | 14% | 45% | 30% / 32% / 34% |
| Municipal engineers' estimates | 140 | about 15% | 33% | — | 20% / 21% / 25% |
| Locked public test set, municipal (last confirmation) | 56 | 14% | 20% | 31% | — |

Exact values: municipal 13.62% under, 44.80% overshoot, 19.52% P50 error; TxDOT 15.49%, 31.46%, 16.10%. For the engineer rows, "under" means their single published estimate was below the low bid. The locked-set row predates the latest data fixes and the scope change.

- **Small municipal jobs** (low bid under $0.5M, 74 lettings): P50 runs about 20% high; P50 error 24%.
- **Jobs over $2M:** P50 runs about 4% low, and P80 is under the low bid on 25% of them. This is where the unders concentrate.
- **Creek erosion and drainage** (42 lettings): P50 error 18%, P80 under 2%, median overshoot 41%. Units recovered by the parser brought in more recent creek and drainage prices and raised this group's estimates by about 4%. Part of the drop in unders (from 10%) is a relabelling, not better pricing.
- **2026:** P80 was under the low bid on 17% of municipal and 23% of TxDOT lettings. TxDOT is above the floor. The guard adapts, with a lag.

### 6.2 By item group

Line prices are compared with the median bidder's, on lines with at least 3 bidders, before the blend and the discount. Job totals are more accurate than single lines, because line errors partly cancel (M13).

| | Municipal 2024–26 | TxDOT 2023–26 |
|---|---|---|
| Lettings / lines | 235 / 10,328 | 807 / 41,159 |
| Half of lines within (dollar-weighted) | 46% (40%) | 30% (26%) |
| Lines within ±20% | 28% | 37% |
| Low bidder against median bidder, same line | 25% | 16% |
| Most accurate (half of lines within) | ADA curb ramps 16%, driveways 18%, valves and hydrants 22%, sidewalk 23%, inlets 23%, concrete pavement 25% (12.4% of dollars), box culverts 27%, storm pipe 28% | construction exits 13%, asphalt pavement 14%, bridge concrete 14%, sidewalk 15%, headwalls 17%, concrete riprap 17%, stone riprap 18%, box culverts 19%, storm pipe 19% |
| Least accurate | site preparation / clearing 168%, mobilization 142% (ours ÷ median bid 1.46), irrigation 136%, site preparation lump sum 128%, traffic control lump sum 119%, landscape planting 100%, unclassified lines 98%, SWPPP lump sum 98%, headwalls 83% | preparing right of way / clearing 160%, traffic control per month 65%, structure demolition 65%, cleaning / desilting 59%, mobilization 51% |

- City items with the most money still to gain: water pipe (8% of city dollars, half within 44%), mobilization (5%, runs 46% high), asphalt (4%, half within 36%), unclassified lines (4%, half within 98%). Fire hydrants run 31% low, pavement marking 20% low, miscellaneous concrete 13% high.
- Many items are 6–9 points more accurate on TxDOT than in cities. TxDOT's standard item codes keep wording uniform and history deep. The full tables are in the README.

### 6.3 What it is good and bad at

- **Good:** standard concrete, drainage and paving items, which are priced close to the bidders' own spread; TxDOT totals, which are near engineer level; showing the evidence behind every number.
- **Bad:** lump sums and general-condition items, which do not scale with the job; specialty equipment (marked not covered); city items with thin history; unusual schedules, where the comparables swing; weakly matched main items on large jobs.
- **Not fixable from bid history:** about a third of the worst municipal misses are ordinary market variation between bidders. Another sixth are one-off specialty items, now marked not covered.

### 6.4 What a preparer should check by hand

1. **Not-covered lines.** Get vendor quotes.
2. **Low-confidence lump sums.** Compare them with the historical share (mobilization is typically about 5% of the low bidder's total). Mobilization and temporary erosion control are the engineer's call.
3. **Heavy lines on thin evidence.** A line that carries 15% or more of P50 on fewer than 3 earlier lettings.
4. **Large jobs (over $2M).** Check each main item against its references.
5. **Small jobs (under $0.5M).** The total runs high, the lump sums most of all.
6. **Large abandoned pipes.** The size may not have constrained the match (section 2.2).
7. **Rare units.** A line in a rare unit may be another product. Read the converted reference price and decide.
8. **The similar projects.** They carry half of the weight of the bid part. Swapping one can move the result.
9. **The bidder count.** It is a forecast when quoting.
10. **The recent price level.** In 2026 low bids ran higher than history suggests, and the guard lags.

---

## 7. Glossary

**General terms**

| Term | Meaning |
|---|---|
| OPCC | Opinion of probable construction cost: the engineer's estimate of what a job will cost. |
| Bid tabulation (bid tab) | The table published after a bid opening, with every bidder's unit price on every line. |
| Letting | One bid opening. |
| Low bid; median bidder | The total of the lowest bidder (not always the award); the bidder whose total sits in the middle. |
| P50; P80 | The median estimate of the low bid; the not-under figure, P50 ÷ divisor. |
| Lump sum (LS) | A line priced as one amount, such as mobilization. |
| Mobilization | The one-time cost of moving onto the site. Low bidders put about 5% of their total in it. |
| Owner-set money (allowance) | An amount the owner prints on the bid form and every bidder copies. Taken at face value. |
| Contingency | Money reserved for the unforeseen. As a line it is owner-set money; in an OPCC it is added below the subtotal (10% by default). |
| TxDOT; DFW; HCI | Texas Department of Transportation; the Dallas–Fort Worth area; TxDOT's official Highway Cost Index. |
| SWPPP | Storm water pollution prevention plan: temporary erosion control. |

**Evaluation terms**

| Term | Meaning |
|---|---|
| Forward in time | Each letting is priced and calibrated from lettings dated before it only. |
| Under rate | Share of lettings whose P80 is below the low bid. The floor is 20% (`NOT_UNDER_FLOOR`). |
| Median overshoot | Median of P80 ÷ low bid − 1 among lettings where P80 ≥ low bid. |
| In band | P80 ÷ low bid within 1.00–1.13. |
| Matched not-under rate | Comparing two methods after giving each the multiplier that makes the same share of its lettings not under. |
| Mean abs log error; P50 median error | Mean of the absolute log of P50 ÷ low bid; median of the absolute value of P50 ÷ low bid − 1. |
| Line error sum; line log error | Sum over lines of the absolute dollar gap to the median bidder; the absolute log ratio per line. |
| Pre-registration; registration | Writing down arms and the pass/fail rule before scoring; an entry in `REGISTRATIONS.md` for a decision confirmed on the locked set or by a prospective check. |
| Paired bootstrap | Resampling lettings to get a 90% interval for the difference between two methods on the same lettings. |
| B; arm | The baseline, which is the production rule of the time; a variant under test. |
| own; B80 | An arm relearns its own P80 margin; an arm (and production, switch `P80_FROM_B`) uses production B's divisor and guard level. |
| Tracks T, U, U+ | The three ways to pass adoption rule v2: better totals; better unit prices with nothing worse; a large unit-price gain for a small loss on totals. |
| Locked public test set | 59 groups of public lettings (`source_set == "public_test"`) that never train and are opened only to confirm a registered decision. |
| Decoy | Public lettings drawn like the locked set and held out in process, for development. |
| Prospective check | A registered test on lettings dated after a decision, which can switch a change on or off. |
| `source_set` | Row label: `external` (public, may train), `internal` (private company test files), `public_test` (locked set). |
| Guards | `train_test_guard`, `provenance_check`, `company_twin_scan`: the leak checks of section 4.3. A *twin* is a public copy of a private test project. |
| Snapshot; parser version | A frozen copy of the data used by an experiment; the parser's rule version (.18 to .34b). |

**Model terms**

| Term | Meaning |
|---|---|
| Item family | The class of a pay item (`storm_pipe`, `sidewalk`, …). The web app says "item type". `other` means not classified. |
| Family key | The family, plus `#rm` for a removal-only description. |
| Token; Jaccard | A content word or number of a description; the overlap of two token sets, intersection ÷ union. |
| Token cell; band cell; family cell | Reference cells keyed by (token set, unit, quantity band); (family key, unit, size bucket, quantity band); (family key, unit, size bucket). |
| Band; size bucket | The quantity band (0–10 … 25,000+); the size bucket in inches (0–6 … 37+). |
| `usable`; thin cell | A cell's evidence flag (`yes`, `thin`, `no`, `unclassified`); a cell with few lettings behind it. |
| Two-stage median | Median within a letting, then across lettings. |
| Vintage year | The dollar-weighted year a reference price is "from". |
| Price index; escalation | The matched, chained index by market; the factor index(bid year) ÷ index(vintage). |
| Narrow carry | Carrying an index arm's last level flat to the bid year, only when no arm has that year. |
| Competition discount f(N) | Low bid ÷ median bidder total, by number of bidders. The municipal table is the pooled table × one factor. |
| C (clamp) | A bidder count above the table's top bin uses the top bin (`CLAMP_ABOVE_TOP_BIN`). |
| Market total; bid part | The sum of the line prices, an estimate of the median bidder's total; the discounted, blended part of P50 before centring. |
| CMP; V2 | The price from the comparables method; the blend sqrt(CMP × line-priced bid part). |
| F1; F1P | The size fallback; its pipe-only form (off). |
| F2; AB; AB2 | The removal identity; the abandonment flag in the token tier; the same as a family key `#ab` (off). |
| Typical line amount | What a removal-only line with no removal cell gets: the schedule's median line amount. |
| Quantity shape | A per-family multiplier by quantity band, used in the family tier. |
| T1; C4; D1 | TxDOT only. C4: exact token cell first, and mobilization as a share of the rest. D1: discount by bidder class × model size class. |
| Centring; lc | The factor that puts the median of P50 ÷ low bid at 1 over earlier lettings (`level_correction`). |
| Divisor; multiplier | P80 = P50 ÷ divisor (`p_not_under`); the multiplier is 1 ÷ divisor. |
| Guard; `GUARD_TARGET`; `GUARD_GAMMA` | The adaptive rule that sets the divisor's quantile level; its aim (0.16); its step (0.02 municipal, 0.004 TxDOT). |
| `FORWARD_MIN_PRIOR` | 40: earlier forward-scored lettings needed before constants exist. |
| `COVERAGE_SCOPE`; not covered | The switch and the status of specialty lines that are not priced and need a vendor quote. |
| Low confidence | Mobilization and four lump-sum classes shown apart with a historical share (`LOWCONF`). |
| N1 | The web app's forecast of the bidder count from similar earlier lettings. |
| `MARKET_ACCURACY`; `UNDER_CURVE` | The web app's copy of the headline accuracy; its curve of how often m × P50 came in under. |

**Arms that are off or were rejected**

| Name | Meaning |
|---|---|
| MC; E; T2; D | Round 10 switches: municipal-only cells; removal flag in the municipal comparables; family cells need 2 lettings; three identical bids on a lump sum count as owner-set money. |
| CORE; `round11_core_not2` | MC + E + F1P + T2 + D; the same without T2 (off, waiting for its check). |
| P_L | Round 13: municipal price level for cells dominated by TxDOT lettings (`round13_l`; off, waiting for its check). |
| P_T | Round 13: shrinking a price backed by a single letting toward its parent cell. |
| P_LS; P_SC | Round 12: lump sums as a share of the job; size-classed centring. |
| P_C; P_W; P_CW; P_CWT | Round 14: a mobilization line priced at its printed cap; weak token matches blended with the band price; both; both plus P_T. |
| X1; X2; X3 | Round 18: clamp + abandonment flag (installed, with B80); X1 + P_CWT; X1 + P_CW. |
| F; G; TF | Round 20: five parser rules plus token cells keyed by family (TF); the three family rules that were installed as parser .34b. |
| O1; S1 | `other` lines priced from the most similar descriptions; borrowed statewide erosion/drainage prices. |
| W36; CW12, CW24; R1, R2; FC; OW1, OW2 | Level corrections: a 36-month window; recency-weighted centring; ridge centring by size; a facility-job correction; owner effects. |
| M_T; M_Z | Round 19: per-job P80 margins by thin-evidence share; by size. |
| UNIT_POOL; U3; U4 | Unit conversion: pool all units of a dimension; convert only thin units; leave thin rare-unit lines unpriced. |
