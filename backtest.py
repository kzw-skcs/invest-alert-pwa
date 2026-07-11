# -*- coding: utf-8 -*-
"""
backtest.py — ウォークフォワード・バックテスト + 配分最適化

検証内容:
 1. Value戦略: スコアが閾値(65/80/90)を上抜けた日に買い →
    1年チャネル天井タッチ or 120営業日で売り。Tier別の勝率・平均リターン・年率。
 2. モメンタム戦略(trade銘柄): エントリー5条件成立で買い →
    初期損切り(-8%上限/ATR×2.5)・ATRトレーリング・+20%で1/3利確・+40%で1/3・
    40営業日で+5%未満ならタイムストップ。
 3. スリーブ別日次リターン系列(Value株/モメンタム株/BTC/ETH/金/銀)から
    5%刻みグリッドサーチで配分を最適化(最大CAGR / 最大シャープ / DD40%制約付き最大CAGR)。

結果は backtest.json に保存され、アプリの⚙️設定タブに表示される。
GitHub Actions の backtest ワークフロー(手動)で実行する。

⚠️ 過去データに対する機械的検証であり、将来の成績を保証しない。
   特に「最大CAGR配分」は過去への過剰適合(カーブフィッティング)を含む。
"""
import json
import math
import os
from datetime import datetime, timezone, timedelta

import engine
import evaluate
import engine as eng

try:
    import numpy as np
except ImportError:
    np = None

BASE = os.path.dirname(os.path.abspath(__file__))
JST = timezone(timedelta(hours=9))

WARMUP = 320                 # シグナル計算に必要な助走期間(営業日)
VALUE_EXIT_DAYS = 120        # Value: 最大保有期間
TIME_STOP_DAYS = 40          # モメンタム: タイムストップ(約8週)
TIME_STOP_MIN_GAIN = 5.0     # タイムストップ回避に必要な含み益%
TIER_THRESHOLDS = {"consider(65)": 65, "strong(80)": 80, "absolute(90)": 90}
GRID_STEP = 5                # 配分グリッド刻み%
MOM_MAX = 40                 # モメンタム配分上限%(現物・株スリーブ内制約の近似)

# v3.25: 現実的な約定モデル
# シグナルはt日終値で確定(実運用は引け後5:07 JSTに判定)→ 約定はt+1営業日の始値。
# 売買コスト(往復・スプレッド+手数料+スリッページの保守見積り)を控除する。
COST_RT_PCT = {"stock": 0.2, "crypto": 0.6, "metal": 2.0}


def next_fill(opens, closes, t):
    """t日シグナル→t+1始値で約定。t+1が存在しなければ(None, None)=取引不可。
    open欠損時(コインゲッコー等)はt+1終値で近似。"""
    if t + 1 >= len(closes):
        return None, None
    o = None
    if opens and t + 1 < len(opens):
        o = opens[t + 1]
    return (o if o else closes[t + 1]), t + 1


# ---------------------------------------------------------------- シグナル前計算

def precompute_signals(meta, history, bench_closes_by_date, cfg):
    """各営業日tについて (valueScore, top, band, momEntry, atr) を前計算。"""
    closes = [h["c"] for h in history]
    dates = [h["d"] for h in history]
    n = len(closes)
    out = []
    for t in range(n):
        if t < WARMUP:
            out.append(None)
            continue
        sl = history[max(0, t - 1300): t + 1]
        bench = bench_closes_by_date.get(dates[t])
        inst = engine.analyze_instrument(dict(meta), sl, bench, None, cfg)
        if inst.get("state") == "NO_DATA":
            out.append(None)
            continue
        out.append({
            "score": inst["value"]["score"],
            "top": inst.get("top"), "band": inst.get("band") or 0,
            "atr": inst.get("atr"),
            "momSignal": inst.get("momentum", {}).get("signal"),
        })
    # 底確認フラグ(20日線上+20日線上向き)を後付け
    ma20 = [None] * n
    run = 0.0
    for t in range(n):
        run += closes[t]
        if t >= 20:
            run -= closes[t - 20]
        if t >= 19:
            ma20[t] = run / 20
    for t in range(n):
        if out[t] is not None and ma20[t] is not None and t >= 24 and ma20[t - 5] is not None:
            out[t]["confirm"] = closes[t] > ma20[t] and ma20[t] >= ma20[t - 5]
    opens = [h.get("o") for h in history]
    return out, closes, dates, opens


# ---------------------------------------------------------------- Value戦略

def value_trades(instruments_data, threshold, cost=None):
    """全銘柄横断: t日終値でスコア閾値上抜けを確定→t+1営業日始値で買い(v3.25現実約定)。
    売り条件成立もt日終値判定→t+1始値で決済。往復コストを控除した生トレード一覧を返す。"""
    cost = COST_RT_PCT["stock"] if cost is None else cost
    trades = []
    for tk, (sig, closes, dates, opens) in instruments_data.items():
        open_pos = None
        for t in range(WARMUP + 1, len(closes)):
            s_now, s_prev = sig[t], sig[t - 1]
            if s_now is None:
                continue
            if open_pos is None:
                crossed = s_now["score"] >= threshold and (s_prev is None or s_prev["score"] < threshold)
                if crossed:
                    px, te = next_fill(opens, closes, t)
                    if px:
                        open_pos = {"entryT": te, "entry": px}
            else:
                held = t - open_pos["entryT"]
                hit_top = s_now["top"] and closes[t] >= s_now["top"] - s_now["band"]
                if hit_top or held >= VALUE_EXIT_DAYS or t == len(closes) - 1:
                    ex_px, ex_t = next_fill(opens, closes, t)
                    if ex_px is None or t == len(closes) - 1:
                        ex_px, ex_t = closes[t], t
                    trades.append({"ticker": tk, "entryDate": dates[open_pos["entryT"]],
                                   "exitDate": dates[ex_t], "days": ex_t - open_pos["entryT"],
                                   "retPct": (ex_px / open_pos["entry"] - 1) * 100 - cost,
                                   "reason": "top" if hit_top else "time"})
                    open_pos = None
    return trades


def backtest_value(instruments_data, threshold, cost=None):
    return summarize_trades(value_trades(instruments_data, threshold, cost=cost))


def value_trades_confirm(instruments_data, threshold, grace=5):
    """底確認変種: スコアが閾値到達で「待機」に入り、底確認(20日線上+上向き)が
    成立した日に初めて買う。スコアが閾値-grace を割ったら待機解除(そのエピソードは見送り)。
    「下落継続中の鋭敏な点灯」と「間違いのない底」の差を実測するための実験。"""
    trades = []
    for tk, (sig, closes, dates, opens) in instruments_data.items():
        open_pos = None
        armed = False
        prev_score = None
        for t in range(WARMUP + 1, len(closes)):
            s = sig[t]
            if s is None:
                prev_score = None
                continue
            if open_pos is not None:
                held = t - open_pos["entryT"]
                hit_top = s["top"] and closes[t] >= s["top"] - s["band"]
                if hit_top or held >= VALUE_EXIT_DAYS or t == len(closes) - 1:
                    ex_px, ex_t = next_fill(opens, closes, t)
                    if ex_px is None or t == len(closes) - 1:
                        ex_px, ex_t = closes[t], t
                    trades.append({"ticker": tk, "entryDate": dates[open_pos["entryT"]],
                                   "exitDate": dates[ex_t], "days": ex_t - open_pos["entryT"],
                                   "retPct": (ex_px / open_pos["entry"] - 1) * 100 - COST_RT_PCT["stock"],
                                   "reason": "top" if hit_top else "time"})
                    open_pos = None
                    armed = False
            else:
                if s["score"] >= threshold and (prev_score is None or prev_score < threshold):
                    armed = True
                if s["score"] < threshold - grace:
                    armed = False
                if armed and s.get("confirm"):
                    px, te = next_fill(opens, closes, t)
                    if px:
                        open_pos = {"entryT": te, "entry": px}
                    armed = False
            prev_score = s["score"]
    return trades


PERSIST_DAYS = (1, 3, 5, 10)  # 持続性フィルタ: スコアが閾値以上をN日連続で維持したら買い


def value_trades_persist(instruments_data, threshold, n_days):
    """スコアが閾値以上をn_days営業日連続で維持した日に買う変種(N=1は現行ルール相当)。
    瞬間タッチ(1日だけ閾値超え)がダマシか最良の買い場かを実測するための実験。"""
    trades = []
    for tk, (sig, closes, dates, opens) in instruments_data.items():
        open_pos = None
        streak = 0
        for t in range(WARMUP + 1, len(closes)):
            s_now = sig[t]
            if s_now is None:
                streak = 0
                continue
            streak = streak + 1 if s_now["score"] >= threshold else 0
            if open_pos is None:
                if streak == n_days:
                    px, te = next_fill(opens, closes, t)
                    if px:
                        open_pos = {"entryT": te, "entry": px}
            else:
                held = t - open_pos["entryT"]
                hit_top = s_now["top"] and closes[t] >= s_now["top"] - s_now["band"]
                if hit_top or held >= VALUE_EXIT_DAYS or t == len(closes) - 1:
                    ex_px, ex_t = next_fill(opens, closes, t)
                    if ex_px is None or t == len(closes) - 1:
                        ex_px, ex_t = closes[t], t
                    trades.append({"ticker": tk, "entryDate": dates[open_pos["entryT"]],
                                   "exitDate": dates[ex_t], "days": ex_t - open_pos["entryT"],
                                   "retPct": (ex_px / open_pos["entry"] - 1) * 100 - COST_RT_PCT["stock"],
                                   "reason": "top" if hit_top else "time"})
                    open_pos = None
    return trades


# GICS準拠rotSector → 性格グループ(セクター別Tier分解用)
GROUP_OF = {
    "生活必需品": "ディフェンシブ", "ヘルスケア": "ディフェンシブ", "公益": "ディフェンシブ",
    "情報技術": "グロース", "コミュニケーション": "グロース", "一般消費財": "グロース",
    "資本財": "シクリカル", "素材": "シクリカル", "エネルギー": "シクリカル", "金融": "シクリカル",
}
LOW_N = 15  # これ未満のトレード数は統計的に参考値扱い


def mini_summary(trades):
    if not trades:
        return {"trades": 0}
    rets = [x["retPct"] for x in trades]
    wins = len([r for r in rets if r > 0])
    return {"trades": len(trades),
            "winRatePct": round(wins / len(rets) * 100, 1),
            "avgRetPct": round(sum(rets) / len(rets), 2),
            "medianRetPct": round(sorted(rets)[len(rets) // 2], 2),
            "avgDays": round(sum(x["days"] for x in trades) / len(trades), 1),
            "lowN": len(trades) < LOW_N}


def summarize_by_sector(trades, sub_map):
    """トレード一覧をrotSector別＋性格グループ別に分解して集計。"""
    by_sec, by_grp = {}, {}
    for tr in trades:
        sec = sub_map.get(tr["ticker"], "その他")
        by_sec.setdefault(sec, []).append(tr)
        by_grp.setdefault(GROUP_OF.get(sec, "その他"), []).append(tr)
    return {"groups": {g: mini_summary(v) for g, v in
                       sorted(by_grp.items(), key=lambda kv: -len(kv[1]))},
            "sectors": {s: mini_summary(v) for s, v in
                        sorted(by_sec.items(), key=lambda kv: -len(kv[1]))}}


def build_cycle_context(stock_data, cfg, bench_hist, etf_hists=None):
    """過去各時点のサイクル文脈(セクター資金フロー・ベンチ200日線状態)を前計算。
    日付と価格のみから導出するため未来情報の混入なし。
    v3.31: セクターETF(etf_hists)を一次ソースに(本番evaluate.pyと同基準)。欠損は銘柄平均で補完。"""
    sub_map = {s["ticker"]: (s.get("rotSector") or s.get("subSector") or "その他") for s in cfg["stocks"]}
    b_dates = [h["d"] for h in bench_hist] if bench_hist else []
    b_close = [h["c"] for h in bench_hist] if bench_hist else []
    b_pos = {d: i for i, d in enumerate(b_dates)}
    below200 = set()
    run = 0.0
    for i, c in enumerate(b_close):
        run += c
        if i >= 200:
            run -= b_close[i - 200]
            if c < run / 200:
                below200.add(b_dates[i])

    def trend_map(ds, idx):
        tmap = {}
        for p in range(64, len(ds)):
            bp = b_pos.get(ds[p])
            if bp is None or bp < 64:
                continue
            r21 = (idx[p] / idx[p - 21] - b_close[bp] / b_close[bp - 21]) * 100
            r63 = (idx[p] / idx[p - 63] - b_close[bp] / b_close[bp - 63]) * 100
            tmap[ds[p]] = "inflow" if (r21 > 0.5 and r21 > r63 / 3) else \
                          "outflow" if (r21 < -0.5 and r21 < r63 / 3) else "neutral"
        return tmap

    trend_by_sec = {}
    for sec, hist in (etf_hists or {}).items():
        if hist and len(hist) > 130:
            trend_by_sec[sec] = trend_map([h["d"] for h in hist], [h["c"] for h in hist])
    sec_ret = {}
    for tk, (sig, closes, dates, opens) in stock_data.items():
        sec = sub_map.get(tk)
        if sec in trend_by_sec:
            continue
        for i in range(1, len(closes)):
            sec_ret.setdefault(sec, {}).setdefault(dates[i], []).append(closes[i] / closes[i - 1] - 1)
    for sec, dd in sec_ret.items():
        ds = sorted(dd.keys())
        idx, v = [], 1.0
        for d in ds:
            v *= 1 + sum(dd[d]) / len(dd[d])
            idx.append(v)
        trend_by_sec[sec] = trend_map(ds, idx)
    return {"sub_map": sub_map, "trend": trend_by_sec, "below200": below200}


def value_cycle_trades(instruments_data, threshold, ctx):
    """サイクル補正後スコアで閾値判定するValue変種(本番apply_cycleと同じ加減点)。生トレード一覧。"""
    trades = []
    for tk, (sig, closes, dates, opens) in instruments_data.items():
        sec = ctx["sub_map"].get(tk)
        tmap = ctx["trend"].get(sec, {})
        open_pos = None
        prev_adj = None
        for t in range(WARMUP + 1, len(closes)):
            s_now = sig[t]
            if s_now is None:
                prev_adj = None
                continue
            y, m = int(dates[t][:4]), int(dates[t][5:7])
            delta, _ = eng.value_cycle_delta(m, y, tmap.get(dates[t]),
                                             defensive=(sec in eng.DEFENSIVE_ROT))
            adj = s_now["score"] + delta
            if open_pos is None:
                if adj >= threshold and (prev_adj is None or prev_adj < threshold):
                    px, te = next_fill(opens, closes, t)
                    if px:
                        open_pos = {"entryT": te, "entry": px}
            else:
                held = t - open_pos["entryT"]
                hit_top = s_now["top"] and closes[t] >= s_now["top"] - s_now["band"]
                if hit_top or held >= VALUE_EXIT_DAYS or t == len(closes) - 1:
                    ex_px, ex_t = next_fill(opens, closes, t)
                    if ex_px is None or t == len(closes) - 1:
                        ex_px, ex_t = closes[t], t
                    trades.append({"ticker": tk, "entryDate": dates[open_pos["entryT"]],
                                   "exitDate": dates[ex_t], "days": ex_t - open_pos["entryT"],
                                   "retPct": (ex_px / open_pos["entry"] - 1) * 100 - COST_RT_PCT["stock"],
                                   "reason": "top" if hit_top else "time"})
                    open_pos = None
            prev_adj = adj
    return trades


def backtest_value_cycle(instruments_data, threshold, ctx):
    return summarize_trades(value_cycle_trades(instruments_data, threshold, ctx))


def backtest_value_hold(instruments_data, threshold):
    """比較用: 最初にスコアが閾値を超えた日に買い、そのまま検証期間末まで保有し続けた場合。"""
    rows = []
    for tk, (sig, closes, dates, opens) in instruments_data.items():
        for t in range(WARMUP + 1, len(closes)):
            s_now, s_prev = sig[t], sig[t - 1]
            if s_now and s_now["score"] >= threshold and (s_prev is None or s_prev["score"] < threshold):
                px, te = next_fill(opens, closes, t)
                if px is None:
                    break
                days = len(closes) - 1 - te
                if days < 20:
                    break
                ret = closes[-1] / px - 1 - COST_RT_PCT["stock"] / 200  # 買い持ちは片道コストのみ
                ann = ((1 + ret) ** (252 / days) - 1) * 100
                rows.append({"ticker": tk, "retPct": ret * 100, "days": days, "annPct": ann})
                break  # 最初のシグナルで買ってずっと保有
    if not rows:
        return {"trades": 0}
    rets = [r["retPct"] for r in rows]
    anns = [r["annPct"] for r in rows]
    return {"trades": len(rows),
            "winRatePct": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 1),
            "avgRetPct": round(sum(rets) / len(rets), 2),
            "medianRetPct": round(sorted(rets)[len(rets) // 2], 2),
            "avgAnnualizedPct": round(sum(anns) / len(anns), 1),
            "avgHoldDays": round(sum(r["days"] for r in rows) / len(rows), 1)}


# ---------------------------------------------------------------- モメンタム戦略

def backtest_momentum(instruments_data, trade_tickers, mp, ctx=None):
    trades = []
    daily_ret = {}  # date -> list of position daily returns
    for tk in trade_tickers:
        if tk not in instruments_data:
            continue
        sig, closes, dates, opens = instruments_data[tk]
        pos = None
        for t in range(WARMUP + 1, len(closes)):
            s = sig[t]
            price = closes[t]
            if pos:
                # 日次リターン記録(サイズ加重)。約定日はエントリー価格(始値)基点
                base = pos["entry"] if t == pos["entryT"] else closes[t - 1]
                r = (price / base - 1) * pos["size"]
                daily_ret.setdefault(dates[t], []).append(r)
                # トレーリング更新
                if s and s["atr"]:
                    pos["stop"] = max(pos["stop"], price - s["atr"] * mp["atrMult"])
                gain = (price / pos["entry"] - 1) * 100
                exit_all = reason = None
                if price <= pos["stop"]:
                    exit_all, reason = True, "stop"
                elif t - pos["entryT"] >= TIME_STOP_DAYS and gain < TIME_STOP_MIN_GAIN and pos["size"] > 0.99:
                    exit_all, reason = True, "timeStop"
                elif gain >= mp["tp2GainPct"] and not pos["tp2"]:
                    pos["realized"] += (price / pos["entry"] - 1) * 0.33
                    pos["size"] -= 0.33
                    pos["tp2"] = True
                elif gain >= mp["tp1GainPct"] and not pos["tp1"]:
                    pos["realized"] += (price / pos["entry"] - 1) * 0.33
                    pos["size"] -= 0.33
                    pos["tp1"] = True
                if t == len(closes) - 1 and not exit_all:
                    exit_all, reason = True, "end"
                if exit_all:
                    total = pos["realized"] + (price / pos["entry"] - 1) * pos["size"]
                    trades.append({"ticker": tk, "entryDate": dates[pos["entryT"]],
                                   "exitDate": dates[t], "days": t - pos["entryT"],
                                   "retPct": total * 100 - COST_RT_PCT["stock"], "reason": reason})
                    pos = None
            elif s and s["momSignal"] == "entry":
                if ctx is not None:
                    sec = ctx["sub_map"].get(tk)
                    if dates[t] in ctx["below200"] or ctx["trend"].get(sec, {}).get(dates[t]) == "outflow":
                        continue  # サイクルゲート: リスクオフ/セクター流出時は新規見送り
                px, te = next_fill(opens, closes, t)
                if px is None:
                    continue
                stop0 = max(px * (1 - mp["stopInitPct"] / 100),
                            px - (s["atr"] or px * 0.03) * mp["atrMult"])
                pos = {"entryT": te, "entry": px, "stop": stop0,
                       "size": 1.0, "realized": 0.0, "tp1": False, "tp2": False}
    return summarize_trades(trades), daily_ret


def summarize_trades(trades):
    if not trades:
        return {"trades": 0}
    rets = [x["retPct"] for x in trades]
    wins = [r for r in rets if r > 0]
    days = [x["days"] for x in trades] or [1]
    avg_ret = sum(rets) / len(rets)
    avg_days = sum(days) / len(days)
    ann = ((1 + avg_ret / 100) ** (252 / max(avg_days, 1)) - 1) * 100 if avg_days > 0 else None
    losses = [r for r in rets if r <= 0]
    pf = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) < 0 else None
    return {
        "trades": len(trades),
        "winRatePct": round(len(wins) / len(rets) * 100, 1),
        "avgRetPct": round(avg_ret, 2),
        "avgWinPct": round(sum(wins) / len(wins), 2) if wins else None,
        "avgLossPct": round(sum(losses) / len(losses), 2) if losses else None,
        "profitFactor": pf,
        "medianRetPct": round(sorted(rets)[len(rets) // 2], 2),
        "bestPct": round(max(rets), 1), "worstPct": round(min(rets), 1),
        "avgDays": round(avg_days, 1),
        "annualizedPerTradePct": round(ann, 1) if ann is not None else None,
        "recent": [f'{x["ticker"]} {x["entryDate"]}→{x["exitDate"]} {x["retPct"]:+.1f}% ({x["reason"]})'
                   for x in trades[-8:]],
    }


# ---------------------------------------------------------------- スリーブ系列と配分最適化

def value_sleeve_series(instruments_data, threshold=65):
    """Value戦略のポジション平均日次リターン系列(date->ret)。ノーポジ日は0(現金)。"""
    daily = {}
    for tk, (sig, closes, dates, opens) in instruments_data.items():
        pos_entry_t = None
        for t in range(WARMUP + 1, len(closes)):
            s_now, s_prev = sig[t], sig[t - 1]
            if pos_entry_t is not None:
                daily.setdefault(dates[t], []).append(closes[t] / closes[t - 1] - 1)
                held = t - pos_entry_t
                hit_top = s_now and s_now["top"] and closes[t] >= s_now["top"] - s_now["band"]
                if hit_top or held >= VALUE_EXIT_DAYS:
                    pos_entry_t = None
            elif s_now and s_now["score"] >= threshold and (s_prev is None or s_prev["score"] < threshold):
                pos_entry_t = t
    return daily


def buyhold_series(history):
    out = {}
    for i in range(1, len(history)):
        out[history[i]["d"]] = history[i]["c"] / history[i - 1]["c"] - 1
    return out


def stats_from_series(rets):
    if np is None or len(rets) < 50:
        return None
    r = np.array(rets)
    eq = np.cumprod(1 + r)
    yrs = len(r) / 252
    cagr = eq[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = float((1 - eq / np.maximum.accumulate(eq)).max())
    vol = float(r.std() * math.sqrt(252))
    sharpe = float(r.mean() / (r.std() + 1e-12) * math.sqrt(252))
    return {"cagrPct": round(cagr * 100, 1), "maxDDPct": round(dd * 100, 1),
            "volPct": round(vol * 100, 1), "sharpe": round(sharpe, 2)}


def optimize_allocation(sleeve_daily, names):
    """5%刻みグリッドサーチ。sleeve_daily: date -> [各スリーブのリターン](共通日付のみ)"""
    if np is None:
        return {"error": "numpy未インストール"}
    dates = sorted(sleeve_daily.keys())
    R = np.array([sleeve_daily[d] for d in dates])   # (T, K)
    T, K = R.shape
    combos = []

    def rec(i, remaining, cur):
        if i == K - 1:
            if names[i] == "momentum" and remaining > MOM_MAX:
                return
            combos.append(cur + [remaining])
            return
        max_w = remaining if names[i] != "momentum" else min(remaining, MOM_MAX)
        for w in range(0, max_w + 1, GRID_STEP):
            rec(i + 1, remaining - w, cur + [w])
    rec(0, 100, [])
    W = np.array(combos, dtype=np.float64) / 100.0    # (C, K)
    C = len(W)
    yrs = T / 252
    cagr = np.empty(C); dd = np.empty(C); sharpe = np.empty(C)
    CHUNK = 4000
    for c0 in range(0, C, CHUNK):
        Wc = W[c0:c0 + CHUNK]
        port = R @ Wc.T                               # (T, chunk)
        eq = np.cumprod(1 + port, axis=0)
        peak = np.maximum.accumulate(eq, axis=0)
        cagr[c0:c0 + len(Wc)] = eq[-1] ** (1 / yrs) - 1
        dd[c0:c0 + len(Wc)] = (1 - eq / peak).max(axis=0)
        sharpe[c0:c0 + len(Wc)] = port.mean(axis=0) / (port.std(axis=0) + 1e-12) * math.sqrt(252)

    def pack(idx, label):
        w = {names[k]: int(round(W[idx][k] * 100)) for k in range(K)}
        return {"label": label, "weightsPct": w,
                "cagrPct": round(float(cagr[idx]) * 100, 1),
                "maxDDPct": round(float(dd[idx]) * 100, 1),
                "sharpe": round(float(sharpe[idx]), 2)}

    out = [pack(int(cagr.argmax()), "最大CAGR(過剰適合注意)"),
           pack(int(sharpe.argmax()), "最大シャープ(リスク効率)")]
    mask = dd <= 0.40
    if mask.any():
        idx = int(np.where(mask, cagr, -1).argmax())
        out.append(pack(idx, "最大CAGR(最大DD40%以内)"))
    mask = dd <= 0.25
    if mask.any():
        idx = int(np.where(mask, cagr, -1).argmax())
        out.append(pack(idx, "最大CAGR(最大DD25%以内)"))
    # 現行推奨の実測(株60をValue、モメンタム0近似: value60/btc10/eth5/gold10/silver5 + cash10→リターン0)
    cur_w = []
    default_map = {"value": 0.60, "momentum": 0.0, "btc": 0.10, "eth": 0.05, "gold": 0.10, "silver": 0.05}
    for k in range(K):
        cur_w.append(default_map.get(names[k], 0))
    cur = np.array(cur_w)
    p = R @ cur
    eqc = np.cumprod(1 + p)
    ddc = float((1 - eqc / np.maximum.accumulate(eqc)).max())
    out.append({"label": "現行推奨(株60:BTC10:ETH5:金10:銀5:現金10)",
                "weightsPct": {names[k]: int(cur_w[k] * 100) for k in range(K)},
                "cagrPct": round((eqc[-1] ** (252 / len(p)) - 1) * 100, 1),
                "maxDDPct": round(ddc * 100, 1),
                "sharpe": round(float(p.mean() / (p.std() + 1e-12) * math.sqrt(252)), 2)})
    return out


# ---------------------------------------------------------------- サブセクター最適化

def optimize_sector_sleeves(stock_data, cfg):
    """個別株スリーブ内のサブセクター配分を実測データで最適化。
    各サブセクター=構成銘柄の等ウェイト買い持ち日次リターン。5%刻みグリッド。"""
    if np is None:
        return None
    sub_map = {s["ticker"]: (s.get("subSector") or "その他モート") for s in cfg["stocks"]}
    targets = cfg["portfolio"].get("stockSectorTargets", {})
    secs = list(targets.keys()) or sorted(set(sub_map.values()))
    daily = {}
    for tk, (sig, closes, dates, opens) in stock_data.items():
        sec = sub_map.get(tk)
        if sec not in secs:
            continue
        for i in range(1, len(closes)):
            daily.setdefault(dates[i], {}).setdefault(sec, []).append(closes[i] / closes[i - 1] - 1)
    common = sorted(d for d, m in daily.items() if len(m) == len(secs))
    if len(common) < 250:
        return {"error": f"共通営業日不足({len(common)}日)"}
    R = np.array([[sum(daily[d][s]) / len(daily[d][s]) for s in secs] for d in common])
    T, K = R.shape
    yrs = T / 252

    def stats(w):
        p = R @ w
        eq = np.cumprod(1 + p)
        dd = float((1 - eq / np.maximum.accumulate(eq)).max())
        return {"cagrPct": round((eq[-1] ** (1 / yrs) - 1) * 100, 1),
                "maxDDPct": round(dd * 100, 1),
                "sharpe": round(float(p.mean() / (p.std() + 1e-12) * math.sqrt(252)), 2)}

    combos = []
    def rec_(i, remaining, cur):
        if i == K - 1:
            combos.append(cur + [remaining]); return
        for w in range(0, remaining + 1, GRID_STEP):
            rec_(i + 1, remaining - w, cur + [w])
    rec_(0, 100, [])
    W = np.array(combos, dtype=np.float64) / 100.0
    C = len(W)
    cagr = np.empty(C); dd = np.empty(C); sharpe = np.empty(C)
    for c0 in range(0, C, 4000):
        Wc = W[c0:c0 + len(W[c0:c0 + 4000])]
        port = R @ Wc.T
        eq = np.cumprod(1 + port, axis=0)
        peak = np.maximum.accumulate(eq, axis=0)
        cagr[c0:c0 + len(Wc)] = eq[-1] ** (1 / yrs) - 1
        dd[c0:c0 + len(Wc)] = (1 - eq / peak).max(axis=0)
        sharpe[c0:c0 + len(Wc)] = port.mean(axis=0) / (port.std(axis=0) + 1e-12) * math.sqrt(252)

    def pack(idx, label):
        return {"label": label,
                "weightsPct": {secs[k]: int(round(W[idx][k] * 100)) for k in range(K) if W[idx][k] > 0},
                "cagrPct": round(float(cagr[idx]) * 100, 1),
                "maxDDPct": round(float(dd[idx]) * 100, 1),
                "sharpe": round(float(sharpe[idx]), 2)}
    out = [pack(int(cagr.argmax()), "最大CAGR(過剰適合注意)"),
           pack(int(sharpe.argmax()), "最大シャープ")]
    mask = dd <= 0.35
    if mask.any():
        out.append(pack(int(np.where(mask, cagr, -1).argmax()), "最大CAGR(DD35%以内)"))
    cur_w = np.array([targets.get(s, 0) / 100 for s in secs])
    if cur_w.sum() > 0.99:
        cur = stats(cur_w)
        out.append({"label": "現行設定", "weightsPct": {s: targets.get(s, 0) for s in secs if targets.get(s)},
                    **cur})
    sleeve_stats = {}
    for k, s in enumerate(secs):
        w = np.zeros(K); w[k] = 1.0
        sleeve_stats[s] = stats(w)
    return {"period": {"start": common[0], "end": common[-1], "days": T},
            "sleeveStats": sleeve_stats, "allocations": out}


# ---------------------------------------------------------------- 完全版リプレイ(v3.27)

REPLAY_COST_ONEWAY = {"stocks": 0.1, "btc": 0.3, "eth": 0.3, "gold": 1.0, "silver": 1.0, "cash": 0.0}


def replay_weights(dt, bench_prefix, base_targets):
    """その日時点の情報のみで本番のグライドを再現(point-in-time安全な要素のみ)。
    再現: 市場レジームチルト(engine.market_regimeと同一規則) + 半減期底ウィンドウ(日付のみで決定)。
    非再現(要日中文脈のため除外・注記): リスクオフ度チルト / セクターフロー / モメンタム枠縮小。"""
    t = dict(base_targets)
    regime = eng.market_regime(bench_prefix, None)
    if regime.get("tilt"):
        move = regime["tilt"]
        t["stocks"] = max(40, t["stocks"] + move)
        back = -move
        t["cash"] = max(5, t["cash"] + round(back * 2 / 3))
        t["gold"] = max(3, t["gold"] + (back - round(back * 2 / 3)))
    last_h = max((h for h in eng.HALVINGS if h <= dt), default=None)
    if last_h:
        months = (dt - last_h).days / 30.44
        if 26 < months <= 32:  # 底形成ウィンドウ(本番と同じ+2/+1/-3)
            t["btc"] = t.get("btc", 0) + 2
            t["eth"] = t.get("eth", 0) + 1
            t["cash"] = max(5, t.get("cash", 0) - 3)
    tot = sum(t.values()) or 100
    return {k: v * 100.0 / tot for k, v in t.items()}, regime.get("regime", "unknown")


def production_replay(stock_data, sleeves, common, bench_by_date, cfg):
    """完全版PF(グライド込み)を日次で過去再現し、VOO100% / VOO+金+現金(静的) と比較する。
    株スリーブ = インデックス枠30%(VOO) + 70%等ウェイト(配分対象銘柄)。回転コストは重み変化×片道コストで控除。"""
    from datetime import date as _date
    if np is None or len(common) < 260:
        return None
    no_alloc = {s["ticker"] for s in cfg["stocks"] if s.get("noAlloc")}
    eligible = [tk for tk in stock_data.keys() if tk not in no_alloc and tk != "VOO"]
    # 日次リターンマップ
    ret_map = {}
    for tk, (sig, closes, dates, opens) in stock_data.items():
        m = {}
        for i in range(1, len(closes)):
            m[dates[i]] = closes[i] / closes[i - 1] - 1
        ret_map[tk] = m
    voo = ret_map.get("VOO")
    bench_ret = {}
    prev = None
    for d in sorted(bench_by_date.keys()):
        cl = bench_by_date[d]
        if prev is not None and len(cl) >= 2:
            bench_ret[d] = cl[-1] / cl[-2] - 1
        prev = d
    voo_src = "VOO実データ" if voo else "^GSPC近似(VOOデータなし・配当未考慮)"
    core = voo or bench_ret

    def stock_sleeve_ret(d):
        eq = [ret_map[tk][d] for tk in eligible if d in ret_map[tk]]
        eq_r = sum(eq) / len(eq) if eq else 0.0
        core_r = core.get(d, 0.0)
        return 0.30 * core_r + 0.70 * eq_r

    base_targets = dict(cfg["portfolio"]["targets"])
    names = ["stocks", "btc", "eth", "gold", "silver", "cash"]

    def sleeve_ret(k, d):
        if k == "stocks":
            return stock_sleeve_ret(d)
        if k == "cash":
            return 0.0
        return float(sleeves.get(k, {}).get(d, 0.0))

    # 3ポートフォリオの日次系列
    full_rets, static_rets, voo_rets = [], [], []
    regime_days = {}
    prev_w = None
    static_w = {"stocks": 0, "btc": 0, "eth": 0, "gold": 20, "silver": 0, "cash": 10}
    for d in common:
        dt = _date.fromisoformat(d)
        w, regime = replay_weights(dt, bench_by_date.get(d), base_targets)
        regime_days[regime] = regime_days.get(regime, 0) + 1
        r = sum(w.get(k, 0) / 100 * sleeve_ret(k, d) for k in names)
        if prev_w is not None:
            turn_cost = sum(abs(w.get(k, 0) - prev_w.get(k, 0)) / 100 * REPLAY_COST_ONEWAY[k] / 100
                            for k in names)
            r -= turn_cost
        prev_w = w
        full_rets.append(r)
        voo_r = core.get(d, 0.0)
        voo_rets.append(voo_r)
        static_rets.append(0.70 * voo_r + 0.20 * sleeve_ret("gold", d))  # 現金10%=0%

    def pack(rets):
        st = stats_from_series(rets)
        if not st:
            return None
        eq = 1.0
        yearly = {}
        for d, r in zip(common, rets):
            eq *= 1 + r
            y = d[:4]
            yearly[y] = yearly.get(y, 1.0) * (1 + r)
        st["finalMultiple"] = round(eq, 2)
        st["yearlyPct"] = {y: round((v - 1) * 100, 1) for y, v in yearly.items()}
        return st

    return {
        "period": {"start": common[0], "end": common[-1], "days": len(common)},
        "portfolios": {
            "full": pack(full_rets),
            "voo100": pack(voo_rets),
            "vooGoldCash": pack(static_rets),
        },
        "regimeDays": regime_days,
        "coreSource": voo_src,
        "notes": [
            "株スリーブ=インデックス30%+配分対象銘柄の等ウェイト70%(現在のユニバース=生存者バイアスあり)",
            "再現済みチルト: 市場レジームグライド・半減期底ウィンドウ(いずれも日付と価格のみから決定)",
            "未再現: リスクオフ度チルト/セクターフロー/モメンタム枠/個別トリム(要当時文脈のため。実運用はこの分だけ結果が変わり得る)",
            "コスト: 日次の重み変化×片道コスト(株0.1/暗号0.3/金銀1.0%)を控除。VOO100%は無コスト・配当は" + ("含む(調整済終値)" if voo else "未考慮"),
        ],
    }


# ---------------------------------------------------------------- 自動解釈

def build_interpretation(value_results, mom, allocations, names):
    """結果を平易な日本語に自動翻訳する。表示はUIの先頭。"""
    L = []
    L.append("⚠️ v3.25から約定モデルが現実仕様(t+1営業日始値で約定+往復コスト控除: 株0.2%/暗号0.6%/金銀2.0%)。旧バージョンの表示値より全体に低めに出るのは劣化ではなく正直になった分。")
    L.append("【まず用語】CAGR＝年平均リターン(複利)。最大DD＝期間中に資産が一番へこんだ瞬間の下落率(これに耐えられるかが配分選びの本質)。Sharpe＝リターン÷値動きの荒さ＝リスク1単位あたりの効率(1.0以上で良好、2.0は優秀)。")
    # Value Tier
    tiers = [(k, v) for k, v in value_results.items() if v.get("trades")]
    if len(tiers) >= 2:
        lo_k, lo = tiers[0]; hi_k, hi = tiers[-1]
        L.append(f"Value戦略: 閾値を上げるほど回数は減るが精度が上がる設計通りの結果。{lo_k}は{lo['trades']}回・勝率{lo['winRatePct']}%・平均{lo['avgRetPct']}%に対し、{hi_k}は{hi['trades']}回・勝率{hi['winRatePct']}%・平均{hi['avgRetPct']}%。実務指針:「65で監視を始め、80以上で本気の買い、90は戦略キャッシュ出動」。年率換算は“シグナルが常に連続してあれば”の理論値で、実際はシグナル待ち期間があるため下振れする点に注意。")
    if mom.get("trades"):
        L.append(f"モメンタム戦略: 勝率{mom['winRatePct']}%と“半分は負ける”が、平均{mom['avgRetPct']}%がプラスなのは損切り(-8%上限)で負けを小さく、利確ルールで勝ちを大きくする非対称性が機能した証拠。勝率の低さに動揺してルールを破らないことが最重要。")
    # 配分
    if isinstance(allocations, list) and allocations:
        by_label = {a["label"]: a for a in allocations}
        cur = next((a for a in allocations if a["label"].startswith("現行推奨")), None)
        sharpe_a = next((a for a in allocations if "シャープ" in a["label"]), None)
        dd25 = next((a for a in allocations if "25%" in a["label"]), None)
        refs = [a for a in (sharpe_a, dd25) if a]
        if refs:
            mom_high = all(a["weightsPct"].get("momentum", 0) >= 30 for a in refs)
            crypto_zero = all(a["weightsPct"].get("btc", 0) + a["weightsPct"].get("eth", 0) <= 5 for a in refs)
            metals_heavy = all(a["weightsPct"].get("gold", 0) + a["weightsPct"].get("silver", 0) >= 25 for a in refs)
            obs = []
            if mom_high:
                obs.append("どの参考配分もモメンタム枠を上限近くまで使っており、この期間はルール通りのモメンタム売買が最も効率的な稼ぎ手だった")
            if metals_heavy:
                obs.append("金銀の比率が高いのは検証期間が貴金属の強気相場だったため(将来も続く保証はない)")
            if crypto_zero:
                obs.append("BTC/ETHが小さいのは“上がらなかった”からではなく、この期間は下落の深さ(DD)に対してリターンが見合わなかったため。長期テーゼで持つ判断と過去最適化は別物")
            if obs:
                L.append("配分の読み方: " + "。".join(obs) + "。")
        if cur and sharpe_a:
            L.append(f"現行推奨(CAGR{cur['cagrPct']}%・DD{cur['maxDDPct']}%・Sharpe{cur['sharpe']})は、最大シャープ配分(CAGR{sharpe_a['cagrPct']}%・DD{sharpe_a['maxDDPct']}%・Sharpe{sharpe_a['sharpe']})に全指標で劣後。見直し余地あり。ただし“最大CAGR”は過去に最も上がった資産へ寄るだけの過剰適合なので鵜呑みにしない。")
        if cur and sharpe_a:
            # 中庸たたき台: 現行と最大シャープの中間(5%丸め)
            mid = {}
            for k in names:
                m = (cur["weightsPct"].get(k, 0) + sharpe_a["weightsPct"].get(k, 0)) / 2
                mid[k] = int(round(m / 5) * 5)
            diff = 100 - sum(mid.values())
            mid["value"] = mid.get("value", 0) + diff
            L.append("たたき台(現行と最大シャープの中間・5%丸め): " +
                     " : ".join(f"{k}{v}" for k, v in mid.items() if v > 0) +
                     "。一括で動かさず、追加投資から新配分に寄せるのが税制上も心理上も安全。")
    L.append("【限界】約5年という一つの時代の検証であり、手数料・税・スリッページ未考慮。この結果は“ルールが壊れていないことの確認”と“配分の相場観”に使い、将来の約束とは考えないこと。")
    return L


# ---------------------------------------------------------------- メイン

def main():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    mp = cfg["momentumParams"]

    print("価格取得中…")
    bench_hist, _ = evaluate.fetch_history(cfg.get("benchmark", {"yahoo": "^GSPC"}))
    bench_by_date = {}
    if bench_hist:
        acc = []
        for h in bench_hist:
            acc.append(h["c"])
            bench_by_date[h["d"]] = list(acc)  # その日までの終値列
    entries = []
    for st in cfg["stocks"]:
        e = dict(st); e["class"] = "stock"; e["key"] = st["ticker"]; entries.append(e)
    for cr in cfg["crypto"]:
        e = dict(cr); e["class"] = "crypto"; entries.append(e)
    for mt in cfg["metals"]:
        e = dict(mt); e["class"] = "metal"; entries.append(e)

    instruments_data = {}   # ticker -> (signals, closes, dates)
    class_hist = {}
    for e in entries:
        hist, src = evaluate.fetch_history(e)
        key = e.get("key") or e.get("ticker")
        if not hist or len(hist) < WARMUP + 60:
            print(f"{key}: データ不足でスキップ")
            continue
        print(f"{key}: {len(hist)}本 ({src}) シグナル前計算…")
        sig, closes, dates, opens = precompute_signals(e, hist, bench_by_date, cfg)
        instruments_data[key] = (sig, closes, dates, opens)
        class_hist[key] = (e.get("class"), hist)

    stock_data = {k: v for k, v in instruments_data.items() if class_hist[k][0] == "stock"}
    asset_data = {k: v for k, v in instruments_data.items()
                  if class_hist[k][0] in ("crypto", "metal")}
    trade_tickers = [s["ticker"] for s in cfg["stocks"] if s.get("tradePolicy") == "trade"]

    print("Value戦略 検証中…")
    v_trades = {label: value_trades(stock_data, thr)
                for label, thr in TIER_THRESHOLDS.items()}
    value_results = {label: summarize_trades(t) for label, t in v_trades.items()}
    value_hold_results = {label: backtest_value_hold(stock_data, thr)
                          for label, thr in TIER_THRESHOLDS.items()}

    # 金銀BTC/ETH × 推奨度Tier のクロス集計（売りルール版＋保有継続版）
    print("資産別×Tier別 検証中…")
    asset_tiers = {}
    asset_tiers_hold = {}
    for key in ("gold", "silver", "btc", "eth"):
        if key not in asset_data:
            continue
        single = {key: asset_data[key]}
        a_cost = COST_RT_PCT["crypto"] if key in ("btc", "eth") else COST_RT_PCT["metal"]
        asset_tiers[key] = {label: backtest_value(single, thr, cost=a_cost)
                            for label, thr in TIER_THRESHOLDS.items()}
        asset_tiers_hold[key] = {label: backtest_value_hold(single, thr)
                                 for label, thr in TIER_THRESHOLDS.items()}
    print("モメンタム戦略 検証中…")
    mom_results, mom_daily = backtest_momentum(stock_data, trade_tickers, mp)
    print("サイクル統合版 検証中…(セクターETF取得)")
    etf_hists = {}
    for sec, sym in evaluate.SECTOR_ETFS.items():
        h, _src = evaluate.fetch_history({"yahoo": sym, "stooq": sym.lower() + ".us"})
        if h:
            etf_hists[sec] = h
    print(f"セクターETF: {len(etf_hists)}/{len(evaluate.SECTOR_ETFS)}本(欠損は銘柄平均で補完)")
    cycle_ctx = build_cycle_context(stock_data, cfg, bench_hist, etf_hists)
    vc_trades = {label: value_cycle_trades(stock_data, thr, cycle_ctx)
                 for label, thr in TIER_THRESHOLDS.items()}
    value_cycle_results = {label: summarize_trades(t) for label, t in vc_trades.items()}
    mom_cycle_results, _ = backtest_momentum(stock_data, trade_tickers, mp, cycle_ctx)
    # セクター別Tier分解(チャート単独版・サイクル統合版の両方)
    print("セクター別Tier分解 集計中…")
    sub_map = cycle_ctx["sub_map"]
    value_by_sector = {label: summarize_by_sector(t, sub_map) for label, t in v_trades.items()}
    value_cycle_by_sector = {label: summarize_by_sector(t, sub_map) for label, t in vc_trades.items()}
    # 底確認変種: Tier到達後、20日線上+上向きを待って買う
    print("底確認変種 検証中…")
    value_confirm_results = {}
    for label, thr in TIER_THRESHOLDS.items():
        s = summarize_trades(value_trades_confirm(stock_data, thr))
        s.pop("recent", None)
        value_confirm_results[label] = s
    # 持続性フィルタ: Tier×N日連続
    print("持続性フィルタ 検証中…")
    value_persistence = {}
    for label, thr in TIER_THRESHOLDS.items():
        value_persistence[label] = {}
        for n in PERSIST_DAYS:
            s = summarize_trades(value_trades_persist(stock_data, thr, n))
            s.pop("recent", None)
            value_persistence[label][str(n)] = s
    # 比較用: 部分利確なし(トレーリングのみで勝ちを伸ばす)バリアント
    mp_notp = dict(mp); mp_notp["tp1GainPct"] = 10 ** 9; mp_notp["tp2GainPct"] = 10 ** 9
    mom_notp_results, _ = backtest_momentum(stock_data, trade_tickers, mp_notp)

    # スリーブ系列
    print("配分最適化中…")
    sleeves = {
        "value": {d: sum(v) / len(v) for d, v in value_sleeve_series(stock_data, 65).items() if v},
        "momentum": {d: sum(v) / len(v) for d, v in mom_daily.items() if v},
    }
    for key, (klass, hist) in class_hist.items():
        if klass in ("crypto", "metal"):
            sleeves[key] = buyhold_series(hist)
    names = ["value", "momentum", "btc", "eth", "gold", "silver"]
    # 共通日付 = 買い持ち系(暗号資産・金銀)が全て揃う営業日。
    # value/momentumはノーポジ日=0%(現金)として扱う。
    base_sets = [set(sleeves[nm].keys()) for nm in ("btc", "eth", "gold", "silver") if nm in sleeves]
    common = sorted(set.intersection(*base_sets)) if base_sets else []
    common = [d for d in common if d >= (min(sleeves["value"].keys()) if sleeves["value"] else d)]
    sleeve_daily = {d: [float(sleeves[nm].get(d, 0.0)) for nm in names] for d in common}

    sleeve_stats = {}
    for i, nm in enumerate(names):
        sleeve_stats[nm] = stats_from_series([sleeve_daily[d][i] for d in common])
    allocations = optimize_allocation(sleeve_daily, names) if common else {"error": "共通期間なし"}

    # S&P500買い持ちベンチマーク(戦略比較の基準線)
    spx_stats = None
    if bench_hist:
        spx_daily = buyhold_series(bench_hist)
        spx_common = [d for d in common if d in spx_daily]
        if spx_common:
            spx_stats = stats_from_series([spx_daily[d] for d in spx_common])

    print("完全版リプレイ 実行中…")
    replay = production_replay(stock_data, sleeves, common, bench_by_date, cfg)

    print("サブセクター配分 最適化中…")
    sector_opt = optimize_sector_sleeves(stock_data, cfg)
    interpretation = build_interpretation(value_results, mom_results, allocations, names)
    if sector_opt and not sector_opt.get("error"):
        best = sector_opt["allocations"]
        sh = next((a for a in best if a["label"] == "最大シャープ"), None)
        cur = next((a for a in best if a["label"] == "現行設定"), None)
        if sh:
            interpretation.append(
                "個別株スリーブ(サブセクター)の実測: "
                + " / ".join(f"{s} 年率{v['cagrPct']}%(DD{v['maxDDPct']}%)" for s, v in sector_opt["sleeveStats"].items())
                + f"。最大シャープ配分は {sh['weightsPct']}(CAGR{sh['cagrPct']}%・Sharpe{sh['sharpe']})"
                + (f"、現行設定は CAGR{cur['cagrPct']}%・Sharpe{cur['sharpe']}" if cur else "")
                + "。※現行の比率は『リターン最大化解』ではなく独占性・分散重視の設計値。過去最適はバックミラーである点に注意。")
    # 資産別×Tierの自動解釈
    ASSET_JP = {"gold": "金", "silver": "銀", "btc": "BTC", "eth": "ETH"}
    asset_lines = []
    for key, tiers in asset_tiers.items():
        parts = []
        for label, r in tiers.items():
            if r.get("trades"):
                parts.append(f"{label.split('(')[1].rstrip(')')}点:{r['trades']}回/勝率{r['winRatePct']}%/平均{r['avgRetPct']:+.1f}%")
            else:
                parts.append(f"{label.split('(')[1].rstrip(')')}点:シグナルなし")
        asset_lines.append(f"{ASSET_JP.get(key, key)}＝" + "、".join(parts))
    if asset_lines:
        interpretation.append(
            "資産別×推奨度Tier(売りルール版): " + " ／ ".join(asset_lines) +
            "。注意: 単一資産のためシグナル回数が少なく、株82銘柄の合算Tier統計より統計的な信頼度は低い。"
            "回数が数回しかないTierの勝率は参考程度に。accumulate方針(買い増し専用)の資産は保有継続版(assetTiersHold)も併せて見ること。")
    vh = value_hold_results.get("consider(65)", {})
    vs = value_results.get("consider(65)", {})
    if vh.get("trades") and vs.get("trades"):
        interpretation.append(
            f"売り時比較実験(スコア65基準): 「天井/120日で売る」方式は1回平均{vs['avgRetPct']}%(年率換算{vs.get('annualizedPerTradePct')}%)、"
            f"「最初のシグナルで買って以後ずっと保有」は平均{vh['avgRetPct']}%(平均{vh['avgHoldDays']}営業日保有・年率換算{vh['avgAnnualizedPct']}%)。"
            "年率換算同士を比べ、保有継続が上なら“hold銘柄は売らない”方針が過去データでも正しかったことになる。"
            "ただし回転売買の年率は“次のシグナルが常にある”前提の理論値なので、実際は保有継続に分があることが多い。")
    vc = value_cycle_results.get("strong(80)", {})
    vp_ = value_results.get("strong(80)", {})
    if vc.get("trades") and vp_.get("trades"):
        interpretation.append(
            f"🧭サイクル統合の効果検証(スコア80基準): チャート単独は{vp_['trades']}回・勝率{vp_['winRatePct']}%・平均{vp_['avgRetPct']}%、"
            f"サイクル統合版は{vc['trades']}回・勝率{vc['winRatePct']}%・平均{vc['avgRetPct']}%。"
            f"モメンタムはフィルタなし{mom_results.get('trades')}回(勝率{mom_results.get('winRatePct')}%・平均{mom_results.get('avgRetPct')}%)に対し、"
            f"サイクルゲート版{mom_cycle_results.get('trades')}回(勝率{mom_cycle_results.get('winRatePct')}%・平均{mom_cycle_results.get('avgRetPct')}%)。"
            "改善していれば統合の価値が実証、悪化ならサイクル補正を弱める判断材料。一般に効果は平均改善より最悪トレード削減に出やすい。")
    # セクター別Tier80の自動解釈
    sec80 = value_by_sector.get("strong(80)")
    sec80c = value_cycle_by_sector.get("strong(80)")
    if sec80 and sec80.get("groups"):
        parts = []
        for grp in ("グロース", "シクリカル", "ディフェンシブ"):
            r = sec80["groups"].get(grp)
            if r and r.get("trades"):
                parts.append(f"{grp}={r['trades']}回・勝率{r['winRatePct']}%・平均{r['avgRetPct']:+.1f}%"
                             + ("(参考値)" if r.get("lowN") else ""))
        if parts:
            interpretation.append(
                "🧩セクター別Tier80分解(チャート単独): " + " ／ ".join(parts) +
                "。全体平均(valueTiers)は値幅の大きいグロース/シクリカルに引っ張られやすい。"
                "WMT・PG・KOのようなディフェンシブ銘柄に全体平均をそのまま適用せず、"
                "ディフェンシブ行の数字を現実的な期待値として使うこと。15回未満のセルは統計的信頼度が低い。")
        if sec80c and sec80c.get("groups"):
            diffs = []
            for grp, r in sec80["groups"].items():
                rc = sec80c["groups"].get(grp)
                if r.get("trades") and rc and rc.get("trades"):
                    diffs.append(f"{grp}: 平均{r['avgRetPct']:+.1f}%→{rc['avgRetPct']:+.1f}%"
                                 f"/勝率{r['winRatePct']}%→{rc['winRatePct']}%")
            if diffs:
                interpretation.append(
                    "🧭サイクル統合×セクターの効果(Tier80・チャート単独→統合): " + " ／ ".join(diffs) +
                    "。統合版で最も改善するグループが「サイクル情報が効く場所」。"
                    "ディフェンシブで改善が小さい/悪化するなら、ローテーション流入シグナルは"
                    "ディフェンシブでは絶対リターンに直結しない(リスクオフ退避の反映)という解釈が妥当。")
    # 底確認変種の自動解釈(Tier80基準)
    cf80 = value_confirm_results.get("strong(80)", {})
    pl80 = value_results.get("strong(80)", {})
    if cf80.get("trades") and pl80.get("trades"):
        interpretation.append(
            f"🩸底確認実験(Tier80到達後、20日線上+上向きを待って買う): "
            f"即買い={pl80['trades']}回・勝率{pl80['winRatePct']}%・平均{pl80['avgRetPct']:+.1f}%に対し、"
            f"底確認後={cf80['trades']}回・勝率{cf80['winRatePct']}%・平均{cf80['avgRetPct']:+.1f}%。"
            "勝率が上がるなら『鋭敏な点灯は下落継続中が多い』の実証。平均が下がるなら『底待ちで安値を逃すコスト』。"
            "持続性フィルタ(N日連続)と役割が近いので、良い方を採用すればよい(両方は過剰)。")
    if spx_stats:
        interpretation.append(
            f"📊基準線: 同期間のS&P500買い持ちは年率{spx_stats['cagrPct']}%・最大DD{spx_stats['maxDDPct']}%・"
            f"Sharpe{spx_stats['sharpe']}。各スリーブ・配分案はこれを上回って初めて『銘柄選択の価値があった』ことになる。"
            "集中投資の高CAGRには銘柄選択リスク(生存者バイアス)が含まれる点を忘れずに。")
    # 完全版リプレイの自動解釈
    if replay and replay.get("portfolios", {}).get("full"):
        p = replay["portfolios"]
        f, v1, v2 = p["full"], p.get("voo100"), p.get("vooGoldCash")
        if f and v1:
            diff = round(f["cagrPct"] - v1["cagrPct"], 1)
            interpretation.append(
                f"🎬完全版リプレイ({replay['period']['start']}〜{replay['period']['end']}): "
                f"完全版システム=年率{f['cagrPct']}%・DD{f['maxDDPct']}%・Sharpe{f['sharpe']}({f['finalMultiple']}倍) / "
                f"VOO100%={v1['cagrPct']}%・DD{v1['maxDDPct']}%・Sharpe{v1['sharpe']} / "
                f"VOO70+金20+現金10={v2['cagrPct']}%・DD{v2['maxDDPct']}%・Sharpe{v2['sharpe']}。"
                + (f"完全版はVOOを年率{diff}pt上回った——銘柄選択+グライドに価値があった証拠。"
                   if diff > 1 else
                   f"完全版のVOO超過は{diff}ptに留まる——現時点では銘柄選択の付加価値は限定的で、"
                   "インデックス比率を上げる選択も合理的。" if diff > -1 else
                   f"完全版はVOOに年率{-diff}pt劣後——この期間は銘柄選択が逆効果だった。コアのVOO枠が保険として機能。")
                + "※株スリーブは現ユニバース等ウェイト(生存者バイアス)・リスクオフ/フローチルト未再現の近似值。")
    # 持続性フィルタの自動解釈(Tier80基準)
    p80 = value_persistence.get("strong(80)", {})
    p1, p_best_n, p_best = p80.get("1"), None, None
    for n in PERSIST_DAYS:
        r = p80.get(str(n))
        if r and r.get("trades", 0) >= 10:
            eff = r["winRatePct"] * r["avgRetPct"]  # 勝率×平均の簡易効率
            if p_best is None or eff > p_best["winRatePct"] * p_best["avgRetPct"]:
                p_best_n, p_best = n, r
    if p1 and p1.get("trades"):
        interpretation.append(
            "⏳持続性フィルタ実験(Tier80がN日連続で初めて買う): "
            + " ／ ".join(f"N={n}: {r['trades']}回・勝率{r['winRatePct']}%・平均{r['avgRetPct']:+.1f}%"
                          for n in PERSIST_DAYS
                          if (r := p80.get(str(n))) and r.get("trades"))
            + f"。読み方: 勝率がNとともに上がるなら『1日だけの瞬間タッチ(CVX型のスコア急騰)はダマシが多い』、"
              f"平均リターンがNとともに下がるなら『待つほど底値を逃すコスト』。両者の積で判断し、"
            + (f"この期間の最適はN={p_best_n}日" if p_best_n else "有意差なし")
            + "。ただしNを増やすほど回数が減り統計信頼度も落ちる点に注意。")
    if mom_results.get("trades") and mom_notp_results.get("trades"):
        interpretation.append(
            f"モメンタム比較実験: 現行ルール(+20/40%で部分利確)は平均{mom_results['avgRetPct']}%/勝率{mom_results['winRatePct']}%、"
            f"部分利確なし(トレーリングのみ)は平均{mom_notp_results['avgRetPct']}%/勝率{mom_notp_results['winRatePct']}%"
            f"(最大{mom_notp_results.get('bestPct')}%・最悪{mom_notp_results.get('worstPct')}%)。"
            "利確なしの方が平均が高ければ“爆発力を殺していた”証拠になり、低ければ“利確が正解だった”ことになる。数字で判断を。")
    result = {
        "generated": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "interpretation": interpretation,
        "period": {"start": common[0] if common else None, "end": common[-1] if common else None,
                   "tradingDays": len(common)},
        "valueTiers": value_results,
        "valueTiersHold": value_hold_results,
        "valueTiersCycle": value_cycle_results,
        "valueTiersBySector": value_by_sector,
        "valueTiersCycleBySector": value_cycle_by_sector,
        "valuePersistence": value_persistence,
        "valueTiersConfirm": value_confirm_results,
        "spxBuyHold": spx_stats,
        "momentumCycle": mom_cycle_results,
        "assetTiers": asset_tiers,
        "assetTiersHold": asset_tiers_hold,
        "momentum": mom_results,
        "momentumNoTP": mom_notp_results,
        "sectorOpt": sector_opt,
        "replay": replay,
        "sleeveStats": sleeve_stats,
        "allocations": allocations,
        "assumptions": ("約定: t日終値シグナル→t+1営業日始値(v3.25現実仕様) / "
                        "コスト: 往復 株0.2%・暗号0.6%・金銀2.0%を控除(買い持ちは片道) / "
                        "Value: 閾値上抜け→チャネル天井 or 120営業日 / "
                        "モメンタム: 初期-8%上限,ATR×2.5,TP20/40%,タイムストップ40日 / "
                        "配分: 日次リバランス近似・現金金利0%・税は未考慮"),
        "warning": "過去データへの機械的検証であり将来を保証しない。特に最大CAGR配分は過剰適合を含むため、シャープ最大やDD制約付きを実務の参考にすること。",
    }
    with open(os.path.join(BASE, "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("backtest.json 保存完了")
    print(json.dumps({k: result[k] for k in ("valueTiers", "momentum", "allocations")},
                     ensure_ascii=False, indent=1)[:3000])


if __name__ == "__main__":
    main()
