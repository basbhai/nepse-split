#!/usr/bin/env python3
"""
analysis/decision_context_study.py — study dataset: the market state around every BUY/WAIT/AVOID decision.

One row per market_log decision, with
  * NEPSE / Sensitive / Float index indicators and market breadth, as known BEFORE the decision day
  * the decision stock's SECTOR sub-index (built from price_history + share_sectors) indicators + sector breadth
  * stock-level context (momentum, relative strength vs sector / NEPSE, volume ratio)
  * forward outcomes (1/5/10/17 trading days) for the stock, NEPSE and the sector, and alpha vs each
Outputs (output/study/): decision_context.csv, decision_context_summary.json, decision_context_summary.md.
Read-only on the database. The `council_text` field of the JSON is what monthly_council attaches.

Method notes (also in the .md):
  * Trading days = days the NEPSE index actually moved. price_history carries copied rows on weekends/holidays
    (~346-365 days/yr vs ~230 real), so calendar rows are NOT used.
  * Features use data up to the trading day BEFORE the decision (decisions are made intraday, the day's close is
    not known). Forward returns start from the close of the decision day (entry proxy).
  * Sector sub-index = median daily return of member equities (|ret|<=11%, >=3 members), chained. Bonus/right
    adjustments are not handled; windows with a |daily move|>11% are flagged `fwd_suspect_H` and excluded.
  * Statistics use only the first decision of each (symbol, action) chain (gap >5 trading days starts a new chain),
    because repeat flags are not independent. Spearman p-values get a Benjamini-Hochberg correction.
Usage: python -m analysis.decision_context_study
"""
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output" / "study"
HORIZONS = (1, 5, 10, 17)
START = "2022-07-01"          # price_history is missing 2021-07..2022-06
MIN_BUCKET_N, MIN_CORR_N = 8, 25
IDX = {"58": "nepse_", "57": "sens_", "62": "float_"}


def _q(sql: str, params: tuple = ()) -> pd.DataFrame:
    from db.connection import _db
    with _db() as cur:
        cur.execute(sql, params)
        return pd.DataFrame([dict(r) for r in cur.fetchall()])


def _num(s):
    return pd.to_numeric(s.astype(str).str.replace(",", "", regex=False), errors="coerce")


def tech(s: pd.Series, pre: str) -> pd.DataFrame:
    """Indicator frame for a level series, indexed by trading date."""
    r, f = s.pct_change(), pd.DataFrame(index=s.index)
    f[pre + "ret_1"], f[pre + "ret_5"], f[pre + "ret_20"] = r * 100, s.pct_change(5) * 100, s.pct_change(20) * 100
    for n in (20, 50, 200):
        f[f"{pre}gap_ema{n}"] = (s / s.ewm(span=n, adjust=False).mean() - 1) * 100
    d = s.diff()
    up, dn = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(), (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    f[pre + "rsi14"] = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    macd = s.ewm(span=12, adjust=False).mean() - s.ewm(span=26, adjust=False).mean()
    f[pre + "macd_hist_pct"] = (macd - macd.ewm(span=9, adjust=False).mean()) / s * 100
    f[pre + "vol20"] = r.rolling(20).std() * 100
    f[pre + "dd250"] = (s / s.rolling(250, min_periods=60).max() - 1) * 100
    return f


def build() -> tuple[pd.DataFrame, dict, dict]:
    # ── trading-day calendar from the NEPSE index ──────────────────────────────
    ix = _q("SELECT date, index_id, current_value, turnover FROM nepse_indices WHERE index_id IN ('58','57','62') AND date >= %s ORDER BY date", (START,))
    ix["date"], ix["v"] = pd.to_datetime(ix["date"].str[:10]), _num(ix["current_value"])
    n58 = ix[ix.index_id == "58"].drop_duplicates("date").set_index("date").sort_index()
    days = pd.DatetimeIndex(n58.index[n58["v"].diff().fillna(1) != 0])
    # ── index features ────────────────────────────────────────────────────────
    feats = []
    for iid, pre in IDX.items():
        s = ix[ix.index_id == iid].drop_duplicates("date").set_index("date")["v"].reindex(days).ffill()
        feats.append(tech(s, pre))
    mkt = pd.concat(feats, axis=1)
    mkt["float_minus_nepse_ret20"] = mkt["float_ret_20"] - mkt["nepse_ret_20"]
    mkt["sens_minus_nepse_ret20"] = mkt["sens_ret_20"] - mkt["nepse_ret_20"]
    to = _num(n58["turnover"]).reindex(days)
    mkt["nepse_turnover_ratio"] = to / to.rolling(20).mean()
    br = _q("SELECT date, advancing, declining FROM market_breadth WHERE date >= %s ORDER BY date", (START,))
    br["date"] = pd.to_datetime(br["date"].str[:10])
    br = br.drop_duplicates("date").set_index("date")
    ar = (_num(br["advancing"]) / (_num(br["advancing"]) + _num(br["declining"]))).reindex(days)
    mkt["adv_ratio_1d"], mkt["adv_ratio_5d"] = ar, ar.rolling(5, min_periods=3).mean()
    nep_level = n58["v"].reindex(days).ffill()

    # ── prices, sectors ───────────────────────────────────────────────────────
    sec = _q("SELECT symbol, sectorname FROM share_sectors WHERE instrumenttype='Equity'")
    sec_of = dict(zip(sec["symbol"], sec["sectorname"].str.strip()))
    ph = _q("SELECT date, symbol, close, volume FROM price_history WHERE date >= %s", (START,))
    ph["date"] = pd.to_datetime(ph["date"].str[:10])
    ph = ph[ph["date"].isin(days) & ph["symbol"].isin(sec_of)]
    ph["close"], ph["volume"] = _num(ph["close"]), _num(ph["volume"])
    ph = ph.drop_duplicates(["date", "symbol"], keep="last")
    close = ph.pivot(index="date", columns="symbol", values="close").reindex(days)
    vol = ph.pivot(index="date", columns="symbol", values="volume").reindex(days)
    ret = (close / close.shift(1) - 1).where(vol > 0)
    ema20 = close.ffill().ewm(span=20, adjust=False).mean()
    sec_feat, sec_level = {}, {}
    for name in sorted(set(sec_of.values())):
        mem = [c for c in close.columns if sec_of.get(c) == name]
        if len(mem) < 3:
            continue
        r = ret[mem].where(ret[mem].abs() <= 0.11)
        sr = r.median(axis=1).where(r.notna().sum(axis=1) >= 3)
        lvl = (1 + sr.fillna(0)).cumprod() * 100
        f = tech(lvl, "sec_")
        f["sec_pct_above_ema20"] = (close[mem] > ema20[mem]).where(close[mem].notna()).mean(axis=1) * 100
        f["sec_pct_adv"] = (r > 0).sum(axis=1) / r.notna().sum(axis=1).replace(0, np.nan) * 100
        f["sec_n_members"] = r.notna().sum(axis=1)
        sec_feat[name], sec_level[name] = f, lvl

    # ── decisions ─────────────────────────────────────────────────────────────
    d = _q("SELECT id, date, time, symbol, sector, action, confidence, primary_signal, engine_source, outcome, conf_score, wait_condition "
           "FROM market_log WHERE action IN ('BUY','WAIT','AVOID') ORDER BY date, id")
    d["date"] = pd.to_datetime(d["date"].str[:10])
    d["sector_map"] = d["symbol"].map(sec_of)
    cf = close.ffill()
    rows = []
    for _, x in d.iterrows():
        e = int(days.searchsorted(x["date"]))                        # first trading day >= decision date
        row = x.to_dict() | {"date": x["date"].date().isoformat(), "entry_day": str(days[e].date()) if e < len(days) else "",
                             "date_is_trading_day": bool(e < len(days) and days[e] == x["date"]), "no_data": e >= len(days) or e < 1}
        if row["no_data"]:
            rows.append(row); continue
        a = days[e - 1]                                              # features as of the prior trading day
        row["asof_day"] = str(a.date())
        row.update(mkt.loc[a].to_dict())
        sname, sym = x["sector_map"], x["symbol"]
        if sname in sec_feat:
            row.update(sec_feat[sname].loc[a].to_dict())
        if sym in close.columns:
            c, v = cf[sym], vol[sym]
            row["stock_ret_5"], row["stock_ret_20"] = (c.iloc[e - 1] / c.iloc[max(e - 6, 0)] - 1) * 100, (c.iloc[e - 1] / c.iloc[max(e - 21, 0)] - 1) * 100
            row["stock_gap_ema20"] = (c.iloc[e - 1] / ema20[sym].iloc[e - 1] - 1) * 100
            row["stock_vol_ratio"] = v.iloc[e - 1] / v.iloc[max(e - 21, 0):e - 1].mean() if v.iloc[max(e - 21, 0):e - 1].mean() > 0 else np.nan
            if "sec_ret_20" in row:
                row["stock_rel20_vs_sec"] = row["stock_ret_20"] - row["sec_ret_20"]
            row["stock_rel20_vs_nepse"] = row["stock_ret_20"] - row["nepse_ret_20"]
            for H in HORIZONS:
                if e + H >= len(days):
                    continue
                p0, p1 = c.iloc[e], c.iloc[e + H]
                row[f"fwd_stock_{H}"] = (p1 / p0 - 1) * 100 if p0 > 0 and p1 > 0 else np.nan
                row[f"fwd_suspect_{H}"] = bool((ret[sym].iloc[e + 1:e + H + 1].abs() > 0.11).any())
                row[f"fwd_nepse_{H}"] = (nep_level.iloc[e + H] / nep_level.iloc[e] - 1) * 100
                if sname in sec_level:
                    lv = sec_level[sname]
                    row[f"fwd_sector_{H}"] = (lv.iloc[e + H] / lv.iloc[e] - 1) * 100
                row[f"alpha_nepse_{H}"] = row[f"fwd_stock_{H}"] - row[f"fwd_nepse_{H}"]
                if f"fwd_sector_{H}" in row:
                    row[f"alpha_sector_{H}"] = row[f"fwd_stock_{H}"] - row[f"fwd_sector_{H}"]
        row["_e"] = e
        rows.append(row)
    df = pd.DataFrame(rows).sort_values(["symbol", "action", "_e"], na_position="last")
    gap = df.groupby(["symbol", "action"])["_e"].diff()
    df["first_in_chain"] = gap.isna() | (gap > 5)
    df = df.drop(columns="_e").sort_values(["date", "id"]).reset_index(drop=True)
    meta = {"trading_days": len(days), "first_day": str(days[0].date()), "last_day": str(days[-1].date()),
            "sectors": sorted(sec_feat), "equities": int(close.shape[1])}
    return df, meta, sec_level


def _bh(p: list[float]) -> list[float]:
    p = np.asarray(p, float); n = len(p); o = np.argsort(p)
    q = np.empty(n); prev = 1.0
    for rank, i in enumerate(o[::-1]):
        prev = min(prev, p[i] * n / (n - rank)); q[i] = prev
    return q.tolist()


def _shift_test(x, y, n=2000, seed=0) -> tuple[float, float]:
    """Spearman rho with a circular-shift permutation p-value (keeps each series' autocorrelation)."""
    rx, ry = stats.rankdata(x), stats.rankdata(y)
    obs = float(np.corrcoef(rx, ry)[0, 1]); L = len(rx); lo = max(10, int(0.05 * L))
    sh = np.random.default_rng(seed).integers(lo, L - lo, n)
    null = np.array([np.corrcoef(rx, np.roll(ry, k))[0, 1] for k in sh])
    return obs, float((np.sum(np.abs(null) >= abs(obs)) + 1) / (n + 1))


def _index_series() -> dict:
    """NEPSE/Sensitive/Float level series from 2011 with copied (unchanged) rows dropped."""
    hist = _q("SELECT date, index_id, current_value FROM nepse_indices WHERE index_id IN ('58','57','62') ORDER BY date")
    hist["date"], hist["v"] = pd.to_datetime(hist["date"].str[:10]), _num(hist["current_value"])
    series = {}
    for iid, pre in IDX.items():
        s = hist[hist.index_id == iid].drop_duplicates("date").set_index("date")["v"].sort_index()
        series[pre.rstrip("_")] = s[s.diff().fillna(1) != 0]                      # drop copied (unchanged) rows
    return series


def _index_tests(series: dict, split="2024-01-01") -> list[dict]:
    """Does an index's own indicator state predict its next-H-day return? All trading days, no decision selection.
    Circular-shift p, BH over all tests, pre/post-2024 sign check. Sector indexes only exist from START."""
    res = []
    for name, lvl in series.items():
        f = tech(lvl, "")
        for H in (5, 10, 20):
            fw = (lvl.shift(-H) / lvl - 1) * 100
            for col in f.columns:
                z = pd.concat([f[col], fw], axis=1, keys=["x", "y"]).dropna()
                z = z.iloc[250:]                                                  # EMA/vol warm-up
                if len(z) < 150 or z.x.nunique() < 5:
                    continue
                rho, p = _shift_test(z.x.values, z.y.values, n=1000)
                pre, post = z[z.index < split], z[z.index >= split]
                same = None
                if len(pre) > 60 and len(post) > 60:
                    r1 = stats.spearmanr(pre.x, pre.y)[0]; r2 = stats.spearmanr(post.x, post.y)[0]
                    same = bool(np.sign(r1) == np.sign(r2))
                res.append({"index": name, "horizon": H, "feature": col, "n": int(len(z)), "rho": round(rho, 3), "p": p, "oos_same_sign": same})
    for t, q in zip(res, _bh([t["p"] for t in res])):
        t["q_bh"] = round(q, 3); t["p"] = round(t["p"], 4)
    return res


def summarize(df: pd.DataFrame, meta: dict, idx_tests: list[dict]) -> dict:
    ok = df[~df["no_data"] & df["first_in_chain"]]
    feat_cols = [c for c in df.columns if c.startswith(("nepse_", "sens_", "float_", "sec_", "stock_", "adv_", "float_minus", "sens_minus")) and df[c].dtype != object]
    out = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "meta": meta, "n_decisions": int(len(df)),
           "n_first_in_chain": int(df["first_in_chain"].sum()), "by_action": {}, "buckets": [], "correlations": []}
    for act, g in df.groupby("action"):
        o = g["outcome"].value_counts().to_dict()
        gg = ok[ok.action == act]
        out["by_action"][act] = {"n": int(len(g)), "first_in_chain": int((g.first_in_chain).sum()), "outcomes": o,
                                 **{f"mean_alpha_nepse_{H}": round(float(gg[f"alpha_nepse_{H}"].where(~gg[f"fwd_suspect_{H}"].fillna(False).astype(bool)).mean()), 2) for H in (5, 10, 17) if f"alpha_nepse_{H}" in gg}}
    # regime buckets (tercile cut points computed over all first-in-chain decisions)
    for col in ["nepse_gap_ema200", "nepse_ret_20", "nepse_rsi14", "nepse_vol20", "adv_ratio_5d", "sec_ret_20", "sec_pct_above_ema20", "stock_rel20_vs_sec", "stock_vol_ratio"]:
        if col not in ok or ok[col].notna().sum() < 30:
            continue
        b = pd.qcut(ok[col], 3, labels=["low", "mid", "high"], duplicates="drop") if col != "nepse_gap_ema200" else np.where(ok[col] >= 0, "above", "below")
        for act in ok.action.unique():
            for lab in pd.unique(b):
                m = ok[(ok.action == act) & (np.asarray(b) == lab) & ~ok["fwd_suspect_10"].fillna(True).astype(bool)]
                m = m[m["alpha_nepse_10"].notna()]
                if len(m) >= MIN_BUCKET_N:
                    out["buckets"].append({"feature": col, "bucket": str(lab), "action": act, "n": int(len(m)),
                                           "mean_alpha_nepse_10": round(float(m.alpha_nepse_10.mean()), 2),
                                           "hit_rate_alpha_pos": round(float((m.alpha_nepse_10 > 0).mean()), 2),
                                           "mean_fwd_stock_10": round(float(m.fwd_stock_10.mean()), 2)})
    # Feature -> alpha tests. Market-level features are identical for every decision on a day, and horizons overlap,
    # so they are tested on ONE value per day with a circular-shift permutation p-value (autocorrelation-safe).
    # Stock/sector features are demeaned within each decision day (cross-sectional test) before Spearman.
    mkt_cols = [c for c in feat_cols if c.startswith(("nepse_", "sens_", "float_", "adv_"))]
    tests = []
    for act in ["WAIT", "AVOID", "BUY"]:
        for H in (5, 10, 17):
            ac = f"alpha_nepse_{H}"
            if ac not in ok:
                continue
            t = ok[(ok.action == act) & ~ok[f"fwd_suspect_{H}"].fillna(True).astype(bool)].dropna(subset=[ac])
            for col in feat_cols:
                if col in mkt_cols:
                    z = t.groupby("entry_day")[[col, ac]].mean().dropna().sort_index()
                    if len(z) >= MIN_CORR_N and z[col].nunique() > 3:
                        rho, p = _shift_test(z[col].values, z[ac].values)
                        tests.append({"action": act, "horizon": H, "feature": col, "level": "market(per-day)", "n": int(len(z)), "rho": round(rho, 3), "p": p})
                else:
                    z = t[["entry_day", col, ac]].dropna()
                    z = z[z.groupby("entry_day")[col].transform("size") >= 2]
                    if len(z) >= MIN_CORR_N and z[col].nunique() > 3:
                        x = z[col] - z.groupby("entry_day")[col].transform("mean"); y = z[ac] - z.groupby("entry_day")[ac].transform("mean")
                        rho, p = stats.spearmanr(x, y)
                        tests.append({"action": act, "horizon": H, "feature": col, "level": "within-day", "n": int(len(z)), "rho": round(float(rho), 3), "p": float(p)})
    if tests:
        for t, q in zip(tests, _bh([t["p"] for t in tests])):
            t["q_bh"] = round(q, 3); t["p"] = round(t["p"], 4)
    out["n_tests"] = len(tests)
    out["correlations"] = sorted(tests, key=lambda t: t["p"])[:15]
    out["n_significant_q05"] = sum(1 for t in tests if t["q_bh"] < 0.05)
    out["index_predictability"] = sorted(idx_tests, key=lambda t: t["p"])[:20]
    out["n_index_tests"] = len(idx_tests)
    out["n_index_significant_q05"] = sum(1 for t in idx_tests if t["q_bh"] < 0.05)
    out["n_index_significant_and_oos_consistent"] = sum(1 for t in idx_tests if t["q_bh"] < 0.05 and t["oos_same_sign"])
    out["council_text"] = _council_text(out)
    return out


def _council_text(s: dict) -> str:
    a = s["by_action"]
    lines = [f"Decision-context study ({s['generated']}): {s['n_decisions']} decisions, {s['n_first_in_chain']} independent (first of each symbol/action chain).",
             "Mean 10d alpha vs NEPSE (first-in-chain): " + ", ".join(f"{k} {v.get('mean_alpha_nepse_10', 'n/a')}% (n={v['first_in_chain']})" for k, v in a.items()) + ".",
             f"{s['n_tests']} feature-vs-alpha tests on decisions; {s['n_significant_q05']} survive Benjamini-Hochberg q<0.05 (market-level features tested per-day with circular-shift p; stock/sector features within-day).",
             f"Index predictability (own indicators vs next 5/10/20d return, all trading days, NEPSE/Sensitive/Float since 2011, sector indexes since 2022-07): {s['n_index_tests']} tests, {s['n_index_significant_q05']} q<0.05, {s['n_index_significant_and_oos_consistent']} also same-sign before/after 2024."]
    for t in s["correlations"][:3]:
        lines.append(f"  top decision test: {t['action']} {t['feature']} vs {t['horizon']}d alpha rho={t['rho']} n={t['n']} q={t['q_bh']}")
    for t in s["index_predictability"][:3]:
        lines.append(f"  top index test: {t['index']} {t['feature']} vs next {t['horizon']}d return rho={t['rho']} n={t['n']} q={t['q_bh']} oos_same_sign={t['oos_same_sign']}")
    best = sorted(s["buckets"], key=lambda b: -abs(b["mean_alpha_nepse_10"]))[:3]
    for b in best:
        lines.append(f"  bucket: {b['action']} when {b['feature']}={b['bucket']}: 10d alpha {b['mean_alpha_nepse_10']}% hit {b['hit_rate_alpha_pos']} n={b['n']}")
    lines.append("Caveat: small samples, intraday decisions, no bonus/right adjustment; treat patterns as hypotheses, not findings.")
    return "\n".join(lines)


def write(df: pd.DataFrame, s: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "decision_context.csv", index=False)
    (OUT / "decision_context_summary.json").write_text(json.dumps(s, indent=2, default=str))
    md = [f"# Decision-context study — {s['generated']}", "", "```", s["council_text"], "```", "",
          f"Trading days {s['meta']['first_day']} to {s['meta']['last_day']} ({s['meta']['trading_days']}); {s['meta']['equities']} equities; sectors: {', '.join(s['meta']['sectors'])}.", "",
          "## By action", "", "| action | n | first-in-chain | mean 5d | mean 10d | mean 17d alpha vs NEPSE | outcomes |", "|---|---|---|---|---|---|---|"]
    for k, v in s["by_action"].items():
        md.append(f"| {k} | {v['n']} | {v['first_in_chain']} | {v.get('mean_alpha_nepse_5')} | {v.get('mean_alpha_nepse_10')} | {v.get('mean_alpha_nepse_17')} | {v['outcomes']} |")
    md += ["", "## Regime buckets (10d alpha vs NEPSE, first-in-chain)", "", "| action | feature | bucket | n | mean alpha | hit | mean fwd |", "|---|---|---|---|---|---|---|"]
    md += [f"| {b['action']} | {b['feature']} | {b['bucket']} | {b['n']} | {b['mean_alpha_nepse_10']} | {b['hit_rate_alpha_pos']} | {b['mean_fwd_stock_10']} |" for b in sorted(s["buckets"], key=lambda b: (b["action"], b["feature"], b["bucket"]))]
    md += ["", f"## Top Spearman (of {s['n_tests']} tests; BH-corrected)", "", "| action | horizon | feature | n | rho | p | q |", "|---|---|---|---|---|---|---|"]
    md += [f"| {t['action']} | {t['horizon']}d | {t['feature']} | {t['n']} | {t['rho']} | {t['p']} | {t['q_bh']} |" for t in s["correlations"]]
    md += ["", f"## Index predictability (own indicators -> next-H-day return; {s['n_index_tests']} tests, top 20 by p)", "", "| index | horizon | feature | n | rho | p | q | pre/post-2024 same sign |", "|---|---|---|---|---|---|---|---|"]
    md += [f"| {t['index']} | {t['horizon']}d | {t['feature']} | {t['n']} | {t['rho']} | {t['p']} | {t['q_bh']} | {t['oos_same_sign']} |" for t in s["index_predictability"]]
    md += ["", "## Columns (decision_context.csv)", "",
           "- `nepse_*`, `sens_*`, `float_*`: ret_1/5/20 (%), gap_ema20/50/200 (% above/below EMA), rsi14, macd_hist_pct, vol20 (daily std %), dd250 (% from 250d high). `adv_ratio_1d/5d`, `nepse_turnover_ratio`, `float_minus_nepse_ret20`.",
           "- `sec_*`: same indicators on the stock's constructed sector index, plus `sec_pct_above_ema20`, `sec_pct_adv`, `sec_n_members`.",
           "- `stock_*`: ret_5/20, gap_ema20, vol_ratio, rel20_vs_sec, rel20_vs_nepse.",
           "- `fwd_stock_H`, `fwd_nepse_H`, `fwd_sector_H`, `alpha_nepse_H`, `alpha_sector_H` for H in 1,5,10,17 trading days from the decision-day close; `fwd_suspect_H` = a >11% daily move in the window (likely bonus/right adjustment).",
           "- `first_in_chain`: first decision for this symbol+action after a >5-trading-day gap. Use it for any statistics.", "",
           "Caveats: decisions are intraday while features stop at the prior close; entry proxy is the decision-day close; sector indexes are equal-weight medians, not official; sample sizes are small, so every pattern is a hypothesis."]
    (OUT / "decision_context_summary.md").write_text("\n".join(md))


if __name__ == "__main__":
    df, meta, sec_level = build()
    series = _index_series()
    series.update({"sector:" + k: v[v.diff().fillna(1) != 0] for k, v in sec_level.items()})
    s = summarize(df, meta, _index_tests(series))
    write(df, s)
    print(s["council_text"]); print("\nwritten to", OUT)
