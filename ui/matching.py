"""Match a free-text takeoff line onto something priceable.

The user types "8\" PVC SDR-26 Sanitary Sewer" and the price tables are keyed
on either `item_vocabulary.desc_norm` (city bid tabs), `price_book_txdot.item_no`
(TxDOT), or `price_reference.(item_family, unit, size_bucket)`. This module
produces a ranked list of candidates with an explicit method and score, so the
UI can show WHY a line matched and let the estimator override it.

Scoring is deliberately boring and inspectable -- IDF-weighted token overlap
plus a size-in-inches agreement bonus and a unit agreement bonus. No embedding
model, because an estimator has to be able to look at a match and tell whether
it is right.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import data_access as da
import upstream

# Tokens that carry no discriminating information in a bid item description.
STOP = {
    "and", "or", "the", "of", "for", "with", "per", "to", "in", "a", "an",
    "all", "any", "as", "at", "by", "on", "complete", "place", "including",
    "incl", "furnish", "install", "installed", "new", "item", "no", "each",
    "shall", "be", "type", "size", "inch", "inches", "ft", "feet",
}

# Matching below this score is reported but flagged as unreliable.
WEAK_MATCH = 0.34
GOOD_MATCH = 0.60

# Words that change WHAT the work is, not just how it is described. Removing
# and replacing concrete pavement costs far more than placing new pavement,
# and a "temporary" or "relocated" article prices nothing like the permanent
# one. Token overlap alone treats these as near-identical, so a candidate that
# disagrees with the query on any of them is pushed down hard.
SCOPE_MODIFIERS = {
    "remove": {"remove", "removal", "removing", "demo", "demolition",
               "demolish", "demolishing"},
    "replace": {"replace", "replacement", "replacing", "rr"},
    "relocate": {"relocate", "relocation", "reset", "resetting", "adjust",
                 "adjusting"},
    "abandon": {"abandon", "abandonment", "plug", "plugging"},
    "temporary": {"temporary", "temp", "interim"},
    "repair": {"repair", "repairing", "rehab", "rehabilitation", "rehabilitate"},
    "salvage": {"salvage", "salvaged"},
}

# What a scope-modifier disagreement costs. 0.45 is enough to drop a
# textually near-perfect "R&R reinforced concrete pavement" below a plainer
# match for "6 inch reinforced concrete pavement", without discarding it --
# it still shows in the picker, where it may be what the estimator meant.
SCOPE_MISMATCH_PENALTY = 0.45

# The family the upstream classifier assigns when no rule fires. It is a
# "don't know", not a kind of work, so it must never price a line on its own.
UNCLASSIFIED_FAMILY = "other"

# Evidence weighting. The vocabulary contains items backed by a single
# project; without this, one of those wins on exact wording over a family
# aggregate backed by 657. `usable` in the reference data draws the line at
# >=5 projects (yes), 3-4 (thin), <3 (no), so the same thresholds are used.
def _evidence_multiplier(n_projects: float) -> float:
    if n_projects >= 20:
        return 1.00
    if n_projects >= 5:
        return 0.93
    if n_projects >= 3:
        return 0.78
    if n_projects >= 2:
        return 0.50
    # One project is one bid tab. A near-perfect text match against one used
    # to score 0.475 and beat the 6-project family aggregate for the same
    # family and unit at 0.387, so a real riprap line was priced at $262/SY
    # off a single observation instead of $132/SY off six. The row stays
    # available in the match picker and in the comparables; it just cannot
    # win the match on its own.
    return 0.28


@dataclass
class Candidate:
    """One priceable thing a takeoff line could be."""
    key: str                  # opaque id the pricing layer resolves
    kind: str                 # vocabulary | txdot | family
    label: str                # what to show the estimator
    unit: str
    item_family: str
    score: float
    method: str
    n_projects: float
    median_price: Optional[float] = None
    detail: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(float(self.score), 3)
        return d


# ---------------------------------------------------------------------------
# Index construction (built once, cached)
# ---------------------------------------------------------------------------

def _tokens(s: str) -> List[str]:
    return [t for t in re.split(r"[^a-z0-9]+", (s or "").lower())
            if t and t not in STOP and len(t) > 1]


class _Index:
    def __init__(self) -> None:
        vocab = da.table("vocabulary")
        self.vocab = vocab.reset_index(drop=True)
        self.vocab_tokens = [set(_tokens(d)) for d in
                             self.vocab["desc_norm"].astype(str)]

        txdot = da.table("txdot_price_book")
        # usable == "no" means fewer than 3 supporting lettings, and
        # "unclassified" means the row is an unrecognised-item catch-all.
        # Neither belongs in the candidate pool: keeping them out beats
        # offering them and then disclaiming them.
        txdot = txdot[~txdot["usable"].astype(str).str.lower()
                      .isin(("no", "unclassified"))]
        self.txdot = txdot.reset_index(drop=True)
        txdot_text = self.txdot["item_no"].astype(str)
        if "description" in self.txdot.columns:
            txdot_text = txdot_text + " " + self.txdot["description"].fillna("").astype(str)
        self.txdot_tokens = [set(_tokens(t)) for t in txdot_text]

        pref = da.table("price_reference")
        self.price_ref = pref.reset_index(drop=True)

        # IDF over the union of both corpora.
        df: Dict[str, int] = {}
        for ts in self.vocab_tokens + self.txdot_tokens:
            for t in ts:
                df[t] = df.get(t, 0) + 1
        n_docs = max(1, len(self.vocab_tokens) + len(self.txdot_tokens))
        self.idf = {t: math.log(1.0 + n_docs / c) for t, c in df.items()}
        self._default_idf = math.log(1.0 + n_docs)

        # size_in per vocabulary row, for the "8 inch" vs "24 inch" distinction
        # that token overlap alone gets wrong.
        if "size_in" in self.vocab.columns:
            self.vocab_size = pd.to_numeric(self.vocab["size_in"],
                                            errors="coerce").to_numpy()
        else:
            self.vocab_size = np.full(len(self.vocab), np.nan)
        self.vocab_units = self.vocab["unit"].astype(str).str.upper().to_numpy()
        self.txdot_units = self.txdot["unit"].astype(str).str.upper().to_numpy()

        # TxDOT carries its size inside the description -- CONC PVMT (CONT
        # REINF - CRCP) (8"), RC PIPE (CL III) (24 IN). Without pulling that
        # out, a request for 6" concrete pavement matches the 8", 10" and 12"
        # items equally well on words alone, and thickness is the single
        # biggest price driver in the family.
        self.txdot_size = np.array(
            [upstream.extract_size_in(upstream.norm_desc(t)) or np.nan
             for t in txdot_text], dtype=float)

        # Postings (token -> rows that contain it). A row that shares no
        # token with the query scores 0 and is skipped anyway, so scoring
        # only the rows in the query's postings gives exactly the same
        # candidates -- about 10x faster, which lets every takeoff line be
        # checked for a weak match when it is classified.
        self.vocab_post = self._postings(self.vocab_tokens)
        self.txdot_post = self._postings(self.txdot_tokens)
        # evidence depth per row as plain floats (the same `float(x or 0)`
        # the scorer applied to each row before)
        self.vocab_nproj = [float(x or 0) for x in
                            self.vocab["n_projects"].tolist()] \
            if "n_projects" in self.vocab.columns else [0.0] * len(self.vocab)
        self.txdot_nlet = [float(x or 0) for x in
                           self.txdot["n_lettings"].tolist()] \
            if "n_lettings" in self.txdot.columns else [0.0] * len(self.txdot)

    @staticmethod
    def _postings(token_sets) -> Dict[str, List[int]]:
        post: Dict[str, List[int]] = {}
        for i, ts in enumerate(token_sets):
            for t in ts:
                post.setdefault(t, []).append(i)
        return post

    @staticmethod
    def rows_for(post: Dict[str, List[int]], q_tokens: set) -> List[int]:
        """Rows sharing at least one token with the query, in row order."""
        rows = set()
        for t in q_tokens:
            rows.update(post.get(t, ()))
        return sorted(rows)

    def weight(self, t: str) -> float:
        return self.idf.get(t, self._default_idf)


_index: Optional[_Index] = None


def index() -> _Index:
    global _index
    if _index is None:
        _index = _Index()
    return _index


def reset() -> None:
    global _index
    _index = None
    _score_cached.cache_clear()


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _overlap(query: set, cand: set, idx: _Index) -> float:
    """IDF-weighted containment, normalised by the query's own weight.

    Containment rather than Jaccard: a bid item description is often longer
    than what the estimator typed ("8 in pvc sanitary sewer pipe sdr 26 all
    depths" vs "8\" PVC sewer"), and Jaccard punishes that correct match for
    the extra words.
    """
    if not query or not cand:
        return 0.0
    qw = sum(idx.weight(t) for t in query)
    if qw <= 0:
        return 0.0
    shared = sum(idx.weight(t) for t in query & cand)
    cover = shared / qw
    # A candidate stuffed with unrelated words is a worse match than a tight
    # one, so damp by how much of the candidate the query explains.
    cw = sum(idx.weight(t) for t in cand)
    tight = (shared / cw) if cw > 0 else 0.0
    return 0.78 * cover + 0.22 * tight


#: Share of the query's IDF weight a token must carry to be worth reporting
#: as missing. ASSUMPTION.
MISSING_TERM_SHARE = 0.20


def missing_terms(description: str, candidate_label: str, limit: int = 3):
    """Distinctive words in the query that the matched item never mentions.

    Containment scoring rewards a candidate for the words it shares and says
    nothing about the one it drops, so a description can lose its subject and
    still score well on the qualifiers around it. Two real ones:

      "Concrete Headwall, 24 inch RCP" -> "Connect to Existing 24-inch RCP",
      a pipe connection rather than a cast structure, at 0.726 -- beating
      genuine headwall rows backed by 62 projects at 0.724.

      "Articulated Concrete Block Mat" -> "BLOCK SODDING", grass instead of
      concrete block revetment, carried by "block" and "concrete" while
      "articulated" was simply absent.

    This was first tried as a score penalty and backed out: on IDF alone a
    subject ("articulated", lead 1.46) cannot be told from a qualifier
    ("grouted", lead 1.40), so any threshold that caught the bad matches also
    demoted good ones. As DISCLOSURE it needs no such distinction -- "the
    matched item does not mention 'grouted'" is worth knowing either way, and
    it moves no number.
    """
    q = set(_tokens(description or ""))
    c = set(_tokens(candidate_label or ""))
    if not q or not c:
        return []
    idx = index()
    total = sum(idx.weight(t) for t in q)
    if total <= 0:
        return []
    out = [(idx.weight(t), t) for t in q - c
           if idx.weight(t) / total >= MISSING_TERM_SHARE]
    out.sort(reverse=True)
    return [t for _, t in out[:limit]]


def _scope_adjust(score: float, q_tokens: set, c_tokens: set) -> float:
    """Penalise disagreement on scope-changing words (remove/replace/temp/...)."""
    for group in SCOPE_MODIFIERS.values():
        in_q = bool(q_tokens & group)
        in_c = bool(c_tokens & group)
        if in_q != in_c:
            score *= SCOPE_MISMATCH_PENALTY
    return score


def _size_adjust(score: float, q_size: Optional[float],
                 c_size: Optional[float]) -> float:
    """Diameter is the single most price-relevant token in utility items."""
    if q_size is None or c_size is None or not np.isfinite(c_size):
        return score
    if abs(q_size - c_size) < 1e-6:
        return min(1.0, score + 0.16)
    ratio = max(q_size, c_size) / max(1e-6, min(q_size, c_size))
    if ratio >= 2.0:
        return score * 0.45      # 8" vs 24" is a different item, not a near miss
    return score * 0.78


@lru_cache(maxsize=4096)
def _score_cached(desc: str, unit: str, limit: int) -> tuple:
    return tuple(_score(desc, unit, limit))


def _score(desc: str, unit: str, limit: int) -> List[Candidate]:
    idx = index()
    dn = upstream.norm_desc(desc)
    q_tokens = set(_tokens(dn))
    q_size = upstream.extract_size_in(dn)
    q_unit = upstream.norm_unit(unit).upper() if unit else ""
    q_fam = upstream.item_family(dn)

    # Every row is scored first and only the rows that make the cut become
    # Candidate objects. The sort key (-score, source, row) is the order the
    # old stable sort over [vocabulary rows..., TxDOT rows...] produced.
    scored: List[tuple] = []

    # --- vocabulary (city bid tabs: the DFW-local evidence) ----------------
    v = idx.vocab
    for i in idx.rows_for(idx.vocab_post, q_tokens):
        s = _overlap(q_tokens, idx.vocab_tokens[i], idx)
        if s < 0.12:
            continue
        s = _size_adjust(s, q_size, idx.vocab_size[i])
        s = _scope_adjust(s, q_tokens, idx.vocab_tokens[i])
        cu = idx.vocab_units[i]
        if q_unit:
            if cu == q_unit:
                s = min(1.0, s + 0.10)
            else:
                # A different unit usually means a different pay item, but the
                # user's unit may also just be the typo. Penalise, don't drop.
                s *= 0.62
        # Thin evidence must not outrank a well-supported near-equal match.
        s *= _evidence_multiplier(idx.vocab_nproj[i])
        sc = min(1.0, s)
        scored.append((-sc, 0, i, sc))

    # --- TxDOT price book --------------------------------------------------
    t = idx.txdot
    for i in idx.rows_for(idx.txdot_post, q_tokens):
        s = _overlap(q_tokens, idx.txdot_tokens[i], idx)
        if s < 0.12:
            continue
        s = _size_adjust(s, q_size, idx.txdot_size[i])
        s = _scope_adjust(s, q_tokens, idx.txdot_tokens[i])
        cu = idx.txdot_units[i]
        if q_unit:
            s = min(1.0, s + 0.10) if cu == q_unit else s * 0.62
        # TxDOT is statewide highway work; for a DFW municipal channel job it
        # is weaker evidence than the city tabs, so it starts slightly behind.
        s *= 0.92
        s *= _evidence_multiplier(idx.txdot_nlet[i])
        sc = min(1.0, s)
        scored.append((-sc, 1, i, sc))

    scored.sort(key=lambda x: x[:3])
    out: List[Candidate] = []
    for _, src, i, sc in scored[:limit]:
        if src == 0:
            row = v.iloc[i]
            cu = idx.vocab_units[i]
            n_proj = float(row.get("n_projects") or 0)
            out.append(Candidate(
                key="vocab::%d" % i,
                kind="vocabulary",
                label=str(row.get("example_description") or row.get("desc_norm") or ""),
                unit=cu,
                item_family=str(row.get("item_family") or ""),
                score=sc,
                method="desc_overlap",
                n_projects=n_proj,
                median_price=_f(row.get("pooled_median_price")),
                detail="%d projects, %s" % (int(n_proj), cu),
            ))
        else:
            row = t.iloc[i]
            cu = idx.txdot_units[i]
            n_let = float(row.get("n_lettings") or 0)
            out.append(Candidate(
                key="txdot::%d" % i,
                kind="txdot",
                label=str(row.get("description") or row.get("item_no") or ""),
                unit=cu,
                item_family="",
                score=sc,
                method="desc_overlap",
                n_projects=n_let,
                median_price=_f(row.get("price_median")),
                detail="%d TxDOT lettings, %s" % (int(n_let), cu),
            ))

    # --- family fallback ---------------------------------------------------
    # Always offered, even when a description match scored well: it is the
    # broadest, best-supported aggregate and sometimes the honest answer.
    fam_cand = _family_candidate(q_fam, q_unit, dn)
    if fam_cand is not None:
        out.append(fam_cand)
        out.sort(key=lambda c: -c.score)
    return out


def _family_candidate(fam: str, unit: str, dn: str) -> Optional[Candidate]:
    pref = index().price_ref
    sub = pref[pref["item_family"].astype(str) == fam]
    if unit:
        exact = sub[sub["unit"].astype(str).str.upper() == unit]
        if len(exact):
            sub = exact
    if not len(sub):
        return None
    # Prefer the row with the most projects behind it; among equals, a usable one.
    rank = {"yes": 0, "thin": 1, "no": 2, "unclassified": 4}
    sub = sub.assign(_r=sub["usable"].astype(str).str.lower().map(rank).fillna(3))
    sub = sub.sort_values(["_r", "n_projects"], ascending=[True, False])
    row = sub.iloc[0]
    n_proj = float(row.get("n_projects") or 0)
    # The family aggregate pools genuinely different items, so it starts low --
    # but it rises with evidence depth, because `traffic_control / MO` across
    # 657 projects is a better answer than a textually perfect vocabulary row
    # backed by one. Tops out at 0.58, below a solid specific match.
    usable = str(row.get("usable")).lower()
    base = 0.28 if usable == "yes" else (0.20 if usable == "thin" else 0.12)
    depth = min(0.30, 0.055 * math.log1p(max(0.0, n_proj))) if usable == "yes" else 0.0
    score = base + depth
    if usable == "unclassified":
        # Set by the ETL on the `other` catch-all. Project count is high there
        # and would otherwise buy depth credit, so the flag overrides both.
        base, depth, score = 0.05, 0.0, 0.05

    # `other` is not a family, it is "the classifier did not recognise this".
    # It pools 855 projects of unrelated EA items, so its average prices
    # nothing. Left at a normal score it quietly becomes the winning match for
    # every description the rules do not cover -- including outright
    # nonsense -- and the tool returns a confident number for an item it never
    # identified. Scored below the weak-match threshold so it can never win
    # silently, and refused outright by the pricing layer unless the estimator
    # picks it deliberately.
    if fam == UNCLASSIFIED_FAMILY:
        score = 0.05
    unclassified = fam == UNCLASSIFIED_FAMILY or usable == "unclassified"

    i = int(sub.index[0])
    return Candidate(
        key="family::%d" % i,
        kind="family",
        label=("unclassified (%s) -- no item family matched"
               % row.get("unit")) if unclassified
              else "%s (%s) -- family average" % (fam, row.get("unit")),
        unit=str(row.get("unit") or ""),
        item_family=fam,
        score=score,
        method="family_fallback",
        n_projects=n_proj,
        median_price=_f(row.get("price_median")),
        detail="%d projects pooled across the %s family" % (int(n_proj), fam),
    )


def _f(v) -> Optional[float]:
    try:
        f = float(v)
        return f if np.isfinite(f) else None
    except (TypeError, ValueError):
        return None


#: `usable='no'` in the reference files means fewer than 3 supporting
#: projects. Such a row may be the right answer and stays in the picker, but
#: it must not be the one the tool reaches for by itself.
MIN_USABLE_PROJECTS = 3


def _demote_below_floor(cands: List[Candidate]) -> List[Candidate]:
    """Keep a below-floor row out of first place while any usable one exists.

    Expressed as a ranking rule rather than more weight in
    `_evidence_multiplier`, because tuning a multiplier until the known
    examples come out right is how the thin-row problem kept coming back in a
    new disguise: the rule being enforced is a flat one ("not the automatic
    answer below 3 projects"), and a smooth weight can only approximate it.
    Only position 0 moves -- the rest stay in score order, so the picker still
    reads as a ranked list and the thin row sits right below, one click away.
    """
    if len(cands) < 2 or (cands[0].n_projects or 0) >= MIN_USABLE_PROJECTS:
        return cands
    for i in range(1, len(cands)):
        if (cands[i].n_projects or 0) >= MIN_USABLE_PROJECTS:
            cands.insert(0, cands.pop(i))
            break
    return cands


def candidates(description: str, unit: str = "", limit: int = 8) -> List[Candidate]:
    """Ranked priceable candidates for one takeoff line."""
    if not (description or "").strip():
        return []
    return _demote_below_floor(
        list(_score_cached(description.strip(), (unit or "").strip(), limit)))


def best(description: str, unit: str = "") -> Optional[Candidate]:
    c = candidates(description, unit, limit=8)
    return c[0] if c else None


def search(query: str, unit: str = "", limit: int = 25) -> List[Candidate]:
    """Free search used by the UI's 'change match' picker."""
    return candidates(query, unit, limit=limit)


def resolve(key: str) -> Optional[dict]:
    """Turn a candidate key back into the row that prices it."""
    idx = index()
    try:
        kind, pos = key.split("::", 1)
        pos = int(pos)
    except (ValueError, AttributeError):
        return None
    if kind == "vocab" and 0 <= pos < len(idx.vocab):
        return {"kind": "vocabulary", "row": idx.vocab.iloc[pos].to_dict()}
    if kind == "txdot" and 0 <= pos < len(idx.txdot):
        return {"kind": "txdot", "row": idx.txdot.iloc[pos].to_dict()}
    if kind == "family":
        if pos in idx.price_ref.index:
            return {"kind": "family", "row": idx.price_ref.loc[pos].to_dict()}
    return None
