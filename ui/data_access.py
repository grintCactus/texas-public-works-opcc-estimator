"""Read-only access layer over data/clean.

Contract rules this module enforces (the ETL upstream is still running and
rewrites these files, possibly adding columns):

* Every frame is read BY COLUMN NAME. Nothing is positional.
* Extra columns are tolerated and carried through untouched.
* A missing REQUIRED column raises DataContractError at load time with the
  file name, the missing column and the columns that were actually present --
  it never degrades into a silent KeyError halfway through a price lookup.
* The CSVs are written with a UTF-8 BOM, so everything is read with
  encoding="utf-8-sig". Reading them as plain utf-8 silently renames the first
  column to "\\ufeffitem_family" and every lookup on it misses.

master_panel.csv is ~164 MB and is re-read as a Parquet cache keyed on the
source file's mtime+size, so a rewrite by the ETL invalidates the cache
automatically.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------

UI_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(UI_DIR)
CLEAN_DIR = os.path.join(PROJECT_ROOT, "data", "clean")

#: A directory searched BEFORE data/clean, set with BID_UI_CLEAN_OVERLAY.
#: Per file, not all-or-nothing: drop one candidate CSV in it and everything
#: else still resolves normally. It exists so a table can be tried against the
#: real app -- a new ETL output before it replaces the live one, or a consumer
#: written ahead of the file it reads -- without writing into data/clean,
#: which this UI must never touch.
OVERLAY_DIR = os.environ.get("BID_UI_CLEAN_OVERLAY", "").strip() or None
CACHE_DIR = os.path.join(UI_DIR, ".cache")
SRC_DIR = os.path.join(PROJECT_ROOT, "src")


class DataContractError(RuntimeError):
    """A file under data/clean is missing a column the UI depends on."""


# Columns that MUST be read as strings. pandas infers an all-integer `year`
# column as float64 the moment it contains one blank, so a clean 2026 becomes
# "2026.0" under astype(str) and every year comparison silently misses --
# no error, just zero rows. The same bites id-shaped columns: TxDOT item
# numbers (104-7001), CSJs (0918-47-015) and the pipe-joined item_key, where a
# coerced value breaks the join to price_by_quantity_band without complaint.
STRING_COLUMNS = {
    "year": str, "item_no": str, "item_key": str, "project_key": str,
    "bid_date": str, "src_file": str, "csj": str, "size_bucket": str,
    "owner": str, "unit": str, "desc_norm": str, "item_family": str,
    # Band LABELS ("50-200"), not quantities. They start with `qty_`, so the
    # numeric coercion below turned every one of them into NaN on load -- the
    # whole family shape table read as present and matched nothing, with no
    # error anywhere.
    "qty_band": str, "anchor_band": str,
}

# Rows the upstream parser has flagged as misread. ext_mismatch means
# unit price x quantity did not reconcile with the extended amount on the
# sheet; unit_suspect means the unit column does not hold units (a sign the
# columns slipped). Either way some number on that row is wrong. They are
# already excluded from price_reference upstream, but the UI reads the raw
# panel directly for comparables -- and showing a cost engineer a misparsed
# row as evidence is worse than showing them nothing.
QUALITY_FLAG_COLUMNS = ("ext_mismatch", "unit_suspect")


@dataclass(frozen=True)
class TableSpec:
    filename: str
    required: tuple          # hard failure if absent
    optional: tuple = ()     # used when present, ignored when not
    label: str = ""
    #: True when the FILE itself may be absent. The loader then returns an
    #: empty frame with the declared columns instead of raising, so the UI can
    #: ship the consumer for a table the ETL has not produced yet and have it
    #: switch on by itself when the file lands. Required columns are still
    #: enforced once the file exists.
    optional_file: bool = False


# The required/optional split is the actual contract. Anything in `required`
# is load-bearing for pricing; anything in `optional` is an enrichment the UI
# degrades gracefully without.
TABLES: Dict[str, TableSpec] = {
    "price_reference": TableSpec(
        "price_reference.csv",
        required=("item_family", "unit", "n_projects", "price_p25",
                  "price_median", "price_p75", "price_form", "usable"),
        optional=(# The ETL's own dollar-weighted sample midpoint, preferred
                  # over the panel derivation: three consumers each deriving
                  # the same quantity is three different answers.
                  "vintage_year",
                  "size_bucket", "qty_elasticity", "qty_median", "iqr_ratio",
                  "first_year", "last_year", "n_owners", "n_bid_obs",
                  "n_items_for_elasticity"),
        label="Unit-price reference",
    ),
    "competition_discount": TableSpec(
        "competition_discount_pooled.csv",
        required=("n_bidders", "low_over_median"),
        optional=("lettings", "p25", "p75", "sigma_log"),
        label="Competition discount",
    ),
    "quantity_band": TableSpec(
        "price_by_quantity_band.csv",
        required=("item_key", "unit"),
        optional=("0-10", "10-50", "50-200", "200-1000", "1000-5000",
                  "5000-25000", "25000+", "small_over_large", "n_bands",
                  "fam", "desc"),
        label="Price by quantity band",
    ),
    "erosion_reference": TableSpec(
        "erosion_price_reference.csv",
        required=("specialty", "unit", "n_projects", "median"),
        optional=("p25", "p75", "dfw_projects", "dfw_median",
                  # Added upstream so channel work gets a real quantity pivot;
                  # deliberately named as in price_reference.csv.
                  "qty_median", "qty_p25", "qty_p75"),
        label="Erosion specialty reference",
    ),
    "txdot_price_book": TableSpec(
        "price_book_txdot.csv",
        required=("item_no", "unit", "price_median", "usable"),
        optional=("description", "n_lettings", "price_p25", "price_p75",
                  "qty_median", "iqr_ratio", "first_year", "last_year"),
        label="TxDOT price book",
    ),
    "vocabulary": TableSpec(
        "item_vocabulary.csv",
        required=("desc_norm", "item_family", "unit", "n_projects"),
        optional=("pooled_median_price", "price_p25", "price_p75",
                  "example_description", "qty_min", "qty_max", "size_in",
                  "material", "n_bid_observations", "total_dollars"),
        label="Item vocabulary",
    ),
    "project_metadata": TableSpec(
        "project_metadata.csv",
        required=("src_file", "owner"),
        optional=("project_title", "work_type", "is_construction",
                  "award_amount", "agenda_date", "source_group", "url"),
        label="Project metadata",
    ),
    "price_time_index": TableSpec(
        "price_time_index.csv",
        required=("to", "index"),
        optional=("from", "n_matched_items", "yoy_pct"),
        label="Price time index (pooled)",
    ),
    # Quantity SHAPE per (item_family, unit), from a two-way fixed-effects
    # fit -- each item's own price level is absorbed, so what is left is pure
    # shape and immune to which items happen to sit in which band.
    "qty_shape_family": TableSpec(
        "price_by_quantity_band_family.csv",
        required=("item_family", "unit", "qty_band", "shape_multiplier"),
        optional=("anchor_band", "n_items_in_band", "n_items", "n_obs",
                  "resid_sigma_log", "resid_sigma_log_band",
                  "bands_in_component", "dropped_components", "converged",
                  "scope"),
        label="Quantity shape by family",
        optional_file=True,
    ),
    # Measured error of borrowing a family's shape for one item, by how many
    # quantity bands the adjustment crosses.
    "qty_borrow_sigma": TableSpec(
        "qty_shape_borrow_sigma.csv",
        required=("band_distance", "sigma_log_wellsupported"),
        optional=("n_pairs", "sigma_log", "n_pairs_wellsupported", "scope"),
        label="Shape-borrowing error",
        optional_file=True,
    ),
    # End-to-end accuracy of the modelling session's estimator, measured out
    # of fold. The quantiles are of est/actual, so actual = est / ratio.
    "estimator_calibration": TableSpec(
        "estimator_calibration.csv",
        required=("market", "mode", "median_ratio"),
        optional=("n", "median_abs_error", "p10", "p25", "p75", "p90",
                  "level_correction", "discount_table", "train_scope"),
        label="Estimator calibration",
        optional_file=True,
    ),
    # Per-basket chained index. The pooled series above must NOT be used for
    # channel work: over ten years the erosion basket runs 160.7 against the
    # pooled 139.6, because the pooled median is held down by high-row-count
    # families that do not track material inflation.
    "price_index_basket": TableSpec(
        "price_time_index_by_basket.csv",
        required=("basket", "to_year", "index_level"),
        optional=("from_year", "yoy_pct", "n_matched_items", "scope",
                  "base_year"),
        label="Price index by basket",
        optional_file=True,
    ),
}

# master_panel is handled separately (size + Parquet cache).
MASTER_PANEL_FILE = "master_panel.csv"
MASTER_PANEL_REQUIRED = (
    "source", "owner", "project_key", "bid_date", "description",
    "item_family", "unit", "quantity", "bidder", "unit_price", "n_bidders",
    "competition_class", "project_kind",
)
MASTER_PANEL_OPTIONAL = (
    "desc_norm", "item_key", "year", "size_bucket", "size_in", "item_no",
    "is_low_bidder", "is_engineer_estimate", "price_basis", "constr_share",
    # Added upstream. `source_set` separates public training data from the
    # client's own files, which are test-only; `letting_id` is the real
    # bidding unit (one PDF can hold several lettings, so src_file is not a
    # project key).
    "source_set", "letting_id", "award_basis", "material", "work_type",
    # Quality flags and date precision, added upstream after the first cut.
    "ext_mismatch", "unit_suspect", "date_precision",
)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_lock = threading.RLock()
_cache: Dict[str, pd.DataFrame] = {}
_load_report: Dict[str, dict] = {}
#: Populated when the panel cache is (re)built; surfaced in the UI's data panel.
_panel_build_stats: Dict[str, int] = {}


def _path(filename: str) -> str:
    if OVERLAY_DIR:
        over = os.path.join(OVERLAY_DIR, filename)
        if os.path.exists(over):
            return over
    return os.path.join(CLEAN_DIR, filename)


def _check_columns(df: pd.DataFrame, spec: TableSpec, filename: str) -> None:
    missing = [c for c in spec.required if c not in df.columns]
    if missing:
        raise DataContractError(
            "%s is missing required column(s) %s. Columns present: %s. "
            "The upstream ETL may have renamed them -- ui/data_access.py "
            "TABLES must be updated to match." % (
                filename, ", ".join(missing), ", ".join(map(str, df.columns)))
        )


def _numeric(df: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def table(name: str) -> pd.DataFrame:
    """Load a reference table by logical name, cached for the process."""
    with _lock:
        if name in _cache:
            return _cache[name]
        if name not in TABLES:
            raise KeyError("unknown table %r (known: %s)"
                           % (name, ", ".join(sorted(TABLES))))
        spec = TABLES[name]
        p = _path(spec.filename)
        if not os.path.exists(p):
            if spec.optional_file:
                df = pd.DataFrame(columns=list(spec.required) +
                                          list(spec.optional))
                _load_report[name] = {
                    "file": spec.filename, "rows": 0,
                    "unexpected_columns": [], "mtime": None,
                    "absent": True,
                }
                _cache[name] = df
                return df
            raise DataContractError(
                "%s not found under %s. The UI reads data/clean read-only; "
                "run the ETL that produces it first." % (spec.filename, CLEAN_DIR))
        head = pd.read_csv(p, encoding="utf-8-sig", nrows=0)
        head.columns = [str(c).strip() for c in head.columns]
        dtypes = {c: t for c, t in STRING_COLUMNS.items() if c in head.columns}
        df = pd.read_csv(p, encoding="utf-8-sig", low_memory=False, dtype=dtypes)
        df.columns = [str(c).strip() for c in df.columns]
        _check_columns(df, spec, spec.filename)
        df = _numeric(df, [c for c in df.columns if c not in STRING_COLUMNS
                           and c.startswith(
            ("price_", "n_", "qty_", "p25", "p75", "median", "dfw_", "sigma",
             "iqr_", "low_over", "index", "award_", "small_over", "pooled_",
             "total_"))])
        extra = [c for c in df.columns
                 if c not in spec.required and c not in spec.optional]
        _load_report[name] = {
            "file": spec.filename, "rows": int(len(df)),
            "unexpected_columns": extra,
            "mtime": os.path.getmtime(p),
        }
        if extra:
            log.info("%s has %d column(s) the UI does not use yet: %s",
                     spec.filename, len(extra), ", ".join(extra))
        _cache[name] = df
        return df


# ---------------------------------------------------------------------------
# master_panel: Parquet cache
# ---------------------------------------------------------------------------

def _cache_token(path: str) -> str:
    st = os.stat(path)
    return "%d_%d" % (int(st.st_mtime), st.st_size)


def _panel_schema_token() -> str:
    """Fingerprint of the columns the UI asks for.

    The cache key is the CSV's mtime+size, which notices an ETL rewrite but
    NOT a change on this side. Adding a column to MASTER_PANEL_OPTIONAL left
    the old Parquet in place, so the new column stayed silently absent and
    every consumer saw the shape it had yesterday.
    """
    import hashlib
    cols = ",".join(MASTER_PANEL_REQUIRED + MASTER_PANEL_OPTIONAL)
    return hashlib.sha1(cols.encode("utf-8")).hexdigest()[:8]



def _panel_cache_path(token: str) -> str:
    return os.path.join(CACHE_DIR, "master_panel_%s_%s.parquet"
                        % (token, _panel_schema_token()))


def _build_panel_cache(src: str, dest: str) -> pd.DataFrame:
    """Read master_panel.csv in chunks, keep the comparables columns, write Parquet."""
    header = pd.read_csv(src, encoding="utf-8-sig", nrows=0)
    header.columns = [str(c).strip() for c in header.columns]
    present = set(header.columns)
    missing = [c for c in MASTER_PANEL_REQUIRED if c not in present]
    if missing:
        raise DataContractError(
            "%s is missing required column(s) %s. Columns present: %s."
            % (MASTER_PANEL_FILE, ", ".join(missing), ", ".join(sorted(present))))
    usecols = [c for c in MASTER_PANEL_REQUIRED + MASTER_PANEL_OPTIONAL
               if c in present]
    dtypes = {c: t for c, t in STRING_COLUMNS.items() if c in usecols}
    for c in QUALITY_FLAG_COLUMNS:
        if c in usecols:
            dtypes[c] = str

    n_raw = n_flagged = 0
    parts: List[pd.DataFrame] = []
    reader = pd.read_csv(src, encoding="utf-8-sig", usecols=usecols,
                         chunksize=250_000, low_memory=False, dtype=dtypes)
    for chunk in reader:
        chunk.columns = [str(c).strip() for c in chunk.columns]
        n_raw += len(chunk)
        chunk = _numeric(chunk, ("quantity", "unit_price", "n_bidders",
                                 "size_in", "constr_share"))
        # Drop rows the parser itself flagged as misread, BEFORE anything can
        # show them as a comparable. See QUALITY_FLAG_COLUMNS.
        for c in QUALITY_FLAG_COLUMNS:
            if c in chunk.columns:
                bad = chunk[c].astype(str).str.strip().str.upper() == "Y"
                n_flagged += int(bad.sum())
                chunk = chunk[~bad]
        # Rows with no price cannot support a comparable.
        chunk = chunk[chunk["unit_price"].notna() & (chunk["unit_price"] > 0)]
        for c in ("source", "owner", "item_family", "unit",
                  "competition_class", "project_kind"):
            if c in chunk.columns:
                chunk[c] = chunk[c].astype("category")
        parts.append(chunk)
    df = pd.concat(parts, ignore_index=True)
    for c in ("source", "owner", "item_family", "unit",
              "competition_class", "project_kind"):
        if c in df.columns:
            df[c] = df[c].astype(str).astype("category")
    os.makedirs(CACHE_DIR, exist_ok=True)
    tmp = dest + ".tmp"
    df.to_parquet(tmp, index=False)
    os.replace(tmp, dest)
    # No prune here: panel() prunes on every resolve, including immediately
    # before calling this. The call that used to sit on this line referenced a
    # `token` that is not in this scope, so the FIRST run after any ETL
    # rewrite -- the only run that reaches the rebuild path -- died with a
    # NameError. Covered by DataContract.test_panel_cache_rebuild_path_runs.
    _panel_build_stats.update({"rows_read": n_raw, "rows_quality_flagged": n_flagged,
                              "rows_kept": int(len(df))})
    log.info("panel: %d rows read, %d dropped as misread (ext_mismatch / "
             "unit_suspect), %d kept", n_raw, n_flagged, len(df))
    return df


def _prune_stale(keep_token: str) -> None:
    """Drop every cache that is not the one the current code would build.

    Keeping "anything containing the panel token" stopped being enough once
    the derived caches were also keyed on how they were derived: every edit
    to this module left the previous derivation's Parquet behind, still
    carrying the current panel token, so nothing ever removed it. And
    `vintage_year_` was never on the prune list at all, so those survived
    every ETL run. Now the keep-set is the exact current file names.
    """
    if not os.path.isdir(CACHE_DIR):
        return
    keep = {os.path.basename(_panel_cache_path(keep_token))}
    keep |= {os.path.basename(_derived_cache_path(n, keep_token))
             for n in DERIVED_CACHES}
    for f in os.listdir(CACHE_DIR):
        if not f.endswith(".parquet") or f in keep:
            continue
        if f.startswith(("master_panel_",) + tuple(n + "_" for n in DERIVED_CACHES)):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
            except OSError:
                pass


#: Every derived table cached under CACHE_DIR, so pruning knows its names.
DERIVED_CACHES = ("lump_sum_share", "quantity_coverage", "vintage_year")


def panel() -> pd.DataFrame:
    """The full observation panel, used for comparables and coverage checks."""
    with _lock:
        if "panel" in _cache:
            return _cache["panel"]
        src = _path(MASTER_PANEL_FILE)
        if not os.path.exists(src):
            raise DataContractError(
                "%s not found under %s." % (MASTER_PANEL_FILE, CLEAN_DIR))
        token = _cache_token(src)
        dest = _panel_cache_path(token)
        # Prune on every resolve, not only on rebuild: a cache built in an
        # earlier process leaves its predecessors behind otherwise.
        _prune_stale(token)
        if os.path.exists(dest):
            try:
                df = pd.read_parquet(dest)
            except Exception:
                log.warning("panel cache unreadable, rebuilding")
                df = _build_panel_cache(src, dest)
        else:
            log.info("building master_panel Parquet cache (first run, ~30 s)")
            df = _build_panel_cache(src, dest)
        if "desc_norm" not in df.columns:
            df["desc_norm"] = ""
        _load_report["panel"] = {
            "file": MASTER_PANEL_FILE, "rows": int(len(df)),
            "cache": os.path.basename(dest),
            "mtime": os.path.getmtime(src),
            "quality_filtered": _panel_build_stats.get("rows_quality_flagged"),
            "has_quality_flags": any(c in df.columns
                                     for c in QUALITY_FLAG_COLUMNS),
        }
        _cache["panel"] = df
        return df


# ---------------------------------------------------------------------------
# Derived aggregates
# ---------------------------------------------------------------------------

def _derivation_token() -> str:
    """Fingerprint of everything on THIS side that shapes a derived table.

    The cache key was the source CSV's mtime+size alone, which notices an ETL
    rewrite but nothing here. Narrowing the price pool therefore left every
    derived statistic -- lump-sum shares, quantity coverage, vintage years --
    served from Parquet built against the OLD pool, silently. The shares still
    carried the engineer-estimate and unbalanced-bid rows they were supposed
    to have lost, and nothing said so.
    """
    import hashlib
    # The derivation CODE lives in this file, and editing it is the change
    # most likely to go unnoticed: excluding the `needs_review` sentinel from
    # the lump-sum shares changed neither the columns nor the pool, so the
    # old shares -- one family at exactly 100% of the bid -- kept being served
    # from Parquet. Hashing this module's source makes any edit here rebuild
    # the derived tables (seconds); the 165 MB panel cache is keyed separately
    # and is untouched by it.
    try:
        with open(os.path.abspath(__file__), "rb") as fh:
            src = hashlib.sha1(fh.read()).hexdigest()[:8]
    except OSError:
        src = "nosrc"
    parts = [",".join(MASTER_PANEL_REQUIRED + MASTER_PANEL_OPTIONAL),
             ";".join("%s=%s" % (c, r) for c, r in POOL_PREDICATES), src]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def _derived_cache_path(name: str, token: str) -> str:
    return os.path.join(CACHE_DIR, "%s_%s_%s.parquet"
                        % (name, token, _derivation_token()))


def vintage_year() -> pd.DataFrame:
    """The year each (item_family, unit) median is effectively "as of".

    A median pooled over several years of lettings sits at the middle of its
    own sample, not at today. Escalating it to a future bid date needs that
    midpoint, and no reference table carries one -- so it is taken from the
    panel the medians were built from. Dollar-weighted, because a median
    dominated by one large recent letting is nearer that year than a plain
    count would suggest.

    Returns item_family, unit, n_obs, year_median, year_weighted, year_max.
    """
    with _lock:
        if "vintage_year" in _cache:
            return _cache["vintage_year"]
        token = _cache_token(_path(MASTER_PANEL_FILE))
        dest = _derived_cache_path("vintage_year", token)
        if os.path.exists(dest):
            try:
                df = pd.read_parquet(dest)
                _cache["vintage_year"] = df
                return df
            except Exception:  # noqa: BLE001
                pass

        p = price_pool()
        cols = {"item_family", "unit", "unit_price", "quantity"}
        if not cols.issubset(p.columns) or "year" not in p.columns:
            df = pd.DataFrame(columns=["item_family", "unit", "n_obs",
                                       "year_median", "year_weighted",
                                       "year_max"])
            _cache["vintage_year"] = df
            return df

        d = p[["item_family", "unit", "year", "unit_price", "quantity",
               "competition_class", "project_kind"]].copy()
        if "project_kind" in d.columns:
            d = d[d["project_kind"].astype(str) == "construction"]
        if "competition_class" in d.columns:
            d = d[d["competition_class"].astype(str) == "competitive"]
        # `year` is pinned to str upstream (the float-inference trap), so it
        # has to be coerced back here rather than assumed numeric.
        d["_y"] = pd.to_numeric(d["year"], errors="coerce")
        d = d[d["_y"].between(1990, 2100)]
        d["_w"] = (pd.to_numeric(d["quantity"], errors="coerce").fillna(1.0)
                   * pd.to_numeric(d["unit_price"], errors="coerce")).clip(lower=0)
        d = d[d["_w"].notna()]
        g = d.groupby(["item_family", "unit"], observed=True)
        out = g.agg(n_obs=("_y", "size"), year_median=("_y", "median"),
                    year_max=("_y", "max")).reset_index()
        wsum = g.apply(lambda x: (
            (x["_y"] * x["_w"]).sum() / x["_w"].sum()
            if x["_w"].sum() > 0 else x["_y"].median()),
            include_groups=False).rename("year_weighted").reset_index()
        out = out.merge(wsum, on=["item_family", "unit"], how="left")
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = dest + ".tmp"
        out.to_parquet(tmp, index=False)
        os.replace(tmp, dest)
        _cache["vintage_year"] = out
        return out


#: The one definition of "a price that may inform an estimate".
#:
#: Hand-copied from `comp` in src/build_master.py into one consumer, which is
#: how it drifted: the comparables filter had item_family / unit /
#: competition_class / project_kind and nothing else, so the evidence shown
#: under a price could include the project's own engineer estimate and the
#: client's own test-side files. The client's rule has been stated three
#: times -- OPCC is never an input nor a display basis, and company files are
#: test-only -- and the panel still carries 44,333 engineer-estimate rows and
#: 1,269 unbalanced-bid rows for other purposes.
#:
#: Everything that derives a statistic or shows evidence goes through here.
POOL_PREDICATES = (
    ("is_engineer_estimate", "not_true"),
    ("source_set", "eq:external"),
    ("competition_class", "eq:competitive"),
    ("project_kind", "eq:construction"),
    ("price_basis", "ne:uneven_proposal"),
)


def price_pool() -> pd.DataFrame:
    """The panel, reduced to rows that may inform or justify a price."""
    with _lock:
        if "price_pool" in _cache:
            return _cache["price_pool"]
        p = panel()
        m = pd.Series(True, index=p.index)
        applied, skipped = [], []
        for col, rule in POOL_PREDICATES:
            if col not in p.columns:
                skipped.append(col)
                continue
            vals = p[col].astype(str).str.strip().str.lower()
            if rule == "not_true":
                m &= ~vals.isin(("true", "1", "yes"))
            elif rule.startswith("eq:"):
                m &= vals == rule[3:]
            elif rule.startswith("ne:"):
                m &= vals != rule[3:]
            applied.append(col)
        out = p[m]
        _load_report["price_pool"] = {
            "rows": int(len(out)), "rows_in_panel": int(len(p)),
            "dropped": int(len(p) - len(out)),
            "predicates_applied": applied,
            "predicates_skipped": skipped,
        }
        if skipped:
            log.warning("price_pool: %s absent from the panel, so that "
                        "filter did not run", ", ".join(skipped))
        _cache["price_pool"] = out
        return out


def lump_sum_share() -> pd.DataFrame:
    """Overhead families as a share of each bidder's own total, from the panel.

    Mobilization, traffic control, bonds and the like are bid as a lump sum.
    Their unit price is a project total masquerading as a rate -- mobilization
    LS runs p25 $16.6k / median $86.5k / p75 $300k in price_reference, an IQR
    ratio of 21, which is useless as a rate. What IS stable is the share of
    the bid those items take, which is how San Antonio's cost manual handles
    mobilization (fixed % of construction cost rather than a unit price).

    Returns one row per item_family with the observed share distribution.
    """
    with _lock:
        if "lump_sum_share" in _cache:
            return _cache["lump_sum_share"]
        token = _cache_token(_path(MASTER_PANEL_FILE))
        dest = _derived_cache_path("lump_sum_share", token)
        if os.path.exists(dest):
            try:
                df = pd.read_parquet(dest)
                _cache["lump_sum_share"] = df
                return df
            except Exception:
                pass

        p = price_pool()
        need = {"project_key", "bidder", "unit_price", "quantity", "item_family"}
        if not need.issubset(p.columns):
            df = pd.DataFrame(columns=["item_family", "n_bids", "share_p25",
                                       "share_median", "share_p75"])
            _cache["lump_sum_share"] = df
            return df

        d = p[["project_key", "bidder", "item_family", "quantity",
               "unit_price", "project_kind"]].copy()
        if "project_kind" in d.columns:
            d = d[d["project_kind"].astype(str) == "construction"]
        d["extended"] = d["quantity"].fillna(1.0) * d["unit_price"]
        d = d[d["extended"].notna() & (d["extended"] > 0)]
        # Sentinel families are not kinds of work. `needs_review` came out at
        # a share_median of exactly 1.0 over 48 bids -- bids whose every line
        # is unclassified, so the "family" is trivially 100% of the bid. Fed
        # to the gross-up (T = D / (1 - sum s)) a share of 1.0 is a division
        # by zero wearing a percentage sign.
        d = d[~d["item_family"].astype(str).isin(
            ("needs_review", "other", "", "nan"))]
        # One bid = one (project, bidder) pair. Share is within that bid only.
        bid_total = d.groupby(["project_key", "bidder"], observed=True)["extended"].transform("sum")
        d["share"] = d["extended"] / bid_total
        d = d[(bid_total > 0) & d["share"].between(0, 1)]
        fam = (d.groupby(["project_key", "bidder", "item_family"], observed=True)["share"]
                 .sum().reset_index())
        out = (fam.groupby("item_family", observed=True)["share"]
                  .agg(n_bids="size",
                       share_p25=lambda s: s.quantile(0.25),
                       share_median="median",
                       share_p75=lambda s: s.quantile(0.75))
                  .reset_index())
        out = out[out["n_bids"] >= 20].reset_index(drop=True)
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = dest + ".tmp"
        out.to_parquet(tmp, index=False)
        os.replace(tmp, dest)
        _cache["lump_sum_share"] = out
        return out


def quantity_coverage() -> pd.DataFrame:
    """Observed quantity range per (item_family, unit) -- drives extrapolation warnings."""
    with _lock:
        if "quantity_coverage" in _cache:
            return _cache["quantity_coverage"]
        token = _cache_token(_path(MASTER_PANEL_FILE))
        dest = _derived_cache_path("quantity_coverage", token)
        if os.path.exists(dest):
            try:
                df = pd.read_parquet(dest)
                _cache["quantity_coverage"] = df
                return df
            except Exception:
                pass
        p = price_pool()
        d = p[["item_family", "unit", "quantity"]].copy()
        d["quantity"] = pd.to_numeric(d["quantity"], errors="coerce")
        d = d[d["quantity"].notna() & (d["quantity"] > 0)]
        out = (d.groupby(["item_family", "unit"], observed=True)["quantity"]
                 .agg(n_obs="size",
                      qty_min="min",
                      qty_p05=lambda s: s.quantile(0.05),
                      qty_median="median",
                      qty_p95=lambda s: s.quantile(0.95),
                      qty_max="max")
                 .reset_index())
        os.makedirs(CACHE_DIR, exist_ok=True)
        tmp = dest + ".tmp"
        out.to_parquet(tmp, index=False)
        os.replace(tmp, dest)
        _cache["quantity_coverage"] = out
        return out


# ---------------------------------------------------------------------------
# Small lookups the UI needs for dropdowns
# ---------------------------------------------------------------------------

def owners() -> List[str]:
    try:
        meta = table("project_metadata")
    except DataContractError:
        return []
    vals = meta["owner"].dropna().astype(str)
    counts = vals.value_counts()
    return [o for o in counts.index.tolist() if o and o.lower() != "nan"]


def work_types() -> List[str]:
    try:
        meta = table("project_metadata")
    except DataContractError:
        return []
    if "work_type" not in meta.columns:
        return []
    counts = meta["work_type"].dropna().astype(str).value_counts()
    return [w for w in counts.index.tolist() if w and w.lower() != "nan"]


def item_families() -> List[str]:
    fams = set(table("price_reference")["item_family"].dropna().astype(str))
    return sorted(f for f in fams if f)


def load_report() -> Dict[str, dict]:
    """What actually got loaded -- surfaced in the UI's data-status panel."""
    return dict(_load_report)


def warm(progress=None) -> Dict[str, dict]:
    """Load everything up front so the first request is not the slow one."""
    steps = list(TABLES) + ["panel", "lump_sum_share", "quantity_coverage"]
    for i, name in enumerate(steps):
        if progress:
            progress(i, len(steps), name)
        try:
            if name == "panel":
                panel()
            elif name == "lump_sum_share":
                lump_sum_share()
            elif name == "quantity_coverage":
                quantity_coverage()
            else:
                table(name)
        except DataContractError as e:
            _load_report[name] = {"error": str(e)}
            log.error("%s", e)
        except Exception as e:  # noqa: BLE001 - report, never crash startup
            _load_report[name] = {"error": "%s: %s" % (type(e).__name__, e)}
            log.exception("failed loading %s", name)
    return load_report()


def invalidate() -> None:
    with _lock:
        _cache.clear()
        _load_report.clear()
