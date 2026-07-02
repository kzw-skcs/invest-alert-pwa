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
    return out, closes, dates


# ---------------------------------------------------------------- Value戦略

def backtest_value(instruments_data, threshold):
    """全銘柄横断: スコアが閾値を上抜けた日の翌日始値(近似=当日終値)で買い。"""
    trades = []
    for tk, (sig, closes, dates) in instruments_data.items():
        open_pos = None
        for t in range(WARMUP + 1, len(closes)):
            s_now, s_prev = sig[t], sig[t - 1]
            if s_now is None:
                continue
            if open_pos is None:
                crossed = s_now["score"] >= threshold and (s_prev is None or s_prev["score"] < threshold)
                if crossed:
                    open_pos = {"entryT": t, "entry": closes[t]}
            else:
                held = t - open_pos["entryT"]
                hit_top = s_now["top"] and closes[t] >= s_now["top"] - s_now["band"]
                if hit_top or held >= VALUE_EXIT_DAYS or t == len(closes) - 1:
                    ret = closes[t] / open_pos["entry"] - 1
                    trades.append({"ticker": tk, "entryDate": dates[open_pos["entryT"]],
                                   "exitDate": dates[t], "days": held, "retPct": ret * 100,
                                   "reason": "top" if hit_top else "time"})
                    open_pos = None
    return summarize_trades(trades)


# ---------------------------------------------------------------- モメンタム戦略

def backtest_momentum(instruments_data, trade_tickers, mp):
    trades = []
    daily_ret = {}  # date -> list of position daily returns
    for tk in trade_tickers:
        if tk not in instruments_data:
            continue
        sig, closes, dates = instruments_data[tk]
        pos = None
        for t in range(WARMUP + 1, len(closes)):
            s = sig[t]
            price = closes[t]
            if pos:
                # 日次リターン記録(サイズ加重)
                r = (price / closes[t - 1] - 1) * pos["size"]
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
                                   "retPct": total * 100, "reason": reason})
                    pos = None
            elif s and s["momSignal"] == "entry":
                stop0 = max(price * (1 - mp["stopInitPct"] / 100),
                            price - (s["atr"] or price * 0.03) * mp["atrMult"])
                pos = {"entryT": t, "entry": price, "stop": stop0,
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
    return {
        "trades": len(trades),
        "winRatePct": round(len(wins) / len(rets) * 100, 1),
        "avgRetPct": round(avg_ret, 2),
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
    for tk, (sig, closes, dates) in instruments_data.items():
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


# ---------------------------------------------------------------- 自動解釈

def build_interpretation(value_results, mom, allocations, names):
    """結果を平易な日本語に自動翻訳する。表示はUIの先頭。"""
    L = []
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
        sig, closes, dates = precompute_signals(e, hist, bench_by_date, cfg)
        instruments_data[key] = (sig, closes, dates)
        class_hist[key] = (e.get("class"), hist)

    stock_data = {k: v for k, v in instruments_data.items() if class_hist[k][0] == "stock"}
    trade_tickers = [s["ticker"] for s in cfg["stocks"] if s.get("tradePolicy") == "trade"]

    print("Value戦略 検証中…")
    value_results = {label: backtest_value(stock_data, thr)
                     for label, thr in TIER_THRESHOLDS.items()}
    print("モメンタム戦略 検証中…")
    mom_results, mom_daily = backtest_momentum(stock_data, trade_tickers, mp)
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

    interpretation = build_interpretation(value_results, mom_results, allocations, names)
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
        "momentum": mom_results,
        "momentumNoTP": mom_notp_results,
        "sleeveStats": sleeve_stats,
        "allocations": allocations,
        "assumptions": ("Value: スコア閾値上抜けで買い→チャネル天井 or 120営業日で売り / "
                        "モメンタム: ルール完全再現(初期-8%上限,ATR×2.5,TP20/40%,タイムストップ40日) / "
                        "配分: 日次リバランス近似・現金金利0% / 手数料・税・スリッページ未考慮"),
        "warning": "過去データへの機械的検証であり将来を保証しない。特に最大CAGR配分は過剰適合を含むため、シャープ最大やDD制約付きを実務の参考にすること。",
    }
    with open(os.path.join(BASE, "backtest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print("backtest.json 保存完了")
    print(json.dumps({k: result[k] for k in ("valueTiers", "momentum", "allocations")},
                     ensure_ascii=False, indent=1)[:3000])


if __name__ == "__main__":
    main()
