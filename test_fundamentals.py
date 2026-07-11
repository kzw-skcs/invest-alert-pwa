# -*- coding: utf-8 -*-
"""fundamentals.py v3.28(4α定量評価)の純関数テスト。ネット不要。"""
import sys

import fundamentals as F

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")


print("[Reverse DCF]")
fcf, g_true = 100.0, 0.12
mktcap = F.dcf_value(fcf, g_true)
g_imp = F.implied_fcf_growth(mktcap, fcf)
check("織り込み成長率の逆算が一致", abs(g_imp - g_true) < 0.002, f"{g_imp:.4f}")
check("割高ほど高い織り込み成長", F.implied_fcf_growth(mktcap * 2, fcf) > g_true)
check("FCFなしはNone", F.implied_fcf_growth(mktcap, 0) is None and F.implied_fcf_growth(None, fcf) is None)
check("成長が高いほど価値大", F.dcf_value(100, 0.15) > F.dcf_value(100, 0.05))

print("[Alpha Finance]")
good = {"netCash": True, "interestCoverage": 30, "fcfPosYears": 5, "fcfYears": 5,
        "currentRatio": 2.0, "fcfMargin": 0.25}
bad = {"netCash": False, "netDebtEbitda": 5.5, "interestCoverage": 1.5, "fcfPosYears": 1,
       "fcfYears": 5, "currentRatio": 0.6, "fcfMargin": -0.02}
s1, p1, w1 = F.alpha_finance_score(good)
s2, p2, w2 = F.alpha_finance_score(bad)
check("純現金+高FCF=満点", s1 == 25.0, s1)
check("重債務=低得点", s2 < 5, s2)
check("負債の重警告", any("重警告" in w for w in w2))
s3, _, w3 = F.alpha_finance_score({"roe": 0.15, "opMargin": 0.35, "epsGrowth": 0.12}, is_financial=True)
check("金融業は簡易モデル+明記", s3 == 25 and any("金融業" in w for w in w3))
sN, _, wN = F.alpha_finance_score({"currentRatio": 1.2})
check("データ不足はNone", sN is None and "データ不足" in wN)

print("[Alpha Management定量]")
mg1, _ = F.alpha_mgmt_quant({"sharesChg5yPct": -8, "roicApprox": 0.25, "sbcToRev": 0.01,
                             "goodwillToAssets": 0.05, "fcfPosYears": 5})
mg2, _ = F.alpha_mgmt_quant({"sharesChg5yPct": 25, "roicApprox": 0.03, "sbcToRev": 0.15,
                             "goodwillToAssets": 0.55, "fcfPosYears": 1})
check("買い戻し+高ROIC=満点", mg1 == 25.0, mg1)
check("希薄化+低規律=0点", mg2 == 0.0, mg2)
check("2項目未満はNone", F.alpha_mgmt_quant({"roicApprox": 0.2})[0] is None)

print("[Alpha Money]")
m_ok = {"fcfLatest": 100.0, "shares": 10.0, "revGrowth": 0.15}
fair = F.dcf_value(100.0, 0.15) / 10.0
mo = F.alpha_money(m_ok, price=fair * 0.55)
check("安全余裕40%超=25点", mo and mo["score"] == 25 and mo["safetyMarginPct"] > 40)
mo2 = F.alpha_money(m_ok, price=fair * 1.3)
check("割高=低スコア", mo2 and mo2["score"] <= 2)
check("購入上限=基本価値×0.75", mo and mo["buyCeiling"] == round(mo["fairBase"] * 0.75, 2))
check("FCF赤字はNone(無理にスコアを作らない)", F.alpha_money({"fcfLatest": -5, "shares": 10}, 100) is None)
check("成長は20%で頭打ち(暴走防止)", F.alpha_money({"fcfLatest": 100, "shares": 10, "revGrowth": 0.90}, 100)["assumedBaseGrowthPct"] == 20.0)

print(f"\n結果: ✅{PASS} / ❌{FAIL}")
sys.exit(1 if FAIL else 0)
