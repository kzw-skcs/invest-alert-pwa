# -*- coding: utf-8 -*-
"""v3.21のconfig変更を「現在のconfig.json」へ安全に注入する(丸ごと上書きしない)。
実行: python3 apply_v321_config.py   (リポジトリ直下で、git pull後に実行)
内容: ①VOO追加(コア・買い増し専用) ②インデックス枠30%新設 ③生活必需品5銘柄を監視専用(noAlloc)に。
再実行しても安全(冪等)。"""
import json

with open("config.json", encoding="utf-8") as f:
    cfg = json.load(f)

if not any(s["ticker"] == "VOO" for s in cfg["stocks"]):
    cfg["stocks"].insert(0, {
        "ticker": "VOO", "stooq": "voo.us", "name": "Vanguard S&P500 ETF",
        "sector": "インデックス", "subSector": "インデックス(コア)", "rotSector": "インデックス",
        "tradePolicy": "accumulate", "conviction": 5,
        "note": "外れない土台。暴落時(Tier80+✅確定)に厚く買い増し、売らない。選択リスクゼロの保険"})
    print("VOO 追加")

cfg["portfolio"]["stockSectorTargets"] = {
    "インデックス(コア)": 30, "AI・半導体/クラウド": 35, "電力・DCインフラ": 10,
    "エネルギー": 10, "宇宙・防衛": 10, "その他モート": 5}
print("サブセクター枠更新:", cfg["portfolio"]["stockSectorTargets"])

for s in cfg["stocks"]:
    if s.get("rotSector") == "生活必需品" and not s.get("noAlloc"):
        s["noAlloc"] = True
        if "【監視専用】" not in (s.get("note") or ""):
            s["note"] = (s.get("note", "") + " 【監視専用】ディフェンシブRSはリスクオフ判定の材料。防御役は金+現金グライドとVOO内包分が代替").strip()
        print("監視専用化:", s["ticker"])

with open("config.json", "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
print("config.json 更新完了")
