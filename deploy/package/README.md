---
title: Texas Public Works OPCC Estimator
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 7860
pinned: false
short_description: Expected low bid and OPCC for Texas public works
---

# Texas Public Works OPCC Estimator

A web tool for estimators preparing an opinion of probable construction cost
(OPCC) for Texas public works, mostly city and county jobs in the
Dallas-Fort Worth area, and TxDOT work.

Paste or upload a schedule of bid items (Excel, CSV, PDF, or text). The tool
classifies each line with the same rules that built its price history, then
shows:

- the expected low bid (P50) and a recommended OPCC (P80);
- for every line, the past public bids behind its price, with links to the
  public source documents;
- similar past projects, the bidders' spread, and the lines it does not cover
  (pumps, generators, prefabricated bridges and similar items need a vendor
  quote);
- an editable OPCC worksheet you can export to Excel or CSV.

The model's number is one input. The references are there so you can judge
each line yourself.

## Data

The price history is built only from public bid tabulations: city and county
bid tabs and council documents, and TxDOT bid data. Nothing in this demo comes
from private or client files.

## Privacy

Files you upload are processed on this server and not stored. Do not upload
confidential documents. Corrections and saved worksheets stay in your own
browser.

## Source code, method and accuracy

https://github.com/grintCactus/texas-public-works-opcc-estimator

The repository describes the method and reports the measured accuracy,
including item by item, for city and TxDOT work.
