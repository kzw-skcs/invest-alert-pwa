"""
アルゴリズム再分析（自己フィードバック）。
過去に出したシグナル（predictions.jsonl）を、実際のその後の値動きと突き合わせて
「予想的中率」を計算し、結果に応じて判定パラメータ(settings)を自動調整する。
ユーザーが実際に売買したかどうかは無関係（純粋に値動きベースで評価）。

- 週1回スケジュール実行 ＋ PWAの「アルゴリズム再分析」ボタン（workflow_dispatch）から実行。
- 出力: analysis.json（的中率レポート・調整履歴）、config.json（settings更新）。

純ロジック（outcome / aggregate / propose_adjustments）はネット不要で単体テスト可能。
"""
from __future__ import annotations
import json, os, datetime
import engine as E

HERE = os.path.dirname(os.path.abspath(__file__))


# ---------- 純ロジック（テスト可能） ----------
def window_days(pred, default=60):
    d = pred.get("expDays")
    return int(d) if d else default


def outcome(pred, future, hit_pct):
    """
    pred: 1件の予測。future: シグナル日より後・ウィンドウ内の [(date, close), ...]。
    買い: 期間内に price*(1+hit_pct%) 以上へ上昇したら的中。
    売り: 期間内に price*(1-hit_pct%) 以下へ下落したら的中。
    返り値: ('hit'|'miss'|None) と最大変動率。futureが空なら None。
    """
    price = pred.get("price")
    if not price or not future:
        return None, None
    closes = [c for _, c in future]
    if pred["side"] in ("buy",):
        peak = max(closes)
        max_move = (peak - price) / price * 100
        hit = peak >= price * (1 + hit_pct / 100)
        reached_target = (pred.get("top") is not None and peak >= pred["top"])
        return ("hit" if hit else "miss"), {"maxUpPct": round(max_move, 2),
                                            "reachedTarget": reached_target}
    else:  # sell
        trough = min(closes)
        max_move = (trough - price) / price * 100
        hit = trough <= price * (1 - hit_pct / 100)
        return ("hit" if hit else "miss"), {"maxDownPct": round(max_move, 2)}


def aggregate(evaluated):
    """evaluated: [{side,state,score,result}] から的中率を集計。"""
    def bucket():
        return {"n": 0, "hits": 0}
    by_side = {"buy": bucket(), "sell": bucket()}
    by_state = {}
    by_scoreband = {"high(>=65)": bucket(), "mid(50-64)": bucket(), "low(<50)": bucket()}
    for e in evaluated:
        if e["result"] not in ("hit", "miss"):
            continue
        hit = 1 if e["result"] == "hit" else 0
        bs = by_side.setdefault(e["side"], bucket())
        bs["n"] += 1; bs["hits"] += hit
        st = by_state.setdefault(e["state"], bucket())
        st["n"] += 1; st["hits"] += hit
        sc = e.get("score") or 0
        key = "high(>=65)" if sc >= 65 else ("mid(50-64)" if sc >= 50 else "low(<50)")
        by_scoreband[key]["n"] += 1; by_scoreband[key]["hits"] += hit

    def rate(b):
        b = dict(b); b["rate"] = round(b["hits"] / b["n"], 3) if b["n"] else None
        return b
    return {
        "bySide": {k: rate(v) for k, v in by_side.items()},
        "byState": {k: rate(v) for k, v in by_state.items()},
        "byScoreBand": {k: rate(v) for k, v in by_scoreband.items()},
        "overall": rate({"n": sum(b["n"] for b in by_side.values()),
                         "hits": sum(b["hits"] for b in by_side.values())}),
    }


def propose_adjustments(stats, settings, min_n=8):
    """的中率に応じて settings を控えめに自動調整。返り値: (new_settings, adjustments[])"""
    s = dict(settings)
    adj = []

    buy = stats["bySide"].get("buy", {})
    if buy.get("n", 0) >= min_n and buy.get("rate") is not None:
        old = s.get("bandPctOfRange", 0.03)
        if buy["rate"] < 0.5:  # 早すぎる→帯を狭めて底に近い時だけ買い場に
            new = max(0.01, round(old * 0.85, 4))
            if new != old:
                s["bandPctOfRange"] = new
                adj.append({"param": "bandPctOfRange", "from": old, "to": new,
                            "reason": f"買い的中率{buy['rate']*100:.0f}%が低い→帯を狭め底付近に限定"})
        elif buy["rate"] > 0.7:  # 良好→やや広げて機会を増やす
            new = min(0.06, round(old * 1.1, 4))
            if new != old:
                s["bandPctOfRange"] = new
                adj.append({"param": "bandPctOfRange", "from": old, "to": new,
                            "reason": f"買い的中率{buy['rate']*100:.0f}%が良好→帯を広げ機会を拡大"})

    low = stats["byScoreBand"].get("low(<50)", {})
    high = stats["byScoreBand"].get("high(>=65)", {})
    if (low.get("n", 0) >= min_n and high.get("rate") is not None
            and low.get("rate") is not None and low["rate"] + 0.15 < high["rate"]):
        old = s.get("minScoreToAlert", 0)
        if old < 50:
            s["minScoreToAlert"] = 50
            adj.append({"param": "minScoreToAlert", "from": old, "to": 50,
                        "reason": f"低スコア的中率{low['rate']*100:.0f}%<<高スコア{high['rate']*100:.0f}%→低スコアの通知を抑制"})
    return s, adj


# ---------- 本体（ネット使用） ----------
def main():
    import evaluate as EV
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    settings = cfg["settings"]
    hit_pct = settings.get("hitThresholdPct", 3)
    today = datetime.date.today()

    pred_path = os.path.join(HERE, "predictions.jsonl")
    if not os.path.exists(pred_path):
        print("predictions.jsonl なし → 蓄積待ち")
        write_analysis({"note": "予測データ蓄積中（まだ評価対象がありません）"}, settings, cfg)
        return
    preds = []
    with open(pred_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                preds.append(json.loads(line))

    # 銘柄ごとの価格系列を取得（キャッシュ）
    fetchers = {}
    for s in cfg["stocks"]:
        fetchers[s["key"] if "key" in s else s["ticker"]] = ("stooq", s["stooq"])
        fetchers[s["ticker"]] = ("stooq", s["stooq"])
    for m in cfg["metals"]:
        fetchers[m["key"]] = ("stooq", m["stooq"])
    for c in cfg["crypto"]:
        fetchers[c["key"]] = ("coingecko", c["coingeckoId"])

    cache = {}

    def series_for(key):
        if key in cache:
            return cache[key]
        spec = fetchers.get(key)
        if not spec:
            cache[key] = []
            return []
        try:
            kind, sym = spec
            cache[key] = EV.fetch_stooq(sym) if kind == "stooq" else EV.fetch_coingecko(sym)
        except Exception as e:  # noqa
            print("取得失敗", key, e); cache[key] = []
        return cache[key]

    evaluated, pending = [], 0
    for p in preds:
        wd = window_days(p)
        sig_date = datetime.datetime.strptime(p["date"], "%Y-%m-%d").date()
        if today < sig_date + datetime.timedelta(days=wd):
            pending += 1
            continue
        ser = series_for(p["key"])
        future = [(d, c) for d, c in ser
                  if sig_date < datetime.datetime.strptime(d, "%Y-%m-%d").date()
                  <= sig_date + datetime.timedelta(days=wd)]
        res, detail = outcome(p, future, hit_pct)
        if res is None:
            pending += 1
            continue
        evaluated.append({"side": p["side"], "state": p["state"],
                          "score": p.get("score"), "result": res, "detail": detail,
                          "key": p["key"], "date": p["date"]})

    stats = aggregate(evaluated)
    new_settings, adj = propose_adjustments(stats, settings)
    cfg["settings"] = new_settings
    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    report = {
        "evaluatedCount": len(evaluated),
        "pendingCount": pending,
        "hitThresholdPct": hit_pct,
        "stats": stats,
        "adjustments": adj,
    }
    write_analysis(report, new_settings, cfg)
    print(f"再分析完了: 評価{len(evaluated)}件 / 保留{pending}件 / 調整{len(adj)}件")
    if stats.get("overall", {}).get("rate") is not None:
        print(f"総合的中率: {stats['overall']['rate']*100:.0f}%")


def write_analysis(report, settings, cfg):
    path = os.path.join(HERE, "analysis.json")
    prev = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:  # noqa
            prev = {}
    now = datetime.datetime.now(datetime.timezone.utc)
    jst = now.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    history = prev.get("history", [])
    if "stats" in report:
        history.insert(0, {
            "at": jst.strftime("%Y-%m-%d %H:%M JST"),
            "overall": report["stats"].get("overall"),
            "bySide": report["stats"].get("bySide"),
            "adjustments": report.get("adjustments", []),
            "evaluatedCount": report.get("evaluatedCount"),
        })
        history = history[:20]
    out = {"generated": now.isoformat(), "generatedJST": jst.strftime("%Y-%m-%d %H:%M JST"),
           "settings": settings, **report, "history": history}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
