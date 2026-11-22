# Data: sources, collection and cleaning

This file describes the data behind the Texas public-works bid-price estimator:

- where the data comes from;
- how it was collected;
- how raw documents (PDFs, scans, spreadsheets) are cleaned into line-by-line bid prices that can be trusted.

It is written for an engineer who takes the project over and has to extend the collection or the cleaning pipeline.

**Conventions**

- Counts are from the data build of 2026-11-21 (parser version `2026-11-21.34b-r20b-families`) unless another date is given. Times are local times of the project machine.
- Paths are relative to a working copy. The public repository holds the code and the register of the locked test set (`data/test_holdout/`). The rest of `data/`, the `handoff/` folder and the working reports are not in it.
- "Not recorded" means the project's records do not give the fact. Nothing in this file is estimated.
- A *letting* is one bid opening. A *bid tabulation* (bid tab) is the table that lists every bidder's unit price for every line item. An *engineer's estimate* (or OPCC, opinion of probable construction cost) is the designer's own price for the same schedule.
- An *agency* is the city, county or district that let the work. The code and the folder names call it `owner`. *The project owner* is the person responsible for this project.
- DFW means 14 counties: Dallas, Tarrant, Collin, Denton, Ellis, Rockwall, Johnson, Parker, Kaufman, Hunt, Wise, Hood, Somervell, Navarro.

---

## 1. Summary

### 1.1 What the estimator is built on

The estimator prices a bid schedule from earlier public bids. After cleaning, all data sits in one panel. One row is one bidder's unit price for one line item of one letting.

**The panel (build of 2026-11-21)**

| Source | Price basis | Rows | Projects | Bidders | Bid dates |
|---|---|---|---|---|---|
| City and county bid tabs | every bidder's unit price | 233,409 | 1,071 | 2,533 | 2020-01-03 to 2026-11-06 |
| City bid tabs whose bidders did not price the same schedule | `uneven_proposal`; kept out of the reference | 332 | 5 | 40 | 2021-07-16 to 2026-07-31 |
| Garland average bid price reports | contract price | 829 | 56 | 1 | 2023-02-16 to 2024-12-19 |
| TxDOT, current open data, DFW | every bidder's unit price | 200,794 | 1,066 | 285 | 2024-11-05 to 2026-11-16 |
| TxDOT archive, DFW | every bidder's unit price | 431,859 | 2,024 | 477 | 2015-06-16 to 2024-12-04 |
| **Total** | | **867,223** | **4,222** | 3,101 contractors | |

**Which rows may set a price**

By number of bidders:

| Class | Meaning | Rows | Projects |
|---|---|---|---|
| `competitive` | 3 or more bidders | 768,729 | 2,826 |
| `thin` | 2 bidders | 39,727 | 653 |
| `sole` | 1 bidder | 6,487 | 297 |
| `unknown` | no bidder structure in the source (the Garland reports) | 851 | 57 |

Only `competitive` rows build reference prices. The classes cover fewer rows than the panel; the build report does not say why.

By content: 3,524 projects are construction, 97 are mixed and 601 are not construction (services, supplies, equipment). Only construction projects build reference prices. Most of the 601 come from the TxDOT archive (249), TxDOT (170), Collin County (125), Mesquite (20) and the Legistar cities (19). The project summary, written from the build before parser .34b, counts 606.

**The file index of the municipal corpus** (`data/external/INDEX.csv`)

- 1,703 files in 78 agency folders (the Legistar batch counts as one folder).
- 971 files can give unit prices: 838 full line-item tables, 120 probable line-item tables, 10 municipal average-price tables, 3 structured datasets.
- 708 files give totals only. 24 could not be read.
- Text comes from a text layer in 1,685 files and from OCR in 18.
- By bid year: 2020: 30; 2021: 269; 2022: 329; 2023: 258; 2024: 297; 2025: 311; 2026: 193; no year: 16.
- 185 line-item files are erosion or drainage work.

**What is scored** (public README, 2026-11-21): 807 DFW TxDOT lettings of 2023–26 and 235 DFW municipal lettings of 2024–26. A locked public test set of 71 files (59 letting groups) never trains anything.

**How fast it grew.** On 2026-11-18 the training side held 701,439 rows and 3,550 projects; only 60,586 rows and 391 projects were municipal. Three days later the municipal side had 1,071 projects. After 2026-11-20 01:48, 194 public tabs were merged: 31 from the overnight erosion/drainage harvest, 47 / 30 / 23 / 17 / 35 / 5 from city rounds 1–6, and 5 + 1 held tabs released by parser rounds 10 and 11.

### 1.2 The rule: public data only

- **Public channels only.** Under the Texas Public Information Act, bid results are public records. Cities generally publish them one or two working days after the opening, or as attachments to a council agenda.
- **Line items, not totals.** Another project's total is nearly useless, because scopes differ. Each file is classified first. Only files with a line-item schedule are modelled. Totals-only files are marked `summary_only` and not used.
- **Real bids only.** No supplier quotes. No survey indices for pricing or escalation. Engineer estimates and OPCCs are a yardstick, never an input.
- **Company files are test data.** A private set of company bid files exists. It never trains anything (section 9).

### 1.3 Where the original documents are published

The original public documents are in the companion repository:

https://github.com/grintCactus/texas-public-works-bid-tabs

- It holds 2,150 public bid tabulations and award documents, with each file's source page.
- At its first upload (2026-11-20) it held 1,830 PDF, 287 ZIP (TxDOT monthly archives), 17 DOCX and 16 XLSX files in 83 agency folders, with a `README.md` and a `manifest.csv`.
- `manifest.csv` then had a source link for 1,708 files and a date for 2,112.
- Left out at publication: 33 files stopped by the private confidentiality scan, 34 byte-identical duplicates, 1 file over 95 MB, and 5 housing-repair tabs that print private home addresses.
- 21 files named `.pdf` were really Word (17) or Excel (4) files; they are published under their true extension.

The three counts above (1,703 indexed files, 2,150 published files, 4,222 panel projects) measure different things. The index covers the active municipal corpus. The published set also holds archive ZIPs and out-of-window files. A panel project is one letting.

---

## 2. Sources

### 2.1 TxDOT

TxDOT is the largest source. It is the only one that carries an engineer's estimate on every letting, so it is also the best place to test a method. The estimator's focus is municipal work. Since 2026-11-20 TxDOT is treated as the fallback price source where city history is thin, and as a demonstration of the method.

**(a) Open data portal: the "Bid Tabulations" dataset**

- Page: https://data.texas.gov/dataset/Bid-Tabulations/de7b-7dna (Socrata dataset `de7b-7dna`; API `https://data.texas.gov/resource/de7b-7dna.json`).
- Published by TxDOT. It holds state lettings and local lettings.
- One row is one bidder's unit price for one bid item. The same row carries the engineer's estimate unit price, the low-bidder flag, the bid rank, the quantity, county, district, let date and contractor name.
- The engineer's estimate appears as a vendor ("Txdot Engineer's Estimate", rank `EE`). It is flagged and kept out of every bid statistic.
- Fetched by `src/fetch_txdot_bidtabs.py`, 25,000 rows per page, in two modes:
  - `--mode dfw`: every bid item in the 14 counties, to `data/external/txdot/txdot_bidtabs_dfw.csv`;
  - `--mode erosion_items`: channel-erosion pay items statewide, by 13 description keywords (GABION, MATTRESS, RIPRAP, ROCK FILTER, SOIL RETENTION, RETAINING WALL, SOIL NAIL, ROCK NAIL, EROSION, CHANNEL, FLUME, BLOCK SOD, FILTER FABRIC), to `txdot_bidtabs_erosion_statewide.csv`.
- Counts:
  - DFW file: 209,104 rows, 291 bidders, lettings 2024-11 to 2026-11. A re-fetch on 2026-11-19 added two newly published lettings (58 rows), giving 209,162. The earlier rows were identical row by row.
  - Statewide erosion items: 44,288 rows, of which 19,126 can price channel repair. The statewide pull is necessary: of 939 gabion rows only 114 are in the 14 DFW counties.
  - Full statewide pull (2026-11-19 22:15–22:24): 42 pages, 1.5 seconds between pages, dataset version checked before and after. 1,048,348 rows: 235,792 engineer-estimate rows and 812,556 bidder rows. Let dates run 2024-09-06 to 2026-11-19. Of 8,029 lettings, 4,222 have bidder quotes. This file serves only the all-bidders-median benchmark. It does not enter the pipeline.
- Traps:
  - **Rolling window.** The dataset keeps only the last 24 months and deletes older rows. Both fetch modes must be re-run regularly and the history accumulated locally.
  - **Certificates.** The site's certificate chain ends in "GlobalSign Root R46". The certificate store bundled with Python on the Mac lacks it, so Python HTTPS requests fail. Use curl, or point Python at the system certificate file.
  - **"Engineer" in a name.** An early loader treated every vendor name containing "engineer" as the engineer's estimate. That deleted a real contractor whose name contains "Engineering": 2,988 of 209,104 rows in 32 lettings. In 5 lettings the low bid was replaced by the second-lowest bid (0.7% to 10.9% too high), and one single-bidder letting (107 rows) vanished. The fix uses a strict pattern together with the authoritative field `bid_rank_sequence_number == "EE"`. The two agree exactly: 45,827 rows each.
  - **Re-lets.** One project id can be let twice. Bidders are counted per (project id, let date), and the project key becomes `project_id::let date` when a project was let twice. Before that, one bidder's two bids were added together and the bidder fell from first to last.
  - **A+B lettings.** The time part (item 800-x001, priced per working day) is not contract money. It is kept out of the line items and stored apart.

**(b) The bid tabulation archive**

- Directory: https://ftp.txdot.gov/pub/txdot-info/cmd/cserve/bidtab/ (303 monthly zips; the newest is 2024-12).
- One zip is one month, statewide. The file-name prefix gives the kind: `sc` construction, `sm` maintenance contracts, `ll` routine and material contracts.
- Inside, each project is one fixed-width `.TXT` file with the engineer's estimate and every bidder's unit prices.
- Downloaded on 2026-11-17: all 303 zips (32 MB), 13,956 project tabs, of which 2,024 are in the 14 DFW counties.
- Years: the project summary and the parser call the archive 2015–2024, and the parsed DFW rows run from 2015-06-16 to 2024-12-04. The guide to the source pages heads it "2010–2024".
- Parsed by `src/parse_txdot_archive.py`.
- Traps:
  - Bidders wrap in threes. Only bidders 1–3 sit on the item row; the others follow on "BIDDERS n - m" continuation rows.
  - Item codes are 7, 8, 10 or 11 characters long. The first parser accepted only 7, and a rejected item's continuation rows overwrote the previous item. After the fix the rows went from 285,067 to 432,138 (+51.6%) and the distinct items from 4,774 to 8,706.
  - Alternate rows carry a `1X`..`5X` prefix.
  - Item codes changed generation: the archive is almost all 6xxx, the current feed 7xxx. Joining on code matches almost nothing and raises no error. A crosswalk is built on spec item, description and unit.
  - The text is cp1252, not UTF-8.
- Quality: in 99.97% of "tab × item" groups each bidder has exactly one price.
- Overlap: the current feed and the archive publish some lettings twice under unlike keys. 22 lettings (6,813 rows, 12.39% of the 2024 dollars) were counted twice until the cross-source de-duplication was added.

**(c) The published average prices**

- Dashboard: https://tableau.txdot.gov/views/BidItemAverageCost/BidItemAvgCostDashboard
- It gives the low bidders' weighted average price per bid item. It has no project, no contractor and no per-project quantity.
- Scraped on 2026-11-17 with Bidder Rank = Low Bidder and let dates = Last 12 months, for four geographic scopes. Result: 8 Excel crosstabs, 9,183 item rows and 30,381 "item × letting date" rows.
- Export as Excel. The CSV export rounds unit prices to whole dollars.
- Parsed by `src/parse_txdot_bidtabs.py` into `data/txdot/`.
- Use: comparison only. All 2,756 DFW items were recomputed from the open-data rows. That showed two rules the dashboard does not state: one project is left out, and a re-let project counts once.
- The project then built an all-bidders-median table of 288,821 rows. It runs about 8% above the dashboard's low-bid average (13% for items with at least 5 lettings). No single factor converts one into the other.

**(d) Plans Online (not used).** TxDOT plan sets are kept for about two years. Downloading means accepting a licence (no resale; the downloader indemnifies TxDOT). Only one 5 MB file was downloaded as a test.

### 2.2 City and county bid archives

Some agencies publish bid tabs on a purchasing or bid-results page. This was the first municipal batch (2026-11-17 to 11-18).

What they give: line-item tabs with every bidder's unit prices. Some tabs print the engineer's estimate as one more column. Some agencies publish totals only.

| Agency | Public page | How it is fetched | Files | Notes and traps |
|---|---|---|---|---|
| Arlington | https://www.arlingtontx.gov/Government/Budget-Finance/Purchasing/Bidding-Procurement | folders by year; the page expands them with JavaScript | 61 | Uniform format. 11 are drainage or erosion (PWDR). Harvests on 2026-11-20 logged 403 for scripted requests to the main site and file store; the purchasing page answered 200 on 2026-11-21. The old document library answers 403. |
| Mesquite (purchasing site) | `https://apps.cityofmesquite.com/purchasingweb/documents/awarded/<year-number>.pdf` | file URLs enumerated by bid number | 144 | 4 are direct erosion work (gabions, concrete channel repair). Annual material contracts are flagged `materials_supply`. In round 3 the 34 construction bid numbers of 2023–2026 all returned 404; newer bids are on Mesquite's Legistar. |
| Garland | URL not recorded | the city's own "Average Bid Price" reports | 2 editions (2023, 2024) | Not bid tabs. Under each item the report lists one contract price per project. Parsed by `src/parse_garland_avg.py`. |
| Rowlett | https://www.rowletttx.gov/1438/Public-Openings-Tabulations-Pre-Bid-Conf | bid-results page | 32 | Line-item tabs for 2021–2024. From 2025 only a "Bid Tabulation Summary" (totals). |
| Dallas County | https://www.dallascounty.org/departments/purchasing/currentbidtabs.php | file URLs follow a pattern by year | 6 | One or two road projects a year. The page links yearly Bid Tabulations pages for 2009–2025. Bid dates are on the Bid Readings page. |
| Denton (city) | Laserfiche document library (URL not recorded) | by document number | 2, then 15 more on 11-19 | Document numbers run through the whole purchasing library. Listing them all would mean downloading tens of thousands of files. Stopped. |
| Collin County | https://eagenda.collincountytx.gov/agenda_publish.cfm?id=&mt=ALL | `src/fetch_collin_bidtabs.py` walks the agenda ids and scrapes the attachment links | 540 | The document folder cannot be listed and file names must match exactly, so the agendas are the only way in. Most files are service and supply purchases. Filter by content. |
| Tarrant County | not recorded | earlier award PDFs | part of the 11-19 county files | The index page is gone. New tabs are in a vendor portal that needs a login. The county tab prints no bid-opening date; the file name carries the award date. |

Reference material, not part of the DFW unit-price reference:

- San Antonio's Average Unit Bid Price list (June 2022): a mean of all bids over 12 months. It is used for magnitude and for an outside check of the quantity effect. It holds the only outside price for high-performance turf reinforcement mat ($20.50/SY).
- The Dallas Water Utilities "Average Costs Manual for Pipeline Construction Cost Estimation" (September 2021): a trimmed mean of bids with standard deviation and count.
- NRCS Emergency Watershed Protection files.

Each published price book has its own price basis (low bid, contract price, mean of all bids). The `price_basis` column carries the difference. Mixing bases silently is the main way a price reference goes wrong.

### 2.3 Council agenda systems

A city award goes through the council or a committee. The bid tab is usually attached to the award item. Most municipal data came this way.

What they give: the attachments of award items. These are line-item tabs, totals summaries (bid recaps), recommendation letters and whole packets. An engineer's estimate is printed on some tabs: on 15 of the 31 tabs of the overnight harvest and on 19 of the 47 of city round 1.

How they are fetched: Legistar and Collin County have scripts in `src/`. The other platforms were swept with one script per platform and per round. Those of city rounds 1 to 6 are kept in the working copy (section 11.1). Those of the overnight harvest and of the statewide harvest were written in a temporary folder; the records do not say that they were kept.

| Platform | Agencies and files | Public interface used |
|---|---|---|
| Legistar | 11 DFW cities, 421 files in the first batch: Mesquite 96, Carrollton 66, Farmers Branch 44, Coppell 38, Lewisville 38, McKinney 37, Keller 32, North Richland Hills 32, Mansfield 31, Plano 5, Grand Prairie 2 | `https://webapi.legistar.com/v1/<client>/matters`, `/matters/<id>/attachments`, `/events/<id>/eventitems?Attachments=1`; pages at `https://<client>.legistar.com/Legislation.aspx` |
| CivicClerk | about 30 small cities; the largest source of new data from 2026-11-20 | portal `https://<tenant>.portal.civicclerk.com/`; search `https://<tenant>.api.civicclerk.com/v1/Search?search=…` |
| OnBase Agenda Online | Frisco: 148 fetched on 11-19, 136 in the active folder | https://agenda.friscotexas.gov/OnBaseAgendaOnline/ |
| Destiny AgendaQuick | DeSoto (13 files in the active folder), Cedar Hill, Lancaster, Little Elm; Collin County's eAgenda is the same kind | `https://public.destinyhosted.com/agenda_publish.cfm?id=<id>` |
| NovusAGENDA | Saginaw, about 7 files (2019–2023) | https://saginawtx.novusagenda.com/agendapublic/ |
| Granicus | Denton County commissioners court (1 line-item tab), Granbury (1) | https://dentoncounty.granicus.com/ViewPublisher.php?view_id=26 |
| CivicWeb | Murphy | https://murphytx.civicweb.net/Portal/ |
| Municode Meetings | Grand Prairie (committee packets) | meeting list linked from https://www.gptx.org/Government/Mayor-and-City-Council/City-Council-Meetings |
| Packets and agenda files on the city's own site | Richardson (82 fetched, 80 in the active folder); Rockwall (3 tabs from 89 packets read with HTTP range requests); Southlake (8: Agenda Center file ids enumerated, plus one Internet Archive copy) | city web sites |

**Legistar**

- Fetched by `src/fetch_legistar_bidtabs.py`. It pulls every council matter introduced on or after `--since` (default 2020-01-01), keeps titles that look like a construction award, and downloads attachments whose name looks like a bid tab, recap or summary. It writes one manifest per client (`_manifest_<client>.csv`) with the matter title, the attachment name and the URL.
- The script's default client list has 8 names: `cityofdallas`, `mckinney`, `plano`, `mesquite`, `coppell`, `farmersbranch`, `mansfield`, `grandprairie`. The clients `carrolltontx`, `cityoflewisville`, `cityofkeller` and `nrhtx` must be passed with `--clients`. The script runs 8 worker threads by default and has no delay between requests; the collecting rules of section 6 still apply.
- A Legistar client name is not always the city name. More than 150 name variants were tried at first and about 250 more later. Only 11 DFW cities gave tabs. Dallas is on Legistar but its attachments hold no bid tab. Denton (`denton-tx`) and University Park (`uptexas`) were found later and hold no usable tab.
- The file name encodes the date the matter was introduced. That is a meeting-side date, not the bid date.
- The matter title often names the winner and the amount. That is an independent figure to check parsed totals against (`data/clean/award_reconciliation.csv`).
- Mansfield is on Legistar until 2025-02 and on CivicClerk from 2025-03. Grand Prairie's Legistar has no data after early 2021.
- Earlier sweeps missed files whose titles lacked the search words ("Stream … Bank Restoration", "Pond Restoration"). A later sweep used `eventitems?Attachments=1`, which returns all items and attachments of one meeting in one call: 13 clients, 2,588 council meetings since 2020-01, about 2,600 calls, about 165 attachments, 9 new tabs. It also looked for award items where the corpus held only a totals summary while a line-item table hung under the same item.
- Many Legistar attachments are not line-item tabs: bidders lists, bid recaps, best-value score sheets, or the winner's own proposal.
- The best find of the first batch was Farmers Branch's creek erosion repair, let by reach in 2021 and again in 2025.

**CivicClerk**

- The public full-text search covers attachment text. That matters because many consultants' recommendation letters carry the full line-item table while the attachment is named only "Letter". Sweeps that filtered by attachment name had missed them.
- **Paging bug.** The `@odata.nextLink` the search returns always leads to an empty page. The first sweep therefore saw only the newest 15 meetings per search term. Paging with `&$skip=15,30,…` works. The `-word` (NOT) operator and `$filter` do not work.
- A packet's text can be read without downloading the PDF: `GetMeetingFileStream(fileId=…,plainText=true)`.
- Attachments sit on a shared blob server. Download links are signed and expire after about a day. Manifests record the path without the signature. Point people to the meeting page, not to the file link.
- Coverage so far: about 45 towns in the first sweep (10 with finds); about 50 tenants paged to the end in round 2; 195 tenant names tried, with 9 new tenants inside the 14 counties.
- Archive depth differs by tenant. Watauga starts in 2018, Azle in 2022-09, Mansfield in 2025-03, Highland Park in 2025-05. Celina's attachment index reaches only 2026; its older tabs sit inside whole packets of 600 to 1,700 pages. Anna's packets start in 2021-07; its 2021 packets are on a public Laserfiche site.
- **Tabs inside whole packets** (rounds 4–6). Many tabs are never posted as attachments; they are pages of the agenda packet. The method: score packet search results by their summary text, download the best, count amounts, units and lines with four or more amounts on each page, cluster such pages into candidate segments, and check each segment by eye against the corpus. Scoring words: plus for tab wording, units, amounts, several bidders' prices side by side, drainage and street words; minus for supply, services, PID and design contracts.

**Other platforms**

- Frisco's OnBase: the bid number on the award item matches the city's solicitation number.
- Destiny: in DeSoto all 33 construction award items of 2020–2026 were opened and 36 attachments downloaded; 10 were line-item tabs. The attachment is called "Bid Tabulation Report – (project name)".
- NovusAGENDA: Saginaw moved to a JavaScript-only agenda system after mid-2023. Nothing was fetched from it.
- Denton County's Granicus: all 323 meetings from 2020-01 to 2026-11 were listed; 21 construction award items; 10 "Court Docs - Award" files downloaded; 1 was a line-item tab.
- Grand Prairie: construction awards are reviewed first by the Council Development Committee. The full bid tab is in the committee's packet; the council item carries totals only.
- Haltom City (145 council packets, 2022-01 to 2026-11, read page by page) and Grapevine (84 agendas and 6 packets): award memos list totals only.
- Tarrant County runs CDBG lettings for small cities. Their consultant letters carry a line-item tab and an estimate; they turn up in the cities' agendas.

### 2.4 Dallas and Fort Worth

The two largest cities have no public line-item tabs. This is one root cause of thin city data. In the panel, Dallas has 0 lettings for 2020–26 and Fort Worth has 1.

- **Fort Worth.** 426 bid pages and 285 weekly bid reports were checked. 5,866 council communication pages with 9,189 attachments held 1 construction unit-price tab. Document links on a third-party platform open a JavaScript application that a script cannot read. From 2024 everything runs through a vendor portal that needs registration.
- **Dallas.** Legistar returned 21,688 matters; about 3,000 list attachments; none is a bid tab. The site search sits behind a firewall. Kept: 27 agenda information sheets (lettings opened 2020-04 to 2025-12) and one 106-row table of bidder totals. Line-item tabs exist only on the vendor portal.
- The two searches still brought 43 files (36 line-item) from small cities nearby and 12 line-item tabs from Southlake, Rockwall and Irving.

### 2.5 The statewide erosion and drainage harvest (outside DFW)

- **Purpose.** DFW erosion items are thin. The question was whether prices can be borrowed from the rest of Texas. An inventory of 2026-11-19 counted 389 lettings in Texas whose main work is bank protection: 94 in DFW (34 city or county, 60 TxDOT) and 295 elsewhere. Only 3 of the city lettings print an engineer's estimate.
- **Result (2026-11-20).** 172 files: 159 line-item tabs and 13 totals-only files that print an engineer's estimate. Of the 159, 68 are streambank, erosion or channel work, 88 are drainage, culvert or detention, and 3 are other. 65 print an estimate. 14 are scans. There are 35 agencies in 10 TxDOT districts (Houston 71, Pharr 30, Austin 27, Waco 10, San Antonio 7, Corpus Christi 6, El Paso 5, Beaumont 1, Bryan 1, Odessa 1).
- **How.** The same agenda platforms, plus county commissioners courts and drainage districts. One flood-control district's bids are on a vendor portal, but its line-item tabs are in the county court's public Legistar attachments.
- **Blocked.** Austin's asset site showed a bot check after about 40 requests. The harvest stopped there; later files came from the Internet Archive, and the newest 5 from the city site at 10 seconds apart. The San Antonio River Authority answered 403 on every page.
- **Usability test.** In a private parse, 121 files gave rows and 94 lettings entered the price pool (27,745 rows). 13% of rows had no unit. The 14 scans gave 0 rows. There is no stable single regional factor: the statewide / DFW median is 1.04 with quartiles 0.70 to 1.79. Priced as unseen lettings, the estimator's median error on 14 lettings with an estimate was 36.5%, against 17.2% for the engineers (erosion class: 86.9% against 12.8%).
- **Decision.** A borrowing scheme passed its registered rule, but the gain was judged to be within noise. It was not installed. The batch stays in staging (`data/raw_downloads/_staging_statewide_2026-11-20/`, with `MANIFEST.csv`) and does not train.
- **If it is used later:** some files are IDIQ contracts with notional quantities (unit prices only); the Houston and Pharr districts hold more than 60% of the files and must not dominate a regional adjustment.

### 2.6 Cost indices and labour-wage data

The estimator escalates prices with its own matched-pair chained index, computed from the bid data, with one arm for municipal work and one for TxDOT. Outside indices are for comparison. The TxDOT arm follows the shape of TxDOT's Highway Cost Index (correlation 0.84) but not its level (142.2 against 154.9, 2016 = 100).

| Series | Source | Result | Use |
|---|---|---|---|
| TxDOT Highway Cost Index (HCI) | PDF report of August 2026, `https://www.txdot.gov/content/dam/docs/division/cst/202608-hci-report.pdf` | 384 rows, 2016-01 to 2026-08 | Computed from winning bids. Allowed for escalation. Comparison anchor. |
| FHWA National Highway Construction Cost Index (NHCCI) | `https://data.transportation.gov/resource/r94d-n4f9.csv` | 82 rows, 2016Q1 to 2026Q1 | Bid-based. Comparison anchor. |
| BLS Producer Price Index, 10 series | BLS public API | 1,300 rows | Background only. Input prices exclude competition and margin. Never used to price or to escalate. There is no PPI specific to highway or street construction. |
| QCEW weekly wage, NAICS 237310 (highway, street and bridge construction), by county | `https://data.bls.gov/cew/data/api/{year}/a/industry/{naics}.csv` (annual) and `/{year}/{1-4}/…` (quarterly) | annual 2015–2025, quarterly 2015Q1–2026Q1 | The labour series that is kept. A wage bill per worker, not an hourly rate. |
| CES hourly earnings, NAICS 2373, national | BLS public API | monthly | Kept as the hours-based comparison. No DFW-level construction series exists in CES. |

All index files are built by `src/fetch_cost_indices.py`; the wage files by `src/parse_bls_qcew.py`, `src/parse_bls_qcew_quarterly.py` and `src/parse_bls_oews.py`. None of these runs in the rebuild. The sources do not record a wage series as an estimator input.

**Rejected, and why**

- **DOL prevailing-wage disclosure data (H-1B / PERM).** Wrong statute: an immigration wage floor for mostly white-collar jobs. Of 282,095 records only 111 are DFW construction trades. It was filed as the wage source twice by mistake before it was excluded.
- **Davis-Bacon wage determinations.** Right statute, no time dimension. Rates are frozen at the survey date: the DFW "Heavy" determinations issued in 2026 still carry 1990/91 survey rates, with a common labourer at $7.25.
- **OEWS occupational wages.** The API serves only the latest period (May 2025). The historical files are behind bot protection that admits only clients that send a contact e-mail address in their request header. That is personal data and is not sent. The ten yearly files were fetched once through a browser on 2026-11-18, before the no-browser rule existed. OEWS does not feed the estimator.

**Traps and lessons**

- QCEW writes a suppressed cell as a literal zero with `disclosure_code == "N"`. The parser blanks it, flags it and logs it. The DFW aggregate is suppressed for 2024 and absent for 2025; it is rebuilt as an employment-weighted mean of the counties that publish, a method that lands within 0.7% of the published figure in the nine years where both exist.
- **Compare like with like.** DFW highway wages seemed to pull away from an all-construction index from 2022, by 4.9 points in 2025. Against the same industry (NAICS 2373) the gap vanished: DFW +41.4%, national +41.7%.
- **Check values against what is physically possible.** A $0 weekly wage passed a code check, and a set of hours with almost no seasonal swing passed a series check. Both were caught only by asking whether the number can exist.
- **"Not fetched" is not "does not exist".** The BLS API allows 25 queries a day. Four series whose quota had run out were once written down as "series does not exist". The records now keep "the source answered: no data" apart from "the request did not succeed".

---

## 3. How to find a bid tabulation

This is the manual flow. The harvest scripts use the same public interfaces, so a person clicking through finds the same files. A URL marked "(checked)" answered 200 to a script on 2026-11-21. Other URLs come from the project's records.

```
TxDOT:           open data portal or archive directory -> filter -> download the table or the zip
City or county:  city web site -> agenda system -> the meeting that awarded -> the award item -> attachment "Bid Tab"
```

### 3.1 TxDOT

**Open data portal** — https://data.texas.gov/dataset/Bid-Tabulations/de7b-7dna (checked)

1. Open the page and click **View Data**.
2. Click **Filter** and add conditions, for example `county` = Dallas or Tarrant, a range on `project_actual_let_date`, or `bid_item_description` contains GABION or RIPRAP.
3. Click **Export** and choose CSV.
4. In a spreadsheet, group by `project_id` to see one letting and by `vendor_name` to see each bidder's prices.

Old rows are deleted. Keep your own copy.

**Archive** — https://ftp.txdot.gov/pub/txdot-info/cmd/cserve/bidtab/ (checked)

1. Open the directory and find the month by file name. `sc202403.zip` holds the construction lettings of March 2024.
2. Download and unzip.
3. Open a `.TXT` file in a text editor. The COUNTY line in the heading gives the county. The engineer's estimate and each bidder's total come first, then the unit prices.

**Average-cost dashboard (comparison only)** — https://tableau.txdot.gov/views/BidItemAverageCost/BidItemAvgCostDashboard (checked)

1. Choose the county.
2. Set Bidder Rank to Low Bidder.
3. Set the let date to Last 12 months.
4. Click **Download → Crosstab → Excel**. Do not choose CSV.

### 3.2 Cities and counties

**How to recognise the right file**

- Common attachment names: Bid Tab, Bid Tabulation, Tabulation of Bids, Bid Proposal Evaluation.
- Small cities often attach the full table inside their consultant's recommendation letter. The attachment may be named only "Letter".
- Inside, each bidder has a group of columns: Item / Description / Qty / Unit / Unit Price / Total.
- A file with one total per bidder (Bid Summary, Bid Recap) is not a line-item table.

**Legistar (11 cities)**

- Examples: Lewisville https://cityoflewisville.legistar.com/Legislation.aspx (checked); Mesquite https://mesquite.legistar.com/Legislation.aspx (checked); Farmers Branch https://farmersbranch.legistar.com/Legislation.aspx (checked). Also Carrollton, Coppell, McKinney, Keller, North Richland Hills, Mansfield (before 2025-02) and Plano, at `https://<client>.legistar.com`. Keller is `cityofkeller`; North Richland Hills is `nrhtx`.
- Steps:
  1. Open the Legislation page.
  2. Type the project name, the bid number or "bid tab" in the search box.
  3. Tick **Attachments**, so the text of attachments is searched too.
  4. Choose the year under Years and click Search.
  5. Click the File # of a result. On the detail page, open "Bid Tab" in the **Attachments** row.
- The item title usually reads "Consideration of a Bid Award for … to (contractor) in the Amount of $…".

**CivicClerk (about 30 small cities)**

- Examples: Colleyville https://colleyvilletx.portal.civicclerk.com/ (checked); Highland Park https://highlandparktx.portal.civicclerk.com/ (checked); Mansfield from 2025-03 https://mansfieldtx.portal.civicclerk.com/ (checked); Aubrey https://aubreytx.portal.civicclerk.com/ (checked). Also Celina, Watauga, Azle, Kaufman, Trophy Club, Kennedale, Euless, Lucas, Justin, Rockwall, Richland Hills and others, mostly at `https://<city>tx.portal.civicclerk.com`.
- Steps:
  1. Open the portal. In the meeting list, find the City Council meeting that made the award, by date or with the search.
  2. Open the agenda and open the award item.
  3. Among the attachments, open "Bid Tab", "Bid Tabulation" or the consultant's recommendation letter.
  4. If no attachment has it, open the whole Agenda Packet and search for "Tabulation".

**OnBase Agenda Online (Frisco)** — https://agenda.friscotexas.gov/OnBaseAgendaOnline/ (checked)

1. Click **Meeting Search** at the top.
2. Choose the meeting type City Council (CDC and EDC also exist) and the date.
3. Open the agenda and find the award item. Its title carries a bid number such as "Bid No. 2612-022".
4. Under the item, open the attachment "… Bid Tab.pdf".

**Destiny AgendaQuick**

- Examples: DeSoto https://public.destinyhosted.com/agenda_publish.cfm?id=33768 (checked); Cedar Hill https://public.destinyhosted.com/agenda_publish.cfm?id=23327 (checked); Collin County commissioners court https://eagenda.collincountytx.gov/agenda_publish.cfm?id=&mt=ALL (checked). Also Lancaster (id=52622) and Little Elm (id=54885).
- Steps:
  1. Choose the meeting type and the year and month.
  2. Click the meeting, then click the item title in the agenda.
  3. The item page lists its attachments. Open "Bid Tabulation" or "Bid Tab".
- Collin County's attachments are mostly service and equipment purchases. Open the file to see whether it is construction.

**NovusAGENDA (Saginaw)** — https://saginawtx.novusagenda.com/agendapublic/ (checked)

1. Click Meeting Search.
2. Choose Custom Date Range and the meeting type City Council Regular Meeting.
3. Open the agenda, click the item and look at the attachments.

**City purchasing and bid-results pages**

- Arlington "Bidding & Procurement": https://www.arlingtontx.gov/Government/Budget-Finance/Purchasing/Bidding-Procurement (checked). The folders are by department. The project number shows the work type: PWDR drainage and erosion, PWST streets, PWSM street maintenance, WU water and wastewater.
- Rowlett "Public Openings, Tabulations": https://www.rowletttx.gov/1438/Public-Openings-Tabulations-Pre-Bid-Conf (checked).
- Mesquite purchasing site: `https://apps.cityofmesquite.com/purchasingweb/documents/awarded/<year-number>.pdf`.
- Steps: open the page, find the year or the project number, click "Bid Tab".

**Whole council or committee packets**

- Examples:
  - Grand Prairie council meetings: https://www.gptx.org/Government/Mayor-and-City-Council/City-Council-Meetings (checked). The page embeds a Municode Meetings list: https://meetings.municode.com/PublishPage/index?cid=GPTX&ppid=0a01ee0f-2750-46df-ae4f-bb7135af7019&p=1 . On 2026-11-21 this list answered a script with a verification page. It was not bypassed. An ordinary browser opens it.
  - Murphy (CivicWeb): https://murphytx.civicweb.net/Portal/ (checked). The bid tab is inside the item's Staff Report PDF.
  - Richardson: council agenda packets on www.cor.net.
- Steps:
  1. Find the meeting and download the whole Packet. Packets are often tens of MB and hundreds of pages.
  2. Search for the project name, the bid number, "Tabulation" or "Bid Opened".
  3. The bid tab is usually a few pages after the staff report or the consultant's letter.
- The Grand Prairie trick: the council item says, for example, "(Reviewed by the City Council Development Committee on 05/21/2026)". Go to the committee meeting of that date. Its packet holds the full tab.

**County purchasing pages and commissioners courts**

- Dallas County purchasing: https://www.dallascounty.org/departments/purchasing/currentbidtabs.php (checked). Each row of a yearly page is a bid number, a description and a link to the tab.
- Denton County commissioners court (Granicus): https://dentoncounty.granicus.com/ViewPublisher.php?view_id=26 (checked). Use Search Archives and tick "Attachment Items". Award files are called "Court Docs - Award".
- Collin County: see Destiny above.

### 3.3 Worked examples, from home page to PDF

**Example 1: Lewisville, Valley Vista & Poydras drainage (Legistar)**

1. Open https://cityoflewisville.legistar.com/Legislation.aspx .
2. Search "Valley Vista", tick Attachments, set Years to 2026, click Search.
3. Click File # **26-4641** (2026-06-18), a bid award item.
4. In the Attachments row, open **"02 Bid Tab - Valley Vista & Poydras Drainage Improvements"** (1 page).

File address in the project's records: https://cityoflewisville.legistar1.com/cityoflewisville/attachments/a81d5649-71f9-45e2-994f-c58eabb87d9a.pdf

**Example 2: Colleyville, Water Project 18 and Pecan Park phase 2 drainage (CivicClerk)**

1. Open https://colleyvilletx.portal.civicclerk.com/ .
2. Find the City Council meeting of **2026-03-10**.
3. Open the agenda and find the item that awards ENG-2026-001.
4. Open the attachment **"Bid Tabulation"**: 1 page, 16 bidders. The item text says the bids were opened on 2025-12-30.

In the project's records: CivicClerk meeting id 2667, attachment id 49224.

**Example 3: Grand Prairie, Briar Hill erosion, Bid 26020 (committee packet)**

1. Open https://www.gptx.org , click Government, then City Council Meetings.
2. In the embedded meeting list, find the **2026-05-21 City Council Development Committee**.
3. Click **Packet** (about 45 MB, 170 pages).
4. Search for "Briar Hill" or "26020". Pages 155–156 are the staff report. **Pages 157–164 are the bid tab**, with 8 bidders.

**Seven more public examples** (all from the project's records)

| Letting | Platform | Where it was found |
|---|---|---|
| Grand Prairie, Fish Creek west bank (Bid 25087) | packet | The stand-alone tab in the corpus was an attachment of a meeting item; its link was not recorded. The same table is in the Development Committee packet of 2025-05-20. |
| Grand Prairie, Kirby Creek (RFB 25115) | packet | Development Committee packet of 2025-09-03: memo on page 53, bid tab on page 55. |
| Highland Park, Hackberry Creek 1A/1B | CivicClerk | Council meeting of 2026-05-05, bid tab attachment. |
| Mansfield, Chapel Hill Dr drainage | CivicClerk packet | Council Agenda Packet of 2025-03-24, page 71. |
| Aubrey, detention pond and Sherman St culvert | CivicClerk | Council meeting of 2026-11-19, award recommendation package, pages 3–4. |
| Frisco, Meadow Hill Dr (Bid 2612-022) | OnBase | Council meeting of 2026-05-05, item 75174, attachment "3- Meadow Hill Drive Recon 19143 Bid Tab.pdf". |
| Murphy, Timbers area cast-iron water main replacement | CivicWeb | Council meeting of 2026-10-06, pages 15–16 of the item's Staff Report. |

### 3.4 Notes on the flow

- Portals that need a login were never touched (section 5).
- Blocked sites were not bypassed. Some sites answer scripts with 403 or with a Cloudflare or Akamai verification page.
- A few files came from the Internet Archive's public copy of a page. The original address is recorded.
- Files are saved as published, unchanged.
- The date in some file names is the meeting date (Legistar, Grand Prairie), not necessarily the bid date.

---

## 4. The harvest rounds

In the last column, "scored" means municipal lettings of 2024–26 that the estimator could score forward in time after the merge. "Pre-merge" is the median error of the estimate's expected low bid on the new lettings, priced as unseen, against the engineers' printed estimates.

| Round | Date | Sources and method | Files | What came of it |
|---|---|---|---|---|
| TxDOT | 2026-11-17 | Open data (DFW file, statewide erosion items), archive (303 zips), dashboard (8 crosstabs) | see 2.1 | The TxDOT side of the panel. |
| First cities and counties | 2026-11-17 to 11-18 | Legistar API 421; Arlington 61; Mesquite purchasing 144; Garland reports (2 editions); Rowlett 32; Dallas County 6; Denton 2; Collin County 540 | see 2.2, 2.3 | On 11-18 the municipal side had 391 projects. One download through a browser raised a save dialog for each file; the no-browser rule dates from that day. |
| Third round | 2026-11-19, early | Frisco (OnBase) 148; Richardson (packets) 82; Irving 23; Rowlett and others 20; Denton 15; Fort Worth 1; Flower Mound 1 | about 290 | All by curl. Lewisville's site blocked scripts. Three lettings on the private company list were met and not downloaded. Pre-2020 files were moved out afterwards. |
| Three light harvests | 2026-11-19, from 10:08 | Cities: 117 line-item tabs from 10 new cities (Cedar Hill 29, Midlothian 27, Colleyville 25, Bedford 10, Little Elm 9, Celina 8, Watauga 4, Lancaster 3, Richland Hills 1, Rockwall 1). Counties: 13 (Kaufman, Tarrant, Dallas, Parker). Water utilities and agencies: 13 (Tarrant County, Denton County, TRWD) | 117 + 13 + 13 | 9 + 5 + 5 are mainly erosion, drainage or culverts. 7 county files are scans. A gap found at 12:30 that day: the rebuild had no OCR step, and 7 scans had been dropped silently in two rebuilds. Water agencies mostly publish totals only. |
| Fort Worth and Dallas routes | 2026-11-19, evening | See 2.4 | 43 (36 line-item) + 12 line-item | Merged the same evening (11 of the 12). Neither city has public line-item tabs. First rebuild on the new machine: parsed rows 131,586 → 173,931; 310 scorable municipal lettings. |
| Erosion and drainage, overnight | 2026-11-20, early | Legistar rescan with creek, drainage, pond and bank words; CivicClerk public API (about 45 towns); Destiny (DeSoto 4, Cedar Hill 2); NovusAGENDA (Saginaw 1); Granicus (Granbury 1) | 31 line-item + 4 totals with an estimate | 9 streambank / erosion / channel, 17 drainage / culvert / detention, 5 road jobs with a large storm-sewer section. 15 print an estimate; 2 have one bidder. 12 new agencies. Merged at 01:50 with parser round 3. |
| Statewide | 2026-11-20, 03:3x | See 2.5 | 172 (159 line-item) | Stays in staging. Does not train. |
| City round 1 | 2026-11-20, 11:27–12:20 | CivicClerk full-text search; consultants' letters named "Letter" | 47 staged, 47 merged | 17 agencies; 7 erosion or drainage; 19 print an estimate. Pre-merge on 5 lettings: 33.8% against 20.1%. Scored: 144 → 169. |
| City round 2 | 2026-11-20, 13:21–14:00 | CivicClerk paged to the end with `$skip` (about 50 tenants) | 30 (+1 OPCC) staged, 30 merged | 10 agencies; 11 files of 2020–2023; 1 drainage. 0 duplicates against 2,808 documents. Pre-merge on 10 lettings: 22.2% against 15.9%. Scored: 169 → 183. |
| City round 3 | 2026-11-20, 18:19–19:25 | A gap table by agency and year first; Legistar `eventitems` sweep; Destiny (DeSoto); NovusAGENDA (Saginaw); Denton County Granicus | 25 staged, 23 merged | 0 erosion or drainage: the drainage awards seen were totals, single bidders, resolutions only, or already held. Pre-merge on 9 lettings: 58.6%; engineers on 12 estimates: 18.9%. The staging manifest was deleted at merge, so these 23 files lacked source URLs for a time. Scored: 190. |
| City round 4 | 2026-11-21, 00:29–01:16 | Tabs inside whole CivicClerk packets (14 of the 19); a CDBG search over 16 small-city tenants; Dallas County's yearly tab pages | 19 staged, 17 merged | About 1,800 packet search results scored, 41 packets downloaded. 424 requests, at least 1.1 s apart, no 429. Pre-merge on 6 lettings: 37.8%. Scored: 190. |
| City round 5 | 2026-11-21, 01:59–02:45 | 1,784 pooled packet results → 1,570 of 2021–2026 not yet held → 582 packets downloaded (about 35 GB) → 252 + 118 candidate segments | 45 staged, 35 merged | 13 agencies; 6 drainage. 10 held: 3 read zero rows, 2 read an estimate column as a bidder, 3 print a wrong unit price, and others. Annual and as-needed contracts not taken. 584 requests, all 200. Pre-merge on 26 lettings: 28.3%. Scored: 206. |
| City round 6 | 2026-11-21, 09:55–10:40 | The remaining 988 packets ranked; the top 300 taken (298 obtained, about 8.8 GB) | 10 staged, 5 merged | New tabs per 50 packets: 1, 0, 2, 1, 0, 1. Stop rule: fewer than 2 in the last 100. 688 packets remain unread. Found a de-duplication gap (section 8.6). Scored: 224. |
| Released by the parser | 2026-11-21 | Held tabs that parser rounds 10 and 11 could read | 5 + 1 | Scored after pricing rounds 18 and 20: 234, then 235. |

**What the pre-merge scores showed.** On new lettings the estimator's error was usually above the engineers'. Most of the gap came from pricing defects that can be fixed: a line that fell into the catch-all family (pavement milling at $4/SY priced at $82.50/SY), a mobilization lump sum that does not shrink with the job (a $280,000 job given $270,000 of mobilization), and per-each prices that run high. These defects became the targets of the next parser and pricing rounds.

**"Cannot be found" is often "cannot be found this way".** After the first batch, Frisco, Richardson, Irving, Flower Mound and DeSoto were listed as unreachable, because their bid portals need a login. Their tabs later came from council packets and agenda systems. CivicClerk and NovusAGENDA had been written off as false hits of a wildcard DNS; the CivicClerk public interface then became the main new source.

---

## 5. What is deliberately not collected or not used, and why

### 5.1 The gates a document passes

1. **Classification** (`src/audit_external.py`, `src/index_external.py`). Usable: `line_item`, `likely_line_item`, `unit_price_table`, `unit_price_dataset`. Not usable: `summary_only`, and `unreadable` until it has been read by OCR.
2. **Time window.** 2021 and later for all work; 2020 and later for erosion and drainage (section 8.7). A file with no established date is excluded.
3. **Kind of contract.** Construction, material supply or service, judged by content. Supply and service contracts stay out of the construction reference.
4. **Competition.** Only lettings with 3 or more bidders build reference prices.
5. **Can it be scored as a project?** The lowest bidder must be identified, and the parsed total must match the printed total (or the parse must cover 80% or more of the document's items). Otherwise the letting is not scored, but its unit prices stay in the price pool.
6. **Arithmetic.** Quantity × unit price = extended amount.
7. **Company-copy scan.** Before the rebuild, after OCR and after the parse.
8. **Pre-merge scoring.** The new letting is priced as unseen and compared with its printed estimate.

### 5.2 Not used

| Not used | Why |
|---|---|
| Company files and their public copies | Test data only. Copies are quarantined (section 9). |
| Bid tabs from before the time window (2020 and earlier; 2019 and earlier for erosion and drainage) | The pre-pandemic cost base is not comparable. They are moved to `_pre2020/`, not deleted, and can be restored. |
| Totals-only files (708) | Scopes are not comparable. |
| Annual, as-needed and work-order contracts, for scoring | Their "total" is an evaluation formula over notional quantities, not the price of a project. Their unit prices stay in the pool. |
| Material supply and service contracts | Not construction prices. For example, 623 rows of "stone delivered to the yard" once sat in the stone riprap price. |
| The engineer's estimate column of a tab | A yardstick only. Never a bidder and never a feature. |
| BLS PPI and other survey indices | Not transaction prices. |
| Plan sheets | Too few can be downloaded without a login. In a pilot of 55 DFW municipal lettings with plans, 8 full sets (14.5%) could be fetched; about 40% sit behind registration. No image recognition is done; text only. |
| The statewide erosion and drainage batch | The borrowing scheme was not installed. |
| Single-bidder tabs | No competition. Reference unit prices at most. |

### 5.3 Not collected

| Not collected | Why |
|---|---|
| Tabs behind vendor portals (Bonfire, IonWave, CivCast, Public Purchase, BidNet, PeopleSoft) | They need a registered vendor account. Registration and login are the project owner's decision. |
| Line-item tabs that exist only on request | A public information request is filed by the project owner in person, not by the project. |
| Pages behind a search form that needs a POST | Submitting forms is not done. |
| Sites that block scripts | Recorded as blocked, not bypassed: 403 pages, Akamai, Cloudflare and Imperva checks, 429 responses, JavaScript-only agenda pages. The Internet Archive later refused requests as well. |
| Buildings, treatment plants, pump stations with tanks, water towers, fibre, signs, pure landscaping, supply and service bids | Passed over in the packet rounds. |

---

## 6. Rules for collecting

1. **Public pages only.** No login, no account, no form submission, no CAPTCHA. No personal data is sent to any outside service.
2. **Scripts only.** Use curl or Python. Never download through a browser's save dialog and never open an office application: collection must run unattended.
3. **Throttle.** At least 1 to 1.2 seconds between requests to one host, and a generic client name in the request header (`bid-tab-research/1.0`). The habit is about 40 requests per site in a round. Sweeps of public APIs went far beyond that count (about 2,600 Legistar API calls in round 3) and kept the spacing. No rate limiting was seen.
4. **Stop when blocked.** On a 403 or a verification page, stop and record the site as blocked. Do not disguise the script as a browser and do not work around the block.
5. **The Internet Archive is unsettled.** About 14 files came from the Internet Archive after the live site blocked scripts (Euless 1, Southlake 1, about 12 Austin reference tabs). The reports state it openly. The rule does not say whether this counts as bypassing, and the project owner has not ruled. Ask before relying on it.
6. **Outward acts belong to the project owner.** Vendor registration, logins and public information requests are never done by the project on its own.
7. **Every file keeps its source page.** Each landed file gets a manifest row: source URL (several URLs when attachments were combined), md5, bid date and the basis of that date, bidders, whether an estimate is printed, whether it is a scan. For signed links, record the stable path or the meeting page.
8. **"Not fetched" is not "does not exist".** Record "the source has none" and "the request did not succeed" as different facts. List what was not swept.
9. **Where files go.**
   - Bid tabs: `data/external/<agency>/` (Legistar: `data/external/legistar/<client>/`).
   - Agenda packets, totals summaries and anything that is not a bid tab: `data/raw_downloads/<city>/`. The parser does not read there.
   - Never the top level of `data/`. The parser treats a top-level PDF or XLSX as a company file.
   - Staging belongs under `data/raw_downloads/_staging_<round>/`, not under `data/external/`. The parser walks every subfolder of `data/external/` except a short skip list, so a staging folder there would be parsed as an agency.
10. **The fixed routine for every batch.**
    1. Land the files in staging. Do not touch live data.
    2. De-duplicate: md5, then agency + bid number, then agency + title + date. When an existing file has the same bid number, check whether it is only a totals summary (section 8.6).
    3. Scan for public copies of company lettings, against the private company list. A hit goes to quarantine and never to staging.
    4. Parse the batch in a private copy and price each new letting as unseen. Compare with the engineer's estimate printed on the tab.
    5. Merge: copy to the agency folder, verify the md5, append a row to the merge log (`data/external/_merge_2026-11-20.csv`: merged_at, owner, file, md5, work_type, bid_date, source), keep the manifest.
    6. Rebuild everything, pass both guards, and re-score by year.
11. **One writer at a time.** The audit, the index and the quarantine write shared files. Never run them beside a parse or a harvest (`src/pipeline_lock.py` enforces a single writer).

---

## 7. The cleaning pipeline

```
public bid tabs (PDF / XLSX / scans)
   |  parse: text layer, coordinate reader, OCR (best of four engines), arithmetic checks
   v
panel: one row per bidder x line item, with item family, unit, size, quantity band
   v
reference prices (two-stage medians), price index by market, competition discount by bidder count
   v
estimator -> forward calibration -> P50 and P80
```

P50 is the expected low bid. P80 is a not-under estimate: the actual low bid should come in below it on at least 80% of lettings.

### 7.1 The steps of `src/rebuild_all.sh`, in order

Run `bash src/rebuild_all.sh`. The run stops at the first step that fails. A leak found by a guard or by the company-copy scan stops it too. The log goes to `reports/measurements/rebuild_<timestamp>.log`. `--from <script-name>` skips every step before that script. `BIDTAB_WORKERS` sets the number of parser processes (default 8). The script stops at once if `pdftotext` is missing: without it some steps fall back to other extractors with no error, and classifications drift.

1. **`company_twin_scan.py`** (private, not in the public repository). Reads the raw files where the parser reads, and the private pattern list. Exits with an error when a public copy of a company letting is found. It reads full text, about 5 minutes a pass. Risk: a file whose text cannot be read is reported as not checked. Five such scans remained, all in the pre-2020 folder.
2. **`audit_external.py`**. Reads every file under `data/external/`. For a PDF it tries three extractors in order: `pdftotext -layout`, pdfplumber, then PyMuPDF re-laid out from word boxes. An OCR sidecar is preferred when one exists. Writes `_audit.csv` with one classification per file. Risks: an empty extraction looks like an empty document (14 files read empty in the first two extractors and perfectly in the third); `summary_only` is a heuristic and is sometimes wrong; two audits writing at once made 54 Mesquite and 17 Rowlett files vanish from the results with no error.
3. **`ocr_scans.py`**. Reads `_audit.csv` and surveys every PDF page by page. Reads image-only, overprinted and vector-glyph pages by OCR (section 8.1). Writes one cached text sidecar per PDF under `data/external/_ocr/`, plus `_ocr_engine_choice.csv` and `_ocr_page_targets.csv`. The original files stay byte-identical. Risk: OCR digits are approximate; nothing downstream trusts a cell that its own row does not prove.
4. **`audit_external.py` again**, so scans classify from their OCR text, and **`company_twin_scan.py` again**, so the scan reads the sidecars. Before this order existed, a scanned public copy of a company tab was checked on its file name alone.
5. **`index_external.py`**. Reads `_audit.csv` and the documents. Writes `INDEX.csv` and `INDEX.md`: agency, project title, bid date and where it came from, bidders, work type, classification, and whether the file is in the time window (`in_scope`, `reference`, `unknown_date`, `pre_2020`). Risk: the column `scope_2020plus` and its value `pre_2020` are historical names; the live cutoff is in `window_from_year`.
6. **`quarantine_pre2020.py`**. Moves out-of-window files to `data/external/_pre2020/`, keeping the subfolder, and logs each move in `_pre2020/_moved.csv`. `--dry-run` shows what would move and `--restore` puts everything back. Risks: it moves files, so it must not run while a parser reads them; the folder must not be renamed, because the parser excludes `_pre2020/` by name.
7. **`parse_bidtabs.py`**. Reads the PDFs and workbooks (with `parse_coords.py` for the coordinate reader and `parse_ocr_sidecars.py` for scans). Writes `data/clean/line_items.csv`: one row per bidder × line item, with the engineer's estimate kept as a flagged pseudo-bidder. Every exclusion is counted and reported. The parse cache is keyed on `PARSER_VERSION`, so bumping the version re-parses every file. Risks: the whole of section 8. On the old machine a pool that recycled workers once hung for an hour with no error; recycling is off by default.
8. **`company_twin_scan.py --rows`**. Compares parsed rows. A public letting that repeats a company file's bid rows (item, quantity, unit price) is a copy, whatever its title says.
9. **`qc_report.py`**. Reads `line_items.csv`. Writes `reports/qc_report.md`, `project_meta.csv` and `item_family_stats.csv`: which parts of the data can carry an estimate and which cannot.
10. **`load_txdot.py`**. Reads the DFW open-data CSV. Writes `txdot_line_items.csv`, `txdot_project_totals.csv` and a measured competition discount. Flags the engineer's estimate rows. Risks: the "engineer" substring bug, re-lets and A+B items (section 2.1).
11. **`parse_txdot_archive.py`**. Reads the 303 zips. Writes `txdot_archive_line_items.csv`, `txdot_archive_project_totals.csv` and the code crosswalk. Risks: wrapped bidders, code lengths, code generations, encoding (section 2.1).
12. **`parse_garland_avg.py`** and **`parse_dwu_manual.py`**. Read the two published price books. The Garland detail lines are used, not its printed averages: the averages are quantity-weighted, and one printed total is rounded.
13. **`load_erosion_statewide.py`**. Reads the statewide TxDOT erosion items. Measures the DFW / non-DFW price ratio per item and writes an erosion price reference. It cannot fill two gaps: TxDOT has no turf reinforcement mat item (its "soil retention blanket" is a different, thin product at about $2/SY) and no channel grade-control beam.
14. **`build_project_metadata.py`**. Writes `project_metadata.csv` (title, agency, work type) and `award_reconciliation.csv` (parsed low bid against the award amount in the council item title).
15. **`build_vocabulary.py`**. Writes `item_vocabulary.csv`, the list of every distinct line item with its normalised keys.
16. **`award_identity.py`**. Decides, per municipal letting, which bidder is lowest and on what evidence. Writes `municipal_award.csv`. It runs before and after the panel build.
17. **`build_master.py`**. Combines every cleaned source into `master_panel.csv`. Applies the duplicate rule again, the bidder-name canonicalisation, the manual bid dates (`data/external/_sourced_bid_dates.csv`), the competition class and the project kind. Builds `price_reference.csv` by two-stage medians: across bidders within a project first, then across projects. Writes `reports/master_summary.md`.
18. **`competition_and_crosswalk.py`**. Fits the competition discount (expected low bid / median bid, by number of bidders) and the 6xxx → 7xxx code crosswalk.
19. **`index_by_market.py`**. Computes the matched-pair price index separately for TxDOT and for municipal work. Only precise bid dates are used: an agenda date lands weeks after the opening, and a bid number gives a fiscal year.
20. **`estimate_low_bid.py`**. Runs the forward calibration of the estimator and writes `estimator_calibration.csv`.
21. **`estimate_accuracy_benchmark.py`** and **`opcc_analysis.py`**. Benchmarks only. The first measures how far TxDOT's sealed engineer estimates are from the low bid. An estimate never becomes a feature or a target.
22. **`provenance_check.py`** and **`train_test_guard.py`**. The two guards (section 9). A non-zero exit means a real leak.

A full rebuild takes 10 to 25 minutes on the project machine (10 minutes on 2026-11-19; 24 minutes on 2026-11-20, with two company-copy passes of about 5 minutes each).

### 7.2 Tools outside the rebuild

| Script | What it does |
|---|---|
| `fetch_txdot_bidtabs.py`, `fetch_legistar_bidtabs.py`, `fetch_collin_bidtabs.py`, `fetch_cost_indices.py` | Collection (section 2). |
| `ocr_verify.py` | Scores each OCR'd tab by its own arithmetic and writes `_ocr_trust.csv`. Report only. |
| `check_interleaved_text.py`, `pick_extractor.py` | Find PDFs whose text comes out with two columns woven together, and choose the extractor per file. |
| `find_text_alternatives.py` | For a scanned Legistar tab, looks for a text-layer sibling under the same council matter. |
| `duplicate_census.py` | Lists byte-identical files (`_duplicates.csv`). Report only. |
| `multi_letting.py` | Flags PDFs that hold more than one solicitation (`_multi_letting.csv`). |
| `audit_arithmetic.py` | Writes `reports/arithmetic_audit.md`. |
| `export_manifest.py`, `export_header_lines.py` | List which files that should have produced rows produced none, and what their header line looks like. |
| `item_specs.py` | Reads dimensions with their meaning from descriptions (wall height, riprap thickness, box span × rise). Report only; the estimator does not import it. |
| `parse_txdot_bidtabs.py`, `parse_bls_qcew.py`, `parse_bls_oews.py` | Dashboard exports and wage data (sections 2.1, 2.6). |

### 7.3 Parser versions

Each parser change bumps `PARSER_VERSION`. Figures are forward-in-time scores on municipal lettings of 2024–26 unless noted. "Overshoot" is the median amount by which P80 exceeds the low bid; "P50" is the median error of the expected low bid. A before/after pair holds only inside its own comparison: between versions new tabs were merged and the estimator changed.

| Version | Date | What changed | Effect |
|---|---|---|---|
| .18 | 11-19 | 7 misread tabs; 6 rules that crafted input could fool; consultant names and "P.E." added to estimate detection; 19 tables whose "bidder name is an amount" dropped whole | Panel 707k → 760k rows |
| .19 | 11-19 | 15 generic rules for files that read zero rows | 43 zero-row files gave about 10,000 rows; an independent check found 10 files worse |
| .20 | 11-19 | 11 fixes from that check, each reproduced by removing it alone | Columns matching a printed total 2,571 → 2,584; arithmetic failures 3,335 → 3,036 |
| .21 | 11-19 | OCR sidecar rows wired in | 181,868 rows, 1,775 from OCR |
| .22 | 11-19 | 14,032 quantities recovered from "total ÷ unit price, all bidders agree"; completeness gate; locked test-set label | Rows with a quantity 165,244 → 179,278 |
| .23 | 11-20 | Round 3: 49 rules for layouts, packets, names and dates | 1,507 files replayed: arithmetic failures 3,036 → 1,443; winner total within 1% of the printed total 76.4% → 82.8% |
| .24b | 11-20 | Round 3b: family by main noun; "remove and replace X" priced as X; material supply stays unclassified | Overshoot 69.0% → 59.5%; P50 28.8% → 26.4% |
| .24c | 11-20 | Milling and thin surface treatments split from asphalt paving | Overshoot 59.5% → 56.5% |
| .25 | 11-20 | TxDOT A+B lettings, re-lets, alternates, dropped rows, stricter completeness gate | Installed together with an estimator change; not separable |
| .26 | 11-20 | Item numbers glued into descriptions; sizes above 144 inches rejected; box culverts sized by opening area; quantities stated in lump-sum descriptions; 7 new families | 144 lettings: overshoot 51.3% → 48.6%; P50 26.0% → 24.5% |
| .27 | 11-20 | Erosion and drainage families: desilting, irrigation, railing; trenchless crossings as casing | 37 erosion/drainage lettings: overshoot 58.2% → 51.4%; P50 26.6% → 23.2%. All municipal: overshoot 49.1% → 51.4% |
| .28 | 11-20 | Linear materials counted per each lose the unit; 5,734 code-only descriptions recovered; LF sidewalk sized by width | 184 lettings: overshoot 46.8% → 44.7%; P50 23.8% → 22.9% |
| .29 | 11-20 | Round 8: families fixed in order of dollars mispriced; 9 new families | 186 lettings: P50 22.9% → 19.1%; overshoot 44.7% → 43.1% |
| .30 | 11-20 | Round 9: date rules; annual and as-needed contracts; apparel; a thickness on an EA or LS line is not a size | 188 lettings: overshoot 42.6% → 41.6%; P50 19.6% → 18.8% |
| .31 | 11-21 | Round 10: more table layouts, the engineer's column, name cleaning | Failed the literal rule (overshoot +0.25 points), all from two lettings whose truth was corrected; installed by precedent |
| .32 | 11-21 | Round 11: units recovered on 8,470 rows ($311M); culvert families | Passes on the lettings whose truth did not change |
| .33 | 11-21 | Round 12: 637 rows change family (trail earthwork, abandoned and plugged pipe) | 224 lettings: overshoot 46.26% → 45.78%; nothing else moved |
| .34b | 11-21 | Three item types left unclassified: traffic signals, pavement marking named by its line, signs named by code or panel size | 234 lettings scored by both builds: overshoot 45.3% → 44.6%; P50 19.7% → 19.4% |

**The adoption rule for a parser change.** Municipal P50 error and overshoot must not get worse. Each year's under rate (P80 below the low bid) must stay at or below max(20%, baseline + 3 points). TxDOT's main figures must not worsen by more than 0.5 point. **Precedent:** a change that corrects the truth (a low bid that was read wrong, a letting that becomes scorable) is judged on the lettings whose truth did not change.

---

## 8. Cleaning topics

### 8.1 Text layers, scans and OCR

**A text layer can lie.**

- Hidden text: glyphs clipped away or covered by an opaque white box are in the layer but not on the page. The parser reads only visible text.
- Interleaved headers: a two-line caption "Unit / Price" can come out as "P U ri n c i e t". In 35 files, two text columns are woven together letter by letter in `pdftotext`. PyMuPDF's word-box rebuild reads them, but it welds neighbouring words instead. The choice per file is made by a segmentation test: a welded token splits into real words, an interleaved one does not. The word list is built from the corpus, from files not under suspicion.
- Overprinted layers: one tab summed to $349.7 million where the page prints $337,782.
- A city's own poor OCR layer: one tab read as 755 garbage rows with bid totals from $700 million to $2.9 trillion. It was moved to `data/raw_downloads/unparseable/`.

**Which pages go to OCR.** The choice is per page, from the PDF itself:

- an image page: under 30 characters of text and an image over 30% of the page;
- an overprinted page: over 15% of its glyphs drawn on the spot of a different glyph;
- a vector-glyph page: under 200 characters, no scan image over 30% of the page, and 200 or more filled curve paths. Its words are drawn as outlines ("Microsoft: Print To PDF" of a spreadsheet, CAD exports). One such tab had 1,346 curve paths and a text layer of 69 characters, its digital signature;
- every other page keeps its text layer.

In a packet, only target pages that look like a bid tab are transcribed (six or more money amounts, tab wording, no invoice). A sideways scan is turned 90 or 270 degrees when that reads far more amounts. Horizontal table rules are detected on the image and written into the sidecar, so wrapped description lines can be assigned to the right row.

**Several engines, and the arithmetic as referee.** Every available engine reads the page. On the project machine that is four readings: RapidOCR, Tesseract 5 in two page-segmentation modes, and Apple Vision (which does not run inside a sandboxed shell and is then skipped). On Windows the system OCR engine reads too. The sidecar keeps the reading that the sidecar parser can use most: cells that pass quantity × unit price = total and sit unambiguously under a named bidder. A reading cannot gain such cells by being damaged. No engine wins every file. On 18 scans (balanced / checkable rows): Windows 169/170, RapidOCR 207/224, Tesseract 161/166 and 152/157. Ranking by usable cells instead of balanced rows changed the engine for 11 of 40 files.

**What an OCR cell must prove.**

- A price is emitted only when its own row balances within 0.5%.
- Known confusions are undone before the test ("oo" for "00", "s" for "$", a space after the thousands, "," read as "." and the reverse). That is safe because the test comes after: no repair makes a wrong reading balance.
- OCR loses empty cells, and a lost cell shifts every later price one column left. So each row's cells are aligned to the page's bidder columns, and a cell is kept only when every best alignment gives it the same column.
- Bidder names come only from the tab's header. A column with no name over it is dropped. Names are never made up.
- Rows are marked `text_source = ocr`, with the engine and the kind of check (`qty_x_price`, or the weaker `unit_equals_total` for quantity 1).

**How far OCR rows can be trusted.** Digits are reliable: 735 OCR price cells checked against the images, 0 wrong. Descriptions are weak: in a 15-row sample of one tab, about 11 rows carried the last text line of the item above. 19% of OCR rows have no unit. A photographed handwritten sheet stays unread.

**Limits.** Small type on sideways 200 dpi black-and-white scans read 0 usable cells in all four engines. Descriptions folded over 2 to 5 lines, with the prices on the last line, read 0 cells. A scan that carries its scanner's own messy text layer can be skipped by the OCR targeting.

**"No text" is not always a scan.** Of 6 such files in one check, 1 needed OCR (vector glyphs), 4 were one-page totals summaries with fewer than 200 characters, and 1 was a spreadsheet saved as `.pdf`.

### 8.2 Table layouts and the coordinate reader

- **Two reading strategies.** The table strategy uses the PDF's detected tables. The coordinate strategy (`src/parse_coords.py`) takes the header line's x positions as column anchors and assigns every word to a column by its x. It exists because table detection merged several data rows into one cell on a regular template: quantities came out as the digits of four rows glued together, and about 12% of rows failed the arithmetic. Joining a column's words also repairs "$ 6 0,000.00".
- **Column detection.** Columns are detected, not hard-coded. A price column is an adjacent header pair: a unit-price caption followed by a total caption. That separates the price "Unit" from the unit-of-measure "Unit", which is never followed by a total.
- **Choosing between the strategies.** An unreadable extended cell always counts against a reading. If the two readings differ in bidder count by a factor of 1.5 or more, coverage overrides the arithmetic score. If they read disjoint pages, they are merged. On a tie, the reading whose bidder totals appear among the document's printed amounts wins.
- **Why these rules exist.** The first quality gate rewarded damage: a row whose total could not be read left the denominator, so a parse that had lost 80% of the prices scored 1.00 and beat the correct one. A bug then made bidders 2..N vanish while the surviving column balanced perfectly.
- **Layout rules** are written by layout. No rule names a file, an agency or a bid number. Examples:
  - a header spread over two or three lines, or price captions stacked ("UNIT" over "COST");
  - a continuation page inherits the columns only when the page size is the same and the page has item rows (otherwise a tab's columns and names were carried onto checklists and score sheets later in a packet);
  - bidder names above the table, below the header, or over each bidder's own block;
  - titles, agenda stamps ("Item 12."), form furniture, rank captions ("2nd LOWEST"), award remarks, addresses and phone lines are not names;
  - a single bidder name is lent only to a table on the same or the next page;
  - a wrapped description line goes to its own item, using the widest vertical gap as the cell boundary;
  - an item number printed on the line above or below its prices;
  - a turned page's few upright words are stamps, not table content;
  - a quantity glued to the number printed after it is restored only when two or more bidders' total ÷ unit price point to the same quantity;
  - a table with no header: the quantity column is the one that satisfies quantity × unit price = total (the old rule took the item-number column: 2,381 rows, 87.9% of all arithmetic failures);
  - two quantity columns: take the one that reproduces the totals;
  - per-bidder Unit / Qty / Price / Total blocks under wrong captions: proved by the arithmetic on 80% or more of the rows.
- **Zero-row files.** On 2026-11-19, 673 of 1,297 public files gave no row. 420 were rightly empty (totals summaries, blank bid forms, memos). 215 were real parsing problems: header not recognised (93), amounts packed into wrapped cells (22), scans (26), rotated or interleaved text (15), broken glyphs (7), bidder names not found (25), Office files named `.pdf` (13), and others. 38 more were duplicates of a file already held. One symptom had several causes; counts are by cause.
- **Self-tests.** `python src/parse_bidtabs.py --selftest` runs every rule on small synthetic pages, with the look-alikes each rule must leave alone.

### 8.3 Spreadsheets and disguised files

- Workbooks are read directly.
- 13 Legistar attachments named `.pdf` were Office packages (3 Excel, 10 Word). Files are now recognised by content. Excel is read. Word has no reader; it is counted and skipped.
- One PDF omits a row that was hidden in the source spreadsheet. Every printed total is $16,000 higher than the items. This cannot be recovered from the file.
- Supply sheets and service quotes use the same forms as construction tabs. They are sorted out by content (section 5.1).

### 8.4 Units

- **Spellings.** The unit vocabulary grew round by round: "per" followed by a unit, PER LINEAR, SQ FT, VLF, LSUM, "B.A." for an allowance, and more.
- **Units printed in odd places.** At the end of the description (14 files, 2,601 rows recovered). Inside the quantity cell ("154.83 AC"). A whole unit column stacked into one cell. A unit column under an unrecognised caption (U/M, Measure). A unit cell that repeats the quantity.
- **Round 11** recovered the unit on 8,470 rows. In the 43 files where 90% or more of the rows had no unit, rows with a unit went from 39 to 7,515 ($311 million). A check of 149 rows against the originals found all correct. Six files print no unit at all; they are left alone.
- **When a unit is in doubt, keep the money.** Clear or flag the unit; never delete the row or change the amount. A row without a unit stays in the letting's totals and stays out of every unit-price table. Cases: a lump sum with a quantity above 1; pipe or grout counted per each with a quantity of 500 or more; conduit and cable per each with 100 or more; tracer wire and warning tape per each; trench safety per each.
- **A quantity stated in a lump-sum description** ("(28,731 CY)") becomes the real quantity and unit; the unit price is the lump sum divided by it. The amount does not change.
- **Units are an agency's habit.** One Arlington file lists the same "18–24 inch rock riprap" by SY and by CY. So no SY ↔ CY conversion by thickness is made. Unit conversion was tested later for pricing and not adopted: a rarer unit often marks a different product.

### 8.5 Item families, sizes and "other"

- **The pricing cell** is family + unit + size + quantity band. The estimator first tries the item's words (a token match, `src/item_tokens.py`: Jaccard similarity of at least 0.35, the unit as a hard constraint, and at least one shared dimension when both descriptions carry one). The family cell is the fallback.
- **"other" means unclassified.** It must never set a price. In an early build an invented item could draw $3,970/EA from it. In the latest quality report it still holds 14.4% of observations.
- **The main noun decides** (round 3b). A clause after "with" or "including" no longer sets the family. "Remove and replace X" is priced as X: among 1,589 municipal remove-and-replace items the share filed as demolition fell from 92.3% to 0.5%. A spot check of 238 rows found 3 wrong (1.3%).
- **Unanchored patterns** were a recurring cause. `move-in` matched "reMOVE INlet" (131 rows). `tree` matched "sTREEt" (297 rows). "Remove & Reset" never matched because "&" had been turned into a space (461 rows).
- **The place does not decide the family.** A trail or sidewalk named beside earthwork or planting words, with no surfacing material named, is earthwork or planting. A driveway culvert is pipe, removal or reset, not a driveway.
- **Abandoning is not installing.** Abandoning, plugging or grout-filling an existing pipe is utility removal work, never new pipe.
- **Size rules.**
  - A box culvert's size is its opening area (span × rise, in square feet).
  - A sidewalk priced per LF takes its width in feet.
  - A fire hydrant carries no size (its 6-inch lead once put it in the cell of 8-inch gate valves).
  - A number above 144 inches is not a size.
  - A thickness on an EA or LS line is not a size (one concrete pad went from $16,887 to $2,937 each, against bids of about $1,000).
- **Split what is mixed.** Riprap was one family holding concrete riprap ($624/CY, 62 lettings) and stone riprap ($242/CY, 82 lettings); the mixed median of $336/CY was 46% off both. Rounds 5 to 8 added families such as flowable backfill, grading, pipe bursting, geotextile, landscape planting, bypass pumping, adjust to grade, concrete repair, bridge concrete, fire hydrant and valve assembly.
- **Material supply stays unclassified**, so supply-only lettings stay out of the construction pool.
- **Checking a family change.** Round 12 used a blind review: for each of 128 places the old and the new family were labelled A and B at random and judged against the original page. The new family was chosen in all 128.

### 8.6 De-duplication and public twins

- **The same tab arrives by several routes.** Harvesters de-duplicate by URL, not by content. One document is attached to several agenda items; a tab is attached to the award and again to a later amendment; Mesquite tabs were fetched from the purchasing site and from Legistar; a packet carries a copy of a stand-alone tab.
- **Why it matters.** A duplicate leaves the median about where it was and tightens the spread. The item then looks more stable than it is. The damage lands on the dispersion and on the competition discount, so a median-only check does not show it.
- **The rule** (`src/dedup.py`), applied in the parser and again in the panel build:
  - a row's signature is (bidder, unit, quantity, unit price);
  - two sheets that share 90% or more of their signatures are one letting; below that the match needs corroboration, and the bid date is not used for it (dates are weakest exactly where duplicates are hardest to see);
  - candidate pairs come from an index that skips very common signatures, but the overlap is always recomputed exactly. An earlier version used the capped count as the measure and missed a 3.2-million-dollar letting recorded twice (819 rows);
  - a missing quantity is "none", not NaN, or a row can never match its own copy;
  - the stand-alone tab is kept over the packet copy; a company copy always survives, because it is test data.
- **Not duplicates.** A re-bid of the same project and a revised tab are real observations. Two TxDOT contracts with the same three bidders and identical unit prices were two different projects.
- **TxDOT across feeds.** The current feed and the archive overlap (section 2.1).
- **One file, several lettings.** A council packet can hold two lettings; one letting can span eight sheets. Collapsing a packet into one record once made "14 bidders on one project" out of an 8-bidder and a 6-bidder letting. Sheets are joined into lettings inside the award step (section 8.9).
- **The harvest-side trap.** De-duplicating new downloads by agency + bid number treated a line-item tab (17 bidders, 51 items) as the totals summary already held, and it lay unread in a local packet for three days. Check what kind of file the existing one is.
- **Superseding.** When a text-layer tab replaces a scan that reads zero rows, move the scan out. Otherwise the letting counts twice on the day OCR reads the scan.
- Public copies of company lettings are a separate matter (section 9).

### 8.7 Dates and the pre-2020 quarantine

- **The window.** Bids before the cutoff were priced on a cost base that no longer applies. The cutoff is 2021 for all work, because material inflation landed in 2021–22. It is 2020 for erosion and drainage, because every municipal gabion and creek tab then held was from 2020; a flat 2021 cutoff would have removed the family. The price of keeping 2020 is that those prices must be escalated with an index.
- **Undated files are excluded.** Five files of 2017 once slipped into the active set because neither their name nor their text gave a year.
- **Quarantine, not deletion.** Out-of-window files go to `_pre2020/` and can be restored.
- **Which date wins**, highest first: a manual override with its stated basis (`_sourced_bid_dates.csv`); a full date in the file name; a date printed in the PDF on an opening cue; the year in the bid number (a fiscal year, not the bid year); a bare year in the file name.
- **Dates read wrong.** A footer's print stamp beat the bid date. A start-of-work date and a completion date were taken as the bid date. One tab's original prints "June 23, 2002" for a 2022 letting.
- **The round 9 date rules.** Month names in any case and abbreviated. A weekday, "Certified on" or a time on a quarter hour as a weak cue. Print stamps, "Page N of M" footers, file paths and odd clock times are never dates. A January or February date printed with last year's number gains a year only under four joint conditions. A weak date more than 30 days after the file-name date gives way to the file-name date. These rules changed the date or its precision on 98 lettings.
- **Precise or approximate.** Each date carries its source and whether it is precise. Only precise dates feed the price index. Dates also set the order in which lettings are priced forward in time.
- **Dates move results.** Correcting two lettings read as 2002 instead of 2022 changed the municipal index step for 2021→2022 from +25.6% to +23.1%, and so moved most municipal estimates a little.
- **Open cases.** A county tab that prints no opening date keeps the award date as an upper bound. Some agencies' file-name dates are meeting dates.

### 8.8 The arithmetic audit

- **The test.** Quantity × unit price = the printed extended amount. The document imposes this on itself, so a number glued together from two cells fails it, and a correctly read price passes even when its description or family is wrong.
- **Why not a text search.** "Does this unit price appear in the PDF?" is weaker and misfires: it cannot tell a real price filed under the wrong item from a price concatenated out of two numbers.
- **The audit of 2026-11-18.** 76,951 rows; 88.6% checkable; 97.50% of those balance. 9.3% have a printed amount but no quantity. Those are column-boundary failures; nothing is checkable, so they pass every gate silently.
- **Necessary, not sufficient.** Balance describes only the rows that survived. Any failure that drops rows makes the rate better, not worse. Read it together with coverage: bidder columns, rows per column, and each bidder's sum against the printed total.
- **Later uses of the same identity.** Recovering quantities (14,032 rows where total ÷ unit price agrees across all bidders), choosing the quantity column, proving price columns, refereeing OCR engines, and rejecting a whole bad text layer.
- Rows that fail are flagged (`ext_mismatch`) and kept out of the panel.

### 8.9 Award and bidder identity, alternates and base bids

- **The target is the lowest bid of all bidders,** not the award. A low bidder who was later disqualified still counts: the bid shows a price a contractor was willing to take.
- **The winner is a bidder, not a price.** It is chosen once per letting on the sum of quantity × unit price. Taking the minimum unit price item by item builds a lower envelope that no bidder ever quoted, measured at 23% below the actual winner.
- **Decide or refuse.** Each bidder's total is summed over the schedule quoted by at least half the bidders. A winner is named only if it quoted that whole schedule and every rival's lower bound is strictly above its total. Where the agency printed a column as the low bid, that statement outranks the arithmetic.
- **Completeness.** A decided letting gets a status: `printed_total` (the winner's total matches a printed total to the dollar), `covers_document`, `unverifiable`, or a gated status: `partial`, `total_mismatch`, `unit_price_agreement`, `on_call_quantities`. A gated letting has no winner and is not scored. Its unit prices stay in the pool. The stricter gate of round 4 cut the lettings with a winner from 574 to 487.
- **"Matches the printed total" can be gamed.** A one-row column matches itself and a subtotal can match by chance. Only TOTAL lines and multi-row columns count.
- **Base bid and alternates.**
  - Banners open sections: "Base Bid", "Alternate 1", "ADD ALTERNATE", a numbered schedule with "alternate" anywhere in its title.
  - A numbered row that equals the sum of its N.x sub-rows is a subtotal and is dropped.
  - An item that every bidder's printed total leaves out is moved out of the base bid.
  - Contingency inside the printed base total stays in the base bid.
  - The time part of an A+B letting is not money bid for work. In the TxDOT archive it is 8.2% of the printed winning total at the median and 28.2% at most.
  - In one early template 68% of an agency's rows fell into a false "base bid" because the section was reset on every page.
- **Re-lets** of one project on different dates are separate lettings.
- **Competitive sealed proposals.** In some lettings price is only 40–50% of the score, and the lowest price does not always win. The harvest manifests note them.
- **Bidder names.** One contractor arrives spelled several ways. The canonical key keeps only letters and digits: 1,191 distinct names collapse to 1,077. A different legal suffix is not merged, and a shorter name is not merged into a longer one; a false merge removes a real competitor. Header noise is stripped: addresses, captions, "BID OF", rank numbers, agenda stamps. A price column with no name is never kept as an anonymous bidder: it may be an estimate or an average.
- **Why names matter.** The bidder count sets the competition class and the competition discount. Two spellings of one firm on one tab count as two bidders.

### 8.10 Annual and as-needed contracts

- **Why they are not scored.** They have no fixed scope. The quantities are notional or banded by work-order size. The contract is a spending cap. Bidders know the total is only an evaluation formula and may unbalance their prices. Scoring them would also carry their noise into the P80 constants of every letting.
- **Their unit prices stay** in the pool: they are real competitive prices.
- **How they are found.** By the file name; by the first 8 non-empty lines of the document ("annual … contract", "annual as needed", "work order generated", "term contract", "on-call"); by a schedule of measured items all at quantity 1 (a unit-price agreement); and by a small register for cases the document does not reveal.
- **Limits.** One manhole-lining contract carries no "annual" wording and cannot be recognised by a general rule. "As needed" and "IDIQ" alone were dropped as cues: the first matched item notes and delivery terms, the second a company name.
- **Not the same thing.** An "annual program" with plans and a street list is a defined project and is scored.
- After parser .30 the municipal lettings that can be scored went from 561 to 559.

### 8.11 Telling an engineer's column from a bidder's

A tab often prints the engineer's estimate as one more price column. If it is read as a bidder, an estimate trains the model, and it may even be taken as the low bid.

- **How it is recognised:**
  - a strict pattern for estimate captions, including misspellings ("ENGINEER OPION OF COST", "opinion of probable cost");
  - consultant firm names, and "P.E." only in a header with no bidder label;
  - the letterhead rule: a column named exactly like the firm in the document's first lines, on a line with design-trade words, is the preparer's column. Tested on 2,774 bidder names with 0 false hits. A contractor with "Engineering" in its name is not affected, because its name is not in the letterhead;
  - an estimate caption over a column with no name of its own;
  - an estimate column reprinted on later pages is dropped;
  - a private local configuration may add further labels (`src/local_config.py`, key `engineer_label_patterns`); a checkout without it reads a few public tabs' estimate column as a bidder (`docs/HANDOFF.md`, section 3).
- **A hard-rule check** scanned three ways (names, printed estimate amounts, a column equal to the average of the others) and found no estimate column read as a bidder.
- **The estimate is kept** as a flagged row (`is_engineer_estimate`), for use as a yardstick.
- **Use official totals.** The first TxDOT comparison summed the estimate line by line from the panel. On about one letting in ten that gave roughly half the official total, and the first conclusion was wrong. The comparison now uses the official totals.
- **Printed estimates need care.** Some are rounded, some preliminary, some include a contingency the bids do not.

### 8.12 Coverage of specialty items

- **The decision.** Items with almost no comparable history (pumps, generators, prefabricated bridges) are marked "not covered". The estimate leaves them out and lists them for a separate vendor quote. Any price for them would be unreasonable.
- **The rule** (`src/coverage.py`) was fixed before any scoring under it. It reads only what a schedule shows at estimate time: description, family and unit. Seven classes: pumps and pump or lift station equipment; generators and electrical gear; prefabricated or pedestrian bridges; tanks and process equipment; specialty buildings; synthetic turf and play equipment; meter vaults.
- **Only the equipment itself.** The line must be in a catch-all or equipment family, or be priced per LS or EA. Ordinary civil work at such a facility (pads, fences, paving, pipe), pure removals, and temporary bypass pumping remain covered.
- **Reach.** It flags lines in 58 of 847 municipal lettings.
- **Scoring.** The covered estimate is compared with the low bid minus the low bidder's own amounts on the uncovered lines.
- A schedule with more than half of its lines uncovered is not estimated. A "whole facility" rule was switched off: one lift-station line would have voided a 248-line, $34 million schedule.

---

## 9. The company-data firewall

The project holds a small private set of company bid files. They are test data only. They never enter training, fitting, calibration or any reference price. Nothing about them is in the public repository, and this file names none of them.

- **A structural definition.** A company file is a file at the top level of `data/`. Everything in a subfolder is public. A list of names drifts: the first list missed one company file that had only totals and no line items.
- **Every row carries `source_set`.** `external` is public training data. Company rows carry their own value. Files of the locked public test set carry a third value, `public_test`.
- **Every fitted table admits `external` rows only:** the price pool, the reference tables, the index, the discount, the vocabulary and the calibration.
- **The danger is the same letting on both sides.** Company files are real public lettings. A harvest can pick up the public copy of one, which then arrives labelled `external`. The model would be tested on a letting it had trained on. A byte-level check does not see it, because the two copies are usually in different formats.
- **How a copy (a "twin") is recognised.** By the coverage of (quantity, unit price) pairs. A real copy reached 100%; the next closest public file reached 27.8%. Agency + title + date fails, because titles differ and dates are missing. "Shared bidders plus a few identical prices" gives about 100 matches, nearly all false: a dozen contractors recur on every DFW tab and round pairs are everywhere.
- **Three guards.**
  1. The company-copy scan runs after every harvest and three times in a rebuild: on the raw files, again once OCR text exists, and on the parsed rows. It checks project names, bid numbers, totals and rows against a private pattern list. A hit stops the build. The script and its list are private.
  2. `provenance_check.py` guards the inputs: which files are company files, whether one solicitation sits on both sides, and whether any row of a company or held-out file is labelled `external` in the panel.
  3. `train_test_guard.py` guards the outputs. It does not read a label column. It recomputes every fitted table on copies of the data with the test rows deleted, and again with one test set admitted. The file on disk must equal the external-only result, and admitting the test rows must change the result. If admitting them changes nothing, the guard reports "no evidence" instead of a pass. It exits with an error on any leak.
- **The locked public test set.** 71 public files in 59 letting groups, drawn at the same fraction from every year × work-type stratum, together with every byte-identical copy and every letting the duplicate rule matched to them. It may be opened only to confirm a decision registered beforehand. Every opening is logged in `data/test_holdout/`.
- **What was caught, in general terms.** Two public copies of company lettings were already in the public data on 2026-11-19. Three lettings on the company list were met during a harvest and not downloaded. One totals-only public copy turned up in a council attachment. Two raw agenda packets carried a company letting's amounts on a few pages: a public copy of a company letting can arrive inside a council packet. All were moved to a quarantine folder under `data/raw_downloads/`, outside the parser's reach. In packets, a name hit alone was not enough: a packet was judged a copy only when the letting's own dollar amounts appeared in it.
- **Working rules.** Do not open the top-level files while working on public data. Do not print company rows or the pattern list into logs or reports. Work on parser changes in a private copy that holds public rows only.
- **The public code repository.** A local pre-commit hook blocks any commit that matches the private pattern list. Apart from the register of the locked test set, `data/` is never pushed; nor are `handoff/` and the working reports.

---

## 10. Typical ways a price gets cleaned wrong, and lessons

### 10.1 Examples (public lettings)

| Line item | Median bid | Price before the fix | Cause |
|---|---|---|---|
| Excavation (Trail), 400 CY | $60/CY | $1,387.65/CY; after the fix $33.05/CY | "Trail" put it in the sidewalk family, priced like concrete sidewalk. The job's P50 went from 3.03 to 1.30 times the low bid. |
| Hydromulch | $1.19/SY | $73/SY | Wrong family |
| Paving fabric | $5/SY | $80/SY | Wrong family |
| Full-depth reclamation | $11/SY | $190/SY | Wrong family |
| Trench safety, EA × 1,514 | $6,342 | $1.07; an error of $9.6 million | Unit and quantity read wrong |
| Box-culvert desilting filed as demolition | $75–89/CY | $2,084/CY | Wrong family |
| Conduit, EA × 1,240 | $53 | $1,559 | Wrong unit |
| Turf bedding and temporary irrigation, 280,000 SF | $0.07 and $0.30/SF | $28.34/SF | Wrong family |
| 2-inch asphalt surface course, 95,000 SY | $13.34/SY | $2.92/SY | Shared a cell with TxDOT milling at about $2.50/SY |
| Abandon 8-inch sewer in place | about $10–40/LF | $74.64/LF | Priced as new pipe |
| Excavation printed as LS with "(28,731 CY)" in the text | — | LS × 1 = $861,948; now 28,731 CY × $30 | Quantity hidden in the description |
| A directional-drilled 18-inch force main, 8,133 LF | $185/LF | $6.50/LF | Fell into the unclassified family |
| Mobilization on a small job | $5,000 | $1.04 million | A lump sum priced at a large-job level |

Parser errors of other kinds:

- A whole description column replaced by item numbers after a header fix: 17,503 to 23,545 rows in 158 projects of 7 agencies. One junk row with description "1" and unit "CASE" then held 14.6% of all dollars.
- An alternate's lump-sum total hung on a small repair item of the archive: a maximum of $190,682.95 on an item whose real maximum is $17,500.
- An estimate column read as a bidder and taken as the low bid.
- A bidder whose printed unit price is wrong by a factor of 1,000 (a turf price of $2,262/SF making a $920 million total). Such tabs are held back.

### 10.2 Lessons

1. **Silent loss is the main failure, not misread prices.** Rows are dropped or mislabelled and then filtered out downstream. Nothing errors and the rest looks consistent. An independent audit on 2026-11-18 confirmed 23 defects; not one of the 7 sources was clean.
2. **A metric that improves when data is lost is an incentive, not a metric.** The quality gate gave 1.00 to a parse that had lost 80% of the prices. Always watch coverage beside consistency.
3. **Deleting a row changes the truth.** The low bid is a sum of rows. Clear a doubtful unit; keep the money.
4. **Crafted self-tests are not enough.** Replay the whole corpus after every change, compare file by file, and reproduce each fix by removing it alone. Have someone who did not write the rule check it, starting from doubt.
5. **A wrong family costs more than a misread number,** but the parser's reach shrinks. By round 9 the rules touched lines worth about $3 million of $303 million of line error. The rest is pricing.
6. **Correcting the truth can make the score worse.** A fixed line exposes an error it used to offset. Hence the precedent in section 7.3.
7. **The rules were found on the same 2024–26 lettings they are scored on.** A clean test needs lettings opened after 2026-11-20.
8. **One symptom is not one cause.** "Zero rows" had four different causes; "a dozen bad rules" was 21 rows; "12 rotated pages" was 0 of 90. Separate the causes before counting.
9. **Hand checks are thin.** The first audit compared 6 of 13,956 archive tabs and 3 of 58 Arlington files by hand. Ratios are more reliable than absolute counts taken from different snapshots.
10. **A recomputation that is not the producing code proves nothing.** The guard calls the producing functions themselves on altered copies of the data.

---

## 11. To extend the data

### 11.1 Add a city or an agency

1. Find its platform (section 3). Look at the agenda system first, then the purchasing page. A login portal for bidding does not mean there are no public tabs: look for the council packets.
2. Fetch with a script, under the rules of section 6. Start from the script of a round that used the same platform. In a working copy the scripts of city rounds 1 to 6 are under `handoff/scratchpad/harvest_city*/scripts/`. Useful ones: the CivicClerk search with `$skip` paging, the Legistar `eventitems` sweep, the Destiny and Granicus item listers, and the landing script that computes the md5, de-duplicates, scans and writes the manifest.
3. Land the files in staging with a manifest. Run the routine of section 6, item 10.
4. After the merge, run `src/audit_external.py`, then `src/index_external.py`, then `src/quarantine_pre2020.py`, in this order. Without them a new file is neither classified nor filtered by date. Do not run them beside a parse.
5. Rebuild, pass both guards, and re-score by year.

### 11.2 Re-run a harvest

- **TxDOT open data.** Run both modes of `src/fetch_txdot_bidtabs.py` regularly. The dataset keeps 24 months only.
- **Legistar.** `python src/fetch_legistar_bidtabs.py --clients <clients> --since 2020-01-01`. Name all the clients you want (section 2.3).
- **Collin County.** `python src/fetch_collin_bidtabs.py --ag-from <n> --ag-to <m>`. Agendas 3271 to 3312 held no tab attachments when last checked.
- **CivicClerk and the other platforms.** Re-run the round scripts. Search results are worth keeping: rounds 5 and 6 worked from the pooled results of earlier rounds.

### 11.3 What is still to mine

- 688 CivicClerk packets ranked 301 to 988 were not read (`handoff/scratchpad/harvest_city6/work/cc/rank6.csv`). The yield had fallen to 0–2 new tabs per 50 packets.
- Denton's Laserfiche library can be probed in small batches near known hit numbers.
- Held tabs can be repaired or transcribed by hand: 2 from round 4, 4 left from round 5, 5 from round 6. The most useful are an erosion tab that reads zero rows and two scanned tabs that print an estimate.
- Scanned pages inside packets were OCR'd only where a page looked like a tab.
- Statewide, several sources were not swept to the end; the harvest report lists them.
- **Only the project owner can:** register vendor accounts for the portals that hold the Dallas and Fort Worth tabs, and file public information requests for line-item tabs of projects that publish totals only.
- **Scarce items stay scarce.** TxDOT has no turf reinforcement mat item and no channel grade-control beam. A 12-inch gabion mattress has 0 DFW lettings in the TxDOT data (32 statewide).

### 11.4 Rebuild

- Needs: Python 3.12 with the project's virtual environment; `pdftotext` (poppler) and Tesseract (under `tools/` in the owner's working copy, or as system packages); RapidOCR in the virtual environment. On macOS, Python HTTPS needs the system certificate file.
- Needs the corpus under `data/external/` and `data/txdot/`. The code repository does not hold it. The companion repository holds 2,150 of the original documents; the tabs harvested on 2026-11-21 are not mirrored there yet, and a rebuild from the mirror alone has not been run. The tables production uses are published as the public data bundle (`deploy/README.md`).
- Four company-only steps (`company_twin_scan.py`, `qc_report.py`, `estimate_accuracy_benchmark.py`, `opcc_analysis.py`) exist only in the owner's working copy; `rebuild_all.sh` skips them where they are absent and says so in the log.
- Command: `bash src/rebuild_all.sh`, or `bash src/rebuild_all.sh --from parse_bidtabs` to skip the earlier steps.
- Expect 10 to 25 minutes. Read the log. Both guards must pass.
- After moving the corpus to another machine, verify the checksums. In the one move so far, one PDF arrived with a single corrupted bit and was replaced from the agency's site.

### 11.5 Change the parser

1. Work in a private copy of the project that holds public rows only. Exclude the locked test set and the company files.
2. Write the rule by layout or by wording. Never name a file, an agency or a bid number.
3. Add a self-test for the rule and for the look-alikes it must leave alone. Run `parse_bidtabs.py --selftest`, `award_identity.py --selftest` and `item_specs.py --selftest`.
4. Bump `PARSER_VERSION` and replay the whole corpus. Compare with the previous build file by file: rows, bidder columns, columns whose total is printed in the original, arithmetic failures, lettings with a winner.
5. Look at every file that changed. Check changed low bids against the original page.
6. Score forward in time and apply the adoption rule (section 7.3).
7. Install, rebuild, pass the guards, and record the version and its effect.

### 11.6 Where things are

| Path | What it holds |
|---|---|
| `data/external/<agency>/` | Public bid tabs as published, with per-agency manifests |
| `data/external/legistar/<client>/` | Legistar attachments, with `_manifest_<client>.csv` |
| `data/external/txdot/`, `data/external/txdot_archive/zips/` | TxDOT open-data CSVs and the archive zips |
| `data/external/_ocr/` | OCR sidecars |
| `data/external/_pre2020/` | Quarantined out-of-window files, with `_moved.csv` |
| `data/external/INDEX.csv`, `_audit.csv`, `_duplicates.csv`, `_multi_letting.csv`, `_ocr_engine_choice.csv`, `_ocr_trust.csv`, `_sourced_bid_dates.csv`, `_merge_2026-11-20.csv` | Registers written by the pipeline or kept by hand |
| `data/external/cost_indices/` | HCI, NHCCI and PPI series |
| `data/txdot/` | Dashboard exports and the all-bidders-median benchmark |
| `data/raw_downloads/` | Agenda packets, totals-only files, staging folders, quarantine |
| `data/clean/` | Derived tables: `line_items.csv`, `master_panel.csv`, `price_reference.csv`, `municipal_award.csv` and others |
| `data/test_holdout/` | The register and access log of the locked public test set |
| `reports/` | Harvest reports, parser round reports, build summaries |
