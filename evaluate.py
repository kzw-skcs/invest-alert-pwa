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
            hist.append({"d": p[0], "c": float(p[4]),
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


# ---------------------------------------------------------------- メイン

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
        time.sleep(0.4)  # レート制限予防

    events = engine.analyze_events(cfg, instruments)
    # テーゼ見直しの重複抑制: 前回data.jsonで既に出ていた銘柄は週1(月曜)のみ再通知
    prev_thesis = set()
    try:
        with open(os.path.join(BASE, "data.json"), encoding="utf-8") as f:
            for pi in json.load(f).get("instruments", []):
                if pi.get("holdMgmt", {}).get("thesisBreak"):
                    prev_thesis.add(pi.get("ticker"))
    except Exception:
        pass
    remind = datetime.now(JST).weekday() == 0
    alerts = engine.build_alerts(instruments, events, vix, cfg, prev_thesis, remind)
    weights = engine.planner_weights(instruments, cfg)

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
        "instruments": instruments,
        "events": events,
        "alerts": alerts,
        "plannerWeights": weights,
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
                    "expReturnPct": i["rec"].get("expReturnPct"),
                    "expDays": i["rec"].get("expDays")})
    log = log[-3000:]
    with open(log_path, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False)
    print(f"signals_log.json: {len(log)}件")

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
