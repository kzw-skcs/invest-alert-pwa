# -*- coding: utf-8 -*-
"""engine.py の単体テスト(v3系・現行API準拠)。外部通信なし・合成データのみ。
実行: python3 test_engine.py  (全件✅で終了コード0、1件でも❌なら1)"""
import json
import os
import sys
from datetime import date, timedelta

import engine as E

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")


def mk_hist(rets, p0=100.0):
    d0 = date(2022, 1, 3)
    px, out = p0, []
    for i, r in enumerate(rets):
        px *= 1 + r
        out.append({"d": (d0 + timedelta(days=int(i * 1.45))).isoformat(), "c": px})
    return out


META = {"ticker": "T", "name": "t", "sector": "x", "subSector": "AI・半導体/クラウド",
        "rotSector": "情報技術", "tradePolicy": "hold", "conviction": 3, "class": "stock"}

print("[基礎関数]")
check("sma", abs(E.sma([1, 2, 3, 4, 5], 3) - 4.0) < 1e-9)
check("sma 部分指定", abs(E.sma([1, 2, 3, 4, 5], 3, 2) - 2.0) < 1e-9)
check("sma データ不足→None", E.sma([1, 2], 5) is None)

print("[analyze_instrument]")
inst = E.analyze_instrument(dict(META), mk_hist([0.001] * 500), None, None, CFG)
check("通常データで解析完了", inst.get("state") != "NO_DATA" and "value" in inst)
check("valueスコア範囲", 0 <= inst["value"]["score"] <= 100)
short = E.analyze_instrument(dict(META), mk_hist([0.001] * 30), None, None, CFG)
check("データ不足でNO_DATA", short["state"] == "NO_DATA")
crash = E.analyze_instrument(dict(META), mk_hist([0.0008] * 460 + [-0.02] * 40), None, None, CFG)
calm = E.analyze_instrument(dict(META), mk_hist([0.0008] * 500), None, None, CFG)
check("急落でスコア上昇(安さに鋭敏)", crash["value"]["score"] > calm["value"]["score"],
      f"crash={crash['value']['score']} calm={calm['value']['score']}")

print("[底状態(v3.19)]")
down = E.analyze_instrument(dict(META), mk_hist([0.001] * 440 + [-0.01] * 60), None, None, CFG)
rec_ = E.analyze_instrument(dict(META), mk_hist([0.001] * 410 + [-0.01] * 60 + [0.012] * 30), None, None, CFG)
check("下落継続中は🩸", down["value"]["bottom"]["ok"] is False)
check("反発+20日線上で🟢", rec_["value"]["bottom"]["ok"] is True)

print("[noAlloc(v3.21)]")
na = E.analyze_instrument({**META, "noAlloc": True}, mk_hist([0.001] * 400), None, None, CFG)
check("noAllocがinstへ伝播", na["noAlloc"] is True)

print("[サイクル補正(v3.20)]")
d_in, _ = E.value_cycle_delta(5, 2025, "inflow", defensive=False)
d_ind, f_ind = E.value_cycle_delta(5, 2025, "inflow", defensive=True)
d_out, _ = E.value_cycle_delta(5, 2025, "outflow", defensive=True)
check("流入+3(通常)", d_in == 3)
check("ディフェンシブ流入は加点なし", d_ind == 0 and any("加点なし" in x for x in f_ind))
check("流出-3は維持", d_out == -3)
check("上限±8", E.value_cycle_delta(9, 2026, "inflow")[0] == 8)
check("DEFENSIVE_ROT定義", set(E.DEFENSIVE_ROT) == {"生活必需品", "ヘルスケア", "公益"})

print("[planner_weights(noAlloc除外+インデックス枠)]")
def fake(tk, sub, conv=4, noAlloc=False, score=60):
    return {"ticker": tk, "class": "stock", "state": "OK", "subSector": sub, "conviction": conv,
            "noAlloc": noAlloc, "value": {"score": score, "tier": "watch"}, "holdMgmt": {}}
w = E.planner_weights([fake("VOO", "インデックス(コア)", 5), fake("NVDA", "AI・半導体/クラウド", 5),
                       fake("WMT", "その他モート", 4, noAlloc=True), fake("ICE", "その他モート", 4)], CFG)
check("監視専用は配分ゼロ", "WMT" not in w)
check("インデックス枠30", abs(w.get("VOO", 0) - 30) < 0.01)
check("その他モート枠をICEが独占", abs(w.get("ICE", 0) - 5) < 0.01)

print("[build_alerts(二段構え+底タグ)]")
def vinst(tk, tier, score, streaks, bok):
    return {"ticker": tk, "state": "OK", "momentum": {}, "holdMgmt": {},
            "value": {"score": score, "tier": tier, "factors": ["f1", "f2", "f3"],
                      "streaks": streaks, "bottom": {"ok": bok, "note": "n"}}}
al = E.build_alerts([
    vinst("A", "strong", 85, {"65": 9, "80": 1, "90": 0}, True),    # 速報1日目
    vinst("B", "strong", 85, {"65": 9, "80": 3, "90": 0}, True),    # 確定
    vinst("C", "absolute", 92, {"65": 9, "80": 9, "90": 4}, False), # 確定+🩸
    vinst("D", "strong", 85, None, True),                            # streaks未生成(初回互換)
], [], {}, {"settings": {"minScoreToAlert": 60}})
am = {a["ticker"]: a for a in al}
check("⏳速報(1/3日)+降格", "⏳速報(1/3日)" in am["A"]["title"] and am["A"]["priority"] == 3)
check("✅確定(3日連続)", "✅確定(3日連続)" in am["B"]["title"] and am["B"]["priority"] == 2)
check("🩸タグは降格なし", "🩸" in am["C"]["title"] and am["C"]["priority"] == 1)
check("streaksなしは従来表記", "速報" not in am["D"]["title"] and am["D"]["priority"] == 2)

print("[品質統合]")
qi = E.analyze_instrument(dict(META), mk_hist([0.0008] * 460 + [-0.02] * 40), None, None, CFG)
s0, r0 = qi["value"]["score"], qi["rec"]["score"]
E.apply_quality(qi, {"score": 20, "parts": {}, "warnings": [], "metrics": {}}, CFG["valueParams"])
check("低品質でスコア低下", qi["value"]["score"] < s0)
check("recにも同期", qi["rec"]["score"] <= r0)

print(f"\n結果: ✅{PASS} / ❌{FAIL}")
sys.exit(1 if FAIL else 0)
