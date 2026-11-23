# Handoff

Written 2026-11-21 and updated 2026-11-22 for whoever maintains this project next. `AGENTS.md` holds the standing rules and commands. `docs/HISTORY.md` records the experiments, `docs/METHOD.md` describes the methods, and `docs/DATA.md` describes the sources.

## Current evaluation scope

On 2026-11-22 the owner resumed iterative municipal pricing experiments and
excluded lump-sum pay items from unit-price comparisons. A lump sum covers the
scope of a particular project, so two prices for one LS may buy different work.
Apply the exclusion to both the baseline and each candidate before calculating
line errors, ranking error causes, resampling, or judging an improvement.
Use the pay item's unit, including source spellings such as `L.S.` and
`Lump Sum`. A quantity of one by itself does not identify a lump sum.

For **project OPCC accuracy**, issue each full-schedule forecast first. Score
covered, non-LS base rows. Determine the low bidder from the complete original
base bid, then use that bidder's extensions on the retained rows. A printed zero is
a price. A missing winning amount, missing eligible model price, or empty or
nonpositive comparison subtotal makes the job unscorable. Keep single EA rows
and covered low-confidence prices. Count the union of LS and Not Covered once.
Use the same row IDs for the incumbent, candidate and any engineer benchmark.

Run `.venv/bin/python src/opcc_accuracy.py receipts.jsonl` for a project
summary. Provide one public project per line with a unique `project_key`,
`headline` (P50/P70/P80 dollars keyed by lowercase name) and `lines`. Each line
has a stable `row_id`, original and normalized units (`unit_raw`, `unit`),
its estimator `status`, and `winner_amount` in dollars from the original low
bidder. Supply `market_amount` on priced rows and `fixed_amount` on owner-set
rows. Optional `engineer_amount` must cover the same retained lines. For each
percentile, hold specified amounts at face value and distribute the balance
among all priced lines proportional to their market amounts before excluding
LS and alternatives. This preserves the forecast made with the original project size and mix.
The error is (retained estimate / retained low-bid amount) minus one. Each
scored letting contributes one observation. Report held jobs and the counts
behind the P80 floor. Historical forecasts without P70 still count for each
available percentile. For model comparisons pass `--compare candidate.jsonl`;
the report identifies unequal project scopes and uses a common denominator for
each percentile. Historical whole-bid figures retain their original
definitions and must be replayed before comparison with these results.

For new original-tab audits, mark each issued position with `bid_scope` set
to `base` or `alternate` and pass `--verify-base-bid` to the scoring command.
Include all originally issued rows and their model allocations, even if an
alternate was eventually awarded. The report distinguishes complete base-only
issues from base amounts allocated out of a mixed issue. Older unlabelled
receipts appear under `unspecified`. A partly labelled or unknown section
holds the job. The strict check requires the frozen `quantity` and the
printed `winner_quantity` and `winner_unit_price` on every retained row,
as well as the issued `model_unit_price` on priced rows. Record `unit`
and `winner_unit` in the same canonical payment units, preserving scale.
Both extensions must reconcile within half a cent. Differences in quantity,
payment unit or extended dollars hold the project and identify the row.
An explicitly reported common-row sensitivity can accompany this hold.
Reissuing a base-only schedule requires running the estimator again.

The historical municipal development replay initially scored 174 of 235 schedules.
It used the original low bidder’s panel quantities and unit prices. Of 7,526
positions on scored jobs, 889 were LS, 12 were Not Covered, and five met both
conditions. The other 6,630 positions entered both sides. Two jobs with verified
zero-quantity positions have zero extensions on both sides; an unknown quantity
has no such interpretation. The incumbent met or exceeded actual on 81/174 at
P50, 112/174 at P70 and 134/174 at P80. Median absolute errors were 21.0%,
25.9% and 38.8%. On these same jobs the earlier scope gave P80 coverage of
151/174. These counts predate the original-document base-scope audit and
cannot establish base-only coverage. The original Watauga parking lot,
Watauga Park Vista and Aubrey Phase 2A W5 tabulations identified priced
alternates in the historical comparison. Watauga's Water/Wastewater CIP
Group 2 also contains two fractional VLF quantities that were rounded in
the archive. The low bidder can be correctly identified while the rows on
both sides still differ from the original base. The historical score falls
below the 80% floor. Historical bidder counts and
uncertain source availability limit the interpretation. Among 12 scored projects with items directly affected by using three recent references
in place of one, that choice reduced the sum of absolute P70 errors by $6,331.51.
P80 met or exceeded the comparable bid subtotal on 134 of 174 for both policies. The local audit is at
`handoff/scratchpad/scoped_municipal_development/prior-zero-corrected-v2/`.

Five separate public city/county bid tabs also have independently reconciled
winning extensions and dated archived forecasts. All five estimates exceeded
the retained actual at each percentile. Only one had engineer extensions on
the same lines. Keep this small retrospective sample separate from the
development replay. Local receipts are in `handoff/scratchpad/scoped_opcc_replay/`.

`ui/build_item_accuracy.py` reproduces the previously published README and
UI tables under their original scoring definition, which included lump
sums. For a new experiment use the physical-unit eligibility check before
computing either arm's line metrics. The 8,700-line development receipts
are under `handoff/scratchpad/round23_latest_exact_blend/physical-results/`.

The historical engine reads a full-period quantity-band multiplier table.
Some reconstructed unit rates and project totals, including the Round 23
development calibration, can therefore use information from after the
letting. Label these historical comparisons accordingly. A strict
as-of-opening comparison also needs date-correct multiplier and calibration
inputs. The source audit is in
`handoff/scratchpad/external_project_totals/SHAPE_AUDIT.md`; the historical
evaluation code documents the fixed table in `src/holdout_eval.py`.

The Rockwall County Cedar Creek Dam No. 9 original form has a sealed
pre-opening forecast. Twenty-one of its 22 base items were priced, including
all 11 LS rows. Irrigation water has no matching rate. The two topsoil rows
also illustrate a worksheet limitation: the same three much smaller
projects supplied both prices until the original thickness and work scope
were entered. See `handoff/scratchpad/cedar_creek_prebid/RESULT.md`.
Final addenda and actual bids are pending. This case contributes no
completed prospective score.

The county's two printed topsoil depths had zero direct historical
matches after searching across item types and screening the quantity.
All recent candidate descriptions within the quantity range specified
4 inches, compared with 6 or 12 inches on the contract. The supplemental
statewide Van Zandt County Dam No. 103 original form also has a sealed
forecast. Its 25-item base includes 11 LS rows, 13 priced physical rows
and one unpriced irrigation-water item. County award ranking can include
duration. This case remains outside the DFW prospective cohort. See
`handoff/scratchpad/topsoil_public_source_search/RESULT.md` and
`handoff/scratchpad/van_zandt_cedar_creek_103_prebid/RESULT.md`.

The owner's subsequent standing request authorizes continued public-data
collection and iterative pricing experiments. Keep source collection,
independent evaluation and production adoption separate. The Round 23
recent-bid blend passed the development unit-price gate and remains a
candidate for independent confirmation. Rounds 24 and 26–49 have not justified
a change to it. Three agreeing recent projects reduced developmental unit
error on 21 positions, but its 90% interval included an increase. None of the
358 independently reviewed physical items on five external contracts had
three qualified references within two years. For pavement, check the priced
work as well as the unit. Two friction treatments and a reinforcing grid
reproduced a $1.69/SY estimate for a 2-inch asphalt surface whose original
low bidder priced it at $14.90/SY. The preceding city contract supported a
$17.00/SY forecast on that item alone. The opened target's three-bid median
was $18.13/SY. The native replay brought that project's P50 to within 0.84%
of the actual low bid. With forward calibration the Round 23 P80 count moved
from 134 to 135 of 174. See `handoff/scratchpad/round51_native_overlay/`.
A separate Austin contract, CLMC1159, now has seven prospective
unit-price forecasts sealed before its scheduled December 17, 2026 opening.
Round 25 verifies dates against originals. See the dated
entries in `docs/HISTORY.md`. The local worksheet and local P70 trial are
described below. Production still uses its previously adopted model.

## Round 21 result — 2026-11-22

Keep the original pricing model. The owner authorized a single round to test
the recent three-project price rule on municipal model inputs. The candidate
failed all three registered adoption tracks. The owner's local worksheet
starting-price rule remains in use.

On 235 matched municipal lettings in 2024–2026, the candidate's P80 fell below
the actual low bid on 30 lettings (12.77%), down from 32 (13.62%). Its median
overshoot rose from 44.80% to 46.54%. The aggregate absolute amount error of
9,598 matched unit-price lines rose 3.23%, from $307.81M to $317.75M. The median
P50 absolute error moved from 19.52% to 19.43%. TxDOT results and line traces
were identical, including all 807 matched lettings in the registered window.

The frozen baseline reproduced all 2,252 forward records field for field. The
baseline-against-itself check gave zero differences in nine groups. The new
public documents were scored first with the original model and strictly earlier
public data. Nine of ten base schedules that passed source review were scorable.
Their P80 fell short once, with 60.90% median overshoot and 38.12% median P50
absolute error. Four exact scopes had printed engineer estimates. The sample
supports a comparison of raw median errors and is too small for a matched
not-under comparison. Source checks still pending keep the new documents out of
training and the production bundle. The replay used public bids and kept the
locked holdout sealed. The original model remains on the public site.

See the Round 21 entry in `docs/HISTORY.md` for the decision, retries and local
evidence paths. The later standing request is recorded above.

## Worksheet reference-price update — 2026-11-22

The owner subsequently authorized processing the newly collected statewide
City/County documents and then a new optimization round after the outstanding
checks. This is a separate content-processing assignment, not an expansion of
the Data Resource Researcher collection role. The new database is built under
`data/processed/statewide_2026-11-22/`; original downloads remain unchanged.
Round 21's single candidate and adoption gates were registered before scoring
in `docs/METHOD.md`, section 1.6. This authorization supersedes the earlier pause
for that round only; it does not adopt a candidate in advance.

The owner separated worksheet unit prices from the project's P70 recommendation.
New worksheets use `basis: "history"`, with the median of three distinct matching
projects' bidder-median unit prices after a bid-year price adjustment. Only
precisely dated public projects in the same market, strictly before the target
date and within two calendar years, can become automatic references; projects
within one year rank first. No older project fills a missing third reference.

The selection works on distinct pay items and bidders, not the existing model
reference cell's pooled rows. `ui/history_prices.py` supplies selected source
snapshots; `ui/history_inflation.py` measures annual matched-item price changes
using only earlier public bids. Thin or missing index evidence leaves a blank
default. It never borrows another market or invents an inflation rate. The
current year's index is partial, and same-year bids receive no within-year
adjustment. Raw prices, factors, adjusted prices and source dates remain auditable.

The rule was fixed before verification in `docs/METHOD.md`, section 1.5. It is a
worksheet starting-price policy, not a refitted P50/P70/P80 model or an accuracy
improvement claim. Typed prices, manual selections, engineer-input requirements,
quotes and fixed allowances remain in force. Saved model-basis worksheets retain
their original meaning. The earlier P70 trial and baseline history follow below.

Verification completed on 2026-11-22: the 350-test UI regression suite completed
with no failures and 21 skips for unavailable optional components/source material; all 23 public-mode
tests passed, and the parser self-tests reported zero failures. Desktop and
390-pixel mobile views were checked. The known-answer P50, P70 and original P80
and every P50 line amount remain identical to the prior local trial. The actual
public examples were exported to Excel and PDF, with all PDF pages rendered,
selected source snapshots reconciled and formula caches checked.

Strict coverage is intentionally incomplete. The 43-line known-answer schedule
has no qualifying three-project automatic defaults under its historical bid
date. The current seven-line public check has one: excavation at $33.50/CY,
from three 2026 project medians; the other unfixed/unquoted lines remain blank.
These are wiring and audit checks, not measured accuracy improvements. The
worksheet's range and reference count now describe those selected recent
projects; the old model's accuracy remains separately labelled. Duplicate model
comparisons were removed from the narrow table footer because the worksheet
summary already shows them.

## Local P70 trial update — 2026-11-22

The owner requested a local P70 recommendation trial, registered before scoring
in `docs/METHOD.md`, section 1.4. The main page now shows **Expected low bid
(P50)** and **Recommended OPCC (P70)**. The original P80 remains available in
the API and workbook as a baseline, and existing P80 worksheet selections keep
their original meaning. The trial started locally and went live on Oracle Cloud
on 2026-11-22. The 70% figure is the calibration target. See the deploy log below.
The sections below retain the 2026-11-21 production baseline and its history.

The original public bundle files were preserved. The trial builder reconstructs
only `txdot_letting_spread.csv` and `municipal_letting_totals.csv` in an isolated
directory, with symlinks to the existing public tables. The full-period pooled
and municipal discount tables reproduced exactly: zero differences in `f`,
`lettings`, `p25`, or `p75`. The forward calibration then reproduced every
original calibration field, with 2,252 public records. No private data or locked
public holdout was opened or used.

The normal bundle setup installs the public `estimator_p70_calibration.csv` shipped in `deploy/calibration/` when its base tables match. The three aggregate rows are bound by SHA256
to the unchanged `estimator_calibration.csv`. Its municipal p30 is
`0.8629316945605551`; its TxDOT p30 is `0.8912989816216509`. Each is an empirical
30th percentile of forward estimate-to-actual ratios, centred using the
original published P50 level correction. P70 divides the existing P50 by this
percentile; it is not an interpolation between P50 and P80.

### Reproduce the local trial

To recompute the trial from scratch, run from the repository root with the public bundle installed. Use a fresh
output directory; the builder preserves existing outputs and refuses to
replace different contents. Keep source code unchanged during calibration.
`prepare` is an input reconstruction check; `calibrate` is the separately
registered forward run. The command chain stops on failure. The copy step
preserves any existing sidecar, and `cmp` requires it to match the successful
new build before running the wiring check.

```bash
p70_run="ui/.local/p70-trial-$(date -u +%Y%m%dT%H%M%SZ)"
.venv/bin/python ui/p70_trial_build.py prepare --out "$p70_run" &&
.venv/bin/python ui/p70_trial_build.py calibrate --out "$p70_run" \
    --registration docs/METHOD.md &&
cp -n "$p70_run/estimator_p70_calibration.csv" data/clean/estimator_p70_calibration.csv &&
cmp "$p70_run/estimator_p70_calibration.csv" data/clean/estimator_p70_calibration.csv &&
.venv/bin/python ui/integration_check.py ui/fixtures/takeoff_public_creek_bank.csv \
    --market municipal --bidders 5 --bid-date 2025-03-10
```

The successful trial's known answer is **P50 = $765,836.50,
P70 = $887,482.17, P80 = $1,043,089.75**, with `IDENTICAL` between the API and
estimator paths. The original P50 and P80 amounts are unchanged. This fixture
checks wiring; its own bids are in the history, so it is not an accuracy test.

Verification: the UI suite completed 321 tests with 21 skips and no failures;
all 23 public-mode tests passed. The actual 43-line example's Excel and two-page
Letter PDF were rendered and checked. The workbook retains all three model
figures and 42 valid formula caches; four lines awaiting an engineer's price
remain blank rather than being silently filled.

### Forward diagnostic and limits

The aggregate diagnostic is saved locally at
`ui/.local/p70-trial/p70-forward-check.json`, with its input checksum. It replays
the generated public `forward-records.json`; it does not rerun line pricing.
For each market and date, it uses at least 40 records dated strictly earlier,
recomputes `_centre(prior)`, and takes
`sorted(centred_ratios)[int(.30 * (n - 1))]`. All records on the same date use
the same earlier history. The 2,163 records carrying an original P50 reproduce
that P50 exactly, with maximum absolute difference zero. Both reported windows
have a complete original P80 comparison on the same lettings.

- **Municipal, 2024-01-01 through 2026-12-31: 235 lettings.** P70 is under the
  actual low bid on 49 lettings (**20.851064%**), with conditional median
  overshoot **32.911168%**. Original P80: 32 under (**13.617021%**), with
  conditional median overshoot **44.796202%**. The registered 30% P70 under-rate
  threshold **passes**.
- **TxDOT, 2023-01-01 through 2026-12-31: 807 lettings.** P70 is under on
  255 lettings (**31.598513%**), with conditional median overshoot
  **25.641039%**. Original P80: 125 under (**15.489467%**), with conditional
  median overshoot **31.461125%**. The registered 30% threshold **fails**.
  TxDOT's 2026 subset is under on 82 of 207 lettings (**39.613527%**).

Conditional median overshoot means the median of estimate / actual minus one
among estimates at or above actual. Actual follows the existing covered-lines
scope when a schedule contains uncovered work. These are forward historical
diagnostics on the existing public development records, not independent
held-out validation. The TxDOT failure is retained as measured; no alternative
percentile or guard was selected after scoring. Do not relabel the historical
P80 accuracy figures as P70 or claim demonstrated 70% future coverage.

## 1. Where the project stands

- **Product.** A web app that helps an estimator build an OPCC for Texas public works: it reads a takeoff, shows the earlier public bids behind every line, estimates P50 (expected low bid) and P80 (a not-under OPCC), and exports an OPCC as Excel and PDF. The estimator stays in charge: the model's number is one input among the references.
- **Public site.** https://129-146-109-44.sslip.io, in public mode (nothing stored, no AI reading).
- **At the original handoff.** The owner paused algorithm optimization on 2026-11-21 after Round 20. The subsequent requests and active evaluation scope are recorded at the top of this document.
- **Production estimator.** `src/estimate_low_bid.py` with: line pricing from layered reference tables, the comparables blend (V2), the size fallback (F1), the removal identity (F2), T1 on TxDOT (C4 + D1), the coverage scope (specialty items "not covered"), the bidder-count clamp and the abandonment identity (round 18), forward calibration with the adaptive guard (aim 0.16, cap 0.20) and narrow carry. Parser version `.34b` (round 20 arm G).
- **Historical accuracy on the original scope, including lump sums** (figures at the 2026-11-21 handoff):

| | Lettings | P50 median error | P80 under the low bid | Median overshoot | Overshoot at 70 / 75 / 80% not-under |
|---|---|---|---|---|---|
| City / municipal (DFW), 2024–26 | 235 | 20% | 14% | 45% | 30 / 32 / 34% |
| Municipal engineers' own estimates | 140 | about 15% | 33% | n/a | 20 / 21 / 25% |
| TxDOT (DFW counties), 2023–26 | 807 | 16% | 15% | 31% | 26 / 27 / 30% |
| TxDOT engineers' own estimates | 451 | 21% | 14% | n/a | 19 / 22 / 23% |

  The goal: P80 below the actual low bid on at most 20% of lettings (met under the historical scope), and within that the smallest overshoot (13% is the aspiration; engineers reach about 25% at the same not-under rate; the project is at 34% for cities). The city gap is almost all in estimating the bidders' typical price per job, not in the step from the median bid to the low bid.
- **Last deploy.** 2026-11-22 23:57 CST. Oracle Cloud serves commit `6fafa0e` with `bundle-2026-11-21` and its registered P70 calibration. See the deploy log at the end.

## 2. The first hour

```bash
git clone https://github.com/grintCactus/texas-public-works-opcc-estimator.git
cd texas-public-works-opcc-estimator
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
sh deploy/tools/install_hooks.sh
.venv/bin/python deploy/tools/fetch_bundle.py
.venv/bin/python ui/run.py --test
.venv/bin/python ui/run.py --no-browser      # then open http://127.0.0.1:8017/
```

### Known-answer check

The fixture `ui/fixtures/takeoff_public_creek_bank.csv` is one public letting as bid (a city creek-bank stabilization, 43 lines, bid 2025-03-10, 5 bidders). Its own bids are in the price history, so this checks the wiring, not the accuracy.

```bash
.venv/bin/python ui/integration_check.py ui/fixtures/takeoff_public_creek_bank.csv \
    --market municipal --bidders 5 --bid-date 2025-03-10
```

With bundle `bundle-2026-11-21` the app and the estimator's own entry point both give:

| | Value |
|---|---|
| P50 (expected low bid) | $765,836.50 |
| P80 (recommended OPCC) | $1,043,089.75 |

and the check ends with `IDENTICAL`. Different numbers mean a different bundle or changed pricing code: find out which before going on.

## 3. What is in this repository, and what is not

**In the repository**

- All pipeline code that runs on public data: harvesting, auditing, OCR, the parser, the panel and reference tables, the index, the discount, the estimator, the evaluation harness and the leak guards (`src/`).
- The web app and its tests (`ui/`).
- The deploy package files, server setup and tools (`deploy/`).
- The registry of the locked public test set and of every registered decision (`data/test_holdout/`).
- These documents.

**Published elsewhere**

- **The public data bundle**: the built tables the app reads, public lettings only (350 MB unpacked). A release asset of this repository; `deploy/bundle.lock.json` pins it and `deploy/tools/fetch_bundle.py` gets it.
- **The original public documents**: https://github.com/grintCactus/texas-public-works-bid-tabs (bid tabulations and award documents, with each file's source page).

**Only on the owner's machine** (never in a repository)

| What | Why it stays local |
|---|---|
| A private test set: bid files a company supplied, their rows in the full tables (`source_set == "internal"`), and the reports and scripts that score them | Confidential. They never train or fit anything; they exist to test the public model. |
| `data/local_config.json`, `.git/company_patterns.txt`, `.git/scan_private.json` | The private configuration and the patterns the guards scan for. |
| Four company-only scripts that `rebuild_all.sh` skips when absent: `company_twin_scan.py`, `qc_report.py`, `estimate_accuracy_benchmark.py`, `opcc_analysis.py`; and research files (`price_model_v2.py`, `provenance.py`, `opcc_benchmark.py`, `company_test_v2.py`, `parse_dol_wages.py`, the superseded `ui/pricing.py`) | They read or name the private test set, or are superseded research. |
| The full built tables (`data/clean`, with test rows), the working corpus (`data/external`, `data/txdot`: about 2,170 public files in 83 owner folders, plus raw downloads) | Size, and the test rows. The public part is the bundle and the documents repository. |
| The locked public test set's rows (`source_set == "public_test"`) | They are public documents, but the bundle leaves them out so that nothing can be tuned on them. |
| Experiment reports (`reports/`, about 110 files, mostly in Chinese) and the working area of every round (`handoff/scratchpad/`: registrations, round scripts, data snapshots, scoring scripts) | Not reviewed for publication. `docs/HISTORY.md` summarizes them and names the report of each round. |
| The SSH key of the server and the cloud account | Credentials. |

**What this means in practice**

- Running the app, changing the page, the exports, the readers and the server, testing and redeploying: all possible from a fresh checkout.
- Rebuilding the tables from documents (`src/rebuild_all.sh`) needs a corpus under `data/external/` and `data/txdot/`. The documents repository holds the first upload (2,150 files, 2026-11-20); the files harvested on 2026-11-21 (about 65 public tabs) are on the owner's machine and not yet mirrored. A rebuild from the mirror alone therefore gives slightly different tables from production. The bundle is the production data.
- The private configuration also carries extra labels that mark an engineer's-estimate column in a few public tabs. A rebuild without it reads that column as a bidder in three public lettings. Ask the owner before relying on a rebuild made without it.
- Confirmations on the private test set can only be run on the owner's machine.

## 4. Open obligations

Registered in `data/test_holdout/REGISTRATIONS.md`. Each needs new lettings to be harvested, parsed and scored forward; none is due yet.

| What | Trigger | Rule |
|---|---|---|
| T1 on TxDOT (on in production) | 100 DFW TxDOT lettings bid after 2026-11-20, or 2027-03-31 | Score T1 on and off, forward, on the same new lettings. Keep T1 only if its P80 under rate is at most 20% and its median overshoot is below T1-off. Switch: `pricing_rule(txdot_c4d1=False)`. |
| Round 11 (`round11_core_not2`, off) | 40 DFW municipal lettings dated after 2026-11-20, scorable | Switch on only if P80 under rate at most 20% and median overshoot strictly below production's on the same lettings. |
| Round 13 (`round13_l`, off) | same 40 lettings | Same rule. Report jobs of $2M and more separately. |
| Round 18 (clamp + abandonment identity, on) | same 40 lettings | Switch off if its P80 under rate is above 20%, or its median overshoot is more than 1.0 point above production with those switches off. |
| Round 20 arm G (parser `.34b`, on) | same 40 lettings | Revert to parser `.33` and rebuild if the P80 under rate is above 20%, or the median overshoot is more than 1.0 point above the `.33` run's. |
| The forward-floor rule (guard aim 0.16, narrow carry) | lettings of 2027 | Out-of-sample check registered when it shipped (`docs/HISTORY.md`). |

Also owed after any data rebuild: the leak guards must pass, the README's figures and item tables are regenerated, the bundle is rebuilt and released, the site is redeployed (`deploy/README.md`, section B).

## 5. Known issues and ideas, by area

**Model (paused; listed so they are not rediscovered)**

- City lump sums (mobilization, traffic control, site preparation, SWPPP) are priced at about the same dollar amount on every job; bidders price them as a share of the job. Share pricing fixed the lines but not job totals (rounds 12 and 14): on small jobs it exposed unit-priced lines that run low. The two must be fixed together.
- The largest city misses come from weakly matched main items on large jobs, and from specialty items (now "not covered").
- City water pipe, asphalt and unclassified lines carry the most dollars still to gain (`README.md`, item tables).
- Recent years run higher than history: in 2026 the P80 under rate was 17% (municipal) and 23% (TxDOT).
- Do not spend rounds on centring windows, recency weights or segment levels: a level shared by all jobs cancels out of P80 (`docs/METHOD.md`).

**Parser and data**

- 263 parsed rows from three PDFs carry unmapped ligatures printed as `(cid:NNN)` (for example "ti", "tt", "ft" in one font). The text reads wrong in references and weakens matching for those rows.
- City data is thin for many item types; some cities publish bid tabs only behind vendor portals, which are not collected (registering as a vendor is the owner's decision).
- The documents repository is behind the working corpus by the 2026-11-21 harvests.

**Web app**

- "Check: weak match" lines list "Did you mean" suggestions whose first entry can be the line's own text when the same letting is in the history. Cosmetic.
- Long "Did you mean" lists could be collapsed; the owner has not decided.
- **AI mode is a placeholder.** The upload step shows "AI mode — coming later" with a disabled "Read by AI" button. The owner's intent: upload a job's engineering drawings and documents through the same drop area, and have an AI list the work items and quantities as a takeoff for the estimator to check. Nothing is built. The older "read a PDF with an AI model" code (`ui/ingest/claude_reader.py`) is off in public mode and folded into this placeholder.
- Project and phase history (keeping each design phase's OPCC and comparing phases) was discussed and not built.

## 6. How the owner wants the product to behave

These came from the owner's reviews; they are requirements, not suggestions.

- **Plain words.** No jargon, codes, abbreviations a contractor would not use, or text cut with "…". "Item type", not "family". Explanations are visible without hovering. Warnings are one sentence with what to do, and can be ticked off.
- **A fixed layout.** Rows and columns do not jump when a number changes. Long descriptions wrap; nothing is truncated.
- **The OPCC as estimators print it.** Title, project block (name, date, prepared by, project no.), then item no., description, units, quantity, unit cost, total cost; Sub-Total, Construction Contingency, Project Total; an interim-review box at the bottom right from Units to Total Cost, compact and left-aligned (the seal goes there on submission). US Letter, portrait, one page wide, a bold serif for headings, no inner grid lines in print. No company name anywhere: the blanks say "Company" and "TBPE Firm Registration No.".
- **Conventions.** Mobilization about 5% of the other items, rounded up to $500; a construction contingency is always present, 10% by default; picked prices are round numbers in steps that grow with the price (typed prices are never rounded); temporary erosion control and mobilization are the engineer's input, with the model's price offered as a quick pick.
- **One export.** Excel workbook and the same OPCC as PDF. Formulas also carry their values so previews that do not calculate show the right totals. The sheet must read well in any viewer: check a rendered image, not only the file.
- **P50 | P80 switch,** P80 by default. A not-covered line has exactly one number wherever it is typed.
- **Dates are the user's own day** (the page sends its local date; the server runs on UTC).
- **Uploads go in through one drop area.** No extra buttons or filters.
- **The user's prices are output only** and never feed any table.

## 7. Working rules learned the hard way

- **Judge the floor forward in time.** Leave-one-out hid a downward drift; constants that looked clean gave 24% under on recent lettings.
- **An adaptive rule aimed at the floor sits on it** and breaches about half the time: the guard aims at 16% with a 20% cap.
- **A parser change that corrects ground truth** (a low bid, a date, a label) is judged on the lettings whose truth did not change.
- **A symptom count is not a defect count.** Split misses by cause before fixing: data error, real underestimate, specialty item.
- **A minority unit can mark a different product** (seeding by the acre vs sod by the square yard). Do not pool units blindly; conversion is shown as a hint only.
- **Abandon is not remove, and neither is new work.** Pipe abandonment (plug, grout in place) has its own history.
- **Fixing one biased line class can expose another.** Check job totals, not only the lines.
- **Never over-sample the scarce, important work type into a holdout.** The first test-set draw did, and was retired before use.
- **A public harvest can bring in a copy of a private test file.** On the owner's machine the twin scan runs before every rebuild. A harvest made elsewhere must be checked there before its tables are used for a test on the private set.
- **Python on macOS from python.org ships no CA bundle:** use `SSL_CERT_FILE=/etc/ssl/cert.pem`, or `curl`, before deciding a site blocks you.
- **pdftotext must be on PATH** for the pipeline; without it several steps fall back silently and classifications drift (`rebuild_all.sh` stops instead).
- **Chinese program keys** in four files (bootstrap group names, access-log actions) are data: do not translate them.

## 8. After a data rebuild

1. `bash src/rebuild_all.sh` ends with `DONE`, and `provenance_check.py` and `train_test_guard.py` passed inside it.
2. Freeze complete forward forecasts and score the covered, non-LS project amounts with `src/opcc_accuracy.py`. Publish the eligible and held project counts and the basis of the actual amounts. The historical `MARKET_ACCURACY` and `UNDER_CURVE` use the earlier scope and belong with that dated baseline.
3. `ui/build_item_accuracy.py` rebuilds `ui/item_accuracy.json`; regenerate the README's item tables from it.
4. `deploy/README.md`, section B: bundle, package, identity check, release, deploy.
5. Record the build in `data/test_holdout/REGISTRATIONS.md` if a registered change was installed.

The scripts for step 2 and for the README tables (forward scoreboard, matched not-under overshoot, under curve, table generator) are small and live in the owner's working area (`handoff/scratchpad/fwd/`, `line_accuracy/`, `contingency/`). They are not in the repository yet; bring them in, cleaned, when data work resumes.

## 9. Source changes at handoff (2026-11-21)

To publish the whole pipeline, company-specific content left the code. No number changed:

- New `src/local_config.py` reads an optional private file. Three things moved into it: the names of files with a site-level layout (`parse_bidtabs.SITE_LEVEL_FILES`), the name of a researched-dates table (`build_master._apply_local_date_overrides`), and extra engineer's-estimate labels (`parse_bidtabs.ENGINEER_RE`). With the private file present the compiled patterns and sets are identical to before.
- Comments and self-test strings that named private files were reworded; report text written in Chinese by `build_master.py`, `index_by_market.py`, `load_erosion_statewide.py` and `build_project_metadata.py` is now English (the value "DIFF" replaces a Chinese word in `award_reconciliation.csv`, which nothing reads back).
- `rebuild_all.sh` skips the four company-only steps where their scripts are absent.
- Verified by a full rebuild with an empty parse cache (all 1,686 files parsed again, 0 cache hits): 80 of the 81 CSV tables in `data/clean` are byte-identical to the tables before the change; the 81st is `award_reconciliation.csv`, identical once that one word is mapped. Both leak guards passed. The parser's self-tests pass with and without the private file. The UI suite passes on the working copy and on a fresh checkout with the public bundle.
- File md5s named in older registrations refer to the same logic.

## 10. Deploy log

| When | Code | Data bundle | Notes |
|---|---|---|---|
| 2026-11-20 21:02 | first deploy | parser .29 | Site live. |
| 2026-11-21 20:15 | `176d550` | built 2026-11-21 20:08 (parser .34b, round 20) | Allowance alert follows the amount typed; OPCC date is the user's day. |
| 2026-11-21 23:46 | `ae9d0de`, from a fresh clone | `bundle-2026-11-21` (the same build, as published) | First deploy by the documented path: `fetch_bundle.py`, `assemble_space.py`, `check_package.sh` (identical in 400 schedules), `redeploy.sh`. `live_check.py` on the live site gave the known answer. Pricing unchanged; the package now holds the published code only. |
| 2026-11-22 23:07 CST | `3ce350a` | `bundle-2026-11-21` with registered P70 calibration | All 400 package comparisons matched. The 43-line fixture on Oracle Cloud returned P50 $765,836.50, P70 $887,482.17 and P80 $1,043,089.75. The public page, manual and worksheet script matched the tested package. The live API preserved 45 units of 100 LF and priced 4,500 LF. |
| 2026-11-22 23:57 CST | `6fafa0e` | `bundle-2026-11-21` with registered P70 calibration | Restored selectable P50 and P70 estimate cards. Public desktop and 390-pixel touch browser checks switched the worksheet from P50 to P70 and back. Six automatic unit prices changed and were restored. The public fixture matched all three known percentiles. All 400 package comparisons matched. |
