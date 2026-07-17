# -*- coding: utf-8 -*-
"""reanalyze.py(v3.24 Triple Barrier)+ evaluate.update_episodes のテスト。ネット不要。"""
import sys

import reanalyze as R
import evaluate as EV

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")


def mk_hist(closes, start="2026-01-01"):
    from datetime import date, timedelta
    d0 = date.fromisoformat(start)
    return [{"d": (d0 + timedelta(days=i)).isoformat(), "c": c} for i, c in enumerate(closes)]


print("[triple_barrier]")
# 勝ち: +5%に先に到達
o, r, n = R.triple_barrier(mk_hist([100, 102, 106, 90]), "2026-01-01", 100, 5, -8, 40)
check("先に上側→win", o == "win" and r == 6.0, f"{o} {r}")
# 負け: -8%に先に到達
o, r, _ = R.triple_barrier(mk_hist([100, 97, 91, 120]), "2026-01-01", 100, 5, -8, 40)
check("先に下側→loss", o == "loss" and r == -9.0, f"{o} {r}")
# 時間切れ: どちらにも届かず40日
o, r, _ = R.triple_barrier(mk_hist([100] + [101] * 45), "2026-01-01", 100, 5, -8, 40)
check("到達なし→expired(勝敗に混ぜない)", o == "expired" and r == 1.0, f"{o} {r}")
# 進行中: データが40日分ない
o, r, _ = R.triple_barrier(mk_hist([100, 101, 102]), "2026-01-01", 100, 5, -8, 40)
check("データ不足→open", o == "open", f"{o}")
# エントリー日以降のバーから評価(過去は見ない)
o, r, _ = R.triple_barrier(mk_hist([50, 100, 106]), "2026-01-02", 100, 5, -8, 40)
check("entry_date以降のみ評価", o == "win", f"{o} {r}")
# 旧版の水増し(40日後プラス→hit)が廃止されていること
o, _, _ = R.triple_barrier(mk_hist([100] + [103] * 45), "2026-01-01", 100, 5, -8, 40)
check("+5%未達のプラスはwinでなくexpired", o == "expired")

print("[aggregate(期待値・PF)]")
agg = R.aggregate([("win", 6.0), ("win", 5.5), ("loss", -8.5), ("expired", 2.0), ("open", 1.0)])
check("勝率=win/(win+loss)のみ", agg["winRatePct"] == 66.7, agg["winRatePct"])
check("expired別集計", agg["expired"] == 1 and agg["open"] == 1)
check("期待値=全クローズ平均", abs(agg["expectancyPct"] - (6.0 + 5.5 - 8.5 + 2.0) / 4) < 0.01, agg["expectancyPct"])
check("PF=総利益/総損失", abs(agg["profitFactor"] - (6.0 + 5.5 + 2.0) / 8.5) < 0.01, agg["profitFactor"])
check("平均利益/平均損失", agg["avgWinPct"] == 5.75 and agg["avgLossPct"] == -8.5)

print("[エピソードのライフサイクル(evaluate.update_episodes)]")
def inst(tk, score, streaks, mom_state=None, price=100.0):
    m = {"momState": mom_state} if mom_state else {}
    return {"ticker": tk, "state": "OK", "price": price, "rotSector": "情報技術",
            "history": [{"d": "BX", "c": price}],
            "tradePolicy": "trade" if mom_state else "hold",  # v3.32: momエピソードはtrade銘柄のみ
            "value": {"score": score, "streaks": streaks}, "momentum": m, "quality": {"score": 70}}
cyc = {"riskOff": {"score": 30}, "sectors": {"情報技術": {"trend": "inflow"}}}
mpf = {"regime": {"regime": "neutral", "label": "巡航"}}
store = {"open": [], "closed": []}
# 1日目: 80到達(streak=1)→開始
EV.update_episodes([inst("NVDA", 82, {"65": 5, "80": 1, "90": 0})], cyc, mpf, "2026-07-01", store)
check("streak=1で開始", len(store["open"]) == 1 and store["open"][0]["startDate"] == "2026-07-01")
# 同日再実行: 二重開始しない
EV.update_episodes([inst("NVDA", 82, {"65": 5, "80": 1, "90": 0})], cyc, mpf, "2026-07-01", store)
check("同日再実行で二重開始なし", len(store["open"]) == 1)
# 3日目: streak=3→確定記録
EV.update_episodes([inst("NVDA", 85, {"65": 7, "80": 3, "90": 0}, price=98.0)], cyc, mpf, "2026-07-03", store)
ep = store["open"][0]
check("streak=3で確定日記録", ep.get("confirmDate") == "2026-07-03" and ep.get("confirmPrice") == 98.0)
check("maxScore更新", ep["maxScore"] == 85)
# 剥落: streak=0→クローズ
EV.update_episodes([inst("NVDA", 70, {"65": 9, "80": 0, "90": 0}, price=104.0)], cyc, mpf, "2026-07-10", store)
check("剥落でクローズ", len(store["open"]) == 0 and len(store["closed"]) == 1)
check("終了理由tierExit", store["closed"][0]["endReason"] == "tierExit" and store["closed"][0]["endPrice"] == 104.0)
# 再到達: 新エピソードとして独立
EV.update_episodes([inst("NVDA", 83, {"65": 9, "80": 1, "90": 0})], cyc, mpf, "2026-07-20", store)
check("再到達は新エピソード", len(store["open"]) == 1 and store["open"][0]["startDate"] == "2026-07-20")
# モメンタム: ENTRY→EXIT
EV.update_episodes([inst("ANET", 60, {"65": 0, "80": 0, "90": 0}, mom_state="ENTRY")], cyc, mpf, "2026-07-20", store)
check("mom ENTRYで開始", any(e["strat"] == "mom" for e in store["open"]))
EV.update_episodes([inst("ANET", 60, {"65": 0, "80": 0, "90": 0}, mom_state="EXIT")], cyc, mpf, "2026-07-25", store)
check("mom EXITでクローズ", not any(e["strat"] == "mom" for e in store["open"])
      and any(e["strat"] == "mom" and e["endReason"] == "momExit" for e in store["closed"]))
# 銘柄がリストから消えた場合
EV.update_episodes([], cyc, mpf, "2026-07-26", store)
check("リスト外はremovedでクローズ", len(store["open"]) == 0)

print("[compute_streaks(v3.29: 営業日バー基準)]")
# 新バーで+1
s = EV.compute_streaks(85, "2026-07-10", {"65": 5, "80": 2, "90": 0}, "2026-07-09")
check("新バーで+1", s["80"] == 3 and s["65"] == 6)
# 同じバー(週末・祝日・JST跨ぎ・再実行)は維持
s = EV.compute_streaks(85, "2026-07-10", {"80": 2, "65": 5}, "2026-07-10")
check("同一バーは維持(週末水増しなし)", s["80"] == 2)
# 剥落は即0
s = EV.compute_streaks(75, "2026-07-11", {"80": 5}, "2026-07-10")
check("剥落で0", s["80"] == 0)
# 新規到達は1(バー同一でも品質更新等でスコアが動いた場合)
s = EV.compute_streaks(85, "2026-07-10", {"80": 0}, "2026-07-10")
check("新規到達は1", s["80"] == 1)

print("[Champion/Challenger(v3.26/v3.29バー基準)]")
CHAL = {"enabled": True, "label": "厳選", "thresholds": {"80": 85, "90": 92}, "confirmN": 5}
def inst2(tk, score, streaks, bar, mom_state=None, price=100.0):
    m = {"momState": mom_state} if mom_state else {}
    return {"ticker": tk, "state": "OK", "price": price, "rotSector": "情報技術",
            "history": [{"d": bar, "c": price}],
            "value": {"score": score, "streaks": streaks}, "momentum": m, "quality": {"score": 70}}
st2 = {"open": [], "closed": []}
# バーB1: 83点→Championのみ
EV.update_episodes([inst2("NVDA", 83, {"65": 5, "80": 1, "90": 0}, "B1")], cyc, mpf, "2026-08-01", st2, CHAL)
arms = [(e["strat"], e.get("arm")) for e in st2["open"]]
check("83点: Championのみ開始", ("value80", "champion") in arms and ("value80", "challenger") not in arms)
# バーB2: 86点→Challenger開始(st=1)
EV.update_episodes([inst2("NVDA", 86, {"65": 6, "80": 2, "90": 0}, "B2")], cyc, mpf, "2026-08-02", st2, CHAL)
check("86点: Challenger開始", any(a == ("value80", "challenger") for a in
      [(e["strat"], e.get("arm")) for e in st2["open"]]))
check("Chalストリーク=1", st2["chalStreaks"]["NVDA"]["80"] == 1)
# 週末: 日付は進むがバーはB2のまま → 加算されない(今回の修正の核心)
EV.update_episodes([inst2("NVDA", 86, {"65": 6, "80": 2, "90": 0}, "B2")], cyc, mpf, "2026-08-03", st2, CHAL)
EV.update_episodes([inst2("NVDA", 86, {"65": 6, "80": 2, "90": 0}, "B2")], cyc, mpf, "2026-08-04", st2, CHAL)
check("週末(同一バー)は加算なし", st2["chalStreaks"]["NVDA"]["80"] == 1, st2["chalStreaks"]["NVDA"])
# 新バーが4本→計5バーで確定
for n, b in enumerate(("B3", "B4", "B5", "B6")):
    EV.update_episodes([inst2("NVDA", 87, {"65": 9, "80": 9, "90": 0}, b)], cyc, mpf, f"2026-08-0{5 + n}", st2, CHAL)
chal_ep = next(e for e in st2["open"] if e.get("arm") == "challenger")
check("Challenger確定は5バー目", chal_ep.get("confirmDate") == "2026-08-08" and st2["chalStreaks"]["NVDA"]["80"] == 5,
      (chal_ep.get("confirmDate"), st2["chalStreaks"]["NVDA"]))
# 84点に低下: Challengerのみクローズ
EV.update_episodes([inst2("NVDA", 84, {"65": 9, "80": 9, "90": 0}, "B7")], cyc, mpf, "2026-08-09", st2, CHAL)
arms = [(e["strat"], e.get("arm")) for e in st2["open"]]
check("84点: Challengerのみクローズ", ("value80", "champion") in arms and ("value80", "challenger") not in arms)

print("[challenger_verdict]")
champ_agg = {"win": 40, "loss": 12, "expired": 8, "winRatePct": 76.9, "expectancyPct": 2.5}
chal_agg = {"win": 45, "loss": 8, "expired": 7, "winRatePct": 84.9, "expectancyPct": 3.8}
s_, t_ = R.challenger_verdict(champ_agg, chal_agg, "2026-01-01", "2026-08-01")
check("条件充足でChallenger勝ち", s_ == "challenger_wins")
s_, t_ = R.challenger_verdict(champ_agg, chal_agg, "2026-07-01", "2026-08-01")
check("180日未満は蓄積中", s_ == "accumulating")
s_, t_ = R.challenger_verdict(champ_agg, {"win": 5, "loss": 2, "expired": 1, "winRatePct": 71, "expectancyPct": 5.0},
                              "2026-01-01", "2026-08-01")
check("50件未満は蓄積中", s_ == "accumulating")
s_, t_ = R.challenger_verdict({"win": 45, "loss": 8, "expired": 7, "winRatePct": 84.9, "expectancyPct": 3.8},
                              {"win": 40, "loss": 12, "expired": 8, "winRatePct": 76.9, "expectancyPct": 2.5},
                              "2026-01-01", "2026-08-01")
check("劣後ならChampion維持", s_ == "champion_wins")

print("[risk_model(v3.30)]")
import random
random.seed(3)
from datetime import date as _date, timedelta as _td
d0 = _date(2023, 1, 2)
dates = [(d0 + _td(days=i)).isoformat() for i in range(800)]
def synth_hist(vol_daily, drift=0.0004):
    px, out = 100.0, []
    for d in dates:
        px *= 1 + random.gauss(drift, vol_daily)
        out.append({"d": d, "c": px})
    return out
shm = {f"S{i}": synth_hist(0.015) for i in range(6)}
ahm = {"btc": synth_hist(0.04), "eth": synth_hist(0.05),
       "gold": synth_hist(0.008), "silver": synth_hist(0.018)}
rm = EV.risk_model(shm, ahm, {"stocks": 60, "btc": 10, "eth": 10, "gold": 5, "silver": 5, "cash": 10})
check("リスクモデル出力あり", rm is not None and rm["windowDays"] >= 200)
if rm:
    v = rm["volPct"]
    check("ボラ序列(ETH>BTC>銀>金)", v["eth"] > v["btc"] > v["silver"] > v["gold"], v)
    cur = next(c for c in rm["candidates"] if c["label"] == "現行目標")
    rc = cur["riskContribPct"]
    check("リスク寄与合計≈100", abs(sum(rc.values()) - 100) < 1.5, sum(rc.values()))
    check("暗号のリスク寄与>資本比率", rc["btc"] + rc["eth"] > 20, rc)
    check("候補6種+DD/CAGR算出", len(rm["candidates"]) == 6 and all(
        0 <= c["maxDDPct"] <= 100 and c.get("cagrPct") is not None for c in rm["candidates"]))
    zero = next(c for c in rm["candidates"] if c["label"] == "暗号ゼロ")
    check("暗号ゼロはボラ低下", zero["volPct"] < cur["volPct"], (zero["volPct"], cur["volPct"]))
    check("正直な注記", "参考値" in rm["note"])

print("[build_sector_idx(v3.31: ETF基準+フォールバック)]")
etf = {"情報技術": [{"d": f"D{i}", "c": 100 + i} for i in range(80)]}
shm2 = {"CAT": [{"d": f"D{i}", "c": 50 + i * 0.1} for i in range(80)],
        "NVDA": [{"d": f"D{i}", "c": 200 + i} for i in range(80)]}
smap = {"CAT": "資本財", "NVDA": "情報技術"}
sidx, ssrc = EV.build_sector_idx(shm2, smap, etf)
check("ETFが一次ソース", ssrc.get("情報技術", "").startswith("ETF") and sidx["情報技術"][0] == 100)
check("ETF欠損セクターは銘柄平均", ssrc.get("資本財") == "銘柄平均(fallback)" and "資本財" in sidx)
check("ETF採用セクターに銘柄平均が混ざらない", abs(sidx["情報技術"][-1] - 179) < 1e-6)
check("SECTOR_ETFSはGICS10セクター", len(EV.SECTOR_ETFS) == 10)

print("[定数と提案閾値]")
check("提案には30独立エピソード必要", R.MIN_EPISODES_FOR_SUGGESTION == 30)
check("損切りは-8%(モメンタムルールと整合)", R.MOM_MISS_PCT == -8)
for key, (lo, hi) in R.BOUNDS.items():
    check(f"BOUNDS {key}: lo<hi", lo < hi)

print(f"\n結果: ✅{PASS} / ❌{FAIL}")
sys.exit(1 if FAIL else 0)
