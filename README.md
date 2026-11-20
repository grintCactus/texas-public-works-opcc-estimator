# Texas Public Works OPCC Estimator

Estimates what a Texas public-works construction project will cost at bid time. For any bid schedule (a list of items with quantities) it predicts:

- **P50**, the expected low bid;
- **P80**, a "not-under" opinion of probable construction cost (OPCC), set so that the actual low bid comes in below it on at least 80% of lettings.

The focus is municipal work in the Dallas–Fort Worth area, especially creek erosion repair, drainage and streambank stabilization. Texas Department of Transportation (TxDOT) lettings are covered as a second market.

The model is trained **only on public bid tabulations**. Every number it reports was measured **forward in time**: each letting is priced using only lettings dated before it, the way the tool would actually be used.

> **Status: work in progress.** The data pipeline and the evaluation framework are in place. Accuracy is still well short of the goal (see below). This repository is being published step by step.

---

## The goal

The estimate is meant to be used as an OPCC, where underestimating is the costly mistake. The target is:

1. **Floor:** the P80 estimate is below the actual low bid on **at most 20%** of lettings.
2. **Score:** as many lettings as possible land with P80 / low bid between **1.00 and 1.13** (over, but by no more than 13%).

## Where it stands (2026-11-20)

All figures are forward in time, on lettings the model had not seen when it priced them.

| | Lettings | P50 median error | P80 under the low bid | P80 within 1.00–1.13 |
|---|---|---|---|---|
| DFW municipal, 2024–26 | 140 | 29% | 17% | 9% |
| Locked public test set, municipal | 58 | 22% | 14% (90% interval 7–21%) | 14% |
| TxDOT (DFW counties), 2023–26 | 798 | 26% | 16% | 13% |

For comparison, engineers' own estimates have these median errors:

- DFW municipal: **15%** on 140 lettings checked against the original documents. They are under the low bid 33% of the time and within 1.00–1.13 only 23% of the time.
- TxDOT: about **14%**.

The floor holds. The point estimate is about twice as far off as an engineer's.

Experiments in progress, not yet adopted:

- **Pricing ordinary line items from the most similar earlier projects ("comparables").** This cut municipal error significantly and TxDOT median error from 26% to 15–18%.
- **Mobilization as a share of the job, and a competition discount that depends on job size.** These remove most of the size bias on TxDOT, where small jobs were overestimated and large ones underestimated.

None is adopted until it passes a test registered in advance, judged per year, and is then confirmed once on the locked test set.

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
estimate(schedule) ── price each line, sum, discount  →  P50
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

目前底线守住了，但估价本身的误差约为工程师估价的两倍，还在改进中。

## License

No license has been chosen yet; all rights reserved by the author.
