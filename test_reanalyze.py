# -*- coding: utf-8 -*-
"""reanalyze.py のテスト(v3.23時点: モジュール健全性+定数の妥当性のみ)。
※的中判定ロジックは現在main()内にインラインのため単体テスト不可。
  v3.24でTriple Barrier方式の純関数へ切り出し、ここに判定テストを追加する予定。"""
import sys

import reanalyze as R

PASS = FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  ✅ {name} {extra}")
    else:
        FAIL += 1; print(f"  ❌ {name} {extra}")


print("[定数の妥当性]")
check("的中閾値が正", R.VALUE_HIT_PCT > 0 and R.MOM_HIT_PCT > 0)
check("失敗閾値が負", R.VALUE_MISS_PCT < 0 and R.MOM_MISS_PCT < 0)
check("損切りは-8%(モメンタムルールと整合)", R.MOM_MISS_PCT == -8)
print("[自動調整の安全範囲]")
for key, (lo, hi) in R.BOUNDS.items():
    check(f"BOUNDS {key}: lo<hi", lo < hi, f"({lo},{hi})")
check("minScoreToAlert範囲が常識的", R.BOUNDS["minScoreToAlert"][0] >= 40 and R.BOUNDS["minScoreToAlert"][1] <= 80)

print(f"\n結果: ✅{PASS} / ❌{FAIL}")
sys.exit(1 if FAIL else 0)
