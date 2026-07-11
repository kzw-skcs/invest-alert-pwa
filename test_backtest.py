# -*- coding: utf-8 -*-
"""backtest.py の約定モデル(v3.25: t+1始値+コスト控除)のテスト。ネット不要・合成データのみ。"""
import sys

import backtest as bt

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")


W = bt.WARMUP


def mk(scores, closes_seq, opens_seq=None):
    """WARMUP埋め+指定シーケンスでinstruments_dataの1銘柄分を作る。"""
    n = W + 1 + len(scores)
    closes = [100.0] * (W + 1) + list(closes_seq)
    opens = ([None] * (W + 1) + list(opens_seq)) if opens_seq else [None] * n
    dates = [f"D{i:05d}" for i in range(n)]
    sig = [None] * (W + 1) + [{"score": s, "top": None, "band": 0, "atr": 1.0,
                               "momSignal": None} for s in scores]
    return (sig, closes, dates, opens)


print("[next_fill]")
opens = [None] * 5 + [101.5]
closes = [100.0] * 6
px, te = bt.next_fill(opens, closes, 4)
check("t+1始値で約定", px == 101.5 and te == 5)
px, te = bt.next_fill([None] * 6, closes, 4)
check("open欠損→t+1終値で近似", px == 100.0 and te == 5)
px, te = bt.next_fill(opens, closes, 5)
check("最終バーでは約定不可", px is None and te is None)

print("[value_trades: t+1始値エントリー+コスト]")
# クロス翌日の始値102で買い、120日ルールで決済
scores = [70, 85] + [70] * 130
closes_seq = [100.0] * 2 + [110.0] * 130
opens_seq = [None, None, 102.0] + [110.0] * 129
d = {"A": mk(scores, closes_seq, opens_seq)}
tr = bt.value_trades(d, 80)
check("1トレード成立", len(tr) == 1, tr[:1])
if tr:
    check("エントリーはクロス翌日", tr[0]["entryDate"] == f"D{W + 3:05d}", tr[0]["entryDate"])
    # entry=102(始値)、exit=110、gross=+7.843% - 0.2%コスト
    expect = (110.0 / 102.0 - 1) * 100 - bt.COST_RT_PCT["stock"]
    check("始値基準+コスト控除のリターン", abs(tr[0]["retPct"] - expect) < 0.01,
          f"{tr[0]['retPct']:.2f} vs {expect:.2f}")

# 最終バーでのクロスは約定不可(未来の始値が存在しない)
d2 = {"A": mk([70, 85], [100.0, 100.0])}
check("最終バークロスは取引なし", len(bt.value_trades(d2, 80)) == 0)

# コスト指定(暗号資産0.6%)が効くこと
tr_c = bt.value_trades(d, 80, cost=bt.COST_RT_PCT["crypto"])
if tr and tr_c:
    check("コスト引数で差が出る", abs((tr[0]["retPct"] - tr_c[0]["retPct"]) - (0.6 - 0.2)) < 0.01)

print("[value_trades_persist: N=3も翌日始値]")
scores = [70] + [85] * 5 + [70] * 130
closes_seq = [100.0] * 6 + [108.0] * 130
opens_seq = [None] * 4 + [103.0] + [108.0] * 131
d3 = {"A": mk(scores, closes_seq, opens_seq)}
tr3 = bt.value_trades_persist(d3, 80, 3)
# streak=3成立はW+4(85の3日目)→約定はその翌日W+5
check("3日連続の翌日始値で約定", len(tr3) == 1 and tr3[0]["entryDate"] == f"D{W + 5:05d}",
      tr3[0]["entryDate"] if tr3 else "なし")

print("[summarize_trades: 期待値分解]")
s = bt.summarize_trades([
    {"ticker": "A", "entryDate": "x", "exitDate": "y", "days": 10, "retPct": 10.0, "reason": "top"},
    {"ticker": "B", "entryDate": "x", "exitDate": "y", "days": 10, "retPct": 6.0, "reason": "top"},
    {"ticker": "C", "entryDate": "x", "exitDate": "y", "days": 10, "retPct": -4.0, "reason": "time"},
])
check("avgWin/avgLoss", s["avgWinPct"] == 8.0 and s["avgLossPct"] == -4.0)
check("ProfitFactor", abs(s["profitFactor"] - 4.0) < 0.01, s["profitFactor"])

print("[モメンタム: 翌日始値エントリー]")
n_len = 60
mscores = []
for i in range(n_len):
    mscores.append({"score": 50, "top": None, "band": 0, "atr": 1.0,
                    "momSignal": "entry" if i == 5 else "none"})
closes = [100.0] * (W + 1) + [100.0 + i * 0.1 for i in range(n_len)]
opens = [None] * (W + 1 + 6) + [104.0] + [None] * (n_len - 7)
sig = [None] * (W + 1) + mscores
dm = {"M": (sig, closes, [f"D{i:05d}" for i in range(len(closes))], opens)}
res, daily = bt.backtest_momentum(dm, ["M"], {"stopInitPct": 8, "atrMult": 2.5,
                                              "tp1GainPct": 20, "tp2GainPct": 40})
# 始値104でギャップアップ約定→当日終値100.6がATRストップ(101.5)割れ→即日損切り
# = エントリー価格が始値基点になっている証拠(旧仕様なら終値100.5エントリーでストップ99.0、損切りされない)
expect_stop = (100.6 / 104.0 - 1) * 100 - bt.COST_RT_PCT["stock"]
check("エントリー翌日始値104が基点(ギャップ分でストップ)", res.get("trades", 0) >= 1 and
      abs(res["worstPct"] - expect_stop) < 0.15, f"{res.get('worstPct')} vs {expect_stop:.2f}")

print("[replay_weights(v3.27)]")
from datetime import date, timedelta
# ベンチ不足→レジームunknown・チルトなし → 基準配分のまま
w, reg = bt.replay_weights(date(2023, 5, 1), [100.0] * 50, {"stocks": 60, "btc": 10, "eth": 10, "gold": 5, "silver": 5, "cash": 10})
check("データ不足はチルトなし", abs(w["stocks"] - 60) < 0.01 and reg == "unknown")
# 半減期底ウィンドウ(2020-05-11の+27ヶ月≒2022-08) → BTC+2/ETH+1/現金-3
w2, _ = bt.replay_weights(date(2022, 8, 15), [100.0] * 50, {"stocks": 60, "btc": 10, "eth": 10, "gold": 5, "silver": 5, "cash": 10})
check("半減期底でBTC+2/ETH+1", abs(w2["btc"] - 12) < 0.01 and abs(w2["eth"] - 11) < 0.01 and abs(w2["cash"] - 7) < 0.01, w2)
check("正規化100", abs(sum(w2.values()) - 100) < 0.01)

print("[production_replay(v3.27)]")
import json as _json
cfg = _json.load(open("config.json"))
d0 = date(2022, 1, 3)
N = 320
days = [(d0 + timedelta(days=int(i * 1.4))).isoformat() for i in range(N)]
def series(rate):
    px, out, rets = 100.0, {}, {}
    prev = None
    for d in days:
        if prev is not None:
            rets[d] = rate
        px *= 1 + rate
        prev = d
    return rets
def mkstock(rate):
    closes = [100.0]
    for _ in range(N - 1):
        closes.append(closes[-1] * (1 + rate))
    sig = [None] * N
    return (sig, closes, days, [None] * N)
stock_data = {"VOO": mkstock(0.0004), "AAA": mkstock(0.001), "BBB": mkstock(0.0006),
              "WMT": mkstock(0.0002)}
# WMTをnoAlloc扱いに(cfg上そうなっている前提を確認)
assert any(s["ticker"] == "WMT" and s.get("noAlloc") for s in cfg["stocks"])
sleeves = {"btc": series(0.002), "eth": series(0.001), "gold": series(0.0005), "silver": series(0.0003)}
bench_by_date = {}
acc = []
for d in days:
    acc.append(100.0)
    bench_by_date[d] = list(acc)
rep = bt.production_replay(stock_data, sleeves, days, bench_by_date, cfg)
check("リプレイ出力あり", rep is not None and rep["portfolios"]["full"] is not None)
if rep:
    f, v = rep["portfolios"]["full"], rep["portfolios"]["voo100"]
    check("3PF揃う", all(rep["portfolios"].get(k) for k in ("full", "voo100", "vooGoldCash")))
    check("倍率>0", f["finalMultiple"] > 0 and v["finalMultiple"] > 0)
    check("年次リターンあり", bool(f.get("yearlyPct")))
    check("注記に生存者バイアス明記", any("生存者バイアス" in n for n in rep["notes"]))

print(f"\n結果: ✅{PASS} / ❌{FAIL}")
sys.exit(1 if FAIL else 0)
