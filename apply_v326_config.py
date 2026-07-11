# -*- coding: utf-8 -*-
"""v3.26のconfig変更を現在のconfig.jsonへ安全に注入する(丸ごと上書きしない・冪等)。
実行: python3 apply_v326_config.py   (リポジトリ直下で、git pull後に実行)
内容: Champion/Challenger仮想運用のChallenger定義を追加。
Challenger = 厳選アーム(スコア閾値85/92・確定5日連続)。本番シグナルには一切影響せず、
episodes.jsonにarm="challenger"として並走記録され、50件+180日後に対決判定が出る。"""
import json
from datetime import date

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

if "challenger" not in cfg:
    cfg["challenger"] = {
        "enabled": True,
        "label": "厳選アーム(閾値85/92・確定5日)",
        "thresholds": {"80": 85, "90": 92},
        "confirmN": 5,
        "startedAt": date.today().isoformat(),
        "_comment": "Champion(現行80/90・確定3日)と同じスコア系列で並走する仮想運用。本番シグナルには無影響。50件+180日で対決判定"
    }
    print("challenger 追加:", cfg["challenger"]["label"])
else:
    print("challenger は設定済み(変更なし):", cfg["challenger"].get("label"))

with open("config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("config.json 更新完了")
