"""偽の価格を一切出さない初期プレースホルダ data.json を生成。
実データは GitHub Actions の daily 実行後に上書きされる。"""
import json, os, datetime
HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    cfg = json.load(f)

instruments = []
def add(meta, cls):
    instruments.append({
        "key": meta.get("key") or meta.get("ticker"),
        "ticker": meta.get("ticker") or meta.get("key"),
        "name": meta["name"], "sector": meta.get("sector", ""), "class": cls,
        "state": "UNKNOWN", "stateLabel": "実データ未取得", "emoji": "⏳",
        "color": "#9ca3af", "side": "none", "actionable": False,
        "tradePolicy": meta.get("tradePolicy", "trade"),
        "price": None, "top": None, "bottom": None, "history": [],
        "rec": {"action": "hold", "score": 0, "stars": 0, "level": "-"},
        "longRangeAvailable": False,
    })
for s in cfg["stocks"]:
    add(s, "stock")
for m in cfg["metals"]:
    add(m, "metal")
for c in cfg["crypto"]:
    add(c, "crypto")

out = {
    "updatedJST": "実データ未取得",
    "needsRun": True, "sample": True,
    "settings": cfg["settings"], "targets": cfg["targets"],
    "vix": {"value": None, "changePct": None, "level": None},
    "fx": {"usdjpy": None},
    "instruments": instruments, "alerts": [], "errors": [],
}
with open(os.path.join(HERE, "data.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"プレースホルダ data.json 生成: {len(instruments)}資産（価格なし）")
