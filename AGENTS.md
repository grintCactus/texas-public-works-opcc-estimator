# AGENTS.md

Instructions for any coding agent working in this repository. Read this file first, then `docs/HANDOFF.md`.

## What this project is

A working aid for estimators who prepare an opinion of probable construction cost (OPCC) for Texas public works: city creek-erosion, drainage and streambank jobs in the Dallas–Fort Worth area first, TxDOT lettings second. For a bid schedule it shows the earlier public bids behind every line and estimates **P50** (the expected low bid) and **P80** (a "not-under" OPCC). It is a pipeline (public bid tabulations → parsed panel → reference prices → estimator) plus a small web app (`ui/`) and a public demo site.

- Public site: https://129-146-109-44.sslip.io
- Status, numbers and method for people: `README.md`
- Everything a new maintainer needs: `docs/` (index at the end of this file)

## Rules that are never broken

1. **Public data only.** The model is trained and tuned only on public bid tabulations. This repository is public: no client or company file, no employer name, no company project name or amount, no personal information (names, e-mail addresses, home paths) may ever be committed. A private test set exists only on the owner's machine; nothing here names it.
2. **Never commit local-only material:** `data/` (except `data/test_holdout/`), `reports/`, `handoff/`, `ui/.inbox/`, `ui/.cache/`, `ui/.local/`, `deploy/space/`, `deploy/bundle/`, `tools/`, `.venv/`. Install the guard once per checkout: `sh deploy/tools/install_hooks.sh`. If it blocks a commit, fix the content; never bypass it (`--no-verify` is not allowed).
3. **If a private local configuration exists** (`data/local_config.json`, `.git/company_patterns.txt`, `.git/scan_private.json`: owner's machine only), never print, copy, summarize or commit what it holds. Work through the tools that read it (`deploy/tools/scan_public.py` prints counts only).
4. **Engineer estimates are a yardstick, never an input.** They are not a feature, a target or a reference price. Rows flagged `is_engineer_estimate` never train.
5. **Every accuracy figure is forward in time:** each letting is priced from lettings dated before it. Never report leave-one-out or in-sample figures as accuracy.
6. **The locked public test set is not for development.** `data/test_holdout/public_holdout.csv` lists files that never train anything. It is opened only to confirm a decision registered beforehand in `data/test_holdout/REGISTRATIONS.md`, with `src/holdout_eval.py --confirm`, and every opening is logged. Develop on `--decoy`. Never choose between variants or tune on it. After four openings, new confirmations use lettings dated after 2026-11-20 instead.
7. **Register before scoring.** A change to pricing, calibration or the parser's item rules is written down with its pass/fail rule before it is scored (`docs/METHOD.md`, "Adoption rule"). The P80 under rate is a hard floor in every track.
8. **Model work is paused.** The owner stopped algorithm optimization on 2026-11-21 after round 20. Do not start a pricing round, a parser round or a data rebuild unless the owner asks for it. Fixes that leave every number identical (verified) are fine.
9. **English only** in this repository: code, comments, documents, commit messages, the site. (Four files keep some Chinese program keys on purpose; the guard knows them. Do not translate those keys: they feed seeds and logs.)
10. **Ask the owner before any outward-facing act:** creating an account, registering as a vendor, filing a public-information request, sending a message or e-mail, posting anywhere, paying for anything, creating a repository or a release, changing visibility. Pushing commits to this repository and redeploying the site are part of normal work once the checks below pass.
11. **Ask before deleting** data, downloads, caches or reports. Prefer moving aside.
12. **Fetch with scripts, never through a browser's download dialog,** and throttle. A file that could not be fetched is "not fetched", not "does not exist".

## Set up a fresh checkout

Python 3.12, macOS or Linux.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
sh deploy/tools/install_hooks.sh
.venv/bin/python deploy/tools/fetch_bundle.py     # public data bundle -> data/clean (21 MB download)
.venv/bin/python ui/run.py --no-browser           # http://127.0.0.1:8017/ ; ready about 10 s after start
```

System programs: `pdftotext` (poppler) and `tesseract` are needed by the data pipeline and by OCR of scanned uploads (macOS: `brew install poppler tesseract`; Debian/Ubuntu: `apt-get install poppler-utils tesseract-ocr`). The app starts without them.

Check the setup with a known answer (`docs/HANDOFF.md`, "Known-answer check"):

```bash
.venv/bin/python ui/integration_check.py ui/fixtures/takeoff_public_creek_bank.csv \
    --market municipal --bidders 5 --bid-date 2025-03-10
```

## Tests

```bash
.venv/bin/python ui/run.py --test                              # the UI regression suite (stdlib unittest)
.venv/bin/python -m unittest ui/test_public_mode.py            # the public-mode rules
.venv/bin/python -m unittest ui/test_intake.py ui/test_fuzz.py # reading takeoffs
.venv/bin/python src/parse_bidtabs.py --selftest               # the parser's rules
```

- On a fresh checkout with the public bundle the UI suite runs 300 tests and skips those that need more: a working copy's source documents, `pdftotext`, or the optional `anthropic` package. Nothing may fail.
- Browser tests (`ui/test_browser.py`) need `requirements-dev.txt` and `python -m playwright install chromium`; without them they are skipped.
- `src/holdout_eval.py --selftest` (the evaluation harness, on a decoy, about 2 minutes) and every forward score need the full built tables of a working copy; the public bundle holds only what the app reads.
- Run the UI suite and the public-mode tests before every push that touches `ui/` or `src/`.

## Layout

| Path | What it is |
|---|---|
| `src/` | The pipeline: harvesting (`fetch_*.py`), auditing and OCR, the parser (`parse_bidtabs.py`, `parse_coords.py`), the panel and reference tables (`build_master.py`), the index and the discount, the estimator (`estimate_low_bid.py`, `comparables.py`, `coverage.py`), evaluation (`holdout_eval.py`) and the leak guards (`provenance_check.py`, `train_test_guard.py`). `rebuild_all.sh` runs it in order. |
| `ui/` | The web app: a standard-library HTTP server (`app.py`, `run.py`), the engine wrapper (`engine_production.py`), takeoff readers (`ingest/`), the worksheet and exports (`worksheet.py`, `exporter.py`), the page (`static/`), tests. No web framework, no build step. |
| `deploy/` | The public site: package files, server setup, bundle tools, the commit guard. See `deploy/README.md`. |
| `data/test_holdout/` | Registry of the locked public test set and of every registered decision. |
| `docs/` | Handoff, history, method, data. |
| `data/clean/` | Built tables (not committed). A fresh checkout gets the public bundle here. |

`src/local_config.py` reads a private file that exists only on the owner's machine. Without it every value is empty and everything runs on public data alone.

## How to work here

- **Verify, then say.** Run the check before reporting a result. Report failures with their output. If a step was skipped, say so.
- **A measured result beats a plausible one.** When a number looks odd, re-run before diagnosing; the file on disk may be stale. Separate causes before counting: one symptom can have several causes.
- **Keep the estimator and the web app identical.** The app prices through the same `src/estimate_low_bid.py`. After any change to `src/` or `ui/engine_production.py`, the identity check (`deploy/tools/check_package.sh`) must pass before a deploy.
- **Do not edit `src/` or `ui/` while a rebuild, an evaluation or a deploy check is running:** they import modules step by step.
- **Level corrections do not lower P80's overshoot.** A correction shared by all jobs cancels out of P80 (`docs/METHOD.md`). Work on per-job pricing accuracy instead.
- **A change that tightens forward residuals also shrinks the P80 margin.** Judge the under rate on lettings the change has not seen.
- **Plain words in the page:** no jargon, codes or truncated text; "item type", not "family"; one sentence per warning with what to do. The page's layout stays fixed when numbers change.
- **One export:** an Excel workbook and the same OPCC as a PDF, both on US Letter and readable in any viewer (formulas also carry their values). No other downloads.
- **The owner's prices never feed the model.** Worksheet inputs are output only.
- **Commit messages:** one line that says what changed for a reader of the log; no ticket numbers. Use a no-reply address as the commit identity.

## Everyday tasks

| Task | How |
|---|---|
| Price a schedule from the command line | `.venv/bin/python src/estimate_low_bid.py --schedule FILE.csv` (see the module's docstring), or `ui/integration_check.py` to go through the app's API |
| Change the page or the exports | `ui/static/`, `ui/worksheet.py`, `ui/exporter.py`; then the UI suite and, if installed, the browser tests |
| Update the public site | `deploy/README.md`, section A (code only) or B (after a data rebuild) |
| Rebuild the tables from documents | `bash src/rebuild_all.sh` on a checkout that has the corpus under `data/external/` and `data/txdot/` (`docs/DATA.md`). Paused: owner's call. |
| Score a pricing change | `src/holdout_eval.py --decoy SEED --compare-switch NAME` (paired, forward); register first (`docs/METHOD.md`). Needs a working copy's full tables. Paused: owner's call. |
| Refresh the README's accuracy tables | `ui/build_item_accuracy.py` and the steps in `docs/HANDOFF.md`, "After a data rebuild" |

## Documents

| File | Read it for |
|---|---|
| `docs/HANDOFF.md` | The state of the project at handoff, what is and is not in this repository, open obligations, known issues, the first tasks. **Start here.** |
| `docs/HISTORY.md` | Everything that was tried, in order, with the numbers: the pricing rounds 1–20, the parser rounds, the data harvests, what was adopted and what was not, and why. |
| `docs/METHOD.md` | How a price is computed, how accuracy is measured, the adoption rule, the mathematics, the glossary. |
| `docs/DATA.md` | Where the data comes from, how to find a bid tabulation, how documents are cleaned. |
| `deploy/README.md` | Building the package and updating the public site. |
| `data/test_holdout/README.md`, `REGISTRATIONS.md` | The locked test set and every registered decision. |
| `ui/README.md` | The web app in detail. |
