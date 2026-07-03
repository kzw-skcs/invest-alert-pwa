# -*- coding: utf-8 -*-
"""
fundamentals.py — ファンダメンタル品質スコア(Quality 0-100)の週次算出
Yahoo Finance quoteSummary APIから財務指標を取得し fundamentals.json に保存。
GitHub Actions の fundamentals ワークフロー(週1土曜+手動)で実行。

スコア構成(各25点):
  収益性:   ROE + 営業利益率
  成長性:   売上成長率 + 利益成長率
  財務健全: 純負債/EBITDA + FCFマージン
  価格妥当: 予想PER(絶対水準バンド。無ければPSR)
取得失敗銘柄は quality なし(=判定への補正もスキップ)として安全に扱う。
"""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

import http.cookiejar

BASE = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125 Safari/537.36")}
MODULES = "financialData,defaultKeyStatistics,summaryDetail"

# Yahooはcookie+crumb認証を要求するため、cookie jar付きopenerで取得する
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))
_CRUMB = {"v": None}


def _get_crumb(force=False):
    if _CRUMB["v"] and not force:
        return _CRUMB["v"]
    for seed in ("https://fc.yahoo.com/", "https://finance.yahoo.com/"):
        try:
            _OPENER.open(urllib.request.Request(seed, headers=UA), timeout=15).read()
        except Exception:
            pass  # fc.yahoo.comは404を返すがcookieは付与される
    r = _OPENER.open(urllib.request.Request(
        "https://query1.finance.yahoo.com/v1/test/getcrumb", headers=UA), timeout=15)
    _CRUMB["v"] = r.read().decode().strip()
    print(f"crumb取得: {'OK' if _CRUMB['v'] else '失敗'}")
    return _CRUMB["v"]


def fetch_summary(symbol):
    err = None
    for attempt in range(3):
        try:
            crumb = _get_crumb(force=(attempt > 0))
            host = "query1" if attempt % 2 == 0 else "query2"
            url = (f"https://{host}.finance.yahoo.com/v10/finance/quoteSummary/"
                   f"{urllib.parse.quote(symbol)}?modules={MODULES}"
                   f"&crumb={urllib.parse.quote(crumb or '')}")
            with _OPENER.open(urllib.request.Request(url, headers=UA), timeout=20) as r:
                j = json.loads(r.read().decode())
            return j["quoteSummary"]["result"][0]
        except Exception as e:
            err = e
            time.sleep(1.5)
    print(f"  {symbol}: 取得失敗 {err}")
    return None


def raw(d, *keys):
    """ネストされたYahooのrawフィールドを安全に取り出す"""
    for k in keys:
        if d is None:
            return None
        d = d.get(k)
    if isinstance(d, dict):
        return d.get("raw")
    return d


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def scale(v, worst, best, points):
    """worst→0点、best→満点の線形スケール"""
    if v is None:
        return None
    if best > worst:
        return clamp((v - worst) / (best - worst), 0, 1) * points
    return clamp((worst - v) / (worst - best), 0, 1) * points


def quality_score(m):
    """m: metrics dict → (score, parts, warnings)"""
    parts = {}
    warnings = []
    # 収益性 25
    p1 = []
    s = scale(m.get("roe"), 0.0, 0.25, 12.5)
    if s is not None: p1.append(s)
    s = scale(m.get("opMargin"), 0.0, 0.25, 12.5)
    if s is not None: p1.append(s)
    parts["profit"] = round(sum(p1) / len(p1) * 2, 1) if p1 else None
    # 成長性 25
    p2 = []
    s = scale(m.get("revGrowth"), -0.05, 0.25, 12.5)
    if s is not None: p2.append(s)
    s = scale(m.get("epsGrowth"), -0.05, 0.30, 12.5)
    if s is not None: p2.append(s)
    parts["growth"] = round(sum(p2) / len(p2) * 2, 1) if p2 else None
    # 財務健全 25
    p3 = []
    nd_ebitda = m.get("netDebtEbitda")
    s = scale(nd_ebitda, 4.0, 0.0, 12.5) if nd_ebitda is not None else None
    if s is not None: p3.append(s)
    s = scale(m.get("fcfMargin"), 0.0, 0.20, 12.5)
    if s is not None: p3.append(s)
    parts["health"] = round(sum(p3) / len(p3) * 2, 1) if p3 else None
    # 価格妥当 25
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
    if nd_ebitda is not None and nd_ebitda > 3.5:
        warnings.append(f"高負債(純負債/EBITDA {nd_ebitda:.1f})")
    return score, parts, warnings


def extract_metrics(res):
    fd = res.get("financialData", {})
    ks = res.get("defaultKeyStatistics", {})
    sd = res.get("summaryDetail", {})
    revenue = raw(fd, "totalRevenue")
    fcf = raw(fd, "freeCashflow")
    ebitda = raw(fd, "ebitda")
    debt = raw(fd, "totalDebt")
    cash = raw(fd, "totalCash")
    m = {
        "roe": raw(fd, "returnOnEquity"),
        "opMargin": raw(fd, "operatingMargins"),
        "grossMargin": raw(fd, "grossMargins"),
        "revGrowth": raw(fd, "revenueGrowth"),
        "epsGrowth": raw(fd, "earningsGrowth"),
        "fwdPE": raw(ks, "forwardPE") or raw(sd, "forwardPE"),
        "trailPE": raw(sd, "trailingPE"),
        "psr": raw(sd, "priceToSalesTrailing12Months"),
        "fcfMargin": (fcf / revenue) if (fcf and revenue) else None,
        "netDebtEbitda": ((debt or 0) - (cash or 0)) / ebitda if ebitda else None,
    }
    return m


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    out = {}
    ok = 0
    for st in cfg["stocks"]:
        tk = st["ticker"]
        res = fetch_summary(tk)
        if not res:
            continue
        m = extract_metrics(res)
        score, parts, warns = quality_score(m)
        disp = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items() if v is not None}
        out[tk] = {"score": score, "parts": parts, "warnings": warns, "metrics": disp}
        ok += 1
        print(f"{tk}: Q={score} {warns}")
        time.sleep(0.6)
    data = {"updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"), "tickers": out}
    with open(os.path.join(BASE, "fundamentals.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"fundamentals.json 保存: {ok}/{len(cfg['stocks'])}銘柄")


if __name__ == "__main__":
    main()
