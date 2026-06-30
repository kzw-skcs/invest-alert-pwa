"""reanalyze.py 純ロジックのテスト（ネット不要）。"""
import reanalyze as R

PASS = FAIL = 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✅ {name} {extra}")
    else: FAIL += 1; print(f"  ❌ {name} {extra}")

print("[outcome]")
buy = {"side": "buy", "price": 100, "top": 110}
r, d = R.outcome(buy, [("2026-01-02", 102), ("2026-01-05", 104.5)], 3)
check("買い的中(+4.5%)", r == "hit", f"{r} {d}")
r, d = R.outcome(buy, [("2026-01-02", 101), ("2026-01-05", 102)], 3)
check("買い不的中(+2%)", r == "miss", f"{r} {d}")
r, d = R.outcome(buy, [("2026-01-02", 112)], 3)
check("買い目標到達", d and d.get("reachedTarget") is True, f"{d}")
sell = {"side": "sell", "price": 100}
r, d = R.outcome(sell, [("2026-01-02", 96)], 3)
check("売り的中(-4%)", r == "hit", f"{r} {d}")
r, d = R.outcome(sell, [("2026-01-02", 99)], 3)
check("売り不的中(-1%)", r == "miss", f"{r} {d}")
r, d = R.outcome(buy, [], 3)
check("future空→None", r is None)

print("[aggregate]")
ev = ([{"side":"buy","state":"BUY","score":70,"result":"hit"}]*7 +
      [{"side":"buy","state":"BUY","score":40,"result":"miss"}]*3 +
      [{"side":"sell","state":"SELL","score":60,"result":"hit"}]*2)
st = R.aggregate(ev)
check("買いn=10", st["bySide"]["buy"]["n"] == 10, f"{st['bySide']['buy']}")
check("買い的中率0.7", st["bySide"]["buy"]["rate"] == 0.7)
check("総合的中率算出", st["overall"]["rate"] is not None, f"{st['overall']}")

print("[propose_adjustments]")
settings = {"bandPctOfRange": 0.03, "minScoreToAlert": 0}
# 買い的中率低い→帯を狭める
low_stats = R.aggregate([{"side":"buy","state":"BUY","score":50,"result":"miss"}]*7 +
                        [{"side":"buy","state":"BUY","score":50,"result":"hit"}]*3)
ns, adj = R.propose_adjustments(low_stats, settings)
check("低的中→帯縮小", ns["bandPctOfRange"] < 0.03, f"{ns['bandPctOfRange']} {[a['param'] for a in adj]}")
# 買い的中率高い→帯を広げる
hi_stats = R.aggregate([{"side":"buy","state":"BUY","score":70,"result":"hit"}]*8 +
                       [{"side":"buy","state":"BUY","score":70,"result":"miss"}]*2)
ns, adj = R.propose_adjustments(hi_stats, settings)
check("高的中→帯拡大", ns["bandPctOfRange"] > 0.03, f"{ns['bandPctOfRange']}")
# スコア帯で差→minScore引上げ
band_stats = R.aggregate([{"side":"buy","state":"BUY","score":70,"result":"hit"}]*9 +
                         [{"side":"buy","state":"BUY","score":40,"result":"miss"}]*9)
ns, adj = R.propose_adjustments(band_stats, settings)
check("スコア差→minScore=50", ns["minScoreToAlert"] == 50, f"{ns['minScoreToAlert']}")

print(f"\n==== 結果: PASS={PASS} / FAIL={FAIL} ====")
exit(1 if FAIL else 0)
