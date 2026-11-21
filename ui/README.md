# Bid Estimator (`ui/`)

A local web page on top of the production estimator (`src/estimate_low_bid.py`).
A bid schedule (takeoff) goes in; out come:

- **Expected low bid (P50)** and **Recommended OPCC (P80)** — the low bid came in
  at or below P80 in about 80% of past lettings — with their bands and how many
  lettings calibrated them;
- the lines that are **not covered** (specialty items with no comparable bid
  history) and need a vendor quote, and the complete total once quotes are in;
- the **low-confidence** lump sums (mobilization, general conditions) set against
  what earlier low bidders gave them;
- every line at the P50 level with its basis and a **typical accuracy** badge;
- the competition step, and the **similar past projects** the estimate leans on;
- a CSV / Excel export of all of it.

Every price on the page is the production estimator's own number. The UI adds
display, never a price.

## Run it (macOS)

From the project root:

```sh
.venv/bin/python ui/run.py                    # http://127.0.0.1:8017/ and opens a browser
.venv/bin/python ui/run.py --no-browser --port 8017
.venv/bin/python ui/run.py --check            # load, price a sample through the API and
                                              # directly (must be identical), then the tests
.venv/bin/python ui/run.py --test             # the regression suite only
.venv/bin/python ui/test_ui.py SimilarProjects   # one test class
```

For the Claude preview pane, `.claude/launch.json` has a configuration named
`bid-estimator-ui` (the same command, `--no-browser --port 8017`).

- **Start-up:** about 10 s. The estimator's tables load once (`Tables()` +
  `attach_v2f1f2()`, which reads `data/clean/master_panel.csv`, about 7.5 s), then
  one more pass over the public rows of the panel builds the bid totals, line
  prices and bidder-count history (about 2 s). The page shows a loading state
  meanwhile. The per-line reference index then builds in the background (about
  4 s); estimates do not wait for it.
- **Memory:** about 1.1 GB resident once everything is loaded, 1.4 GB at the peak.
- **An estimate** then takes about 10 ms.
- **Server:** the Python standard library (`http.server`), loopback only, no auth
  because there is no network surface. No web framework, no build step, no CDN:
  the front end is plain HTML / CSS / JavaScript in `static/`.

## The engine

`engine_production.py` is the UI's only pricing path. It imports
`src/estimate_low_bid.py` through `upstream.py` and calls it exactly as the
estimator's own entry point, `run_schedule()`, does:

```python
tab = Tables(); tab.attach_v2f1f2()                     # once, at start-up
r = estimate(tab, items, n_bidders, market, to_year,
             target={"date": bid_date, "owner": owner, "work_type": work_type})
```

- **Lines** carry `description`, `desc_norm`, `family`, `unit`, `quantity`,
  `size_bucket` and `size_in`, plus `amount` for owner-set money and `quote` for a
  vendor quote. `to_year` is the bid date's year.
- **Classification** uses the parser's own functions, in the parser's order
  (`upstream.classify_line`): `norm_desc`, `item_family`, `extract_size_in`,
  `norm_unit` (with `unit_from_description` for a bare YD / SQ), `lf_width_size`,
  and `build_master.size_bucket`. On a sample of public panel rows it gives back
  the panel's own desc_norm, unit and size bucket exactly, and its family except
  where the pipeline later marked a row `needs_review` from document-level
  evidence. The user can override a line's family; the server re-classifies every
  line from its raw text on every request.
- **Switches:** the estimator's switches (`pricing_rule`) are never changed.
- **Decisions are keyed on dict keys and class ids** (`price_key`, `usable`,
  `uncovered`, a low-confidence group's `class`), never on the estimator's label
  text. The estimator's display labels are replaced by the UI's own English
  wording, and a test checks that no CJK text reaches the page.
- **Superseded:** `pricing.py`, the UI's own P10 / P50 / P90 engine of 2026-11-18,
  stays on disk and nothing imports it. `data_access.py` now only serves the
  history search's tables; its master-panel Parquet cache (`.cache/`) is no longer
  built or read.

## The page, top to bottom

1. **Status bar:** when `data/clean` was built and the latest letting in it, the
   estimator file's md5 and rule (for example V2+F1+F2, T1 on TxDOT, coverage
   scope), load time and memory, parser and OCR availability, and the theme
   toggle.
2. **Takeoff:** paste or upload (.xlsx, .csv, PDF with a text layer, .docx),
   then the editable confirmation grid. Each line shows its classification:
   family, size bucket and unit, and badges for not covered, low confidence,
   possible owner-set money and unclassified. **Change** corrects the family or
   searches earlier public line items. An **Owner-set $** column takes an
   allowance's amount at face value.
3. **Project details:** market (City / County or TxDOT), owner and work type
   (lists from the public lettings on record), bid date, **expected bidders**
   (prefilled, editable, with the history it came from), an **Owner-set
   amounts** box for every allowance line (see below), and a vendor-quote box
   for every not-covered line.
4. **Results:**
   1. **P50 and P80**, the half-of-lettings and 80%-of-lettings bands, and the
      number of lettings that calibrated them.
   2. **One context line:** the market's forward accuracy, for example city /
      municipal 2024–26: P50 median error 23%, P80 under the low bid on 15% of
      lettings, median overshoot 45%; TxDOT 2023–26: 16% / 16% / 32%. The figures
      are `MARKET_ACCURACY` in `engine_production.py`, copied from the project
      README's "Where it stands" table; update them together.
   3. **Alerts** decided on the result's keys (no estimate, unpriced lines,
      not-covered lines, unpriced allowances, thin evidence), then the
      estimator's own warnings verbatim as notes.
   4. **Not covered — get a vendor quote:** the lines, their class, the quotes,
      and the complete totals (P50 + quotes, P80 + quotes) once every line has a
      quote. P50 and P80 exclude these lines.
   5. **Low confidence:** each mobilization / general-condition class with our
      amount at the P50 level, its share of P50, the earlier low bidders'
      share-of-job range (p25–p75) and the dollar range it implies.
   6. **Line table:** item, description, classification, quantity, unit, unit
      price and amount at the P50 level, the basis, the accuracy badge and flags.
      A line's P50-level amount is its market amount × (P50 − owner-set money) /
      market total, so the lines add up to P50 exactly; owner-set lines stay at
      face value. Click a row for its derivation.
   7. **Competition:** the bidders used (and the rule's suggestion) and the
      discount factor with its table, bin and lettings.
   8. **Similar past projects:** see below.
5. **Export:** CSV, and Excel (.xlsx) with one sheet per block, including each
   line's top-3 references.

## Owner-set allowances

An allowance, contingency or force account is money the owner writes into the
bid form; every bidder copies it, so it is entered, not estimated. A line is
treated as one when `estimate_low_bid.allowance_like()` says so (the
contingency family, ALLOW-type units, allowance / contingency / force-account /
cash-allowance wording) or it is worded as the owner's money ("owner's
reserve", "owner directed work" -- but not owner-furnished material, which
bidders still price to install).

- **Entering the amount:** the Owner-set amounts box in Project details (or the
  grid's "Owner-set $" column). It is passed to the estimator as
  `run_schedule()` passes owner-set money (`amount` + `owner_fixed`), so it goes
  in at face value, outside the discount and the centring.
- **Importing it:** an uploaded takeoff's amount / total / extended column (else
  quantity × its unit-price column) is used for allowance lines only. Every
  other line is estimated, never read off the file.
- **Until an amount is entered** the line keeps the estimator's own number, so
  the totals still equal production's, but it is marked red ("Allowance amount
  missing ...") and a banner above the headline says the totals are
  understated.

## References behind each price

Click a line in the results to see the evidence behind its price, loaded on
demand (`POST /api/references`):

- **How it was derived, in plain words:** the tier the estimator used (word
  match with its Jaccard, family + size + quantity band, family median, size
  scaling, quantity factor), the cell's median, the escalation to the bid year,
  and the step to the P50 level.
- **The cell is the estimator's own:** re-identified with
  `item_market_price`'s selection rules on the same tables, and rebuilt from
  `estimate_low_bid._reference_pool()` -- public rows only -- with the
  estimator's own keys (`family_key`, `item_tokens.tokens`,
  `qty_shape.band_of`). The page checks, and the tests assert, that the median
  of the listed lettings' median bids is the published cell price and that it
  times the trace's factors is the line's price, to the last bit.
- **The references:** the three most relevant lettings (recency, wording,
  quantity, same owner), with project, owner, letting date, bidders, the
  project's low bid, the item as written, its quantity, the median bidder's
  unit price (what the model uses, also escalated to the bid year), the low
  bidder's, and the bids' range; "show all" lists every one, sortable by date,
  unit price and quantity, and each opens to every bid on that item. Only
  lettings dated before the bid date are listed; the published cells are built
  from every public letting, so for a past bid date the header says how many
  later lettings are in the median but not listed.
- **Special lines** say what they are: owner-set (face value), not covered (no
  price), unpriced, share-priced (TxDOT mobilization), a removal line at the
  schedule's typical amount. A generic catch-all match carries a warning.
- The Excel export has each line's top-3 references on a "References" sheet.

## Themes

A Light / Dark / System toggle sits in the status bar. System is the default
and follows the operating system live (for example macOS switching at night);
a Light or Dark choice is remembered in the browser once picked. The theme is
set before first paint, so there is no flash. Every colour in `style.css` is a
token defined twice, light on `:root` and dark on `:root[data-theme="dark"]`;
the tests check there is no other colour literal and that text meets WCAG AA
contrast in both. Exports carry no theme.

## Expected bidders (rule N1)

The prefill is the registered rule N1
(`handoff/scratchpad/diag_twostage/NHAT_RULE.md`, locally): the median bidder
count of earlier lettings (dated strictly before the bid date) of the same
market × work type × size class. The first cell with at least 10 lettings wins:
market × work type × size, then market × size, then market, then all markets;
otherwise 4. The size class (under $0.5M, $0.5–2M, $2M and over) is that of the
schedule's own model total plus owner-set money, which the bidder count does not
enter.

The history is rebuilt from `data/clean` at start-up: the production comparables
pools give each letting's date and bidder count; the panel's first public row
gives its work type. The rule sizes each earlier letting by its own forward model
total. `data/clean` holds those totals for TxDOT only
(`estimator_t1_txdot.csv`), so municipal lettings are sized by their median
bidder total instead. Checked against the registered run:

- TxDOT: the same n-hat as the registered map on 807 of 807 lettings (2023–26).
- Municipal: 160 of 213 (2024–26), a mean difference of 0.32 bidders, and the
  same accuracy against the actual counts (mean absolute error 2.74 vs 2.78).
  Fed the registered totals, the implementation agrees on 211 of 213.

## Item accuracy badges

Each priced line shows how close the estimator's line price has been, for its
item group, in the forward back-test: "typically ±17%" means half of the lines
were within 17% of the median bid on the same line. Green is within 25%, amber
within 50%, red beyond. Lump sums (mobilization and the general-condition LS
classes), unclassified lines and allowance lines are drawn hollow and dashed:
their figure is weak. Groups with fewer than 40 lines in 15 lettings show "no
item figure". The tooltip gives the line count, the typical ratio (runs high /
low) and the bidders' own spread.

The figures are `item_accuracy.json`, the same groups, filters and statistics as
the project README's "Item-level accuracy" tables. To regenerate it after the
line-accuracy pass has been re-run (see the project README):

```sh
.venv/bin/python ui/build_item_accuracy.py                         # default inputs
.venv/bin/python ui/build_item_accuracy.py LINES_CSV FWD_REC_JSON  # other inputs
```

The defaults are `handoff/scratchpad/line_accuracy/out/mob_lines_28.csv` and
`fwd_rec_28.json`. The file records its inputs' md5s.

## Similar past projects

Under every estimate, up to four cards: the comparables `src/comparables.py`
itself chose for the P50 blend, most similar first. Only public lettings dated
before the bid date can appear. When fewer than four earlier jobs share at least
10% of the schedule's dollars (the comparables rule's minimum overlap), fewer
cards are shown and the page says so. Each card shows:

- owner, project name, letting date, work type and number of bidders;
- the low bid and the other bids (2nd low, median, high);
- its size against this job (their low bid / our P50);
- the overlap in words ("shares 12 of 26 item types, 46% of the dollars") and the
  similarity score (hover for its parts: overlap × size × owner × work type ×
  age);
- **this job at that project's low-bid prices:** `comparables.price_target` run
  on this schedule with every other candidate hidden, so the lines are
  transferred (escalated, quantity- and competition-adjusted) exactly as the
  blend does it;
- the public source: the bid-tab document name, and its link where the data has
  one.

**Matched lines** opens the line-by-line view: each of our lines next to the line
the comparables method would take from that project, with that project's low
bidder's and median bidder's unit prices and our P50-level unit price.

## The OPCC is not an input

There is no field for it. `POST /api/estimate` answers 400 to `opcc`,
`engineer_estimate`, `target_price`, `budget` and the like. The OPCC is only an
after-the-fact backtest baseline.

## Wiring check

```sh
.venv/bin/python ui/integration_check.py TAKEOFF.csv --market municipal \
    --bidders 5 --bid-date 2025-10-09 [--owner ID] [--work-type W] \
    [--amount ITEM=AMOUNT]    # an owner-set allowance, repeatable
```

It prices the file through the UI's HTTP API (a real socket) and through
`run_schedule()` on a schedule CSV with the same classification, and compares
P50, P80, the market total, owner-set money, the complete totals and every line
price. They must be identical.

## Tests

`test_ui.py` (stdlib `unittest`, 96 tests, about 30-45 s including the loads)
pins, among others:

- **Safety:** the OPCC is refused; unclassified, unpriced and quantity-less lines
  are never priced silently; one bidder gets no discount; a bidder count far
  outside the table gets no estimate; no CJK text on the page.
- **ProductionParity:** P50 / P80 equal `estimate_low_bid`'s own output, and
  `run_schedule()`'s; the lines add up to P50.
- **NotCovered / OwnerSet / LowConfidence:** the three blocks, keyed on classes.
- **Provenance:** no company (internal) or locked-test row in the comparables
  pools, the chosen comparables, the similar-project cards or the bidder history;
  the reference tables carry `train_scope = public_external_only`.
- **Classification:** the parser's functions, checked against the panel itself.
- **SimilarProjects:** the cards are comparables.py's picks in its order; the
  per-project figure is price_target's own.
- **Allowances:** Hunterwood (public bid form) with its $80,000 allowance equals
  `run_schedule()` given amount + owner_fixed=yes; a missing amount is flagged.
- **References:** every priced line's listed lettings reproduce its price; all
  public and dated before the bid date.
- **Theme:** no colour literal outside the two token blocks; WCAG AA contrast.
- **ExpectedBidders, ItemAccuracy, Contract, Export, Http, Matching,
  TakeoffParsing, OCRGate, DataContract.**

## Files

```
ui/
  run.py                  launcher: serve, --check, --test
  app.py                  HTTP API + static files (standard library)
  engine_production.py    the production estimator, as JSON for the page
  upstream.py             the only import point for src/
  exporter.py             CSV / XLSX export
  build_item_accuracy.py  writes item_accuracy.json
  item_accuracy.json      per-item forward accuracy for the badges
  integration_check.py    UI vs run_schedule() on one takeoff
  test_ui.py              the regression suite
  matching.py             history search for the classification picker
  data_access.py          read-only loader for the search tables
  ingest/                 takeoff parsing (xlsx / csv / pdf / docx / text),
                          bid-tab preview (stages files in .inbox/), OCR gate
  static/                 index.html, app.js, style.css
  pricing.py              superseded engine, unused
```

Nothing here writes to `data/` or `src/`. `.inbox/` and `.cache/` stay local and
are never read by the pricing path.

## API

| Route | Does |
|---|---|
| `GET /api/status` | engine state, data date, estimator md5 and rule, parser |
| `GET /api/options` | markets, owners, work types, families, units, discount tables, accuracy context |
| `POST /api/takeoff/text`, `/api/takeoff/file` | parse a takeoff |
| `POST /api/classify` | the parser's classification of lines |
| `GET /api/history/search` | earlier public line items like a query |
| `POST /api/bidders/suggest` | rule N1's expected bidder count |
| `POST /api/estimate` | the estimate |
| `POST /api/references` | the reference projects behind one line (`{request, row_id}`) |
| `POST /api/export/csv`, `/api/export/xlsx` | the export |
| `POST /api/bidtab/parse`, `GET /api/bidtab/inbox` | bid-tab preview and staging |
