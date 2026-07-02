# -*- coding: utf-8 -*-
"""
reanalyze.py v2 — 過去シグナルの的中率検証 + パラメータ自動調整
週1(日曜)+手動実行。signals_log.json の各シグナルについて、
その後の実際の値動きと突き合わせて戦略別(value/momentum)の的中率を計算し、
結果に応じて判定パラメータを安全な範囲内で自動調整、analysis.jsonに記録する。
※あなたが実際に売買したかは無関係(シグナル自体の品質評価)。
"""
import json
import os
from datetime import datetime, timezone, timedelta

import evaluate  # fetch関数を再利用

BASE = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))

# 的中定義
VALUE_HORIZON_DAYS = 40    # 営業日: value買いシグナル後この期間内に
VALUE_HIT_PCT = 5          # +5%以上上昇すれば的中、-8%以下なら失敗
VALUE_MISS_PCT = -8
MOM_HORIZON_DAYS = 40
MOM_HIT_PCT = 8            # モメンタムは+8%で的中、-8%(初期損切り相当)で失敗
MOM_MISS_PCT = -8

# 自動調整の安全範囲
BOUNDS = {
    "bandPctOfRange": (0.02, 0.06),
    "minScoreToAlert": (50, 75),
    "stopInitPct": (6, 10),
}


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    log_path = os.path.join(BASE, "signals_log.json")
    if not os.path.exists(log_path):
        print("signals_log.json なし。データ蓄積待ち。")
        write_analysis({"note": "シグナルログなし。データ蓄積待ち。"}, [])
        return
    with open(log_path, encoding="utf-8") as f:
        log = json.load(f)

    # 価格キャッシュ
    tickers = sorted({x["ticker"] for x in log})
    price_map = {}
    all_entries = ([dict(s, key=s["ticker"]) for s in cfg["stocks"]]
                   + cfg["crypto"] + cfg["metals"])
    entry_map = {}
    for e in all_entries:
        entry_map[e.get("ticker") or e.get("key")] = e
        if e.get("key"):
            entry_map[e["key"]] = e
    for t in tickers:
        e = entry_map.get(t)
        if not e:
            continue
        hist, _ = evaluate.fetch_history(e)
        if hist:
            price_map[t] = hist

    results = {"value": {"hit": 0, "miss": 0, "open": 0},
               "momentum": {"hit": 0, "miss": 0, "open": 0}}
    details = []
    for sig in log:
        hist = price_map.get(sig["ticker"])
        if not hist:
            continue
        dates = [h["d"] for h in hist]
        try:
            idx = next(i for i, d in enumerate(dates) if d >= sig["date"])
        except StopIteration:
            continue
        p0 = sig["price"]
        for typ in sig["types"]:
            strat = "momentum" if typ == "mom_entry" else "value"
            horizon = MOM_HORIZON_DAYS if strat == "momentum" else VALUE_HORIZON_DAYS
            hit_pct = MOM_HIT_PCT if strat == "momentum" else VALUE_HIT_PCT
            miss_pct = MOM_MISS_PCT if strat == "momentum" else VALUE_MISS_PCT
            window = hist[idx: idx + horizon + 1]
            outcome = "open"
            for h in window:
                chg = (h["c"] / p0 - 1) * 100
                if chg <= miss_pct:
                    outcome = "miss"; break
                if chg >= hit_pct:
                    outcome = "hit"; break
            else:
                if len(window) >= horizon:
                    last_chg = (window[-1]["c"] / p0 - 1) * 100
                    outcome = "hit" if last_chg > 0 else "miss"
            results[strat][outcome] += 1
            details.append({"date": sig["date"], "ticker": sig["ticker"],
                            "type": typ, "outcome": outcome})

    def rate(r):
        closed = r["hit"] + r["miss"]
        return round(r["hit"] / closed * 100, 1) if closed else None

    v_rate, m_rate = rate(results["value"]), rate(results["momentum"])
    print(f"Value的中率: {v_rate}% {results['value']}")
    print(f"Momentum的中率: {m_rate}% {results['momentum']}")

    # ---- パラメータ自動調整(安全範囲内・小刻み) ----
    adjustments = []
    s = cfg["settings"]; mp = cfg["momentumParams"]
    closed_v = results["value"]["hit"] + results["value"]["miss"]
    closed_m = results["momentum"]["hit"] + results["momentum"]["miss"]
    if closed_v >= 10 and v_rate is not None:
        if v_rate < 45:  # 精度不足 → 足切りを厳しく・帯を狭く
            new = min(BOUNDS["minScoreToAlert"][1], s["minScoreToAlert"] + 5)
            if new != s["minScoreToAlert"]:
                adjustments.append(f"minScoreToAlert {s['minScoreToAlert']}→{new} (Value的中率{v_rate}%)")
                s["minScoreToAlert"] = new
            new_b = max(BOUNDS["bandPctOfRange"][0], round(s["bandPctOfRange"] - 0.005, 3))
            if new_b != s["bandPctOfRange"]:
                adjustments.append(f"bandPctOfRange {s['bandPctOfRange']}→{new_b}")
                s["bandPctOfRange"] = new_b
        elif v_rate > 65:  # 精度十分 → 機会を増やす
            new = max(BOUNDS["minScoreToAlert"][0], s["minScoreToAlert"] - 5)
            if new != s["minScoreToAlert"]:
                adjustments.append(f"minScoreToAlert {s['minScoreToAlert']}→{new} (Value的中率{v_rate}%)")
                s["minScoreToAlert"] = new
    if closed_m >= 10 and m_rate is not None:
        if m_rate < 40:
            new = min(BOUNDS["stopInitPct"][1], mp["stopInitPct"] + 1)
            if new != mp["stopInitPct"]:
                adjustments.append(f"stopInitPct {mp['stopInitPct']}→{new} (Momentum的中率{m_rate}%)")
                mp["stopInitPct"] = new
        elif m_rate > 60:
            new = max(BOUNDS["stopInitPct"][0], mp["stopInitPct"] - 1)
            if new != mp["stopInitPct"]:
                adjustments.append(f"stopInitPct {mp['stopInitPct']}→{new} (Momentum的中率{m_rate}%)")
                mp["stopInitPct"] = new

    if adjustments:
        with open(os.path.join(BASE, "config.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        print("config.json 調整:", adjustments)

    write_analysis({
        "valueHitRate": v_rate, "momentumHitRate": m_rate,
        "valueResults": results["value"], "momentumResults": results["momentum"],
        "totalSignals": len(details),
        "note": ("シグナル数が少ないため参考値。数ヶ月の蓄積後に意味を持ちます。"
                 if (closed_v + closed_m) < 20 else ""),
    }, adjustments)


def write_analysis(summary, adjustments):
    path = os.path.join(BASE, "analysis.json")
    prev = {"history": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            pass
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    entry = {"date": now, **summary, "adjustments": adjustments}
    hist = prev.get("history", [])
    hist.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"latest": entry, "history": hist[-52:]}, f, ensure_ascii=False, indent=1)
    print("analysis.json 更新")


if __name__ == "__main__":
    main()
