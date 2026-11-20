# Bid Estimator (`ui/`)

Local estimating tool: a bill of quantities in, an expected **low bid** out as a
P10 / P50 / P90 range, with the historical evidence behind every line.

```powershell
python ui\run.py
```

Opens <http://127.0.0.1:8017/>. First start reads `data/clean/master_panel.csv`
(~165 MB) once into a Parquet cache under `ui/.cache/`; after that startup is
under a second.

```powershell
python ui\run.py --check
```

Loads every table, prices a known takeoff, then runs the regression suite.
Exits non-zero on any failure. Run this first if something looks wrong, and
after any ETL run that changes `data/clean`.

```powershell
python ui\run.py --test
```

The regression suite alone (104 tests, ~6 s). Also runnable as
`python ui\test_ui.py`, or one class at a time:
`python ui\test_ui.py Safety`.

Other flags: `--port`, `--host`, `--no-browser`, `--reload`, `--no-tests`.

### What the tests are for

`test_ui.py::Safety` pins down defects that were found by hand and each
produced a **confident, wrong number with no warning** — the worst failure
mode here, because an estimator cannot tell a bad answer from a good one by
looking at it:

* a nonsense description priced at $3,970/EA off the `other` catch-all family;
* `Item | Description | Unit | Quantity` — the commonest bid-schedule header
  there is — silently yielding an **empty** takeoff, because a bare `Item`
  column captured the description role and every row was then dropped as
  "description too short";
* `Total Station Survey Equipment`, a real pay item, being dropped as a
  totals row;
* a single-project vocabulary entry outranking a 648-project family average;
* the entered quantity being ignored outright — three of the four price
  sources publish no reference quantity, so stage 1 of a two-stage estimate
  applied a factor of exactly 1.0 and said nothing. The erosion table is
  *preferred* for channel work, so a 12 SY spot repair and a 2,600 SY
  reconstruction came back at the same rate;
* every riprap line losing the erosion specialty table, because the ETL split
  `riprap` into stone / concrete / removal and the UI still asked for the old
  name. `riprap_stone/CY` alone carries 908 projects;
* `Permanent Turf Reinforcement Mat, HP-TRM` priced at **$1.37/SY** off the
  soil-retention-blanket row, against $40–43/SY measured upstream for that
  product — a hand-written family→specialty mapping substituting a different
  product, about 30× out, with no warning beyond a generic weak match.

`ErosionSpecialty`, `QuantityHandling` and `PanelCache` cover the same ground
for the client's core trade, the quantity curve, and the cache-rebuild path
that only runs after an ETL rewrite.

`UpstreamContract` pins the shape of the ETL's output, which is this UI's
input. Every defect that has reached production here arrived that way: a rerun
changed `data/clean` and the UI kept running on worse data without a word. It
covers the three ways that happens — a load-bearing *optional* column vanishing
(no error by design, the estimate just gets worse), a **value** moving (which
no schema check sees: `riprap` became riprap_stone / riprap_conc /
riprap_removal and 908 projects went unreachable), and a **new** value in a
column the UI switches on (which falls through a `.get` default and is handled
as something it is not). It also refuses a family→specialty mapping whose two
sides differ by more than 4× in price, which is a product substitution rather
than a fallback.

`Contract` pins the JSON shape `ui/static/app.js` consumes, so the modelling
session can tell immediately whether a change to `pricing.py` breaks the UI.
The data-backed tests assert invariants (ordering, direction, refusal) rather
than specific prices, so an ETL rewrite changes the numbers without breaking
the suite.

## Stack and why

| Choice | Reason |
|---|---|
| **FastAPI + uvicorn**, vanilla JS front end | The confirmation grid has to be a real editable spreadsheet — per-cell edit, keyboard navigation down a column, per-row expansion, a searchable match picker. Streamlit reruns the whole script on every keystroke and cannot express that without fighting it, and with a 550k-row panel in memory those reruns are not free. |
| **No build step, no CDN, no npm** | Must start from a Python prompt on a Windows machine, offline. Everything is served from `ui/static/`. |
| **pandas + pyarrow** | Already present; the Parquet cache turns a 15-second CSV read into a 0.3-second one and invalidates on the ETL's mtime+size, which matters because the ETL rewrites `data/clean` while this is running. |
| **No fuzzy-match library** | Matching uses IDF-weighted token overlap in `matching.py`. An estimator has to be able to look at a match and tell whether it is right; a similarity score from an opaque library does not support that. |

Only `fastapi` had to be installed. See `requirements.txt`.

## Layout

```
ui/
  run.py            launcher + self-check + test runner
  test_ui.py        regression suite (stdlib unittest)
  app.py            FastAPI routes
  pricing.py        the estimator -- STABLE INTERFACE, see below
  matching.py       takeoff text -> priceable historical item
  data_access.py    read-only loader for data/clean, with a column contract
  upstream.py       the only import point for src/ code
  ingest/
    takeoff.py      scenario A: xlsx / csv / pdf / docx / image / pasted text
    bidtab.py       scenario B: wraps src/parse_bidtabs.py
    ocr.py          OCR adapter -- waiting on src/ocr_extract.py
  static/           index.html, app.js, style.css
  .cache/           Parquet caches (safe to delete)
  .inbox/           bid tabs staged for the ETL
```

Nothing here writes to `data/clean/`, `data/external/` or `src/`.

## The estimate

```
expected low unit price = market median price x f(expected bidder count)
```

with the median first moved to the line's own quantity.

1. **Stage 1 — market median.** The matched item's median from
   `price_reference.csv`, `item_vocabulary.csv`, `price_book_txdot.csv` or
   `erosion_price_reference.csv` (preferred for channel work, and its DFW
   column wins once 3+ DFW projects back it). Then moved to the line's
   quantity, by whichever of three routes has the best evidence:

   1. the item's **own** observed band in `price_by_quantity_band.csv`;
   2. failing that, the observed band of **sibling items in the same family**
      — shape only, never price level: the ratio between the band holding this
      quantity and the band holding the item's reference quantity, which is
      self-contained within that row and independent of whose median is being
      moved. That table is keyed `txdot|<item>|<unit>` and only the TxDOT
      source ever builds such a key, so without this the empirical route
      reached about 7% of lines and everything else fell through to a fitted
      slope; with it the empirical routes cover about 46%;
   3. failing that, the fitted elasticity (−0.14 pooled default, capped ×2.5),
      which is the weakest of the three — see below.
2. **Stage 1b — escalation.** A median pooled over several years of
   lettings is priced at the middle of its own sample, not at the letting
   being bid. `price_time_index_by_basket.csv` moves it to the expected bid
   year, chained from the sample's dollar-weighted midpoint (derived in
   `data_access.vintage_year`, since no reference table carries an as-of
   year). The **basket matters more than the escalation**: over ten years the
   erosion basket runs 160.7 against the pooled 139.6, because the pooled
   median is held down by families with huge row counts that do not track
   material inflation, so channel work never uses the pooled series. The
   index is published at `txdot_statewide` scope, which is stated on every
   escalated line — it is a statewide series applied to local work. Never
   extrapolated past its last year: beyond that it clamps, says how many
   years carry no escalation at all, and widens the interval. With no bid
   year entered nothing is escalated and the project says so, because silence
   there would pass stale prices off as current ones.
3. **Stage 2 — competition.** `competition_discount_pooled.csv` at the entered
   bidder count: ×0.939 at 2 bidders, ×0.901 at 3, ×0.872 at 4, ×0.821 at 12.
   Clamped, never extrapolated, outside 2–12.
4. **Interval.** Lognormal. Line σ combines the reference IQR (shrunk), the
   discount's own σ, evidence depth and any quantity extrapolation. Lines
   aggregate to the project with assumed correlation ρ=0.55 — treating them as
   independent would collapse a 60-line project's interval to nothing, which is
   the most dangerous thing this tool could do. The project σ is floored at
   0.205, derived from TxDOT's 13.8% median absolute error, so no total ever
   claims to beat the industry benchmark.
5. **Lump-sum overhead** (`LS`/`ALLOW`/`LOT` in mobilization, bonds, traffic
   control, trench safety, testing, surveying, contingency) gets a share of
   construction cost, not a rate — mobilization at `LS` reads p25 $16.6k /
   median $86.5k / p75 $300k, an IQR ratio of 21, because that "unit price" is
   a project total. Shares are computed from the panel itself
   (`data_access.lump_sum_share`); mobilization comes out at a median **8.7%**
   of the bid, which brackets San Antonio's manual figure of 11%. The project
   total is then `T = D / (1 − Σ shares)`.

### What is measured vs assumed

Everything tunable is in the `CALIBRATION` block at the top of `pricing.py`.

**Measured from the data:** the discount curve, the quantity elasticities, all
medians and IQRs, the lump-sum shares, the observed quantity coverage.

**Assumed, pending calibration:** `REF_SPREAD_SHRINK` (0.55),
`LINE_ERROR_CORRELATION` (0.55), `QTY_FACTOR_CLAMP` (2.5),
`SIGMA_QTY_EXTRAPOLATION` (0.22), `SIGMA_SMALL_QTY` (0.20),
`SIGMA_QTY_MODEL_SHARE` (0.5), `SIGMA_QTY_FAMILY_BAND_SHARE` (0.25),
`SMALL_QTY_RATIO` (0.05), `SIGMA_ESCALATION_SHARE` (0.35),
`SIGMA_ESCALATION_UNCOVERED` (0.09),
`MIN_PROJECTS_FOR_QTY_REF` (3), `MIN_OBS_FOR_QTY_REF` (20),
`SIGMA_BY_USABLE`. The benchmark floor (0.2046) is derived arithmetic, not a
free parameter.

**Known to be shaky, not merely unmeasured:** the quantity slope itself.
The modelling session paired the same item at two quantities *inside the same
bid document* — same file, description and unit, three or more bidders in
common, so place, year, owner and competition are all held fixed:

| quantity ratio | pairs | median elasticity | share at zero |
|---|---|---|---|
| 2–5× | 133 | +0.0000 | 83% |
| 5–20× | 68 | +0.0000 | 84% |
| 20–100× | 13 | −0.0120 | 46% |

Contractors overwhelmingly quote **one** unit price for an item whatever the
quantity, and the effect only appears past roughly 20×. So the cross-project
−0.14 is composition — a 10,000 CY job is won by a different contractor from a
50 CY job — not a quantity effect. Composition is still predictive for a new
project, so the adjustment stays; but on this data the fitted route moves the
price by a median 12% (p90 60%), and treating that as known would be the
largest unflagged assumption in the tool. It therefore costs interval width
(`SIGMA_QTY_MODEL_SHARE`), the family-band route costs less
(`SIGMA_QTY_FAMILY_BAND_SHARE`), and the item's own observed band costs
nothing. One caveat the modelling session attached, worth repeating: part of
that spike at zero is bid-preparation habit — a bidder reusing one rate across
sections — so the finding is "contractors do not price by quantity", not
"quantity does not affect cost".

### Replacing the model

Keep this interface and the UI needs no changes:

```python
engine = PriceEngine()
result = engine.estimate(EstimateRequest(
    items=[LineItemInput(row_id, description, unit, quantity, match_key, include)],
    n_bidders=4, work_type="erosion_creek", owner="", bid_year=None,
))                                           # -> EstimateResult
engine.comparables(line.comparables_key, unit=..., quantity=..., owner=...)
```

`EstimateResult.lines[i]` must keep `treatment` (`unit_price` /
`percent_of_cost` / `unpriced`), `unit_price` and `total` as
`Interval(p10, p50, p90)`, `evidence`, `warnings` and `comparables_key`.

### The OPCC is not an input

There is no field for it and `POST /api/estimate` rejects `opcc`,
`engineer_estimate`, `target_price` and friends with a 400. The OPCC is only an
after-the-fact backtest baseline; feeding it in would recycle the
+51% / +22% / −35% errors the system exists to remove.

## Mocked / not yet connected

* **OCR.** `src/ocr_extract.py` is not delivered. `ingest/ocr.py` calls
  `extract_words(path_or_image) -> list[{text, x0, x1, top}]` and reports
  unavailable until it exists; image upload returns an explanatory error rather
  than inventing line items. `MOCK_ENABLED = True` in that file turns on a
  stand-in for exercising the column splitter — anything it produces is tagged
  `mock` and the UI refuses to price it.
* **Writing to `data/clean`.** Deliberate. Reviewed bid tabs are copied to
  `ui/.inbox/` for the ETL to pick up.
* **Price escalation.** The consumer is written, tested and inert: it needs
  `price_time_index_by_basket.csv`, which the ETL has not produced yet. Until
  that file exists the bid-year field stays hidden and no number moves. The
  pooled `price_time_index.csv` is deliberately NOT used as a substitute — it
  is the one series that must not price channel work.

Everything else is live data.

## Previewing a data file that does not exist yet

```powershell
$env:BID_UI_CLEAN_OVERLAY = "C:\path\to\candidate"; python ui\run.py
```

A directory searched **before** `data/clean`, per file. Drop one candidate CSV
in it and everything else resolves normally — enough to try a new ETL output,
or a consumer written ahead of its file, against the real app without writing
into `data/clean`, which this UI must never touch. While one is active the
status bar carries an amber **DATA OVERLAY ACTIVE** marker, so a number
produced under it cannot be mistaken for a real estimate.

## Column contract

`data_access.TABLES` lists required and optional columns per file. Files are
read **by column name** with `encoding="utf-8-sig"` (they carry a BOM — reading
as plain `utf-8` silently renames the first column). Extra columns are
tolerated and reported in **Data & method**; a missing required column raises
`DataContractError` at startup naming the file, the column and what was
actually present.
