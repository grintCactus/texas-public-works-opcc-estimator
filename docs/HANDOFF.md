# Handoff

Written 2026-11-21 for whoever maintains this project next. `AGENTS.md` holds the standing rules and the commands; this file says where the project stands, what is and is not in this repository, what is owed, and what to do first. The long records are in `docs/HISTORY.md` (what was tried), `docs/METHOD.md` (how it works) and `docs/DATA.md` (where the data comes from).

## 1. Where the project stands

- **Product.** A web app that helps an estimator build an OPCC for Texas public works: it reads a takeoff, shows the earlier public bids behind every line, estimates P50 (expected low bid) and P80 (a not-under OPCC), and exports an OPCC as Excel and PDF. The estimator stays in charge: the model's number is one input among the references.
- **Public site.** https://129-146-109-44.sslip.io, in public mode (nothing stored, no AI reading).
- **Model work is paused.** The project owner stopped algorithm optimization on 2026-11-21 at 18:23, after round 20. Nothing is running and no round is open. Start none unless the owner asks.
- **Production estimator.** `src/estimate_low_bid.py` with: line pricing from layered reference tables, the comparables blend (V2), the size fallback (F1), the removal identity (F2), T1 on TxDOT (C4 + D1), the coverage scope (specialty items "not covered"), the bidder-count clamp and the abandonment identity (round 18), forward calibration with the adaptive guard (aim 0.16, cap 0.20) and narrow carry. Parser version `.34b` (round 20 arm G).
- **Accuracy, forward in time** (each letting priced from earlier lettings only):

| | Lettings | P50 median error | P80 under the low bid | Median overshoot | Overshoot at 70 / 75 / 80% not-under |
|---|---|---|---|---|---|
| City / municipal (DFW), 2024–26 | 235 | 20% | 14% | 45% | 30 / 32 / 34% |
| Municipal engineers' own estimates | 140 | about 15% | 33% | n/a | 20 / 21 / 25% |
| TxDOT (DFW counties), 2023–26 | 807 | 16% | 15% | 31% | 26 / 27 / 30% |
| TxDOT engineers' own estimates | 451 | 21% | 14% | n/a | 19 / 22 / 23% |

  The goal: P80 below the actual low bid on at most 20% of lettings (met), and within that the smallest overshoot (13% is the aspiration; engineers reach about 25% at the same not-under rate; the project is at 34% for cities). The city gap is almost all in estimating the bidders' typical price per job, not in the step from the median bid to the low bid.
- **Last deploy.** 2026-11-21 23:46, from a fresh clone at commit `ae9d0de` with data bundle `bundle-2026-11-21` (built 2026-11-21 20:08). See the deploy log at the end.

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
2. Score production forward and update `README.md` ("Where it stands") and `ui/engine_production.py` (`MARKET_ACCURACY`, `UNDER_CURVE`).
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
