# Project history: Texas public-works OPCC estimator

Covers 2026-11-17 to 2026-11-22. The first handoff followed Round 20. The next day's local worksheet work and single optimization round are recorded below.

## 1. How to read this file

- **What it covers.** What the project tried, in what order, why, with what numbers, and what was decided: data harvesting, parsing, every pricing round, and the rules used to judge them. The web app and the public site have a short history of their own in section 12.
- **Forward in time.** Historical forward runs restrict reference bids and calibration records by letting date. Some use a full-period quantity-band adjustment. Strict as-of-opening comparisons must also date this adjustment, as described in the current evaluation scope in `docs/HANDOFF.md`. "Municipal" means DFW city and county lettings bid in 2024–26. "TxDOT" means TxDOT lettings in the DFW counties bid in 2023–26.
- **Figures move with the data.** Data was added and the parser changed every day. Figures from different dates are on different data builds and different letting counts. Compare two figures only where the text says they are on the same lettings.
- **Local reports.** File names such as `reports/p50_fix_round18.md` point to reports in the owner's working copy. They are not in the repository. The registrations in `data/test_holdout/REGISTRATIONS.md` are in the repository.
- **Company data.** A private company test set exists. It never trains or tunes anything. Its results stay local and none are quoted here.

### Terms

| Term | Meaning |
|---|---|
| P50 | Expected low bid (the median estimate). |
| P80 | The "not-under" estimate used as the OPCC: P50 ÷ a divisor fitted forward in time. |
| Under rate | Share of lettings where P80 is below the actual low bid. The floor is at most 20%. |
| Median overshoot | Median of P80 ÷ low bid − 1 among lettings where P80 ≥ low bid. The main score since 2026-11-20. |
| In band | Estimate ÷ low bid within 1.00–1.13. A hard gate in rounds 1–4; only reported since 2026-11-20. |
| P50 error | Median of abs(P50 ÷ low bid − 1). |
| Mean abs-log error | Mean of abs(ln(P50 ÷ low bid)). 0.41 is roughly a typical miss of 50%. The reports write it "mean \|ln\|". |
| P80 multiplier | Median of P80 ÷ P50, equal to 1 ÷ divisor. |
| Matched not-under overshoot | Each method gets one multiplier that makes exactly 70% / 75% / 80% of its lettings not under; the median overshoot is read at that point. In-sample, report only. |
| Line error sum | Σ abs(our line price − median bidder's amount) over municipal lines with at least 3 bidders. "Ours" is the line price before discount, blend and centring. |
| B | Production at the time of a round (the baseline). |
| Arm | One tested variant. |
| own / B80 | Two ways to score an arm's P80. "own" relearns the divisor and guard on the arm's own forward records. "B80" keeps B's divisor and guard level, so only P50 changes. |
| Item family | The class an item is filed under (the UI calls it "item type"). |
| Token | A word of the item description after generic words are dropped. Lines are matched on token overlap. |
| Lump sum (LS) | A line bid as one amount. |
| Owner-set allowance | A line whose amount the owner fixes. Added at face value. |
| Not covered | A specialty line with almost no bid history. Not priced; listed for a vendor quote. |
| Low confidence | A lump-sum line shown with a warning. Its price still counts. |
| Centring | One level factor per market that makes the median of earlier (estimate ÷ low bid) equal to 1. |
| Comparables blend | Geometric mean of the line-priced estimate and a price built from the five most similar earlier jobs. Called V2. |
| Competition discount | f(N): the median of low total ÷ median bidder's total at bidder count N. |
| Locked public test set | 71 public files (59 letting groups) that never train anything. Opened only to confirm a registered decision. |
| Pre-registration | Arms, data, code hashes and the pass/fail rule written down before any arm is scored. |
| Prospective check | A registered check on lettings bid after 2026-11-20. |

## 2. Timeline

The first two days ran on a Windows PC. On 2026-11-19 at 13:27 the PC crashed for the third time from a memory fault. The owner decided to move everything to a dedicated Mac, and work continued there from 17:30. The public code repository had its first commit on 2026-11-20 at 01:20 and 47 commits by 2026-11-21 16:32.

### 2026-11-17 (day 1, PC)

- Scraped TxDOT's public dashboard (low bid only, last 12 months).
- Downloaded TxDOT's FTP archive: 303 monthly archives, 2015–2024.
- The owner set a rule: everything downloaded goes under `data/` and into the cleaning pipeline without asking.

### 2026-11-18 (day 2, PC)

- By early morning the first city and county sources had landed: the Legistar API for 11 cities, Arlington, Mesquite, Garland, Rowlett, Collin County and others. After a full rerun the training side held 701,439 rows and 3,550 projects, of which municipal 60,586 rows and 391 projects.
- An independent audit against the source PDFs confirmed 23 parser defects. None of the 7 sources was clean. The 11 main defects were fixed.
- The owner set the accuracy target (at least 80% not under; overshoot preferably at most 13%) and stressed for the third time that company data is test only.
- The modelling recommendation was written (market median price × competition discount). The competition discount was calibrated for the first time (2,546 lettings then; refit at every rebuild, 3,345 now). A company OPCC benchmark was run as a test only. Wage and cost-index data had several pitfalls.
- A browser-based download raised a save dialog for every file. From then on all downloads must run unattended (curl or Python only).

### 2026-11-19, early morning to 13:27 (day 3, PC)

- 00:45: two public copies of company projects were found in the public data and quarantined. A new guard, `company_twin_scan`, was added.
- Third harvest: about 290 files (Frisco 148, Richardson 82, Irving 23, Rowlett and others 20, Denton 15, and others).
- The GBM unit-price model v2 was finished and audited. The municipal gain was not significant. It was not connected.
- 07:41 rebuild: master panel 760,096 rows; 216 scorable municipal lettings. Under strict leave-one-out (not forward): P50 error 23.1%, P80 under 19.9%, in band 7.4%, overshoot 45.1%.
- Daytime: the thin-reference experiment (8 variants, none met the rule); the segment test, which found that the calibration and the estimator were not the same tool (later called "alignment"); a split by year showed estimates drifting down in recent years, which started the forward-floor experiment.
- 10:08–12:2x: three parallel harvests: 10 new cities with 117 tabs, counties 13, water utilities and agencies 13.
- 13:27: the PC crashed for the third time. The Dallas and Fort Worth harvests were interrupted. At 15:4x the owner decided to move to a Mac.

### 2026-11-19, from 17:30 (day 3, Mac)

- 17:55 environment restored. 18:13 the first full rebuild took 10 minutes (1,453 files parsed in 3 minutes); parsed rows went from 131,586 to 173,931. 18:4x four-engine OCR finished.
- 18:36 "alignment + forward calibration + guard R4g + narrow year carry" went into the production code. 18:4x spread-based P80 multipliers (not adopted) and the comparables study. 19:4x four pricing defects fixed together (not adopted).
- 19:0x–20:19 the Dallas and Fort Worth harvests were resumed and merged; OCR rows were connected (parser .21). 20:30 the guard target changed from 20% to 16%.
- 21:1x the owner proposed holding out part of the public data as a pure test. 21:5x version 2 of the locked public test set was drawn. 22:47 first opening: the current rule holds the floor.
- 22:xx the engineer benchmark was recomputed by work type; streambank inventory; statewide TxDOT median-price benchmark. 23:15 round 1 of the P50 fixes started (not adopted).

### 2026-11-20 (day 4, Mac)

- 00:3x–00:4x rounds 2, 3 and 4 (none adopted). 01:3x the public code repository was created; the owner stressed that company data must never be uploaded. 01:4x parser round 3 (.23) was rebuilt together with 31 new tabs.
- 02:0x the owner restated the goal (within the floor, the less overshoot the better). Registration 2 was written. 02:38 second opening of the locked set: V2 not confirmed.
- 03:1x round 5. 03:2x item-family overhaul (parser round 3b). 03:3x 159 statewide erosion/drainage tabs. 03:46 and 03:58 parser .24b and .24c.
- 04:22 third opening: V2+F1+F2 confirmed. 05:06 it went live together with parser .25. 05:51 T1 (TxDOT only) went live.
- 08:4x–10:0x rounds 7–10. 09:3x the statewide borrowing arm S1 was not installed. 09:55–10:00 the owner set the "not covered" rule. 10:35 parser .26. 10:51 the not-covered scope went live.
- 10:4x the owner stated that the work to be served is mainly municipal, with little TxDOT. 11:23 fourth opening: CORE not confirmed.
- Afternoon to night: city round 1, 47 tabs (12:22); parser .27 (13:20); round 11 (13:5x, switched off); city round 2, 30 tabs (14:15); error-source and mobilization diagnostic (14:5x); .28 (15:10); two-stage diagnostic (15:2x); item-level accuracy table (15:5x); round 12 (16:17, not adopted); .29 (18:15); round 13 (18:56, switched off); city round 3, 23 tabs (19:40); plans pilot (19:4x); public data repository (20:02); .30 (22:34); round 14 and unit conversion (23:2x, not adopted).

### 2026-11-21 (day 5, Mac)

- 00:0x unit conversion U4 (not adopted). 01:15 city round 4, 17 tabs. 02:42 city round 5, 35 tabs. 04:44 parser .31. 07:17 parser .32.
- 09:1x coverage scope v3 (not adopted). 10:0x round 15 (not adopted; it established that centring cancels in P80). 11:02 city round 6, 5 tabs.
- 12:3x round 16 (bidder-count clamp). 13:4x round 17 (abandonment identity). 14:06–14:35 parser .33. 14:22 the owner changed the adoption rule: more accurate unit prices can also go live. 15:22 round 18 passed; it was installed and verified by 16:00.
- About 15:30 the owner set a limit: algorithm optimization runs to 2026-11-22 09:00 at the latest, then the project summary. At 18:23 this changed to "stop when round 20 finishes".
- 16:36 round 19 reported (no arm passed). 16:36 round 20 started.
- 17:28 round 20's registered arms F and F+C failed. 17:58 the addendum arm G passed track U. 18:00 the round-20 experiment run replaced the production parser file against its instructions; it was stopped before it wrote any data and the parser was restored.
- 18:12 G was installed (parser .34b) with a full rebuild. 18:52 the install was verified and matched the expected figures.

### 2026-11-22 (day 6)

- The owner authorized isolated cleaning of the new statewide City/County
  documents and one pricing experiment after verification. The owner's
  three-project worksheet policy and the earlier P70 recommendation remained
  separate decisions. Round 21 completed at 22:31 UTC. The original model and
  published data bundle stayed in production.

## 3. Scoreboard over time

Each row is production's own rule on the data of that moment. Letting counts differ, so rows cannot be compared strictly; they show direction.

### 3.1 Municipal (DFW), lettings bid 2024–26, forward in time

| When | Change | Lettings | P80 under | Median overshoot | P50 error |
|---|---|---|---|---|---|
| 11-19 18:36 | Alignment + forward calibration + guard (target 20%) + narrow carry, first Mac data | 162 | 19.1% | 52.8% | 26.8% |
| 11-19 20:30 | Guard target 16%, cap 20% (after the 20:10 rebuild) | 176 | 17.0% | 46.8% | 28.6% |
| 11-19 22:40 | Parser .22; locked test set taken out of training | 140 | 16.4% | 51.8% | 29.2% |
| 11-19 23:20 | Two misread bid dates corrected; baseline of round 1 (snapshot 1) | 140 | 17.1% | 60.8% | 29.2% |
| 11-20 02:05 | Parser .23 + 31 new tabs | 168 | 15.5% | 69.0% | 28.8% |
| 11-20 03:46 | .24b (material supply moved out) | 170 | 17.1% | 59.5% | 26.4% |
| 11-20 03:58 | .24c (milling and thin treatments split out) | 170 | 16.5% | 56.5% | 26.5% |
| 11-20 05:06 | V2+F1+F2 live, with parser .25 | 145 | 18.6% | 51.3% | 26.0% |
| 11-20 10:35 | .26 | 144 | 18.1% | 48.6% | 24.5% |
| 11-20 10:51 | "Not covered" scope | 144 | 18.1% | 50.5% | 24.3% |
| 11-20 12:22 | City round 1 | 169 | 17.2% | 49.1% | 23.4% |
| 11-20 13:20 | .27 | 169 | 16.6% | 51.4% | 23.0% |
| 11-20 14:15 | City round 2 | 183 | 15.3% | 46.8% | 23.8% |
| 11-20 15:10 | .28 | 184 | 14.7% | 44.7% | 22.9% |
| 11-20 18:15 | .29 | 186 | 16.1% | 43.1% | 19.1% |
| 11-20 19:44 | City round 3 | 190 | 15.3% | 42.6% | 19.6% |
| 11-20 22:34 | .30 | 188 | 14.4% | 41.6% | 18.8% |
| 11-21 01:15 | City round 4 | 190 | 14.7% | 45.2% | 19.8% |
| 11-21 02:42 | City round 5 | 206 | 15.0% | 46.0% | 19.5% |
| 11-21 04:44 | .31 | 210 | 14.8% | 46.6% | 19.6% |
| 11-21 07:17 | .32 | 224 | 14.3% | 46.1% | 20.5% |
| 11-21 11:02 | City round 6 | 224 | 14.3% | 46.3% | 20.2% |
| 11-21 14:35 | .33 | 224 | 14.29% | 45.78% | 20.20% |
| 11-21 16:00 | Round 18 live | 234 | 13.68% | 45.25% | 19.67% |
| 11-21 18:52 | Round 20 arm G live (parser .34b) | 235 | 13.62% | 44.80% | 19.52% |

Notes:

- The first four rows come from `reports/forward_floor_integration.md`; the rest from the owner's project summary.
- V2+F1+F2 row: on the same .25 data, the old rule scored 148 lettings (under 16.2%, overshoot 59.2%, P50 error 27.1%) and the new rule 145. On the 144 lettings scored by both, overshoot went from 57.6% to 50.6% and the under rate from 16.7% to 18.8%. Use the 144-letting pair for a paired comparison.
- During 11-20, municipal overshoot fell from 69% to about 42% and P50 error from 29% to 19%. Most of it came from parser corrections and V2+F1+F2.
- On 11-21 overshoot rose back to 45–46%. This is not a regression: the newly merged lettings are harder (small cities, many specialty items). On the lettings scored both before and after, the changes are within noise.

### 3.2 TxDOT (DFW counties), lettings bid 2023–26, forward in time

| When | Change | Lettings | P80 under | Median overshoot | P50 error |
|---|---|---|---|---|---|
| 11-19 18:36 | Guard (target 20%) + narrow carry | 798 | 18.7% | not recorded | not recorded |
| 11-19 20:30 | Guard target 16% | 798 | 15.9% | 52.9% | 26.9% |
| 11-19 23:20 | Baseline of round 1 (snapshot 1) | 798 | 16.2% | 51.6% | 26.4% |
| 11-20 02:05 | Parser .23, TxDOT refreshed | 799 | 16.3% | 51.3% | 26.2% |
| 11-20 04:0x | .24c (snapshot 3) | 793 | 16.4% | 52.6% | 26.9% |
| 11-20 05:06 | V2+F1+F2 live, with .25 | 796 | 16.7% | 37.2% | 20.2% |
| 11-20 05:51 | T1 live (TxDOT only) | 796 | 16.0% | 31.3% | 16.6% |
| 11-20 10:51 | .26 and "not covered" scope | 796 | 16.0% | 32.2% | 16.6% |
| 11-20 13:20 | .27 | 805 | 15.5% | 31.9% | 16.5% |
| 11-20 15:10 | .28 | 805 | 15.5% | 31.8% | 16.4% |
| 11-20 18:15 | .29 | 806 | 15.8% | 31.6% | 16.1% |
| 11-20 22:34 | .30 | 806 | 15.6% | 31.3% | 16.2% |
| 11-21 07:17 | .32 | 807 | 15.6% | 31.5% | 16.1% |
| 11-21 14:35 | .33 | 807 | 15.61% | 31.50% | 16.15% |
| 11-21 16:00 | Round 18 live | 807 | 15.61% | 31.50% | 16.16% |
| 11-21 18:52 | Round 20 arm G live (parser .34b) | 807 | 15.49% | 31.46% | 16.10% |

The TxDOT rows are the production baseline rows of the round reports on each data build. Each baseline was checked to reproduce production letting by letting.

### 3.3 Where it stands (2026-11-21, from the public README)

| | Lettings | P50 error | P80 under the low bid | Median overshoot | Overshoot at 70% / 75% / 80% not under |
|---|---|---|---|---|---|
| TxDOT (DFW counties), 2023–26 | 807 | 16% | 15% | 31% | 26% / 27% / 30% |
| TxDOT engineers' own estimates (same 451 lettings, official totals) | 451 | 21% | 14% | n/a | 19% / 22% / 23% |
| City / municipal (DFW), 2024–26 | 235 | 20% | 14% | 45% | 30% / 32% / 34% |
| Municipal engineers' own estimates | 140 | about 15% | 33% | n/a | 20% / 21% / 25% |
| Locked public test set, municipal (last confirmation, 11-20) | 56 | 14% | 20% | 31% | n/a |

- Creek erosion and drainage group (42 lettings): P50 error 18%, P80 under 2%, median overshoot 41%.
- Small city jobs (low bid under $0.5M, 74 lettings): P50 about 20% high, P50 error 24%. Jobs over $2M: P50 about 4% low, and P80 is under the low bid on 25% of them.
- 2026 only: P80 under on 17% of municipal and 23% of TxDOT lettings. TxDOT is above the 20% floor. The guard adapts with a lag.
- For the engineer rows, "P80 under the low bid" means their single published estimate was below the low bid.

## 4. Before the numbered rounds (2026-11-18 to 2026-11-19)

### 4.1 Model choices tried early

| Study | Idea and result | Outcome |
|---|---|---|
| Competition discount | The low bid is the minimum of N bids, so the estimator prices the median bidder and then applies f(N). With no discount, estimate ÷ actual was 1.156 on 2,040 lettings. A discount on the total beat a discount on each unit price on 517 of 831 lettings. Municipal = combined table × one municipal factor, refit at each rebuild (0.9752 on 608 lettings on 11-21; between 0.9752 and 0.9839 over the three days). | Adopted |
| Quantity effect | Inside one bid tab, when the same item appears at two quantities, 80% of the pairs carry exactly the same unit price (87% in classified families). The cross-project slope of −0.141 is more likely a composition effect. A small-quantity form a + b/q did not beat the power law (70 vs 37, 50 ties). | No default quantity scaling; quantity bands are part of the price key |
| Price index weighting | Dollar weighting fit TxDOT's Highway Cost Index worse (mean absolute deviation 4.66 → 9.40 points). | Not adopted |
| Price index by market | Separate municipal and TxDOT arms; engineer-estimate rows removed from the index. The TxDOT arm correlates 0.84 in shape with the Highway Cost Index. | Adopted |
| Escalation to the bid year | Error 26.8% → 24.8% (municipal) and 27.2% → 25.1% (TxDOT). It was the only one of six calibration-versus-estimator differences that was significant in both markets. | Adopted |
| Owner-set allowances at face value | Of 224 such items bid by at least 2 bidders, 200 carried the same amount from every bidder. | Adopted |
| GBM unit-price model v2 | Rolling backtest on past data only: municipal 24.8% → 23.7% (90% interval −11.9 to +4.2 points, not significant); TxDOT 25.1% → 15.8% (significant). Random 5-fold looked better but was not honest in time. | Not connected |
| Hierarchical log-linear mixed model | Recommended on 11-18 as stage 3 (partial pooling, honest intervals for sparse items). | Never implemented |
| Municipal "got worse" diagnosis | After a rebuild the leave-one-out median error seemed to go from 19.9% to 22.9%, but the two figures were on different lettings. On the same 124 lettings it was 20.78% → 22.88% (interval −3.6 to +4.7): noise. For newly scored lettings, having the new tabs in the reference pool lowered the median error from 32.5% to 26.4% (significant). | Keep the new data; fix parse errors |
| Thin reference prices (8 variants) | Pre-registered, strict leave-one-out. None met the rule. The error is not in thinly supported unit prices. Shrinkage made TxDOT worse. | None adopted |
| City price factors | Leave-one-out 23.8% → 24.6%, worse. Between-owner variance is only 11%. | Not adopted |
| Segment test | Erosion/drainage was the worst segment (P50 error 31.8% against 21.9% for the rest). Segment-specific factors did not help. This test found the alignment defect (4.2). | Factors not adopted |
| Level corrections by project type or title words | Forward, 160 municipal lettings since 2023: 21.1% without; 25.2% by type; 27.8% with all text features. Type effects flip between years. One signal looked stable (schedules with dewatering or diversion items run 10–17% low). It was registered separately and could not be judged: 7 flagged lettings where 8 were required. | Not adopted |
| P80 multiplier by predictable spread (3 variants) | Municipal: none raised the in-band share significantly, and all had an under rate above 20%. TxDOT: all three significantly better. | Not adopted |
| TxDOT streambank overshoot | 57 lettings ran high by a mean log of +0.240, of which mobilization was +0.228 (95%). Streambank items themselves ran low (−0.067). P50 ÷ low bid fell from 2.78 (under $100k) to 0.81 (over $3M). | Led to round 1 |

### 4.2 Alignment

- The published constants had been fitted on a calibration path that did not escalate unit prices. The calibration and the estimator were not the same tool.
- The fix: calibration calls `estimate()` directly, pricing each letting from reference tables with that letting removed.
- Under strict leave-one-out (230 municipal lettings, under rate 20.0%) the in-band share went from 7.4% to 17.1%.
- Split by bid year, the median P50 ÷ low bid drifted down: about 1.04–1.06 in 2021–23, 0.97 in 2024, 0.88 in 2025, 0.95 in 2026. With constants set from earlier years only, the under rate was 24.6% (24.1% in 2025–26). The floor did not hold.
- Conclusion: alignment is right, but it had to wait for a forward-in-time rule. Leave-one-out lets future lettings into the reference tables, the index and the constants, and flatters exactly the number the owner cares about.

### 4.3 Forward floor: forward calibration, guard and narrow year carry (live 2026-11-19 18:36)

- **Forward calibration.** Reference prices, index, discount and constants for each letting use only lettings dated earlier. Lettings on the same day do not see each other.
- **Guard (R4g).** P80 = P50 ÷ p, where p is the α-quantile of earlier centred ratios. α is set by a one-sided adaptive rule: each letting with P80 under the low bid lowers α by γ × 0.84; every other letting raises it by γ × 0.16. α is limited to 2%–20%, so the guard only widens P80 and never makes it narrower than the ordinary 20th percentile. γ is 0.02 for municipal and 0.004 for TxDOT.
- **Narrow year carry.** Only when no index arm has the target year are the arms carried forward at their last year's level. If any arm has that year, nothing changes.
- **PC data, 125 lettings:** fixed 20th percentile 22.4% under / 13.6% in band; with the guard 19.2% / 8.8%; with the guard and a full year carry 19.2% / 4.8%.
- **Why not the full carry.** Full carry plus guard gave 4.0 points less in-band share than the guard alone (90% interval −8.0 to −0.8), with no gain at the floor. "No carry" is wrong: until the index has a 2027 row, every 2027 letting would go unescalated.
- **Mac data, municipal, 162 lettings:** old rule (no carry, fixed P80) 22.8% under; the adopted rule 19.1% under, 14.2% in band; full carry plus guard 19.1% / 13.6%.
- **Registered for later:** an out-of-sample check of the narrow carry on lettings from 2027-01-01 (section 8.2).
- Local report: `reports/forward_floor_integration.md`.

### 4.4 Guard target 0.16 with the 0.20 cap (2026-11-19 20:30)

- The 20:10 rebuild added 11 parser fixes, OCR rows and 47 new tabs. Forward, the municipal under rate rose from 19.1% to 21.0% (37 of 176 lettings), and 25.0% for 2026 alone.
- Root cause: the guard aimed at exactly 20%. An adaptive rule lands, on average, where it is aimed, so it is over the line about half of the time.
- Change: aim at 16%, but keep the cap at the ordinary 20th percentile.

| Municipal 2024–26, 176 lettings | Under | In band | Median overshoot |
|---|---|---|---|
| Target 20% (before) | 21.0% | 14.8% | 46.4% |
| Target 16%, cap 16% | 16.5% | 8.0% | 54.7% |
| Target 16%, cap 20% (live) | 17.0% | 14.8% | 46.8% |

- The 16% was chosen after seeing this result, as the first point where both markets stay at or under 20% in every year. It is therefore registered for a check on 2027 lettings (section 8.2).

### 4.5 Comparables study and the four-defects experiment (2026-11-19 evening)

- The owner asked why engineers are accurate. On the same 448 TxDOT lettings, with each side's own bias removed, the engineers' spread was 12.9% and the model's 26.0%. Engineers pick 3–5 similar past jobs and scale by quantity.
- A study of 8 DFW municipal lettings with engineer estimates found the model high on 5 (1.48–2.96×) and low on 3. Three fixable defects explained the high ones: lump sums that do not scale with job size (mobilization at $85k–116k per line), removal priced as new work, and "other" items priced by family and band cells.
- **Comparables method (CMP).** Candidates are earlier lettings of the same market. Similarity = description overlap × size × same owner (×1.5) × same work type (×1.5) × age (halves every 3 years). The top 5 are used, with overlap of at least 0.10. Their low-bid unit prices are moved over and adjusted for quantity, escalation and competition.
- **Pre-registered test, 159 municipal lettings since 2023.** Pure CMP: P50 error 25.2% → 22.9% (not significant), P80 under 22.0% (breaks the floor). Not adopted. Blend with the line method: mean abs-log error 0.380 → 0.342 (−0.038, interval −0.060 to −0.017), under 20.1%. TxDOT, 798 lettings, pure CMP: 27.6% → 16.2%.
- **Coverage decides quality.** On the 59 lettings where comparables covered at least 80% of the dollars, CMP error was 12.8% (line method 17.9%, engineers 13.3%). On the 38 lettings with under half covered it was 36.1%.
- **Four defects fixed together, on 65 municipal lettings never seen before:**

| Arm | Under ≤ 20% | Accuracy significantly better | In band not down by more than 2 points | Result |
|---|---|---|---|---|
| V1: removal identity, LS scaled by job size, "other" by tokens, factor for TxDOT-dominated cells | No (21.5%) | No (−0.014, interval includes 0) | Yes (−1.5) | Fail |
| V2: comparables blend | Yes (20.0%) | Yes (−0.037) | No (−6.2; 13.8% → 7.7%) | Fail |
| V3: both | No (24.6%) | No (−0.034, interval includes 0) | No (−6.2) | Fail |

- None was adopted then. V2 came back in rounds 3 and 5 and was confirmed on new data under a new rule.
- Local reports: `reports/comparables.md`, `reports/pricing_defects_experiment.md`.

### 4.6 Locked public test set (2026-11-19 21:10–22:47)

- The owner proposed drawing a few dozen public bid tabs at random, keeping them out of training, and using them purely as a test.
- **Version 1 was retired before any scoring.** Half of it was erosion/drainage. It locked away 30 of the 43 erosion/drainage letting groups of 2022–26, leaving 13 in training and none for 2023 and 2026.
- **Version 2:** seed 20261119; 22% of every year × work-type cell for 2022–2026; 59 groups (9 erosion/drainage), 71 files with byte-identical copies. The draw looked only at bid date and work type.
- These files are tagged `source_set = "public_test"` at parse time. Every fitted table admits only `external` rows. `src/train_test_guard.py` recomputes each fitted quantity with the test rows switched on and off and fails the build if any fitted number is reached.
- Use: only to confirm a decision registered beforehand. Development uses decoy sets drawn with the same strata and the same group counts.
- It was opened four times (58, 57, 56 and 48 lettings scored):

| Opening | When | Decision confirmed | Lettings | Result | Verdict |
|---|---|---|---|---|---|
| 1 | 11-19 22:47 | Does the production rule hold the floor? | 58 | Under 8/58 = 13.8% (90% interval 6.9–20.8%); in band 13.8%; overshoot 38.2%; P50 error 22.4% | Confirmed ("half clean": the 16% target was set on data that still held these lettings) |
| 2 | 11-20 02:38 | V2 | 57 | Current rule 14.0% under / 46.3% overshoot; V2 17.5% / 47.4%; P50 error 21.2% → 15.6% | Not confirmed (overshoot not lower) |
| 3 | 11-20 04:22 | V2+F1+F2 | 56 | Under 16.1% → 19.6% (11/56); overshoot 35.9% → 31.0%; P50 error 18.4% → 13.6% | Confirmed; went live |
| 4 | 11-20 11:23 | CORE | 48 | Under 18.8% → 29.2% (+10.4 points, interval +4.2 to +18.8); overshoot 30.4% → 33.4% | Not confirmed; switches stay off |

- After the fourth opening, municipal changes are confirmed on new DFW municipal lettings dated after 2026-11-20 (prospective checks).
- The four openings used different data builds, so the current rule's own results (overshoot 38.2%, 46.3%, 35.9%, 30.4%) are not a comparable series. Registration 3 promised to report the comparison with engineers at the same not-under rate; the record of the third opening does not contain it.

### 4.7 What was learned before round 1

- Judge the floor forward in time. Leave-one-out hides the downward drift of price levels.
- An adaptive rule lands where it is aimed. Aim below the line (16%), not on it.
- The in-band target is a narrow slot. With a multiplier of about 1.44, P50 ÷ low bid must land in 0.69–0.78 for P80 to fall in 1.00–1.13.
- The gap to the engineers is in the point estimate, not in the P80 rule. On the same 50 DFW municipal lettings on 11-19, the engineers' median error was 14.4% and the model's P50 error 29.2%.
- There is a structural floor. Even with every median unit price right, "median price × f(N)" leaves about 8.5% error on municipal (σ 0.122) and about 6.1% on TxDOT: the randomness of competition itself.
- The engineer yardstick, checked against source documents on 140 DFW municipal lettings: median error 15.2% (interval 13.1–18.1), 32.9% under the low bid, 22.9% in band, median overshoot 18.7%; raised to 80% not under, overshoot 25.1%. Matched at 70% / 75% / 80% not under: 19.8% / 21.3% / 24.9%.
- The first 12 engineer estimates used to set the goal were too small a sample. After one letting was corrected against its source, they show median error 13.3%, 3 of 12 under, 5 of 12 in band (42%, not half).
- The first TxDOT engineer comparison summed estimate lines from the panel. For about a tenth of the lettings that sum was only about half of the official total, so the first conclusion was wrong. It was redone with official totals.

## 5. Pricing rounds 1–20, in order

### 5.0 How every round was run

- **Pre-registration.** Each round first writes a file with its arms, the hashes of data and code, the pass/fail rule and the tie rule. The file's hash and time are recorded before any arm is scored. A registration is not edited afterwards; additions are dated addenda written before scoring. Several registration titles carry a hand-written time that is a little late; the hash record is the time that counts.
- **Baseline check.** B must first reproduce production letting by letting and field by field (round 6: 2,063 lettings × 17 fields, 0 differences).
- **Paired bootstrap.** Arm and B are compared on the same lettings. Lettings are resampled 2,000 times (seed 20261119). Intervals are 90% percentile intervals of the difference.
- **Windows.** Municipal lettings from 2024-01-01; TxDOT lettings from 2023-01-01. Results are also reported by year.
- **Isolation.** Arms run in a private copy of `src/` and `data/clean`. Production is changed only by a separate install step. The locked test set is never read by an arm.

The pass/fail rule changed four times:

| Rounds | Rule |
|---|---|
| 1–4 | All three must hold. (1) Municipal mean abs-log error significantly lower (interval wholly below 0), TxDOT not significantly worse. (2) Floor: under rate at most 20% overall, and each year at most max(20%, B that year + 3 points). (3) Municipal in-band share not below B − 1 point. |
| 5–10 | After the owner's clarification of 11-20 02:0x, in band is only reported. (a) Floor as above. (b) Municipal median overshoot strictly below B; TxDOT not above B. (c) The interval of the mean abs-log difference is not wholly above 0. Arms within 2 points of the lowest overshoot tie; a tie goes to the simplest arm. |
| 11–17, "track T" | Adds (d): the upper end of the 90% interval of the overshoot difference is below 0. Every arm is scored in own and B80 mode; a tie prefers B80. An own-mode winner stays switched off until its prospective check. |
| 14–17, line track | A B80 arm that fails (d) can still be adopted if it meets (a), (c), (b′) upper end of the overshoot difference at most +1.0 point, and (L) a significantly lower line error sum. |
| 18–20, adoption rule v2 | Three tracks T, U and U+. See the subsection before round 18. |

### Round 1: lump sums priced as a share of the schedule (C1–C5)

- **When.** Registered 2026-11-19 23:15; an appendix was added at 23:33, before any arm was scored. Data: snapshot 1 (the 22:43 rebuild). 140 municipal and 798 TxDOT lettings.
- **Question.** Lump-sum overhead did not scale with job size. Municipal LS lines were 4.8× the bid median on jobs under $0.5M and 0.14× on jobs over $10M, and P50 ÷ low bid fell with size. Pricing them as a share of the schedule should flatten the size gradient.
- **What was tested.** C1: mobilization = r × the sum of the other lines (r about 0.050 municipal, about 0.097 TxDOT). C1b: r by size band. C2: C1 plus lump-sum and monthly traffic control. C4: C1 plus "exact match first". C5: all eight overhead families as shares. C1+C2+C4. An oracle arm (each letting's mobilization set to its own bidders' median) as an upper bound.
- **Rule.** Rounds 1–4 rule: municipal accuracy significantly better, floors held, in band not more than 1 point below B.
- **Result.** TxDOT: every arm significantly better (C4: mean abs-log −0.073, overshoot 51.6% → 40.0%). Municipal: no arm significant (C1 −0.005, interval −0.024 to +0.015); in band fell from 9.3% to 5.7% or lower; C2, C5 and the combination had a 2026 under rate of 27.0%, over the 24.6% cap.
- **Decision.** None adopted.
- **Lesson.** Mobilization is a small part of municipal error: even the oracle improved only −0.029. On top of C1, replacing every non-LS line by the letting's own bid median takes mean abs-log from 0.407 to 0.180, so the error is in ordinary unit prices. On 73 TxDOT bridge-maintenance lettings the under rate rose from 12.3% to 21.9–27.4%: once mobilization was right, underpriced riprap showed.
- **Local report.** `reports/p50_fix_experiment.md`.

### Round 2: discount by size (D1) and centring by size (R1, R2)

- **When.** Registered 2026-11-19 23:55:17 (addendum 2026-11-20 00:02:11). Snapshot 1.
- **Question.** Perhaps the size effect hides in the fixed discount and in the single centring factor, not in the lines.
- **What was tested.** D1: the competition discount in cells of bidder-count band × the model's own pre-discount size band, fitted forward; cells with fewer than 10 lettings use the current f(n). R1: centring as a ridge regression on size. R2: R1 plus LS dollar share, non-exact-match dollar share and bidder-count band. R2+C4. D1+R2.
- **Rule.** Rounds 1–4 rule.
- **Result.** No arm improved municipal accuracy significantly: D1 −0.000, R1 −0.002, R2 and R2+C4 −0.011, D1+R2 −0.009, all with intervals that include 0. D1 moved the municipal in-band share by +4.3 points (9.3% → 13.6%) and overshoot from 60.8% to 51.8% without changing accuracy. R2+C4 improved TxDOT significantly but dropped municipal in band to 6.4%.
- **Decision.** None adopted.
- **Lesson.** Regression coefficients drift and change sign over time. D1 changes where P80 lands, not how accurate P50 is. Municipal error is not a size problem.
- **Local report.** `reports/p50_fix_round2.md`.

### Round 3: comparables blend V2, with and without D1

- **When.** Registered 2026-11-20 00:13:40 (addendum 00:18:51). Snapshot 1.
- **Question.** V2 was more accurate on 65 new lettings but lost in-band share. D1 raises the in-band share. They might complement each other.
- **What was tested.** V2; V2+D1; C4+D1; V2+C4+D1.
- **Rule.** Rounds 1–4 rule.
- **Result.** For the first time municipal accuracy improved significantly: V2 −0.029 (interval −0.048 to −0.009), V2+D1 −0.030, V2+C4+D1 −0.031. TxDOT improved a lot (V2 −0.103; V2+C4+D1 −0.137, P50 error 26.4% → 15.4%). But municipal in band was 1.4% (V2), 5.7% and 5.0% against B's 9.3%. C4+D1 had a 2026 municipal under rate of 29.7%, over the cap.
- **Decision.** None adopted.
- **Lesson.** V2 missed a few more lettings in 2021–23, so the guard entered 2024 wider. The P80 multiplier rose to 1.547, and the range of P50 ÷ low bid that lands in band moved to 0.65–0.73, with only 7 lettings in it. A diagnostic by family showed "other" (unclassified lines) as the largest municipal error block: an oracle on it lowers mean abs-log by 0.060 (0.411 → 0.351). 72% of its dollars were priced by band cells at about 0.15× the true price.
- **Local report.** `reports/p50_fix_round3.md`.

### Round 4: calibration on the latest 36 months only (W36)

- **When.** Registered 2026-11-20 00:31:25 (addendum 00:37:50; scored 00:38). Snapshot 1.
- **Question.** Calibration remembers all history and carries the early guard state into the present. The market also moves (DFW TxDOT unit prices fell about 15% from 2025 to 2026).
- **What was tested.** W36: centring, P80 divisor and guard use only the latest 36 months (at least 60 lettings in the window). V2+D1+W36. V2+C4+D1+W36.
- **Rule.** Rounds 1–4 rule.
- **Result.** W36 alone left accuracy unchanged (−0.002). V2+D1+W36 was significantly more accurate (−0.031, interval −0.050 to −0.010) and held the floors, but had 11 lettings in band where 12 were needed (7.9% against 8.3%). V2+C4+D1+W36 had 3.6% in band.
- **Decision.** None adopted.
- **Lesson.** The window did remove the early drag (multiplier 1.503 → 1.439). The remaining in-band gap comes from where P50 lands, not from the guard.
- **Local report.** `reports/p50_fix_round4.md`.

### Registration 2 and the second opening of the locked set (V2)

- **When.** 2026-11-20 02:1x registered; 02:38 opened. Data: snapshot 2 (the 02:01 rebuild: parser .23, 31 new DFW tabs, TxDOT refreshed).
- **Question.** At 02:0x the owner restated the goal: the floor stays; within the floor the score is how far over P80 is when it is over, the less the better; 13% is a hope, not a hard requirement. Which of the round 3 and 4 options is best under that goal?
- **What was tested.** The seven frozen options: W36, C4+D1, V2, V2+D1, V2+D1+W36, V2+C4+D1, V2+C4+D1+W36.
- **Rule.** (a) floor; (b) municipal overshoot below the current rule, TxDOT not above; (c) accuracy not worse. Lowest municipal overshoot wins; within 2 points is a tie, and the simplest wins. Locked-set conditions: under rate at most 20%, and overshoot below the current rule on the same lettings.
- **Result.** Development pick: V2 (municipal under 16.1%, overshoot 56.9% against 69.0%, mean abs-log −0.035, interval −0.054 to −0.016; TxDOT overshoot 36.3% against 51.3%). Locked set, 57 lettings: current rule 14.0% under and 46.3% overshoot; V2 17.5% and 47.4%. P50 error fell from 21.2% to 15.6%.
- **Decision.** Not confirmed (overshoot not lower); not adopted.
- **Lesson.** V2's P50 is more accurate on unseen lettings, but its P80 multiplier got smaller (1.550 → 1.495), 2 more lettings came in under, and the overshoot did not shrink.
- **Local record.** `data/test_holdout/REGISTRATIONS.md`, section 2.

### Round 5: two line-pricing defects, F1 (size extrapolation) and F2 (removal identity)

- **When.** 2026-11-20 02:5x (addendum written 02:53:35). Snapshot 2. 168 municipal and 799 TxDOT lettings.
- **Question.** An audit of the worst lines found two defects. A 48-inch pipe fell into the unsized cell (about $45/LF, 0.037× the bids). And "remove" was a stop word, so install lines could take removal prices.
- **What was tested.** F1: a sized line with no cell of its size in any quantity band uses the nearest sized cell × (size ÷ reference size)^k instead of the unsized cell. F2: pure removal lines match only removal history; all other lines match only non-removal history. F1+F2. V2+F1+F2.
- **Rule.** Rounds 5–10 rule. Simplicity order F1 < F2 < F1+F2 < V2+F1+F2.
- **Result.** Municipal overshoot: B 69.0%; F1 63.6%; F2 60.4% (−8.6 points, interval −17.4 to −1.9); F1+F2 59.2%; V2+F1+F2 58.4%. F2: under rate 15.5% → 16.1%; TxDOT overshoot 51.3% → 48.8%.
- **Decision.** All four qualified; F2 was chosen by the tie rule. Nothing was installed, because the data changed first (next subsection).
- **Lesson.** Removal lines summed to 1.42× their bid medians before F2 and 0.82× after. F1 fixed the 48-inch pipe (that letting's P80 ÷ low bid went from 0.938 to 1.135) but also magnified misread sizes (a curb read as "5430 in", 1.25-inch milling read as 25): 68 lines moved closer and 79 farther. The example line quoted in the registration was in fact priced correctly in forward pricing; the defect was real on other lines (F2 changed the pricing source of 900 municipal non-removal lines).
- **Local report.** `reports/p50_fix_round5.md`.

### Round 5 re-run on snapshot 3, registration 3, third opening: V2+F1+F2 goes live

- **When.** 2026-11-20 04:0x–04:22; live at 05:06.
- **Question.** Parser .24c changed the data. Does round 5's choice survive an unchanged re-run?
- **What was tested.** The same four candidates and the same rule on snapshot 3.
- **Rule.** As round 5. Locked-set conditions as in registration 2.
- **Result.** F1, F2 and F1+F2 failed (b): their municipal overshoot (59.0%, 56.8%, 55.6%) was not below B on the same lettings (55.7%, 55.7%, 55.3%), mostly because B itself had improved from 69.0% to 56.5%. Only V2+F1+F2 qualified: 166 lettings, overshoot 55.3% → 53.1%, under 16.9% → 18.1%, mean abs-log −0.031 (interval −0.052 to −0.012); TxDOT (793 lettings) overshoot 52.6% → 36.6%. Locked set, third opening, 56 lettings: under 16.1% → 19.6% (11 of 56), overshoot 35.9% → 31.0%, P50 error 18.4% → 13.6%.
- **Decision.** Confirmed and adopted. Live at 05:06 together with parser .25. Switches: `pricing_rule()` can turn F1, F2 and V2 off separately.
- **Lesson.** The same option can flip between data snapshots. Re-run on the current data, then confirm on unseen data. On the 9 erosion/drainage lettings of the locked set the variant was worse (under 0% → 11.1%); the sample is too small for a conclusion.
- **Local reports.** `reports/p50_fix_round5_snap3.md`, `reports/v2f1f2_integration.md`.

### Tail audit (2026-11-20 04:0x)

- **Question.** The owner asked whether extreme lettings, where the low bid is far below the second bid, disturb training.
- **What was done.** The 110 lettings at both tails were checked against their source documents.
- **Result.** 102 were real bids, 7 were parse errors and 1 was a pairing error. After correction they were still in the tails.
- **Decision.** Training already uses medians, so one very low bidder has little effect. The test side must not drop them, because real use will meet them. The parse errors went into parser round 4 (.25).
- **Local report.** `reports/tail_audit_2026-11-20.md`.

### Round 6: "other" lines by most similar description (O1), and T1 for TxDOT

- **When.** Registered 2026-11-20 05:25:32. Snapshot 4. B = V2+F1+F2. 145 municipal and 796 TxDOT lettings.
- **Question.** "Other" was the largest municipal error block. Can unclassified lines be priced from their nearest described neighbours? And does C4 + D1 pay on TxDOT, where rounds 1–3 showed it helps?
- **What was tested.** O1: "other" lines priced from the 5 most similar earlier descriptions (same unit and quantity band). T1 = C4 + D1 on TxDOT only: exact matches first, mobilization as a share of the schedule, and a competition discount that varies with job size. O1+T1.
- **Rule.** Rounds 5–10 rule. For T1, (b) is judged on TxDOT, and municipal must equal B letting by letting.
- **Result.** O1: municipal overshoot 51.3% → 54.0% (+2.7 points, interval +0.1 to +7.6), although P50 error fell from 26.0% to 23.3%. T1: TxDOT overshoot 37.2% → 31.3% (−5.9 points, interval −8.0 to −3.0), P50 error 20.2% → 16.6%, under rate 16.7% → 16.0%; municipal identical letting by letting.
- **Decision.** T1 was the only qualifier. Live at 05:51, TxDOT only (registration 4). It could not be confirmed on the locked set, which holds municipal lettings only, so it carries a prospective check. Switch: `estimate_low_bid.pricing_rule(txdot_c4d1=False)` turns it off.
- **Lesson.** O1 made the lines much more accurate (dollar-weighted abs-log 2.00 → 0.69), but 69% of the in-scope lines found no neighbour, the facility tail stayed out of reach, and the multiplier went up. Known T1 risks: TxDOT 2026 under rate 22.5%; bridge maintenance (72 lettings) under rate 20.8% → 26.4%.
- **Local report.** `reports/p50_fix_round6.md`.

### Round 7: letting-level correction for facility jobs (FC, FC-ED)

- **When.** Registered 2026-11-20 08:42:46. Snapshot 5. B = V2+F1+F2+T1.
- **Question.** Pump stations, generators and pedestrian bridges have no line history. Can a correction be learned per letting instead?
- **What was tested.** FC: flag a letting as a facility job when facility-keyword lines plus "other" LS lines hold at least 0.20 of B's line dollars, then shift its bid part by the shrunken mean residual of earlier flagged lettings. FC-ED: the same, plus a second group for erosion and drainage work.
- **Rule.** Rounds 5–10 rule.
- **Result.** FC: overshoot 51.29%, exactly equal to B; the rule needs strictly lower. FC-ED: 53.0%.
- **Decision.** None adopted.
- **Lesson.** Only 6 of the 15 flagged lettings were real facility jobs. One pedestrian bridge had a flag share of 0, because B had not priced the bridge line at all. Erosion/drainage residuals average about 0: its under-estimates come from spread, not from level. This led to the "not covered" scope.
- **Local report.** `reports/p50_fix_round7.md`.

### Round 8: raise weakly supported lines (L1, U1)

- **When.** Registered 2026-11-20 09:01:14. Snapshot 5.
- **Question.** Do lines priced on weak evidence run low, so that raising them helps?
- **What was tested.** L1: weak lines priced at the 75th percentile of their reference cell instead of the median. U1: a letting-level correction on the share of weak dollars. L1+U1.
- **Rule.** Rounds 5–10 rule.
- **Result.** L1: overshoot 58.3% (+7.0 points). U1 was identical to B, because its coefficient was held at 0 on every date.
- **Decision.** None adopted.
- **Lesson.** The weak-evidence share is unrelated to the residual (Spearman −0.01). L1 raised line dollars by 16.6% in total.
- **Local report.** `reports/p50_fix_round8.md`.

### Round 9: owner (city) effects (OW1, OW2)

- **When.** Registered 2026-11-20 09:13:41. Snapshot 5.
- **Question.** Does a city's past residual predict its next letting? The registration said in advance that it expected failure: the correlation is +0.035 (p 0.64) and the between-owner variance is estimated at 0.
- **What was tested.** OW1: shift each letting by the shrunken mean residual of the same owner's earlier lettings (none below 3 lettings). OW2: the same with weights that halve every 365 days.
- **Rule.** Rounds 5–10 rule.
- **Result.** Overshoot 51.76% and 51.86% against B's 51.29%.
- **Decision.** None adopted.
- **Lesson.** The deepest misses come from owners with fewer than 3 earlier lettings.
- **Local report.** `reports/p50_fix_round9.md`.

### Statewide borrowing S1 (2026-11-20 08:40–09:3x)

- **Question.** DFW has too few erosion and drainage lettings. Can prices be borrowed from the rest of Texas?
- **What was done.** 159 statewide itemized tabs were parsed in a private copy. 121 gave rows; 94 lettings entered the price pool (27,745 bid rows). 13% of the lines had no unit; 14 scans gave no rows. Regional price levels have no stable single factor (statewide ÷ DFW median 1.04, interquartile range 0.70–1.79). Scored as unseen lettings before any merge, on the 14 with an engineer's estimate: model P50 error 36.5%, engineers 17.2%.
- **Rule.** Rounds 5–10 rule.
- **Result.** S1: municipal overshoot 51.3% → 50.4% (difference −0.9 points, interval −1.7 to +3.2). The 2024 under rate rose from 20.0% to 22.0%. Of 118 changed lines, only 52 moved closer to the bid median.
- **Decision.** S1 passed the registered rule but was not installed: the gain is inside the noise. The statewide tabs stay in staging and are not in training.
- **Lesson.** The test exposed production defects (borrow excavation priced as flowable backfill, fine grading off by a factor of hundreds, item numbers glued into descriptions and read as sizes, box culverts of all sizes in one cell). They went into parser round 5. S1 is to be re-registered after the intake problems are fixed.
- **Local report.** `reports/statewide_integration_test.md`.

### Line-by-line diagnostic of the worst municipal lettings (2026-11-20 09:3x)

- **Question.** Where do the worst municipal estimates come from?
- **Result.** Low tail (24 lettings under 0.70× the low bid, total gap −$64.8M): 82% from line prices, 19% from competition. High tail (16 lettings over 1.6×, +$14.0M): 83% from line prices. If every line were priced at the letting's own bid median: mean abs-log 0.359 → 0.093, P50 error 26.0% → 7.5%, overshoot 51.3% → 13.1%.
- **Causes, low tail (by dollars / by lettings).** Lump sums that do not scale 21.2% / 8.6%; no comparable history 17.4% / 26.1%; ordinary market variation 14.3%; cells polluted by TxDOT price levels 11.4%; wrong family 5.2%. High tail: lump sums 19.1% / 54.6%; wrong family 13.6%; size extrapolation 11.8%; misread unit or quantity 10.1%.
- **Two general defects found.** (a) Municipal lines were priced from cells that mix in TxDOT lettings, which dominate by count: seeding by the SY at $0.30 (TxDOT) against $2.50 (municipal); mobilization LS at $120k (1,492 lettings) against $60k (302 lettings). (b) General-conditions lump sums are nearly constant in the model while bidders scale them with the job: by job-size quintile the model's mobilization stayed at $94k–147k while the bid median rose from $15.6k to $485k (4.6–6.2% of the contract, median 4.9%).
- **Limit.** About a third of the low tail and 27% of the high tail (competition plus ordinary market variation) cannot be fixed from bid history. A realistic gain is overshoot from about 51% to 40% and P50 error from 26% to 21%.
- **Decision.** Its proposals became the switches of round 10 and the "not covered" scope.
- **Local report.** `reports/diag_worst_municipal_2026-11-20.md`.

### Round 10: municipal fixes from the diagnostic (MC, CORE, CORE+LS)

- **When.** Registered 2026-11-20 09:55:51. Snapshot 5. 145 municipal lettings (142 common for CORE).
- **Question.** Do the defects named by the diagnostic, fixed as general rules, lower the overshoot?
- **What was tested.** MC: a municipal line reads a municipal-only cell when it has at least 3 earlier municipal lettings, otherwise the mixed cell. CORE = MC + E (removal identity on the comparables side) + F1P (size extrapolation for pipe families only) + T2 (family cells need at least 2 lettings) + D (a lump sum bid identically by 3 or more bidders is an owner-set allowance). CORE+LS: CORE plus lump sums priced as shares.
- **Rule.** Rounds 5–10 rule.
- **Result.** MC: overshoot 51.3% → 45.0% (−6.3 points, interval −8.5 to −0.2), P50 error 26.0% → 20.6%, under rate unchanged at 18.6% (2026: 26.8% against a cap of 27.4%). CORE: overshoot 40.0%, but under rate 20.4% overall and 29.3% in 2026. CORE+LS: 2026 under rate 29.3%.
- **Decision.** MC was the only qualifier and was sent to confirmation (registration 5). No patch was applied.
- **Lesson.** T2 turns lines into "unpriced" and so removes money silently (one rock-anchor line of about $732k). The share rule cut a small job's mobilization far below the bids.
- **Local report.** `reports/p50_fix_round10.md`.

### Coverage-scope change: specialty items "not covered" (live 2026-11-20 10:44–10:51)

- **Question.** The diagnostic showed that about a sixth of the low tail by dollars (26% by lettings) is specialty equipment with no real history. The owner decided at 09:55–10:00: pumps, generators, prefabricated bridges and the like are "not covered"; do not price them, flag them, and judge accuracy on the covered part only.
- **Rule of the scope.** Fixed in `src/coverage.py` before any scoring under it. Seven classes: pumps and pump or lift-station equipment; generators and electrical equipment; prefabricated or pedestrian bridges; tanks and process equipment; special buildings; artificial turf and playground equipment; meter vaults. Only the equipment itself counts; ordinary civil work at a facility stays covered. Scoring compares the covered estimate with the low bid minus the low bidder's own amounts on those lines. It flags 58 of 847 municipal lettings.
- **Version 2 (10:40, before the re-run).** Five "wrong meaning" cases were corrected (for example a rented generator, or cleaning a bridge railing). The "whole facility" rule was switched off (`WHOLE_FACILITY_RULE = False`): one "lift station complete in place" line had made a 248-line, $34 million schedule unfit for any estimate. Still in force: when more than half of a schedule's lines are not covered, the letting gets no estimate.
- **Result.** On snapshot 5: municipal under rate 18.6% → 17.4%, overshoot 51.3% → 48.9%, P50 error 26.0% → 24.5%; TxDOT basically unchanged. On the .26 data (144 lettings): old scope 18.1% / 48.6% / 24.5%; new scope 18.1% / 50.5% / 24.3%.
- **Decision.** Adopted as the owner's definition of what is estimated. Switch: `COVERAGE_SCOPE = True`; `pricing_rule(coverage_scope=False)` or `--full-scope` gives the old scope. A vendor quote entered for a not-covered line is added at face value.
- **Local report.** `reports/coverage_scope_integration.md`.

### Round 10 re-run under the new scope, registration 5, fourth opening: CORE fails

- **When.** 2026-11-20 10:59–11:23. Snapshot 6 (parser .26, not-covered scope).
- **Question.** Which round 10 arm does the registered rule pick on the new data and scope, and does it hold on the locked set?
- **What was tested.** MC, CORE, CORE+LS, re-run exactly as registered.
- **Rule.** Rounds 5–10 rule; locked-set conditions as before.
- **Result.** All three qualified: MC 44.4%, CORE 42.0%, CORE+LS 43.3%. CORE against B on the same 142 lettings: overshoot 49.0% → 42.0% (−7.0 points, interval −12.0 to −3.0), P50 error 24.1% → 19.8%, under rate unchanged at 18.3%; 2026 at 25.0% against a cap of 25.5%, one letting short of breaking it. The tie rule picked CORE. Under the old scope CORE would have failed the 2026 cap. Locked set, fourth opening, 48 lettings: under rate 18.8% → 29.2%, overshoot 30.4% → 33.4%.
- **Decision.** Not confirmed, not adopted. The preset `round10_core` stays in the code with all five switches off.
- **Lesson.** A change that tightens the forward residuals also narrows the P80 margin (multiplier 1.39 → 1.31). Unseen lettings did not tighten as much, so the under rate went up. Look at the under rate on unseen lettings first. This produced the B80 mode: a price improvement may not narrow the margin on the side.
- **Local report.** `reports/p50_fix_round10_rerun_snap6.md`.

### Afternoon diagnostics (2026-11-20)

**Erosion/drainage group, line by line (13:0x).** By dollars the largest single cause was family errors that the parser can fix: 22.7% of the absolute gap ($18.5M). The largest case was sod-bed preparation and temporary irrigation, 280,000 SF priced at $28.34/SF against bids of $0.07 and $0.30. About 26% of the gap cannot be fixed (competition 15.5% plus ordinary market variation 10.4%). It led to parser .27. Local report: `reports/diag_ed_and_parser_round6.md`.

**Error sources and mobilization (14:5x, 183 municipal lettings).** Shares of abs-log error: ordinary unit-priced items about 78% (top 10 families 33%, other families 32%, family "other" 13%); mobilization 17%; other general-conditions lump sums 7%; competition discount 9%; the comparables blend −11% (it pulls error back). With every line at the bid median, P50 error falls from 23.8% to about 8%. Mobilization is a stable share of the low bidder's total (median 4.8%; half of lettings in 3.6–5.8%), while the model priced it at about $77k–79k per line at every size. Fixing mobilization alone barely moves total error (23.8% → 20.9% even with the low bidder's actual amount). Outcome: a display-only "low confidence" flag for mobilization and general-conditions lump sums; a forward re-run of 2,129 lettings showed 0 differences in P50 and P80. Local report: `reports/error_sources_and_mobilization_2026-11-20.md`.

**Two-stage diagnostic (15:2x, 184 municipal lettings).** Stage 1 is predicting the median bidder's total; stage 2 is the step from that median to the low bid.

| | Stage 1 | Stage 2 |
|---|---|---|
| Share of mean abs-log error | 78% | 22% |
| Share of variance | 95% | 5% |

| Scenario | P50 error | Median overshoot |
|---|---|---|
| Now | 22.9% | 44.7% |
| Stage 1 perfect | 8.4% | 15.0% |
| Stage 2 perfect | 21.5% | 46.7% |

Stage 1 runs about 13% high on small jobs and about 5% low on large jobs. With a forecast bidder count instead of the actual one, municipal is unchanged (under 14.7% → 14.7%) and TxDOT overshoot rises from 31.8% to 34.1%. On jobs under $0.5M, lump sums lift the total by about 28% while unit-priced lines run about 12% low; the two cancel, which is why fixing mobilization alone fails. Local report: `reports/two_stage_diagnostic_2026-11-20.md`.

**Item-level accuracy (15:5x).** The owner asked which items are already accurate. Over all municipal lines the typical miss was 48% (TxDOT 31%). Best: curb ramps 17%, driveways 20%, inlets 22%. Worst: mobilization 150%, lump-sum traffic control, site preparation and SWPPP 115–121%, "other" 102%. The current table is in the public README. Local report: `reports/line_accuracy_by_item_2026-11-20.md`.

### Round 11: every arm scored in own and B80 mode; CORE without T2; TOK; BLANK

- **When.** Registered 2026-11-20 13:34:29. Snapshot 7 (parser .27). 169 municipal and 805 TxDOT lettings.
- **Question.** CORE failed on unseen lettings because its own P80 margin had narrowed. Does a price change still win when it must keep B's margin (B80)? Three further ideas were added: CORE without T2 (T2 removed money silently), size-sensitive tokens (TOK) and a rule for blank descriptions (BLANK).
- **What was tested.** P_MC, P_CORE, P_CORE_noT2, P_MC_TOK and P_MC_BLANK, each in own and B80 mode: 10 arms.
- **Rule.** Track T: (a)–(d). Ties within 2 points go first to B80, then to the simplest (MC < MC_BLANK < MC_TOK < CORE_noT2 < CORE).
- **Result.** 9 of 10 arms qualified (P_MC/B80 failed (d): upper end +0.3 point). Lowest overshoot: P_CORE/own 37.3%. P_CORE_noT2/own was within 2 points and simpler, so it was chosen: overshoot 51.4% → 38.2% (−13.2 points, interval −16.7 to −8.8), under rate unchanged at 16.6%, P50 error 23.0% → 19.4%. The best B80 arm reached 42.6%.
- **Decision.** In the code as preset `round11_core_not2` (municipal cells, comparables-side removal identity, pipe-only size extrapolation, identical-bid owner-set allowance). Switched off, waiting for the prospective check on 40 new municipal lettings, because an own-mode arm carries exactly the margin risk that sank CORE.
- **Lesson.** Most of an own arm's gain comes from a narrower margin. Matched at 80% not under, the chosen arm (34.0%) is still about 1.4× the engineers (24.9%).
- **Local report.** `reports/p50_fix_round11.md`.

### Round 12: lump sums as a share of the job (P_LS), centring by size band (P_SC)

- **When.** Registered 2026-11-20 16:00:07. Snapshot 8 (parser .28). 184 municipal lettings. Municipal only; TxDOT identical in every arm.
- **Question.** At about 15:3x the owner pointed out that lump sums such as mobilization and temporary erosion control are usually figured as a percentage of the whole project, so their dollar amounts cannot be compared between projects.
- **What was tested.** P_LS: five lump-sum classes (mobilization, traffic control, site preparation, SWPPP / temporary erosion control, bonds and insurance) priced as a historical share × the rest of the schedule, with the share learned by size band (under $0.5M, $0.5–2M, $2M and over). P_SC: the production centring rule applied within each size band. P_LSC: both. Each in own and B80 mode: 6 arms.
- **Rule.** Track T.
- **Result.** No arm passed. P_SC/own had an overshoot of 44.74% against B's 44.71%, not strictly lower. The lump-sum arms had a 2026 under rate of 22.2% or more, over the 22.0% cap, and an overshoot above B.
- **Decision.** Not adopted. Production unchanged.
- **Lesson.** The lines themselves became much more accurate: over 499 lump-sum lines the abs-log miss against the median bidder fell from 0.85 to 0.52 (mobilization 0.91 → 0.35); small-job lump sums went from 2.77× to 0.87× the bids and large-job ones from 0.56× to 0.97×. P_SC removed the size bias of P50 (median P50 ÷ low bid on small / medium / large jobs 1.26 / 1.07 / 0.94 → 1.10 / 1.05 / 1.00). But the spread of total errors did not shrink (mean abs-log 0.331), so the overshoot did not fall. Fixing a class of lines that is biased by size helps totals only if the remaining spread gets smaller. On small jobs, lower lump sums expose unit-priced lines that run low; on large jobs the misses are large and specific to each job.
- **For reference.** Historical shares of the rest of the schedule, all municipal lettings to 2026-11-20, for under $0.5M / $0.5–2M / $2M and over: mobilization 6.7% / 5.1% / 4.9%; traffic control 3.0% / 1.4% / 1.2%; site preparation 4.8% / 3.0% / 2.0%; SWPPP 2.0% / 0.8% / 0.4%.
- **Local report.** `reports/p50_fix_round12.md`.

### Round 13: shrink thin cells (P_T); municipal price level for TxDOT-dominated cells (P_L)

- **When.** Registered 2026-11-20 18:46:52. Snapshot 9 (parser .29). 186 municipal and 806 TxDOT lettings; 8,687 municipal lines with a line error sum of $300.2M.
- **Question.** A diagnostic showed two things. Family and F1 cells backed by a single letting priced 135 lines with $32.7M of absolute error. And band cells dominated by TxDOT lettings (80% or more) ran at 0.81 of the bid median, against 0.96 for purely municipal cells.
- **What was tested.** P_T: a last-tier price backed by fewer than 5 lettings is shrunk toward its parent cell (same family and unit), with weight n/(n+3) on the cell. P_L: a municipal line priced from a band or family cell is multiplied by the measured municipal ÷ TxDOT level ratio of its family, raised to the TxDOT share of the cell; the token tier is left alone. P_TL: both. Each in own and B80 mode: 6 arms.
- **Rule.** Track T.
- **Result.** P_L/own: overshoot 43.1% → 38.0% (−5.2 points, interval −7.2 to −1.1), under rate 16.1% → 14.5%, P50 error 19.1% → 19.4%. P_TL/own also passed (38.7%), a tie; the simpler P_L wins. P_L/B80 was −3.1 points with an interval that crosses 0 (−5.8 to +0.7) and failed (d). P_T failed (d) in both modes.
- **Decision.** P_L/own chosen. As an own-mode arm it is in the code as preset `round13_l`, switched off, waiting for the prospective check. Jobs of $2M and more are to be reported separately there: in development their under rate went from 25.9% to 27.6%.
- **Lesson.** Most of P_L's gain comes from a tighter margin (multiplier 1.309 → 1.288). P_T fixed the worst single lines (140 lines, $24.7M → $9.5M; a 48-inch water line from 3.70× to 0.77× the bids). But those overpriced lines had been offsetting under-pricing elsewhere: that letting's P80 ÷ low bid went from 1.52 to 0.95 and it became an under.
- **Local report.** `reports/p50_fix_round13.md`.

### Round 14: capped mobilization (P_C), weak token matches (P_W), and combinations

- **When.** Registered 2026-11-20 23:06:31. Snapshot 10 (parser .30). 188 municipal lettings, 8,766 lines.
- **Question.** Where a bid form prints a cap on mobilization ("5% Max"), bidders bid at about the cap (bid median about 0.95 of it). The model priced these lines at 3.88× the bids on jobs under $2M and 0.38× on jobs of $2M and over. A level correction would be absorbed by centring; only removing line noise can help, and weak token matches (overlap under 0.5) are the noisiest.
- **What was tested.** P_C: a capped mobilization line priced at the cap × the rest of the schedule. P_W: a weakly matched token price blended with the band-cell price of the same line. P_CW: both. P_CWT: both plus round 13's P_T. Each in own and B80 mode: 8 arms.
- **Rule.** Track T, plus the line track, registered in this round because the worksheet shows line prices.
- **Result.** Track T: all 8 arms failed (d). Line track: the 4 B80 arms passed (a), (c) and (L) but failed (b′), with upper ends of +2.9, +1.9, +1.9 and +2.2 points against the +1.0 limit. Line error sum: B $299.4M; P_C $289.0M; P_W $291.8M; P_CW $281.7M; P_CWT $265.8M (−$33.6M). The 51 capped mobilization lines went from $14.85M to $4.46M of error (2.00× → 1.01× the bids). Under rates of the B80 arms: 16.0%, 14.9%, 14.9% and 16.0% against B's 14.4%.
- **Decision.** Not adopted. P_CWT and P_CW were re-tested in round 18 as X2 and X3 and failed there on the under rate.
- **Lesson.** Correcting the cap exposes unit-priced lines that run low on the same schedule: 3 lettings became unders (2 small jobs, 1 medium). On one small job P80 ÷ low bid went from 1.13 to 0.71. On large jobs mobilization is only about 5% of the job, so the cap lifts P50 by 1–2.5% and rescued none of the 5 large-job unders (one went from 0.944 to 0.976, still under). Large-job unders come from weakly matched main items.
- **Local report.** `reports/p50_fix_round14.md`.

### Unit conversion studies U, U3, U4 (2026-11-20 22:41 to 2026-11-21 00:0x)

- **Question.** At about 22:25 the owner pointed out that takeoffs come in mixed units (earthwork, for example) and that conversion must be considered. The project's execution rule: convert exactly within one dimension only (SF or AC to SY, CF to CY, LB to TON); never between area and volume.
- **The problem, measured.** Rewriting the units of 188 municipal takeoffs changed P50 by more than 1% on 157 of them (more than 20% on 79) and left 624 lines unpriced. One sample of 4 lines priced at $335,687 written in CY/SY and $729,408 written in CF/SF.
- **Rule.** Five registered conditions: (I) the same takeoff prices the same in any unit; (a) floor; (b′) upper end of the overshoot difference at most +1.0 point; (c) accuracy not worse; (L) line error not worse. Data: snapshot 10, 188 municipal and 806 TxDOT lettings.

| Arm (switch) | Registered | What it does | Result | Verdict |
|---|---|---|---|---|
| U, `UNIT_POOL` | 11-20 23:04:40 | All history pooled into one unit per dimension | Invariance holds (both spellings $381,963). Municipal: overshoot 41.6% → 41.3% (−0.3, interval −2.3 to +2.3); line error $24.78M → $23.50M on 670 lines (interval −4.75 to +1.77). TxDOT: overshoot 31.3% → 32.4%; line error $340.45M → $408.27M (+$67.81M) | (I) and (a) pass; (b′) and (L) fail in both markets |
| U2 (post hoc, not registered) | n/a | Each family's most common unit as the standard | TxDOT line error from +67.8M down to +3.9M; (b′) upper end +1.02 | Still fails |
| U3, `UNIT_FALLBACK` | 11-20 23:23:06 | Convert only lines whose own unit has fewer than 5 earlier lettings | Lines left unpriced by a rewrite: municipal 624 → 42, TxDOT 566 → 0. Forward, only 25 municipal and 4 TxDOT lines were converted | Municipal fails (b′) and (L); TxDOT fails (c) and (L) |
| U4, `UNIT_THIN_UNPRICED` | 11-20 23:56:58 | Leave unpriced a line resting on fewer than 5 lettings in a unit that is rare for its family | Touched 15 municipal and 2 TxDOT lettings. Municipal overshoot −1.32 points (interval −2.04 to +1.23) | Municipal fails (b′) narrowly; TxDOT fails (c) by a hair |

- **Decision.** None is used for P50 or P80. The patches exist locally and were not applied. The worksheet shows a converted reference price as a hint for the estimator.
- **Lesson.** A rare unit often marks a different product: TxDOT seeding by the acre costs about 0.24× seeding by the SY, and TxDOT sidewalk by the SF runs 1.5× the SY price; 7 of 15 unit pairs failed a price-consistency check. Converting also moves the quantity band: after thousand-gallon watering was converted to gallons, 245 of 297 watering lines fell into the top band. Lines that U3 converted were often another product or a misfiled family (TxDOT full-depth repair by the SY priced from bridge-deck repair by the SF, about 5 times too high).
- **Local report.** `reports/unit_conversion_2026-11-20.md`.

### Park equipment as "not covered" (coverage scope v3, 2026-11-21 08:49)

- **Question.** Playground units, shade structures and dog-park gear are bought by model. In the bid history they spread 10–100× across jobs, and the model priced a $165,000 play unit at about $1,000. Should they be "not covered"?
- **What was tested.** One new coverage class for park and recreation equipment: 34 line types in 6 lettings. Registered 08:49:09. Parser .32 data, 224 municipal lettings.
- **Rule.** (a) floor; (b′) upper end of the overshoot difference at most +1.0 point; (c) accuracy not worse; (d) deducted dollars at most 2%.
- **Result.** Under rate 14.3% → 13.4%. Overshoot 46.1% → 46.3% (+0.26 point, interval −2.39 to +2.07): (b′) failed. TxDOT identical.
- **Decision.** Not adopted.
- **Lesson.** On two small park jobs, taking the equipment out left mostly lump sums. The model's mobilization there was about $140,000 against the low bidder's $25,000. The underpriced equipment had been hiding that. The idea waits for the small-job lump-sum problem.
- **Local report.** `reports/coverage_v3_2026-11-21.md`.

### Round 15: recency-weighted centring (CW12, CW24)

- **When.** Registered 2026-11-21 09:43:20. Parser .32 data. 224 municipal lettings.
- **Question.** Would weighting recent lettings more in the centring factor track the price level better?
- **What was tested.** CW12 and CW24 (half-lives of 12 and 24 months), each in own and B80 mode: 4 arms.
- **Rule.** Track T.
- **Result.** No arm passed. Own mode was B in effect (CW12/own −0.02 point; CW24/own +0.00). CW12/B80 moved overshoot from 46.1% to 47.2% (+1.09 points) and the under rate from 14.3% to 15.2%. P50 was not more accurate (mean abs-log +0.0013 and +0.0003, intervals cross 0).
- **Decision.** Not adopted.
- **Lesson.** Centring cancels in P80. P80 = P50 ÷ divisor, and the divisor is a quantile of earlier ratios centred by the same factor; a factor shared by all lettings drops out. Only owner-set allowance dollars leak through (165 of 397 municipal records carry them, median 4.3% of the estimate). The yearly median of P50 ÷ low bid was 0.857 (2021), 0.957, 0.923, 1.084 (2024), 1.011, 1.056 (2026): a step between 2023 and 2024, not a trend. Year means explain 0.2% of the variance. Centring every year exactly, after the fact, moved the matched-80% overshoot from 36.2% to 37.8%. No more rounds on centring, recency weights or segment levels.
- **Local report.** `reports/p50_fix_round15.md`.

### Round 16: bidder count above the discount table's top bin (C)

- **When.** Registered 2026-11-21 12:16:07 (addendum 12:19:19, correcting a mechanism check only). City-round-6 data. 224 municipal and 807 TxDOT common lettings.
- **Question.** The estimator refused to estimate when the bidder count was more than 2 bins beyond the nearest bin of the discount table. The published table reaches 14, so schedules with 17 or more bidders got no estimate. Forward tables are thinner (top bin 11–13 in 2021–2026), so 36 lettings were refused: 12 municipal with 15–21 bidders and 24 TxDOT from 2016–2020.
- **What was tested.** C: a bidder count above the top bin is priced off the top bin and flagged as extrapolated. Scored in own and B80 mode.
- **Rule.** Track T read as "not worse", on common lettings, in both markets: (b) overshoot at most B + 1.0 point; (d) upper end at most +1.0 point. New condition (e): newly scored lettings can only veto (3 or more in the window with more than half under).
- **Result.** C/B80: municipal overshoot −0.24 point (interval −0.96 to 0.00); under rate unchanged in every year; TxDOT unchanged. C/own failed: +1.33 points (interval −1.03 to +3.24); one newly scored 2023 letting was an under, the guard reacted, and the 2024 overshoot grew. 35 of the 36 refused lettings now get an estimate. The 10 municipal lettings in the window with 16–21 bidders: none under.
- **Decision.** C/B80 chosen; installed as part of round 18.
- **Lesson.** The newly scored high-count lettings run high (median P50 ÷ low bid 1.098). Once they enter the centring pool they lower the municipal centring factor (1.0551 → 1.0433), so every new municipal P50 drops by about 1.1%. That comes from those lettings being overestimated, not from better pricing.
- **Local report.** `reports/p50_fix_round16.md`.

### Round 17: abandonment identity (AB)

- **When.** Registered 2026-11-21 13:28:13 (addendum 13:31:17, correcting a mechanism check only). Same data as round 16.
- **Question.** At about 13:00 the owner pointed out that for pipes "remove" and "abandon" mean different things: one takes the pipe out, the other leaves it and plugs it, at lower cost. Abandonment lines were being priced from new-pipe history, and abandonment cells were dragging down new work.
- **What was tested.** AB: a line that only abandons (abandon wording, or plug / grout / flowable-fill wording on an existing line) carries a marker token; lines with different markers never match. AB2: AB plus a separate "#ab" family key. Each in own and B80 mode.
- **Rule.** Municipal: track T. TxDOT: "not worse" reading. Line track for the B80 arms.
- **Result.** Track T: all four arms failed municipal (d). Line track: AB/B80 passed. Line error sum $341.52M → $340.12M (−$1.39M, interval −$2.54M to −$0.42M) over 9,991 lines; overshoot −0.28 point (interval −0.79 to +0.22); under rate unchanged in every year. AB2 failed (L) (−$0.91M, upper end +$0.29M). The 97 flagged lines went from $1.584M to $1.262M of error. New-work lines that abandonment cells had dragged down: 130 repriced, $2.453M → $1.381M. A test line "abandon existing 8-inch sewer in place" went from $141.75/LF to $29.02/LF; the public median for abandonment up to 12 inches is $29.38/LF.
- **Decision.** AB/B80 chosen; installed as part of round 18.
- **Lesson.** Half of the gain was unexpected: new work stopped being priced from abandonment cells. Known defect: a 42-inch abandonment went from $65.11 to $23.50/LF against bids of $225. When one side states a size and the other does not, size is not a hard constraint. Side effect: the municipal centring factor falls by 0.56%.
- **Local report.** `reports/p50_fix_round17.md`.

### Adoption rule v2 (2026-11-21 14:22)

The owner changed what can go live: not only a more accurate total, but also, with everything else unchanged, more accurate unit prices, because estimators read and pick unit prices. At about 14:25 the owner added that a loss of 1% in total accuracy is acceptable for a gain of 5–10% or more in unit-price accuracy.

| Track | Passes when |
|---|---|
| T (total better) | (a)–(d) as in rounds 11–17. |
| U (unit prices better, nothing else worse) | (a) Municipal under rate not above B, overall and in every year: a hard floor. (L) Line error sum lower with the interval's upper end below 0, and the median line abs-log not higher. (U0) P50 error and overshoot not significantly worse (interval lower end at most 0), with point estimates at most +0.5 and +1.0 point. (c) TxDOT not worse. |
| U+ (unit prices much better; the total may give a little) | Line error sum lower by at least 5% (upper end below 0, median line abs-log not higher). P50 error and overshoot point estimates may each worsen by up to +1.0 point, with no significance test. (a) and (c) as in U. |

- The under rate (a) is a hard floor in every track.
- Reading of (a): in U and U+ it means "not above B", overall and in every year. It does not mean "at most 20%". This was written down after round 18 asked. The "consequences" section of the rule file still says the round 14 B80 arms qualify under U; that sentence was written before the clarification and is wrong under the final reading.
- Local file: `ADOPTION_RULE_2026-11-21.md` in the owner's working copy.

### Round 18: rounds 16 + 17 (+ 14) combined on the .33 data (X1, X2, X3)

- **When.** Registered 2026-11-21 14:58:40. Parser .33 data. 224 municipal and 807 TxDOT common lettings. Switched on at 15:20:15; rebuild finished 15:30:36; verified by 16:00.
- **Question.** Do rounds 16 and 17, and round 14's line fixes, hold together on the new data under adoption rule v2?
- **What was tested.** X1 = bidder-count clamp + abandonment identity. X2 = X1 + round 14's P_CWT. X3 = X1 + P_CW. All in B80 mode.
- **Rule.** Adoption rule v2. Among passing arms the largest line gain wins.
- **Result.** X1: under rate 14.29%, unchanged overall and in every year (11.3% / 14.8% / 16.7%); overshoot 45.78% → 45.54% (−0.25 point, interval −1.05 to −0.05); P50 error 20.20% → 19.88%; line error sum $338.40M → $337.01M (−$1.39M, −0.41%). TxDOT unchanged. X2 and X3 cut the line error sum by 11.6% and 6.1%, but their under rates rose to 15.62% and 15.18%, which breaks the hard floor.
- **Decision.** X1/B80 passed tracks T and U and went live at 15:20. B80 became the production default (`P80_FROM_B = True`). After the install production scored 234 municipal lettings: 13.68% / 45.25% / 19.67%. The 10 extra lettings have 16–21 bidders and none is an under. A prospective switch-off check is registered (section 8.2).
- **Lesson.** The capped-mobilization rule turned three small jobs into unders (on one, P80 ÷ low bid went from 1.166 to 0.725): fix one thing, expose another. Side effect of X1: P50 and P80 of a new municipal schedule fell by 1.44% (centring factor 1.059152 → 1.043934). Practical note: with the switches on, 12 self-tests that asserted "production = switches off" would have blocked the rebuild; a separate switch-on patch changed them.
- **Local report.** `reports/p50_fix_round18.md`.

### Round 19: per-job P80 margin (M_T, M_Z); small-job unit-price lift (S, SC)

- **When.** Registered 2026-11-21 16:23:49. 234 municipal lettings. B = production after round 18.
- **Question.** Two questions. Should the P80 margin vary by job? And do small jobs need their unit prices lifted, so that the capped-mobilization rule (C) can go in?
- **What was tested.** M_T: the P80 divisor by tercile of the job's thin-evidence dollar share. M_Z: the divisor by tercile of the model's own line total. S: a unit-price factor by size band, fitted forward. SC = S + C. MTSC and MZSC. C alone was report only.
- **Rule.** Adoption rule v2, with (a) read as "not above B, overall and in every year".
- **Result.** No arm passed.

| Arm | Under rate (lettings) | Median overshoot (difference, interval) | Line error sum | Fails |
|---|---|---|---|---|
| B | 13.68% (32) | 45.25% | $340.21M | |
| M_T | 13.25% (31) | 47.46% (+2.22; −2.37 to +3.88) | unchanged | (b), (d) |
| M_Z | 13.68% (32) | 44.22% (−1.02; −4.13 to +1.65) | unchanged | (d) only |
| S | 14.53% (34) | 45.04% (−0.20; −3.28 to +1.25) | $337.99M | (a), (L) |
| SC | 14.10% (33) | 45.32% (+0.07; −3.91 to +1.61) | $326.52M (−4.0%) | (a), (L), (U0) |
| C alone (report only) | 14.53% (34) | 45.38% (+0.13) | $328.88M (−3.3%) | (a) only |

- **Decision.** None. Production unchanged.
- **Lesson.** M_T pointed the wrong way: the jobs with the thinnest evidence had the lowest P50 error (16.9%), and those with the least thin evidence the highest (24.9%). S's premise does not hold in dollars: the fitted factors were mostly below 1 (small jobs 0.91–0.97). On small jobs 21% of the lines are under half of the bid median (together −$5.15M) while 24% are over 1.25× (together +$2.86M); the dollar-weighted centre is 0.986. The small-job shortfall comes from a few specialty lines matched to the wrong price history: a screening wall at 0.07×, a TxDOT-style retaining wall at 0.09×, solar crosswalk lights at 0.04×, pavement markings at 0.15–0.22×, an unpriced mow strip. No more rounds on banded P80 margins.
- **Local report.** `reports/p50_fix_round19.md`.

### Round 20: rules for specialty lines matched to the wrong price history (F, G)

- **When.** 2026-11-21. Started 16:36. Main registration recorded 17:05:58 and scored 17:28. Addendum recorded 17:32:44 and scored 17:58:50. Installed 18:12; verified 18:52. 234 municipal and 807 TxDOT common lettings.
- **Question.** Round 19 traced the small-job unders to a few specialty lines with the wrong price history. Can rules in the parser, the classifier and the price cells fix them, and does the capped-mobilization rule (C) then pass on top?
- **What was tested.** F: parser .34 with five rules, plus TF. The five rules: a new screening-wall family; flashing beacons, flashers, crosswalk lights, signal poles and mast arms to traffic signals; markings named by their line (stop bars, solid / broken / double yellow, raised markers, marked crosswalks) to pavement marking; signs named by an MUTCD code or a panel size to signing; walls no longer sized by their height. TF: a line with a clear family takes token cells only from its own family. F+C: F plus C. Addendum, written after the main results: G = only the three family rules for signals, markings and signs, estimator unchanged; G+C.
- **Rule.** Adoption rule v2. "TxDOT not worse" includes: under rate not above B in each of 2023–2026.
- **Result.**

| Arm | Municipal under (lettings) | By year 2024 / 2025 / 2026 | Median overshoot (difference, interval) | P50 error | Line error sum | Verdict |
|---|---|---|---|---|---|---|
| B | 13.68% (32) | 8 / 12 / 12 | 45.25% | 19.67% | $340.17M | |
| F | 12.82% (30) | 6 / 12 / 12 | 45.08% (−0.17; −2.71 to +3.73) | 19.72% | $338.59M (−0.46%; interval crosses 0) | Fail: neither total nor unit prices significantly better; more TxDOT unders in 2024 (27 → 30) |
| F+C | 12.82% (30) | 6 / 11 / 13 | 45.74% (+0.49; −3.35 to +3.46) | 19.82% | $327.18M (−3.82%; significant) | Fail: one more municipal under in 2026 (hard floor) |
| G | 13.68% (32) | 8 / 12 / 12 | 44.56% (−0.68; −1.24 to +0.51) | 19.43% | $339.37M (−$0.84M, −0.25%; interval −$1.80M to −$0.19M) | Pass, track U |
| G+C | 14.10% (33) | 7 / 13 / 13 | 44.91% (−0.34; −2.79 to +1.29) | 20.26% | $328.02M (−3.58%) | Fail: more unders in 2025 and 2026 |

  The line error sums of F and F+C are on 10,236 common lines and those of G and G+C on 10,245. TxDOT under G: under rate 15.49% (125 lettings; B 15.61%, 126), overshoot 31.46%, P50 error 16.10%: not worse.
- **Decision.** F and F+C failed. G passed track U under the addendum and was installed at 18:12: only the parser file changed (.33 → .34b), the estimator did not, and a full rebuild followed. Every guard passed. Five reference and calibration files matched the round's own run byte for byte. Production after the install: municipal 235 lettings, under rate 13.62%, overshoot 44.80%, P50 error 19.52%; TxDOT 807 lettings, 15.49% / 31.46% / 16.10%. Because the addendum was written after the main results, on the same data, G is post hoc and carries a prospective check (section 8.2).
- **What each rule did (report only).** The three family rules were right but moved little money: 8 beacon and signal lines, 28 marking lines and 13 sign lines; their line error went from $1.21M to $0.38M, and lines off by more than 4× from 25 to 2. The new screening-wall family had one line in the window and flipped it from 0.07× to 2.72×: the new cell was too thin. Not sizing walls by height made things worse: 13 of 15 repriced lines moved farther; the height-banded cells carried information. TF was flat on municipal and good on TxDOT. C again fixed mobilization (58 lines, $16.01M → $4.65M), but one small job still became an under: its retaining wall is bid by the SY, and the SY retaining-wall cell actually holds sidewalk-type items.
- **Incident.** At 18:00 the round's experiment run replaced the production parser file and started a full rebuild, against its instructions. It was stopped before it wrote `data/clean`. The .33 parser was restored from backup, and G was then installed by the INSTALL steps. Rule drawn from it: an experiment may only touch its own private directory; production is changed only by the install procedure.
- **Lesson.** (1) Taking a class out of "other" changes the price of every line left in "other", because the fallback cell is the median of all unrecognised lines. (2) Fixing a line exposes whatever it was offsetting: under F, an 8-inch water main installed by pipe bursting, which had been priced far above the bids from a new-pipe cell, was priced almost exactly from its own family; the job's comparables price was low and had been propped up by that one line, so P80 ÷ low bid fell from 1.434 to 0.938. (3) "Not higher in any year" is a very strict reading for TxDOT: TxDOT improved significantly overall under F (unders 126 → 123) and still failed because 2024 had more unders, two of them at the line (1.003 → 1.000). Decide before the next registration whether TxDOT uses this reading or the one of rounds 16–18 (each year at most max(20%, B + 3 points)).
- **After.** At 18:23 the owner decided to pause optimization when this round finished.
- **Local report.** `reports/p50_fix_round20.md`.

### Round 21 — Recent municipal project medians (R3-B80)

- **Question.** The owner set the local worksheet's starting unit price to the
  adjusted median of three comparable projects' bidder medians. Round 21
  tested that rule as a municipal model input on lines it could price, with
  production prices as the fallback. The candidate recomputed the P50 level
  from its own strictly earlier letting records and retained the original
  dated P80 divisor and guard. The registration fixed one candidate and all
  three adoption tracks in `docs/METHOD.md`, section 1.6.
- **Public benchmark.** Ten reviewed City/County base schedules qualified for
  the isolated check. The original model scored nine using the earlier frozen
  public corpus. One lacked enough earlier calibration records. On the nine,
  P80 fell below the actual low bid on one (11.11%). Conditional median
  overshoot was 60.90% and median P50 absolute error was 38.12%. The four
  complete matched scopes with printed engineer estimates had raw median
  absolute errors of 14.53% for the engineer and 110.79% for P50. Those four
  observations are a descriptive comparison. Matched not-under and prospective
  validation remain open. All new documents stayed out of training and
  calibration. Engineer estimates served only as a yardstick. The replay used
  public bids and kept the locked holdout sealed.
- **Replay checks.** All 2,252 original forward records and 2,217 original
  margin records reproduced exactly. The baseline-against-itself check
  returned zero changes across nine groups and 2,000 letting-level resamples
  per group. The candidate added or lost zero lettings. Both arms had 235
  municipal lettings in 2024–2026 and 807 TxDOT lettings in 2023–2026. In both
  arms, 120 covered municipal lines and 32 covered TxDOT lines lacked prices,
  and the coverage rules excluded 24 municipal and three TxDOT lines.
- **Municipal result.** P80 unders fell from 32 to 30 (13.62% to 12.77%).
  By year they moved from 8 to 7 of 71 in 2024, stayed at 12 of 83 in 2025,
  and moved from 12 to 11 of 81 in 2026. Median P50 absolute error moved
  from 19.52% to 19.43%. Conditional median P80 overshoot rose from 44.80%
  to 46.54%, a change of +1.74 percentage points with a paired 90% interval
  of −0.38 to +5.00 points.
- **Unit prices.** The candidate replaced 1,549 prices on 161 municipal
  lettings in the registered window. On all 9,598 lines with exact paired
  item outcomes, the aggregate absolute amount error rose 3.23%, from
  $307.81M to $317.75M. The increase was $9.95M with a paired 90% interval
  of +$5.14M to +$15.20M. Median absolute log error rose from 0.3708 to
  0.3743. The paired line set removes ambiguous item mappings and requires
  three distinct bidders for the item median.
- **Decision.** T failed on overshoot. U and U+ failed on unit prices. The
  P80 under-rate conditions passed. TxDOT records and line traces were
  identical, with P80 under on 125 of 807 (15.49%). Keep the original model
  and calibration. Keep the independent local worksheet policy. No bundle
  or public deploy changed.
- **Rerun.** The first candidate aborted before paired scoring. It assigned
  constants to seven early TxDOT lettings for which the original P80 margin
  had too little history. Amendment 2 bound score availability to the
  original margin records while preserving the candidate's own strictly
  earlier P50 level and every invariant check. Twenty mechanical tests
  passed. The amended baseline's first run stopped at the final fingerprint
  check when a reporting script was added inside the fingerprinted directory.
  The script was moved out of that directory. The frozen fingerprints
  matched exactly and the full baseline, unseen benchmark, candidate and
  report were rerun in a fresh output directory. Earlier evidence and
  logs were retained. The source data, candidate and decision rules stayed
  fixed throughout the retries.
- **Local evidence.** `handoff/scratchpad/round21_recent_medians_2026-11-22/`
  holds the registrations and first-attempt evidence. Under its
  `run-amendment2-retry/` directory, `report.json` contains the paired scores
  and decisions, `unseen-checkpoint.json` contains the separate public
  benchmark, and `results-audit.json` records the independent reconciliation
  of coverage counts, scored prices and report metrics.

### Round 22 — The most recent comparable pay item

On 2026-11-22 the owner asked to test the engineering worksheet practice of
using the latest comparable pay item and rounding its bidder median **upward
to two significant figures**. A $312 price becomes $320. The test first sought
one exact item from the prior twelve months. If unavailable, it sought up to
three comparable projects from the prior two years. Source prices required
the same unit and comparable specifications, a quantity within a factor of
two, three distinct real bidders, and dates strictly before opening. Prices
were adjusted using the existing prior-date municipal bid-price index.

- Of 235 historical municipal lettings, 77 had precise source dates matching
  the forward replay. Of 3,134 rows in these 77, 236 received prices and 233
  could be compared to a unique item with a known bidder median.
- On the 110 recent exact matches, signed median error was **+0.75%** and
  median absolute error **20.49%**. The existing unit-price method had
  **19.79%** median absolute error on those same rows. On the 123 secondary
  matches, signed median error was **+10.13%** and median absolute error
  **28.00%**, compared with **26.23%** for the existing method. Of these 123,
  102 had only one qualifying project.
- On all 233 pairs the sum of absolute extended errors fell from $2.211M to
  $1.987M. A descriptive 90% letting-cluster bootstrap interval for the change
  ranged from a $785k reduction to a $301k increase. Keeping the same
  references, rounding itself added $81k of absolute extended error across
  these rows. The secondary tier already had a signed median error of +8.17%
  before rounding.
- No complete historical worksheet could be priced solely from matches. On
  13 verifiable full baskets with at least one substitution, filling other
  lines with existing pre-discount prices gave a median absolute error of
  **28.47%** against the whole low bid, compared with **32.49%** using existing
  prices on all lines. Two of the 13 mixed estimates fell below the low bid.
  Historical fixed amounts derived from a bidder's price were excluded.
- Separately, 10 reviewed statewide schedules contained 1,064 items. On 39
  recent exact matches the signed median error was **+12.76%**, with **30.43%**
  median absolute error. Across both tiers 42 matched items accounted for only
  1.87% of the combined low-bid dollars. No purely matched full estimate was
  available. One full mixed estimate contained a substitution, too few to
  establish full-project performance statewide.

The results support testing recent exact items as one source of price evidence.
They do not establish the error of an entire worksheet made only of latest
prices. The existing worksheet and production model continued in use. Local
registration, paired records, source references and attribution arithmetic are
in `handoff/scratchpad/round22_latest_pay_item/`.

### Round 23 — Recent exact bid evidence in the municipal estimate

The candidate uses the geometric mean of the existing market unit price and
the most recent qualifying exact item's price. The latter is the median of
at least three bidders on one strictly earlier project, adjusted for price
changes and rounded upward to two significant figures. It requires the same
unit and specifications, comparable quantity and an opening within a year.

Before candidate scores were inspected, the owner excluded lump sums from
all unit-price comparisons. The baseline and candidate used the same 8,700
eligible municipal pay items after 898 LS items were removed. Full-project
totals included the original scope. An independent check found zero LS lines
among the 233 historical and 42 statewide scored pairs in Round 22.

- On the 110 comparable items in 32 projects to which the new rule applied,
  median absolute unit-price error fell from 19.79% to 16.31%. Across all
  8,700 items it moved from 32.65% to 32.64%. The sum of quantity times
  absolute unit-price error fell by $135,821 (0.053%). The paired 90% interval
  from 2,000 project-level resamples was a reduction of $17,078 to $277,647.
- Across 235 municipal projects the median absolute P50 error was 19.52%
  with either rule on the registered scope. P80 equaled or exceeded the
  comparison bid on 203 of 235 projects in both runs. Fifteen comparison
  bids covered only the in-scope portion of the contract, and 64 projects
  had at least one unpriced in-scope line. These two groups overlap. On the
  162 full contracts with every line priced, P80 met or exceeded the full low
  bid on 145 projects with either rule. Of those contracts, 102 also had no
  retrospectively identified fixed amount. Both rules covered 89 of those
  102. The historical totals use the actual bidder count. Independent trials
  below used five bidders specified before opening the target prices.
  On the registered scope, when P80 met or exceeded the comparison bid, its median
  overshoot went from 44.80% to 44.55%. All 807 TxDOT comparisons were
  identical.
- The candidate passed the registered unit-price improvement track (U) in
  development. Independent confirmation is pending. The report and the
  pre-score amendment are in
  `handoff/scratchpad/round23_latest_exact_blend/`, under
  `physical-results/report.json` and `OWNER_SCOPE_AMENDMENT.json`.

Two independently sealed batches from five additional agencies then tested
the same frozen rule. Of 18 source documents, five furnished a complete,
verified bid schedule for this exercise. They contained 67 items, of which
18 were lump sums and two had no comparable model price. On the 47 paired
non-LS items the median absolute unit-price error was 48.93% for both arms.
Only two items qualified for the recent exact blend. On those two items its
median absolute error was 56.58%, compared with 54.38% using the original
price. The sum of absolute errors times quantity increased by $78.48.
Only two projects had prices for every line. No project in the batch was
eligible for the registered post-2026-11-20 confirmation, and none printed
an engineer estimate. The small independent comparison did not establish
an improvement. Evidence is in
`handoff/scratchpad/fresh_city_county_validation/delivery18-results.json`.

### Round 24 — Two older exact pay items when the past year has none

When no recent exact reference qualified, the candidate took the two most
recent eligible projects with the exact item 12 to 24 months before opening.
It took the median of their separately inflation-adjusted bidder medians,
rounded it upward to two significant figures, and blended it with the
existing price in the same way as Round 23. The schedule, unit, quantity and
three-bidder checks stayed the same. Before scoring, 14 additional lines in
eight historical projects qualified.

The median absolute unit-price error on those 14 lines fell from 43.75% to
32.52%. Eleven lines improved, reducing their combined absolute extended
error by $13,534. Three worsened by a combined $28,104. The largest was a
2,466 SY flexible-base line. Its bid median was $37.20/SY. The older
references moved its price from $38.63 to $44.39/SY and added $14,197 of
error. Across all 14 lines, the increase was $14,570. The paired 90% interval
for the change across all 8,700 non-LS lines went from a reduction of $12,151
to an increase of $45,623. The registered incremental unit-price gate failed.
The extension was not adopted.

The P80 coverage count and median absolute P50 error on the registered
235-project scope stayed at 203/235 and 19.52%. The separate P70 diagnostic
used each arm's empirical 30th percentile of price ratios from strictly
earlier openings. On the 162 complete contracts, incumbent P70 covered
134 (82.72%) and Round 23 and Round 24 each covered 133 (82.10%). P80
covered 145 (89.51%) in all three. On that complete scope the incumbent
P70 and P80 had conditional median overshoots of 32.24% and 44.33%.
These comparisons used actual historical bidder counts. The additional
five-project check used five bidders specified before scoring. Only two
complete scopes were priced there, so it cannot establish P70 or P80
coverage. The experiment and the P70 calculations are filed under
`handoff/scratchpad/round24_older_exact/` and
`handoff/scratchpad/p70_recent_item_analysis/`.

### Round 25 — Source evidence for dates and comparable scope

The date audit verified nine Forney and Highland Park openings against official
documents and archived evidence that each document was available by a given
date. Two opening dates changed. Seven existing dates were confirmed. Forney
CIP 283 opened on May 7, 2026. Its May 20 letter recommended an award, and the
reviewed council packet was publicly available by May 27. The three dates
serve different purposes.

Three of the nine had ordinary competitive base bids for scope review. Four
were best-value cases and the other two had only one or two bidders. On 294
non-LS target lines in the three ordinary cases, the initial source-verified
reference pool supplied zero eligible recent exact treatments at the verified
opening dates. The larger historical pool, using its existing availability
assumptions, identified 11 potential matches. Ten depend on the publication
history of four specific bid tabs. The remaining cross-year match lacks an
independently supported inflation adjustment. No error or coverage result
was calculated from these leads in the first pass. A second source audit
established that the North Richland Hills and Farmers Branch bid tabs were
already public before the Wycliffe Avenue opening. Repeating the strictly
dated search recovered six recent exact references on that one project.
Frisco's observed publication evidence fell after the target date and the
Richardson source had not been fetched. These six references alone cannot
calibrate a project P50, P70 or P80. The two source packets are in
`data/processed/historical_bid_dates_v1/` and
`data/processed/historical_bid_dates_v2/`.

A separate retrospective unit check sealed six worksheet prices before
reading their target bid columns. All six historical medians were verified
against the original bid tabs, rounded upward to two significant figures
and compared with the median of the three responsive bids on the Wycliffe
base item. The median absolute percentage error was 44.20%. Five prices were
low. Project signs were close ($760 versus $750 each). Monthly barricades,
signs and traffic handling were $3,600 versus $56,150 per month. This one
line accounted for $1,103,550 of the $1,120,479 in absolute extended error
on the six items. The pay unit for this service was MO. It remains in the
non-LS comparison. These are six items on one historical job. See the
original-tab checks and sealed predictions in
`handoff/scratchpad/round25_verified_opening/wycliffe_six/`.

### Round 26 — Age and quantity as weights for a recent exact price

The owner asked that a newer bid at a more comparable quantity carry more
weight. Round 26 tested one rule against Round 23. The recent price's weight
was the average of its age similarity and quantity similarity. Age similarity
decreased from one to zero across the preceding year. Quantity similarity was
one at equal quantities and zero if one quantity was twice the other. The
input items, prices, scopes, dates and rounding were otherwise identical.

The 110 matched non-LS prices had median absolute unit-price error of 20.39%
under the variable weight, compared with 16.31% at the fixed equal weight.
The quantity-weighted absolute dollar error increased by $37,024 relative to
Round 23 (paired 90% interval, $5,256 to $69,991). The increase included
$18,731 on the 59 lines receiving more weight and $18,293 on the 51 receiving
less. Age and quantity alone did not improve the choice of weight. The arm
failed its registered line gate, so the project replay was not run. The
registration and report are in `handoff/scratchpad/round26_recency_quantity_weight/`.

### Round 27 — Apply the cross-year bid index in proportion to elapsed time

The existing matched-bid index compares prices by calendar year and uses
only bids already available by the target opening. This experiment applied
the cross-year factor as an annual growth rate over the actual time between
the two openings. This is a hypothesis because the measured index compares
two sets of bids and the target year's set is incomplete. The exact reference,
upward rounding rule and equal-weight blend were the same as in Round 23.

There were 36 cross-year lines in 14 historical projects, with source ages
of 149 to 349 days. After rounding, prices changed on 20 lines in five
projects. Median absolute error on those 20 fell from 11.16% to 9.53%, while
aggregate absolute extended error increased by $2,645 relative to Round 23.
The 90% project-level interval ranged from a $2,164 reduction to a $10,656
increase. The incremental line gate failed. The registered full-project
stage was therefore skipped. No cases measured a reference just a few days
across New Year. Evidence is in
`handoff/scratchpad/round27_elapsed_inflation/`.

**Independent Laredo schedules.** In the two sealed schedules, seven of
45 base items had lump-sum units and two vertical-foot items did not pass the
original unit check. Both arms supplied the same unit prices on the 36
matched physical items, with median absolute error of 67.78%. The first
schedule had 33 comparable items with a printed engineer unit price. Its
median absolute error against actual bidder medians was 42.22% for the
engineer and 67.98% for the model. No recent exact source changed a price.
This is one project's engineer comparison.

The source-data correction later mapped two “Vertical Foot” entries to VF.
In a separately labelled post-disclosure replay, the 9-item Laredo manhole
base then had a model price on every line. The frozen incumbent P70 was
$4.86M and the verified low base bid was $1.79M, before the separately
printed five-percent addition. For the applied liner the estimate came from
a broader manhole/VF price group. For removal of existing coating it came
from a typical schedule-line amount. Neither price had a recent exact
public reference. The base total illustrates the importance of reporting
what supports a price even when every line has a value. Original and
corrected assessments are retained separately in
`handoff/scratchpad/fresh_city_county_validation/collection230710/` and
`collection230710_vf/` beneath the same parent.

**Historical engineer unit-price yardstick.** The 8,700 audited municipal
non-LS pairs were joined to explicitly flagged engineer rows by project, item
number, normalized description, unit and quantity. There were 2,096 unique
positive engineer matches across 32 projects. Each arm was compared to the
same item's observed bidder median. Median absolute percentage errors were
33.51% for engineers, 32.93% for the incumbent and 32.87% for Round 23.
Quantity times absolute unit error summed to $40.54M, $48.59M and $48.57M
respectively. Engineers had a lower sum than Round 23 on 25 of 32 projects.
Within each project's paired sample, the largest fifth of lines by original
estimated value accounted for $5.63M of the $8.03M difference. Only 21 lines
had both an engineer estimate and a Round 23 recent exact substitution.
These are unit-price comparisons. Absolute errors cannot cancel here as
overestimates and underestimates can in a contract total. Registration and
per-item evidence are in `handoff/scratchpad/municipal_engineer_unit_comparison/`.

**Additional independent pay items on held municipal schedules.** Five
previously held Texas bid tabs were registered for a separate unit check.
Source review identified 475 eligible non-LS items on three projects.
There were positive model and bidder-median prices on 474, covering Baytown
(330), Helotes (63) and League City (81). Both models had a median absolute
unit-price error of 42.45% on these rows. Only five Baytown rows used a
recent exact reference. Their aggregate absolute extended error fell by
$3,173, and their median absolute percentage error fell from 35.38% to
34.37%. On League City's 81 items with comparable printed engineer estimates,
the engineer's median absolute error was 16.67% versus 42.42% for the model.
Pricing used the reviewed subset of each schedule, so these comparisons
measure unit accuracy only. All five whole contracts remain held. See
`handoff/scratchpad/fresh_city_county_validation/held_statewide_items/`.

In Baytown the decrease in extended error on ductile iron fittings alone was
$4,982. The other four substitutions together increased error by $1,809.
A retrospective direct-price comparison on the same 474 rows found five
exact matches in the preceding year. On those five, the unblended, rounded
recent bidder median had a median absolute error of 35.00%, compared with
35.38% for the original market price and 34.37% for the blend. Ten more had
an exact source only in the second year. Their direct-price error was 41.27%
versus 27.85% for the original price. A further seven had similar wording
only. In total, 22 rows could be priced by the historical worksheet rule,
including 20 based on a single matching project. On those 22 its median
absolute error was 39.32% versus 31.71% for the original model. Filling the
other 452 rows with the original model added $52,393 in absolute extended
error across the 474 items. These were already disclosed outcomes.

### Round 28 — Agreement among three previously selected projects

This test screened the three references the existing worksheet had already
selected. Each had to support the same measured pay item within a factor of
two in quantity and have at least three distinct bidders. Their three
inflation-adjusted interquartile price ranges had to overlap. Of 439 recorded
three-project matches on the verified-date historical cohort, 64 already had
a Round 23 exact match. Among the remaining 375, seven lacked enough bidders
in a reference, 324 had at least one out-of-range quantity and 43 lacked
sufficiently clear scope or specifications. One line met the rule. Its
absolute extended error increased by $3.25. The incremental gate failed.
This count describes the original three selections. It does not establish
how many suitable references exist in the full source pool. Evidence is in
`handoff/scratchpad/round28_three_project_iqr/`.

### Round 29 — Qualify source projects before ranking them

Round 28 could discard a line when one of the first three references failed
a scope or quantity check even though another qualifying source existed.
Round 29 applied the same bidder, date, unit, specification and quantity
checks to the full two-year source pool before selecting three distinct
projects. It then checked whether their bidder price ranges overlapped.
Eight new lines had three qualifying projects. Six lines across five projects
also passed the range check. Five of those lines were missed by Round 28.

Compared with Round 23, the six substitutions reduced aggregate absolute
extended error by $4,315, with a project-paired 90% interval spanning a
$12,339 reduction to a $1,576 increase. That interval failed the registered
improvement condition. The experiment ended at the unit-price stage. The
registration, source selection and results are in
`handoff/scratchpad/round29_rank_qualified_projects/`.

### Round 30 — Check the most recent bidders' price range before substituting

With the same 110 recent exact matches and the same two-significant-figure
rounding as Round 23, use the recent median in the equal-weight blend only
if the original market price is outside the middle 50% of actual prices
on that historical item. The original market price fell inside the range
on 56 items, below it on 33 and above it on 21. All historical item medians,
bidder counts, units and dates were reproduced. Median absolute unit error
on the 110 rose from 16.31% for the fixed blend to 18.41% for the range
rule. Absolute extended error increased by $14,567 compared with the blend
(90% paired interval from an $11,379 reduction to a $46,757 increase).
The proposed rule failed its incremental line gate, and its project totals
were not scored. Evidence is in
`handoff/scratchpad/round30_recent_bidder_range/`.

### Round 31 — Separate finished concrete pavement from flexible base

The concrete pavement on Frisco's King Road was priced using two earlier
flexible-base bids. Round 31 excluded a candidate price group only when
the target was clearly a finished surface and every source in that group
was positively identified as incompatible. It preserved ambiguous or
combined work and used the estimator's normal fallback. Three non-LS
prices changed across the 235 development projects, with no lost prices.
Frisco's 29,271 SY line moved from $21.77 to $77.98/SY against a bidder
median of $84.81/SY. Its absolute extended error fell by $1,645,329.
The other two changes increased that error by $49,266 in total. Against
Round 23, the net decrease of $1,596,063 had a paired 90% interval from a
$4,850,375 reduction to a $92,072 increase. The incremental gate failed.
The full-project replay did not follow. Inputs, source checks and results
are in `handoff/scratchpad/round31_pavement_material/`.

### Round 32 — Carry the nominal water-pipe diameter into reference selection

The original reference for Grand Prairie's 24-inch PVC line came from two
8-inch pipe items. This experiment required an explicit target diameter on
a water-pipe LF item, checked the source descriptions, and searched for a
same-diameter replacement when every contributor to the selected price group
had a conflicting diameter. The check distinguished nominal carrier size from
depth, length, pressure class and the dimensions of an assembly. Twenty-nine
lines across 20 projects had confirmed conflicts. Twenty-six replacement
prices across 17 projects had a comparable bidder median for scoring.

On the 26, median absolute unit-price error fell from 37.78% to 32.68%.
On the 4,520 LF discovery line the price moved from $109.34 to $334.60/LF
against $433/LF. Aggregate absolute extended error on all 8,700 lines fell
by $143,188 versus Round 23. Its project-paired 90% interval extended from
a $2.457M reduction to a $1.872M increase. Excluding the entire discovery
letting, the error increased by $874,992. The candidate failed the unit
gate, so it was not scored on contract totals. Evidence is in
`handoff/scratchpad/round32_water_pipe_diameter/`.

### Round 33 — Recent pipe bids with the same material and construction method

Of the 29 same-diameter groups selected in Round 32, 13 included a known
different material and 23 included a quantity outside a factor of two.
Conservative date bounds showed that at least 25 contained a source more
than two years old and eight consisted entirely of such sources. These are
overlapping counts.
The two largest increases in absolute extended error were $545,142 and
$398,195. In both cases the target was PVC and the replacement group also
included ductile iron. With a fixed requirement for the same explicit pipe
diameter, material and installation method, an opening within 24 months and
a comparable quantity, one job supplied a replacement. Rockwall's 16-inch
open-cut PVC item had a five-bidder source 21 days earlier at 1.42 times
the target quantity. Its blended estimate moved from $133.13 to $163.18/LF,
against a $206.25 bidder median. The absolute extended error decreased by
$12,318. The incremental paired 90% interval was a $36,954 reduction to no
change. Its upper bound did not pass the registered gate. This one job
does not establish an improvement on other lettings. The case evidence and
registered results are in `handoff/scratchpad/round33_water_pipe_specs/`.

The worksheet's default price follows a separate three-project rule. Replaying
the 29 water-pipe cases and the three pavement substitutions through the local
worksheet reference API, both with automatic and confirmed item types (64
line results), reproduced every frozen selection and status. All 32 defaults
were blank. Twenty-four had no reference, six had one and two had two.
All ten partial references were within two years; seven differed in quantity
by more than a factor of two. A partial suggestion did not populate a
worksheet price. See `handoff/scratchpad/worksheet_product_reference_audit/`.

### Round 34 — Traffic-control work per month

The known Wycliffe monthly traffic-control error prompted a test on other
municipal schedules. Earlier bids had to cover the same traffic-control
service and the same kinds of paving and pipe work. The trial compared
planned paving area or pipe length per month, using the corresponding
physical unit for each type of work. Lump sums did not enter the test.
Of 42 monthly traffic-control items, one belonged to the discovery project,
30 lacked a precise archived opening date and seven lacked an unambiguous
scope. Four could be checked. Two had an earlier bid with the same printed
service, but the projects involved different types of work. None met all
conditions. The trial reproduced the Round 23 prices on 8,447 non-lump-sum
items in 234 other projects. No performance gain could be established.
See `handoff/scratchpad/round34_monthly_traffic_scope/`.

### Round 35 — Prefer a verified recent bid from the same public owner

This trial selected the latest recent and exactly matching item from the
same confirmed purchasing authority when available. It kept Round 23 if
no such independent procurement could be identified. Of the 110 original
recent substitutions, 51 had evidence of distinct procurements from the
same owner. Round 23 had already chosen every one. All 8,700 non-LS prices
matched the comparator. Refining the mapping of the Legistar platform to
its individual municipalities changed no selection. No whole-project replay
or promotion followed. See `handoff/scratchpad/round35_local_owner/`.

### Round 36 — Match the specified asphalt surface treatment

Burnet County's chip seal was estimated at $20.93/SY against a bidder
median of $3.25/SY. Its 35 reference projects included 34 concrete
surface treatments and one high-friction treatment. No reference was chip
seal. Three distinct Calhoun County treatments shared one $25.19/SY
estimate built from 13 concrete surface treatments. This prompted a trial
requiring the same printed application, binder or aggregate specification,
payment unit, similar quantity and a verified earlier opening date. Of 22
historical municipal treatment items, 19 lacked sufficient specification
and the other three lacked verified openings. All 8,700 eligible non-LS
unit prices reproduced Round 23. The two counties identified the problem.
They provide no independent confirmation of a replacement price. See
`handoff/scratchpad/round36_surface_treatment/`.

**Chronology of the external recent-price comparisons.** The review traced
71 source selections on 68 previously seen non-LS items. For 53 items across
nine projects, each selected original price table was already public when
the target opened. Median absolute unit errors were 18.48% for the incumbent,
18.42% for Round 23 and 33.86% for the direct recent price. The direct
method reduced absolute quantity-weighted dollar error by $80,975 against
Round 23. Only three projects improved. Removing the Fort Bend project
reversed that difference to a $160,410 increase. In the nested 14-item
group where the original opening and required time window were also verified,
the three median errors were 25.71%, 26.82% and 36.40%. Six of those items
were in the same year and required no escalation. The full-period historical
quantity calibration still limits this comparison. The item evidence and
all three nested samples are in
`handoff/scratchpad/external_publication_verified_units/cut2/RESULTS.md`.

The published-source subset also isolates the owner's upward rounding rule.
Keeping the original donors and inflation adjustment, direct-price median
absolute error was 34.38% before rounding and 33.86% afterward. Across the
14 items with verified dates and windows, upward rounding raised the value
of the basket by $25,350 but changed the sum of absolute errors by $9.
These are rounding effects on specific physical items. See
`handoff/scratchpad/external_direct_rounding/RESULT.md`.

**Estimating across a calendar-year boundary.** A separately registered
trial projected prices to a January opening from the preceding November 22,
using only comparisons between two earlier twelve-month periods. Of 475
eligible physical items opening in the first 90 days of 2024–26, 22 had a
recent exact reference using recorded openings as a proxy for availability.
Keeping that last price and projecting the trend both had a 23.54% median
absolute unit error. Projecting raised summed absolute extended error by
$1,340. The January 1–15 subset covered one project. No pairs qualified
under the stricter requirement for prior publication of the actual dated
source. All 14 physical items at the future Van Zandt dam lacked an eligible
direct reference even before allowing for inflation. The trial supplied
no basis to change January prices. See
`handoff/scratchpad/future_year_price_trial/RESULT.md`.

**Engineer totals on the same bid scope.** Of 235 historical municipal
records, two met all registered checks for a complete priced scope, a
precise date, a fully reconciled contractor basket, no retrospective fixed
amount and a matching engineer price for every line. The printed base
totals were then independently checked in the original municipal documents.
At Farmers Branch the lowest of six base bids was $211,103.50, and the
engineer's base was $259,558.63. The $15,000 alternate was separate in both
columns. In Kennedale the low bid was $642,878.50 and the engineer's estimate
before contingency was $771,110.00. The city received seven bids. Its tab
named six firms and labelled the seventh populated column “Contractor 7”.
The frozen model backtest used six named bidders for that case. These two
contracts do not establish an engineer comparison at a common not-under
rate. The protocol, exact scope and printed-source receipts are in
`handoff/scratchpad/municipal_engineer_project_comparison/` and
`data/processed/municipal_engineer_project_sources_v1/`.

Both schedules were replayed with a pre-opening assumption of five bidders.
Before changing that assumption, the two frozen models reproduced all 12
archived P50/P70/P80 figures across the two jobs to the cent at six bidders.
With five bidders, original and Round 23 P70 were $760,427 and $757,402
in Kennedale, and $344,322 and $356,820 in Farmers Branch, respectively.
The latter forecast went farther above the actual base bid when recent
prices were blended. At Kennedale's actual seven bidders the corresponding
P70 values were $758,771 and $755,753. These are historical scenarios on
verified base bid scopes. See the five-bidder results beneath the project
comparison directory.

Official documents supplied four more opening dates for reviewing earlier
whole-project comparisons. Only Frisco's Meadow Hill Drive had a common
complete, ordinary base. Its 66 items and seven submitted bids reconciled
to the $1,958,894.31 low; the engineer estimated $2,610,431. Replaying
the two models with five bidders assumed, on the verified April 9 date,
gave P70 of $2,326,685 and $2,326,831. The historical corpus convention
supplied the strictly earlier calibration dates; individual publication
times could not be proven for the full calibration pool. See
`data/processed/engineer_source_four/RESULT.md`.

**New historical Weatherford unit test.** Frozen predictions on 36
eligible non-LS items from the Franklin water project had median absolute
unit-price error of 30.28% for both arms. Only the fire hydrant assembly
changed. The target had 33 assemblies, with a ten-bidder median of $7,225
each. The original price was $6,552.43 and the Round 23 blend was $6,820.72.
A Justin bid tab for 31 assemblies was independently located in a council
packet published August 14, 2025, before Weatherford opened on February 3,
2026. Its 12 named contractors' unit prices and extensions were reconciled
to the original two pages. Their median was $6,650.53. The historical
matched-bid price index multiplier was 1.052665, yielding $7,000.78.
Rounding up to two significant figures gave a direct worksheet reference
of $7,100, 1.73% below the target median. The blended estimate was 5.60%
low and the original estimate 9.31% low. The construction allowance and
unresolved printed quantity prevented a whole-contract accuracy result.
Details are in `handoff/scratchpad/fresh_city_county_validation/weatherford_franklin/`
and `data/processed/weatherford_prior_source_dates/`.

**Recent bids across the external public projects.** A retrospective comparison
of 21 distinct reviewed projects found 1,546 eligible non-lump-sum items on
19 jobs. Each target was the median of at least three original contractors.
Only 68 items on 11 jobs had a qualified recent reference. The original
model, the Round 23 blend and direct use of the rounded historical price
had median absolute errors of 25.19%, 20.53% and 32.92% on those same
68 items. For the 47 with exact matches in the preceding year the figures
were 25.03%, 20.29% and 31.67%. The other 1,478 physical items had no
qualifying direct price. Ten projects had their Round 23 forecasts
reconstructed after outcomes were known. This is not a prospective
confirmation or a comparison of whole OPCCs.

Individual large quantities explain why the comparison by dollars gives
a different answer. On the Williamson County Ronald Reagan project,
1,224 LF of 36-inch Class III concrete pipe had a five-bidder median of
$195/LF. The Frisco comparison 110 days earlier had 1,143 LF and five
bidder prices with a median of $181/LF. The rounded direct price was
$190/LF, the blend $242.45/LF and the original $309.39/LF. The blended
rate reduced absolute line error by $81,930. Across all 68 items direct
pricing saved $56,293 relative to the blend in summed absolute extended
error. Leaving out one project at a time changed that difference from a
$157,317 reduction to a $184,881 increase. Source row identifiers, all
project contributions and exclusions are recorded in
`handoff/scratchpad/external_recent_price_pool/`.

**Whole base bids on the external projects.** Of the 21 reviewed cases,
four met the complete prebid input, full model coverage and matching
printed base requirements. All 14 LS lines counted toward the four
totals. All four P50, P70 and P80 forecasts exceeded their respective
low bids under both models. The median P70 overshoot was 253.3% for the
original and 253.2% for the blend. Only Williamson County had a matching
printed engineer total. The four cases do not establish a common-risk
engineer benchmark. See `handoff/scratchpad/external_project_totals/`.

These historical reconstructions use the engine's published quantity-band
multipliers, fitted across the full development period. The Round 23
historical calibration records share that limitation. On Frisco's Edgewood
streambank job, removing this table lowered the recent-blend P70 by
$3,919.60 under the five-bidder assumption. The original two-bidder tab
cannot supply an item-level benchmark requiring three contractors.

The largest source of error on Williamson's Ronald Reagan Boulevard job
was the payment unit for video-detection cable. Item 238 printed EA for
13,503 units of VIVDS cabling. The five-bidder median was $3.20. The
model took $10,619.51 from the general traffic-signal EA group. TxDOT
Special Specification 6306 measures the communications cable by the foot.
In a historical sensitivity assuming the engineer confirms LF for this
line, all other 304 line traces stayed identical, and the recent-blend
P70 fell from $215.27 million to $59.95 million against a $52.16 million
actual base low. The original scored record retains the printed unit. A
local intake and result warning asks the user to verify the contract unit
before using the EA price. See `handoff/scratchpad/vivds_unit_resolution/`.

### Round 37 — Blend the medians of three recent projects

The direct comparison first located 24 non-LS items across 17 municipal
projects with three eligible historical references. Across those items,
using three medians instead of the latest alone lowered the median absolute
unit error from 31.45% to 22.98% and absolute extended error by $9,004.
Direct use of three nevertheless cost $19,300 more in absolute error than
Round 23. An independent reconstruction of all contractor columns on one
source showed that two of the 24 relied on an incomplete table. Omitting
both left 22 items and the same $9,004 saving over the latest alone.

In a project replay with quantity factors computed at each opening and
strictly earlier calibration, P80 covered 203 of 235 historical comparisons
with either direct-three or Round 23. Direct-three increased the summed
absolute project errors by $21,507 at P50, $27,182 at P70 and $29,200 at
P80 relative to Round 23. Full and partially covered bases are identified
in the report. Only two original engineer totals could be linked for a
project yardstick. These data cannot verify performance against engineers
at the same coverage.

Round 37 registered a geometric blend of the incumbent and the
three-project price for the remaining 22 physical items. All other items
kept Round 23. Across 8,700 physical items, median absolute unit error
was 32.58%, compared with 32.64% for Round 23. The new blend saved
$3,261 in absolute extended error. The project-level 90% interval ranged
from a $17,297 saving to an $11,564 increase. That missed the registered
improvement threshold. See `handoff/scratchpad/round37_three_project_blend/`
and `handoff/scratchpad/three_versus_one_projects/`.

Across 1,546 separately reviewed external physical items only one had
three qualifying references under the historical opening-date proxy.
On that Baytown trench-safety item a missing horizontal continuation in
the Mansfield historical panel changed the corrected three-source forecast
from $2.00 to $1.40/LF. The actual three-source history involved different
utility scopes. The target median was $2.00/LF, and the latest-only
prediction was $1.10/LF. See `handoff/scratchpad/three_versus_one_external/RESULT.md`.

An additional historical external case, Frisco's FY2026 erosion contract,
had all 114 bidder extensions and three complete base bids reconciled.
The low base was $642,655. The frozen Round 23 P70 was $5,434,916.
Median absolute error on its 29 non-LS items was 38.93%. Two existing
joint-sealing lines used a price pool containing pavement reconstruction,
and two riprap lines specified a reinforced concrete toe wall absent
from the selected historical descriptions. Together these four physical
lines explain 76.78% of summed absolute line error. None of that
percentage apportions the whole-project error. The nine lump sums stayed
in the contract sum. See `handoff/scratchpad/historical_flood_bank_validation/FRISCO.md`.

Recomputing quantity multipliers for the Frisco opening from 2,110
strictly earlier recorded sources gave the same individual prices and
project totals. Recomputing the prior calibration as well put Round 23
P70 at $5,419,823. With 121 fully priced prior contracts and no
inferred fixed amount, P70 was $5,252,200. Neither history choice
accounts for the gap to the $642,655 actual base. See
`handoff/scratchpad/frisco_prior_quantity/RESULT.md`.

Using the repaired six-contractor Mansfield median on the two affected
historic selections gave $76,073 summed absolute physical-line error for
latest-direct and $69,138 for three-direct. The latter was still greater
than Round 23's $56,564 on the 24 shared items. The restored source was
absent from all 163 archived Round 23 direct references. The pooled
market-price inputs remained frozen for this diagnostic. See
`handoff/scratchpad/dated_source_repair/RESULT.md`.

Of 329 independently measurable items on four other complete external
contracts, one unit rate changed with date-specific quantity factors.
Rebuilding the earlier municipal calibration lowered the Round 23 median
P70 overshoot on these four from 253.21% to 251.83%. All four priced
complete contracts stayed above their actual base lows. Reconstructing
the six whole-contract pricing stages showed that the line-estimate sum
already exceeded the base low on each of the four and on the newly
reviewed Frisco erosion job. Project comparables moved the Wichita Falls
five-bidder total upward from $237,931 to $338,239, and Calhoun County's
downward from $803,520 to $550,752. See
`handoff/scratchpad/external_project_totals/STAGES.md`.

The City of Frisco's earlier concrete joint-sealant procurement
2409-134 had three contractors, three named road segments and seven
optional quantity bands. All 30 official rates use lane-miles. The
quantity of actual joints in LF was not reported. The source also
requires work without backer rod and includes traffic control in its
price. It supplies zero same-scope LF prices for the later erosion
job. See `handoff/scratchpad/new_joint_seal_references/FRISCO.md`.

### Rounds 38–41 — Evidence for unit prices and the covered project subtotal

The owner changed the project score to the original low bidder's covered,
non-lump-sum subtotal. The revised development replay scored 174 of 235
municipal schedules. The incumbent P80 reached that subtotal on 134 of 174.
The following retrospective experiments kept the model and public release
unchanged. The historical panel reconstructs winning extensions from quantities
and unit prices. Independent publication dates are available for a smaller subset.

- **Round 38.** Of 110 previously eligible unit-price replacements, one had
  an independently verified target opening, a matching complete bidder column
  and a reference published before the target date. Its substitution increased
  the absolute extended error by $441.24 against the incumbent.
- **Round 39.** Recalibrating the upper percentiles on previous scoped outcomes
  raised P80 coverage from 95/125 to 102/125 on jobs with enough calibration
  history. Among jobs it covered, the median overshoot rose from 42.36% to
  47.00%. P70 and P80 quantile losses both increased. The uncertainty change
  failed its registered tests.
- **Round 40.** Of 40 covered-subtotal P80 underestimates, 38 were already low
  when the unadjusted market unit prices were extended. A search over 243
  documented source positions for 6,035 comparable item positions found one
  fully eligible direct replacement. It increased absolute extended error by
  $474.33 against the Round 23 candidate.
- **Round 41.** A conditional calibration used the predeclared 25% threshold
  for the share of modeled variable cost excluded from the score. Of 57
  triggered targets, 12 had the required 40 earlier comparable histories.
  P80 coverage moved from 134/174 to 136/174, below the 80% floor. None of
  the eight independently timed targets had enough verified priors. The
  experiment qualified for neither adoption nor independent confirmation.

The registered inputs, forecasts and results are in the corresponding
`round38_source_certified_blend`, `round39_scoped_calibration`,
`round40_unit_underbid` and `round41_scope_exposure` local study directories.

## 6. Parser rounds (versions .18 to .34b)

The parser turns PDFs, scans and spreadsheets into one row per bidder × line item. Every change raises `PARSER_VERSION`. The version is part of the parse-cache key, so a new version re-parses everything. Parser rounds alternated with pricing rounds: each install was followed by a full rebuild and a re-score, and the next pricing round ran on the new data.

**The first audit (2026-11-18).** An independent audit against the source PDFs confirmed 23 defects; none of the 7 sources was clean. The main failure mode was silently dropped data, not misread prices. One defect changed the target itself: the TxDOT reader treated any name containing "engineer" as the engineer's estimate and so deleted a real contractor whose name contains "Engineering". That touched 2,988 of 209,104 rows in 32 lettings; in 5 of them the low bid had been replaced by the second-lowest bid (0.7–10.9% higher). Fixing the item-code length in the TxDOT archive reader raised its rows from 285,067 to 432,138 (+51.6%).

Effects below are municipal 2024–26, forward in time, unless noted. A before/after pair is valid only inside its own comparison. Versions cannot be chained, because new tabs and estimator changes came in between.

| Version | When | What it fixed | Lines or lettings touched | Effect on the scores |
|---|---|---|---|---|
| .18 | 11-19 early (PC) | 7 misread tabs (5 fixed, 2 confirmed as printed); 6 rules tightened; consultant names and "P.E." added to engineer-estimate detection; tables whose "bidder name" is an amount dropped | 19 misaligned tables dropped; the new detection hit only its 2 targets among 1,393 bidder names | Panel 707k → 760k rows; scorable lettings 125 → 216 (new data mixed in); strict leave-one-out P50 error 23.1% |
| .19 | Rules written 11-19 morning; first used on the Mac at 18:13 | 15 general rules for files that parsed to 0 rows | 43 zero-row files gave about 10 thousand rows; an independent check found 10 files had got worse | Not recorded |
| .20 | 11-19 20:0x | 11 general fixes proposed by that check, each reproduced by removing it alone | Bid columns matching a printed total 2,571 → 2,584; arithmetic mismatches 3,335 → 3,036 | With OCR rows and 47 new tabs, the under rate rose from 19.1% to 21.0% (176 lettings); see 4.4 |
| .21 | 11-19 20:19 | OCR rows connected | 181,868 rows, of which 1,775 from OCR; 735 OCR price cells checked against the images with 0 errors | Not recorded separately |
| .22 | 11-19 22:0x | Quantities recovered from "amount ÷ unit price, consistent across bidders"; completeness gate; locked-test-set tag | 14,032 lines got a quantity (165,244 → 179,278); scorable lettings 359 → 370 | With the locked set removed: 140 lettings, under 16.4%, overshoot 51.8% |
| .23 (round 3) | 11-20 01:4x | 49 rules for layouts, agenda packets, names and dates | Replay of 1,507 files: arithmetic mismatches 3,036 → 1,443; low-bidder total within 1% of the printed total 76.4% → 82.8%; hand-checked low bids 49/66 → 62/68; rows changed in 302 files | With 31 new tabs: 168 lettings; under 17.1% → 15.5%; overshoot 60.8% → 69.0%; P50 error 29.2% → 28.8% |
| .24b (round 3b) | 11-20 03:46 | Item family decided by the head noun; "remove and replace X" priced as X; material supply moved to "other" | 42,163 lines changed family (4.8%) in 1,443 lettings; material supply was 4,592 lines on the .23 panel | Overshoot 69.0% → 59.5%; P50 error 28.8% → 26.4% |
| .24c | 11-20 03:58 | Milling and thin treatments split out of asphalt paving | Not recorded | Overshoot 59.5% → 56.5% |
| .25 (round 4) | 11-20 04:50–05:02 | Fixes from the tail audit: TxDOT A+B bidding, re-lettings, alternates, dropped rows, a stricter completeness gate | In TxDOT A+B lettings the per-day part had been a median 8.2% of the low total (up to 28.2%) | Installed together with V2+F1+F2; the two effects cannot be separated |
| .26 (round 5) | 11-20 10:3x | Item numbers glued into descriptions; numbers above 144 in are not a size; box culverts sized by opening area; a lump sum with a quantity in its description gets the real quantity; 7 new families | Not recorded | 144 lettings: overshoot 51.3% → 48.6%; P50 error 26.0% → 24.5% |
| .27 (round 6) | 11-20 13:20 | Family errors in the erosion/drainage group: new families for desilting, irrigation and railing; trenchless crossings filed under casing | Not recorded | Erosion/drainage, 37 lettings: overshoot 58.2% → 51.4%, P50 error 26.6% → 23.2%. All municipal: overshoot 49.1% → 51.4% |
| .28 (round 7) | 11-20 15:10 | Linear materials counted "each" lose their unit (the money stays); code-only descriptions recovered; sidewalk banded by width | 5,734 lines got a description back | 184 lettings: overshoot 46.8% → 44.7%; P50 error 23.8% → 22.9% |
| .29 (round 8) | 11-20 18:15 | Families fixed in order of the dollars at stake; 9 new families | Line error on the same 8,670 lines $333.0M → $299.0M ("other" $47.6M → $23.0M) | 186 lettings: P50 error 22.9% → 19.1% (interval −6.3 to −0.8 points); overshoot 44.7% → 43.1%. The largest single step |
| .30 (round 9) | 11-20 22:34 | Bid-date rules; annual / as-needed contracts no longer scored as projects (unit prices stay in the pool); a few family rules; a thickness on an EA or LS line is not a size | Date or date precision changed on 98 lettings; the rules reached lines holding only about $3M of $303M line error | 188 lettings: overshoot 42.6% → 41.6%; P50 error 19.6% → 18.8% |
| .31 (round 10) | 11-21 04:44 | More table layouts; an engineer's column told from a bidder's; bidder-name cleaning | 6 of 10 held tabs now parse correctly; 5 were merged | Failed the literal rule (overshoot +0.25 point), entirely because of two lettings whose ground truth was corrected. Installed under the precedent below |
| .32 (round 11) | 11-21 07:17 | Units printed in odd places recovered; culvert families corrected | 8,470 more lines carry a unit; in the 43 files that had almost none, lines with a unit went from 39 to 7,515 ($2.8M → $311M of bid amounts); 149 spot-checked lines all correct | Passed once the 22 lettings whose truth or scoring state changed were set aside: 208 lettings, overshoot 47.38% → 46.00%, P50 error 19.84% → 19.83% |
| .33 (round 12) | 11-21 14:06–14:35 | Family only: trail earthwork out of sidewalk, "Earthwork" to excavation, abandoned and plugged pipe to utility removal | 637 lines, 83 descriptions; 128 places checked blind, all in favour of the new family | 224 lettings: overshoot 46.26% → 45.78%; nothing else moved |
| .34b (round 20, arm G) | 11-21 18:12 | Three family rules: beacons and signal hardware; markings named by their line; signs named by code or panel size | Corpus-wide 304, 896 and 190 lines; in the municipal window 8, 28 and 13 lines | 234 lettings: overshoot 45.25% → 44.56%; P50 error 19.67% → 19.43%; line error sum −0.25% |

One count is unresolved: a later note puts the .24b material-supply move at about 4.4k lines and 86 lettings on the .24c rebuild. The difference from 4,592 lines is not recorded.

**Adoption rule for parser changes.** Municipal P50 error and overshoot must not get worse. Each year's under rate is at most max(20%, baseline + 3 points). No main TxDOT measure may worsen by more than 0.5 point.

**Precedent for a change that corrects ground truth.** Round 10 failed the rule literally. All of the worsening came from two lettings whose "truth" had been corrected: in one, two add-alternates had been counted in the low bid; the other became scorable for the first time. Without them the figures hardly moved. The precedent: a parser change that corrects ground truth is judged on the lettings whose truth did not change. Rounds 11 and 12 were installed under it.

**How prices went wrong in parsing** (examples from the reports):

- Trail excavation, 400 CY: bids $60/CY, model $1,387.65/CY, because "trail" put it in the sidewalk family. After the fix $33.05/CY.
- Hydromulch: bids $1.19/SY, model $73/SY (wrong family).
- A municipal 2-inch asphalt overlay, 95,000 SY: bids $13.34/SY, model $2.92/SY, because it shared a cell with TxDOT milling at about $2.5/SY.
- A lump sum whose description says "(28,731 CY)" was priced as 1 LS instead of 28,731 CY.

## 7. Data harvest rounds

Fixed procedure for every batch since 2026-11-20: staging area; duplicate check (file hash, bid number, project name); scan for public copies of company projects; score the new lettings as unseen and compare with the engineer's estimate printed on the tab; merge; full rebuild; both guards; re-score by year.

Harvest manners: about 40 requests per site at most, at least 1–1.2 seconds apart, a generic client name. At a 403 or a challenge page the harvest stops and records "blocked"; it never goes around a block. Vendor portals that need an account (Bonfire, IonWave, CivCast, Public Purchase, BidNet) were not touched.

| Round | Date | What was added | Effect on the scores |
|---|---|---|---|
| TxDOT sources | 11-17 | The public dashboard (low bid only, last 12 months). The FTP archive: 303 monthly archives, 2015–2024, holding 13,956 project tabs, 2,024 of them in DFW. The open-data per-item bid dataset: a DFW file of 209,104 rows over 14 counties, 2024-11 to 2026-11 (it keeps a rolling 24 months only) | The base of the TxDOT market. No score recorded |
| First cities and counties | 11-17 to 11-18 | Legistar public API: 421 files from 11 cities. Arlington 61, Mesquite 144, Rowlett 32, Dallas County 6, Denton 2, Collin County 540. Garland's own average-unit-price tables (2 editions) | After the 11-18 full rerun: 701,439 training rows, 3,550 projects; municipal 60,586 rows, 391 projects |
| Third harvest | 11-19 early (PC) | About 290 files, all fetched without a browser: Frisco 148, Richardson 82, Irving 23, Rowlett and others 20, Denton 15, Fort Worth 1, Flower Mound 1 | 07:41 rebuild: 760,096 panel rows; 216 scorable municipal lettings (strict leave-one-out P50 error 23.1%) |
| Three parallel harvests | 11-19 10:08–12:2x | 10 new cities: 117 itemized tabs. Counties: 13. Water utilities and agencies: 13 | Not recorded separately |
| Dallas and Fort Worth routes | 11-19 evening (Mac) | Neither city publishes itemized tabs outside its vendor portal. Side finds from nearby small cities: 36 itemized tabs of 43 files, and 11 merged of 12 itemized | First Mac rebuild: parsed rows 131,586 → 173,931. After 47 new tabs, OCR rows and parser .20 the under rate rose from 19.1% to 21.0% (176 lettings), which led to the 16% guard target |
| DFW erosion/drainage, overnight | 11-20, merged 01:50 | 31 itemized tabs: streambank / erosion / channel 9, drainage / culvert / detention 17, roads with a large storm-sewer part 5. 15 print an engineer's estimate; 12 owners new to the data. Found through CivicClerk's public API, Legistar re-scans and other agenda systems | Merged with parser .23: 140 → 168 lettings; under 17.1% → 15.5%; overshoot 60.8% → 69.0%; P50 error 29.2% → 28.8% |
| Statewide erosion/drainage | 11-20 03:3x | 172 files from outside DFW: 159 itemized and 13 totals-only with an estimate; 35 owners; 14 scans | Not merged; kept in staging. See S1 in section 5 |
| City round 1 | 11-20 11:27–12:20 | 47 staged, 47 merged; 7 erosion/drainage; 19 with an estimate; 17 owners. New route: CivicClerk full-text search, which found tabs attached to consultants' recommendation letters | Before merge, 5 lettings: model 33.8%, engineers 20.1%. After: 144 → 169 lettings; under 18.1% → 17.2%; overshoot 50.5% → 49.1%; P50 error 24.3% → 23.4% |
| City round 2 | 11-20 13:21–14:00 | 30 merged; 1 erosion/drainage; 13 with an estimate. Found a paging bug in CivicClerk search (the "next" link always returns an empty page), so round 1 had seen only the newest 15 meetings per search term | Before merge, 10 lettings: model 22.2%, engineers 15.9%. After: 169 → 183 lettings; under 16.6% → 15.3%; overshoot 51.4% → 46.8%; P50 error 23.0% → 23.8% |
| City round 3 | 11-20 18:19–19:25 | 25 staged, 23 merged; 0 erosion/drainage; 13 with an estimate. Started from a gap table by owner and year; used Legistar meeting attachments, Destiny, NovusAGENDA and Granicus | Before merge: model 58.6% on 9 lettings; engineers 18.9% on 12 estimates. After: 190 lettings; under 15.3%; overshoot 42.6%; P50 error 19.6% |
| City round 4 | 11-21 00:29–01:16 | 19 staged, 17 merged; 2 erosion/drainage; 4 with an estimate. New route: tabs bound inside whole agenda packets (14 of the 19) | Before merge, 6 lettings: model 37.8%, 3 of 6 under. After: 190 lettings; 14.7% / 45.2% / 19.8% |
| City round 5 | 11-21 01:59–02:45 | 45 staged, 35 merged (6 more released later); 6 erosion/drainage; 7 with an estimate. 582 agenda packets (about 35 GB) downloaded and searched page by page | Before merge, 26 lettings: model 28.3%, 4 of 26 under. After: 206 lettings; 15.0% / 46.0% / 19.5% |
| City round 6 | 11-21 09:55–10:40 | 10 staged, 5 merged; 1 erosion/drainage; 2 with an estimate. 298 packets read under a stopping rule; 688 lower-ranked packets left unread | Only 1 letting scorable before merge. After: 224 lettings; 14.3% / 46.3% / 20.2% |

- 194 public tabs were merged after 2026-11-20 01:48: the overnight 31, city rounds 47 / 30 / 23 / 17 / 35 / 5, and 5 + 1 released after parser rounds 10 and 11.
- The panel at the 2026-11-21 15:20 rebuild: 867,223 rows and 4,222 projects. City and county tabs: 233,409 rows and 1,071 projects (391 on 11-18).
- Before merge, the model's error on new lettings was usually above the engineers'. Most of the gap came from fixable pricing defects (lines filed under "other", small-job mobilization that does not scale, high per-each prices). Those became the targets of the parser and pricing rounds.
- Round 6 also showed that the estimator refused schedules with bidder counts beyond the discount table; that started round 16.
- A plans pilot (11-20) sampled 55 DFW municipal lettings with drawings: a full plan set could be downloaded without a login for only 8 (14.5%). Drawings are not used.
- A public data repository was published on 11-20 at 20:02 with 2,150 public bid tabulations and award documents, each with its source page.

## 8. Production now, switches, and rejected ideas

### 8.1 What is in production now

How a price is computed:

1. **Each line** gets a reference price, looked up by tier: token match, then band cell (family + unit + size band + quantity band), then family cell. The unit is always a hard key. The price is escalated to the bid year with the market's own paired, chained price index. Identity rules apply: F1 size extrapolation, F2 removal identity, AB abandonment identity. Not-covered lines are not priced.
2. **The lines are summed.** This is the estimate of the median bidder's total.
3. **× competition discount f(N).** Municipal uses the combined table × a municipal factor (0.9752). A bidder count above the top bin uses the top bin.
4. **Comparables blend (V2).** Geometric mean with a price built from the 5 most similar earlier jobs. TxDOT also uses T1.
5. **× centring factor**, fitted forward.
6. **+ owner-set allowances** at face value. The result is P50.
7. **P80 = P50 ÷ divisor**, fitted forward, with the guard (target 16%, capped at the ordinary 20th percentile), taken in B80 mode. In production, B80 means the divisor and the guard level are those of production with the round 10, 11, 13, 16 and 17 switches off, at each date; P50 and centring are production's own.

| In production | Since | Evidence |
|---|---|---|
| Public data only; two-stage median reference prices (within a letting, then across lettings); token → band → family tiers; quantity bands | 11-18 / 11-19 | Leave-one-out, municipal / TxDOT. Quantity bands: 32.6% / 50.1% → 28.6% / 30.8%. Token tier: 28.5% / 30.9% → about 23% / 22–25% (two code comments give different figures, and the original measurement was not kept) |
| Competition discount on the total; municipal = combined table × 0.9752 | 11-18 | The total-based discount won 517 of 831 lettings |
| Paired, chained price index by market; reference prices escalated to the bid year | 11-19 | Error 26.8% → 24.8% (municipal) |
| Owner-set allowances at face value, not discounted, not centred | 11-19 | 200 of 224 such items carried the same amount from every bidder |
| Alignment + forward calibration + guard R4g + narrow year carry | 11-19 18:36 | 125 lettings: under rate 22.4% → 19.2% |
| Guard target 16%, cap 20% | 11-19 20:30 | Aimed at 20% the under rate was 21.0%; aimed at 16% it was 17.0% with the same in-band share |
| V2 comparables blend + F1 + F2 | 11-20 05:06 | Third opening of the locked set: under 19.6%, overshoot 35.9% → 31.0%, P50 error 18.4% → 13.6% |
| T1 (TxDOT only): exact match first + mobilization as a share + discount by size | 11-20 05:51 | Round 6: TxDOT overshoot 37.2% → 31.3%, P50 error 20.2% → 16.6% |
| "Not covered" scope v2: seven classes of specialty equipment | 11-20 10:44–10:51 | Owner's decision; rule fixed before scoring |
| Low-confidence display for mobilization and general-conditions lump sums (display only) | 11-20 afternoon | Forward re-run of 2,129 lettings: 0 differences in P50 and P80 |
| Annual / as-needed contracts not scored as projects (unit prices kept) | 11-20 22:34 (.30) | Their totals are an evaluation formula over notional quantities |
| Round 18 X1/B80: bidder-count clamp + abandonment identity + B80 | 11-21 15:20 | Passed tracks T and U |
| Round 20 arm G: parser .34b with three family rules | 11-21 18:12 | Passed track U; post hoc, with a prospective check |

Constants after the round 20 install: municipal centring factor 1.043585, P80 divisor 0.7342 (P80 is about P50 × 1.362), guard level 0.148, 15.36% under on 358 scored lettings. TxDOT: centring factor 1.013033, divisor 0.784635 (about × 1.2745), guard level 0.14448, 15.8% under on 1,778 lettings.

### 8.2 Registered checks: what is on with a switch-off rule, and what is off waiting

| Item | State | The check runs when | Condition | Switch or preset |
|---|---|---|---|---|
| T1 (TxDOT only) | On | 100 DFW TxDOT lettings bid after 2026-11-20 are in the data, or on 2027-03-31, whichever comes first | Score with T1 on and off on the same new lettings. Keep T1 only if the under rate is at most 20% and the overshoot is below T1 off | `pricing_rule(txdot_c4d1=False)` turns it off |
| Round 11: municipal cells + comparables-side removal identity + pipe-only F1 + identical-bid owner-set | Off | At least 40 DFW municipal lettings dated after 2026-11-20 are in the data and scorable | Switch on only if the under rate is at most 20% and the overshoot is strictly below production on the same lettings | `round11_core_not2` |
| Round 13: municipal price level for TxDOT-dominated cells | Off | Same 40 lettings | Same condition. Report jobs of $2M and more separately | `round13_l` |
| Round 18: clamp + abandonment identity + B80 | On | Same 40 lettings | Switch off in both markets if the under rate is above 20%, or the overshoot is more than 1.0 point above production with round 18's switches off | Round 18 switches |
| Round 20 arm G: parser .34b | On | Same 40 lettings | Revert the parser file to .33 and rebuild if the under rate is above 20%, or the overshoot is more than 1.0 point above the .33 run | Parser file |
| Guard target 16% | On | 30 scorable municipal lettings bid from 2027-01-01 | Above 20% under: lower the target by 2 points. At most 18% under, and in band more than 3 points below the 20%-target rule: consider 18%, after a new registration | `GUARD_TARGET` |
| Narrow year carry | On | Lettings bid from 2027-01-01: 30 municipal, 100 TxDOT | Keep it if the municipal under rate is at most 20% and the in-band share is not more than 2 points below the full carry; otherwise switch to the full carry | Not recorded |

- Each change is checked on its own, paired against production with the code and data of that time. Round 13's report adds: pair each with B separately, do not stack.
- **Open point.** Rounds 11 and 13 were chosen in own mode. Since round 18 production uses B80 by default, and turning those presets on does not turn B80 off. The registrations do not say which mode the check uses. Write that down before the check runs.
- Also in the code and switched off, not waiting for a check: the preset `round10_core` (failed on the locked set), the pipe-only size extrapolation `F1_PIPE_ONLY`, and the identical-bid owner-set rule `IDENTICAL_BID_OWNER_SET`.

### 8.3 What was rejected and why

| Idea | Result | Why it failed |
|---|---|---|
| GBM unit-price model v2 | Municipal 24.8% → 23.7%, not significant; TxDOT 25.1% → 15.8% | No reliable municipal gain; the TxDOT price level does not carry forward in time |
| Thin-reference fixes (8 variants) | None met the rule | The error is not in thinly supported unit prices |
| City price factors; owner effects (OW1, OW2) | 23.8% → 24.6%; overshoot 51.76% / 51.86% against 51.29% | An owner's past residual does not predict the next (correlation +0.035) |
| Level corrections by project type or title | Error 21.1% → 25.2–27.8% | Type effects flip between years |
| P80 multiplier by spread; per-job margins (M_T, M_Z) | Municipal not significant (spread, M_Z) or the wrong way (M_T) | The thin-evidence share is not a measure of uncertainty here. The spread-based multiplier did help on TxDOT |
| Lump sums as a share (round 1 C1/C2/C5; round 12 P_LS) | Lines more accurate; totals not | Total spread unchanged; lower lump sums expose low unit prices on small jobs |
| Capped mobilization (P_C; rounds 14, 18, 19, 20) | Line error on 58 lines $16.01M → $4.65M | Turns small jobs into unders. After round 20 one job still blocks it (a retaining wall bid by the SY, matched to the wrong cell) |
| Discount by size for municipal (D1); centring by size (R1, R2, P_SC) | Accuracy unchanged; overshoot not lower | Centring cancels in P80; coefficients drift |
| 36-month window (W36); recency-weighted centring (CW12, CW24) | Did not pass | The level change is a step, not a trend |
| Pure comparables, no blend (CMP) | P80 under 22.0% | Breaks the floor; the blend is in production |
| "Other" lines by most similar description (O1) | P50 error 26.0% → 23.3% but overshoot 51.3% → 54.0% | 69% of the lines found no neighbour; the multiplier went up |
| Facility correction (FC, FC-ED) | Equal to B, or worse | Most flagged jobs were not facilities; replaced by the "not covered" scope |
| Raise weak-evidence lines (L1, U1) | Overshoot +7.0 points | Weak evidence is unrelated to the residual |
| CORE and MC in own mode (round 10) | Development overshoot 49.0% → 42.0%; locked set under rate 18.8% → 29.2% | Its own margin narrowed more than unseen lettings supported |
| Thin-cell shrinkage (P_T); weak-match blend (P_W) | Single lines fixed (140 lines $24.7M → $9.5M) | Removes an offset and creates new unders |
| Statewide erosion/drainage prices (S1) | Overshoot 51.3% → 50.4% | Inside the noise; 2024 under rate +2 points; no stable regional factor |
| Unit conversion (UNIT_POOL, U2, U3, U4) | Invariance achieved; line error worse | A rare unit is often another product; conversion moves the quantity band. Kept as a worksheet hint |
| Park equipment "not covered" (coverage v3) | Overshoot +0.26 point, upper end +2.07 | Exposes the high lump sums of small park jobs |
| Abandonment family key (AB2, "#ab") | Line track failed | The split cells are too thin |
| Clamp in own mode (C/own) | Overshoot +1.33 points | One newly scored under moved the guard; the B80 version is in production |
| Small-job unit-price lift (S, SC) | Fitted factors mostly below 1; under rate up | The premise fails in dollars; fix the mismatched lines instead |
| Screening-wall family; walls not sized by height (round 20 F) | One line flipped 0.07× → 2.72×; 13 of 15 lines farther | New cell too thin; height bands carry information |
| Token cells by family (TF) | Flat on municipal; good on TxDOT | Failed with F. Worth its own TxDOT registration |
| Dewatering / diversion correction | 7 flagged lettings, 8 required | Could not be judged |
| Erosion basket index; dollar-weighted index; index with quarter mix removed | Unstable, or a worse fit | The estimator reads the plain market index |
| Guard cap also at 16%; full year carry | In band 14.8% → 8.0%; 4.0 points less in band | Costs in-band share with no gain at the floor |
| Size keys read from descriptions (box-culvert span × rise and the like) | Readings accurate (306 of 309 values), family classing 78.6% right on unseen text | Not connected; needs a forward test first |
| Drawings | Full sets downloadable for 14.5% of sampled lettings | Too few; the owner ruled out image recognition |

## 9. Lessons, by area

**Data**

- City data is thin and hard to get. The big cities keep itemized tabs behind vendor portals (Dallas has 0 lettings in the panel, Fort Worth 1). Small cities' tabs hide in agenda packets of 600–1,700 pages, sometimes as an attachment named only "Letter".
- "Cannot be found" often means "cannot be found this way". Several cities first written off were later reached through council agenda systems.
- Key creek-erosion items barely exist in public data. A 12-inch gabion mattress has 0 lettings in DFW (32 statewide).
- Itemized tabs are worth far more than totals. 708 totals-only files are not used, because scopes are not comparable.
- External series have traps. Compare benchmarks within the same industry and adjustment basis. A $0 weekly wage and hours with no seasonal pattern were caught only by asking whether the number is physically possible. "Not fetched" must be recorded apart from "does not exist".
- Harvests must run unattended. Nothing may raise a dialog that needs a click.

**Parsing**

- Silently dropped data is the main risk, and a quality measure that improves when data is dropped is an incentive, not a measure (one gate gave a parse that had lost 80% of its bids a score of 1.00). Watch coverage too: bidder counts, rows per column, printed totals that do not match.
- Wrong classification costs more than misread numbers. But the money the parser can still move got small: by round 9 its rules reached about $3M of $303M of line error. The rest is pricing.
- When a unit is doubtful, keep the money: clear or flag the unit, do not delete the line. Deleting lines changes the ground truth.
- Correcting ground truth can make scores worse, because the corrected line had been offsetting another error.
- Re-run before diagnosing. Output on disk may come from old code.
- Split by cause before counting. One symptom often has several causes.
- Synthetic self-tests are not enough. Replay the whole corpus each round, remove each fix alone to see its effect, and have changes checked by someone who did not write the rule.

**Small samples**

- Only 140–234 municipal lettings can be scored forward, 31–42 in erosion/drainage, and about 56–59 in the locked set (the under-rate interval is about ±8 points). Many effects are 1–3 points and their intervals cross 0.
- The same option can flip between data snapshots (round 5 chose F2 on snapshot 2; only V2+F1+F2 qualified on snapshot 3).
- The rules were found and scored on the same 2024–26 lettings. That is an overfitting risk. A clean test needs lettings after 2026-11-20.
- Do not over-draw a scarce and important stratum into a test set. Draw in proportion, and find test evidence for that stratum elsewhere.

**Centring cancels in P80**

- A level change shared by all lettings does not make P80 more accurate. Rounds 2, 4, 7, 9, 12 and 15 hit this wall in different forms; round 15 wrote out the algebra and made it a structural conclusion.
- The overshoot comes from the spread of single-job errors. It can only fall if single jobs are priced more accurately: line accuracy, weak matches on main items, small-job lump sums, specialty coverage.

**Fixing one thing exposes another**

- On small jobs, lump sums run high and unit-priced lines run low, and the two cancel. Fixing the lump sums (rounds 12, 14, 18, 19) or removing underpriced equipment (coverage v3) exposes the other side and creates unders.
- Fixing a thin cell (P_T) removed an offset: one letting went from 1.52× to 0.95×.
- The same happened inside the parser (rounds 9 and 10) and in round 20 (pipe bursting).
- Fix such things as a pair (lump sums together with small-job unit prices). A fix helps totals only if the remaining spread gets smaller.

**Margin versus accuracy**

- A change that makes P50 more accurate in development often narrows the P80 margin with it. On unseen lettings accuracy improves less, and the under rate goes up (CORE: 29.2% on the locked set). B80 exists for this reason: a price improvement may not narrow the margin on the side.
- An adaptive rule lands where it is aimed. Aimed at 20%, the guard is over the line half of the time; so it aims at 16%.

**Fragile comparables**

- Well-covered schedules are accurate and poorly covered ones are not (12.8% against 36.1%).
- For an unusual schedule the comparables price swings. One sample schedule's P50 rose 48% overnight while its line prices moved only 2–9%: two newly merged lettings had entered its five most similar jobs. This is why the UI lists those jobs.
- V2 failed twice (in band; overshoot on the locked set) before it was confirmed on new data under a new rule.

**Time drift**

- Strict leave-one-out hid a downward drift of price levels. Forward-in-time scoring is required for anything that moves the level or the P80 multiplier, with results split by year.
- The project's own bid-price index moved −15.0% for TxDOT and −6.5% for DFW municipal from 2025 to 2026.
- In 2026 the under rate was 17% (municipal) and 23% (TxDOT). The guard adapts with a lag.

**Process**

- Register before scoring, and verify that the baseline reproduces production exactly before any arm runs.
- Experiments run in a private copy. Production is changed from one place only, by a written install procedure; check that no rebuild is running before changing `src/`.
- Every new batch of data is scored as unseen before it is merged.
- The PC failed three times from a memory fault before the move. After the move every file was verified; one PDF had a single flipped bit and was replaced from its source.
- The test firewall is structural, not a label: company files are whatever sits at the top level of `data/`; public copies of company projects are detected by the overlap of (quantity, unit price) pairs and quarantined; `train_test_guard` proves, cell by cell, that no test row reaches a fitted number.

## 10. Decisions set by the project owner

These are the judgments the project's records attribute to the owner, with when each was made and what it fixed.

| Decision or question | When | What it fixed in the project |
|---|---|---|
| The target: never underestimate; at least 80% not under; overshoot preferably at most 13% | 11-18 | An asymmetric goal: the output is a quantile, with the under rate as a constraint |
| 13% is a hope, not a gate; within the floor, the less overshoot the better | 11-20 about 02:10 | The score became the median overshoot; in band is only reported |
| Compare with engineers at the same not-under rate; matching their accuracy is enough | 11-20 about 02:40 | The matched not-under comparison; engineer estimates as the yardstick |
| Company files are test only; never fit to them (said three times) | 11-18 | Train/test separation, proved by switching test rows on and off |
| Hold out part of the public data as a pure test | 11-19 about 21:10 | The locked public test set |
| Score new data as unseen first, against its printed engineer's estimate, before merging it | 11-20 08:5x | The fixed intake procedure |
| Pumps, generators, prefabricated bridges and the like are "not covered": no price, flagged, and judged with that part taken out of both sides | 11-20 about 09:55–10:00 | The estimated quantity became the covered part of the low bid |
| The work to be served is mainly municipal; municipal accuracy comes first | 11-20 about 10:45 | Municipal is the main test market; TxDOT is a fallback price source and a showcase of the method |
| If mobilization cannot be estimated well, say so | 11-20 14:2x | The low-confidence display |
| Agreed to the two-stage diagnostic | 11-20 15:0x | The error split between pricing and competition |
| Lump sums such as mobilization are a percentage of the job and cannot be compared across jobs as dollar amounts | 11-20 about 15:3x | Rounds 12 and 14; the lump-sum guidance shown to the estimator |
| The tool is an aid for the estimator preparing an OPCC; where the number is weak, put more reference material in front of the estimator | 11-20 about 18:20 | Line accuracy has value of its own; later the unit-price track |
| Takeoff units get mixed; conversion must be considered | 11-20 about 22:25 | The unit-conversion studies and the worksheet hint |
| For pipes, "abandon" is not "remove" | 11-21 about 13:00 | The abandonment identity (round 17) |
| More accurate unit prices can also go live, not only more accurate totals | 11-21 14:22 | Track U of adoption rule v2 |
| A 1% loss in total accuracy is acceptable for a 5–10% or larger gain in unit-price accuracy | 11-21 about 14:25 | Track U+ |
| The worksheet switches between P50 and P80, with P80 as the default | 11-21 15:40–16:20 | The estimator chooses the quantile |
| Asked why engineers are accurate | 11-19 12:20 | Led to the comparables method |
| No image recognition on drawings; text only | 11-19 12:40 | Limits the information the model may use |
| Asked whether extreme lettings disturb training | 11-20 03:4x | Led to the tail audit |
| Stop algorithm work when round 20 finishes | 11-21 18:23 | Round 20 is the last round |

Taken together, the owner's decisions define the problem: the objective (at least 80% not under, then the least overshoot), the benchmark (engineers under the same constraint), the estimated quantity (the covered low bid), the usable data (public, forward in time), and the adoption standard (the floor first, then totals or unit prices). The model solves the problem inside that definition.

## 11. What could be done next

Algorithm work is paused after the single Round 21 exception to the owner's 2026-11-21 decision. These are the candidate steps the sources name, each with the reason it might pay.

1. **Run the prospective checks.** Collect 40 new DFW municipal lettings dated after 2026-11-20, then check rounds 11, 13, 18 and round 20 arm G. Check T1 at 100 new TxDOT lettings or on 2027-03-31. Check the guard target and the narrow carry on 2027 lettings. Why: rounds 11 and 13 showed development overshoot gains of 13.2 and 5.2 points and can only be switched on this way; it is also the only clean test, since every rule so far was found on the 2024–26 lettings.
2. **Restart from the capped-mobilization rule (C).** One small job blocks it: a retaining wall bid by the SY, matched to a cell that holds sidewalk-type items. Why: C cuts the line error of 58 mobilization lines from $16.01M to $4.65M; on its own it lowered the line error sum by 3.3% in round 19.
3. **Fix small jobs as a pair.** Lump sums as a share of the job, together with the unit-priced lines that run low. Why: this one obstacle stopped rounds 12 and 14 and coverage v3; small city jobs come out about 20% high.
4. **Line accuracy in the families with the most dollars.** Water pipe (8% of city dollars, half of lines within 44%), mobilization (5%, runs 46% high), asphalt (4%, half within 36%), unclassified lines (4%, half within 98%), and the low tail. Why: about four fifths of the error is in pricing the median bidder's total; with that stage perfect the overshoot would be 15.0%.
5. **The erosion/drainage group runs about 4% high.** The calibration reacts slowly to recent price levels. Why: this is the work type the tool is aimed at.
6. **More city data.** 688 unread agenda packets; held tabs that can still be repaired; Denton's document library near known hits. Only the owner can open vendor-portal accounts or file public information requests. Why: thin city history is a root cause of the municipal gap, and each round's new lettings also widen the test.
7. **Statewide data.** Fix the intake problems S1 exposed (13% of lines without a unit, scans that gave no rows), then re-register a regional adjustment. Why: key erosion items have almost no DFW history.
8. **Modelling stages never built.** A hierarchical mixed model (honest intervals for sparse items), description-embedding retrieval, and size keys read from descriptions (forward test first). The closest step taken was round 13's thin-cell shrinkage, which fixed single lines but did not pass.
9. **Re-fetch TxDOT regularly.** The open dataset keeps a rolling 24 months; history is lost unless it is accumulated.
10. **Settle two readings before the next registration.** Whether "TxDOT not worse" means no more unders in any year, or the older max(20%, B + 3 points). And which scoring mode (own or B80) the round 11 and 13 checks use.
11. **Product direction.** Put the reference material in front of the estimator; OPCC history across design stages is a later feature; reading a job's drawings and documents by AI is a placeholder in the page (section 12).

## 12. The web app and the public site

The pricing work above is what the estimator computes. This section is what an estimator sees. Detail: `ui/README.md`; the owner's standing requirements for the page: `docs/HANDOFF.md`, section 6.

### 12.1 Timeline

| When | What | Commit |
|---|---|---|
| 11-19 | First local web app. It carried its own pricing engine, separate from the estimator, and waited while the estimator work ran. | local only |
| 11-20 17:30 | The app prices with the production estimator (`ui/engine_production.py` calls `src/estimate_low_bid.py`); the old engine was retired. Final layout: takeoff, project details, estimate, evidence. | `4a10c7d` |
| 11-20 18:20 | The owner repositioned the product: an aid for the estimator preparing an OPCC, with the model's number as one input. | `c34f0f2` |
| 11-20 18:39 | The OPCC worksheet (the estimator's own unit prices and notes next to P50 and P80); full bid tabulations with source links; a reader for messy pasted input. | `9632b41` |
| 11-20 20:02–20:33 | The public documents repository is linked from every reference; public mode (`ui/public_mode.py`): no server-side writes, uploads in memory only, rate limits, optional access code. | `cb269da`, `6576dcd` |
| 11-20 21:02 | The site goes live on a free cloud VM (Docker + Caddy). Before every deploy the package must price identically to the working copy. | `113f522` |
| 11-20 23:10 | Lines that cannot be read are listed apart as "could not be read"; rows can be dragged to reorder; plain wording throughout. | `78524b2` |
| 11-20 23:46 – 11-21 00:04 | Presentation-only results of the unit and lump-sum studies: a converted-unit hint, a printed-cap quick pick, and an alert naming lines that carry at least 15% of P50 on fewer than 3 earlier projects. | `e690a8d`, `b702f03` |
| 11-21 11:12–11:44 | A plain explanation of P50 and P80 with a picture; a construction contingency picked from a list (10% by default) with how often an OPCC that high fell short; a contingency line has no item type. | `2607e93`, `b6a00e1`, `675c696` |
| 11-21 12:05–13:56 | A blank takeoff template (Excel) to download; the estimate laid out as an OPCC (subtotal, contingency, total); the export's first sheet is the OPCC itself; warnings can be ticked off. These follow the conventions an engineer walked the owner through that morning. | `853c6c7`, `ec516cb`, `df66911`, `672d3d0` |
| 11-21 14:39–16:13 | The export laid out for print and readable in any viewer: totals are live formulas that also carry their values, wrapped rows are sized to fit. One export only. A P50 / P80 switch for the worksheet, P80 by default. A not-covered line has one number wherever it is typed. | `698e171`, `0a24545`, `7c550ae`, `3004a5a` |
| 11-21 16:32–17:49 | Past public bids of a not-covered line on hover; the takeoff template in the OPCC's own layout; a Reference column in the worksheet; the interim-review box at the bottom right; each line's note printed beside the OPCC. | `e0f5a48`, `b3c4314`, `73891bb`, `da3d5dd` |
| 11-21 18:57 | The export prints like a paper OPCC on US Letter (no inner lines, serif headings, the box from Units to Total Cost); a PDF of the same page; the "AI mode" placeholder replaces the older "read a PDF with an AI model" button. | `2fb71c7` |
| 11-21 19:35–20:06 | Long descriptions wrap in the takeoff grid; a filled template brings its project block (name, project no., prepared by); the OPCC's date is the user's own day (the server runs on UTC); the owner-set allowance alert disappears once the amount is typed. Site redeployed 20:15. | `8715256`, `081cae9`, `176d550` |

### 12.2 What was learned

- **One estimator.** The first app had its own engine and drifted from the model. Since 11-20 the app calls the estimator, and an identity check on 400 public schedules guards every deploy. A second drift was found on 11-21: the app's line classifier lacked one parser rule ("a thickness on an EA or LS line is not a size"); it now calls the parser's own function.
- **Estimators check, they do not trust.** What the owner asked for, review after review, was evidence and control: the bids behind each price, the similar projects, which lines are weak, their own price beside the model's. Accuracy claims are shown as a track record, not as a promise.
- **Words.** "Family" confused a reader; it became "item type". Codes, abbreviations and cut-off text were removed wherever they appeared.
- **Exports are read in previews.** A workbook that only calculates in Excel shows zeros in a phone or mail preview; formulas now carry their values. A medium border read as a thick box in one viewer. The check is a rendered image of the sheet, not the file.
- **Dates.** The server runs on UTC, a day ahead of Texas every evening; the page sends the user's own date.
- **Ten public takeoffs** were built from public bid tabulations and walked through the page as a usability test (2026-11-21): the fixes of 19:35–20:06 came from it.
