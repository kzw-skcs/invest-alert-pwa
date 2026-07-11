# -*- coding: utf-8 -*-
"""
evaluate.py v2 — 価格取得 + engine実行 + data.json更新 + シグナルログ + Web Push
GitHub Actions (daily, 毎朝6:00 JST) から実行される。
データ源: Yahoo Finance(主) → Stooq → CoinGecko(暗号資産) フォールバック。
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta

import engine

BASE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (invest-alert-pwa; personal use)"}
JST = timezone(timedelta(hours=9))


def http_json(url, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            if i == retries:
                print(f"  fetch失敗 {url}: {e}")
                return None
            time.sleep(1.5 * (i + 1))


def http_text(url, retries=2):
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            if i == retries:
                print(f"  fetch失敗 {url}: {e}")
                return None
            time.sleep(1.5 * (i + 1))


# ---------------------------------------------------------------- データ取得

def fetch_yahoo(symbol, range_="6y"):
    """Yahoo chart API → OHLCV。"""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={range_}&interval=1d&events=history")
    j = http_json(url)
    try:
        res = j["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        hist = []
        for i, t in enumerate(ts):
            c = q["close"][i]
            if c is None:
                continue
            hist.append({
                "d": datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%d"),
                "c": round(float(c), 6),
                "o": round(float(q["open"][i]), 6) if q.get("open") and q["open"][i] else None,
                "h": round(float(q["high"][i]), 6) if q["high"][i] else None,
                "l": round(float(q["low"][i]), 6) if q["low"][i] else None,
                "v": int(q["volume"][i]) if q["volume"][i] else None,
            })
        return hist if len(hist) >= 30 else None
    except Exception:
        return None


def fetch_stooq(symbol):
    txt = http_text(f"https://stooq.com/q/d/l/?s={symbol}&i=d")
    if not txt or txt.lstrip().startswith("<"):
        return None
    hist = []
    for line in txt.strip().splitlines()[1:]:
        p = line.split(",")
        if len(p) < 5:
            continue
        try:
            hist.append({"d": p[0], "c": float(p[4]), "o": float(p[1]),
                         "h": float(p[2]), "l": float(p[3]),
                         "v": int(float(p[5])) if len(p) > 5 and p[5] else None})
        except ValueError:
            continue
    return hist[-1600:] if len(hist) >= 30 else None


def fetch_coingecko(coin_id, days=1825):
    j = http_json(f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                  f"?vs_currency=usd&days={days}&interval=daily")
    try:
        prices = j["prices"]
        vols = {int(v[0] / 86400000): v[1] for v in j.get("total_volumes", [])}
        hist = []
        for ms, price in prices:
            day = int(ms / 86400000)
            hist.append({"d": datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                         "c": round(float(price), 6), "h": None, "l": None,
                         "v": int(vols.get(day, 0)) or None})
        # 同日重複除去
        seen, out = set(), []
        for h in hist:
            if h["d"] not in seen:
                seen.add(h["d"]); out.append(h)
        return out if len(out) >= 30 else None
    except Exception:
        return None


def fetch_history(entry):
    y = entry.get("yahoo") or entry.get("ticker")
    if y:
        h = fetch_yahoo(y)
        if h:
            return h, "yahoo"
    if entry.get("stooq"):
        h = fetch_stooq(entry["stooq"])
        if h:
            return h, "stooq"
    if entry.get("coingeckoId"):
        h = fetch_coingecko(entry["coingeckoId"])
        if h:
            return h, "coingecko"
    return None, None


# ---------------------------------------------------------------- 日本マテリアル店頭価格

def fetch_nihon_material():
    """金・銀の店頭価格(円/g・税込)を取得。第1ソース: 田中貴金属(サーバーレンダリング・構造確認済み)、
    予備: 日本マテリアル。損益計算には買取価格を使用。失敗時はNone(COMEX換算へフォールバック)。"""
    import re
    SANE = {"gold": (8000.0, 80000.0), "silver": (80.0, 3000.0)}
    SOURCES = (
        ("https://gold.tanaka.co.jp/commodity/souba/index.php", "田中貴金属"),
        ("https://www.material.co.jp/market.php", "日本マテリアル"),
        ("https://www.material.co.jp/buy/price.php", "日本マテリアル"),
    )
    for url, source in SOURCES:
        html_text = http_text(url)
        if not html_text or len(html_text) < 500:
            continue
        text = re.sub(r"<[^>]+>", " ", html_text)
        text = re.sub(r"\s+", " ", text)
        out = {}
        for key, jp in (("gold", "金"), ("silver", "銀")):
            vals = []
            for m in re.finditer(jp, text):
                prev = text[m.start() - 1: m.start()]
                nxt = text[m.end(): m.end() + 1]
                if key == "gold" and (prev in ("白", "料", "代", "資", "現", "貴", "純", "年", "地", "課", "税")
                                      or nxt in ("貨", "属", "融", "利")):
                    continue  # 白金・金貨・貴金属・金融などの誤検出を除外
                seg = text[m.end(): m.end() + 110]
                # 他金属のセクションに食い込まないよう区切る
                # 金はプラチナ価格(同レンジ)の混入を防ぐため区切る。銀は価格レンジで自然に分離されるため区切り不要
                stops = ("プラチナ", "銀", "白金") if key == "gold" else ()
                for sw in stops:
                    pos = seg.find(sw)
                    if pos > 0:
                        seg = seg[:pos]
                for nm in re.finditer(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*円", seg):
                    try:
                        v = float(nm.group(1).replace(",", ""))
                    except ValueError:
                        continue
                    lo, hi = SANE[key]
                    if lo <= v <= hi:
                        vals.append(v)
            if vals:
                out[key] = {"sell": max(vals), "buy": min(vals)}  # 小売>買取
        if "gold" in out or "silver" in out:
            out["source"] = source
            out["url"] = url
            out["fetchedAt"] = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
            print(f"金銀店頭価格({source}): {out.get('gold')} / {out.get('silver')}")
            return out
    print("金銀店頭価格: 全ソース取得失敗(COMEX換算にフォールバック)")
    return None


# ---------------------------------------------------------------- メイン

EPISODE_CONFIRM_N = 3      # ✅確定に必要な連続日数(engine.CONFIRM_STREAK_Nと同義)
EPISODE_MAX_CAL_DAYS = 190  # エピソード自動クローズ(暦日≒130営業日。Value出口120営業日+余裕)


def update_episodes(instruments, cycle, model_pf, today, store, challenger=None):
    """シグナルを「エピソード」として追跡する(v3.24)。
    1エピソード = Tier閾値到達(streak=1)〜スコアが閾値を割る(streak=0)まで。
    日次行の重複計上を排し、再分析の1サンプル=1独立局面にする。storeを直接更新する。
    同日再実行では streak が変わらないため二重開始しない(open_byキーで防止)。

    challenger(v3.26): {"enabled":true,"label":str,"thresholds":{"80":85,"90":92},"confirmN":5}
    本番(Champion)と同じスコア系列から、より厳しい閾値のエピソードを arm="challenger" で並走記録。
    Championルールには一切影響しない(仮想運用)。ChallengerのストリークはstoreのchalStreaksで管理。"""
    from datetime import date as _date
    open_eps = store.setdefault("open", [])
    closed = store.setdefault("closed", [])
    reg = (model_pf or {}).get("regime") or {}
    regime = reg.get("regime") or "unknown"
    risk_off = ((cycle or {}).get("riskOff") or {}).get("score")
    # armキー付きで管理(旧エピソードはarmなし=champion扱い)
    open_by = {(e["ticker"], e["strat"], e.get("arm", "champion")): e for e in open_eps}
    chal_on = bool(challenger and challenger.get("enabled"))
    chal_thr = (challenger or {}).get("thresholds") or {"80": 85, "90": 92}
    chal_n = int((challenger or {}).get("confirmN") or 5)
    cs = store.setdefault("chalStreaks", {})
    chal_new_day = store.get("chalDate") != today
    if chal_on:
        store["chalDate"] = today

    def close_ep(ep, price, reason):
        ep["endDate"] = today
        ep["endPrice"] = price
        ep["endReason"] = reason
        open_eps.remove(ep)
        closed.append(ep)

    seen = set()
    for i in instruments:
        if i.get("state") == "NO_DATA":
            continue
        tk = i["ticker"]
        price = i.get("price")
        v = i.get("value") or {}
        streaks = v.get("streaks") or {}
        sec = i.get("rotSector") or i.get("subSector")
        strend = (((cycle or {}).get("sectors") or {}).get(sec) or {}).get("trend")
        # Value 80/90 (Champion=本番ルール)
        for thr in (80, 90):
            strat = f"value{thr}"
            seen.add((tk, strat, "champion"))
            st = int(streaks.get(str(thr)) or 0)
            ep = open_by.get((tk, strat, "champion"))
            if ep is None and st >= 1:
                ep = {"id": f"{tk}-{strat}-{today}", "ticker": tk, "strat": strat, "arm": "champion",
                      "startDate": today, "startPrice": price, "startScore": v.get("score"),
                      "startStreak": st,  # 導入初日は既に連続中の可能性(>1なら開始日は概算)
                      "maxScore": v.get("score"), "q": (i.get("quality") or {}).get("score"),
                      "regime": regime, "riskOff": risk_off, "rotSector": sec, "sectorTrend": strend}
                open_eps.append(ep)
                open_by[(tk, strat, "champion")] = ep
            elif ep is not None:
                if st == 0:
                    close_ep(ep, price, "tierExit")
                    open_by.pop((tk, strat, "champion"), None)
                else:
                    ep["maxScore"] = max(ep.get("maxScore") or 0, v.get("score") or 0)
                    if st >= EPISODE_CONFIRM_N and not ep.get("confirmDate"):
                        ep["confirmDate"] = today
                        ep["confirmPrice"] = price
        # Challenger(v3.26): 同一スコア系列に厳しい閾値を適用した仮想アーム。本番へは無影響
        if chal_on:
            tcs = cs.setdefault(tk, {})
            for base in ("80", "90"):
                thr_c = int(chal_thr.get(base) or 0)
                if not thr_c:
                    continue
                strat = f"value{base}"
                seen.add((tk, strat, "challenger"))
                prev_n = int(tcs.get(base) or 0)
                score = v.get("score") or 0
                if score >= thr_c:
                    st_c = (prev_n + 1) if chal_new_day else max(prev_n, 1)
                else:
                    st_c = 0
                tcs[base] = st_c
                ep = open_by.get((tk, strat, "challenger"))
                if ep is None and st_c >= 1:
                    ep = {"id": f"{tk}-{strat}-chal-{today}", "ticker": tk, "strat": strat,
                          "arm": "challenger", "startDate": today, "startPrice": price,
                          "startScore": score, "startStreak": st_c, "maxScore": score,
                          "q": (i.get("quality") or {}).get("score"),
                          "regime": regime, "riskOff": risk_off, "rotSector": sec, "sectorTrend": strend}
                    open_eps.append(ep)
                    open_by[(tk, strat, "challenger")] = ep
                elif ep is not None:
                    if st_c == 0:
                        close_ep(ep, price, "tierExit")
                        open_by.pop((tk, strat, "challenger"), None)
                    else:
                        ep["maxScore"] = max(ep.get("maxScore") or 0, score)
                        if st_c >= chal_n and not ep.get("confirmDate"):
                            ep["confirmDate"] = today
                            ep["confirmPrice"] = price
        # モメンタム(trade銘柄): ENTRY成立〜EXIT/対象外化まで
        mm = i.get("momentum") or {}
        strat = "mom"
        seen.add((tk, strat, "champion"))
        state = mm.get("momState") or ("ENTRY" if mm.get("signal") == "entry" else None)
        ep = open_by.get((tk, strat, "champion"))
        if ep is None and state == "ENTRY":
            ep = {"id": f"{tk}-{strat}-{today}", "ticker": tk, "strat": strat, "arm": "champion",
                  "startDate": today, "startPrice": price,
                  "startScore": (mm.get("momRec") or {}).get("score"),
                  "regime": regime, "riskOff": risk_off, "rotSector": sec, "sectorTrend": strend}
            open_eps.append(ep)
            open_by[(tk, strat, "champion")] = ep
        elif ep is not None and state in ("EXIT", "WAIT"):
            close_ep(ep, price, "momExit" if state == "EXIT" else "momWait")
            open_by.pop((tk, strat, "champion"), None)
    # 銘柄がウォッチリストから消えた/長期化したエピソードを整理
    t_now = _date.fromisoformat(today)
    for ep in list(open_eps):
        age = (t_now - _date.fromisoformat(ep["startDate"])).days
        if (ep["ticker"], ep["strat"], ep.get("arm", "champion")) not in seen:
            close_ep(ep, ep.get("endPrice") or ep.get("startPrice"), "removed")
        elif age > EPISODE_MAX_CAL_DAYS:
            close_ep(ep, None, "maxDays")  # 終値は再分析側が取得
    store["closed"] = closed[-2000:]
    return store


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    s = cfg["settings"]

    # ベンチマーク(S&P500)・VIX・FX
    bench_hist, _ = fetch_history(cfg.get("benchmark", {"yahoo": "^GSPC", "stooq": "^spx"}))
    bench_closes = [h["c"] for h in bench_hist] if bench_hist else None
    print(f"benchmark: {'OK' if bench_closes else '取得失敗'}")

    vix_hist, _ = fetch_history(cfg["vix"])
    vix_val = vix_prev = None
    if vix_hist and len(vix_hist) >= 2:
        vix_val, vix_prev = vix_hist[-1]["c"], vix_hist[-2]["c"]
    vix = engine.analyze_vix(vix_val, vix_prev, s)
    print(f"VIX: {vix}")

    fx_hist, _ = fetch_history(cfg["fx"])
    usdjpy = fx_hist[-1]["c"] if fx_hist else None
    print(f"USD/JPY: {usdjpy}")

    # 全銘柄
    instruments = []
    stock_hist_map = {}
    gold_closes = None
    btc_hist = None
    entries = []
    for st in cfg["stocks"]:
        e = dict(st); e["class"] = "stock"; e["key"] = st["ticker"]; entries.append(e)
    for cr in cfg["crypto"]:
        e = dict(cr); e["class"] = "crypto"; entries.append(e)
    for mt in cfg["metals"]:
        e = dict(mt); e["class"] = "metal"; entries.append(e)

    for e in entries:
        hist, src = fetch_history(e)
        name = e.get("key") or e.get("ticker")
        if not hist:
            print(f"{name}: 全ソース取得失敗 → NO_DATA")
            hist = []
        else:
            print(f"{name}: {len(hist)}本 ({src})")
        inst = engine.analyze_instrument(e, hist, bench_closes,
                                         vix.get("value"), cfg)
        instruments.append(inst)
        if e.get("class") == "stock" and hist:
            stock_hist_map[e["ticker"]] = hist
        if e.get("key") == "gold" and hist:
            gold_closes = [h["c"] for h in hist]
        if e.get("key") == "btc" and hist:
            btc_hist = hist
        time.sleep(0.4)  # レート制限予防

    # ファンダ品質の統合(fundamentals.jsonがあれば)
    try:
        with open(os.path.join(BASE, "fundamentals.json"), encoding="utf-8") as f:
            fund = json.load(f).get("tickers", {})
        applied = 0
        review_map = {s["ticker"]: s.get("alphaReview") for s in cfg["stocks"] if s.get("alphaReview")}
        for inst in instruments:
            q = fund.get(inst.get("ticker"))
            if q:
                engine.apply_quality(inst, q, cfg["valueParams"])
                # v3.28: 4α定量スコア(Finance/Mgmt定量/Money)を銘柄に添付
                if q.get("alphas"):
                    inst["alphas"] = q["alphas"]
                applied += 1
            # 定性レビュー(Claude生成・config保存)があれば合流
            rv = review_map.get(inst.get("ticker"))
            if rv:
                inst.setdefault("alphas", {})["review"] = rv
        print(f"品質スコア統合: {applied}銘柄")
    except FileNotFoundError:
        print("fundamentals.json なし(品質補正スキップ)")
    except Exception as e:
        print(f"品質統合エラー(スキップ): {e}")

    # サイクル&ローテーション分析(サブセクター等ウェイト指数を構築)
    sub_map = {s["ticker"]: (s.get("rotSector") or s.get("subSector") or "その他") for s in cfg["stocks"]}
    sec_ret = {}
    for tk, hist in stock_hist_map.items():
        sec = sub_map.get(tk)
        if not sec or len(hist) < 70:
            continue
        for i in range(1, len(hist)):
            sec_ret.setdefault(sec, {}).setdefault(hist[i]["d"], []).append(hist[i]["c"] / hist[i - 1]["c"] - 1)
    sector_idx = {}
    for sec, dd in sec_ret.items():
        idx, v = [], 1.0
        for d in sorted(dd.keys()):
            v *= 1 + sum(dd[d]) / len(dd[d])
            idx.append(v)
        sector_idx[sec] = idx
    if gold_closes:
        sector_idx["_gold"] = gold_closes
    cycle = engine.cycle_analysis(sector_idx, bench_closes, vix.get("value"))
    cycle["btcHalving"] = engine.btc_halving_analysis(btc_hist)
    cycle["macro"] = engine.macro_calendar(cfg)
    for ev in cycle["macro"]:
        if ev["phase"] == "imminent":
            alerts_macro = f"{ev['icon']} {ev['type']}まであと{ev['daysTo']}日({ev['date']})"
            print("マクロイベント接近:", alerts_macro)
    print(f"BTC半減期: +{cycle['btcHalving']['monthsSince']}ヶ月 {cycle['btcHalving']['phaseLabel']}")
    gated = 0
    for inst in instruments:
        engine.apply_cycle(inst, cycle, cfg["valueParams"])
        if (inst.get("momentum") or {}).get("cycleGated"):
            gated += 1
    print(f"サイクル補正適用: モメンタム見送り{gated}件")
    print(f"サイクル分析: リスクオフ度{cycle['riskOff']['score']} / セクター{list(cycle['sectors'].keys())}")

    events = engine.analyze_events(cfg, instruments)
    # 前回data.jsonの読込: テーゼ重複抑制 + 前日スコア(スコア急変の透明化)
    prev_thesis = set()
    prev_scores = {}
    prev_streaks = {}
    prev_date = None
    try:
        with open(os.path.join(BASE, "data.json"), encoding="utf-8") as f:
            pdata = json.load(f)
            prev_date = (pdata.get("updatedJST") or "")[:10] or None
            for pi in pdata.get("instruments", []):
                if pi.get("holdMgmt", {}).get("thesisBreak"):
                    prev_thesis.add(pi.get("ticker"))
                if pi.get("value"):
                    prev_scores[pi.get("ticker")] = pi["value"].get("score")
                    prev_streaks[pi.get("ticker")] = pi["value"].get("streaks") or {}
    except Exception:
        pass
    # Tier連続日数トラッキング(持続性フィルタ実測でN=3日連続が最良と判明したため)
    # 同日再実行(🚀ボタン等)では加算しない。欠測日はそのまま(営業日ベース近似)。
    today_jst = datetime.now(JST).strftime("%Y-%m-%d")
    same_day = (prev_date == today_jst)
    for inst in instruments:
        v = inst.get("value")
        if v is None:
            continue
        pv = prev_scores.get(inst.get("ticker"))
        if pv is not None:
            v["scorePrev"] = pv
        ps = prev_streaks.get(inst.get("ticker"), {})
        st = {}
        for key, thr in (("65", 65), ("80", 80), ("90", 90)):
            pn = int(ps.get(key) or 0)
            if v.get("score", 0) >= thr:
                st[key] = max(pn, 1) if same_day else pn + 1
            else:
                st[key] = 0
        v["streaks"] = st
    remind = datetime.now(JST).weekday() == 0
    alerts = engine.build_alerts(instruments, events, vix, cfg, prev_thesis, remind)
    for ev in cycle.get("macro", []):
        if ev["phase"] == "imminent" and ev["daysTo"] >= 0:
            alerts.append({"type": "MACRO_EVENT", "ticker": ev["type"], "priority": 2,
                           "title": f"{ev['icon']} {ev['type']}まであと{ev['daysTo']}日({ev['date']})",
                           "detail": ev["guide"][:90] + "…"})
    if cycle["riskOff"]["score"] >= 60:
        alerts.insert(0, {"type": "CYCLE", "ticker": "MARKET", "priority": 1,
                          "title": f"🛡️ リスクオフ度{cycle['riskOff']['score']} — 有事への備えを",
                          "detail": " / ".join(cycle["riskOff"]["factors"]) + "。現金比率引き上げ・新規買い減速を検討"})
    weights = engine.planner_weights(instruments, cfg)
    model_pf = engine.model_portfolio(instruments, cycle, cfg, weights, bench_closes, vix.get("value"))
    print(f"モデルPF: assets={model_pf['assets']} tilts={len(model_pf['tilts'])}件")

    now = datetime.now(timezone.utc)
    data = {
        "version": 2,
        "updated": now.isoformat(),
        "updatedJST": now.astimezone(JST).strftime("%Y-%m-%d %H:%M JST"),
        "settings": s,
        "valueParams": cfg["valueParams"],
        "momentumParams": cfg["momentumParams"],
        "portfolio": cfg["portfolio"],
        "targets": cfg.get("targets", {}),
        "vix": vix,
        "fx": {"usdjpy": usdjpy},
        "metalJpy": fetch_nihon_material(),
        "instruments": instruments,
        "events": events,
        "alerts": alerts,
        "plannerWeights": weights,
        "cycle": cycle,
        "modelPortfolio": model_pf,
        "discovery": cfg.get("discovery", []),
    }
    with open(os.path.join(BASE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"data.json 更新: 銘柄{len(instruments)} アラート{len(alerts)}")

    # シグナルログ(再分析用に追記)
    log_path = os.path.join(BASE, "signals_log.json")
    log = []
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            log = []
    today = now.astimezone(JST).strftime("%Y-%m-%d")
    log = [x for x in log if x.get("date") != today]  # 同日再実行は上書き
    for i in instruments:
        if i.get("state") == "NO_DATA":
            continue
        entry_types = []
        if i["value"]["tier"] in ("consider", "strong", "absolute"):
            entry_types.append("value_" + i["value"]["tier"])
        if i.get("momentum", {}).get("signal") == "entry":
            entry_types.append("mom_entry")
        if not entry_types:
            continue
        log.append({"date": today, "ticker": i["ticker"], "price": i["price"],
                    "types": entry_types, "score": i["value"]["score"],
                    "q": (i.get("quality") or {}).get("score"),
                    "expReturnPct": i["rec"].get("expReturnPct"),
                    "expDays": i["rec"].get("expDays")})
    log = log[-3000:]
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False)
    print(f"signals_log.json: {len(log)}件")

    # エピソードログ(v3.24): Tier到達〜剥落を1件として記録(日次重複の排除・前向き検証の主データ)
    ep_path = os.path.join(BASE, "episodes.json")
    store = {"open": [], "closed": []}
    if os.path.exists(ep_path):
        try:
            with open(ep_path, encoding="utf-8") as f:
                store = json.load(f)
        except Exception:
            pass
    update_episodes(instruments, cycle, model_pf, today, store, cfg.get("challenger"))
    with open(ep_path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=1)
    print(f"episodes.json: 進行中{len(store['open'])} / 完了{len(store['closed'])}")

    # Web Push(VAPID設定時のみ)
    send_push(alerts)


def send_push(alerts):
    priv = os.environ.get("VAPID_PRIVATE_KEY")
    email = os.environ.get("VAPID_CLAIMS_EMAIL")
    subs_path = os.path.join(BASE, "subscriptions.json")
    urgent = [a for a in alerts if a["priority"] <= 2]
    if not (priv and email and os.path.exists(subs_path) and urgent):
        print("push: スキップ(未設定または対象なし)")
        return
    try:
        from pywebpush import webpush
    except ImportError:
        print("push: pywebpush未インストール")
        return
    with open(subs_path, encoding="utf-8") as f:
        subs = json.load(f)
    body = "\n".join(f"{a['title']}" for a in urgent[:6])
    payload = json.dumps({"title": f"📈 投資シグナル ({len(urgent)}件)", "body": body,
                          "url": "./index.html"}, ensure_ascii=False)
    ok = 0
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=priv,
                    vapid_claims={"sub": f"mailto:{email}"})
            ok += 1
        except Exception as e:
            print(f"push失敗: {e}")
    print(f"push: {ok}/{len(subs)}件送信")


if __name__ == "__main__":
    main()
