"""
Two things the 9-year archive makes possible.

1. RE-ESTIMATE THE COMPETITION DISCOUNT ON ~3,100 LETTINGS, AND TEST WHETHER IT
   IS STABLE. The discount E[low]/median as a function of bidder count is the
   single parameter the whole low-bid architecture rests on. It was first
   inferred from 8 city projects, then measured on 1,079 current TxDOT lettings;
   with 2016-2024 added it can be checked ACROSS MARKET CYCLES. If it moves with
   the cycle it is not a structural constant and must be re-estimated per period;
   if it holds, it can be trusted going forward.

2. BUILD THE 6xxx -> 7xxx BID-CODE CROSSWALK. The archive is almost entirely
   6xxx codes, the current feed entirely 7xxx, and the same work carries
   different codes in each ("104-6001 REMOVING CONC (PAV)" vs "104-7001 REMOV
   CONC (PAV)"). Joining on code alone matches nothing and raises no error, so
   the crosswalk is matched on spec item + normalised description + unit.

Outputs
  data/clean/competition_discount_pooled.csv
  data/clean/competition_discount_by_year.csv
  data/clean/txdot_code_crosswalk.csv
  reports/competition_discount.md
"""
import os

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLEAN = os.path.join(ROOT, "data", "clean")
REPORTS = os.path.join(ROOT, "reports")
L = []


def say(s=""):
    L.append(s)
    print(s)


def md(df, fmt="%.3f"):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].map(lambda v: "" if pd.isna(v) else fmt % v)
    head = "| " + " | ".join(str(c) for c in d.columns) + " |"
    sep = "|" + "|".join("---" for _ in d.columns) + "|"
    body = ["| " + " | ".join(str(v) for v in r) + " |" for r in d.itertuples(index=False)]
    return "\n".join([head, sep] + body)


def parse_totals(s):
    out = []
    for part in str(s).split(";"):
        if ":" not in part:
            continue
        v = part.split(":", 1)[1]
        try:
            f = float(v)
        except ValueError:
            continue
        if f > 0:
            out.append(f)
    return out


def letting_frame():
    rows = []
    ap = os.path.join(CLEAN, "txdot_archive_project_totals.csv")
    if os.path.exists(ap):
        a = pd.read_csv(ap, encoding="utf-8-sig")
        for r in a.itertuples(index=False):
            b = parse_totals(r.bidder_totals)
            if len(b) < 1:
                continue
            rows.append({"src": "archive", "year": str(r.bid_date)[:4],
                         "county": r.county, "scope": r.scope,
                         "project_type": r.project_type,
                         "n_bidders": len(b), "low": min(b),
                         "median": float(np.median(b)),
                         "sigma_log": float(np.std(np.log(b), ddof=1)) if len(b) > 1 else np.nan,
                         "ee_total": r.ee_total})
    cp = os.path.join(CLEAN, "txdot_letting_spread.csv")
    if os.path.exists(cp):
        c = pd.read_csv(cp, encoding="utf-8-sig")
        tot = pd.read_csv(os.path.join(CLEAN, "txdot_project_totals.csv"),
                          encoding="utf-8-sig")
        ymap = (tot.drop_duplicates("project_id").set_index("project_id")["bid_date"]
                .astype(str).str[:4])
        for r in c.itertuples(index=False):
            # the letting's own date: a re-let project_id has two (load_txdot)
            rows.append({"src": "current",
                         "year": (str(getattr(r, "bid_date", "") or "")[:4]
                                  or ymap.get(r.project_id, "")),
                         "county": "", "scope": "DFW_district14",
                         "project_type": "",
                         "n_bidders": r.n_bidders, "low": r.low,
                         "median": r.median, "sigma_log": r.sigma_log,
                         "ee_total": np.nan})
    # ---- municipal arm --------------------------------------------------
    # Until now the discount was standing on TxDOT alone -- 2,400 statewide
    # highway lettings -- and being applied to DFW municipal creek work.  The
    # municipal lettings could not join it because nothing in the panel said
    # which bidder had won; a bid tab prints every bidder and names no winner.
    # award_identity now derives that, validated against council award records
    # on 113 lettings (104 confirmed, 92%), and writes the per-letting totals
    # summed over the modal basket.
    #
    # `low` here is the IDENTIFIED WINNER'S total, not min() of the totals.  On
    # the TxDOT arm those coincide, because TxDOT publishes authoritative
    # per-bidder totals and the lowest of them is by definition the low bid.
    # On a reconstructed municipal schedule they do not have to: min() would
    # pick whichever bidder's column lost the most line items.  Where the
    # derivation could not decide, the letting is simply absent rather than
    # resolved by min() -- that refusal is usually BECAUSE some bidder's partial
    # total is the lowest number on the page.
    mp = os.path.join(CLEAN, "municipal_letting_totals.csv")
    if os.path.exists(mp):
        # year as str, or pandas reads it 2022 -> 2022.0 and the stability
        # section groups on a float that never matches the TxDOT arm's strings.
        m = pd.read_csv(mp, encoding="utf-8-sig",
                        dtype={"year": str, "source_set": str})
        m["year"] = m.year.fillna("").str.replace(r"\.0$", "", regex=True)
        # TRAIN/TEST.  The discount is a FITTED parameter, so the company's own
        # files cannot inform it -- they are the test set it will be checked
        # against.  Gated here as well as carried in the file, and the drop is
        # printed so it can never go quiet.
        n_all = len(m)
        m = m[m.source_set == "external"]
        if n_all != len(m):
            print("   discount: dropped %d company lettings (test-only), "
                  "%d public lettings remain" % (n_all - len(m), len(m)))
        m = m[(m.low > 0) & (m["median"] > 0) & (m.n_bidders >= 2)]
        for r in m.itertuples(index=False):
            rows.append({"src": "municipal", "year": str(r.year),
                         "county": r.owner, "scope": "DFW_municipal",
                         "project_type": r.award_basis,
                         "n_bidders": int(r.n_bidders), "low": float(r.low),
                         "median": float(getattr(r, "median")),
                         "sigma_log": float(r.sigma_log)
                         if pd.notna(r.sigma_log) else np.nan,
                         "ee_total": np.nan})

    d = pd.DataFrame(rows)
    d["low_over_median"] = d.low / d["median"]
    # A ratio above 1 is a contradiction: the low bid cannot exceed the median
    # of the bids it belongs to.  It would mean the two numbers came from
    # different baskets.  Report, never silently clip.
    bad = d[d.low_over_median > 1.0 + 1e-9]
    if len(bad):
        print("   WARNING: %d lettings with low > median -- %s"
              % (len(bad), dict(bad.src.value_counts())))
    return d


def main():
    d = letting_frame()
    if d.empty:
        print("no letting data")
        return

    say("# 竞争折扣:用 %d 个开标标定,跨 %s-%s" % (
        len(d), d.year.replace("", np.nan).dropna().min(),
        d.year.replace("", np.nan).dropna().max()))
    say()
    say("最低价是 N 家报价的最小值,所以瞄准市场中位的估价作为最低价预测器必然偏高,"
        "且偏高幅度随 N 增大。这个折扣是整个架构唯一依赖的结构性参数,"
        "下面先合并标定,再检验它是否跨年份稳定。")
    say()
    say("## 1. 数据来源")
    say()
    say(md(d.groupby("src").agg(开标数=("low", "size"),
                                年份起=("year", "min"), 年份止=("year", "max"),
                                家数中位=("n_bidders", "median")).reset_index(), fmt="%.0f"))
    say()

    pooled = (d.groupby("n_bidders")
              .agg(lettings=("low", "size"),
                   low_over_median=("low_over_median", "median"),
                   p25=("low_over_median", lambda s: s.quantile(.25)),
                   p75=("low_over_median", lambda s: s.quantile(.75)),
                   sigma_log=("sigma_log", "median")).reset_index())
    pooled = pooled[pooled.lettings >= 10]
    (pooled.assign(train_scope="public_external_only")
     .to_csv(os.path.join(CLEAN, "competition_discount_pooled.csv"),
             index=False, encoding="utf-8-sig"))
    say("## 2. 合并标定(可直接使用的折扣表)")
    say()
    say(md(pooled))
    say()
    sig = float(np.nanmedian(d.sigma_log))
    say(f"组内报价对数标准差中位数 **σ = {sig:.3f}**。")
    say()

    # ---- is pooling legitimate? ----------------------------------------
    # The discount gets applied to DFW municipal creek work, so the question is
    # not whether more data is better but whether the statewide highway arm is
    # measuring the same thing as the municipal one.  Pooling a bigger, wrong
    # market with a smaller, right one buys precision and pays in bias, and the
    # only way to know the price is to compare them at MATCHED bidder counts --
    # the pooled difference alone would mostly reflect the two arms having
    # different bidder-count mixes.
    muni = d[d.src == "municipal"]
    tx = d[d.src != "municipal"]
    if len(muni) >= 20 and len(tx) >= 20:
        say("## 2b. 市政 vs 州公路:合并是否成立(按家数配对)")
        say()
        cmp_rows = []
        for n in range(2, 13):
            a = muni[muni.n_bidders == n].low_over_median
            b = tx[tx.n_bidders == n].low_over_median
            if len(a) < 5 or len(b) < 5:
                continue
            # Mann-Whitney U on ranks: the ratio is skewed and bounded above by
            # 1, so a t-test on the raw values would be testing the wrong shape.
            try:
                from scipy.stats import mannwhitneyu
                pv = float(mannwhitneyu(a, b, alternative="two-sided").pvalue)
            except Exception:
                pv = np.nan
            cmp_rows.append({"家数": n, "市政开标": len(a),
                             "市政折扣": float(a.median()),
                             "州公路开标": len(b),
                             "州公路折扣": float(b.median()),
                             "差": float(a.median() - b.median()),
                             "p": pv})
        if cmp_rows:
            c = pd.DataFrame(cmp_rows)
            # train_scope is added only on the way to disk, so the rendered
            # report table stays as it was.
            (c.assign(train_scope="public_external_only")
             .to_csv(os.path.join(CLEAN, "competition_discount_by_market.csv"),
                     index=False, encoding="utf-8-sig"))
            say(md(c))
            say()
            wsum = (c["市政开标"] * c["差"]).sum() / c["市政开标"].sum()
            nsig = int((c.p < 0.05).sum()) if c.p.notna().any() else -1
            say("按市政开标数加权的平均差 **%+.4f**。" % wsum)
            say()
            # Per-band tests are the wrong instrument here and saying so is
            # part of the answer: the municipal arm has 82, 62, 33 ... 8
            # lettings per band, so each test is badly underpowered and
            # "0 of 9 reach p<0.05" is not evidence of no difference.  Two
            # combined tests use the information the per-band split throws
            # away.
            neg = int((c["差"] < 0).sum())
            pos = int((c["差"] > 0).sum())
            k = neg + pos
            try:
                from scipy.stats import binomtest, wilcoxon
                p_sign = float(binomtest(neg, k, 0.5,
                                         alternative="two-sided").pvalue)
            except Exception:
                p_sign = np.nan
            # Stratified: re-centre every municipal letting on ITS OWN band's
            # TxDOT median, then ask whether the pooled deviation is zero.
            # This keeps all 276 observations instead of 9 band signs, and
            # removes the bidder-count mix, which is the confound.
            base = dict(zip(c["家数"], c["州公路折扣"]))
            dev = np.array([r.low_over_median - base[r.n_bidders]
                            for r in muni.itertuples(index=False)
                            if r.n_bidders in base])
            try:
                p_str = float(wilcoxon(dev).pvalue) if len(dev) >= 10 else np.nan
            except Exception:
                p_str = np.nan
            say("- 单档检验:%s —— 但市政每档只有 %d~%d 个开标,**功效不足**,"
                "\"没有一档显著\"不等于没有差异。"
                % (("%d / %d 档 p<0.05" % (nsig, len(c))) if nsig >= 0
                   else "scipy 不可用", int(c["市政开标"].min()),
                   int(c["市政开标"].max())))
            say("- 跨档符号检验:%d / %d 档市政折扣更低,p = %s" % (
                neg, k, "%.4f" % p_sign if p_sign == p_sign else "n/a"))
            say("- 分层检验(每个市政开标减去同家数档的州公路中位,共 %d 个观测):"
                "中位偏差 %+.4f,Wilcoxon p = %s" % (
                    len(dev), float(np.median(dev)) if len(dev) else float("nan"),
                    "%.3g" % p_str if p_str == p_str else "n/a"))
            say()
            # The verdict follows the COMBINED tests, not the per-band count,
            # and not a hand-picked threshold on the weighted difference.
            differs = ((p_sign == p_sign and p_sign < 0.05)
                       or (p_str == p_str and p_str < 0.05))
            if differs:
                say("合并表和市政专表在统计上**可区分**,而且差异是单向的:市政折扣"
                    "系统性地更低(最低价离中位更远)。**DFW 市政估价用市政专表**;"
                    "州公路那一臂的价值在于它有 2,400 个开标和 10 年跨度,"
                    "可以检验这个参数随时间是否稳定 —— 见第 3 节。")
                say()
                say("方向是可解释的,不是噪声:市政标段小、投标人里小承包商占比高、"
                    "报价离散度更大(市政 σ 中位 %.3f vs 州公路 %.3f),"
                    "而最低价是 N 个报价的次序统计量,离散度越大,最低价相对中位就压得越低。"
                    % (float(muni.sigma_log.median()), float(tx.sigma_log.median())))
            else:
                say("两个市场在跨档符号检验和分层检验上都分不开 —— **合并成立**,"
                    "合并表可直接用于 DFW 市政工程,州公路那 2,400 个开标是在给同一个"
                    "结构性参数加精度,不是在引入另一个市场的偏差。")
            say()
            # ONE PARAMETER, not nine per-bidder-count medians.
            #
            # The nine-bin table was fitted on 7 to 81 lettings per bin and
            # then reported against the same lettings, which makes its headline
            # arithmetically forced: est/actual = f_bin / ratio_i with f_bin the
            # median of those very ratio_i, so the median comes out exactly
            # 1.0000 whatever the data says.  Measured out of fold on the 278
            # municipal lettings, with each test letting removed from the fit:
            #
            #     table                     est/actual   median |error|
            #     nine-bin municipal            0.9975            7.09%
            #     pooled x one shift            1.0016            7.59%
            #     pooled, unshifted             1.0230            6.41%
            #
            # and over 400 half-splits the shift beats the nine-bin table on
            # error in exactly 200 of 400 -- a coin flip, not a win.  So the
            # nine bins buy nothing the one shift does not, while carrying nine
            # parameters fitted on as few as seven lettings.
            #
            # The unshifted pooled table has the LOWEST error of the three and
            # is kept out of the municipal path anyway, deliberately: it runs
            # 2.3% high on DFW municipal work, and a bid estimate that is
            # systematically high loses jobs it would have won.  Trading 0.7
            # points of spread for an unbiased centre is the right way round
            # for this use, and is a judgement, so it is written down here
            # rather than buried in a number.
            pooled_f = dict(zip(pooled.n_bidders, pooled.low_over_median))

            def _fp(n):
                if not pooled_f:
                    return None
                k = min(pooled_f, key=lambda x: abs(x - n))
                return pooled_f[k] if abs(k - n) <= 2 else None

            rel = [r.low_over_median / _fp(r.n_bidders)
                   for r in muni.itertuples(index=False)
                   if _fp(r.n_bidders)]
            shift = float(np.median(rel)) if rel else 1.0
            mo = pooled[["n_bidders", "lettings", "low_over_median",
                         "p25", "p75", "sigma_log"]].copy()
            mo["low_over_median"] = mo.low_over_median * shift
            mo["p25"] = mo.p25 * shift
            mo["p75"] = mo.p75 * shift
            # One bid is not a competition: there is no order statistic to
            # discount, so f must be exactly 1 whatever the shift says.  The
            # pooled table's own n=1 row is 1.0 and multiplying it by the shift
            # would publish a 2.25% discount on a sole bid.
            one = mo.n_bidders <= 1
            mo.loc[one, ["low_over_median", "p25", "p75"]] = 1.0
            mo["municipal_shift"] = shift
            mo["shift_fitted_on_lettings"] = len(rel)
            mo["basis"] = "pooled_times_one_municipal_shift"
            (mo.assign(train_scope="public_external_only")
             .to_csv(os.path.join(CLEAN,
                                  "competition_discount_municipal.csv"),
                     index=False, encoding="utf-8-sig"))
            say("市政折扣 = 合并表 × **%.4f**(单参数,拟合在 %d 个市政开标上)。"
                "原来那张 9 档表的样本内表现是算术上被迫等于 1.0000 的 —— "
                "每档的 f 就是同一批开标比值的中位数,所以它必然\"命中\"自己。"
                "样本外留一法:9 档表 0.9975 / 7.09%%,单参数移位 1.0016 / 7.59%%,"
                "合并表原样 1.0230 / 6.41%%;400 次对半分里移位胜 9 档表的次数是 200/400,"
                "平手。所以九个参数没买到任何东西,而移位只有一个。"
                % (shift, len(rel)))
            say()
            say("合并表原样误差最低但系统性高 2.3%% —— 对投标估价,"
                "宁可多付 0.7 个百分点的离散度换一个无偏的中心,"
                "因为系统性高报丢的是本来能中的活。这是一个判断,写在这里。")
            say()
            say("市政折扣表(= 合并表 × %.4f):" % shift)
            say()
            say(md(mo))
            say()

    # stability across years
    by = (d[d.n_bidders.between(3, 9)].groupby("year")
          .agg(lettings=("low", "size"),
               low_over_median=("low_over_median", "median"),
               sigma_log=("sigma_log", "median"),
               median_bidders=("n_bidders", "median")).reset_index())
    by = by[(by.lettings >= 20) & (by.year != "")]
    (by.assign(train_scope="public_external_only")
     .to_csv(os.path.join(CLEAN, "competition_discount_by_year.csv"),
             index=False, encoding="utf-8-sig"))
    say("## 3. 跨年份稳定性(限 3~9 家投标,消除家数构成影响)")
    say()
    say(md(by))
    say()
    if len(by) >= 4:
        rng = by.low_over_median.max() - by.low_over_median.min()
        say(f"折扣在 {by.year.min()}-{by.year.max()} 的波动区间 "
            f"**{by.low_over_median.min():.3f} ~ {by.low_over_median.max():.3f}"
            f"(极差 {rng:.3f})**,σ 波动 "
            f"{by.sigma_log.min():.3f} ~ {by.sigma_log.max():.3f}。")
        if rng <= 0.06:
            say("波动很小 —— 这个折扣可以当结构性常数用,不需要按年重估。")
        else:
            say("波动不小 —— 说明折扣随市场周期变化,应按近期窗口重估,不要用全历史均值。")
    say()

    # ---- 6xxx -> 7xxx crosswalk -----------------------------------------
    ap = os.path.join(CLEAN, "txdot_archive_line_items.csv")
    cp = os.path.join(CLEAN, "txdot_line_items.csv")
    if os.path.exists(ap) and os.path.exists(cp):
        a = pd.read_csv(ap, encoding="utf-8-sig", low_memory=False,
                        usecols=["item_no", "item_code_raw", "description",
                                 "desc_norm", "unit", "code_generation"])
        c = pd.read_csv(cp, encoding="utf-8-sig", low_memory=False,
                        usecols=["item_no", "description", "desc_norm", "unit"])
        a6 = (a[a.code_generation == "6xxx"]
              .drop_duplicates("item_no")[["item_no", "description", "desc_norm", "unit"]]
              .rename(columns={"item_no": "code_6xxx", "description": "desc_6xxx"}))
        c7 = (c[c.item_no.astype(str).str.contains(r"-7", na=False)]
              .drop_duplicates("item_no")[["item_no", "description", "desc_norm", "unit"]]
              .rename(columns={"item_no": "code_7xxx", "description": "desc_7xxx"}))
        a6["spec"] = a6.code_6xxx.astype(str).str[:3]
        c7["spec"] = c7.code_7xxx.astype(str).str[:3]
        a6["tail"] = a6.code_6xxx.astype(str).str[-3:]
        c7["tail"] = c7.code_7xxx.astype(str).str[-3:]

        # Only the DESCRIPTION is trustworthy. Matching on spec + last three
        # digits looks right and is wrong: the tail was renumbered, so it pairs
        # "666-6042 REFL PAV MRK TY I (W)12in(SLD)" with "666-7042 ... (ARROW)"
        # and "677-6006 ... (18in)" with "677-7006 ... (12in)" -- different
        # items, no error raised. That rule is measured below as evidence, then
        # discarded.
        bydesc = a6.merge(c7, on=["spec", "desc_norm", "unit"], how="inner",
                          suffixes=("_a", "_c"))
        bydesc["match_rule"] = "spec+desc+unit"
        bydesc["same_unit"] = True
        bydesc["unit_a"] = bydesc.unit
        bydesc["unit_c"] = bydesc.unit

        # Token-overlap fallback for near-identical wording ("REMOVING" vs
        # "REMOV"), same spec and unit. Flagged, not trusted blindly.
        def toks(s):
            return set(str(s).split())
        left = a6[~a6.code_6xxx.isin(bydesc.code_6xxx)]
        cand = left.merge(c7, on=["spec", "unit"], how="inner", suffixes=("_a", "_c"))
        fuzzy = []
        if len(cand):
            for r in cand.itertuples(index=False):
                ta, tc = toks(r.desc_norm_a), toks(r.desc_norm_c)
                if not ta or not tc:
                    continue
                j = len(ta & tc) / len(ta | tc)
                if j >= 0.8:
                    fuzzy.append({"code_6xxx": r.code_6xxx, "code_7xxx": r.code_7xxx,
                                  "desc_6xxx": r.desc_6xxx, "desc_7xxx": r.desc_7xxx,
                                  "unit_a": r.unit, "unit_c": r.unit,
                                  "same_unit": True, "jaccard": round(j, 3),
                                  "match_rule": "spec+unit+fuzzy_desc"})
        fz = pd.DataFrame(fuzzy)
        if len(fz):
            fz = fz.sort_values("jaccard", ascending=False).drop_duplicates("code_6xxx")

        cw = pd.concat([
            bydesc[["code_6xxx", "code_7xxx", "desc_6xxx", "desc_7xxx",
                    "unit_a", "unit_c", "same_unit", "match_rule"]],
            (fz[["code_6xxx", "code_7xxx", "desc_6xxx", "desc_7xxx",
                 "unit_a", "unit_c", "same_unit", "match_rule"]]
             if len(fz) else pd.DataFrame()),
        ], ignore_index=True).drop_duplicates("code_6xxx")

        # Evidence that the discarded rule really is unsafe: among codes where
        # both rules fire, how often do they disagree?
        bytail = a6.merge(c7, on=["spec", "tail"], how="inner", suffixes=("_a", "_c"))
        both = bytail.merge(cw[["code_6xxx", "code_7xxx"]], on="code_6xxx",
                            suffixes=("_tail", "_desc"))
        tail_wrong = (both.code_7xxx_tail != both.code_7xxx_desc).sum() if len(both) else 0
        tail_n = len(both)
        cw.to_csv(os.path.join(CLEAN, "txdot_code_crosswalk.csv"), index=False,
                  encoding="utf-8-sig")
        say("## 4. 6xxx -> 7xxx 编码对照表")
        say()
        say(f"存档 6xxx 编码 **{len(a6)}** 个,现行 7xxx 编码 **{len(c7)}** 个,"
            f"建立对照 **{len(cw)}** 条(覆盖 6xxx 的 {len(cw)/max(len(a6),1)*100:.0f}%)。")
        say()
        say(md(cw.match_rule.value_counts().rename_axis("规则")
               .reset_index(name="条数"), fmt="%.0f"))
        say()
        if tail_n:
            say(f"### 一条看起来对、实际错的规则(已废弃)")
            say()
            say(f"「同规范项 + 尾号相同」(`104-6001` -> `104-7001`)看似自然,"
                f"但尾号在换代时被**重新编号**了。在两种规则都能匹配的 {tail_n} 个编码里,"
                f"尾号规则有 **{tail_wrong} 个({tail_wrong/tail_n*100:.0f}%)** 指向了"
                f"和描述匹配不同的目标编码。")
            say()
            say("实例:`666-6042 REFL PAV MRK TY I (W)12\"(SLD)` 的尾号对应项是 "
                "`666-7042 REFL PAV MRK TY I (W)(ARROW)` —— 一个是 12 英寸实线、"
                "一个是箭头符号;`677-6006 (18\")` 对应到 `677-7006 (12\")`。"
                "两者都不会报错,但会把不同的活当成同一个活。")
            say()
            say("**所以本表只用描述匹配,不用尾号。**")
            say()
        say("抽样:")
        say()
        say(md(cw.head(12)[["code_6xxx", "code_7xxx", "desc_6xxx", "desc_7xxx",
                            "unit_a", "match_rule"]]))
        say()
        say("**不要直接按 bid_code join 历史和现行数据** —— 匹配率近零且不报错。"
            "要么经这张对照表,要么按 `desc_norm` + `unit` join。")
        say()

    os.makedirs(REPORTS, exist_ok=True)
    with open(os.path.join(REPORTS, "competition_discount.md"), "w",
              encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")
    print("\n-> reports/competition_discount.md")


if __name__ == "__main__":
    main()
