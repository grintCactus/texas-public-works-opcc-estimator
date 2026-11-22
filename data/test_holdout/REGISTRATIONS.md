# Decisions confirmed on the test set: registered in advance

Each registration is written before the test set is opened for it. Once written it is not changed; when a change is unavoidable, a dated note is appended after it.

## 1. Does the estimating rule in production hold the floor on lettings it has never seen? (registered 2026-11-19 21:25)

**Decision to confirm**: the estimator now in production. It is alignment + forward-in-time calibration + the guard (targeting 16%, capped at the ordinary P80) + the narrow year carry (`reports/forward_floor_integration.md`), retrained on the public data with the test set removed.

**When to run**: once the second round of parser fixes is done, the full rebuild is complete (with the test-set files tagged `public_test`) and both guards pass: the first run of `src/holdout_eval.py --confirm`.

**What to look at**: the scorable municipal lettings in the test set (about 60 expected):
- Main metric: the share of lettings with P80 below the low bid (point estimate).
- Also reported: the share within 1.00–1.13x, the median overshoot when over, the P50 median ratio and median error, each with a 90% interval; by year, and erosion/drainage vs other.

**How to judge**:
- Under-share at most 20%: confirmed, reported as it is.
- Above 20%: reported to the user as it is. The rule is **not** changed because of this one result: the test set is not used to tune parameters. Any new change must be registered separately and judged on new lettings after 2026-11-19.

**Note**: the second round of parser fixes drew on the misread files listed by the independent check. That is a data-quality matter unrelated to estimate error; if some of those files are in the test set, it only affects "whether they are read correctly" and involves no look at their estimate results.

### Addendum (2026-11-19 21:55, before any opening)
- **The test set is replaced by version 2** (md5 `9adc9f26a541de73a7b527c9e7951a42`). Version 1 was retired without ever being scored: it locked away 30 of the 43 erosion/drainage letting groups of 2022–26, leaving only 13 in training and none at all for 2023 and 2026, which would have weakened the estimator on the most important work type. Version 2 draws 22% of every "year × work type" cell: 59 groups, 9 of them erosion/drainage, with 34 kept in training. The version 1 list is kept as `public_holdout_v1_unused.csv`.
- **This confirmation is only half clean**: constants such as the guard's 16% target were set on 2024–26 data that still contained these lettings, and their overall results took part in that choice. So the result of registration 1 mainly shows "whether the floor holds on these lettings after retraining without them"; it is not a clean confirmation of the 16%. Only decisions registered after the lock are judged fully cleanly by the test set.
- **Small sample**: with about 59 lettings, the 90% interval of the under-share is about ±8 points. A point estimate above 20% is still reported as "not held", together with the interval, to show how likely it is to be just sampling noise. Erosion/drainage has only 9 lettings: described only, no separate conclusion.

### Result of registration 1 (2026-11-19 22:47, first opening of the test set)
- Run: `src/holdout_eval.py --confirm ...`, on the 22:44 rebuild (parser .22, OCR round 2, test set tagged `public_test`, both guards passed). The access log has the two rows, "start" and "done". Output: `reports/measurements/holdout_confirm_1_20261119.txt`, summary `handoff/scratchpad/holdout_eval/confirm_20261119_224736.json`.
- 58 lettings scored: 58 of the 59 registered letting keys were scored. Not scored: 3 with no usable price rows in the panel, 1 with under 90% of its items priceable (the script's own words; nothing was printed letting by letting, so as not to look at the test set more than needed). Training: 269 municipal lettings scored forward in time.
- **Main metric: P80 below the low bid 8/58 = 13.8% (90% interval 6.9–20.8%). Not above 20%, so per the registration: confirmed.**
- Also reported: within 1.00–1.13x 13.8% (6.9–20.7%); over by more than 13% 72.4%; median overshoot when over 38.2%; P50 median ratio 0.891 (0.828–0.956), P50 median error 22.4%.
- Under-share by year: 2022 18.2% (11 lettings), 2023 10.0% (10), 2024 0% (13), 2025 21.4% (14), 2026 20.0% (10). Each year has only 10–14 lettings, so the intervals are wide.
- Erosion/drainage, 9 lettings: 0 under, 2 in the band, P50 median ratio 1.20, median overshoot when over 90% (described only, no conclusion).
- Per the 21:55 addendum, this is a half-clean confirmation: constants such as the 16% were set on data that still contained these lettings.
- Per the registration, no rule is changed because of this result.

## 2. Pick a P50 option by the goal as the user clarified it, and confirm it on the locked test set (registered 2026-11-20 02:1x, before the re-test results on the new data came out)

**Why register now.** At 02:0x the user restated the goal, in their words: "I'd like your estimates to come out above the low bid almost every time ... in fact you can't do it almost every time ... once it is over ... I'd like it over by as little as possible. That amounts to being as accurate as possible. As for thirteen percent, that is my hope, but it is not a hard requirement."
- So from now on: the floor stays (P80 not below the low bid on at least 80%); **within the floor, the score is how far over it goes when over, the less the better**; the 1.00–1.13 share is only reported, no longer a hard gate.
- The development rules of rounds 1–4 made "in-band share not more than 1 point below the current rule" a hard gate; that was written under the old understanding, and the verdicts of those rounds stand.

**Candidates** (code already frozen: exactly the ones scored in rounds 3 and 4, no new ones added):
- W36
- C4+D1
- V2
- V2+D1
- V2+D1+W36
- V2+C4+D1
- V2+C4+D1+W36

**Data for the pick.** The data after the 02:01 rebuild (parser .23, 31 new DFW bid tabs, TxDOT refreshed). The experiment sub-task's re-test of rounds 3 and 4 on this data (Part 2); its results were not out yet at registration.
- Municipal 2024–26
- TxDOT 2023–26
- Both forward in time

**How the development stage picks.** An option qualifies only if it meets all three:
- **(a) Floor**: municipal P80 under-share ≤ 20% overall, and each year ≤ max(20%, the current rule's value that year + 3 points); the same for TxDOT.
- **(b) Size of the overshoot**:
  - the municipal median overshoot when over (the median of P80/low bid − 1 over the lettings with P80 ≥ the low bid) is below the current rule's;
  - TxDOT's is not above the current rule's.
- **(c) Accuracy not worse**: the 90% interval of the paired difference in municipal mean |ln(P50/low bid)| against the current rule must not lie entirely above 0.

Among the qualifying options, pick the one with the smallest municipal median overshoot. Less than 2 points apart counts as a tie, and a tie goes to the simpler one: W36 < C4+D1 < V2 < V2+D1 < V2+D1+W36 < V2+C4+D1 < V2+C4+D1+W36.

If none qualifies, the locked test set is not opened.

**How to confirm on the locked test set (opened once, recorded in the access log).**
- **Script**: the confirmation script in `handoff/scratchpad/p50fix/confirm/` scores the current rule and the chosen option on the same held-out lettings (paired).
  - The script now supports V2+D1+W36.
  - If another option is chosen, the script is first extended to it and verified on the decoy only (baseline identical to holdout_eval letting by letting, all leak checks passed), and only then is the set opened.
- **Confirmation conditions**, both must hold:
  1. The chosen option's P80 below the low bid ≤ 20%.
  2. The chosen option's median overshoot when over is below the current rule's value on the same lettings.
- **Reported only, not conditions**:
  - mean |ln|, P50 median error, the 1.00–1.13 share, each with a 90% interval;
  - erosion/drainage and other separately.
- **If confirmed**: merge into src, full rebuild, both guards, re-test by year, then go live.
- **If not confirmed**: not adopted, reported as it is, and the option is not tuned because of this result.

**Risks and limits stated in advance.**
- These candidates were designed after looking at the 2024–26 lettings again and again, so the development results are optimistic. The locked test set took no part in these experiments; its result is the one that counts.
- On 3 decoy seeds (59 training lettings held out each), the experiment sub-task found V2+D1+W36's P80 under-share 1.7–5.1 points above the current rule every time, once reaching 23.7%. So it may fail condition 1 on the locked test set.
- The locked test set has about 58 lettings; the 90% interval of the under-share is about ±8 points.

### Addendum to registration 2 (2026-11-20 02:2x, before the re-test results on the new data came out)
The user added at 02:2x: 80% is not a hard gate either; the higher the not-under share the better (going from 70% to 75% also counts as progress); a median overshoot within 13% is ideal, and 15% or 18% is progress as long as it beats what came before. Both goals are "the better, the better", and they trade off: raising P80 in the same model gives fewer unders and more overshoot.

**Neither the pick rule nor the confirmation conditions change**: they already say "the floor does not get worse and the overshoot gets smaller", which is what the user means.

**Only one report item is added**, to compare fairly how accurately each option estimates; it does not enter the verdict: "the overshoot at the same not-under share".
- **Method**: for the current rule and each candidate, on the lettings each has scored, set the P80 multiplier directly so that exactly 75% / 80% / 85% of the lettings are not under, and report the median overshoot when over at that point. Municipal and TxDOT separately.
- **Basis**: this is set directly on the scored lettings, not forward in time; it only serves to compare the spread of the options.

### Development-stage pick for registration 2 (2026-11-20 02:3x, computed mechanically by the rule above, locked test set not touched)
Data: the snapshot after the 02:01 rebuild (experiment sub-task `handoff/scratchpad/p50fix/round3/out_snap2/`, `round4/out_snap2/`).

- **Current rule**: municipal under 15.5%, median overshoot when over 69.0%; TxDOT under 16.3%, median overshoot when over 51.3%.
- **Qualifying**: W36, V2, V2+D1, V2+D1+W36, V2+C4+D1, V2+C4+D1+W36.
  - C4+D1 does not qualify: municipal 2026 under 26.7%, above its cap of 25.2%.
- **Municipal median overshoot when over**:
  - V2+C4+D1: 55.1% (lowest)
  - V2: 56.9%
  - V2+C4+D1+W36: 57.0%
  - V2+D1+W36: 58.7%
  - V2+D1: 59.3%
  - W36: 63.4%
- **Tie**: V2, V2+C4+D1 and V2+C4+D1+W36 are within 2 points of the lowest; by the tie rule the simplest is chosen.
- **Chosen: V2 (the comparables blend).**
  - Municipal: under 16.1% (within the cap every year), median overshoot when over 56.9% (current 69.0%), mean |ln| −0.035 (90% interval −0.054 ~ −0.016).
  - TxDOT: under 16.4%, median overshoot when over 36.3% (current 51.3%).
- **Next step**: per the registration, first extend the confirmation script to V2 (verified on the decoy only), then open the locked test set once.

### Result of registration 2 (2026-11-20 02:38, second opening of the locked test set)
- **How it was run**: `handoff/scratchpad/p50fix/confirm/v2d1w36_eval.py --variant V2 --confirm ...`
  - The data is that of the 02:01 rebuild, file for file identical to snapshot 2 used for the pick.
  - The code md5s all match those of the decoy verification.
  - The access log recorded the "start" and "done" rows automatically.
  - Output: `reports/measurements/holdout_confirm_2_V2_20261120.txt`, `handoff/scratchpad/p50fix/confirm/out/confirm_V2_20261120_023858.json`.
- **Scored**: 57 lettings (of the 59 registered keys, 4 had no usable price rows in the panel and 1 had under 90% of its items priceable).

| | Current rule | V2 | Difference (90% interval) |
|---|---|---|---|
| P80 below the low bid | 14.0% (8 lettings) | 17.5% (10 lettings) | +3.5 points (+0.0 ~ +8.8) |
| Median overshoot when over | 46.3% | 47.4% | +1.2 points (−7.8 ~ +7.7) |
| Mean \|ln\| | 0.309 | 0.280 | −0.029 (−0.069 ~ +0.015) |
| P50 median error | 21.2% | 15.6% | −5.6 points (−10.0 ~ +0.9) |
| Within 1.00–1.13 | 8.8% | 7.0% | −1.8 points |
| Median P80 multiplier | 1.550 | 1.495 | −0.055 |

- **Verdict: condition 1 passes** (17.5% ≤ 20%), **condition 2 fails** (median overshoot when over 47.4%, not below the current rule's 46.3%). **V2 is not confirmed and not adopted.**
- **Facts recorded as they are (described only; the option is not tuned on them)**:
  - V2's P50 is more accurate on unseen lettings: median error 21.2% → 15.6%, close to the engineers' 15%.
  - But the P80 multiplier got smaller, 2 more lettings came in under, and the overshoot did not shrink. This agrees with the development finding that "at the same not-under share, V2's municipal spread is about the same as the current rule's".
  - Erosion/drainage, 9 lettings: neither came in under; V2's median overshoot when over 41.2%, the current rule's 94.5%. Too small a sample; described only.
- This is the second opening of the locked test set. Later decisions need their own registration; since the locked test set has now been opened twice, the next confirmation should preferably also use new lettings after 2026-11-20.

## 3. Confirm the pricing fix chosen in round 5 on the locked test set (registered 2026-11-20 03:1x, before the parser 3b rebuild)
**Background**: round 5 (`handoff/scratchpad/p50fix/PREREGISTRATION_ROUND5.md`) chose F2 on snapshot 2: pure-removal items match only pure-removal history, all other items only non-removal history.
- Municipal: median overshoot when over 69.0% → 60.4%, under 15.5% → 16.1%, mean |ln| −0.015 (interval −0.028 ~ −0.003).
- TxDOT: 48.8% vs 51.3%.
- Report: `reports/p50_fix_round5.md`.

**Which data decides.** The item-classification fix (parser 3b) is in progress; a full rebuild follows and the data will change. Per the round 5 registration, the decision goes by the latest data:
- After the rebuild, round 5 is re-run unchanged on the new snapshot (the same 4 candidates, the same rule).
- What goes to confirmation is the option the round 5 rule chooses on the new data (possibly still F2, possibly another).
- If none qualifies on the new data, the locked test set is not opened.

**Confirmation method (opened once, recorded in the access log).**
- **Script**: the confirmation script in `handoff/scratchpad/p50fix/confirm/` scores the current rule and the chosen option on the same held-out lettings (paired). The script first gets a path for this option (a patched copy of src) and is verified on the decoy only: the current rule identical to holdout_eval letting by letting, all leak checks passed, the confirmation flow run end to end. Only after that verification is the set opened.
- **Confirmation conditions** (as in registration 2), both must hold:
  1. The chosen option's P80 below the low bid ≤ 20%.
  2. The chosen option's median overshoot when over is below the current rule's value on the same lettings.
- **Reported only**: mean |ln|, P50 median error, the 1.00–1.13 share, the comparison with the engineers (overshoot at the same not-under share), each with a 90% interval; erosion/drainage and other separately.
- **If confirmed**: apply the patch (e.g. F2's `round5/patch_F2.diff`), full rebuild, both guards, re-test by year, then go live.
- **If not confirmed**: not adopted, reported as it is.

**Notes.**
- The locked test set has been opened twice (registrations 1 and 2); this will be the third time.
- The current rule's results on these lettings have been seen twice (registration 1: 13.8% under; registration 2: 14.0% under, 46.3% overshoot), but the chosen option has never been computed on the locked test set.
- About 57 lettings, wide intervals: about ±8 points on the overshoot difference, about ±5 points on the under difference.

### Addendum to registration 3 (2026-11-20 04:0x, before the round 5 re-run on snapshot 3 started)
- **The rebuild used parser .24c, not just 3b**:
  - .24 (3b): items classified by their head noun; "remove and rebuild" filed by what gets built; new families for box culverts and complete street lights.
  - .24b: material supply (delivered to a yard or on site, picked up at the plant, material only) goes to other, so supply contracts stay out of the construction pool.
  - .24c: milling and thin treatments (tack/prime coat, seal coat, microsurfacing, crack sealing) split out of the asphalt pavement family.
- **All three change only the item classification**; the estimator code is unchanged (`src/estimate_low_bid.py` md5 `aeb59952…`). This entry was written before the re-run on snapshot 3 started, before any candidate's result on the new data was seen.
- **Already seen**: the current rule scored forward by year on the new data (locked test set not touched). Municipal 2024–26: median overshoot when over 56.5%, under 16.5%.
- **Snapshot 3**: `handoff/scratchpad/p50fix/clean_snapshot_3/`, master_panel `ebedee86…`, file list in `handoff/scratchpad/p50fix/SNAPSHOT_3.txt`.
- Everything else as in the original registration 3.

### Result of registration 3 (2026-11-20 04:22, third opening of the locked test set)
- **Lettings**: 56 scored by both sides. B scored 57, the variant 56; the 1 scored by B only: F1 does not price a line without a sized cell, so this letting fell below 90% priced.
- **B (current)**:
  - P80 below the low bid 9/56 = 16.1%;
  - median overshoot when over 35.9%;
  - mean |ln| 0.290, P50 median error 18.4%;
  - within 1.00–1.13 16.1%, P80 multiplier 1.396.
- **V2+F1+F2**:
  - P80 below the low bid 11/56 = 19.6%;
  - median overshoot when over 31.0%;
  - mean |ln| 0.262; P50 median error 13.6%, 4.8 points below B (90% interval −9.7 ~ −0.1);
  - in the band 12.5%, P80 multiplier 1.311.
- **Verdict**:
  - Condition 1, P80 under-share ≤ 20%: 19.6%, met, just inside the line (11 lettings; 20% of 56 lettings is 11.2).
  - Condition 2, median overshoot when over below B: 31.0% < 35.9%, met.
  - **Both met: confirmed.**
- **The intervals are all wide**:
  - overshoot difference −4.9 points (−14.0 ~ +7.9);
  - under difference +3.6 points (−3.6 ~ +10.7);
  - mean |ln| difference −0.028 (−0.069 ~ +0.017).
- **By group (reported only)**:
  - On the 9 erosion/drainage lettings the variant is worse: under 0% → 11.1%, overshoot 59.3% → 67.2%, in the band 22.2% → 0%.
  - Other 47 lettings: under 19.1% → 21.3%, overshoot 34.7% → 30.3%.
  - The year-by-year table is in the log.
- **Next step (per registration 3)**: merge into src, full rebuild, run both guards, re-test by year, then go live.
  - V2+F1+F2 has no ready-made patch; the comparables method (V2), F1 and F2 must first be brought into src.
  - Once merged, check letting by letting that it matches the implementation used for the confirmation: both the forward scores and the decoy must agree.
- **Records**:
  - Log: `reports/measurements/holdout_confirm_3_V2F1F2_20261120.txt`.
  - Two access-log rows: start 04:21:37, done 04:22:51.

## 4. T1 (TxDOT only) goes live on forward evidence; prospective check on new TxDOT lettings after 2026-11-20 (registered 2026-11-20 05:4x, before it was applied)
**Background.** Round 6 chose T1 on snapshot 4, against the current production V2+F1+F2 as baseline (registration `handoff/scratchpad/p50fix/PREREGISTRATION_ROUND6.md`, md5 `ddc945cb…`; report `reports/p50_fix_round6.md`).
- **What T1 is**: on TxDOT only, it adds C4 (mobilization priced as a share of the schedule, exact matches first) and D1 (a competition discount that varies with job size). Municipal is unchanged letting by letting.
- **TxDOT 2023–26, 796 lettings**:
  - median overshoot when over 37.2% → 31.3% (−5.9 points, 90% interval −8.0 ~ −3.0);
  - mean |ln| −0.027 (−0.034 ~ −0.020), P50 median error 20.2% → 16.6%;
  - P80 under-share 16.7% → 16.0%. 2026 is 22.5%, against a per-year cap of 23.6%.

**Why it is not confirmed on the locked test set.**
- The locked set holds municipal lettings only, and T1 leaves municipal unchanged letting by letting, so a confirmation could show nothing.
- TxDOT has no locked set.
- T1's fitted quantities come from TxDOT rows only. Checked at 05:40: neither TxDOT source (txdot, txdot_archive) has a single internal or public_test row.

**Decision.**
- T1 goes live under the rule registered for round 6.
- The switch `estimate_low_bid.pricing_rule(txdot_c4d1=False)` can turn it off at any time.

**Prospective check (to be run later; written before any new letting appeared).**
- **When**: once 100 lettings bid after 2026-11-20 have appeared in the DFW TxDOT data, or on 2027-03-31, whichever comes first.
- **How**: with the production of that time, on the same new lettings, score forward in time with T1 on and with T1 off.
- **Two conditions**:
  1. T1 on: P80 below the low bid ≤ 20%;
  2. T1 on: median overshoot when over below T1 off.
- **If not met**: if either condition fails, T1 is switched off and the result is reported as it is.

**Known risks (written down, to be looked at then).**
- TxDOT 2026 P80 under-share 22.5%.
- Bridge maintenance, 72 lettings: under-share up from 20.8% to 26.4%.

## 5. Confirm MC from round 10 (municipal lines priced from municipal price cells) on the locked test set, after a re-run under the "not covered" scope on the .26 data (registered 2026-11-20 10:3x, before the re-run and the tooling started)
**Background.** Round 10 chose MC on snapshot 5. Registration `handoff/scratchpad/p50fix/PREREGISTRATION_ROUND10.md`, md5 `0eccbdc5…`; report `reports/p50_fix_round10.md`.
- **What MC is**: when a municipal line is priced, the same key first uses a cell holding municipal lettings only (at least 3 municipal lettings before the pricing date), and the mixed cell only when that is not enough.
- **Result on the 145 municipal lettings of snapshot 5**:
  - median overshoot when over 51.3% → 45.0% (−6.3 points, 90% interval −8.5 ~ −0.2);
  - P50 median error 26.0% → 20.6%;
  - under-share unchanged at 18.6%, 26.8% in 2026.
- **TxDOT**: unchanged letting by letting.

**Two things that change before this confirmation (both unrelated to MC itself).**
1. **Parser .26 is live.** The rebuild finished at 10:31, the guards passed, master_panel `ed742d89…`. The current rule on the new data: municipal 144 lettings, overshoot 48.6%, under 18.1%, error 24.5%.
2. **The "not covered" scope (the user's decision at 10:00).**
   - The rule is in `src/coverage.py`, md5 `5d14769b…`, fixed before any scoring under the new scope.
   - Uncovered lines are not priced.
   - When judging accuracy: our covered estimate against the low bid minus the low bidder's own amounts on those lines.
   - A sensitivity version is computed as well: deducting each line's median bidder amount instead.
   - It starts only once the patch is installed.

**What decides.**
- After the scope patch is installed and the estimator rebuilt, snapshot 6 is made.
- On snapshot 6, round 10 is re-run exactly as registered: B and the three candidates MC, CORE and CORE+LS, the same rules (a)(b)(c) and the same tie rule, B and the candidates all under the new scope.
- What goes to confirmation is the option the re-run chooses (possibly still MC, possibly another, or none at all).
- If none qualifies, the locked test set is not opened.

**Confirmation method (opened once, recorded in the access log).**
- **Tool**: the new paired mode of src/holdout_eval.py (a switch comparison: B = switch off, variant = switch on), scoring both sides on the same held-out lettings.
- **Verify first**: on the decoy only. It must:
  - with the switch off, match holdout_eval's earlier scoring letting by letting;
  - pass every leak check;
  - run the flow end to end.
  Only after that verification is the set opened.
- **Confirmation conditions** (as in registrations 2 and 3), both must hold:
  1. The chosen option's P80 below the low bid ≤ 20%;
  2. The chosen option's median overshoot when over is below the current rule's value on the same lettings.
- **Reported only**:
  - mean |ln|, P50 median error, the 1.00–1.13 share, each with a 90% interval;
  - erosion/drainage and other separately;
  - the result when the uncovered lines are deducted at each line's median bidder amount;
  - the result under the full-price scope (uncovered lines not taken out).
- **If confirmed**: apply the patch, full rebuild, run both guards, re-test by year, then go live.
- **If not confirmed**: not adopted, reported as it is.

**Notes.**
- The locked test set has been opened 3 times; this will be the 4th. MC has never been computed on the locked test set.
- About 56 lettings, wide intervals.
- The locked set holds municipal lettings only, which fits MC changing municipal only.

### Addendum to registration 5 (2026-11-20 10:40, after the scope patch went into src, before snapshot 6 and the re-run)
**Two changes.** Both were decided after seeing the forward scores the sub-task computed on snapshot 5 under the v1 scope. Recorded here as it is: those scores included the current rule's and MC's.

1. **The uncovered rule becomes v2: `src/coverage.py`, md5 `1d3f16a9…`** (v1 was `5d14769b…`; the original is kept at `handoff/scratchpad/coverage_scope/coverage_v1_5d14769b.py`).
   - Only five "wrong meaning" cases were changed, all pointed out by the integration sub-task while reading schedules line by line:
     - cleaning and painting a bridge's railing was classed as a prefab bridge;
     - a temporary (rented) generator was classed as equipment;
     - relocating or resetting existing equipment was classed as the equipment itself;
     - allowance wording such as contingency and unforeseen was classed as equipment;
     - HVAC inside a building was classed as the whole building.
   - In addition, the trailing form of "bridge" must now be directly adjacent.
   - The seven classes and every other rule are unchanged; the self-test grew from 29 to 36 cases, all passing.
2. **Rule (b) for "not covered as a whole" is switched off.** It used to say "if one LS line describes the whole facility, the whole letting gets no estimate".
   - Because of a single line, "lift station complete in place", in Mesquite 25-8014, it judged that whole 248-line, $34 million schedule unfit for an estimate.
   - The user's principle is "price what can be priced, list what is not covered, judge accuracy on the covered part only". That holds for facility-centred projects too.
   - Rule (a) stays: when uncovered lines are more than half of the schedule, the whole letting gets no estimate.
   - In `src/estimate_low_bid.py` this is implemented as `WHOLE_FACILITY_RULE = False`. Each uncovered line is still flagged `whole_facility`.

Everything else as in the original registration 5: snapshot 6 is made after the estimator is rebuilt under the new scope; round 10 is re-run unchanged; the confirmation uses the paired mode; the same two confirmation conditions.
- **Time correction (added 10:5x)**: the comments in `src/coverage.py` v2 and in `src/estimate_low_bid.py` say "~11:00"; the actual change was at about 10:38 (the two files' modification times are 10:39:24 and 10:39:53). The comments were left as they are so as not to change the registered md5s.

### Registration 5: re-run result and the option sent to confirmation (2026-11-20 11:2x, before the locked set was opened)
- **Re-run set-up**: snapshot 6, the "not covered" scope, re-run exactly by the rule registered for round 10 (report `handoff/scratchpad/p50fix/round10b/`).
- **Result**: all three candidates qualify. By the registered tie rule, **CORE** (MC + E + F1P + T2 + D) is chosen, not MC.
  - Why: CORE has the lowest municipal median overshoot (42.0%), CORE+LS (43.3%) is within 2 points, and the simpler of the two is taken.
  - MC is 44.4%, 2.3 points above CORE, which is not a tie.
- **CORE vs B (the same 142 municipal lettings)**:
  - median overshoot when over 49.0% → 42.0% (−7.0 points, 90% interval −12.0 ~ −3.0);
  - P50 median error 24.1% → 19.8%;
  - under-share unchanged at 18.3%, 25.0% in 2026 against a cap of 25.5%: one letting short of breaking it.
- **Patch**: `handoff/scratchpad/p50fix/round10b/patch_CORE.diff`, md5 `1c5d3f99…`.
  - Applied to src with all five switches off by default; production unchanged.
  - The preset `round10_core` turns the five switches on at once.
- **Checks before confirmation (11:1x–11:2x)**:
  - self-tests all pass; the holdout self-test all passes;
  - the full guard PASSED, 8 of the 9 quantities in section 1e GATE HELD;
  - the paired rehearsal on decoy 7 normal;
  - the access log untouched.
- **How it is confirmed**: `--compare-switch round10_core --confirm`. B has all five switches off, the variant all on.
- **Confirmation conditions unchanged**:
  1. The variant's P80 below the low bid ≤ 20%;
  2. The variant's median overshoot when over is below B's value on the same lettings.
- **Known risks** (written down before opening):
  - under the full-price scope CORE fails the 2026 floor;
  - 1 more letting under in the erosion/drainage group;
  - T2 turns a few lines back into unpriced;
  - on the decoys CORE's under-share goes up to 19.0%, so condition 1 may be tight.

### Result of registration 5 (2026-11-20 11:23, fourth opening of the locked test set)
- **Lettings**: 48 scored by both sides. B and the variant each scored 49, each with 1 letting scored by one side only.
  - Of the 59 keys registered at the draw, 46 were scored this time; another 3 scored keys are not in the registry because parser .25 renamed the keys; the files did not change.
- **B** (round10_core off, i.e. current production):
  - P80 below the low bid 9/48 = 18.8%;
  - median overshoot when over 30.4%;
  - mean |ln| 0.231, P50 median error 15.2%;
  - in the band 14.6%, P80 multiplier 1.391.
- **CORE** (round10_core on):
  - P80 below the low bid 14/48 = 29.2%;
  - median overshoot when over 33.4%;
  - mean |ln| 0.216, P50 median error 15.8%;
  - in the band 10.4%, P80 multiplier 1.310.
- **Verdict**:
  - Condition 1, under-share ≤ 20%: 29.2%, **not met**. The difference is +10.4 points, 90% interval +4.2 ~ +18.8, the whole interval above 0.
  - Condition 2, overshoot below B: 33.4% > 30.4%, **not met**.
  - **Neither condition is met: not confirmed, not adopted.** The five switches stay off by default; production unchanged.
- **Reported only**:
  - Slightly better accuracy: mean |ln| −0.016 (−0.031 ~ −0.001).
  - But the P80 multiplier fell by 0.08, and on the locked set 5 more lettings came in under.
  - Erosion/drainage, 6 lettings: under-share 0 on both sides, overshoot 60.3% → 50.8%.
  - Other, 42 lettings: under 21.4% → 33.3%.
  - Sensitivity with the uncovered lines deducted at each line's median bidder amount: B 18.4% / 30.5%, CORE 28.6% / 30.9%; the same conclusion.
- **Records**:
  - Log `reports/measurements/holdout_confirm_4_round10_core_20261120.txt`;
  - Two access-log rows: start 11:22:53, done 11:23:42.
- **Note**: the locked test set has now been opened 4 times. Future confirmations use new lettings after 2026-11-20, as a prospective check.

## 6. Municipal changes waiting for a prospective check (recorded 2026-11-20 19:0x)

The locked test set has been opened four times. Municipal changes chosen in later development rounds are therefore confirmed on new DFW municipal lettings dated after 2026-11-20. Until a change passes its check, it stays in the code with its switch off.

| Round | Change | Preset | Registration | Development result (municipal, forward) |
|---|---|---|---|---|
| 11 | Municipal price cells + E + F1P + D (CORE without T2) | `round11_core_not2` | handoff/scratchpad/p50fix/PREREGISTRATION_ROUND11.md, md5 2ffee091… | snapshot 7 (parser .27), 2024–26: median overshoot 51.4% → 38.2%, P80 under 16.6% |
| 13 | L: municipal price level for band / family-tier cells dominated by TxDOT lettings | `round13_l` | handoff/scratchpad/p50fix/PREREGISTRATION_ROUND13.md, md5 1710c5e7… | snapshot 9 (parser .29), 2024–26: overshoot 43.1% → 38.0% (−5.2, 90% interval −7.2 to −1.1), P80 under 16.1% → 14.5%, P50 error 19.1% → 19.4% |

**Rule, for each change separately:**
- **When:** once at least 40 DFW municipal lettings dated after 2026-11-20 are in the data and scorable.
- **How:** score production and the change forward on them, paired, with the code and data of that time.
- **Pass:** the change's P80 is under the low bid on at most 20% of them, AND its median overshoot is strictly below production's on the same lettings.
- **Outcome:** a change that passes is switched on; a change that fails stays off and is reported as is.

**Notes on round 13:**
- **Where the gain comes from:** mostly from a tighter forward P80 margin (multiplier 1.309 → 1.288). With production's margin it is −3.1 points, and that interval crosses 0. This is why it waits for the check.
- **Report jobs of $2M and more separately.** On those its under-rate went from 25.9% to 27.6% in development.
- **Round 13's other arms:** shrinking prices backed by a single earlier letting (T), and T+L, did not pass or tied and lost on simplicity.
- **Round 12** (lump sums as a share of the job, size-classed centring) did not pass and is not pending.
- **Records:** reports/p50_fix_round13.md; patch applied 2026-11-20 19:0x with the switch off. Production is unchanged.

### Round 18: switched on, with a prospective switch-off check (2026-11-21 15:20)

Rounds 16 + 17 (X1/B80): bidder counts above the discount table's top bin priced off the top bin, the abandonment identity (AB), and the P80 divisor and guard level from production B (B80). Registered in handoff/scratchpad/p50fix/PREREGISTRATION_ROUND18.md (md5 2c1e265f5ae9333bb275111cdd00bc1b) and judged by ADOPTION_RULE_2026-11-21.md. It passed track T and track U on the common lettings: municipal overshoot 45.78% -> 45.54% (90% interval -1.05 to -0.05), P50 error 20.20% -> 19.88%, P80 under 14.29% in every year, TxDOT unchanged. Unlike rounds 11 and 13 it is on in production (src md5: estimate_low_bid bd51fdad..., item_tokens 79738052...), because both markets passed on forward evidence and the floor did not move.

**Switch-off rule:** once at least 40 DFW municipal lettings dated after 2026-11-20 are scorable, score production and production B (round 18's switches off) forward on them, paired. Switch round 18 off in both markets (discount() and tokens() are shared) if its P80 is under the low bid on more than 20% of them, or its median overshoot is more than 1.0 point above B's on the same lettings. Lettings only round 18 prices (above the top bin) are reported apart. Records: reports/p50_fix_round18.md; handoff/scratchpad/p50fix/round18/INSTALL.txt.

### Round 20 arm G: switched on, with a prospective switch-off check (2026-11-21 18:12)

Parser .34b = .33 plus three family rules: flashing beacons, flashers, crosswalk lights, signal poles and mast arms to traffic signals; markings named by their line (stop bars, solid / broken / double yellow, raised markers, marked crosswalks) to pavement marking; signs named by an MUTCD code or a panel size to signing. The estimator is unchanged. The round's registered arms (F, F+C) failed; G was registered afterwards, in handoff/scratchpad/p50fix/PREREGISTRATION_ROUND20_ADDENDUM1.md, on the same data, so it is post hoc. It passed track U of ADOPTION_RULE_2026-11-21.md on the common 234 municipal lettings: P80 under 32 lettings, unchanged in each year; median overshoot 45.25% -> 44.56%; P50 error 19.67% -> 19.43%; line sum |ours - median bid| -0.25% (90% interval -1.80 to -0.19 $M). TxDOT was not worse. Because it is post hoc, it carries the same check as round 18.

Installed 18:12-18:52 by a full rebuild (every guard passed). The five external-only reference and calibration files match the round's own run byte for byte (price_reference e035c3f3, band fa3e6779, tokens b383dcc3, calibration 1c0b73f0, TxDOT T1 10acbe45). Production forward, full windows: municipal 2024-26, 235 lettings, P80 under 13.62%, median overshoot 44.80%, P50 error 19.52%; TxDOT 2023-26, 807 lettings, 15.49% / 31.46% / 16.10%, as INSTALL.txt expected. Algorithm work stopped after this round (user, 2026-11-21 18:23).

**Switch-off rule:** once at least 40 DFW municipal lettings dated after 2026-11-20 are scorable, score production and production with the .33 parser (a full rebuild each) forward on them, paired. Revert the parser file and rebuild if the P80 under rate is above 20%, or the median overshoot is more than 1.0 point above the .33 run's on the same lettings. Records: reports/p50_fix_round20.md; handoff/scratchpad/p50fix/round20/install/G/INSTALL.txt.

