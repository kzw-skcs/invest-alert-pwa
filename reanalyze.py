# -*- coding: utf-8 -*-
"""
reanalyze.py v3 (v3.24) — 前向き検証: エピソード単位のTriple Barrier評価

変更点(GPT5.6レビュー反映):
 1. 主データを episodes.json(1エピソード=1独立局面)に変更。日次重複の水増しを排除。
 2. 判定を Triple Barrier に統一: 上+5% / 下-8% / 40営業日。
    「時間切れ」は勝敗に混ぜず別集計(旧版の『40日後プラスなら的中』の水増しを廃止)。
 3. 勝率だけでなく期待値(1エピソード平均リターン)・平均利益/平均損失・Profit Factorを算出。
 4. パラメータ自動変更を廃止 → 「提案のみ」(suggestions)。独立エピソード30件未満は提案もしない。
 5. 速報(開始日)と確定(3日連続日)の両起点で評価し、二段構えの前向き検証を兼ねる。
旧signals_log.json(日次)も参考値として併記(過去との連続性のため)。
"""
import json
import os
from datetime import datetime, timezone, timedelta

import evaluate  # fetch関数を再利用

BASE = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))

# Triple Barrier定義
VALUE_HORIZON_DAYS = 40
VALUE_HIT_PCT = 5
VALUE_MISS_PCT = -8
MOM_HORIZON_DAYS = 40
MOM_HIT_PCT = 8
MOM_MISS_PCT = -8

MIN_EPISODES_FOR_SUGGESTION = 30   # 提案を出す最低独立エピソード数

# 参考: 提案の際に言及する安全範囲(自動変更はしない)
BOUNDS = {
    "bandPctOfRange": (0.02, 0.06),
    "minScoreToAlert": (50, 75),
    "stopInitPct": (6, 10),
}


def triple_barrier(hist, entry_date, entry_price, up_pct, dn_pct, horizon):
    """終値ベースのTriple Barrier判定。
    戻り値: (outcome, retPct, daysUsed)
      outcome: "win"(先に上側) / "loss"(先に下側) / "expired"(期間満了) / "open"(データ不足・進行中)
    注意: 日足終値のみのため日中の到達順序は不明。1日で両バリアを飛び越えるギャップは
    終値の符号で判定される(保守的な日中OHLC判定はv3.25で対応予定)。"""
    if not hist or not entry_price:
        return "open", None, 0
    idx = None
    for i, h in enumerate(hist):
        if h["d"] >= entry_date:
            idx = i
            break
    if idx is None:
        return "open", None, 0
    window = hist[idx: idx + horizon + 1]
    for n, h in enumerate(window):
        chg = (h["c"] / entry_price - 1) * 100
        if chg <= dn_pct:
            return "loss", round(chg, 2), n
        if chg >= up_pct:
            return "win", round(chg, 2), n
    if len(window) >= horizon:
        return "expired", round((window[-1]["c"] / entry_price - 1) * 100, 2), len(window)
    return "open", (round((window[-1]["c"] / entry_price - 1) * 100, 2) if window else None), len(window)


def barrier_params(strat):
    if strat == "mom":
        return MOM_HIT_PCT, MOM_MISS_PCT, MOM_HORIZON_DAYS
    return VALUE_HIT_PCT, VALUE_MISS_PCT, VALUE_HORIZON_DAYS


def aggregate(rows):
    """rows: [(outcome, retPct)] → 統計。時間切れは勝率に含めず、期待値には含める。"""
    closed = [(o, r) for o, r in rows if o in ("win", "loss", "expired") and r is not None]
    wins = [r for o, r in closed if o == "win"]
    losses = [r for o, r in closed if o == "loss"]
    expired = [r for o, r in closed if o == "expired"]
    n_wl = len(wins) + len(losses)
    out = {"n": len(closed), "win": len(wins), "loss": len(losses), "expired": len(expired),
           "open": len([1 for o, _ in rows if o == "open"]),
           "winRatePct": round(len(wins) / n_wl * 100, 1) if n_wl else None,
           "expectancyPct": round(sum(r for _, r in closed) / len(closed), 2) if closed else None,
           "avgWinPct": round(sum(wins) / len(wins), 2) if wins else None,
           "avgLossPct": round(sum(losses) / len(losses), 2) if losses else None,
           "profitFactor": None}
    gross_win = sum(wins) + sum(r for r in expired if r > 0)
    gross_loss = abs(sum(losses) + sum(r for r in expired if r < 0))
    if gross_loss > 0:
        out["profitFactor"] = round(gross_win / gross_loss, 2)
    return out


def evaluate_episodes(episodes, price_map):
    """エピソードを速報(開始日)/確定(3日連続日)の両起点で評価。"""
    stats = {}
    for ep in episodes:
        hist = price_map.get(ep["ticker"])
        if not hist:
            continue
        up, dn, hz = barrier_params("mom" if ep["strat"] == "mom" else "value")
        for entry_kind, dkey, pkey in (("速報", "startDate", "startPrice"),
                                        ("確定", "confirmDate", "confirmPrice")):
            if not ep.get(dkey) or not ep.get(pkey):
                continue
            o, r, _ = triple_barrier(hist, ep[dkey], ep[pkey], up, dn, hz)
            stats.setdefault(ep["strat"], {}).setdefault(entry_kind, []).append((o, r))
    return {strat: {kind: aggregate(rows) for kind, rows in kinds.items()}
            for strat, kinds in stats.items()}


def build_suggestions(ep_stats, cfg):
    """自動変更はしない。独立エピソードが十分溜まった項目のみ提案文を作る。"""
    sug = []
    s = cfg["settings"]; mp = cfg["momentumParams"]
    v80 = (ep_stats.get("value80") or {}).get("速報")
    if v80 and v80["win"] + v80["loss"] >= MIN_EPISODES_FOR_SUGGESTION:
        if v80["winRatePct"] is not None and v80["winRatePct"] < 45:
            sug.append(f"Value80速報の勝率{v80['winRatePct']}%(n={v80['n']})が低迷。minScoreToAlert "
                       f"{s['minScoreToAlert']}→{min(BOUNDS['minScoreToAlert'][1], s['minScoreToAlert'] + 5)} への引き上げを検討"
                       "(configを手動変更。自動変更は過剰適合防止のため廃止)")
        elif v80["winRatePct"] is not None and v80["winRatePct"] > 70 and (v80["expectancyPct"] or 0) > 3:
            sug.append(f"Value80速報が好調(勝率{v80['winRatePct']}%・期待値{v80['expectancyPct']}%、n={v80['n']})。"
                       f"minScoreToAlert {s['minScoreToAlert']}→{max(BOUNDS['minScoreToAlert'][0], s['minScoreToAlert'] - 5)} で機会を増やす選択肢あり")
    mom = (ep_stats.get("mom") or {}).get("速報")
    if mom and mom["win"] + mom["loss"] >= MIN_EPISODES_FOR_SUGGESTION:
        if mom["winRatePct"] is not None and mom["winRatePct"] < 40:
            sug.append(f"モメンタム勝率{mom['winRatePct']}%(n={mom['n']})。stopInitPct "
                       f"{mp['stopInitPct']}→{min(BOUNDS['stopInitPct'][1], mp['stopInitPct'] + 1)} (損切り緩和)を検討")
    # 二段構えの前向き比較
    v80c = (ep_stats.get("value80") or {}).get("確定")
    if v80 and v80c and v80c["n"] >= 10:
        sug.append(f"二段構え前向き検証: 速報 期待値{v80['expectancyPct']}%/勝率{v80['winRatePct']}% vs "
                   f"確定 期待値{v80c['expectancyPct']}%/勝率{v80c['winRatePct']}%(バックテストでは確定優位。実運用で逆転したら要再検討)")
    return sug


def legacy_stats(log, price_map):
    """旧・日次ログの参考統計(重複あり=水増し傾向。連続性のためのみ保持)。"""
    rows = {"value": [], "momentum": []}
    for sig in log:
        hist = price_map.get(sig["ticker"])
        if not hist:
            continue
        for typ in sig.get("types", []):
            strat = "momentum" if typ == "mom_entry" else "value"
            up, dn, hz = barrier_params("mom" if strat == "momentum" else "value")
            o, r, _ = triple_barrier(hist, sig["date"], sig.get("price"), up, dn, hz)
            rows[strat].append((o, r))
    return {k: aggregate(v) for k, v in rows.items() if v}


def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)

    episodes = []
    ep_path = os.path.join(BASE, "episodes.json")
    if os.path.exists(ep_path):
        try:
            with open(ep_path, encoding="utf-8") as f:
                st = json.load(f)
            episodes = (st.get("closed") or []) + (st.get("open") or [])
        except Exception:
            pass
    log = []
    log_path = os.path.join(BASE, "signals_log.json")
    if os.path.exists(log_path):
        try:
            with open(log_path, encoding="utf-8") as f:
                log = json.load(f)
        except Exception:
            pass
    if not episodes and not log:
        print("エピソード・ログなし。データ蓄積待ち。")
        write_analysis({"note": "シグナルログなし。データ蓄積待ち。"}, [])
        return

    # 価格キャッシュ
    tickers = sorted({e["ticker"] for e in episodes} | {x["ticker"] for x in log})
    all_entries = ([dict(s, key=s["ticker"]) for s in cfg["stocks"]]
                   + cfg["crypto"] + cfg["metals"])
    entry_map = {}
    for e in all_entries:
        entry_map[e.get("ticker") or e.get("key")] = e
        if e.get("key"):
            entry_map[e["key"]] = e
    price_map = {}
    for t in tickers:
        e = entry_map.get(t)
        if not e:
            continue
        hist, _ = evaluate.fetch_history(e)
        if hist:
            price_map[t] = hist

    ep_stats = evaluate_episodes(episodes, price_map)
    lg = legacy_stats(log, price_map)
    suggestions = build_suggestions(ep_stats, cfg)
    n_ep = len(episodes)
    n_closed = sum(v.get("速報", {}).get("n", 0) for v in ep_stats.values())

    print("エピソード統計:", json.dumps(ep_stats, ensure_ascii=False)[:600])
    print("提案:", suggestions or "なし")

    write_analysis({
        "episodeStats": ep_stats,
        "episodeCount": n_ep,
        "legacyStats": lg,
        # 旧UI互換フィールド(値はエピソードベースの速報勝率)
        "valueHitRate": ((ep_stats.get("value80") or {}).get("速報") or {}).get("winRatePct"),
        "momentumHitRate": ((ep_stats.get("mom") or {}).get("速報") or {}).get("winRatePct"),
        "note": (f"独立エピソード評価済み{n_closed}件。30件未満の間は統計は参考値。"
                 if n_closed < MIN_EPISODES_FOR_SUGGESTION else ""),
    }, suggestions)


def write_analysis(summary, suggestions):
    path = os.path.join(BASE, "analysis.json")
    prev = {"history": []}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            pass
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M JST")
    entry = {"date": now, **summary, "suggestions": suggestions,
             "adjustments": []}  # 旧UI互換(自動変更は廃止)
    hist = prev.get("history", [])
    hist.append(entry)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"latest": entry, "history": hist[-52:]}, f, ensure_ascii=False, indent=1)
    print("analysis.json 更新")


if __name__ == "__main__":
    main()
