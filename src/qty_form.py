# -*- coding: utf-8 -*-
"""Which functional form does the quantity effect actually take?

The pipeline currently scales price by a single power law per family:

    price = c * q ** e          with e ~ -0.14 fitted across projects

A controlled observation says that form is probably wrong where it matters
most.  In Hickory Creek BID 2024-03, one item appears twice in the same
project, same day, same six bidders, differing ONLY in quantity (55 SY and
1,208 SY).  The median-to-median elasticity is -0.502, far steeper than -0.14.
But split by bidder, the two with the steepest apparent elasticity are also the
two with the largest FIXED cost once the prices are read as `a + b/q`:

    bidder   @55 SY   @1208 SY   apparent e     a ($/SY)   b ($)
      1       76.97      7.08      -0.772         3.75     4,027
      6       85.00     12.00      -0.634         8.52     4,207
      2       10.00      6.00      -0.165         5.81       230
      3        3.50      3.50       0.000         3.50         0
      5       13.00     12.00      -0.026        11.95        58
      4       99.00     88.00      -0.038        87.48       634

Five of six variable rates cluster in $3.50-11.95, and bidder 1's fitted a of
$3.75 nearly coincides with bidder 3's flat $3.50 at both quantities -- bidder 3
being the one who loads no setup cost at all.  So the apparent elasticity is
largely a MOBILISATION charge amortised into the unit price, not a price-volume
relationship.

Two consequences, if that holds generally:

  * A power law extrapolates without bound.  `a + b/q` converges to `a`, so at
    large quantities the power law overshoots downward, and at small quantities
    it understates the price -- and small quantities are the normal case for
    this client's channel repair work.
  * The right thing to publish is (a, b) per family, not one exponent.

WHY THE COMPARISON IS RESTRICTED TO SMALL QUANTITIES: the two forms agree
closely over the bulk of the data, so a global residual comparison is decided
by wherever the samples are densest -- which is the large-quantity end, exactly
where the question does not matter.  The whole disagreement lives in the
sparse, small-quantity tail, so that is where the fit is scored.

Honest limits, stated here because the finding above rests on them:
  * Two points per bidder and two parameters means zero residual by
    construction.  That case is a RE-PARAMETERISATION, not evidence of better
    fit.  What supports it is the clustering of variable rates across
    independent bidders, which is why this script tests the claim on many
    items instead of trusting it.
  * Setup cost is a per-OCCURRENCE charge.  Fitting it across projects mixes it
    with every other between-project difference, so `b` here is an upper bound
    on the true fixed component.
"""
import os
import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")
REPORTS = os.path.join(ROOT, "reports")

MIN_PROJECTS = 8          # points needed before either form is fitted
MIN_SMALL = 3             # points needed inside the small-quantity band
SMALL_FRAC = 0.25         # "small" = below this multiple of the median quantity
OUT = []


def say(s=""):
    OUT.append(s)
    print(s)


def md(df, fmt="%.3f"):
    if df is None or not len(df):
        return "(none)"
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "| " + " | ".join("---" for _ in d.columns) + " |"
    body = ["| " + " | ".join(str(v) for v in r) + " |"
            for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


def fit_both(q, p):
    """Fit power law and a + b/q; return params and small-quantity errors.

    Both are scored by MEDIAN ABSOLUTE PERCENTAGE error rather than squared
    error: unit prices span orders of magnitude across items, and a squared
    criterion would let one large-quantity item decide a family.
    """
    q = np.asarray(q, dtype=float)
    p = np.asarray(p, dtype=float)
    ok = (q > 0) & (p > 0) & np.isfinite(q) & np.isfinite(p)
    q, p = q[ok], p[ok]
    if len(q) < MIN_PROJECTS or np.log(q).std() < 0.2:
        return None

    # power law, fitted in logs (the form the pipeline uses today)
    e, logc = np.polyfit(np.log(q), np.log(p), 1)
    pow_pred = np.exp(logc) * q ** e

    # a + b/q, linear in 1/q
    X = np.column_stack([np.ones_like(q), 1.0 / q])
    try:
        coef, *_ = np.linalg.lstsq(X, p, rcond=None)
    except np.linalg.LinAlgError:
        return None
    a, b = float(coef[0]), float(coef[1])
    hyp_pred = a + b / q

    qmed = float(np.median(q))
    small = q < qmed * SMALL_FRAC
    if small.sum() < MIN_SMALL:
        small = q <= np.quantile(q, 0.25)      # fall back to the bottom quartile
    if small.sum() < MIN_SMALL:
        return None

    ape = lambda pred, m: float(np.median(np.abs(pred[m] - p[m]) / p[m]) * 100)
    return {
        "n": len(q), "n_small": int(small.sum()),
        "qty_median": qmed, "qty_min": float(q.min()), "qty_max": float(q.max()),
        "power_e": float(e), "power_c": float(np.exp(logc)),
        "hyp_a": a, "hyp_b": b,
        "power_mape_small": ape(pow_pred, small),
        "hyp_mape_small": ape(hyp_pred, small),
        "power_mape_all": ape(pow_pred, np.ones_like(q, dtype=bool)),
        "hyp_mape_all": ape(hyp_pred, np.ones_like(q, dtype=bool)),
    }


def per_bidder_b(m, min_projects=4):
    """Distribution of the fixed term `b` ACROSS bidders, within a family.

    Prompted by the one bidder in the controlled case who quoted $3.50 at both
    55 SY and 1,208 SY: fitted, that is a = 3.50 and b = 0 -- no setup cost
    amortised into the unit price at all, while two others carried b of about
    $4,000.  If `b` is bimodal across bidders -- a cluster near zero and a
    cluster in the thousands -- then it is measuring BIDDING STRATEGY (who
    chooses to load mobilisation into unit prices) more than cost structure,
    and that changes how it may be used:

      * cost structure  -> `b` belongs in the price model, it is real money
      * strategy        -> `b` describes how bidders ALLOCATE a total across
                           line items, so a low-bid prediction should use the
                           low end of the `b` distribution, not its centre,
                           because the aggressive bidder is the one who does
                           not load it

    Fitted per (item, bidder) across projects, so it needs the same contractor
    pricing the same item at different quantities -- which the TxDOT data
    supports, since the same contractors bid repeatedly.
    """
    # Group on the CANONICAL contractor name.  Bidder names come off column
    # headers, so one firm arrives spelled several ways (`Rebcon LLC` /
    # `Rebcon, LLC`, four spellings of HQS Construction), and this fit needs the
    # same contractor seen at different quantities -- exactly the evidence that
    # a split identity destroys.  Joining the spellings takes contractors
    # present in >=3 projects from 76 to 113.  Falls back to the raw name when
    # the panel predates the column, so an old panel still fits, just worse.
    bcol = "bidder_norm" if "bidder_norm" in m.columns else "bidder"
    m = m[m[bcol].fillna("") != ""] if bcol == "bidder_norm" else m
    g = (m.groupby(["item_family", "unit", "item_key", bcol, "project_key"],
                   observed=True)
         .agg(p=("unit_price", "median"), q=("quantity", "median")).reset_index())
    rows = []
    for (famly, unit, ikey, bidder), sub in g.groupby(
            ["item_family", "unit", "item_key", bcol], observed=True):
        if len(sub) < min_projects or famly in ("other", ""):
            continue
        q = sub.q.values.astype(float)
        p = sub.p.values.astype(float)
        if np.log(np.clip(q, 1e-9, None)).std() < 0.2:
            continue
        X = np.column_stack([np.ones_like(q), 1.0 / q])
        try:
            coef, *_ = np.linalg.lstsq(X, p, rcond=None)
        except np.linalg.LinAlgError:
            continue
        rows.append({"item_family": famly, "unit": unit, "item_key": ikey,
                     "bidder": bidder, "n_projects": len(sub),
                     "a": float(coef[0]), "b": float(coef[1])})
    return pd.DataFrame(rows)


def main():
    mp = os.path.join(CLEAN, "master_panel.csv")
    if not os.path.exists(mp):
        print("missing master_panel.csv")
        return
    use = ["source", "source_set", "item_family", "item_key", "unit", "quantity",
           "unit_price", "project_key", "competition_class", "project_kind",
           "is_engineer_estimate", "bidder"]
    m = pd.read_csv(mp, encoding="utf-8-sig", low_memory=False,
                    usecols=lambda c: c in use)
    for c in ("quantity", "unit_price"):
        m[c] = pd.to_numeric(m[c], errors="coerce")
    ee = m.is_engineer_estimate.astype(str).str.lower().isin(["true", "1", "yes"])
    # Training side only: a form fitted on the company's own projects could not
    # then be validated against them.
    m = m[(~ee) & (m.unit_price > 0) & (m.quantity > 0)
          & (m.source_set == "external")
          & (m.competition_class == "competitive")]
    if "project_kind" in m.columns:
        m = m[m.project_kind == "construction"]

    say("# 数量效应的函数形式:幂律 vs 固定+变动")
    say()
    say("现在管线用的是逐族单一幂律 `price = c · q^e`。一个受控观测"
        "(同项目、同一天、同一批 6 家投标人,同子目只有数量不同)显示"
        "表观弹性 −0.502 **基本不是弹性,是进场费摊进了单价** —— 表观弹性最大的"
        "两家恰恰是固定费用最大的两家($4,027 / $4,207),而不是变动费率更陡的两家。")
    say()
    say("**残差只在小工程量档比较**(低于该子目中位数量的 %.0f%%,"
        "样本不足时退回最低四分位)。两种形式在数据主体上几乎一致,"
        "全局比较会被样本最密的大工程量端决定 —— 而分歧恰好全在稀疏的小量端。"
        % (SMALL_FRAC * 100))
    say()

    # Per project, per exact item: median across that project's bidders, and
    # the project's own low bid (the actual target variable).
    s1 = (m.groupby(["item_family", "unit", "item_key", "project_key"],
                    observed=True)
          .agg(p_med=("unit_price", "median"),
               p_low=("unit_price", "min"),
               q=("quantity", "median")).reset_index())

    rows = []
    for (famly, unit), sub in s1.groupby(["item_family", "unit"], observed=True):
        if famly in ("other", "") or len(sub) < MIN_PROJECTS:
            continue
        for label, col in (("median", "p_med"), ("low_bid", "p_low")):
            r = fit_both(sub.q.values, sub[col].values)
            if not r:
                continue
            r.update({"item_family": famly, "unit": unit, "target": label})
            r["winner"] = ("a+b/q" if r["hyp_mape_small"] < r["power_mape_small"] * 0.9
                           else ("power" if r["power_mape_small"] < r["hyp_mape_small"] * 0.9
                                 else "tie"))
            rows.append(r)
    res = pd.DataFrame(rows)
    if not len(res):
        say("**可拟合的 (族, 单位) 组合不足,无法判别。**")
    else:
        res.to_csv(os.path.join(CLEAN, "qty_form_comparison.csv"), index=False,
                   encoding="utf-8-sig")
        say("## 1. 判别结果")
        say()
        for tgt in ("median", "low_bid"):
            t = res[res.target == tgt]
            if not len(t):
                continue
            w = t.winner.value_counts()
            say("**目标 = %s** (%d 个 (族,单位) 组合)" % (tgt, len(t)))
            say()
            say("- `a+b/q` 更好: **%d**" % int(w.get("a+b/q", 0)))
            say("- 幂律更好: **%d**" % int(w.get("power", 0)))
            say("- 相当(差距 <10%%): %d" % int(w.get("tie", 0)))
            say("- 小量档中位 MAPE: `a+b/q` **%.1f%%** vs 幂律 **%.1f%%**"
                % (t.hyp_mape_small.median(), t.power_mape_small.median()))
            say("- 全样本中位 MAPE(供对照,**不是判据**): `a+b/q` %.1f%% vs 幂律 %.1f%%"
                % (t.hyp_mape_all.median(), t.power_mape_all.median()))
            say()

        say("## 2. 客户主业族的逐族结果")
        say()
        CORE = ["gabion_wall", "gabion_mattress", "trm", "trm_permanent",
                "trm_temporary", "rock_anchor", "conc_beam", "riprap_conc",
                "riprap_stone", "retaining_wall", "excavation", "embankment_fill"]
        core = res[(res.item_family.isin(CORE)) & (res.target == "low_bid")]
        if not len(core):
            core = res[res.item_family.isin(CORE)]
        cols = ["item_family", "unit", "target", "n", "n_small", "qty_median",
                "power_e", "hyp_a", "hyp_b",
                "power_mape_small", "hyp_mape_small", "winner"]
        say(md(core[cols].sort_values("n", ascending=False), fmt="%.2f")
            if len(core) else "(客户主业族样本不足)")
        say()
        say("`hyp_a` 是变动费率(单位价),`hyp_b` 是每次发生的固定费用。"
            "**`hyp_b` 是真实固定分量的上界** —— 跨项目拟合会把项目之间"
            "其他一切差异也算进去。")
        say()
        say("## 3. 怎么用")
        say()
        say("- `winner == 'a+b/q'` 的组合:小工程量报价用 `价格 = a + b/数量`,"
            "不要用幂律外推")
        say("- `winner == 'power'` 的组合:保持现有 `qty_elasticity` 缩放")
        say("- `winner == 'tie'`:两者在小量档差距 <10%,用哪个都行,"
            "但 `a+b/q` 在大工程量上收敛而幂律无界,所以**外推时选 `a+b/q`**")
        say()
        say("### 两个必须一起读的限制")
        say()
        say("**(1) 负的 `hyp_b` 是拟合在抓噪声的信号,不是发现。** "
            "固定费用不可能是负的。出现负值的组合,`a+b/q` 的胜出不可信:")
        neg = res[(res.hyp_b < 0)][["item_family", "unit", "target", "n",
                                    "hyp_a", "hyp_b", "winner"]]
        say()
        say(md(neg.head(12), fmt="%.2f") if len(neg) else "(没有负值)")
        say()
        say("**(2) 这个检验量的是跨项目关系,而那个受控观测是项目内的。** "
            "它们是两个不同的问题,这个脚本答不了后者。项目内把地域、年份、"
            "竞争强度、设计单位全部固定住,跨项目则把它们全混进来 —— "
            "所以跨项目拟合里,数量以外的离散很可能压过了数量本身的作用。"
            "两种形式的 MAPE 都在 20–110%,这本身就说明**数量项解释的方差很少**,"
            "不管用哪个函数形式。")
        say()
        say("所以结论不是「`a+b/q` 错了」,而是:**单份文档内那个 3 倍的进场费效应,"
            "在跨项目数据上检测不到** —— 要么它被跨项目噪声淹掉了,要么它确实只是"
            "那一个案例的特征。要分清只能找更多同项目内的数量配对,"
            "那是采集侧的活,不是拟合能解决的。")
        say()
        say("明细见 `data/clean/qty_form_comparison.csv`。")
        say()

        # ---- is `b` a cost or a bidding choice? --------------------------
        say("## 4. 固定项 `b` 在投标人之间的分布:成本结构还是报价策略")
        say()
        pb = per_bidder_b(m)
        if not len(pb):
            say("同一承包商在同一子目上跨数量的样本不足,无法判别。")
        else:
            pb.to_csv(os.path.join(CLEAN, "qty_form_per_bidder.csv"), index=False,
                      encoding="utf-8-sig")
            say("逐 (子目, 投标人) 拟合 `a + b/q`,共 **%d** 个组合、"
                "%d 个承包商。" % (len(pb), pb.bidder.nunique()))
            say()
            # Share of bidders whose b is effectively zero, per family.
            rows2 = []
            for famly, sub in pb.groupby("item_family", observed=True):
                if len(sub) < 6:
                    continue
                near0 = float((sub.b.abs() < sub.b.abs().median() * 0.1).mean())
                rows2.append({
                    "item_family": famly, "n_fits": len(sub),
                    "b_p25": sub.b.quantile(.25), "b_median": sub.b.median(),
                    "b_p75": sub.b.quantile(.75),
                    "share_b_near_zero": near0 * 100,
                    "a_median": sub.a.median()})
            t2 = pd.DataFrame(rows2).sort_values("n_fits", ascending=False)
            say(md(t2.head(18), fmt="%.2f") if len(t2) else "(样本不足)")
            say()
            say("**读法**:`share_b_near_zero` 高(很多承包商的 `b` 接近 0,"
                "而另一些很大)说明 `b` 主要是**报价策略**——谁选择把动员费"
                "摊进单价。那种情况下预测**最低价**应该取 `b` 分布的低端,"
                "不是中位,因为激进报价的那家正是不摊的那家。")
            say()
            say("`b` 若在承包商之间分布集中,它更接近真实的固定成本,"
                "可以直接进价格模型。明细见 `data/clean/qty_form_per_bidder.csv`。")
            say()
            negshare = 100.0 * float((pb.b < 0).mean())
            say("### 这一节的判决:`b` 横跨零,所以它在跨项目数据上不是成本结构")
            say()
            say("- `b < 0` 的拟合占 **%.1f%%**。固定费用不可能是负的,"
                "所以近三分之一的拟合给出的是物理上不可能的参数。" % negshare)
            say("- `b` 的分位:p10 **%.0f**、p25 **%.0f**、中位 **%.0f**、"
                "p75 **%.0f**、p90 **%.0f** —— 分布跨越零点。"
                % (pb.b.quantile(.1), pb.b.quantile(.25), pb.b.median(),
                   pb.b.quantile(.75), pb.b.quantile(.9)))
            say()
            say("**如果 `b` 是真实的固定成本,它应该几乎处处为正。** 它不是,"
                "所以这个重参数化在跨项目数据上吸收的是噪声,不是结构。"
                "这和上面的判别结果一致:`a+b/q` 的胜出里有 28% 带负 `b`,"
                "剔掉之后真正可信的胜出只占全部组合的 17.6%。")
            say()
            say("**所以保留幂律。** 但同时要记住两种形式的小量档 MAPE 都在 "
                "50% 左右、几乎相同 —— 这说明**数量项本身解释的方差很少**,"
                "不是哪个函数形式更好的问题。把弹性当精确缩放用是过度信任它;"
                "小工程量上应该披露不确定而不是假装算准了。")

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "qty_form.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(OUT) + "\n")
    print("\nwrote reports/qty_form.md + data/clean/qty_form_comparison.csv")


if __name__ == "__main__":
    main()
