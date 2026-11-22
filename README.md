# Texas Public Works OPCC Estimator

A working aid for estimators preparing an opinion of probable construction cost (OPCC) for Texas public-works projects. For any bid schedule (a list of items with quantities) it does two things:

- **It puts the evidence in front of the estimator.** For every line it shows the earlier public bids behind the price: project, date, quantity, the low and median bidders' unit prices. Each project links to its full bid tabulation and to the public page it came from. It also shows the most similar past projects, the lines it cannot price (specialty items that need a vendor quote), and the lines whose prices are uncertain.
- **It estimates the bid where the history supports it:**
  - **P50**, the expected low bid;
  - **P80**, a "not-under" OPCC, set so that the actual low bid comes in below it on at least 80% of lettings.

The estimator stays in charge. The model's number is one input among the references, not a replacement for judgment. Where history is thin, the tool says so and leans on the references instead.

The focus is municipal work in the Dallas–Fort Worth area, especially creek erosion repair, drainage and streambank stabilization. Texas Department of Transportation (TxDOT) lettings are covered as a second market.

The model is trained **only on public bid tabulations**. Every number it reports was measured **forward in time**: each letting is priced using only lettings dated before it, the way the tool would actually be used.

> **Status: work in progress.** The data pipeline, the evaluation framework, the confirmed pricing changes and the web app are in place. TxDOT accuracy is near engineer level; municipal accuracy is still short of the goal (see below). Model optimization is paused since 2026-11-21 (after round 20). Maintainers and coding agents start with [`AGENTS.md`](AGENTS.md) and [`docs/HANDOFF.md`](docs/HANDOFF.md).

---

## Using it

**Try it online:** https://129-146-109-44.sslip.io (a demo on a free cloud server; files you upload are processed and not stored, so do not upload confidential documents).

Or run it on your own machine (see [Running it](#running-it)), then open http://127.0.0.1:8017/. Pricing is ready about 10 seconds after start.

1. **Enter the takeoff.**
   - No takeoff yet? Download the blank takeoff template (Excel) at the top of this step. It is laid out like the OPCC it becomes, just not filled in. Fill in one pay item per row: item no., description, units and quantity; an allowance the bid form prints a dollar amount for goes in Total Cost. Then upload the file, or paste the rows with the header row. An uploaded template brings its project name, project no. and prepared by into the worksheet and the export, and long descriptions wrap in the grid.
   - Paste a bid schedule, or upload Excel, CSV, Word or PDF. Scanned PDFs are read with OCR.
   - **AI mode** (coming later): the upload step keeps a place for reading a job's engineering drawings and documents by AI ("Read by AI"), to list the work to be done and the quantities as a takeoff to check. Files go in through the same drop area. It is not built yet; the button only shows where it will be.
   - Check each line's description, unit, quantity and the item type it was matched to. Change the type with the picker if it is wrong.
   - Lines the reader is unsure of are flagged, with the closest items from bid history to pick from. Corrections you make are remembered on your machine.
   - Lines it cannot read at all (stray symbols, garbled text, notes) are listed apart as "could not be read", with their original text; nothing is guessed from them. You can add any of them back.
   - Click "+ Add row" under the table to add a line. Drag a row by any part that is not an input box to move it; the other rows slide aside and it drops in where you let go.
2. **Describe the project.**
   - Market (city/county or TxDOT), owner, work type and bid date. If the owner or work type is not in the list, type it in.
   - Expected number of bidders: it is prefilled from similar past lettings, and you can change it.
   - The amounts of owner-set allowances; these are added at face value.
   - A contingency line (a lump sum worded as a contingency) has no item type and is not priced from history. Pick its size from a list: 5–25% of the subtotal (10% by default), or the owner's fixed amount when the bid form prints one.
   - Vendor quotes for specialty items the tool does not price (pumps, generators, prefabricated bridges, …).
3. **Read the estimate.**
   - P50 (expected low bid) and P80 (a not-under OPCC), with their ranges and the market's track record.
   - "What P50 and P80 mean" opens on a click: plain words with this job's numbers, a picture of the idea (where the low bid may land, and which line is P50 and which is P80), and how the spread is measured.
   - The items left out for a vendor quote, and the lump sums whose prices are uncertain.
   - Warnings are one short sentence each, with what to do and the lines involved. Tick a warning once you have checked it and it turns gray. A not-covered line stops being flagged once it has your number: a vendor quote, or your own price in the worksheet.
4. **Check the evidence.**
   - Click any line to see the earlier public bids behind its price: project, date, quantity, and the average, median and low bidders' unit prices.
   - Open a project to see its whole bid tabulation, the original document, its public source page, and its copy in the data repository.
   - The four most similar past projects are shown with their full bids.
5. **Build your OPCC.**
   - The worksheet's prices start from the model's **P80** (the recommended OPCC) or its **P50**: click either headline card, or P50 | P80 above the totals. A switch puts every line back on the model's price at that level, after a confirmation if you changed any.
   - Keep the model's unit price or pick one from the references, line by line: the newest item worded exactly like yours comes first, then averages, medians and lows. Add notes.
   - A not-covered line has one number: the vendor quote under Project details, the line's amount in the takeoff and its price on the worksheet stay the same whichever you type in. It needs no new estimate, since P50 and P80 leave the line out.
   - Picked prices are rounded to a round number, in a step that grows with the price ($0.50 under $10, then $5, $10, $100, $500, $1,000). Up by default; to the nearest, or off, from the list. A price you type is never rounded.
   - Mobilization and temporary erosion control are left for you to fill in. Whether a job has mobilization, and what erosion control a site needs, are the engineer's call. The model's price stays in the quick picks.
   - The estimate is laid out as an OPCC: the items, then the **subtotal**, then the **contingency** below it, then the **total**. This holds for P50, for P80 and for your own OPCC. A contingency given as a percentage stays that percentage of the subtotal at P50 and at P80.
   - Pick your own construction contingency from a list (0–25% of the subtotal): 10% by default, 0% when the schedule already carries a contingency line (then it is an additional construction contingency). Its row stays at 0% too, so changing it changes numbers, not the layout. The page shows how often an OPCC that high came in under the low bid on past jobs: in cities, P50 + 10% did on about 30% of jobs, and P80 on about 14%.
   - "Your OPCC" is shown next to the model's P50 and P80.
6. **Export** (step 4) gives the Excel workbook, or the OPCC as it prints as a PDF (US Letter); there are no other downloads. The first sheet is the OPCC itself, in the usual layout and with one total: a title, the project, date, prepared by and project no.; then item no., description, units, quantity, unit cost and total cost; then Sub-Total, Construction Contingency and Project Total, and a blank interim-review box at the bottom right, from Units to Total Cost, laid out as on a printed OPCC: "INTERIM REVIEW ONLY", the standard legend, and blanks for the engineer, P.E. serial no., date, company and firm registration no. The seal goes there when the OPCC is submitted. It prints on portrait pages one page wide (columns A-F, the header repeated on each page). The source of each unit cost and your note on the line sit beside the printed block as working notes. A heavy frame, a gray ESTIMATED QUANTITIES band and the interim-review box at the bottom right follow the usual OPCC. The totals are live formulas that also carry their values, so previews that do not calculate (Quick Look, phones, mail) still show the right numbers, and long descriptions wrap with their rows sized to fit. Printed, the OPCC looks like the usual paper OPCC on US Letter: no lines inside the table or under the totals, the title and project block in a bold serif; on screen the sheet's gridlines show the cells both ways, and they are not printed. The PDF is the same page. The Summary draws its own grid lines, and every sheet is set to US Letter. The Summary is one page in the same style: the project, your OPCC (Sub-Total, contingency, Project Total) and how it compares with the model's P50 and P80. Then come every line with the model's prices and your notes, and a references appendix: for each line, its past projects, each with its own item as bid (words, unit, quantity, and low, average and median bid).

![The estimate for a public creek-bank schedule: expected low bid (P50) and recommended OPCC (P80) with their ranges, and one line opened to the earlier public bids behind its price](assets/screenshot-estimate.png)

*The estimate for a public creek-bank schedule (Coppell, 2025). P50 and P80 come with their ranges and a plain explanation of what each means. One line is opened to the three most relevant earlier public bids, with their source pages and public mirror copies where recorded.*

![The OPCC worksheet: the estimator's own unit prices, notes and a contingency next to P50 and P80, and the most similar past project opened to its full bid](assets/screenshot-worksheet.png)

*The OPCC worksheet. Your own unit prices, notes and a contingency picked from a list sit next to P50 and P80. Below it, the most similar past project is opened to its full public bid, with the lines that match yours highlighted.*

**Your inputs stay yours.**
- An engineer's estimate is never an input.
- Your prices never feed back into the model.
- Run locally, nothing leaves your machine.

## The goal

The estimate is meant to be used as an OPCC, where underestimating is the costly mistake. The target is:

1. **Floor:** the P80 estimate is below the actual low bid on **at most 20%** of lettings.
2. **Score:** within that floor, P80 should overshoot the low bid **as little as possible** (13% is the aspiration).
3. **Yardstick:** at the same not-under rate, the overshoot is compared with engineers' own estimates. Engineer estimates are never an input.

## Where it stands (2026-11-21)

All figures are forward in time: each letting is priced from lettings dated before it, and the model had not seen the letting.

| | Lettings | P50 median error | P80 under the low bid | Median overshoot (P80 ≥ low) | Overshoot at 70% / 75% / 80% not-under |
|---|---|---|---|---|---|
| **TxDOT** (DFW counties), 2023–26 | 807 | **16%** | 15% | 31% | **26% / 27% / 30%** |
| TxDOT engineers' own estimates (same 451 lettings, official totals) | 451 | 21% | 14% | — | 19% / 22% / 23% |
| **City / municipal** (DFW), 2024–26 | 235 | **20%** | 14% | 45% | **30% / 32% / 34%** |
| Municipal engineers' own estimates | 140 | ~15% | 33% | — | 20% / 21% / 25% |
| Locked public test set, municipal (last confirmation) | 56 | 14% | 20% | 31% | — |

Municipal figures use the "covered" scope (see below). The discount uses each letting's actual number of bidders, which is not known when quoting. On the previous data build, a forecast bidder count from earlier lettings of the same market, work type and size cost TxDOT about 2 points of overshoot and left municipal unchanged. Figures are from the current data build: parser .34b (2026-11-21), on top of 63 more public bid tabs from smaller DFW cities and counties, many found inside city council agenda packets. Parser round 12 (.33) moved 637 lines to the right item type: trail excavation and grading out of sidewalk (a 400 CY "Excavation (Trail)" had been priced like concrete sidewalk), "Earthwork" to excavation, and pipe abandonment, plugs and grout fill to utility removal work. On the same 224 city jobs, overshoot went from 46.3% to 45.8%; nothing else moved. Round 20 (installed 2026-11-21, parser .34b) taught the parser three item types it had left as "Other": flashing beacons, flashers, crosswalk lights, signal poles and mast arms are traffic signals; markings named by their line (stop bars, solid, broken or double yellow lines, raised markers, marked crosswalks) are pavement marking; signs named by an MUTCD code or a panel size are signs. The estimator did not change. On the 234 city jobs both builds score, the sum of |our line price − median bid| fell 0.25% (90% interval −$1.80M to −$0.19M), overshoot went from 45.3% to 44.6% and the P50 error from 19.7% to 19.4%; the P80 under rate did not change in any year, and TxDOT did not get worse. One more city job became scorable (235). Round 20 was registered after its first two arms had failed, so it carries a check on the first 40 city jobs bid after 2026-11-20. Round 18 (installed 2026-11-21) added two pricing rules. A bidder count above the discount table's top bin is now priced off the top bin, so 10 city jobs with 16–21 bidders get an estimate. Abandon, plug and grout-fill lines are priced only from abandonment history, never from new or removed pipe. On the same 224 city jobs, overshoot went from 45.8% to 45.5% and the P50 error from 20.2% to 19.9%; the P80 under rate did not change, and TxDOT did not move. Parser rounds 10 and 11 made three changes:
- they read more table layouts;
- they tell an engineer's column from a bidder's;
- they recover units printed in odd places, about 8,500 lines.

Many of the newly scorable jobs are hard to estimate, with specialty items the model prices low. Since the parser .29 build:
- overshoot rose from 42% to 46%;
- the overshoot at a matched 80% not-under rate rose from 32% to 34%;
- the median P50 error rose from 19% to 20%.

On the lettings that both builds score, the change is within noise. That build corrected misread bid dates and labels, priced a few more item wordings correctly, and stopped scoring annual / as-needed unit-price contracts as projects: their bid totals are an evaluation formula over notional quantities, not the price of a project. Their unit prices stay in the price history. The locked-set row is from the last confirmation, before the latest data fixes and the scope change. For the engineer rows, "P80 under the low bid" means their single published estimate came in under the low bid.

## Strengths and limitations

**Strengths**

- **TxDOT is close to engineer level.**
  - On 807 DFW TxDOT lettings, the median P50 error is 16%.
  - On the 451 of them with an official engineer's estimate, our median error (17%) is lower than the engineers' (21%), whose estimates run about 20% high.
  - At an 80% not-under rate, the engineers still overshoot less: 23% vs our 30%.
- **Standard items are priced close to the bidders' own spread.** On TxDOT, half of the asphalt, bridge concrete, concrete pavement, riprap, headwall, box culvert and storm pipe lines are within 14–20% of the median bid. In cities, the same holds for curb ramps, driveways and inlets (16–24%). See [Item-level accuracy](#item-level-accuracy).
- **The evaluation is strict.**
  - Every figure is forward in time.
  - Decisions are registered before they are scored.
  - A locked public test set is opened only to confirm a registered decision, and each opening is logged.
  - Leak guards stop any build in which test data reaches a fitted number.
- **Public data only.** Engineer estimates are a yardstick, never an input.
- **Every number can be traced.** The local web UI (`ui/`) prices a schedule with the same estimator. It shows the reference projects behind each line price, the four most similar past projects, and which lines are not covered or low confidence.
- **It says what it does not know.** Specialty items with almost no bid history are marked "not covered" and listed for separate (vendor) pricing instead of being guessed. Examples: pumps and pump/lift-station equipment, generators and electrical gear, prefabricated or pedestrian bridges, tanks and process equipment.

**Limitations**

- **City (municipal) estimates are less accurate.**
  - The median P50 error is 20%, against about 15% for the engineers.
  - At an 80% not-under rate the overshoot is about 34%, against the engineers' 25%.
  - Almost all of this error comes from estimating the bidders' typical (median) price for the schedule. Very little comes from the step from the median bid to the low bid.
- **Lump-sum items do not yet scale with job size.**
  - Mobilization, traffic control, site preparation and SWPPP / erosion-control lump sums are priced at about the same dollar amount on every job. Bidders price them as a share of the job (mobilization is typically about 5%).
  - Small city jobs (low bid under $0.5M, 74 lettings) come out about 20% high (P50 error 24%).
  - Jobs over $2M come out about 4% low, and P80 is under the low bid on 25% of them.
  - Pricing them as a share of the job fixes the lump-sum lines themselves, but it did not make job totals more accurate (round 12, under "Tried and not adopted").
- **Creek erosion and drainage** (42 lettings) is better than the city average: P50 error 18%, P80 under the low bid 2% of the time, median overshoot 41%. The recovered units brought in more recent creek and drainage prices. They raised this group's estimates by about 4%, so it now errs on the high side. Part of the drop in under-estimates (from 10%) is a relabelling, not better pricing: a lift station and an artificial-turf field that had been labelled drainage left this group.
- **Recent lettings run higher than history.** In 2026, P80 was under the low bid on 17% of municipal and 23% of TxDOT lettings; TxDOT is above the 20% floor. The guard adapts, but with a lag.
- **About a third of the worst municipal misses cannot be fixed this way.** They are ordinary market variation between bidders that bid history alone cannot predict. Another sixth are one-off specialty items, which are now marked not covered.
- **City data is thin.** Many city items have little bid history. Some cities publish bid tabs only behind vendor portals, which are not collected. Scanned tabs lose lines.
- **A full rebuild needs the document corpus.** The app and the estimator run from this repository with the public data bundle (see [Running it](#running-it)). Rebuilding the tables from documents needs the bid-tab corpus (several GB); the public documents repository holds most of it, not all.

**Tried and not adopted (yet).** Pricing municipal lines from municipal-only price data, instead of data dominated by TxDOT price levels, together with four smaller pricing fixes.
- **Forward tests:** passed the registered test. Municipal overshoot fell 49% → 42% and P50 error 24% → 20%.
- **Locked test set:** failed. Its tighter P80 margin let P80 fall under the low bid on 29% of lettings, against 19% for the current rule.
- **Status:** kept in the code, switched off. It will be re-checked on new lettings.

**Lump sums as a share of the job (round 12, registered before scoring).** Mobilization, traffic control, site preparation, SWPPP and bonds were priced as a share of the rest of the job. The shares were learned from earlier bids, by job size. A size-dependent centring of P50 was tested alone and together with it.
- **The lump-sum lines themselves became much more accurate.**
  - Against the median bidder, the typical miss fell from about 2.3× to 1.7×.
  - On small jobs our lump sums went from 2.8× the bidders' amounts to 0.9×; on large jobs from 0.56× to 0.97×.
- **Size-dependent centring removed the size bias of P50.** The median P50 ÷ low bid on small / medium / large jobs went from 1.26 / 1.07 / 0.94 to 1.10 / 1.05 / 1.00.
- **The spread of job-total errors did not shrink** (mean |ln| 0.331 either way). So the P80 margin needed to stay at or above the low bid 80% of the time stayed as wide.
  - No variant lowered the median overshoot by the registered margin.
  - The lump-sum variants pushed the 2026 under-rate just over its limit, by one letting.
  - On small jobs, lowering the lump sums exposed unit-priced lines that run low. On large jobs, most of the misses were too large for a few percent to rescue.
- **Status:** not adopted; production is unchanged. The code is kept in a development copy, switched off.

**Waiting for new lettings (round 13, registered before scoring).** Municipal lines priced from cells dominated by TxDOT lettings were moved to the municipal price level.
- **Development result:** the median overshoot fell from 43% to 38%, mostly because the P80 margin got tighter.
- **Status:** as registered, it stays switched off until 40 new municipal lettings after 2026-11-20 confirm it (`data/test_holdout/REGISTRATIONS.md`, section 6).
- **A second change** shrank prices backed by a single earlier letting. It fixed the worst single lines (a 48-inch water line went from 3.7× to 0.8× the bids), but it did not make job totals more accurate.

**Line fixes that did not narrow the margin (round 14, registered before scoring).** Three general fixes were tried, alone and together:
- a mobilization line that prints a cap ("5% Max") priced at that cap times the rest of the job;
- weakly matched item-word prices blended with the price for that kind of item at its size;
- round 13's shrinkage of prices backed by a single earlier letting.

What happened:
- **The lines became clearly more accurate.**
  - Over 8,766 city lines, the total miss against the median bidder fell from $299M to $266M with all three.
  - Capped mobilization lines went from 2.0× to 1.0× the bids.
- **Job totals did not.** No variant lowered the median overshoot by the registered margin.
- **None passed the line-accuracy route either.**
  - That route was registered this round, because the worksheet shows our line prices. It also requires that the median overshoot not rise by more than 1 point at the upper end of its 90% interval.
  - The variants reached +1.9 to +2.9 points.
- **Why the totals did not move.**
  - On small jobs, correcting the overpriced mobilization exposed unit-priced lines that run low.
  - On large jobs, the misses come from weakly matched main items, not from mobilization.
- **Status:** not adopted; production is unchanged.

**Weighting recent jobs more in the level correction (round 15, registered before scoring).** Half-lives of 12 and 24 months were tried.
- **It did not help.** The price-level drift between years is a step, not a trend, so weighting recent years more fixed 2026 but pushed 2024 further off. Year-to-year level explains only 0.2% of the spread of job-total errors.
- **A level change cannot narrow P80 anyway.** P80's margin is learned from the same centred ratios, so a correction shared by all jobs cancels out of P80.
- **Status:** not adopted. The overshoot can only come down if single jobs are priced more accurately.

**Park equipment as "not covered" (registered before scoring).** Playground units, shade structures, dog-park gear and similar vendor-priced park items are bought by model. In the bid history they spread 10–100× across jobs, and the model priced them at a fraction of the bids, about $1,000 for a $165,000 play unit. Marking them "not covered" (left out of P50 / P80, vendor quote asked for) was tested on 34 line types in 6 lettings.
- **It did not pass.** On the two small park jobs, removing the equipment left mostly lump sums, and our mobilization there was about $140,000 against the low bidder's $25,000. The underpriced equipment had been hiding that.
- **Status:** not adopted. It waits for the small-job lump-sum problem to be fixed, which keeps showing up in rounds 12 and 14 and here.

**Converting between units (registered before scoring).** Takeoffs come in mixed units: CY or CF, SY or SF or acres, tons or pounds. Two ways of converting between units of the same kind were tested.
- **Pooling all bids into one unit per kind** makes a takeoff price the same in any unit, to the cent.
  - Today, rewriting the units of 188 city takeoffs changes P50 by more than 20% on 79 of them, and leaves 624 lines unpriced.
  - It was still not adopted, because line accuracy got worse. A rarer unit often marks a different product: seeding by the acre costs about a quarter of sod by the square yard, and sidewalk bid by the square foot runs 1.5 times the square-yard price. Converting thousand-gallon watering into gallons also pushed every quantity into the top quantity class.
- **Converting only lines whose own unit has fewer than 5 earlier lettings** cut the unpriced lines from 624 to 42. The few converted lines were often different products or misclassified, so it did not pass either.
- **Leaving such lines unpriced** was tested too: a line resting on fewer than 5 earlier lettings in a unit that is rare for its item type, when the usual unit has 5 or more. It touched only 15 city lettings and narrowly failed the registered test.
- **Status:** none of the three is used for P50/P80. The worksheet shows a converted reference price as a hint, for the estimator to accept or not. For example: "only 1 earlier letting prices this in CF; 622 price it in CY: about $0.78/CF".

## Item-level accuracy

How close the price of each kind of line item is, before the lines are added up. Job totals are more accurate than single lines, because line errors partly cancel.

**How it is measured**
- Every line of every scored letting is priced forward in time: from earlier lettings only.
- The comparison uses the line price itself, before the comparables blend and the competition discount.
- It is compared with the **median bidder's** amount on the same line, on lines with at least 3 bidders. Owner-set and not-covered lines are left out.

**Columns**
- "Half of lines within": half of the lines are closer to the median bid than this.
- "Ours ÷ median bid": the typical ratio. Above 1 means we run high.
- "Low bidder vs median bidder": how far the low bidder usually is from the median bidder on the same line. That is the spread between bidders themselves, which no estimate can remove.

**What it shows**
- **Standard concrete, drainage and paving items are already close to the bidders' own spread.** Examples: curb ramps, driveways, inlets, sidewalk, concrete pavement, storm pipe and box culverts in cities; riprap, headwalls, box culverts, storm pipe and concrete pavement on TxDOT.
- **Lump sums and other general-condition items are the weakest in both markets.**
  - In cities, mobilization, traffic control, site preparation and SWPPP lump sums are priced at about the same dollar amount on every job, while bidders price them as a share of the job. A share-of-job rule makes these lines much closer to the bids (round 12) but did not improve job totals, so it is not in use yet.
  - On TxDOT, mobilization is already priced as a share of the job.
- **Many items are 6–9 points more accurate on TxDOT than in cities:** sidewalk, concrete pavement, storm pipe and box culverts.
  - TxDOT's standard item codes keep descriptions uniform and history deep. City wording varies, and city data is thinner.
  - Curb ramps, driveways and inlets are about as accurate in cities as on TxDOT.
- **The city items with the most money still to gain:**
  - water pipe (8% of city dollars, half within 44%);
  - mobilization (5%, runs 46% high);
  - asphalt (4%, half within 36%);
  - unclassified lines (4%, half within 98%).
  - Fire hydrants run 31% low, pavement marking 20% low, and miscellaneous concrete 13% high.

#### City / municipal (DFW), 2024–26: 235 lettings, 10,328 lines

Across all lines, half are within **46%** of the median bid (**40%** weighted by dollars); 28% are within ±20%. The low bidder is typically 25% from the median bidder on the same line.

| Item | Lines | Lettings | Share of dollars | Half of lines within | Dollar-weighted | Ours ÷ median bid | Lines within ±20% | Low bidder vs median bidder |
|---|---|---|---|---|---|---|---|---|
| ADA curb ramps | 209 | 98 | 0.8% | 16% | 16% | 0.98 | 56% | 23% |
| Driveways | 133 | 89 | 1.5% | 18% | 14% | 0.98 | 53% | 20% |
| Valves and hydrants | 192 | 82 | 2.0% | 22% | 37% | 0.91 | 46% | 16% |
| Sidewalk | 226 | 133 | 3.6% | 23% | 19% | 1.07 | 46% | 21% |
| Inlets | 278 | 70 | 1.6% | 23% | 24% | 1.00 | 47% | 18% |
| Project signs | 79 | 73 | 0.0% | 24% | 35% | 1.03 | 42% | 33% |
| Signs | 95 | 54 | 0.1% | 24% | 12% | 1.03 | 44% | 18% |
| Concrete pavement | 209 | 110 | 12.4% | 25% | 20% | 1.01 | 41% | 14% |
| Box culverts | 68 | 32 | 1.5% | 27% | 22% | 0.92 | 41% | 17% |
| Storm pipe | 329 | 85 | 3.0% | 28% | 23% | 0.91 | 37% | 20% |
| Water services | 147 | 58 | 2.1% | 28% | 15% | 0.87 | 41% | 23% |
| Flexible base | 116 | 89 | 2.5% | 31% | 45% | 0.94 | 35% | 27% |
| Drilled shafts | 57 | 28 | 1.6% | 33% | 38% | 0.80 | 30% | 15% |
| Embankment / fill | 66 | 53 | 0.7% | 34% | 49% | 1.15 | 29% | 57% |
| Concrete beams | 47 | 31 | 0.5% | 34% | 53% | 0.91 | 26% | 40% |
| Stone riprap | 59 | 45 | 0.8% | 36% | 36% | 0.88 | 25% | 29% |
| Asphalt pavement | 181 | 89 | 4.4% | 36% | 29% | 0.98 | 31% | 19% |
| Subgrade stabilization | 138 | 65 | 2.7% | 37% | 40% | 0.82 | 30% | 18% |
| Traffic signals | 258 | 25 | 0.8% | 39% | 57% | 0.99 | 40% | 11% |
| Seeding and sod | 373 | 188 | 1.9% | 39% | 56% | 0.92 | 32% | 25% |
| Pavement removal | 157 | 95 | 1.6% | 40% | 47% | 0.88 | 25% | 30% |
| Excavation | 153 | 114 | 2.7% | 40% | 37% | 0.94 | 25% | 40% |
| Tree removal | 69 | 46 | 0.1% | 40% | 62% | 0.95 | 22% | 32% |
| Testing and inspection | 71 | 42 | 0.3% | 41% | >1000% | 1.02 | 27% | 32% |
| Electrical and lighting | 427 | 46 | 2.0% | 42% | 65% | 0.85 | 28% | 13% |
| Water pipe | 298 | 74 | 7.8% | 44% | 40% | 0.98 | 29% | 26% |
| Erosion control (unit-priced) | 183 | 81 | 0.4% | 44% | 84% | 0.82 | 26% | 33% |
| Manholes | 175 | 69 | 2.0% | 45% | 69% | 0.93 | 30% | 21% |
| Fire hydrants | 67 | 61 | 0.5% | 46% | 33% | 0.69 | 28% | 16% |
| Casing / encasement | 166 | 75 | 2.8% | 46% | 161% | 0.87 | 25% | 25% |
| Miscellaneous concrete | 147 | 83 | 1.1% | 46% | 39% | 1.13 | 19% | 27% |
| Sidewalk removal | 112 | 65 | 0.2% | 46% | 34% | 1.04 | 25% | 30% |
| Adjust to grade | 179 | 80 | 0.2% | 48% | 48% | 1.07 | 28% | 43% |
| Utility removal / abandonment | 313 | 103 | 0.6% | 49% | 44% | 0.94 | 23% | 45% |
| Sanitary sewer pipe | 131 | 50 | 2.8% | 49% | 51% | 1.00 | 22% | 41% |
| Curb and gutter | 144 | 98 | 0.7% | 49% | 41% | 1.09 | 28% | 36% |
| Traffic control (unit-priced) | 80 | 58 | 0.6% | 52% | 53% | 1.31 | 20% | 29% |
| Pipe fittings | 65 | 39 | 0.3% | 52% | 49% | 1.18 | 28% | 31% |
| Sewer services | 51 | 32 | 0.3% | 55% | 61% | 1.24 | 20% | 36% |
| Relocate / reset | 189 | 81 | 0.2% | 58% | 96% | 0.87 | 23% | 42% |
| Guardrail | 61 | 27 | 0.1% | 59% | 59% | 0.69 | 26% | 22% |
| Other removals | 173 | 88 | 0.5% | 59% | 66% | 0.94 | 24% | 39% |
| Retaining walls | 58 | 34 | 1.6% | 59% | 62% | 1.00 | 19% | 30% |
| Pavement marking | 681 | 93 | 0.5% | 60% | 79% | 0.80 | 20% | 21% |
| Trench safety | 127 | 85 | 0.2% | 65% | 80% | 0.89 | 20% | 69% |
| Tree planting | 71 | 47 | 0.2% | 65% | 222% | 0.94 | 30% | 14% |
| Contingency / allowance lines | 78 | 23 | 0.5% | 68% | 102% | 0.94 | 27% | 21% |
| Fence | 103 | 65 | 0.4% | 72% | 177% | 0.86 | 19% | 27% |
| Structure demolition | 94 | 58 | 0.1% | 74% | 244% | 0.89 | 15% | 34% |
| Headwalls | 60 | 42 | 0.4% | 83% | 309% | 0.73 | 8% | 46% |
| SWPPP / temporary erosion control (lump sum) | 170 | 157 | 0.3% | 98% | 161% | 1.11 | 12% | 49% |
| Unclassified lines | 779 | 164 | 3.7% | 98% | 277% | 0.95 | 16% | 29% |
| Landscape planting | 103 | 23 | 0.2% | 100% | 94% | 1.61 | 17% | 11% |
| Traffic control (lump sum) | 151 | 141 | 0.9% | 119% | 242% | 1.40 | 10% | 59% |
| Site preparation (lump sum) | 82 | 77 | 1.3% | 128% | 388% | 0.97 | 20% | 84% |
| Irrigation | 75 | 44 | 0.6% | 136% | 153% | 1.21 | 4% | 17% |
| Mobilization | 205 | 203 | 4.7% | 142% | 379% | 1.46 | 11% | 35% |
| Site preparation / clearing | 47 | 32 | 1.0% | 168% | 789% | 1.16 | 13% | 100% |

#### TxDOT (DFW counties), 2023–26: 807 lettings, 41,159 lines

Across all lines, half are within **30%** of the median bid (**26%** weighted by dollars); 37% are within ±20%. The low bidder is typically 16% from the median bidder on the same line.

| Item | Lines | Lettings | Share of dollars | Half of lines within | Dollar-weighted | Ours ÷ median bid | Lines within ±20% | Low bidder vs median bidder |
|---|---|---|---|---|---|---|---|---|
| Construction exits | 173 | 172 | 0.1% | 13% | 17% | 0.99 | 65% | 17% |
| Asphalt pavement | 651 | 282 | 11.5% | 14% | 12% | 0.93 | 62% | 9% |
| Bridge concrete | 426 | 75 | 5.1% | 14% | 11% | 0.93 | 62% | 15% |
| Concrete mow strip | 189 | 183 | 0.5% | 15% | 16% | 1.06 | 64% | 11% |
| Sidewalk | 160 | 113 | 0.6% | 15% | 9% | 0.97 | 59% | 12% |
| Construction exit removal | 170 | 170 | 0.0% | 16% | 15% | 1.06 | 58% | 17% |
| Headwalls | 218 | 78 | 0.1% | 17% | 15% | 0.99 | 57% | 9% |
| Concrete riprap | 252 | 192 | 0.5% | 17% | 10% | 1.02 | 57% | 15% |
| Stone riprap | 277 | 197 | 0.9% | 18% | 19% | 0.94 | 55% | 14% |
| Concrete pavement | 132 | 83 | 8.1% | 19% | 8% | 0.92 | 52% | 12% |
| Flowable backfill | 192 | 161 | 0.3% | 19% | 21% | 0.92 | 53% | 17% |
| Box culverts | 362 | 98 | 2.1% | 19% | 20% | 0.94 | 51% | 15% |
| Concrete beams | 64 | 55 | 0.1% | 19% | 19% | 0.95 | 55% | 25% |
| Storm pipe | 634 | 150 | 1.9% | 19% | 16% | 1.01 | 52% | 14% |
| Casing / encasement | 316 | 127 | 0.6% | 19% | 49% | 0.92 | 52% | 12% |
| Bridge railing | 267 | 129 | 1.1% | 20% | 24% | 0.95 | 51% | 15% |
| Lighting assemblies | 160 | 56 | 0.3% | 20% | 36% | 0.86 | 49% | 6% |
| Traffic signals | 2266 | 116 | 1.5% | 20% | 28% | 0.89 | 49% | 10% |
| Inlets | 543 | 75 | 1.1% | 21% | 25% | 0.86 | 48% | 16% |
| Signs | 1392 | 262 | 0.7% | 21% | 27% | 1.04 | 48% | 9% |
| ADA curb ramps | 397 | 102 | 0.2% | 22% | 22% | 0.91 | 48% | 13% |
| Drilled shafts | 574 | 166 | 3.6% | 22% | 32% | 0.86 | 45% | 13% |
| Driveways | 224 | 137 | 0.7% | 22% | 21% | 0.95 | 44% | 13% |
| Flexible base | 127 | 109 | 1.4% | 23% | 17% | 0.91 | 46% | 11% |
| Erosion control (unit-priced) | 1717 | 412 | 0.3% | 24% | 20% | 0.97 | 44% | 20% |
| Wingwalls | 293 | 94 | 0.3% | 24% | 23% | 0.95 | 41% | 14% |
| Borrow / backfill | 141 | 136 | 0.1% | 24% | 21% | 1.05 | 44% | 25% |
| Pavement milling | 236 | 196 | 0.9% | 24% | 21% | 0.98 | 43% | 15% |
| Electrical and lighting | 2109 | 142 | 1.1% | 25% | 42% | 0.85 | 42% | 12% |
| Safety end treatments | 516 | 125 | 0.2% | 25% | 23% | 0.92 | 42% | 17% |
| Retaining walls | 65 | 51 | 2.6% | 26% | 23% | 0.89 | 35% | 23% |
| Subgrade stabilization | 353 | 139 | 2.3% | 29% | 27% | 0.85 | 32% | 11% |
| Concrete joints | 267 | 151 | 0.4% | 29% | 40% | 0.88 | 40% | 21% |
| Concrete barrier | 855 | 181 | 2.0% | 30% | 24% | 1.01 | 36% | 25% |
| Guardrail | 1690 | 289 | 0.8% | 30% | 24% | 1.22 | 33% | 11% |
| Curb and gutter | 288 | 160 | 0.3% | 34% | 30% | 0.90 | 35% | 22% |
| Shoring | 113 | 109 | 1.3% | 34% | 48% | 0.94 | 31% | 27% |
| Surface treatment | 360 | 261 | 0.2% | 34% | 33% | 1.08 | 33% | 29% |
| Embankment / fill | 291 | 229 | 3.0% | 35% | 35% | 1.04 | 31% | 29% |
| Concrete repair | 304 | 128 | 0.7% | 35% | 34% | 1.16 | 30% | 27% |
| Pavement marking | 8266 | 423 | 1.6% | 36% | 26% | 1.13 | 32% | 12% |
| Seeding and sod | 1052 | 299 | 0.8% | 37% | 23% | 1.02 | 30% | 32% |
| Testing and inspection | 98 | 44 | 0.0% | 37% | 17% | 1.11 | 36% | 19% |
| Pavement removal | 433 | 218 | 0.8% | 38% | 38% | 0.89 | 27% | 20% |
| Miscellaneous concrete | 238 | 141 | 0.8% | 38% | 47% | 0.86 | 28% | 25% |
| Other removals | 674 | 277 | 0.7% | 40% | 27% | 0.97 | 30% | 26% |
| Mowing | 451 | 193 | 0.8% | 41% | 21% | 1.22 | 29% | 23% |
| Excavation | 348 | 223 | 1.8% | 42% | 66% | 0.97 | 28% | 32% |
| Fence | 277 | 122 | 0.3% | 43% | 49% | 1.04 | 31% | 25% |
| Utility removal / abandonment | 622 | 188 | 0.9% | 44% | 271% | 0.91 | 28% | 41% |
| Unclassified lines | 3749 | 550 | 10.1% | 44% | 31% | 1.00 | 28% | 17% |
| Sidewalk removal | 252 | 151 | 0.1% | 47% | 57% | 0.96 | 24% | 22% |
| Adjust to grade | 89 | 56 | 0.0% | 48% | 114% | 0.91 | 25% | 43% |
| Mobilization | 913 | 806 | 9.8% | 51% | 40% | 1.06 | 28% | 25% |
| Relocate / reset | 118 | 70 | 0.0% | 54% | 489% | 0.91 | 30% | 28% |
| Manholes | 67 | 26 | 0.1% | 55% | 209% | 0.70 | 30% | 21% |
| Crash cushions | 500 | 124 | 0.5% | 55% | 58% | 1.32 | 19% | 27% |
| Tree removal | 253 | 59 | 0.1% | 57% | 49% | 1.33 | 22% | 60% |
| Cleaning / desilting | 150 | 104 | 0.2% | 59% | 58% | 0.81 | 22% | 28% |
| Traffic control (per month) | 2211 | 784 | 3.9% | 65% | 243% | 1.24 | 23% | 63% |
| Structure demolition | 119 | 73 | 0.1% | 65% | 531% | 0.81 | 23% | 38% |
| Preparing right of way / clearing | 242 | 228 | 5.8% | 160% | >1000% | 1.24 | 9% | 71% |

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
| `ui/` | A small local web app for pricing a schedule, with its tests |
| `deploy/` | The public site: package files, server setup, data-bundle tools, the commit guard |
| `data/test_holdout/` | Registry of the locked public test set and of every registered decision (file list only) |
| `docs/` | For maintainers: [handoff](docs/HANDOFF.md), [history of everything tried](docs/HISTORY.md), [method and mathematics](docs/METHOD.md), [data sources and cleaning](docs/DATA.md) |
| `AGENTS.md` | Standing rules and commands for anyone (or any coding agent) working here |

Published separately:

- **The public data bundle:** the built tables the app reads, public lettings only (about 350 MB unpacked, 21 MB to download). It is a release asset of this repository; `deploy/tools/fetch_bundle.py` downloads and verifies it.
- **The original bid documents:** https://github.com/grintCactus/texas-public-works-bid-tabs (2,150 public bid tabulations and award documents, with each file's source page).

Not included:

- **The working corpus and the full built tables** (several GB), and the rows of the locked public test set.
- **Experiment reports and working files** of each round; `docs/HISTORY.md` summarizes them.
- **Anything private.** A private test set exists outside this repository; a few scripts that only read it are not published, and the rebuild script skips their steps where they are absent.

## Running it

Requires Python 3.12 on macOS or Linux.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python deploy/tools/fetch_bundle.py     # the public data bundle -> data/clean
.venv/bin/python ui/run.py                        # http://127.0.0.1:8017/
```

Pricing is ready about 10 seconds after start. Reading PDF takeoffs and scanned uploads also needs `pdftotext` (poppler) and Tesseract (macOS: `brew install poppler tesseract`; Debian/Ubuntu: `apt-get install poppler-utils tesseract-ocr`).

Tests: `.venv/bin/python ui/run.py --test`. A known-answer check and everything else a maintainer needs are in [`docs/HANDOFF.md`](docs/HANDOFF.md). Updating the public site: [`deploy/README.md`](deploy/README.md).

A full rebuild of the tables (`bash src/rebuild_all.sh`) needs the document corpus under `data/external/` and `data/txdot/`; see [`docs/DATA.md`](docs/DATA.md).

## License

No license has been chosen yet; all rights reserved by the author.
