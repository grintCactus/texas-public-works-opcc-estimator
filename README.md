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

All figures are forward in time, on lettings the model had not seen when it priced them.

| | Lettings | P50 median error | P80 under the low bid | Median overshoot (P80 ≥ low) | Overshoot at 70% / 75% / 80% not-under |
|---|---|---|---|---|---|
| DFW municipal, 2024–26 | 144 | 26% | 19% | 51% | 34% / 45% / 50% |
| Locked public test set, municipal | 56 | 14% | 20% | 31% | — |
| TxDOT (DFW counties), 2023–26 | 796 | 17% | 16% | 31% | 26% / 26% / 30% |

For comparison, engineers' own estimates on DFW municipal lettings:

- **Median error:** about **15%**, on 140 lettings checked against the original documents.
- **Overshoot at 70% / 75% / 80% not-under:** **20% / 21% / 25%**.
- **TxDOT:** engineers' median error is about **14%**.

TxDOT is close to engineer level. Municipal overshoot is still about twice the engineers'.

Adopted on 2026-11-20, each after a test registered in advance:

- **Comparables blend, size fallback, removal identity ("V2+F1+F2", both markets).**
  - Comparables: each job is also priced from its five most similar earlier jobs, and the two prices are blended.
  - Size fallback: a large pipe with no same-size history is scaled from the nearest size.
  - Removal identity: pure removals are priced only from removal history.
  - It was confirmed once on the locked test set: P80 under 19.6%, overshoot 31% (vs 36%), P50 error 14% (vs 18%).
- **Mobilization as a share of the job and a size-aware competition discount ("T1", TxDOT only).**
  - The locked test set holds no TxDOT lettings, so T1 is adopted on forward evidence.
  - It will be re-checked on TxDOT lettings dated after 2026-11-20.

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
| `reports/` | Measurements and experiment reports, mostly in Chinese |
| `ui/` | A small local web app for pricing a schedule |
| `data/test_holdout/` | Registry and access log of the locked public test set (file list only) |
| `HANDOFF.md` | Setup notes from moving the project between machines |

Not included:

- **The data corpus:** several GB of downloaded bid tabs and derived tables.
- **Local tools:** poppler, Tesseract and the Python environment.
- **Some pipeline modules:** they still carry client-specific configuration, which is being moved out into a local file. They will be added in a following commit.

## Running it

Requires Python 3.12 on macOS or Linux.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Text extraction and OCR use poppler (`pdftotext`) and Tesseract, installed from conda-forge into `tools/` (see `tools/README.txt` in a working copy). A full rebuild (`bash src/rebuild_all.sh`) needs the bid-tab corpus, which is not distributed here.

## 中文简介

德州公共工程 OPCC 估价器。给定一份工程量清单，估算开标时的最低价（P50）和“宁高勿低”的 OPCC（P80）：P80 至少 80% 的时候不低于实际最低价，同时尽量不超出 13%。重点是 DFW 地区的市政冲刷修复、排水和河岸防护工程，同时覆盖 TxDOT 工程。

- 只用公开的开标数据训练，公司文件不进入本仓库，也从不参与训练。
- 所有准确度都按时间往前测：每场开标只用比它早的开标定价。
- 另有一个锁定的公开检验集，只用来确认事先登记的决定。

2026-11-20：TxDOT 的估价误差已接近工程师（中位约 17%，工程师约 14%）；DFW 市政在同样不低估比例下的高估仍约为工程师的两倍，还在改进中。

## License

No license has been chosen yet; all rights reserved by the author.
