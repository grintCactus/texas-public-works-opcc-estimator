# Texas Public Works OPCC Estimator

Estimates what a Texas public-works construction project will cost at bid time. For any bid schedule (a list of items with quantities) it predicts:

- **P50**, the expected low bid;
- **P80**, a "not-under" opinion of probable construction cost (OPCC), set so that the actual low bid comes in below it on at least 80% of lettings.

The focus is municipal work in the Dallas–Fort Worth area, especially creek erosion repair, drainage and streambank stabilization. Texas Department of Transportation (TxDOT) lettings are covered as a second market.

The model is trained **only on public bid tabulations**. Every number it reports was measured **forward in time**: each letting is priced using only lettings dated before it, the way the tool would actually be used.

> **Status: work in progress.** The data pipeline, the evaluation framework and two rounds of confirmed pricing changes are in place. TxDOT accuracy is near engineer level; municipal accuracy is still short of the goal (see below). This repository is being published step by step.

---

## The goal

The estimate is meant to be used as an OPCC, where underestimating is the costly mistake. The target is:

1. **Floor:** the P80 estimate is below the actual low bid on **at most 20%** of lettings.
2. **Score:** within that floor, P80 should overshoot the low bid **as little as possible** (13% is the aspiration).
3. **Yardstick:** at the same not-under rate, the overshoot is compared with engineers' own estimates. Engineer estimates are never an input.

## Where it stands (2026-11-20)

All figures are forward in time: each letting is priced from lettings dated before it, and the model had not seen the letting.

| | Lettings | P50 median error | P80 under the low bid | Median overshoot (P80 ≥ low) | Overshoot at 70% / 75% / 80% not-under |
|---|---|---|---|---|---|
| **TxDOT** (DFW counties), 2023–26 | 805 | **16%** | 16% | 32% | **26% / 27% / 30%** |
| TxDOT, bidder count predicted (as when quoting) | 806 | 17% | 15% | 34% | 28% / 29% / 30% |
| TxDOT engineers' own estimates (same 451 lettings, official totals) | 451 | 21% | 14% | — | 19% / 22% / 23% |
| **City / municipal** (DFW), 2024–26 | 184 | **23%** | 15% | 45% | **35% / 34% / 40%** |
| City / municipal, bidder count predicted (as when quoting) | 195 | 23% | 14% | 45% | 35% / 35% / 38% |
| Municipal engineers' own estimates | 140 | ~15% | 33% | — | 20% / 21% / 25% |
| Locked public test set, municipal (last confirmation) | 56 | 14% | 20% | 31% | — |

Municipal figures use the "covered" scope (see below). The main rows give the competition discount each letting's actual number of bidders, which is not known when quoting; the "bidder count predicted" rows use a forecast from earlier lettings of the same market, work type and size instead (a rule written before it was scored). That costs TxDOT about 2 points of overshoot and leaves municipal unchanged. With a predicted count, 11 more municipal lettings with 15–21 bidders can be scored, so that row has 195 lettings. The locked-set row is from the last confirmation, before the latest data fixes and the scope change. For the engineer rows, "P80 under the low bid" means their single published estimate came in under the low bid.

## Strengths and limitations

**Strengths**

- **TxDOT is close to engineer level.**
  - On 805 DFW TxDOT lettings, the median P50 error is 16%.
  - On the 451 of them with an official engineer's estimate, our median error (17%) is lower than the engineers' (21%), whose estimates run about 20% high.
  - At an 80% not-under rate, the engineers still overshoot less: 23% vs our 31%.
- **Standard items are priced close to the bidders' own spread.** On TxDOT, half of the concrete pavement, riprap, headwall, box culvert and storm pipe lines are within 17–20% of the median bid. In cities, the same holds for curb ramps, driveways and inlets (17–22%). See [Item-level accuracy](#item-level-accuracy).
- **The evaluation is strict.**
  - Every figure is forward in time.
  - Decisions are registered before they are scored.
  - A locked public test set is opened only to confirm a registered decision, and each opening is logged.
  - Leak guards stop any build in which test data reaches a fitted number.
- **Public data only.** Engineer estimates are a yardstick, never an input.
- **Every number can be traced.** The local web UI (`ui/`) prices a schedule with the same estimator. It shows the reference projects behind each line price, the four most similar past projects, and which lines are not covered or low confidence.
- **It says what it does not know.** Specialty items with almost no bid history are marked "not covered" and listed for separate (vendor) pricing instead of being guessed. Examples: pumps and pump/lift-station equipment, generators and electrical gear, prefabricated or pedestrian bridges, tanks and process equipment.

**Limitations**

- **City (municipal) estimates are less accurate.**
  - The median P50 error is 23%, against about 15% for the engineers.
  - At an 80% not-under rate the overshoot is about 40%, against the engineers' 25%.
  - Almost all of this error comes from estimating the bidders' typical (median) price for the schedule. Very little comes from the step from the median bid to the low bid.
- **Lump-sum items do not yet scale with job size.**
  - Mobilization, traffic control, site preparation and SWPPP / erosion-control lump sums are priced at about the same dollar amount on every job. Bidders price them as a share of the job (mobilization is typically about 5%).
  - Small city jobs (low bid under $0.5M, 54 lettings) come out about 26% high (P50 error 32%).
  - Jobs over $2M come out about 7% low, and P80 is under the low bid on 24% of them.
  - Pricing them as a share of the job fixes the lump-sum lines themselves, but it did not make job totals more accurate (round 12, under "Tried and not adopted").
- **Creek erosion and drainage** (39 lettings) is now close to the city average: P50 error 20%, P80 under the low bid 10% of the time, median overshoot 41%.
- **Recent lettings run higher than history.** In 2026, P80 was under the low bid on 19% of municipal and 21% of TxDOT lettings; TxDOT is above the 20% floor. The guard adapts, but with a lag.
- **About a third of the worst municipal misses cannot be fixed this way.** They are ordinary market variation between bidders that bid history alone cannot predict. Another sixth are one-off specialty items, which are now marked not covered.
- **City data is thin.** Many city items have little bid history. Some cities publish bid tabs only behind vendor portals, which are not collected. Scanned tabs lose lines.
- **Not runnable end to end from this repository.** A few pipeline modules still carry client-specific configuration and are not published. The bid-tab corpus (several GB) is not distributed.

**Tried and not adopted (yet).** Pricing municipal lines from municipal-only price data, instead of data dominated by TxDOT price levels, together with four smaller pricing fixes.
- **Forward tests:** passed the registered test. Municipal overshoot fell 49% → 42% and P50 error 24% → 20%.
- **Locked test set:** failed. Its tighter P80 margin let P80 fall under the low bid on 29% of lettings, against 19% for the current rule.
- **Status:** kept in the code, switched off. It will be re-checked on new lettings.

**Lump sums as a share of the job (round 12, registered before scoring).** Mobilization, traffic control, site preparation, SWPPP and bonds were priced as a share of the rest of the job. The shares were learned from earlier bids, by job size. A size-dependent centring of P50 was tested alone and together with it.
- **The lump-sum lines themselves became much more accurate.**
  - Against the median bidder, the typical miss fell from about 2.3× to 1.7×.
  - On small jobs our lump sums went from 2.8× the bidders' amounts to 0.9×; on large jobs from 0.56× to 0.97×.
- **Size-dependent centring removed the size bias of P50.** The median P50 ÷ low bid on small / medium / large jobs went from 1.26 / 1.07 / 0.94 to 1.10 / 1.05 / 1.00.
- **The spread of job-total errors did not shrink** (mean |ln| 0.331 either way). So the P80 margin needed to stay at or above the low bid 80% of the time stayed as wide.
  - No variant lowered the median overshoot by the registered margin.
  - The lump-sum variants pushed the 2026 under-rate just over its limit, by one letting.
  - On small jobs, lowering the lump sums exposed unit-priced lines that run low. On large jobs, most of the misses were too large for a few percent to rescue.
- **Status:** not adopted; production is unchanged. The code is kept in a development copy, switched off.

## Item-level accuracy

How close the price of each kind of line item is, before the lines are added up. Job totals are more accurate than single lines, because line errors partly cancel.

**How it is measured**
- Every line of every scored letting is priced forward in time: from earlier lettings only.
- The comparison uses the line price itself, before the comparables blend and the competition discount.
- It is compared with the **median bidder's** amount on the same line, on lines with at least 3 bidders. Owner-set and not-covered lines are left out.

**Columns**
- "Half of lines within": half of the lines are closer to the median bid than this.
- "Ours ÷ median bid": the typical ratio. Above 1 means we run high.
- "Low bidder vs median bidder": how far the low bidder usually is from the median bidder on the same line. That is the spread between bidders themselves, which no estimate can remove.

**What it shows**
- **Standard concrete, drainage and paving items are already close to the bidders' own spread.** Examples: curb ramps, driveways, inlets, sidewalk, concrete pavement, storm pipe and box culverts in cities; riprap, headwalls, box culverts, storm pipe and concrete pavement on TxDOT.
- **Lump sums and other general-condition items are the weakest in both markets.**
  - In cities, mobilization, traffic control, site preparation and SWPPP lump sums are priced at about the same dollar amount on every job, while bidders price them as a share of the job. A share-of-job rule makes these lines much closer to the bids (round 12) but did not improve job totals, so it is not in use yet.
  - On TxDOT, mobilization is already priced as a share of the job.
- **Many items are 7–11 points more accurate on TxDOT than in cities:** sidewalk, concrete pavement, storm pipe and box culverts.
  - TxDOT's standard item codes keep descriptions uniform and history deep. City wording varies, and city data is thinner.
  - Curb ramps, driveways and inlets are as accurate in cities as on TxDOT.
- **The city items with the most money still to gain:**
  - water pipe (8% of city dollars, half within 44%);
  - miscellaneous concrete (6%, runs 17% high);
  - unclassified lines (6%);
  - mobilization (5%);
  - asphalt (4%, half within 43%).
  - Valves and hydrants run 18% low, and pavement marking 24% low.

#### City / municipal (DFW), 2024–26: 184 lettings, 8,676 lines

Across all lines, half are within **48%** of the median bid (**43%** weighted by dollars); 27% are within ±20%. The low bidder is typically 24% from the median bidder on the same line.

| Item | Lines | Lettings | Share of dollars | Half of lines within | Dollar-weighted | Ours ÷ median bid | Lines within ±20% | Low bidder vs median bidder |
|---|---|---|---|---|---|---|---|---|
| ADA curb ramps | 182 | 84 | 0.9% | 17% | 15% | 0.95 | 54% | 21% |
| Driveways | 124 | 75 | 1.7% | 20% | 18% | 0.98 | 51% | 22% |
| Project signs | 59 | 58 | 0.0% | 21% | 30% | 1.03 | 46% | 33% |
| Inlets | 234 | 59 | 1.6% | 22% | 24% | 0.97 | 48% | 16% |
| Sidewalk | 210 | 111 | 4.0% | 26% | 24% | 1.04 | 42% | 21% |
| Signs | 72 | 44 | 0.1% | 27% | 13% | 1.03 | 42% | 18% |
| Concrete pavement | 176 | 87 | 11.2% | 27% | 20% | 0.99 | 40% | 13% |
| Water services | 141 | 61 | 2.3% | 28% | 16% | 0.90 | 42% | 23% |
| Storm pipe | 271 | 65 | 2.9% | 29% | 24% | 0.88 | 36% | 19% |
| Box culverts | 59 | 28 | 1.3% | 30% | 28% | 0.91 | 36% | 13% |
| Flexible base | 86 | 70 | 2.6% | 32% | 48% | 0.89 | 40% | 23% |
| Embankment / fill | 49 | 43 | 0.5% | 34% | 123% | 1.15 | 35% | 50% |
| Pavement removal | 128 | 79 | 1.6% | 34% | 30% | 0.94 | 29% | 32% |
| Sidewalk removal | 92 | 57 | 0.3% | 35% | 26% | 1.14 | 34% | 37% |
| Drilled shafts | 41 | 19 | 1.4% | 37% | 49% | 0.78 | 20% | 16% |
| Stone riprap | 46 | 36 | 0.7% | 37% | 42% | 0.87 | 33% | 24% |
| Valves and hydrants | 285 | 95 | 2.4% | 37% | 47% | 0.82 | 32% | 17% |
| Concrete beams | 43 | 29 | 0.6% | 39% | 52% | 0.96 | 33% | 38% |
| Excavation | 124 | 88 | 2.6% | 39% | 38% | 0.94 | 27% | 35% |
| Curb and gutter | 132 | 79 | 0.7% | 40% | 31% | 1.10 | 30% | 33% |
| Subgrade stabilization | 108 | 52 | 2.5% | 40% | 40% | 0.81 | 24% | 24% |
| Tree removal | 65 | 42 | 0.1% | 41% | 55% | 0.90 | 25% | 33% |
| Erosion control (unit-priced) | 149 | 65 | 0.4% | 42% | 98% | 0.80 | 32% | 30% |
| Electrical and lighting | 374 | 40 | 1.9% | 42% | 64% | 0.85 | 26% | 13% |
| Traffic signals | 110 | 16 | 0.3% | 42% | 122% | 0.97 | 44% | 11% |
| Testing and inspection | 63 | 35 | 0.4% | 42% | >1000% | 1.01 | 32% | 30% |
| Asphalt pavement | 165 | 78 | 4.3% | 43% | 59% | 1.02 | 29% | 20% |
| Water pipe | 250 | 62 | 8.2% | 44% | 42% | 0.93 | 30% | 28% |
| Seeding and sod | 283 | 144 | 1.6% | 45% | 57% | 0.88 | 32% | 21% |
| Other removals | 149 | 73 | 0.5% | 47% | 65% | 0.98 | 28% | 40% |
| Miscellaneous concrete | 190 | 79 | 6.3% | 47% | 23% | 1.17 | 18% | 25% |
| Casing / encasement | 138 | 63 | 2.5% | 49% | 101% | 0.89 | 26% | 25% |
| Sanitary sewer pipe | 108 | 41 | 2.7% | 49% | 50% | 0.95 | 21% | 39% |
| Guardrail | 58 | 24 | 0.1% | 50% | 65% | 0.75 | 31% | 22% |
| Manholes | 197 | 79 | 1.9% | 53% | 107% | 0.93 | 27% | 21% |
| Utility removal / abandonment | 270 | 86 | 0.6% | 55% | 44% | 0.96 | 20% | 44% |
| Traffic control (unit-priced) | 70 | 50 | 0.6% | 55% | 58% | 1.25 | 19% | 29% |
| Tree planting | 40 | 32 | 0.1% | 61% | 265% | 1.01 | 30% | 17% |
| Fence | 75 | 50 | 0.2% | 62% | 75% | 0.82 | 17% | 23% |
| Pipe fittings | 45 | 32 | 0.3% | 64% | 38% | 1.12 | 29% | 21% |
| Sewer services | 53 | 34 | 0.4% | 65% | 69% | 1.23 | 11% | 47% |
| Headwalls | 49 | 35 | 0.2% | 66% | 103% | 0.76 | 10% | 43% |
| Relocate / reset | 213 | 79 | 0.3% | 66% | 104% | 0.90 | 23% | 39% |
| Trench safety | 108 | 71 | 0.1% | 67% | 80% | 0.90 | 19% | 60% |
| Contingency / allowance lines | 76 | 22 | 0.6% | 68% | 135% | 0.90 | 24% | 21% |
| Pavement marking | 527 | 71 | 0.4% | 69% | 91% | 0.76 | 19% | 21% |
| Retaining walls | 48 | 25 | 1.6% | 70% | 90% | 0.98 | 17% | 28% |
| Structure demolition | 78 | 46 | 0.1% | 75% | 211% | 0.76 | 15% | 26% |
| Unclassified lines | 950 | 140 | 5.6% | 102% | 123% | 1.10 | 18% | 21% |
| SWPPP / temporary erosion control (lump sum) | 133 | 124 | 0.3% | 115% | 170% | 1.09 | 9% | 43% |
| Site preparation (lump sum) | 66 | 63 | 1.4% | 117% | 546% | 0.97 | 17% | 90% |
| Traffic control (lump sum) | 124 | 115 | 0.9% | 121% | 202% | 1.33 | 10% | 60% |
| Irrigation | 58 | 33 | 0.6% | 143% | 152% | 0.88 | 7% | 15% |
| Mobilization | 160 | 158 | 4.7% | 150% | 311% | 1.39 | 12% | 35% |

#### TxDOT (DFW counties), 2023–26: 805 lettings, 41,126 lines

Across all lines, half are within **31%** of the median bid (**25%** weighted by dollars); 36% are within ±20%. The low bidder is typically 16% from the median bidder on the same line.

| Item | Lines | Lettings | Share of dollars | Half of lines within | Dollar-weighted | Ours ÷ median bid | Lines within ±20% | Low bidder vs median bidder |
|---|---|---|---|---|---|---|---|---|
| Construction exits | 172 | 171 | 0.1% | 13% | 18% | 0.99 | 65% | 17% |
| Concrete mow strip | 189 | 183 | 0.5% | 15% | 16% | 1.06 | 64% | 11% |
| Construction exit removal | 169 | 169 | 0.0% | 16% | 15% | 1.06 | 58% | 17% |
| Sidewalk | 161 | 114 | 0.6% | 17% | 9% | 0.97 | 58% | 12% |
| Headwalls | 218 | 78 | 0.1% | 17% | 15% | 0.99 | 57% | 9% |
| Traffic signals | 802 | 99 | 0.2% | 17% | 33% | 1.00 | 55% | 11% |
| Concrete riprap | 252 | 192 | 0.5% | 17% | 10% | 1.02 | 56% | 15% |
| Stone riprap | 276 | 196 | 0.8% | 18% | 19% | 0.94 | 54% | 14% |
| Box culverts | 367 | 99 | 2.2% | 19% | 24% | 0.94 | 51% | 16% |
| Flowable backfill | 190 | 160 | 0.3% | 19% | 20% | 0.92 | 53% | 17% |
| Casing / encasement | 312 | 127 | 0.5% | 19% | 34% | 0.92 | 53% | 14% |
| Concrete beams | 64 | 55 | 0.1% | 19% | 19% | 0.95 | 53% | 25% |
| Bridge railing | 267 | 129 | 1.1% | 20% | 25% | 0.95 | 51% | 15% |
| Storm pipe | 600 | 148 | 1.9% | 20% | 16% | 1.01 | 51% | 13% |
| Lighting assemblies | 160 | 56 | 0.3% | 20% | 40% | 0.86 | 49% | 6% |
| Concrete pavement | 141 | 90 | 8.1% | 20% | 8% | 0.93 | 49% | 12% |
| Inlets | 501 | 80 | 1.0% | 21% | 22% | 0.86 | 48% | 16% |
| Signs | 1392 | 262 | 0.7% | 21% | 27% | 1.04 | 48% | 9% |
| Flexible base | 126 | 109 | 1.4% | 22% | 12% | 0.91 | 46% | 12% |
| Drilled shafts | 574 | 166 | 3.6% | 22% | 32% | 0.86 | 45% | 13% |
| ADA curb ramps | 396 | 101 | 0.2% | 22% | 22% | 0.91 | 47% | 13% |
| Driveways | 224 | 137 | 0.7% | 22% | 21% | 0.95 | 44% | 13% |
| Miscellaneous concrete | 829 | 251 | 6.0% | 24% | 13% | 0.99 | 45% | 19% |
| Erosion control (unit-priced) | 1701 | 411 | 0.3% | 24% | 20% | 0.97 | 45% | 20% |
| Wingwalls | 293 | 94 | 0.3% | 24% | 23% | 0.96 | 42% | 14% |
| Pavement milling | 226 | 192 | 0.9% | 24% | 20% | 0.97 | 44% | 16% |
| Borrow / backfill | 141 | 136 | 0.1% | 25% | 22% | 1.05 | 45% | 25% |
| Safety end treatments | 516 | 125 | 0.2% | 25% | 23% | 0.92 | 42% | 17% |
| Electrical and lighting | 2111 | 142 | 1.1% | 25% | 43% | 0.85 | 41% | 12% |
| Retaining walls | 65 | 51 | 2.6% | 26% | 23% | 0.88 | 34% | 23% |
| Subgrade stabilization | 221 | 99 | 1.5% | 29% | 27% | 0.83 | 33% | 12% |
| Concrete joints | 266 | 150 | 0.4% | 29% | 40% | 0.88 | 40% | 21% |
| Concrete barrier | 855 | 181 | 2.0% | 30% | 24% | 1.01 | 36% | 25% |
| Guardrail | 1690 | 289 | 0.8% | 31% | 24% | 1.22 | 33% | 11% |
| Asphalt pavement | 292 | 174 | 3.1% | 31% | 14% | 0.97 | 36% | 16% |
| Unclassified lines | 6245 | 615 | 23.2% | 32% | 19% | 0.93 | 36% | 14% |
| Curb and gutter | 288 | 160 | 0.3% | 33% | 30% | 0.91 | 36% | 22% |
| Shoring | 112 | 108 | 1.3% | 34% | 48% | 0.94 | 31% | 27% |
| Surface treatment | 360 | 261 | 0.2% | 34% | 33% | 1.08 | 33% | 29% |
| Pavement marking | 8366 | 425 | 1.7% | 36% | 25% | 1.13 | 32% | 12% |
| Pavement removal | 259 | 162 | 0.4% | 37% | 38% | 0.90 | 28% | 18% |
| Testing and inspection | 98 | 44 | 0.0% | 38% | 17% | 1.12 | 36% | 19% |
| Seeding and sod | 1050 | 299 | 0.8% | 39% | 23% | 1.02 | 30% | 32% |
| Other removals | 665 | 275 | 0.7% | 39% | 27% | 0.98 | 30% | 25% |
| Embankment / fill | 113 | 86 | 1.5% | 40% | 70% | 0.85 | 28% | 29% |
| Mowing | 450 | 192 | 0.8% | 40% | 21% | 1.22 | 29% | 23% |
| Excavation | 348 | 223 | 1.8% | 42% | 66% | 0.97 | 28% | 32% |
| Fence | 277 | 122 | 0.3% | 42% | 49% | 1.04 | 32% | 25% |
| Utility removal / abandonment | 614 | 188 | 0.9% | 44% | 271% | 0.91 | 29% | 42% |
| Sidewalk removal | 252 | 151 | 0.1% | 47% | 57% | 0.96 | 24% | 22% |
| Mobilization | 910 | 804 | 9.8% | 50% | 39% | 1.08 | 28% | 25% |
| Manholes | 122 | 56 | 0.1% | 51% | 179% | 0.74 | 27% | 31% |
| Relocate / reset | 114 | 68 | 0.0% | 53% | 489% | 0.91 | 31% | 28% |
| Crash cushions | 500 | 124 | 0.5% | 55% | 58% | 1.32 | 19% | 27% |
| Tree removal | 253 | 59 | 0.1% | 56% | 48% | 1.33 | 22% | 60% |
| Cleaning / desilting | 150 | 104 | 0.2% | 61% | 60% | 0.81 | 22% | 28% |
| Traffic control (per month) | 2205 | 782 | 3.9% | 64% | 238% | 1.24 | 23% | 63% |
| Structure demolition | 119 | 73 | 0.1% | 65% | 531% | 0.82 | 24% | 38% |
| Preparing right of way / clearing | 242 | 228 | 5.8% | 157% | >1000% | 1.24 | 10% | 71% |

## How it works

```
public bid tabs (PDF / XLSX / scans)
      │  harvest: city and county bid archives, council agenda packets, TxDOT open data
      ▼
parse  ── text layer, coordinate reader, OCR (best of 4 engines), arithmetic checks
      ▼
panel  ── one row per bidder × line item; item family, unit, size, quantity band
      ▼
reference prices ── two-stage medians (within letting, then across lettings),
                    price index by market, competition discount by bidder count
      ▼
estimate(schedule) ── price each line, sum; blend with the five most similar
                       earlier jobs (comparables); competition discount  →  P50
      ▼
forward calibration ── centring and the P80 divisor from earlier lettings only,
                       plus an adaptive guard aimed at a 16% under-share  →  P80
```

## Rules the project holds itself to

- **Public data only.** No client or company files are in this repository, and none are ever used to train or tune the model.
- **Engineer estimates are a yardstick, never an input.** They are not used as a feature or a target.
- **Forward in time.** Every reported accuracy figure prices each letting using only earlier lettings.
- **Locked public test set.** 71 public files (22% of each year × work-type stratum) never train anything. The set is opened only to confirm a decision registered in advance, and every opening is logged in `data/test_holdout/`.
- **Guards.** `src/provenance_check.py` and `src/train_test_guard.py` recompute fitted tables with test rows switched on and off. A build fails if any test data reaches a fitted number.
- **Pre-registration.** Pricing changes are written down with their pass/fail rule before they are scored.

## Repository layout

| Path | What it is |
|---|---|
| `src/` | Pipeline: harvesting, parsing, OCR, panel build, reference tables, estimator, evaluation and guards |
| `ui/` | A small local web app for pricing a schedule |
| `data/test_holdout/` | Registry and access log of the locked public test set (file list only) |

Not included:

- **The data corpus:** several GB of downloaded bid tabs and derived tables.
- **Experiment reports and setup notes:** kept in the working copy; the results that matter are summarized in this README.
- **Local tools:** poppler, Tesseract and the Python environment.
- **Some pipeline modules:** they still carry client-specific configuration, which is being moved out into a local file. They will be added in a following commit.

## Running it

Requires Python 3.12 on macOS or Linux.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Text extraction and OCR use poppler (`pdftotext`) and Tesseract, installed from conda-forge into `tools/` (see `tools/README.txt` in a working copy). A full rebuild (`bash src/rebuild_all.sh`) needs the bid-tab corpus, which is not distributed here.

The web UI: `.venv/bin/python ui/run.py` opens http://127.0.0.1:8017/. It needs the built tables in `data/clean`; see `ui/README.md`.

## License

No license has been chosen yet; all rights reserved by the author.
