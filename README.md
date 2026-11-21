# Texas Public Works OPCC Estimator

A working aid for estimators preparing an opinion of probable construction cost (OPCC) for Texas public-works projects. For any bid schedule (a list of items with quantities) it does two things:

- **It puts the evidence in front of the estimator.** For every line it shows the earlier public bids behind the price: project, date, quantity, the low and median bidders' unit prices. Each project links to its full bid tabulation and to the public page it came from. It also shows the most similar past projects, the lines it cannot price (specialty items that need a vendor quote), and the lines whose prices are uncertain.
- **It estimates the bid where the history supports it:**
  - **P50**, the expected low bid;
  - **P80**, a "not-under" OPCC, set so that the actual low bid comes in below it on at least 80% of lettings.

The estimator stays in charge. The model's number is one input among the references, not a replacement for judgment. Where history is thin, the tool says so and leans on the references instead.

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
| **TxDOT** (DFW counties), 2023–26 | 806 | **16%** | 16% | 32% | **26% / 26% / 29%** |
| TxDOT engineers' own estimates (same 451 lettings, official totals) | 451 | 21% | 14% | — | 19% / 22% / 23% |
| **City / municipal** (DFW), 2024–26 | 190 | **20%** | 15% | 43% | **31% / 30% / 32%** |
| Municipal engineers' own estimates | 140 | ~15% | 33% | — | 20% / 21% / 25% |
| Locked public test set, municipal (last confirmation) | 56 | 14% | 20% | 31% | — |

Municipal figures use the "covered" scope (see below). The discount uses each letting's actual number of bidders, which is not known when quoting. On the previous data build, a forecast bidder count from earlier lettings of the same market, work type and size cost TxDOT about 2 points of overshoot and left municipal unchanged. Figures are from the current data build (parser .29, plus 23 city bid tabs added on 2026-11-20). The locked-set row is from the last confirmation, before the latest data fixes and the scope change. For the engineer rows, "P80 under the low bid" means their single published estimate came in under the low bid.

## Strengths and limitations

**Strengths**

- **TxDOT is close to engineer level.**
  - On 806 DFW TxDOT lettings, the median P50 error is 16%.
  - On the 451 of them with an official engineer's estimate, our median error (17%) is lower than the engineers' (21%), whose estimates run about 20% high.
  - At an 80% not-under rate, the engineers still overshoot less: 23% vs our 31%.
- **Standard items are priced close to the bidders' own spread.** On TxDOT, half of the asphalt, bridge concrete, concrete pavement, riprap, headwall, box culvert and storm pipe lines are within 14–20% of the median bid. In cities, the same holds for curb ramps, driveways and inlets (17–22%). See [Item-level accuracy](#item-level-accuracy).
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
  - The median P50 error is 20%, against about 15% for the engineers.
  - At an 80% not-under rate the overshoot is about 32%, against the engineers' 25%.
  - Almost all of this error comes from estimating the bidders' typical (median) price for the schedule. Very little comes from the step from the median bid to the low bid.
- **Lump-sum items do not yet scale with job size.**
  - Mobilization, traffic control, site preparation and SWPPP / erosion-control lump sums are priced at about the same dollar amount on every job. Bidders price them as a share of the job (mobilization is typically about 5%).
  - Small city jobs (low bid under $0.5M, 56 lettings) come out about 20% high (P50 error 28%).
  - Jobs over $2M come out about 6% low, and P80 is under the low bid on 29% of them.
  - Pricing them as a share of the job fixes the lump-sum lines themselves, but it did not make job totals more accurate (round 12, under "Tried and not adopted").
- **Creek erosion and drainage** (39 lettings) is now better than the city average: P50 error 15%, P80 under the low bid 10% of the time, median overshoot 31%.
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

**Waiting for new lettings (round 13, registered before scoring).** Municipal lines priced from cells dominated by TxDOT lettings were moved to the municipal price level.
- **Development result:** the median overshoot fell from 43% to 38%, mostly because the P80 margin got tighter.
- **Status:** as registered, it stays switched off until 40 new municipal lettings after 2026-11-20 confirm it (`data/test_holdout/REGISTRATIONS.md`, section 6).
- **A second change** shrank prices backed by a single earlier letting. It fixed the worst single lines (a 48-inch water line went from 3.7× to 0.8× the bids), but it did not make job totals more accurate.

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
- **Many items are 8–9 points more accurate on TxDOT than in cities:** sidewalk, concrete pavement, storm pipe and box culverts.
  - TxDOT's standard item codes keep descriptions uniform and history deep. City wording varies, and city data is thinner.
  - Curb ramps, driveways and inlets are as accurate in cities as on TxDOT.
- **The city items with the most money still to gain:**
  - water pipe (8% of city dollars, half within 45%);
  - mobilization (5%, runs 42% high);
  - asphalt (4%, half within 41%);
  - unclassified lines (3%, half within 100%).
  - Fire hydrants run 35% low, pavement marking 26% low, and miscellaneous concrete 22% high.

#### City / municipal (DFW), 2024–26: 190 lettings, 8,867 lines

Across all lines, half are within **47%** of the median bid (**42%** weighted by dollars); 27% are within ±20%. The low bidder is typically 24% from the median bidder on the same line.

| Item | Lines | Lettings | Share of dollars | Half of lines within | Dollar-weighted | Ours ÷ median bid | Lines within ±20% | Low bidder vs median bidder |
|---|---|---|---|---|---|---|---|---|
| ADA curb ramps | 182 | 84 | 0.9% | 16% | 14% | 0.95 | 55% | 21% |
| Driveways | 122 | 75 | 1.7% | 19% | 18% | 0.97 | 52% | 21% |
| Project signs | 59 | 58 | 0.0% | 21% | 31% | 1.02 | 44% | 33% |
| Inlets | 231 | 57 | 1.6% | 22% | 24% | 0.97 | 48% | 16% |
| Sidewalk | 207 | 111 | 3.9% | 23% | 23% | 1.03 | 45% | 21% |
| Valves and hydrants | 167 | 72 | 1.6% | 24% | 34% | 0.84 | 41% | 15% |
| Signs | 73 | 45 | 0.1% | 26% | 13% | 1.02 | 42% | 19% |
| Concrete pavement | 183 | 88 | 12.6% | 27% | 20% | 0.97 | 40% | 14% |
| Box culverts | 59 | 28 | 1.3% | 27% | 27% | 0.90 | 36% | 13% |
| Embankment / fill | 54 | 47 | 0.6% | 27% | 124% | 1.10 | 31% | 50% |
| Water services | 130 | 52 | 2.3% | 28% | 13% | 0.87 | 39% | 18% |
| Storm pipe | 276 | 65 | 2.9% | 29% | 23% | 0.88 | 37% | 19% |
| Flexible base | 104 | 78 | 2.7% | 32% | 46% | 0.92 | 37% | 25% |
| Traffic signals | 230 | 19 | 0.7% | 34% | 54% | 0.96 | 40% | 11% |
| Stone riprap | 46 | 36 | 0.7% | 36% | 43% | 0.87 | 30% | 24% |
| Drilled shafts | 49 | 24 | 1.5% | 37% | 50% | 0.80 | 29% | 15% |
| Excavation | 124 | 89 | 2.6% | 39% | 38% | 0.93 | 29% | 35% |
| Subgrade stabilization | 120 | 55 | 2.9% | 39% | 40% | 0.79 | 25% | 17% |
| Pavement removal | 138 | 83 | 1.7% | 40% | 50% | 0.92 | 28% | 30% |
| Asphalt pavement | 168 | 81 | 4.5% | 40% | 25% | 1.04 | 30% | 20% |
| Concrete beams | 43 | 29 | 0.5% | 40% | 53% | 0.96 | 33% | 38% |
| Tree removal | 65 | 42 | 0.1% | 41% | 56% | 0.90 | 25% | 33% |
| Curb and gutter | 132 | 79 | 0.7% | 41% | 31% | 1.13 | 33% | 33% |
| Electrical and lighting | 367 | 38 | 1.9% | 42% | 64% | 0.84 | 28% | 13% |
| Water pipe | 256 | 63 | 8.3% | 43% | 42% | 0.91 | 30% | 27% |
| Erosion control (unit-priced) | 161 | 67 | 0.4% | 44% | 98% | 0.79 | 29% | 33% |
| Seeding and sod | 309 | 152 | 1.9% | 45% | 57% | 0.88 | 32% | 22% |
| Testing and inspection | 64 | 36 | 0.2% | 46% | >1000% | 1.02 | 30% | 34% |
| Other removals | 157 | 74 | 0.5% | 46% | 76% | 0.98 | 27% | 40% |
| Sidewalk removal | 107 | 59 | 0.3% | 47% | 27% | 1.05 | 28% | 33% |
| Adjust to grade | 166 | 70 | 0.2% | 49% | 51% | 1.08 | 27% | 46% |
| Guardrail | 59 | 25 | 0.1% | 50% | 66% | 0.67 | 31% | 22% |
| Manholes | 159 | 60 | 1.9% | 50% | 79% | 0.90 | 25% | 20% |
| Casing / encasement | 139 | 63 | 2.5% | 50% | 122% | 0.88 | 26% | 25% |
| Sanitary sewer pipe | 109 | 41 | 2.7% | 51% | 48% | 0.95 | 23% | 42% |
| Miscellaneous concrete | 142 | 68 | 1.1% | 53% | 47% | 1.24 | 16% | 27% |
| Traffic control (unit-priced) | 72 | 51 | 0.6% | 53% | 57% | 1.25 | 19% | 27% |
| Utility removal / abandonment | 271 | 87 | 0.6% | 56% | 44% | 0.94 | 20% | 44% |
| Tree planting | 58 | 38 | 0.2% | 57% | 230% | 0.88 | 29% | 15% |
| Fire hydrants | 59 | 53 | 0.5% | 58% | 37% | 0.63 | 24% | 10% |
| Sewer services | 48 | 28 | 0.4% | 59% | 69% | 1.05 | 19% | 35% |
| Pipe fittings | 54 | 33 | 0.3% | 60% | 60% | 1.05 | 26% | 32% |
| Pavement marking | 533 | 73 | 0.3% | 63% | 88% | 0.78 | 19% | 20% |
| Fence | 75 | 50 | 0.2% | 66% | 80% | 0.84 | 17% | 23% |
| Retaining walls | 47 | 25 | 1.4% | 66% | 96% | 0.90 | 23% | 28% |
| Trench safety | 107 | 71 | 0.1% | 67% | 78% | 0.89 | 16% | 60% |
| Contingency / allowance lines | 76 | 22 | 0.6% | 68% | 108% | 0.91 | 24% | 21% |
| Headwalls | 49 | 35 | 0.2% | 72% | 109% | 0.73 | 8% | 43% |
| Relocate / reset | 172 | 70 | 0.3% | 74% | 106% | 0.84 | 21% | 42% |
| Structure demolition | 78 | 46 | 0.1% | 86% | 211% | 0.76 | 15% | 26% |
| Landscape planting | 81 | 17 | 0.2% | 93% | 72% | 1.61 | 20% | 9% |
| SWPPP / temporary erosion control (lump sum) | 136 | 127 | 0.3% | 105% | 169% | 1.09 | 10% | 42% |
| Unclassified lines | 656 | 131 | 3.5% | 106% | 247% | 0.96 | 17% | 30% |
| Traffic control (lump sum) | 127 | 118 | 0.9% | 112% | 290% | 1.32 | 8% | 59% |
| Site preparation (lump sum) | 67 | 64 | 1.4% | 121% | 546% | 0.96 | 18% | 90% |
| Irrigation | 59 | 33 | 0.6% | 141% | 147% | 1.02 | 7% | 15% |
| Mobilization | 166 | 164 | 4.7% | 151% | 382% | 1.43 | 11% | 35% |

#### TxDOT (DFW counties), 2023–26: 806 lettings, 41,146 lines

Across all lines, half are within **31%** of the median bid (**26%** weighted by dollars); 37% are within ±20%. The low bidder is typically 16% from the median bidder on the same line.

| Item | Lines | Lettings | Share of dollars | Half of lines within | Dollar-weighted | Ours ÷ median bid | Lines within ±20% | Low bidder vs median bidder |
|---|---|---|---|---|---|---|---|---|
| Construction exits | 172 | 171 | 0.1% | 13% | 18% | 0.99 | 65% | 17% |
| Asphalt pavement | 651 | 282 | 11.5% | 14% | 12% | 0.93 | 62% | 9% |
| Bridge concrete | 426 | 75 | 5.1% | 14% | 11% | 0.93 | 62% | 15% |
| Concrete mow strip | 189 | 183 | 0.5% | 15% | 16% | 1.06 | 64% | 11% |
| Sidewalk | 161 | 114 | 0.6% | 16% | 9% | 0.97 | 60% | 12% |
| Construction exit removal | 169 | 169 | 0.0% | 16% | 15% | 1.05 | 58% | 17% |
| Headwalls | 218 | 78 | 0.1% | 17% | 15% | 0.99 | 57% | 9% |
| Concrete riprap | 252 | 192 | 0.5% | 17% | 10% | 1.02 | 56% | 15% |
| Stone riprap | 276 | 196 | 0.8% | 18% | 19% | 0.94 | 54% | 14% |
| Concrete pavement | 132 | 83 | 8.1% | 19% | 8% | 0.92 | 52% | 12% |
| Box culverts | 362 | 98 | 2.1% | 19% | 20% | 0.94 | 51% | 15% |
| Flowable backfill | 190 | 160 | 0.3% | 19% | 20% | 0.92 | 53% | 17% |
| Concrete beams | 64 | 55 | 0.1% | 19% | 19% | 0.95 | 55% | 25% |
| Casing / encasement | 316 | 127 | 0.6% | 19% | 49% | 0.92 | 52% | 12% |
| Storm pipe | 600 | 148 | 1.9% | 20% | 16% | 1.01 | 51% | 13% |
| Bridge railing | 267 | 129 | 1.1% | 20% | 24% | 0.95 | 51% | 15% |
| Lighting assemblies | 160 | 56 | 0.3% | 20% | 32% | 0.86 | 49% | 6% |
| Traffic signals | 2242 | 114 | 1.4% | 20% | 28% | 0.89 | 49% | 10% |
| Inlets | 543 | 75 | 1.1% | 21% | 25% | 0.86 | 49% | 16% |
| Signs | 1392 | 262 | 0.7% | 21% | 27% | 1.04 | 48% | 9% |
| Driveways | 224 | 137 | 0.7% | 22% | 21% | 0.95 | 44% | 13% |
| ADA curb ramps | 397 | 102 | 0.2% | 22% | 22% | 0.91 | 47% | 13% |
| Drilled shafts | 574 | 166 | 3.6% | 22% | 32% | 0.86 | 45% | 13% |
| Flexible base | 127 | 109 | 1.4% | 23% | 17% | 0.91 | 46% | 11% |
| Erosion control (unit-priced) | 1715 | 411 | 0.3% | 24% | 20% | 0.97 | 45% | 20% |
| Wingwalls | 293 | 94 | 0.3% | 24% | 23% | 0.95 | 41% | 14% |
| Borrow / backfill | 141 | 136 | 0.1% | 24% | 21% | 1.05 | 44% | 25% |
| Pavement milling | 236 | 196 | 0.9% | 24% | 21% | 0.98 | 43% | 15% |
| Electrical and lighting | 2109 | 142 | 1.1% | 25% | 42% | 0.85 | 41% | 12% |
| Safety end treatments | 516 | 125 | 0.2% | 25% | 23% | 0.92 | 42% | 17% |
| Retaining walls | 65 | 51 | 2.6% | 26% | 23% | 0.88 | 34% | 23% |
| Subgrade stabilization | 353 | 139 | 2.3% | 29% | 28% | 0.85 | 32% | 11% |
| Concrete joints | 267 | 151 | 0.4% | 29% | 40% | 0.88 | 40% | 21% |
| Concrete barrier | 855 | 181 | 2.0% | 30% | 24% | 1.01 | 36% | 25% |
| Guardrail | 1690 | 289 | 0.8% | 30% | 24% | 1.22 | 33% | 11% |
| Shoring | 112 | 108 | 1.3% | 34% | 48% | 0.94 | 31% | 27% |
| Surface treatment | 360 | 261 | 0.2% | 34% | 33% | 1.08 | 33% | 29% |
| Curb and gutter | 288 | 160 | 0.3% | 34% | 30% | 0.91 | 36% | 22% |
| Embankment / fill | 291 | 229 | 3.0% | 35% | 35% | 1.04 | 31% | 29% |
| Concrete repair | 304 | 128 | 0.7% | 35% | 34% | 1.16 | 30% | 27% |
| Pavement marking | 8266 | 423 | 1.6% | 36% | 26% | 1.13 | 32% | 12% |
| Testing and inspection | 98 | 44 | 0.0% | 37% | 17% | 1.12 | 36% | 19% |
| Pavement removal | 438 | 218 | 0.8% | 38% | 38% | 0.89 | 27% | 20% |
| Miscellaneous concrete | 238 | 141 | 0.8% | 38% | 47% | 0.85 | 28% | 25% |
| Seeding and sod | 1052 | 299 | 0.8% | 38% | 23% | 1.02 | 30% | 32% |
| Other removals | 674 | 277 | 0.7% | 40% | 27% | 0.97 | 30% | 26% |
| Mowing | 450 | 192 | 0.8% | 40% | 21% | 1.22 | 29% | 23% |
| Fence | 277 | 122 | 0.3% | 42% | 49% | 1.04 | 31% | 25% |
| Excavation | 348 | 223 | 1.8% | 42% | 66% | 0.97 | 28% | 32% |
| Utility removal / abandonment | 616 | 188 | 0.9% | 43% | 271% | 0.91 | 29% | 42% |
| Unclassified lines | 3807 | 553 | 10.2% | 44% | 30% | 1.00 | 29% | 17% |
| Sidewalk removal | 252 | 151 | 0.1% | 48% | 57% | 0.96 | 24% | 22% |
| Adjust to grade | 89 | 56 | 0.0% | 48% | 114% | 0.92 | 26% | 43% |
| Mobilization | 912 | 805 | 9.8% | 51% | 40% | 1.07 | 28% | 25% |
| Manholes | 67 | 26 | 0.1% | 55% | 209% | 0.70 | 28% | 21% |
| Crash cushions | 500 | 124 | 0.5% | 55% | 58% | 1.32 | 19% | 27% |
| Tree removal | 253 | 59 | 0.1% | 57% | 49% | 1.33 | 22% | 60% |
| Relocate / reset | 118 | 70 | 0.0% | 57% | 489% | 0.91 | 29% | 28% |
| Cleaning / desilting | 150 | 104 | 0.2% | 59% | 58% | 0.81 | 22% | 28% |
| Traffic control (per month) | 2208 | 783 | 3.9% | 64% | 238% | 1.24 | 23% | 63% |
| Structure demolition | 119 | 73 | 0.1% | 65% | 531% | 0.81 | 24% | 38% |
| Preparing right of way / clearing | 242 | 228 | 5.8% | 157% | >1000% | 1.24 | 9% | 71% |

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
