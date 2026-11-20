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
- **The evaluation is strict.**
  - Every figure is forward in time.
  - Decisions are registered before they are scored.
  - A locked public test set is opened only to confirm a registered decision, and each opening is logged.
  - Leak guards stop any build in which test data reaches a fitted number.
- **Public data only.** Engineer estimates are a yardstick, never an input.
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
  - A fix (lump sums as a share of the job, together with a size correction) is being tested.
- **Creek erosion and drainage** (39 lettings) is now close to the city average: P50 error 20%, P80 under the low bid 10% of the time, median overshoot 41%.
- **Recent lettings run higher than history.** In 2026, P80 was under the low bid on 19% of municipal and 21% of TxDOT lettings; TxDOT is above the 20% floor. The guard adapts, but with a lag.
- **About a third of the worst municipal misses cannot be fixed this way.** They are ordinary market variation between bidders that bid history alone cannot predict. Another sixth are one-off specialty items, which are now marked not covered.
- **City data is thin.** Many city items have little bid history. Some cities publish bid tabs only behind vendor portals, which are not collected. Scanned tabs lose lines.
- **Not runnable end to end from this repository.** A few pipeline modules still carry client-specific configuration and are not published. The bid-tab corpus (several GB) is not distributed.
- **The web UI does not use the latest estimator yet.**

**Tried and not adopted (yet).** Pricing municipal lines from municipal-only price data, instead of data dominated by TxDOT price levels, together with four smaller pricing fixes.
- **Forward tests:** passed the registered test. Municipal overshoot fell 49% → 42% and P50 error 24% → 20%.
- **Locked test set:** failed. Its tighter P80 margin let P80 fall under the low bid on 29% of lettings, against 19% for the current rule.
- **Status:** kept in the code, switched off. It will be re-checked on new lettings.

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

德州公共工程 OPCC 估价器。给定一份工程量清单，估算开标时的最低价（P50）和"宁高勿低"的 OPCC（P80）：P80 至少 80% 的时候不低于实际最低价，同时尽量少高出。重点是 DFW 地区的市政（城市）冲刷修复、排水和河岸防护工程，同时覆盖 TxDOT 工程。

**优势**
- **TxDOT 估得比较准**：接近工程师水平。
  - 805 场开标，P50 误差中位约 16%。
  - 其中 451 场有官方工程师估价：我们的误差中位（17%）比工程师（21%）还低，他们的估价整体偏高约 20%。
  - 但同样 80% 不低估时，工程师高出 23%，我们 31%，还差一点。
- **评估严格**：
  - 所有成绩都按时间往前测；
  - 改动先登记、后打分；
  - 锁定检验集只用来确认事先登记的决定，每次打开都记日志；
  - 防泄漏守卫保证检验数据进不了任何拟合。
- **只用公开数据**：公司文件不进仓库，也不参与训练；工程师估价只作对照。
- **不懂的不硬估**：泵、发电机、预制桥这类几乎没有历史的专项设备标为"未覆盖"，单独列出，请另行询价。

**劣势**
- **城市项目没那么准**：
  - P50 误差中位约 23%，工程师约 15%。
  - 同样 80% 不低估时，我们高出约 40%，工程师约 25%。
  - 误差几乎都出在估"投标人的中位价"这一步，从中位价换算到最低价那一步很小。
- **整项（LS）还不随工程规模变化**：
  - 动员费、交通管制、场地准备、SWPPP/水土保持这些整项，我们每个工程给的金额差不多；投标人是按工程的比例报的，动员费一般约 5%。
  - 所以小工程（最低价 < $50 万）偏高约 26%，大工程（> $200 万）偏低约 7%。
  - 正在测试修法：整项按比例，加按规模校正。
- **冲刷/排水**（39 场）现在和城市平均差不多：误差 20%，低估 10%，高出中位 41%。
- **近年标价走高**：2026 年 TxDOT 的低估比例超过 20%。
- **回测用的是实际投标家数**：报价时不知道，改用预测家数后，TxDOT 高出约多 2 个点，市政不变。
- **有一部分修不了**：最差的那批里约三分之一是投标人之间的正常市场差异，靠历史标价预测不了。
- **城市数据偏薄**：部分城市的标单在供应商平台上，没有收集。
- **还不能完整运行**：部分模块还带着客户配置，暂未公开；标单数据也不随仓库发布。
- **界面还没用上最新的估价器。**

**试过、暂未采用**：市政子目改用市政自己的价格数据（以前被 TxDOT 的价位带偏），加上另外四项小修正。
- **按时间往前测**：通过了事先登记的测试，市政高出 49% → 42%，误差 24% → 20%。
- **锁定检验集**：没通过。P80 的余量收得太紧，低估比例从 19% 升到 29%。
- **现状**：代码保留，开关关着，等有新的开标再核对。

## License

No license has been chosen yet; all rights reserved by the author.
