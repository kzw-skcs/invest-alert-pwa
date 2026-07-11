# -*- coding: utf-8 -*-
"""
fundamentals.py v2 — ファンダメンタル品質スコア(Quality 0-100)の週次算出
データ源: SEC EDGAR 公式API(無料・安定・クラウドIPからも利用可)
  - https://www.sec.gov/files/company_tickers.json (ティッカー→CIK)
  - https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (XBRL財務データ)
株価はYahoo chart API(evaluate.pyと同経路・動作実績あり)からPER/PSR計算に使用。

年次(10-K/20-F/40-F)ベース: ROE・営業利益率・売上/純利益成長率・FCFマージン・実績PER・PSR。
対象外(暗号資産・金銀・SEC非提出企業)は quality なしとして安全にスキップ。
"""
import json
import os
import time
import urllib.request
from datetime import datetime, timezone, timedelta

import evaluate

BASE = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))
# SECはUser-Agentに連絡先を要求
SEC_UA = {"User-Agent": "invest-alert-pwa/2.0 (kazawa@skclinicalsupport.com)"}

TAGS = {
    "rev": [("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
            ("us-gaap", "Revenues"), ("us-gaap", "SalesRevenueNet"),
            ("ifrs-full", "Revenue")],
    "ni": [("us-gaap", "NetIncomeLoss"),
           ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"),
           ("ifrs-full", "ProfitLoss")],
    "eq": [("us-gaap", "StockholdersEquity"),
           ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
           ("ifrs-full", "EquityAttributableToOwnersOfParent"), ("ifrs-full", "Equity")],
    "op": [("us-gaap", "OperatingIncomeLoss"),
           ("ifrs-full", "ProfitLossFromOperatingActivities")],
    "ocf": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
            ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
    "capex": [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
              ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities")],
    "eps": [("us-gaap", "EarningsPerShareDiluted"),
            ("ifrs-full", "DilutedEarningsLossPerShare")],
    # v3.28: Alpha Finance / Alpha Management 定量評価用
    "debt": [("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebt"),
             ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
             ("ifrs-full", "NoncurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
             ("ifrs-full", "CashAndCashEquivalents")],
    "dep": [("us-gaap", "DepreciationDepletionAndAmortization"),
            ("us-gaap", "DepreciationAndAmortization"),
            ("ifrs-full", "DepreciationAndAmortisationExpense")],
    "intexp": [("us-gaap", "InterestExpense"), ("us-gaap", "InterestExpenseNonoperating"),
               ("us-gaap", "InterestExpenseDebt"),
               ("ifrs-full", "InterestExpenseOnBorrowings")],
    "goodwill": [("us-gaap", "Goodwill"), ("ifrs-full", "Goodwill")],
    "assets": [("us-gaap", "Assets"), ("ifrs-full", "Assets")],
    "sbc": [("us-gaap", "ShareBasedCompensation")],
    "buyback": [("us-gaap", "PaymentsForRepurchaseOfCommonStock")],
    "curA": [("us-gaap", "AssetsCurrent"), ("ifrs-full", "CurrentAssets")],
    "curL": [("us-gaap", "LiabilitiesCurrent"), ("ifrs-full", "CurrentLiabilities")],
    "shares": [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
               ("us-gaap", "CommonStockSharesOutstanding"),
               ("dei", "EntityCommonStockSharesOutstanding")],
}


def sec_json(url, retries=3):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=SEC_UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e
            print(f"  sec_json 失敗({i + 1}/{retries + 1}) {url.split('/')[-1]}: {e}")
            time.sleep(3 * (i + 1))
    raise last


def cik_map():
    for url in ("https://www.sec.gov/files/company_tickers.json",
                "https://www.sec.gov/files/company_tickers_exchange.json"):
        try:
            j = sec_json(url)
            if "fields" in j and "data" in j:  # exchange形式
                ti = j["fields"].index("ticker"); ci = j["fields"].index("cik")
                return {row[ti].upper(): int(row[ci]) for row in j["data"] if row[ti]}
            return {v["ticker"].upper(): int(v["cik_str"]) for v in j.values()}
        except Exception as e:
            print(f"CIKマップ取得失敗 {url}: {e}")
    return {}


def annual_series(facts, key, n=3):
    """年次(FY)の値を新しい順で最大n件返す。
    全候補タグを走査し「最新の決算期を持つ系列」を採用する
    (タグの世代交代で古いタグに古いデータだけが残っているケースを回避)。"""
    best = []
    for tax, tag in TAGS[key]:
        node = facts.get("facts", {}).get(tax, {}).get(tag)
        if not node:
            continue
        for unit, arr in node.get("units", {}).items():
            rows = [x for x in arr
                    if x.get("form") in ("10-K", "20-F", "40-F")
                    and x.get("fp") == "FY" and x.get("val") is not None
                    and x.get("end")]
            if not rows:
                continue
            uniq = {}
            for x in rows:
                uniq[x["end"]] = x["val"]
            ends = sorted(uniq.keys(), reverse=True)
            series = [(e, uniq[e]) for e in ends[:n]]
            if not best or series[0][0] > best[0][0]:
                best = series
    return best


def latest_shares(facts):
    node = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if not node:
        return None
    best = None
    for unit, arr in node.get("units", {}).items():
        for x in arr:
            if x.get("val") and x.get("end"):
                if best is None or x["end"] > best[0]:
                    best = (x["end"], x["val"])
    return best[1] if best else None


def growth(series):
    if len(series) < 2 or not series[1][1]:
        return None
    return series[0][1] / series[1][1] - 1


def build_metrics(facts, price):
    rev = annual_series(facts, "rev")
    ni = annual_series(facts, "ni")
    eq = annual_series(facts, "eq")
    op = annual_series(facts, "op")
    ocf = annual_series(facts, "ocf", n=6)
    capex = annual_series(facts, "capex", n=6)
    eps = annual_series(facts, "eps")
    shares = latest_shares(facts)
    r0 = rev[0][1] if rev else None
    m = {
        "roe": (ni[0][1] / eq[0][1]) if (ni and eq and eq[0][1]) else None,
        "opMargin": (op[0][1] / r0) if (op and r0) else None,
        "revGrowth": growth(rev),
        "epsGrowth": growth(ni),  # 純利益成長で代替(表示名も純利益成長)
        "fcfMargin": ((ocf[0][1] - (capex[0][1] if capex else 0)) / r0) if (ocf and r0) else None,
        "fwdPE": None,
        "trailPE": (price / eps[0][1]) if (price and eps and eps[0][1] and eps[0][1] > 0) else None,
        "psr": (price * shares / r0) if (price and shares and r0) else None,
        "fyEnd": rev[0][0] if rev else None,
        "shares": shares,
    }
    # ---- v3.28: Alpha Finance / Management 定量素材 ----
    debt = annual_series(facts, "debt")
    cash = annual_series(facts, "cash")
    dep = annual_series(facts, "dep")
    intexp = annual_series(facts, "intexp")
    goodwill = annual_series(facts, "goodwill")
    assets = annual_series(facts, "assets")
    sbc = annual_series(facts, "sbc")
    buyback = annual_series(facts, "buyback")
    cur_a = annual_series(facts, "curA")
    cur_l = annual_series(facts, "curL")
    sh_hist = annual_series(facts, "shares", n=6)
    ebitda = (op[0][1] + (dep[0][1] if dep else 0)) if op else None
    net_debt = ((debt[0][1] if debt else 0) - (cash[0][1] if cash else 0)) if (debt or cash) else None
    m["netDebtEbitda"] = (net_debt / ebitda) if (net_debt is not None and ebitda and ebitda > 0) else None
    m["netCash"] = (net_debt is not None and net_debt < 0)
    m["interestCoverage"] = (op[0][1] / intexp[0][1]) if (op and intexp and intexp[0][1] and intexp[0][1] > 0) else None
    m["currentRatio"] = (cur_a[0][1] / cur_l[0][1]) if (cur_a and cur_l and cur_l[0][1]) else None
    # FCF黒字年数(直近5期)
    fcf_years = []
    capex_map = {d: v for d, v in (capex or [])}
    for d, v in (ocf or [])[:5]:
        fcf_years.append(v - capex_map.get(d, 0))
    m["fcfPosYears"] = sum(1 for f in fcf_years if f > 0) if fcf_years else None
    m["fcfYears"] = len(fcf_years)
    m["fcfLatest"] = fcf_years[0] if fcf_years else None
    # 希薄化/自社株買い(5年株式数変化。負=買い戻し超過=株主に優しい)
    if sh_hist and len(sh_hist) >= 3 and sh_hist[-1][1]:
        m["sharesChg5yPct"] = (sh_hist[0][1] / sh_hist[-1][1] - 1) * 100
    else:
        m["sharesChg5yPct"] = None
    m["sbcToRev"] = (sbc[0][1] / r0) if (sbc and r0) else None
    m["goodwillToAssets"] = (goodwill[0][1] / assets[0][1]) if (goodwill and assets and assets[0][1]) else None
    m["buybackToRev"] = (buyback[0][1] / r0) if (buyback and r0) else None
    # ROIC近似 = 税引後営業利益(税率21%仮定) / (自己資本+有利子負債)
    inv_cap = ((eq[0][1] if eq else 0) + (debt[0][1] if debt else 0)) if (eq or debt) else None
    m["roicApprox"] = (op[0][1] * 0.79 / inv_cap) if (op and inv_cap and inv_cap > 0) else None
    return m


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def scale(v, worst, best, points):
    if v is None:
        return None
    if best > worst:
        return clamp((v - worst) / (best - worst), 0, 1) * points
    return clamp((worst - v) / (worst - best), 0, 1) * points


def quality_score(m):
    parts = {}
    warnings = []
    p1 = [s for s in (scale(m.get("roe"), 0.0, 0.25, 12.5),
                      scale(m.get("opMargin"), 0.0, 0.25, 12.5)) if s is not None]
    parts["profit"] = round(sum(p1) / len(p1) * 2, 1) if p1 else None
    p2 = [s for s in (scale(m.get("revGrowth"), -0.05, 0.25, 12.5),
                      scale(m.get("epsGrowth"), -0.05, 0.30, 12.5)) if s is not None]
    parts["growth"] = round(sum(p2) / len(p2) * 2, 1) if p2 else None
    p3 = [s for s in (scale(m.get("fcfMargin"), 0.0, 0.20, 25),) if s is not None]
    parts["health"] = round(sum(p3) / len(p3), 1) if p3 else None
    pe = m.get("fwdPE") or m.get("trailPE")
    if pe is not None and pe > 0:
        parts["valuation"] = 25 if pe < 15 else 18 if pe < 22 else 12 if pe < 32 else 6 if pe < 50 else 2
    elif m.get("psr") is not None:
        psr = m["psr"]
        parts["valuation"] = 22 if psr < 3 else 14 if psr < 8 else 6 if psr < 15 else 2
    else:
        parts["valuation"] = None
    avail = [v for v in parts.values() if v is not None]
    if len(avail) < 2:
        return None, parts, ["データ不足"]
    score = round(sum(avail) / (len(avail) * 25) * 100)
    if m.get("revGrowth") is not None and m["revGrowth"] < 0:
        warnings.append("売上成長マイナス")
    if m.get("fcfMargin") is not None and m["fcfMargin"] < 0:
        warnings.append("FCF赤字")
    return score, parts, warnings


# ---------------------------------------------------------------- v3.28: 4α自動評価(定量部)

def alpha_finance_score(m, is_financial=False):
    """Alpha Finance(0-25): 負債耐性・FCF・生存力。銀行等の金融業は純負債/EBITDAが
    無意味なため簡易モデル(ROE安定性・利益率ベース)に切り替え、その旨を明記する。"""
    parts, warns = {}, []
    if is_financial:
        roe = m.get("roe")
        parts["収益力(金融簡易)"] = 10 if (roe or 0) >= 0.12 else 7 if (roe or 0) >= 0.08 else 3 if (roe or 0) > 0 else 0
        om = m.get("opMargin")
        parts["利益率"] = 8 if (om or 0) >= 0.30 else 5 if (om or 0) >= 0.15 else 2 if (om or 0) > 0 else 0
        g = m.get("epsGrowth")
        parts["利益成長"] = 7 if (g or 0) >= 0.10 else 4 if (g or 0) >= 0 else 1
        warns.append("金融業: 簡易モデル(負債指標は業種特性上評価対象外)")
        return min(25, round(sum(parts.values()), 1)), parts, warns
    nde = m.get("netDebtEbitda")
    if m.get("netCash"):
        parts["純負債/EBITDA"] = 8.0
    elif nde is None:
        parts["純負債/EBITDA"] = None
    else:
        parts["純負債/EBITDA"] = 6 if nde < 1 else 4.5 if nde < 2 else 3 if nde < 3 else 1 if nde < 4 else 0
        if nde >= 4:
            warns.append(f"重警告: 純負債/EBITDA {nde:.1f}倍(過大な負債)")
    ic = m.get("interestCoverage")
    parts["利払い余力"] = None if ic is None else (5 if ic >= 15 else 4 if ic >= 8 else 2.5 if ic >= 4 else 1 if ic >= 2 else 0)
    if ic is not None and ic < 2:
        warns.append(f"重警告: インタレストカバレッジ{ic:.1f}倍")
    fy = m.get("fcfPosYears")
    n_fy = m.get("fcfYears") or 0
    parts["FCF黒字継続"] = None if fy is None else (6 if fy >= 5 else 4.5 if fy == 4 else 3 if fy == 3 else 1.5 if fy >= 1 else 0)
    if fy is not None and n_fy >= 3 and fy <= n_fy // 2:
        warns.append("FCF赤字が常態(生存力に疑問)")
    cr = m.get("currentRatio")
    parts["流動比率"] = None if cr is None else (3 if cr >= 1.5 else 2 if cr >= 1.0 else 1 if cr >= 0.8 else 0)
    fm = m.get("fcfMargin")
    parts["FCFマージン"] = None if fm is None else (3 if fm >= 0.15 else 2 if fm >= 0.08 else 1 if fm >= 0.03 else 0.5 if fm > 0 else 0)
    avail = {k: v for k, v in parts.items() if v is not None}
    if len(avail) < 2:
        return None, parts, ["データ不足"]
    max_map = {"純負債/EBITDA": 8, "利払い余力": 5, "FCF黒字継続": 6, "流動比率": 3, "FCFマージン": 3}
    score = sum(avail.values()) / sum(max_map[k] for k in avail) * 25
    return round(score, 1), parts, warns


def alpha_mgmt_quant(m):
    """Alpha Management定量部(0-25): 資本配分の実績のみで機械評価。
    定性(CEO経歴・評判・後継)はClaudeレビュー(alpha-review API)で補完する。"""
    parts = {}
    sc = m.get("sharesChg5yPct")
    parts["自社株買いvs希薄化(5年)"] = None if sc is None else (7 if sc <= -5 else 5 if sc <= 0 else 3 if sc <= 5 else 1 if sc <= 15 else 0)
    ro = m.get("roicApprox")
    parts["ROIC(資本効率)"] = None if ro is None else (6 if ro >= 0.20 else 4 if ro >= 0.12 else 2 if ro >= 0.06 else 0)
    sb = m.get("sbcToRev")
    parts["株式報酬の節度"] = None if sb is None else (4 if sb < 0.02 else 3 if sb < 0.05 else 1 if sb < 0.10 else 0)
    gw = m.get("goodwillToAssets")
    parts["買収規律(のれん比率)"] = None if gw is None else (4 if gw < 0.10 else 3 if gw < 0.25 else 1 if gw < 0.45 else 0)
    fy = m.get("fcfPosYears")
    parts["FCF創出の一貫性"] = None if fy is None else (4 if fy >= 4 else 2 if fy >= 2 else 0)
    avail = {k: v for k, v in parts.items() if v is not None}
    if len(avail) < 2:
        return None, parts
    max_map = {"自社株買いvs希薄化(5年)": 7, "ROIC(資本効率)": 6, "株式報酬の節度": 4,
               "買収規律(のれん比率)": 4, "FCF創出の一貫性": 4}
    score = sum(avail.values()) / sum(max_map[k] for k in avail) * 25
    return round(score, 1), parts


def dcf_value(fcf0, g, years=10, r=0.10, tg=0.03):
    """FCFのDCF現在価値(成長g→終端tg)。"""
    pv = 0.0
    f = fcf0
    for t in range(1, years + 1):
        f = f * (1 + g)
        pv += f / (1 + r) ** t
    terminal = f * (1 + tg) / (r - tg) / (1 + r) ** years
    return pv + terminal


def implied_fcf_growth(mktcap, fcf0, r=0.10, years=10, tg=0.03):
    """Reverse DCF: 現在の時価総額を正当化するのに必要なFCF成長率を二分法で逆算。"""
    if not mktcap or not fcf0 or fcf0 <= 0:
        return None
    lo, hi = -0.50, 0.80
    if dcf_value(fcf0, lo, years, r, tg) > mktcap:
        return lo
    if dcf_value(fcf0, hi, years, r, tg) < mktcap:
        return hi
    for _ in range(60):
        mid = (lo + hi) / 2
        if dcf_value(fcf0, mid, years, r, tg) < mktcap:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def alpha_money(m, price):
    """Alpha Money(0-25): 本質的価値レンジ・購入上限価格・安全余裕(Reverse DCF+3シナリオ)。
    ヒストリカル成長を保守/基本/強気に写像(基本=実績を年20%で頭打ち、保守=その半分)。
    FCF赤字・データ不足はNone(判定不能)を返す=無理にスコアを作らない。"""
    fcf = m.get("fcfLatest")
    shares = m.get("shares")
    if not fcf or fcf <= 0 or not shares or not price:
        return None
    mktcap = price * shares
    hist_g = m.get("revGrowth")
    if hist_g is None:
        hist_g = 0.05
    base_g = max(0.0, min(0.20, hist_g))
    cons_g = base_g / 2
    bull_g = min(0.30, base_g * 1.4 + 0.02)
    fair = {k: dcf_value(fcf, g) / shares for k, g in
            (("conservative", cons_g), ("base", base_g), ("bull", bull_g))}
    implied = implied_fcf_growth(mktcap, fcf)
    buy_ceiling = fair["base"] * 0.75
    sm = (fair["base"] - price) / fair["base"] if fair["base"] > 0 else None
    if sm is None:
        return None
    score = 25 if sm >= 0.40 else 20 if sm >= 0.30 else 15 if sm >= 0.20 else 10 if sm >= 0.10 else 5 if sm >= 0 else 2 if sm >= -0.15 else 0
    return {"score": score,
            "fairLow": round(fair["conservative"], 2), "fairBase": round(fair["base"], 2),
            "fairHigh": round(fair["bull"], 2), "buyCeiling": round(buy_ceiling, 2),
            "safetyMarginPct": round(sm * 100, 1),
            "impliedGrowthPct": round(implied * 100, 1) if implied is not None else None,
            "assumedBaseGrowthPct": round(base_g * 100, 1),
            "note": "簡易DCF(割引率10%・終端3%・実績成長ベース)。仮定に強く依存する参考値であり、精密な企業価値評価の代替ではない"}


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    print("CIKマップ取得中…")
    ciks = cik_map()
    if not ciks:
        print("⚠️ CIKマップが取得できませんでした。既存のfundamentals.jsonを維持して終了します。")
        return
    print(f"CIKマップ: {len(ciks)}社")
    rot_map = {s["ticker"].upper(): s.get("rotSector") for s in cfg["stocks"]}
    out = {}
    ok = fail = 0
    for st in cfg["stocks"]:
        tk = st["ticker"].upper()
        try:
            cik = ciks.get(tk)
            if not cik:
                print(f"{tk}: SEC CIKなし(スキップ)")
                continue
            facts = sec_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", retries=1)
            price = None
            try:
                hist, _ = evaluate.fetch_history({"yahoo": tk, "stooq": tk.lower() + ".us"})
                if hist:
                    price = hist[-1]["c"]
            except Exception:
                pass
            m = build_metrics(facts, price)
            score, parts, warns = quality_score(m)
            # v3.28: 4α定量評価
            is_fin = rot_map.get(tk) == "金融"
            f_score, f_parts, f_warns = alpha_finance_score(m, is_financial=is_fin)
            mg_score, mg_parts = alpha_mgmt_quant(m)
            money = alpha_money(m, price)
            disp = {k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in m.items() if v is not None and k != "shares"}
            out[tk] = {"score": score, "parts": parts, "warnings": warns + f_warns, "metrics": disp,
                       "alphas": {"finance": f_score, "financeParts": f_parts,
                                  "mgmtQuant": mg_score, "mgmtParts": mg_parts,
                                  "money": money}}
            ok += 1
            print(f"{tk}: Q={score} F={f_score} M={mg_score} $={money['score'] if money else '—'} 決算期{m.get('fyEnd')}")
        except Exception as e:
            fail += 1
            print(f"{tk}: 処理失敗(続行) {type(e).__name__}: {e}")
        time.sleep(0.2)  # SECレート制限(10req/s)への配慮
    print(f"成功{ok} / 失敗{fail}")
    if not out:
        print("⚠️ 全銘柄失敗のため既存のfundamentals.jsonを維持します(上書きしない)")
        return
    data = {"updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
            "source": "SEC EDGAR(年次) + Yahoo(株価)", "tickers": out}
    with open(os.path.join(BASE, "fundamentals.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"fundamentals.json 保存: {ok}/{len(cfg['stocks'])}銘柄")


if __name__ == "__main__":
    main()
