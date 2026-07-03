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


def annual_series(facts, key):
    """年次(FY)の値を新しい順で最大3件返す。
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
            series = [(e, uniq[e]) for e in ends[:3]]
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
    ocf = annual_series(facts, "ocf")
    capex = annual_series(facts, "capex")
    eps = annual_series(facts, "eps")
    shares = latest_shares(facts)
    r0 = rev[0][1] if rev else None
    m = {
        "roe": (ni[0][1] / eq[0][1]) if (ni and eq and eq[0][1]) else None,
        "opMargin": (op[0][1] / r0) if (op and r0) else None,
        "revGrowth": growth(rev),
        "epsGrowth": growth(ni),  # 純利益成長で代替
        "fcfMargin": ((ocf[0][1] - (capex[0][1] if capex else 0)) / r0) if (ocf and r0) else None,
        "netDebtEbitda": None,  # EDGARからの安定取得が難しいため対象外
        "fwdPE": None,
        "trailPE": (price / eps[0][1]) if (price and eps and eps[0][1] and eps[0][1] > 0) else None,
        "psr": (price * shares / r0) if (price and shares and r0) else None,
        "fyEnd": rev[0][0] if rev else None,
    }
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


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    print("CIKマップ取得中…")
    ciks = cik_map()
    if not ciks:
        print("⚠️ CIKマップが取得できませんでした。既存のfundamentals.jsonを維持して終了します。")
        return
    print(f"CIKマップ: {len(ciks)}社")
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
            disp = {k: (round(v, 4) if isinstance(v, float) else v)
                    for k, v in m.items() if v is not None}
            out[tk] = {"score": score, "parts": parts, "warnings": warns, "metrics": disp}
            ok += 1
            print(f"{tk}: Q={score} 決算期{m.get('fyEnd')} {warns}")
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
