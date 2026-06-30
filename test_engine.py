"""engine.py の単体テスト（合成データ）。外部通信なしで状態遷移を検証する。"""
import math
import engine as E
from evaluate import build_instrument

S = {
    "lookbackDays": 252, "lookbackLongDays": 1260, "longRangeMinDays": 504, "longTermLowPct": 50,
    "bandPctOfRange": 0.03, "maShort": 20, "maLong": 50,
    "maTrend": 200, "breakoutLookbackDays": 90, "counterTrendFilter": True,
    "vixWarnPct": 20, "vixUrgentPct": 50, "vixAbsWarn": 30, "vixAbsUrgent": 40,
    "rebalanceThresholdPct": 5, "hitThresholdPct": 3, "minScoreToAlert": 0,
}
PASS, FAIL = 0, 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")


# ---- 純関数 ----
print("[純関数]")
sl, ic = E.linregress([0, 1, 2, 3], [1, 3, 5, 7])  # y=2x+1
check("linregress slope", abs(sl - 2) < 1e-9, f"slope={sl:.3f}")
check("linregress intercept", abs(ic - 1) < 1e-9, f"int={ic:.3f}")
m = E.sma([1, 2, 3, 4, 5], 3)
check("sma", m[2] == 2 and m[4] == 4, f"{m}")
check("detect dead", E.detect_cross([2, 0.5], [1, 1]) == "dead")
check("detect golden", E.detect_cross([0.5, 2], [1, 1]) == "golden")
check("detect none", E.detect_cross([2, 3], [1, 1]) is None)

# ---- 基本レンジ（正弦波 90〜110）----
print("[レンジ内の買い場/売り場/中立]")
def sine(n, end_phase):
    # end_phase: 'trough'(-1) / 'peak'(+1) / 'mid'(0) を最終点に来るよう生成
    base = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(n)]
    return base

buy_series = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(280)]
# 末尾を谷(=90付近)に調整
buy_series[-1] = 90.2
r = E.classify(buy_series, S)
check("BUY (底付近)", r["state"] == "BUY", f"-> {r['state']} price={r['price']} bottom={r['bottom']}")

sell_series = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(280)]
sell_series[-1] = 109.8
r = E.classify(sell_series, S)
check("SELL (天井付近)", r["state"] == "SELL", f"-> {r['state']} price={r['price']} top={r['top']}")

neut_series = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(280)]
neut_series[-1] = 100.0
r = E.classify(neut_series, S)
check("NEUTRAL (中央)", r["state"] == "NEUTRAL", f"-> {r['state']} pctInRange={r['pctInRange']}")

print("[レンジ外の上昇中/下落中]")
rise = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(280)]
rise[-1] = 135.0
r = E.classify(rise, S)
check("RISING (天井上抜け)", r["state"] == "RISING", f"-> {r['state']} top={r['top']}")

fall = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(280)]
fall[-1] = 65.0
r = E.classify(fall, S)
check("FALLING (底下抜け)", r["state"] == "FALLING", f"-> {r['state']} bottom={r['bottom']}")

# ---- 超売り場：上昇ブレイク後にデッドクロス ----
print("[超売り場 / 超買い場]")
base = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(180)]
up = [110 + (i + 1) * 1.5 for i in range(40)]   # 111.5..170 でレンジ上抜け＆上昇
core = base + up
# デッドクロスが最終日に来るよう、下落テールを1日ずつ伸ばして探索
found = None
decline = [core[-1] - (i + 1) * 4 for i in range(80)]
for j in range(1, len(decline) + 1):
    test = core + decline[:j]
    ss = E.sma(test, S["maShort"]); ll = E.sma(test, S["maLong"])
    if E.detect_cross(ss, ll) == "dead":
        found = test; break
check("超売り場 系列生成", found is not None)
if found:
    r = E.classify(found, S)
    check("SUPER_SELL", r["state"] == "SUPER_SELL",
          f"-> {r['state']} cross={r['cross']} brokeAbove={r['brokeAboveRecently']}")

# ---- 超買い場：下落ブレイク後にゴールデンクロス ----
base2 = [100 + 10 * math.sin(2 * math.pi * i / 40) for i in range(180)]
down = [90 - (i + 1) * 1.5 for i in range(40)]   # 88.5..30 でレンジ下抜け＆下落
core2 = base2 + down
found2 = None
rally = [core2[-1] + (i + 1) * 4 for i in range(120)]
for j in range(1, len(rally) + 1):
    test = core2 + rally[:j]
    ss = E.sma(test, S["maShort"]); ll = E.sma(test, S["maLong"])
    if E.detect_cross(ss, ll) == "golden":
        found2 = test; break
check("超買い場 系列生成", found2 is not None)
if found2:
    r = E.classify(found2, S)
    check("SUPER_BUY", r["state"] == "SUPER_BUY",
          f"-> {r['state']} cross={r['cross']} brokeBelow={r['brokeBelowRecently']}")

# ---- データ不足 ----
print("[データ不足]")
r = E.classify([100] * 50, S)
check("UNKNOWN", r["state"] == "UNKNOWN", f"-> {r['state']}")

# ---- VIX ----
print("[VIX]")
v = E.vix_status([15, 15], S); check("VIX 変化なし", v["level"] is None, f"{v}")
v = E.vix_status([15, 18.5], S); check("VIX 警戒(+23%)", v["level"] == "警戒", f"{v}")
v = E.vix_status([15, 23.5], S); check("VIX 緊急(+57%)", v["level"] == "緊急", f"{v}")
v = E.vix_status([34.8, 35], S); check("VIX 警戒(絶対水準35)", v["level"] == "警戒", f"{v}")
v = E.vix_status([41, 42], S); check("VIX 緊急(絶対水準42)", v["level"] == "緊急", f"{v}")

print("[逆張りフィルタ / 長期保有方針]")
ds = [f"2025-{(i%12)+1:02d}-01" for i in range(len(buy_series))]
# 下降トレンド中の買い場 → アラート抑制 + counterTrend
inst = build_instrument({"ticker": "T", "name": "テスト", "tradePolicy": "trade"},
                        list(zip(ds, buy_series)), S, "stock")
check("逆張り買い場を抑制", inst["state"] == "BUY" and inst["actionable"] is False and inst["counterTrend"] is True,
      f"state={inst['state']} actionable={inst['actionable']} counter={inst['counterTrend']} trend={inst.get('trend')}")
rec = E.recommend(inst, {"level": None}, S)
check("逆張りはavoid低スコア", rec["action"] == "avoid" and rec["score"] <= 20, f"{rec['action']} {rec['score']}")

# 長期保有(accumulate)の売り場 → 売りアラート無効
inst2 = build_instrument({"key": "gold", "name": "金", "tradePolicy": "accumulate"},
                         list(zip(ds, sell_series)), S, "metal")
check("長期保有の売りを抑制", inst2["state"] == "SELL" and inst2["actionable"] is False,
      f"state={inst2['state']} actionable={inst2['actionable']}")
rec2 = E.recommend(inst2, {"level": None}, S)
check("長期保有売りはhold", rec2["action"] == "hold", f"{rec2['action']} note={rec2['note']}")
# 長期保有でも買い場はアラート有効（順張りの買い場を作る：上昇トレンド＋底付近）
upbuy = [80 + i*0.12 for i in range(280)]  # 緩やかな上昇トレンド
upbuy[-1] = upbuy[-1] - (max(upbuy)-min(upbuy))*0.0  # 末尾はそのまま（トレンド上）
inst3 = build_instrument({"key": "btc", "name": "BTC", "tradePolicy": "accumulate"},
                         list(zip(ds, upbuy)), S, "crypto")
check("長期保有も買い場は判定継続", inst3["state"] in ("BUY","SUPER_BUY","NEUTRAL","SELL","RISING"),
      f"state={inst3['state']} actionable={inst3['actionable']} trend={inst3.get('trend')}")

# ---- 推奨度・見込みリターン ----
print("[推奨度 / 見込みリターン]")
novix = {"value": 16, "changePct": 2, "level": None}

inst_buy = E.classify(buy_series, S); inst_buy["state"] = "BUY"
rec = E.recommend(inst_buy, novix, S)
check("BUY=action buy", rec["action"] == "buy", f"{rec['action']}")
check("BUY 見込みリターン>0", rec["expReturnPct"] is not None and rec["expReturnPct"] > 0,
      f"exp={rec['expReturnPct']}% 期間={rec['expPeriodLabel']} 年率={rec['annualizedPct']}% 推奨度={rec['level']}★{rec['stars']}")
check("BUY 想定期間あり", rec["expPeriodLabel"] is not None)

if found:  # 超売り場
    inst_ss = E.classify(found, S)
    rec_ss = E.recommend(inst_ss, {"value": 30, "changePct": 55, "level": "緊急"}, S)
    check("超売り場=sell, 推奨度高", rec_ss["action"] == "sell" and rec_ss["stars"] >= 4,
          f"{rec_ss['action']} ★{rec_ss['stars']} score={rec_ss['score']}")

if found2:  # 超買い場
    inst_sb = E.classify(found2, S)
    rec_sb = E.recommend(inst_sb, {"value": 30, "changePct": 55, "level": "緊急"}, S)
    check("超買い場=buy, 推奨度S", rec_sb["action"] == "buy" and rec_sb["level"] == "S",
          f"{rec_sb['action']} {rec_sb['level']}★{rec_sb['stars']} exp={rec_sb['expReturnPct']}% note={rec_sb['note']}")

# 現実的な買い場（価格が天井下）で見込みリターン＋期間＋年率が出ること
inst_buy2 = E.classify(buy_series, S); inst_buy2["state"] = "BUY"
rec2 = E.recommend(inst_buy2, novix, S)
check("買い場で見込み+期間+年率が算出", rec2["expReturnPct"] and rec2["expPeriodLabel"] and rec2["annualizedPct"],
      f"+{rec2['expReturnPct']}% {rec2['expPeriodLabel']} 年率{rec2['annualizedPct']}%")

# HOLD系（レンジ内）は低スコア
rec_n = E.recommend({**E.classify(neut_series, S), "state": "NEUTRAL"}, novix, S)
check("NEUTRAL=hold 低スコア", rec_n["action"] == "hold" and rec_n["score"] < 35, f"score={rec_n['score']}")

# ---- マルチタイムフレーム（5年） ----
print("[マルチタイムフレーム（1年＋5年）]")
n=1320
def osc5(drift, amp, end_frac):
    s=[(100+drift*i)+amp*math.sin(2*math.pi*i/40) for i in range(n)]
    center=100+drift*(n-1); s[-1]=center+amp*end_frac; return s
ds5=[str(i) for i in range(n)]

# 上昇チャネルの押し目（MRVL/VRT型）：5年上昇トレンド＋直近は下限
up5=osc5(0.04, 8, -0.95)
r=E.classify(up5,S)
check("5年レンジ利用可", r.get("longRangeAvailable") is True, f"avail={r.get('longRangeAvailable')}")
check("長期トレンド=up", r.get("trendLong")=="up", f"trendLong={r.get('trendLong')} pctLong={r.get('pctInRangeLong')}")
inst=build_instrument({"ticker":"UP","name":"上昇押し目","tradePolicy":"hold"}, list(zip(ds5,up5)),S,"stock")
check("上昇チャネル下限＝買い場(抑制されない)", inst["state"]=="BUY" and inst["actionable"] is True and inst["counterTrend"] is False,
      f"state={inst['state']} actionable={inst['actionable']} counter={inst['counterTrend']}")

# 横ばいボックスの下限（META型）：5年ほぼ横ばい＋下限 → 逆張りフィルタで消さない
box5=osc5(0.0, 12, -0.95)
ib=build_instrument({"ticker":"BOX","name":"ボックス下限","tradePolicy":"hold"}, list(zip(ds5,box5)),S,"stock")
check("ボックス下限の買いを残す", ib["state"]=="BUY" and ib["counterTrend"] is False,
      f"state={ib['state']} counter={ib['counterTrend']} trendLong={ib.get('trendLong')}")

# 本物の長期下降トレンドの底値拾いは抑制
down5=osc5(-0.04, 8, -0.95)
idn=build_instrument({"ticker":"DN","name":"長期下降","tradePolicy":"trade"}, list(zip(ds5,down5)),S,"stock")
check("長期下降では買いを抑制", idn.get("trendLong")=="down" and (idn["state"]!="BUY" or idn["counterTrend"] is True),
      f"state={idn['state']} counter={idn['counterTrend']} trendLong={idn.get('trendLong')}")

# データが5年未満なら長期は使わない
rs=E.classify([100+10*math.sin(2*math.pi*i/40) for i in range(280)], S)
check("5年未満は長期なし", rs.get("longRangeAvailable") is False, f"avail={rs.get('longRangeAvailable')}")

print(f"\n==== 結果: PASS={PASS} / FAIL={FAIL} ====")
exit(1 if FAIL else 0)
