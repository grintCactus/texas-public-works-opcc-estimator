"""The full bid tabulation of one public letting, and its original document.

Every project name the UI shows -- a line's references, "show all", the
similar-project cards -- opens the whole bid of that letting: every line
item as written, every bidder's unit price and extended amount, each
bidder's total with the low bidder marked, and the median. It is built on
demand from data/clean/master_panel.csv, source_set == "external" rows
only (checked again on every row read), and kept in memory.

How a letting is found without reading the 340 MB panel per request: one
background pass after start-up records, per public letting, the byte ranges
of its rows in the file (csv.reader asks for one physical line at a time and
never reads ahead, so the byte count after each row is where the next row
starts). A request then reads only those ranges. If the panel file changes
on disk (a rebuild) the ranges are rebuilt before the next read.

The original document:
  * a public link when one is recorded -- the harvest manifests
    (data/external/**/_manifest*.csv, a `url` / `source_url` column or the
    first http value of the row) joined on the file, then
    project_metadata.csv; else "no public link recorded";
  * the local copy, served by the UI only when the path resolves (symlinks
    followed) to a regular file inside data/external/ that is the source of
    a public letting. Never anything else: not top-level data/*.pdf or
    *.xlsx (company files), not ui/.inbox or ui/.cache, no "..", no
    absolute paths.

The engineer's estimate is shown only when the tab printed one, labelled as
such; nothing here is an input to any estimate.
"""

from __future__ import annotations

import collections
import csv
import glob
import io
import os
import statistics
import threading
import time
import urllib.parse
from typing import Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA_DIR = os.path.join(ROOT, "data")
EXTERNAL_DIR = os.path.join(DATA_DIR, "external")
PANEL = os.path.join(DATA_DIR, "clean", "master_panel.csv")

#: Views kept in memory (a big tab is ~1,500 rows).
CACHE_SIZE = 48

EE_LABEL = ("Engineer's estimate as printed on this tabulation -- shown for "
            "reference only, never an input to the estimate.")

COLS = ("source_set", "project_key", "source", "owner", "bid_date",
        "date_precision", "item_no", "description", "desc_norm", "unit",
        "quantity", "bidder", "bidder_norm", "is_engineer_estimate",
        "is_low_bidder", "unit_price", "work_type", "competition_class",
        "project_kind", "price_basis", "n_bidders")


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("true", "1", "yes")


class _Lines:
    """A binary file's lines as text for csv.reader, counting bytes."""

    def __init__(self, fh):
        self.fh = fh
        self.pos = fh.tell()

    def __iter__(self):
        return self

    def __next__(self):
        b = self.fh.readline()
        if not b:
            raise StopIteration
        self.pos += len(b)
        return b.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# Public links
# ---------------------------------------------------------------------------

def _url_of(row: dict) -> str:
    """The row's public URL: `url`, `source_url`, else the first http(s)
    value. Nothing but http(s) is ever returned."""
    for k in ("url", "source_url"):
        v = str(row.get(k) or "").strip()
        if v.lower().startswith(("http://", "https://")):
            return v
    for v in row.values():
        v = str(v or "").strip()
        if v.lower().startswith(("http://", "https://")):
            return v
    return ""


def host_of(url: str) -> str:
    try:
        h = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return h[4:] if h.startswith("www.") else h


def source_kind(row: dict, host: str) -> str:
    """What the linked page is, in a few words, from the manifest row."""
    t = " ".join(str(row.get(k) or "") for k in (
        "source_kind", "source", "source_document", "kind")).lower()
    t += " " + (host or "").lower()
    if "ionwave" in t or "award" in t:
        return "public bid award attachment"
    if "packet" in t:
        return "council agenda packet"
    if any(w in t for w in ("agenda", "legistar", "granicus", "civicclerk",
                            "destinyhosted", "m&c", "icompass", "council")):
        return "council agenda attachment"
    if any(w in t for w in ("laserfiche", "archive", "weblink")):
        return "public records archive"
    return "public source document"


def safe_url(url: str) -> str:
    """http(s) URLs only -- anything else (javascript:, file:, data:) is
    dropped rather than linked."""
    u = str(url or "").strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urllib.parse.urlsplit(u)
    except ValueError:
        return ""
    return u if parts.scheme.lower() in ("http", "https") and parts.netloc else ""


def load_manifest_links(external_dir: str = None) -> Dict[str, dict]:
    """{"external/<folder>/<file>": {"url", "kind"}} from the harvest
    manifests (data/external/**/_manifest*.csv), plus
    {"#base:<top folder>/<file name>": ...} for a manifest whose `file`
    column lost its sub-folder."""
    ext = external_dir or EXTERNAL_DIR
    out: Dict[str, dict] = {}
    for mf in sorted(glob.glob(os.path.join(ext, "**", "_manifest*.csv"),
                               recursive=True)):
        if os.path.basename(mf) == "_manifest_for_parsing.csv":
            continue
        rel_dir = os.path.relpath(os.path.dirname(mf), os.path.dirname(ext))
        rel_dir = rel_dir.replace("\\", "/")          # external/<folder...>
        top = rel_dir.split("/")[1] if "/" in rel_dir else ""
        try:
            with open(mf, encoding="utf-8-sig", newline="") as fh:
                for r in csv.DictReader(fh):
                    f = str(r.get("file") or r.get("src_file") or "").strip()
                    url = safe_url(_url_of(r))
                    if not f or not url:
                        continue
                    f = f.replace("\\", "/")
                    while f.startswith("./"):
                        f = f[2:]
                    if f.startswith("external/"):
                        key = f
                    elif "/" in rel_dir and f.startswith(rel_dir.split("/", 1)[1] + "/"):
                        key = "external/" + f
                    else:
                        key = rel_dir + "/" + f
                    link = {"url": url, "kind": source_kind(r, host_of(url))}
                    out.setdefault(key, link)
                    out.setdefault("#base:%s/%s" % (top, f.rsplit("/", 1)[-1]),
                                   link)
        except (OSError, csv.Error, UnicodeDecodeError):
            continue
    return out


class SourceLinks:
    """Where a letting came from: its public page (when a manifest or
    project_metadata.csv records one) and its local copy under
    data/external (when the file is there)."""

    def __init__(self, external_dir: str = None):
        self.external_dir = external_dir
        self._links: Optional[Dict[str, dict]] = None
        self._lock = threading.Lock()

    def links(self) -> Dict[str, dict]:
        with self._lock:
            if self._links is None:
                self._links = load_manifest_links(self.external_dir)
            return self._links

    def get(self, pk: str, meta: Optional[dict] = None) -> dict:
        pk = str(pk or "")
        f = pk.split("::")[0]
        link = self.links().get(f)
        if link is None and f.startswith("external/"):
            parts = f.split("/")
            if len(parts) >= 3:
                link = self.links().get("#base:%s/%s" % (parts[1], parts[-1]))
        url, kind = (link or {}).get("url", ""), (link or {}).get("kind", "")
        if not url and meta:
            url = safe_url(meta.get("url") or "")
            if url:
                kind = source_kind({"source_kind": meta.get("source_group")},
                                   host_of(url))
        host = host_of(url) if url else ""
        local = resolve_source(f) if f.startswith("external/") else None
        return {
            "document": f.rsplit("/", 1)[-1] if f.startswith("external/") else pk,
            "url": url, "host": host, "kind": kind,
            "label": ("Source: %s — %s" % (host, kind)) if url else "",
            "no_url_note": "" if url else "no public link recorded",
            "local_path": f if local else "",
            "local_kind": (os.path.splitext(f)[1].lstrip(".").upper()
                           if local else ""),
        }


# ---------------------------------------------------------------------------
# The original file under data/external
# ---------------------------------------------------------------------------

def resolve_source(rel: str, known: Optional[set] = None,
                   data_dir: str = None, external_dir: str = None) -> Optional[str]:
    """The absolute path of a public letting's original file, or None.

    `rel` is the file part of a letting key, relative to data/
    ("external/<folder>/<file>"). Refused: an empty or absolute path, a NUL,
    any "." or ".." segment, a path not under external/, a path that
    resolves (symlinks followed) outside data/external/, anything but a
    regular file, and -- when `known` is given -- a file that is not the
    source of a public letting."""
    data_dir = data_dir or DATA_DIR
    external_dir = external_dir or EXTERNAL_DIR
    if not isinstance(rel, str) or not rel or "\x00" in rel:
        return None
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or os.path.isabs(rel) or ":" in rel.split("/")[0]:
        return None
    parts = rel.split("/")
    if parts[0] != "external" or len(parts) < 2 or \
            any(p in ("", ".", "..") for p in parts):
        return None
    if known is not None and rel not in known:
        return None
    full = os.path.realpath(os.path.join(data_dir, *parts))
    root = os.path.realpath(external_dir)
    if not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    return full


CONTENT_TYPES = {
    ".pdf": ("application/pdf", True),
    ".png": ("image/png", True),
    ".jpg": ("image/jpeg", True),
    ".jpeg": ("image/jpeg", True),
    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
              False),
    ".xls": ("application/vnd.ms-excel", False),
    ".csv": ("text/csv", False),
    ".docx": ("application/vnd.openxmlformats-officedocument."
              "wordprocessingml.document", False),
}


def content_type(path: str) -> Tuple[str, bool]:
    """(MIME type, show inline?) -- only PDFs and images open in the
    browser; anything else is a download, never rendered on the UI's own
    origin."""
    ext = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(ext, ("application/octet-stream", False))


# ---------------------------------------------------------------------------
# The index and the views
# ---------------------------------------------------------------------------

class LettingTabs:
    def __init__(self, panel_path: str = None):
        self.path = panel_path or PANEL
        self._lock = threading.RLock()
        self._ranges: Optional[Dict[str, List[List[int]]]] = None
        self._stamp = None
        self._header: List[str] = []
        self._cache: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
        self.sources = SourceLinks()
        self.files: set = set()
        self.state = {"ready": False, "error": "", "lettings": 0,
                      "seconds": None}

    # -- index ---------------------------------------------------------------
    def _file_stamp(self):
        st = os.stat(self.path)
        return (st.st_mtime_ns, st.st_size)

    def build(self) -> None:
        """One pass over the panel: byte ranges of every public letting."""
        t0 = time.time()
        csv.field_size_limit(10 ** 8)
        ranges: Dict[str, List[List[int]]] = {}
        with open(self.path, "rb") as fh:
            stamp = self._file_stamp()
            head = fh.readline().decode("utf-8-sig")
            header = next(csv.reader([head]))
            i_s, i_k = header.index("source_set"), header.index("project_key")
            lines = _Lines(fh)
            rd = csv.reader(lines)
            start = lines.pos
            for row in rd:
                end = lines.pos
                if len(row) > max(i_s, i_k) and row[i_s] == "external":
                    pk = row[i_k]
                    rr = ranges.get(pk)
                    if rr and rr[-1][1] == start:
                        rr[-1][1] = end
                    elif rr:
                        rr.append([start, end])
                    else:
                        ranges[pk] = [[start, end]]
                start = end
        self._ranges, self._stamp, self._header = ranges, stamp, header
        self._cache.clear()
        self.files = {pk.split("::")[0] for pk in ranges
                      if pk.startswith("external/")}
        self.state.update(ready=True, error="", lettings=len(ranges),
                          seconds=round(time.time() - t0, 1))

    def ensure(self) -> None:
        with self._lock:
            try:
                fresh = self._ranges is not None and \
                    self._stamp == self._file_stamp()
            except OSError as e:
                self.state.update(ready=False, error=str(e))
                raise
            if not fresh:
                try:
                    self.build()
                except Exception as e:  # noqa: BLE001
                    self.state.update(ready=False, error="%s: %s"
                                      % (type(e).__name__, e))
                    raise

    def keys(self) -> List[str]:
        self.ensure()
        return list(self._ranges or {})

    def rows(self, pk: str) -> List[dict]:
        """The letting's panel rows, public only."""
        self.ensure()
        rr = (self._ranges or {}).get(pk)
        if not rr:
            return []
        h = self._header
        ix = {c: h.index(c) for c in COLS if c in h}
        out = []
        with open(self.path, "rb") as fh:
            for a, b in rr:
                fh.seek(a)
                text = fh.read(b - a).decode("utf-8", "replace")
                for row in csv.reader(io.StringIO(text, newline="")):
                    if len(row) != len(h):
                        continue
                    d = {c: row[i] for c, i in ix.items()}
                    # belt and braces: only this letting, only public rows
                    if d.get("source_set") != "external" or \
                            d.get("project_key") != pk:
                        continue
                    out.append(d)
        return out

    # -- the view -------------------------------------------------------------
    def view(self, pk: str, engine) -> Optional[dict]:
        with self._lock:
            self.ensure()
            if pk in self._cache:
                self._cache.move_to_end(pk)
                return self._cache[pk]
        rows = self.rows(pk)
        if not rows:
            return None
        v = build_view(pk, rows, engine, self)
        with self._lock:
            self._cache[pk] = v
            while len(self._cache) > CACHE_SIZE:
                self._cache.popitem(last=False)
        return v

    def source(self, pk: str, meta: Optional[dict] = None) -> dict:
        return self.sources.get(pk, meta)


def _line_key(d: dict) -> tuple:
    return (d.get("item_no") or "", (d.get("desc_norm") or "").strip(),
            d.get("unit") or "", d.get("quantity") or "")


def build_view(pk: str, rows: List[dict], engine, tabs: Optional[LettingTabs] = None) -> dict:
    """The whole tab of one letting from its public rows."""
    import engine_production as ep
    panel = getattr(engine, "panel", None)
    meta = (panel.meta.get(pk) if panel is not None else None) or {}
    market = None
    if panel is not None and pk in getattr(panel, "lettings", {}):
        market = panel.lettings[pk][0]
    if market is None:
        market = "txdot" if (rows[0].get("source") or "").lower().startswith(
            "txdot") else "municipal"

    bidders: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
    ee_rows: List[dict] = []
    lines: List[dict] = []
    open_line: Dict[tuple, List[int]] = {}     # line key -> line indexes
    for d in rows:
        u, q = _f(d.get("unit_price")), _f(d.get("quantity"))
        if _truthy(d.get("is_engineer_estimate")):
            ee_rows.append(d)
            continue
        bk = d.get("bidder_norm") or d.get("bidder") or "(bidder not named)"
        b = bidders.get(bk)
        if b is None:
            b = bidders[bk] = {"id": bk, "name": (d.get("bidder") or bk).strip(),
                               "total": 0.0, "priced_lines": 0,
                               "flag_low": False}
        if d.get("is_low_bidder") == "True":
            b["flag_low"] = True
        k = _line_key(d)
        # the first instance of this line that has no price from this bidder
        # yet: identical lines in two schedules stay two lines
        li = None
        for j in open_line.get(k, []):
            if bk not in lines[j]["_p"]:
                li = j
                break
        if li is None:
            li = len(lines)
            lines.append({"item_no": d.get("item_no") or "",
                          "description": d.get("description") or "",
                          "dn": (d.get("desc_norm") or "").strip(),
                          "unit": d.get("unit") or "",
                          "quantity": q, "_p": {}, "_ee": None})
            open_line.setdefault(k, []).append(li)
        amt = (q * u) if (u is not None and u > 0 and q is not None and q > 0) \
            else None
        lines[li]["_p"][bk] = (u, amt)
        if amt is not None:
            b["total"] += amt
            b["priced_lines"] += 1
    # the engineer's estimate, when printed: on the matching lines
    ee_total = 0.0
    for d in ee_rows:
        u, q = _f(d.get("unit_price")), _f(d.get("quantity"))
        k = _line_key(d)
        idx = open_line.get(k)
        amt = (q * u) if (u is not None and u > 0 and q is not None and q > 0) \
            else None
        if idx:
            for j in idx:
                if lines[j]["_ee"] is None:
                    lines[j]["_ee"] = (u, amt)
                    break
        if amt is not None:
            ee_total += amt

    blist = sorted(bidders.values(),
                   key=lambda b: (b["priced_lines"] == 0, b["total"]))
    flagged = [b for b in blist if b["flag_low"]]
    low_id = flagged[0]["id"] if flagged else (blist[0]["id"] if blist else None)
    order = [b["id"] for b in blist]
    out_lines = []
    for ln in lines:
        prices = []
        us = []
        for bk in order:
            p = ln["_p"].get(bk)
            if p is None:
                prices.append(None)
                continue
            prices.append({"unit_price": p[0], "amount": p[1]})
            if p[0] is not None and p[0] > 0:
                us.append(p[0])
        lowp = ln["_p"].get(low_id) if low_id else None
        ee = ln["_ee"]
        out_lines.append({
            "item_no": ln["item_no"], "description": ln["description"],
            "dn": ln["dn"], "unit": ln["unit"], "quantity": ln["quantity"],
            "prices": prices,
            "median_unit": statistics.median(us) if us else None,
            "low_unit": lowp[0] if lowp else None, "n_bids": len(us),
            "ee": {"unit_price": ee[0], "amount": ee[1]} if ee else None})
    first = rows[0]
    owner_id = first.get("owner") or ""
    if panel is not None:
        wt = panel.first_wt.get(pk, first.get("work_type") or "")
    else:
        wt = first.get("work_type") or ""
    dates = sorted({d.get("bid_date") for d in rows if d.get("bid_date")})
    classes = {
        "competition_class": sorted({d.get("competition_class") or ""
                                     for d in rows} - {""}),
        "project_kind": sorted({d.get("project_kind") or "" for d in rows} - {""}),
        "price_basis": sorted({d.get("price_basis") or "" for d in rows} - {""}),
    }
    src = tabs.source(pk, meta) if tabs is not None else {}
    return {
        "key": pk, "market": market,
        "project": ep.project_title(pk, market),
        "title": meta.get("title") or "",
        "owner_id": owner_id,
        "owner": (ep.owner_name(owner_id) if market == "municipal"
                  else (("TxDOT, %s County" % meta["county"])
                        if meta.get("county") else "TxDOT")),
        "date": dates[0] if dates else "",
        "date_precision": first.get("date_precision") or "",
        "work_type": (ep.work_type_label(wt) if market == "municipal"
                      else (wt or "").title()),
        "n_bidders": len(blist),
        "bidders": [{"id": b["id"], "name": b["name"], "total": b["total"],
                     "priced_lines": b["priced_lines"],
                     "low": b["id"] == low_id} for b in blist],
        "low_bidder": low_id,
        "engineer_estimate": ({"total": ee_total, "label": EE_LABEL}
                              if ee_rows else None),
        "lines": out_lines,
        "n_rows": len(rows),
        "classes": classes,
        "source": src,
        "public_only": True,
        "note": ("Every row of this letting in data/clean/master_panel.csv "
                 "with source_set = external: unit prices as tabulated, "
                 "extended = quantity × unit price, totals = the sum of a "
                 "bidder's extended amounts."),
    }


def to_csv(v: dict) -> bytes:
    """The tab as a CSV file (UTF-8 with BOM, opens in Excel)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Full bid tabulation", v.get("project") or v.get("key")])
    for k, lab in (("title", "Title"), ("owner", "Owner"), ("date", "Letting date"),
                   ("work_type", "Work type"), ("n_bidders", "Bidders")):
        w.writerow([lab, v.get(k)])
    src = v.get("source") or {}
    w.writerow(["Source", src.get("document") or "",
                src.get("url") or src.get("no_url_note") or ""])
    w.writerow(["Letting key", v.get("key")])
    w.writerow(["Data", "public rows only (source_set = external); "
                        "extended = quantity × unit price"])
    w.writerow([])
    bidders = v.get("bidders") or []
    w.writerow(["Bidder", "Total", "Low bidder"])
    for b in bidders:
        w.writerow([b["name"], "%.2f" % b["total"], "yes" if b["low"] else ""])
    ee = v.get("engineer_estimate")
    if ee:
        w.writerow(["Engineer's estimate (printed on the tab; never an input)",
                    "%.2f" % ee["total"], ""])
    w.writerow([])
    head = ["Item", "Description", "Unit", "Quantity"]
    for b in bidders:
        head += ["%s unit price" % b["name"], "%s amount" % b["name"]]
    head += ["Median unit price"]
    if ee:
        head += ["Engineer's estimate unit price", "Engineer's estimate amount"]
    w.writerow(head)

    def num(x, nd=2):
        return "" if x is None else ("%.*f" % (nd, x))

    for ln in v.get("lines") or []:
        r = [ln["item_no"], ln["description"], ln["unit"],
             num(ln["quantity"], 4).rstrip("0").rstrip(".")
             if ln["quantity"] is not None else ""]
        for p in ln["prices"]:
            r += [num(p["unit_price"]) if p else "", num(p["amount"]) if p else ""]
        r += [num(ln["median_unit"])]
        if ee:
            e = ln.get("ee") or {}
            r += [num(e.get("unit_price")), num(e.get("amount"))]
        w.writerow(r)
    tot = ["", "Total", "", ""]
    for b in bidders:
        tot += ["", "%.2f" % b["total"]]
    w.writerow(tot)
    return ("﻿" + buf.getvalue()).encode("utf-8")
