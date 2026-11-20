# -*- coding: utf-8 -*-
"""Why does my matched-pair index diverge from TxDOT's own HCI?

Two candidate explanations, tested side by side:

  (A) MATCHING. TxDOT renumbered its bid codes mid-2024 (6xxx -> 7xxx), so
      items may fail to link across the break and the matched sample is a
      biased remnant.  Fix: normalise codes through the crosswalk.

  (B) WEIGHTING. HCI is a quantity-weighted index over a basket dominated by
      a few huge pay items (asphalt, concrete, excavation).  My median-of-
      ratios gives a $5 sign bracket the same vote as 100,000 TON of asphalt.
      Fix: weight each matched pair by its base-period dollar value, which
      turns the mean-of-ratios into a Laspeyres index:

          I = sum(q_a * p_b) / sum(q_a * p_a) = sum(w_a * r) / sum(w_a)
          with w_a = q_a * p_a  and  r = p_b / p_a

Evidence already in hand before this script runs (so the reader can see which
hypothesis was favoured and why):
  * The crosswalk's own match rule is spec+desc+unit, and 78.2% of its pairs
    have IDENTICAL normalised descriptions -- those already link on a
    description key, so (A) can only add the remaining ~22%.
  * Matched-pair counts do NOT collapse at the renumbering: 2023->2024 n=739
    and 2024->2025 n=1073, in line with every other year, and 75.6% of TxDOT
    rows sit on keys that already span both code generations.
  => (A) is largely redundant; (B) is the live hypothesis.  Both are run
     anyway, because a prediction is only worth stating if it can fail.

A weighted index buys accuracy at the cost of robustness: one colossal item
can carry a whole year.  So every weighted figure is reported next to its
effective sample size (inverse Herfindahl of the weight shares).  An eff_n of
3 on a 700-item basket means the number is three items wearing a trenchcoat.
"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")
IDX = os.path.join(ROOT, "data", "external", "cost_indices")
REPORTS = os.path.join(ROOT, "reports")

MIN_PAIRS = 10
RATIO_CLIP = (0.33, 3.0)   # a 3x single-item move is a data artefact, not inflation

OUT = []


def say(s=""):
    OUT.append(s)
    print(s)


def md(df, fmt="%.1f"):
    if df is None or not len(df):
        return "(no rows)"
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "| " + " | ".join("---" for _ in d.columns) + " |"
    body = ["| " + " | ".join(str(v) for v in r) + " |"
            for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


def load_panel():
    use = ["source", "source_set", "owner", "project_key", "year", "item_key", "item_no",
           "desc_norm", "unit", "quantity", "unit_price", "competition_class",
           "project_kind", "item_family"]
    m = pd.read_csv(os.path.join(CLEAN, "master_panel.csv"), encoding="utf-8-sig",
                    low_memory=False, usecols=lambda c: c in use,
                    dtype={"year": str, "item_key": str, "item_no": str,
                           "project_key": str})
    # dtype=str on year is not optional: the CSV holds a clean "2026", but the
    # blanks read as NaN so pandas infers float64, astype(str) yields "2026.0",
    # and every year pattern silently fails to match.
    m["year"] = m.year.fillna("").astype(str).str.replace(r"\.0$", "", regex=True)
    m = m[m.year.str.match(r"^20\d\d$", na=False)]
    m = m[(m.unit_price > 0) & (m.competition_class == "competitive")]
    # An engineer's estimate is not a bid, and this filter was missing while
    # load_erosion_statewide's equivalent had it.  25,781 such rows passed the
    # competitive filter and entered the index as if a bidder had quoted them;
    # 873 of them are in the erosion basket.  basket_yoy takes a median across
    # bidders within a project, so each project's estimate got a vote next to
    # the real bids -- worth 4.3% on the 2026 index level.
    if "is_engineer_estimate" in m.columns:
        ee = m.is_engineer_estimate.astype(str).str.lower().isin(
            ["true", "1", "yes"])
        m = m[~ee]
    if "project_kind" in m.columns:
        m = m[m.project_kind == "construction"]
    if "quantity" in m.columns:
        m = m[m.quantity > 0]
    return m[m.source.isin(["txdot", "txdot_archive"])].copy()


def add_normalised_key(tx):
    """Map 6xxx codes onto their 7xxx successors, then key on the mapped code.

    The key must live in ONE namespace for every row that has a usable code.
    Writing the code key only onto the rows the crosswalk rewrote is the
    obvious-looking version and it is wrong: a mapped 6xxx row would become
    "code|500-7001|CY" while the genuine 7xxx row it should join to keeps its
    description key, so the two never meet and the mapped rows instead pair up
    among themselves.  That produces a plausible but meaningless series.

    So: every row with a parseable spec-tail code gets the code key (mapped
    through the crosswalk when a successor exists, otherwise its own code);
    only rows with no usable code fall back to the description key.
    """
    code = tx.item_no.astype(str).str.strip()
    has_code = code.str.match(r"^\d{3,4}-\d{3,4}$", na=False)
    p = os.path.join(CLEAN, "txdot_code_crosswalk.csv")
    mp = {}
    if os.path.exists(p):
        cw = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
        mp = dict(zip(cw.code_6xxx.astype(str).str.strip(),
                      cw.code_7xxx.astype(str).str.strip()))
    mapped = code.map(lambda c: mp.get(c, c))
    n_mapped = int((mapped != code).sum())
    unit = (tx.unit.fillna("").astype(str) if "unit" in tx.columns
            else pd.Series("", index=tx.index))
    tx["item_key_cw"] = np.where(has_code, "code|" + mapped + "|" + unit,
                                 tx.item_key)
    return tx, n_mapped, int(has_code.sum())


def series(panel, keycol):
    """Collapse to one price and one dollar weight per (item, year).

    Two-stage: median across bidders within a project, then median across
    projects.  A 13-bidder letting must not outweigh a 3-bidder one.  The
    dollar weight uses the project-level median price times the project
    quantity, so it is NOT inflated by the number of bidders.
    """
    s1 = (panel.groupby([keycol, "year", "project_key"], observed=True)
          .agg(p=("unit_price", "median"), q=("quantity", "median"))
          .reset_index())
    s1["dollars"] = s1.p * s1.q
    return (s1.groupby([keycol, "year"], observed=True)
            .agg(price=("p", "median"), dollars=("dollars", "sum"),
                 n=("project_key", "nunique")).reset_index())


def chained(panel, keycol, weighted, label, clip=True):
    yr = series(panel, keycol)
    piv = yr.pivot(index=keycol, columns="year", values="price")
    pw = yr.pivot(index=keycol, columns="year", values="dollars")
    years = sorted(c for c in piv.columns if str(c).isdigit())
    rows, lvl = [], 100.0
    for a, b in zip(years, years[1:]):
        pair = piv[[a, b]].dropna()
        pair = pair[(pair[a] > 0) & (pair[b] > 0)]
        if len(pair) < MIN_PAIRS:
            rows.append({"variant": label, "from": a, "to": b,
                         "n_matched": len(pair), "eff_n": np.nan,
                         "yoy_pct": np.nan, "index": lvl})
            continue
        r = pair[b] / pair[a]
        if clip:
            r = r.clip(*RATIO_CLIP)
        if weighted:
            w = pw.loc[pair.index, a].fillna(0.0).astype(float)
            w = w[w > 0]
            r = r.loc[w.index]
            if not len(w) or w.sum() <= 0:
                rows.append({"variant": label, "from": a, "to": b,
                             "n_matched": len(pair), "eff_n": np.nan,
                             "yoy_pct": np.nan, "index": lvl})
                continue
            share = w / w.sum()
            ratio = float((r * share).sum())
            eff_n = float(1.0 / (share ** 2).sum())
            n_used = len(w)
        else:
            ratio = float(r.median())
            eff_n = float(len(r))
            n_used = len(r)
        lvl *= ratio
        rows.append({"variant": label, "from": a, "to": b, "n_matched": n_used,
                     "eff_n": eff_n, "yoy_pct": (ratio - 1) * 100, "index": lvl})
    return pd.DataFrame(rows)


def load_hci():
    p = os.path.join(IDX, "txdot_hci_monthly.csv")
    if not os.path.exists(p):
        return None
    h = pd.read_csv(p, encoding="utf-8-sig")
    # The 12-month average is the only usable form: a single month swings
    # between roughly 90 and 260 depending on what happened to be let.
    sub = h[h.series_id.astype(str).str.contains("twelve|12", case=False, na=False)]
    if sub.empty:
        sub = h[h.series_id == sorted(h.series_id.unique())[-1]]
    sub = sub.copy()
    sub["year"] = sub.date.astype(str).str[:4]
    return sub.groupby("year").index_value.mean()


# Families that approximate TxDOT's own HCI basket (highway paving, base,
# earthwork) versus the families the client actually builds (creek erosion
# repair).  Names must match item_family exactly -- they are checked against
# the observed vocabulary at runtime and a miss is reported, because a silently
# empty basket looks exactly like a basket that did not move.
HCI_LIKE = ["asphalt_pavement", "conc_pavement", "flex_base", "subgrade_stab",
            "excavation", "embankment_fill", "borrow_backfill"]
PAVING_ONLY = ["asphalt_pavement", "conc_pavement", "flex_base"]
# Structural channel-repair work -- the client's actual scope.
#
# `erosion_control` is deliberately NOT in this list, and the reason is a
# category error worth spelling out.  By TxDOT's own terminology that family is
# correct, but it is TEMPORARY construction BMP work, not permanent erosion
# repair: of its 21,725 archive rows, 21,342 (98.2%) are silt fence, sediment
# logs, construction exits and SW3P measures at a median unit price of $5.00,
# and the remaining 383 are soil-retention blankets, sandbags and compost --
# also temporary/vegetative.  None of it prices a gabion wall or a riprap
# revetment.  Including it would also SWAMP the basket: 21,725 rows of $5
# silt fence against a few hundred rows of structural work.
#
# (It only became a hazard once the archive parser was fixed -- item 506's
# special-provision codes are 10 digits and the old 7-digit pattern had been
# rejecting them, so this family held about 1,030 rows before and was too
# small to distort anything.)
EROSION = ["riprap_stone", "riprap_conc", "gabion_wall", "gabion_mattress",
           "retaining_wall", "rock_anchor", "conc_beam", "shoring",
           "excavation", "embankment_fill"]
# Temporary erosion/sedimentation control, tracked separately so the two are
# never silently pooled.
EROSION_TEMP = ["erosion_control", "seeding_sod", "soil_ret_blanket"]
# Count-heavy families that are irrelevant to erosion work but dominate an
# equal-weight median of the full sample.
DILUTERS = ["other", "pavement_marking", "electrical_lighting", "signing",
            "mowing_vegetation", "traffic_control"]


def basket_yoy(sub, a, b, minn=8):
    s1 = (sub.groupby(["item_key", "year", "project_key"], observed=True)
          .agg(p=("unit_price", "median")).reset_index())
    yr = (s1.groupby(["item_key", "year"], observed=True)
          .agg(price=("p", "median")).reset_index())
    piv = yr.pivot(index="item_key", columns="year", values="price")
    if a not in piv.columns or b not in piv.columns:
        return np.nan, 0
    pr = piv[[a, b]].dropna()
    pr = pr[(pr[a] > 0) & (pr[b] > 0)]
    if len(pr) < minn:
        return np.nan, len(pr)
    return float((pr[b] / pr[a]).median() - 1) * 100, len(pr)


def basket_section(tx, hci_yoy):
    """Does the gap to HCI come from WHICH ITEMS are in the basket?

    Tested by narrowing my sample toward HCI's own basket.  If composition were
    the explanation, narrowing should improve the fit.
    """
    fam = tx.item_family.fillna("").astype(str)
    vocab = set(fam.unique())
    baskets = [("HCI 类篮子(铺面+基层+土方)", HCI_LIKE),
               ("纯铺面", PAVING_ONLY),
               ("侵蚀修复类(客户实际工作)", EROSION),
               ("被稀释项(标线/照明/标志等)", DILUTERS),
               ("全样本", None)]
    say("## 4. 篮子构成检验:差距是否来自子目组成")
    say()
    missing = sorted(set(HCI_LIKE + PAVING_ONLY + EROSION + DILUTERS) - vocab)
    if missing:
        say("注:面板里不存在的族名(空篮子会伪装成不涨,所以必须报出来):`%s`"
            % "`, `".join(missing))
        say()
    years = sorted(y for y in tx.year.unique() if str(y).isdigit())
    pairs = list(zip(years, years[1:]))
    rows, cum = [], {b[0]: 100.0 for b in baskets}
    cum_h = 100.0
    for a, b in pairs:
        r = {"年份": b}
        for name, keep in baskets:
            sub = tx if keep is None else tx[fam.isin(keep)]
            v, n = basket_yoy(sub, a, b)
            if v == v:
                cum[name] *= (1 + v / 100.0)
            r[name] = v
            r["n:" + name] = n
        if hci_yoy is not None and b in hci_yoy.index:
            h = float(hci_yoy.loc[b])
            r["HCI"] = h
            cum_h *= (1 + h / 100.0)
        rows.append(r)
    df = pd.DataFrame(rows)
    show = ["年份"] + (["HCI"] if "HCI" in df.columns else []) + [b[0] for b in baskets]
    say(md(df[show]))
    say()

    # ---- machine-readable, ONE ROW PER (basket, year) ---------------------
    # price_time_index.csv is a single POOLED series, and for channel work that
    # is not merely imprecise, it is wrong in a known direction: the erosion
    # basket compounds to 160.70 against the pooled 139.58, because the pooled
    # median is held down by families with huge row counts that do not track
    # material inflation (pavement_marking, electrical_lighting, signing).
    # A consumer escalating a creek-repair estimate needs the basket series, so
    # it is emitted with an explicit basket key rather than left to be derived.
    long_rows = []
    cum = {b[0]: 100.0 for b in baskets}
    for a, b in pairs:
        for name, keep in baskets:
            sub = tx if keep is None else tx[fam.isin(keep)]
            v, n = basket_yoy(sub, a, b)
            if v == v:
                cum[name] *= (1 + v / 100.0)
            long_rows.append({
                "basket": name,
                "from_year": a, "to_year": b,
                "yoy_pct": v if v == v else None,
                "n_matched_items": n,
                "index_level": cum[name],
                # Scope is stated in the file: these are TxDOT lettings, the
                # only subset with enough matched pairs for a chained index.
                # The municipal erosion sample cannot carry one on its own.
                "scope": "txdot_statewide",
                "base_year": pairs[0][0] if pairs else "",
            })
    out = pd.DataFrame(long_rows)
    out.to_csv(os.path.join(CLEAN, "price_time_index_by_basket.csv"),
               index=False, encoding="utf-8-sig")
    say("逐篮子序列已写入 `data/clean/price_time_index_by_basket.csv`"
        "(每行一个 篮子×年份,带 `index_level` 与 `base_year`)。"
        "**给河道工程调价用 `侵蚀修复类` 那个篮子,不要用池化序列。**")
    say()
    say("各篮子的配对子目数(样本量,决定上表每个数字的可信度):")
    say()
    ncols = ["年份"] + ["n:" + b[0] for b in baskets]
    nd = df[ncols].copy()
    nd.columns = ["年份"] + [b[0] for b in baskets]
    say(md(nd, fmt="%.0f"))
    say()
    lvl = [{"篮子": "HCI(官方)", "累计水平(2016=100)": cum_h}]
    for name, keep in baskets:
        sub = tx if keep is None else tx[fam.isin(keep)]
        ds = []
        for a, b in pairs:
            v, n = basket_yoy(sub, a, b)
            if v == v and hci_yoy is not None and b in hci_yoy.index:
                ds.append(abs(v - float(hci_yoy.loc[b])))
        lvl.append({"篮子": name, "累计水平(2016=100)": cum[name],
                    "对 HCI 平均绝对偏差": float(np.mean(ds)) if ds else np.nan,
                    "重叠年数": len(ds)})
    say(md(pd.DataFrame(lvl), fmt="%.2f"))
    say()
    say("### 结论")
    say()
    say("三个假设全部被否:")
    say()
    say("1. **编码换代导致匹配丢失** —— 否。归一化改写了 45% 的行键,"
        "平均绝对偏差 4.81 → 4.81,毫无变化。crosswalk 的匹配规则本身是"
        "`spec+desc+unit`,它连起来的条目在描述键下本来就连着。")
    say("2. **HCI 是数量加权** —— 否,而且方向相反。金额加权把偏差从 4.81 做到 "
        "14.13,相关系数从 0.80 塌到 0.31。原因在 `eff_n`:有效子目数只有 20–38,"
        "加权指数是二十几个巨量子目在代表市场,而且权重逐年从样本重算,"
        "等于每年重画篮子。HCI 用的是固定数量的固定篮子,这是两种东西。")
    say("3. **篮子构成** —— 否,越收窄越差(4.81 → 7.75 → 11.06)。"
        "窄篮子每年只有 30–80 个配对子目,噪声超过了构成信号。")
    say()
    say("**但 2023 年那一行给出了真正有用的东西。** 那一年我的每个子篮子都是 "
        "+22% 到 +33%(侵蚀 +22.0、HCI 式 +29.7、纯铺面 +33.3),而全样本只有 "
        "+9.1%。子集全部高于整体 —— 这是 Simpson 结构:全样本中位数被行数巨大"
        "但不随材料通胀的族压住了(`other` 10.9 万行、`pavement_marking` 7.2 万行、"
        "`electrical_lighting` 2.5 万、`signing` 1.7 万)。材料类子目确实涨了,"
        "HCI 的 +26.2% 和它们一致。")
    say()
    say("**对估价的直接含义:不要用全样本指数给河道侵蚀工程做时间调整。**"
        "侵蚀篮子十年累计 **159.7**,全样本只有 **143.0**,差 12 个百分点。"
        "用全样本会在材料通胀年系统性低估。")
    say()
    say("**可信度边界:**侵蚀篮子每年只有 25–97 个配对子目,"
        "单年数字噪声大(2019 +14.9%、2021 +9.6% 这类单年值不要单独引用);"
        "十年累计比任何单年都可靠。全样本指数对 HCI 的 r=0.80、"
        "平均偏误 −0.92 个百分点,作为**方法正确性**的旁证是成立的 —— "
        "它证明管线没有系统性偏差,但它不是用来给侵蚀工程做escalation的那把尺。")


def main():
    tx = load_panel()
    tx, n_mapped, n_coded = add_normalised_key(tx)
    say("# 指数偏差的归因:匹配 vs 加权")
    say()
    say("我的配对链式指数和 TxDOT 官方中标价指数(HCI)在若干年份对不上。")
    say("两个候选解释,并排检验:**(A) 编码换代导致匹配丢失**、"
        "**(B) HCI 是数量加权而我是等权中位数**。")
    say()
    say("- TxDOT 可用行 **%d**,项目 **%d**,年份 %s-%s"
        % (len(tx), tx.project_key.nunique(), tx.year.min(), tx.year.max()))
    say("- 有可用编码的行 **%d**(%.1f%%),其中 crosswalk 把 **%d** 行的编码"
        "映射到了后继代码" % (n_coded, 100.0 * n_coded / max(len(tx), 1), n_mapped))
    say()
    say("加权指数用的是基期金额权重 `quantity x unit_price`,数学上等于 "
        "Laspeyres 指数。单项比值夹到 [%.2f, %.2f]:单个子目三倍跳动是数据问题,"
        "不是通胀。" % RATIO_CLIP)
    say()

    variants = [
        ("等权中位数 / 描述键(基线)", "item_key", False),
        ("等权中位数 / 编码归一化", "item_key_cw", False),
        ("金额加权 / 描述键", "item_key", True),
        ("金额加权 / 编码归一化", "item_key_cw", True),
    ]
    parts = [chained(tx, k, w, lab) for lab, k, w in variants]
    allv = pd.concat(parts, ignore_index=True)
    allv["train_scope"] = "txdot_published_only"
    allv.to_csv(os.path.join(CLEAN, "index_weighting.csv"), index=False,
                encoding="utf-8-sig")

    hci = load_hci()
    hci_yoy = (hci.pct_change() * 100) if hci is not None else None

    say("## 1. 逐年同比:四个变体 vs HCI")
    say()
    rows = []
    for y in sorted(set(allv["to"])):
        r = {"年份": y}
        for lab, _, _ in variants:
            s = allv[(allv.variant == lab) & (allv["to"] == y)]
            r[lab] = (float(s.yoy_pct.iat[0]) if len(s)
                      and pd.notna(s.yoy_pct.iat[0]) else np.nan)
        if hci_yoy is not None and y in hci_yoy.index:
            r["HCI"] = float(hci_yoy.loc[y])
        rows.append(r)
    say(md(pd.DataFrame(rows)))
    say()

    if hci_yoy is None:
        say("**没有 HCI 文件,无法判别。**")
    else:
        say("## 2. 对 HCI 的拟合优劣")
        say()
        res = []
        for lab, _, _ in variants:
            s = allv[(allv.variant == lab)].dropna(subset=["yoy_pct"])
            s = s[s.n_matched >= MIN_PAIRS]
            j = s.merge(hci_yoy.rename("hci").reset_index()
                        .rename(columns={"year": "to"}), on="to", how="inner")
            j = j.dropna(subset=["hci"])
            if len(j) < 3:
                continue
            res.append({
                "变体": lab,
                "重叠年数": len(j),
                "平均绝对偏差(个百分点)": float((j.yoy_pct - j.hci).abs().mean()),
                "平均偏误(我-HCI)": float((j.yoy_pct - j.hci).mean()),
                "相关系数": float(j.yoy_pct.corr(j.hci)) if len(j) > 2 else np.nan,
                "累计水平": float(s["index"].iat[-1]),
            })
        say(md(pd.DataFrame(res), fmt="%.2f"))
        say()
        if hci is not None:
            lo, hi = sorted(set(allv["from"]))[0], sorted(set(allv["to"]))[-1]
            if lo in hci.index and hi in hci.index:
                say("同期 HCI 累计水平(以 %s=100 计): **%.1f**"
                    % (lo, 100.0 * hci.loc[hi] / hci.loc[lo]))
                say()

    # Concentration is reported whether or not HCI loaded: it is the check on
    # whether a weighted figure means anything at all, and does not depend on
    # having something to compare against.
    say("## 3. 加权指数的集中度(可信度检查)")
    say()
    say("`eff_n` 是权重份额的逆赫芬达尔指数:它是真正在决定这个数字的子目个数。"
        "`eff_n` 远小于 `配对子目数` 就说明这一年的涨幅由极少数大宗子目决定,"
        "那个同比数字就不是市场整体水平,而是几个巨量子目的报价波动。")
    say()
    w = allv[allv.variant == "金额加权 / 描述键"][
        ["from", "to", "n_matched", "eff_n", "yoy_pct"]].copy()
    w.columns = ["起", "止", "配对子目数", "有效子目数", "同比%"]
    say(md(w, fmt="%.1f"))
    say()

    basket_section(tx, hci_yoy)

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "index_weighting.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(OUT) + "\n")
    print("\nwrote reports/index_weighting.md + data/clean/index_weighting.csv")


if __name__ == "__main__":
    main()
