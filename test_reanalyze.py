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

print("[Champion/Challenger(v3.26)]")
CHAL = {"enabled": True, "label": "厳選", "thresholds": {"80": 85, "90": 92}, "confirmN": 5}
st2 = {"open": [], "closed": []}
# スコア83: Champion(80)は開始、Challenger(85)は未達
EV.update_episodes([inst("NVDA", 83, {"65": 5, "80": 1, "90": 0})], cyc, mpf, "2026-08-01", st2, CHAL)
arms = [(e["strat"], e.get("arm")) for e in st2["open"]]
check("83点: Championのみ開始", ("value80", "champion") in arms and ("value80", "challenger") not in arms, arms)
# スコア86: Challengerも開始
EV.update_episodes([inst("NVDA", 86, {"65": 6, "80": 2, "90": 0})], cyc, mpf, "2026-08-02", st2, CHAL)
arms = [(e["strat"], e.get("arm")) for e in st2["open"]]
check("86点: Challenger開始", ("value80", "challenger") in arms, arms)
# 同日再実行: Challengerストリークが二重加算されない
EV.update_episodes([inst("NVDA", 86, {"65": 6, "80": 2, "90": 0})], cyc, mpf, "2026-08-02", st2, CHAL)
check("Challenger同日ガード", st2["chalStreaks"]["NVDA"]["80"] == 1, st2["chalStreaks"])
# 5日連続でChallenger確定(Championは3日で確定済みのはず)
for d in ("03", "04", "05", "06"):
    EV.update_episodes([inst("NVDA", 87, {"65": 9, "80": 9, "90": 0})], cyc, mpf, f"2026-08-{d}", st2, CHAL)
chal_ep = next(e for e in st2["open"] if e.get("arm") == "challenger")
check("Challenger確定は5日目", chal_ep.get("confirmDate") == "2026-08-06", chal_ep.get("confirmDate"))
# 84点に低下: Challenger(85未満)だけ剥落クローズ、Champion(80以上)は継続
EV.update_episodes([inst("NVDA", 84, {"65": 9, "80": 9, "90": 0})], cyc, mpf, "2026-08-07", st2, CHAL)
arms = [(e["strat"], e.get("arm")) for e in st2["open"]]
check("84点: Challengerのみクローズ", ("value80", "champion") in arms and ("value80", "challenger") not in arms, arms)

print("[challenger_verdict]")
champ_agg = {"win": 40, "loss": 12, "expired": 8, "winRatePct": 76.9, "expectancyPct": 2.5}
chal_agg = {"win": 45, "loss": 8, "expired": 7, "winRatePct": 84.9, "expectancyPct": 3.8}
s, t = R.challenger_verdict(champ_agg, chal_agg, "2026-01-01", "2026-08-01")
check("条件充足でChallenger勝ち", s == "challenger_wins", s)
s, t = R.challenger_verdict(champ_agg, chal_agg, "2026-07-01", "2026-08-01")
check("180日未満は蓄積中", s == "accumulating", s)
s, t = R.challenger_verdict(champ_agg, {"win": 5, "loss": 2, "expired": 1, "winRatePct": 71, "expectancyPct": 5.0},
                            "2026-01-01", "2026-08-01")
check("50件未満は蓄積中", s == "accumulating", s)
s, t = R.challenger_verdict({"win": 45, "loss": 8, "expired": 7, "winRatePct": 84.9, "expectancyPct": 3.8},
                            {"win": 40, "loss": 12, "expired": 8, "winRatePct": 76.9, "expectancyPct": 2.5},
                            "2026-01-01", "2026-08-01")
check("劣後ならChampion維持", s == "champion_wins", s)

print("[定数と提案閾値]")
check("提案には30独立エピソード必要", R.MIN_EPISODES_FOR_SUGGESTION == 30)
check("損切りは-8%(モメンタムルールと整合)", R.MOM_MISS_PCT == -8)
for key, (lo, hi) in R.BOUNDS.items():
    check(f"BOUNDS {key}: lo<hi", lo < hi)

print(f"\n結果: ✅{PASS} / ❌{FAIL}")
sys.exit(1 if FAIL else 0)
