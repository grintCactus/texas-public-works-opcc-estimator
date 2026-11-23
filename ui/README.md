# Bid Estimator (`ui/`)

A local web page on top of the production estimator (`src/estimate_low_bid.py`).
It is an **assistant for preparing an OPCC**: the model's number is one input,
and the page's main job is to put useful reference material in front of the
estimator, who sets their own prices in the **OPCC worksheet** (see below).
A bid schedule (takeoff) goes in; out come:

- **Expected low bid (P50)** and **Recommended OPCC (P70)** — a local trial
  with a nominal 70% target, calculated from the 30th percentile of earlier
  public forward-scored P50 / actual-low-bid ratios; this is not an
  independently validated P70 hit rate;
- the lines that are **not covered** (specialty items with no comparable bid
  history) and need a vendor quote, and the complete total once quotes are in;
- the **low-confidence** lump sums (mobilization, general conditions) set against
  what earlier low bidders gave them;
- every line at the P50 level with its basis and a **typical accuracy** badge;
- the competition step, and the **similar past projects** the estimate leans on;
- the **OPCC worksheet**: per line a recent bid median, the reference range, your
  own price with quick picks and a note, and **Your OPCC** next to P50 / P70;
- for every project shown, its **full bid tabulation** and its **public source
  page**;
- a CSV / Excel export of all of it, and of the worksheet (your deliverable).

The original P80 totals, multiplier, calibration, bands and historical results
remain unchanged as a baseline. New worksheets use `basis: "history"`: three
recent matching projects' bidder medians, adjusted to the target bid year.
The informational P50/P70 cards do not reprice worksheet items. Existing
`p50`, `p70` and `p80` worksheets retain their original basis and meaning.

Every model price on the page is the production estimator's own number. The UI
adds display, never a model price; your own prices in the worksheet are output
only and never feed back into anything.

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

## Public demo mode

The same server can run as a public website (`ui/public_mode.py`). It is off
unless `BIDEST_PUBLIC=1` is set (or `ui/run.py --public` is used):

```sh
BIDEST_PUBLIC=1 .venv/bin/python ui/run.py     # 0.0.0.0:$PORT, 7860 by default
```

- **Off in public mode:** reading a PDF with Claude (both backends), the bid-tab
  upload and its inbox, every route that writes on the server, and serving local
  original files. These routes answer 404.
- **Kept in the browser:** the corrections memory and saved worksheets live in
  the visitor's own `localStorage`. The browser sends its corrections with each
  classify request; the server uses them for that request only.
- **Links:** a letting links to its source page and to the GitHub mirror
  (`public_mirror.json`). The harvest manifests' links come from
  `data/clean/source_links.json` (written by the deployment build) because
  `data/external` is not shipped.
- **Uploads:** processed in memory and never stored. A temporary copy exists
  only while the request runs, in a RAM-backed directory when there is one
  (`BIDEST_TMPDIR`, else `/dev/shm`). The cap is 25 MB
  (`BIDEST_MAX_UPLOAD_MB`). The page shows: "Files you upload are processed on
  this server and not stored. Do not upload confidential documents."
- **Access code (optional):** with `DEMO_ACCESS_CODE` set, visitors enter it
  once per browser session (an HttpOnly session cookie holding an HMAC of the
  code). Unset, the site is open.
- **Protection:**
  - per-IP rate limits (`BIDEST_TRUSTED_PROXIES=N` reads the client address
    behind N reverse proxies);
  - at most 4 concurrent heavy requests and 128 open connections;
  - a 30 s socket timeout;
  - no tracebacks in error replies;
  - CSP, nosniff and referrer-policy headers (`BIDEST_FRAME_ANCESTORS` for an
    embedding page).
- **OCR:** with Tesseract as the engine, each page is first turned upright by
  Tesseract's own orientation detection (`ocr_scans.osd_turn`).

Tests: `test_public_mode.py`.

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
   scope), load time and memory, parser, the OCR engines for scanned PDFs, the
   number of remembered corrections, and the theme toggle.
2. **Takeoff:** paste or upload (.xlsx, .csv, PDF with a text layer or scanned,
   .docx) through the one drop area; an **AI mode** button keeps the place for
   reading drawings and documents by AI (not built yet); then the editable
   confirmation grid. Each line shows its classification: family, size bucket
   and unit, and badges for not covered, low confidence, possible owner-set
   money, unclassified, **check** (low-confidence reading, with up to three
   one-click suggestions), **remembered** and **read by Claude**. **Change**
   corrects the family or searches earlier public line items. An **Owner-set $**
   column takes an allowance's amount at face value. A color guide above the
   grid explains the tags currently present and what needs review.
   **New item / Other — Engineer input**, in the suggestions or Change dialog,
   is an explicit per-line choice when none fits. It leaves Your $/unit blank
   after estimation; typed prices survive basis changes and re-estimation.
   The request retains `pricing_mode: "engineer"` for the worksheet. This is
   separate from automatic unclassified matches and does not change pricing
   rules or promote a generic model reference into the user's OPCC.
3. **Project details:** market (City / County or TxDOT), owner and work type
   (lists from the public lettings on record), bid date, **expected bidders**
   (prefilled, editable, with the history it came from), an **Owner-set
   amounts** box for every allowance line (see below), and a vendor-quote box
   for every not-covered line.
4. **Results:**
   1. **Expected low bid (P50)** and **Recommended OPCC (P70)**, each with
      its amount only. These are project-level comparisons, not unit-price controls.
   2. **User Manual:** the header opens `/manual.html` in a separate tab,
      keeping the current takeoff and worksheet in place. Definitions,
      probability diagrams, method, classification tags, field guidance,
      contingency guidance and export instructions live there. The current project check uses covered, non-LS items for both estimated and winning amounts. The manual reports the municipal development sample and its eligible count. Historical
      P50 and P80 baseline accuracy and contingency comparisons read
      `market_accuracy` from `/api/options`; these do not validate the P70 trial,
      and are labeled as the earlier baseline including lump sums.
   3. **Alerts** decided on the result's keys (no estimate, unpriced lines,
      not-covered lines, unpriced allowances, thin evidence), then the
      estimator's own warnings verbatim as notes.
   4. **Not covered — get a vendor quote:** the lines, their class, the quotes,
      and the complete totals (P50 + quotes, P70 + quotes) once every line has a
      quote. P50 and P70 exclude these lines; the original P80 complete total is retained in the API.
   5. **Low confidence:** each mobilization / general-condition class with our
      amount at the P50 level, its share of P50, the earlier low bidders'
      share-of-job range (p25–p75) and the dollar range it implies.
   6. **OPCC worksheet** (right under the estimate, the centre of the page):
      see below. The model's unit price per line is its P50-level price: a
      line's market amount × (P50 − owner-set money) / market total, so the
      model's lines add up to P50 exactly; owner-set lines stay at face value.
      The ▸ on a line opens the reference projects behind its price.
   7. **Competition:** the bidders used (and the rule's suggestion) and the
      discount factor with its table, bin and lettings.
   8. **Similar past projects:** see below.
5. **Export:** the OPCC worksheet as Excel (.xlsx) or PDF, from the single
   Export section. The workbook includes the summary, line details and
   reference projects (see below).

## Messy takeoffs

Uploads and pastes are read by the pipeline's own readers, all offline: the
grid / table readers, `pdftotext -layout` (tools/poppler), the bid-tab parser
(`src/parse_bidtabs.read_pdf_best`, preferred when it reads a complete
schedule), and for a scanned PDF the pipeline's OCR engines
(`src/ocr_scans.py`: Apple Vision, RapidOCR, Tesseract). Free text copes with
quantity and unit in one cell ("1,745 SY", "1745SY"), price and amount columns
after the quantity, quantity-before-unit bid forms, wrapped descriptions, column
headers and page footers.

Low-confidence lines are marked **check** with the reasons: no item family
matched, a weak match to the public price history (best history score under
`matching.WEAK_MATCH`), no unit, no quantity, or the reader was unsure.
Not-covered and owner-set lines have their own flags and are not marked. Each
marked line gets up to three earlier public line items as one-click
suggestions: same unit when the line has one, ranked by the history search's
token similarity. A click sets the family (and the unit when the line has
none).

Corrections are remembered: a suggestion clicked, or a family picked under
**Change**, is stored per wording (the parser's normalised description) in
`ui/.local/corrections.json`, local to this machine and excluded from git
(`.git/info/exclude`). The same wording gets the same family next time, tagged
**remembered**; "Not sure — keep the parser's classification" forgets it. Units
are remembered only to fill a missing one; quantities and prices never.

Measured on 25 public bid-tab PDFs (20 with a text layer, 5 scans; 983 ground
truth lines from the panel) and messy text variants, by `intake_eval.py`
(results in `handoff/scratchpad/intake_eval/`):

```sh
.venv/bin/python ui/intake_eval.py --tag after          # the local readers
TAKEOFF_NO_BIDTAB=1 .venv/bin/python ui/intake_eval.py --tag generic
.venv/bin/python ui/intake_eval.py --reader claude      # the Claude reader (costs money)
```

## Setting up Claude reading

Not on the page for now: its button was folded into **AI mode** (2026-11-21),
which is a kept place, not built yet. The reader below stays in the code
(`/api/claude/*`, `ui/intake_eval.py --reader claude`) for when AI mode is built.

Optional. **Read a PDF with Claude** sends one PDF to Anthropic to transcribe
its pay items (item no, description, unit, quantity, plus Claude's per-line
confidence and notes; never prices). It is opt-in per document: a dialog shows
the file name, the page count, the cost estimate and the privacy note, and
nothing is sent until you press **Read** (the server also refuses without
`?consent=yes`). Files in `ui/.inbox` and `ui/.cache` are never sent.
The lines land in the grid tagged **read by Claude**, after checks: unknown
units, missing quantities, lump sums with a quantity other than 1, fractional
EA, repeated item numbers, rows that look like totals (dropped), Claude's own
"low" confidence, and a cross-check against the local reader by item number.

The backend is chosen in this order, and the status bar says which:

1. **Anthropic API** — when the `anthropic` package (`pip install anthropic`,
   optional in `requirements.txt`) finds a credential. Either:
   - `brew install anthropics/tap/ant`, then `ant auth login` (stores a profile
     under `~/.config/anthropic/` that the SDK reads), or
   - an API key from the Anthropic Console in `ANTHROPIC_API_KEY`.
   Model `claude-opus-5-5`, the PDF as a base64 document block, structured
   outputs (a JSON schema), effort `low`, server-side refusal fallbacks
   (`fallbacks: "default"`). **Count tokens** in the dialog gives the exact input
   size from the token-counting endpoint (that also sends the PDF). PDFs over
   20 pages (or 20 MB) are sent in parts; the API's own limits are 600 pages and
   32 MB a request.
2. **Your Claude login** — else, when the Claude Code CLI (`claude`) is on this
   machine and signed in: "This sends the document to Anthropic through your
   Claude login and uses your Claude plan's usage limits." It runs headless
   (`claude -p`) in a fresh temporary directory holding only the PDF, with only
   the Read tool (`--tools Read --allowedTools Read --permission-mode dontAsk`,
   Bash / Edit / Write / web tools denied, no MCP servers), the same JSON
   schema, a spending cap, a 15-minute timeout and no saved session; the
   directory is deleted afterwards. `claude --help` (2.1.141) has no turn-limit
   flag; the cap, the timeout and the one-file, one-tool setup bound the run.
3. Otherwise it is off.

To measure it on the 25-PDF set once a backend is set up (this costs money on
the API backend):

```sh
.venv/bin/python ui/intake_eval.py --reader claude
```

## Full bid tabulations and sources

Every project name the page shows — a line's references, "show all", the
similar-project cards — opens that letting's **full bid tabulation**: owner,
name, letting date, work type, bidders and each bidder's total (low bid
marked), the engineer's estimate only when the tab printed one (labelled; never
an input), and every line item as written with each bidder's unit price and
extended amount and the median. The lines that matched the line you came from
are highlighted, with their low and median and our P50 unit price inline; a
similar-project card expands to the same full bid with "matched lines only" as
an option. CSV export per tab. Built on demand from `master_panel.csv`,
`source_set == "external"` rows only (one background pass records each
letting's byte ranges in the file), cached in memory.

Under every project name, in this order:

1. **Open original (local copy)** — the file we hold, served only when the path
   resolves inside `data/external/` to the source file of a public letting (no
   `..`, no symlink out, never top-level `data/` files, `.inbox` or `.cache`);
2. **Source page** — the public page it came from, from the harvest manifests
   (`data/external/**/_manifest*.csv`), else `project_metadata.csv`, labelled
   with the host and kind ("Source page: legistar1.granicus.com — council
   agenda attachment"), http(s) only, opening in a new tab; or "no public link
   recorded";
3. **Public mirror (GitHub)** — the copy in the public GitHub data repository,
   only when the optional `ui/public_mirror.json` maps the file:
   `{"external/<owner>/<file>": "https://github.com/.../blob/main/<owner>/<file>"}`,
   keyed by the same data-relative path as the local copy (a `data/` prefix is
   tolerated). Only `https://github.com/` links are accepted; the file is
   re-read when it changes. The main session generates it once the data
   repository is published; without it there is no mirror link.

The exports carry `source_url` and `mirror_url` (blank when unmapped) for the
similar projects, the line references and the worksheet's references
appendix.

## OPCC worksheet

The results' line table is the estimator's worksheet. Per line: item,
description, quantity, unit; the **recent bid median**; the
**reference range** — low, median and high of the line's reference projects'
median-bidder unit prices, escalated to the bid year; the model's historical accuracy badge;
**your unit price** (defaults to the selected recent bid median; quick picks: the model, the
reference median, the most recent similar project's low bid, or any reference
project's low or median bid); **your amount**; and a free-text **note** (filled
in for you from a project pick, e.g. "used Coppell 2025 … median bid").

`history_prices.py` selects at most one compatible pay item from each of three
public projects in the same market, with precise bid dates strictly before
the target date and no more than two calendar years old. Eligible projects
within one year rank first. Specifications, units, material and operation must
match; quantities, wording, dates and same owner inform the ordering. A pay
item's median gives each distinct bidder one vote. Fewer than three matching
projects leaves the default blank, with an engineer-input reason.

`history_inflation.py` adjusts each selected median using annual matched-item
price changes from earlier public bids in that market. Each year pair requires
ten matched items; no borrowed market, forecast rate or flat carry hides missing
index evidence. The final default is the median of the three adjusted prices,
charged to cents. Original medians, adjustment factors and full source records
are preserved in `worksheet.lines[].history_price` and the export appendix.
The full method and its verification rules are registered in `docs/METHOD.md`
section 1.5. This evidence is not a calibrated confidence percentage.

References load asynchronously. Automatic prices stay blank until the current
request finishes; errors stay visible. A delayed response cannot overwrite a
typed price or a newer estimate. Re-estimation refreshes automatic history
defaults and preserves deliberate selections, quotes, fixed amounts and notes.
Mobilization, temporary erosion-control lump sums, and New item / Other still
require the engineer's price. New worksheets start without convenience-price
rounding; an explicitly chosen rounding option remains available.

Lines where the model is weak are marked **needs your judgment**: fewer than
3 references, a generic or unclassified match, lump sums, low confidence or
weak item accuracy, not covered, an allowance without its amount. "Only lines
that need your judgment" filters to them and opens their references.

**Your OPCC** = your line amounts (owner-set allowances at face value, vendor
quotes as given) + an optional contingency %, shown next to the model's P50 and
trial P70 with the differences. Construction Contingency remains a separate
allowance for scope and site uncertainty; the P70 margin is not a recommended
contingency percentage.

Export the worksheet as Excel (OPCC, Summary, Lines, References sheets) or PDF: your
prices, notes and totals, the model's P50 / P70 and unchanged P80 baseline, and a references appendix
with `source_url`. Save it under `ui/.local/worksheets/` (local, not in git) or
download it as JSON, and reopen it later: the takeoff and project details come
back, the model is re-run on today's data, your prices and notes are kept.

**Your prices are output only.** They never feed back into the estimator, its
tables, training or calibration: `/api/estimate` refuses a request that carries
worksheet fields, the page never sends them, and only `ui/worksheet.py` reads or
writes worksheet files (`test_ui.Worksheet`).

## Choice lists with an "Other" path

- **Owner:** a type-ahead field. Owner IDs of one city are merged under one name
  (for example `coppell` and `legistar/coppell` are "Coppell"), and the estimator
  gets the ID with the most history, so the same-owner weighting still works.
  "Other — not in the list", or any typed name not in the price history, is
  kept for the results header and the export; the estimator then gets no owner
  (no owner-specific weighting), and the page says so. A name close to a known
  one gets "Did you mean ...?".
- **Work type:** the same, with "Other / not listed": the estimator gets the
  generic (blank) work type, and the page says so.
- **Market:** "Not sure" prices as City / County, the default, and the result
  says the market was not given.
- **Classification picker:** "Not sure — keep the parser's classification".

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

The defaults are `handoff/scratchpad/line_accuracy/out/mob_lines_29.csv` and
`fwd_rec_29.json` (parser .29). The group labels are read from the README
generator (`gen_readme_items.py`'s `LAB` / `TX_LAB`, parsed, not executed),
and after writing, every published group is checked against the project
README's item tables (label, lines, lettings and every printed figure); a
difference is listed and the exit code is 1. The file records its inputs' md5s.

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
price. The P50/P80 baseline must remain identical; P70 is an additional result, not a replacement value in the P80 fields.

## Tests

164 tests (stdlib `unittest`, about 40 s including the loads):
`test_intake.py` (42, offline: no `data/clean`, no network, fake Claude
backends) and `test_ui.py` (122). They pin, among others:

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
- **ClaudeReader** (offline, faked SDK client and CLI): off without a credential
  or CLI and says how to set it up; backend order; nothing sent without
  consent; the documented request (model, JSON schema, effort, fallbacks,
  base64 PDF, no file name); type checks; price columns dropped; bad JSON,
  refusal, truncation, rate limit, auth and network errors typed; splitting;
  company folders refused; the CLI's command line (Read only, no permission
  bypass), timeout and clean-up.
- **TakeoffReading / CorrectionsMemory / IntakeAssist:** messy text, PDF reader
  choice, OCR wiring; the memory round trip; flags and same-unit suggestions.
- **FullBidTab / SourcePaths / SourceLinksShown:** no internal row in a tab;
  bidder totals equal the panel's; the file endpoint stays inside
  `data/external` (`..`, absolute paths, symlinks); manifest URLs shown; only
  http(s) links; link order local copy → source page → GitHub mirror; no
  mirror link without a mapping; only https://github.com/ mirror links.
- **Worksheet:** reference ranges, totals, export, save / reopen, and the hard
  rule that your prices are output only.
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
  intake.py               low-confidence marks, suggestions, corrections memory
  lettings.py             full bid tabulations, source links, original files
  worksheet.py            the OPCC worksheet: totals, export, save / reopen
  intake_eval.py          intake measurement on 25 public PDFs
  test_ui.py, test_intake.py   the regression suite
  public_mode.py          public-demo mode (BIDEST_PUBLIC=1)
  test_public_mode.py     its tests
  matching.py             history search for the classification picker
  data_access.py          read-only loader for the search tables
  ingest/                 takeoff parsing (xlsx / csv / pdf / docx / text),
                          bid-tab preview (stages files in .inbox/), OCR gate,
                          claude_reader.py (the optional Claude reader)
  static/                 index.html, app.js, style.css
  pricing.py              superseded engine, unused
```

Nothing here writes to `data/` or `src/`. `.inbox/` and `.cache/` stay local and
are never read by the pricing path. `.local/` (corrections, worksheets) is local
too and excluded from git.

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
| `GET /api/corrections`, `POST /api/corrections`, `POST /api/corrections/forget` | the corrections memory |
| `GET /api/letting?key=`, `GET /api/letting/csv?key=` | a letting's full bid tabulation |
| `GET /api/source-file?path=` | the local copy of a public letting's original file |
| `POST /api/worksheet/refs` | per line: reference range, latest reference, picks |
| `POST /api/worksheet/export/csv`, `/xlsx` | the OPCC worksheet export |
| `GET /api/worksheet/list`, `GET /api/worksheet/load?file=`, `POST /api/worksheet/save` | saved worksheets (`ui/.local/worksheets/`) |
| `GET /api/claude/status`, `POST /api/claude/preview` | the Claude reader's backend; a PDF's pages and estimate (nothing sent) |
| `POST /api/claude/count?consent=yes`, `/api/claude/read?consent=yes` | exact token count / read (sends the PDF) |
