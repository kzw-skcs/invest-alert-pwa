"""合成データで data.json を生成（オフライン確認・初回プレースホルダ用）。"""
import json, math, os, random, datetime
import engine as E
from evaluate import build_instrument

HERE = os.path.dirname(os.path.abspath(__file__))
random.seed(7)
with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
    cfg = json.load(f)
S = cfg["settings"]


def dates(n):
    start = datetime.date.today() - datetime.timedelta(days=n)
    return [(start + datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def series_buy(base):
    return [base + base * 0.1 * math.sin(2 * math.pi * i / 40) + random.uniform(-base*0.005, base*0.005) for i in range(280)]


def make(kind, base):
    n = 1320  # 約5年（長期レンジ表示の確認用）
    drift = base * 0.0003  # 緩やかな長期上昇（5年トレンド=up）
    amp = base * 0.1
    s = [(base + drift * i) + amp * math.sin(2 * math.pi * i / 40) for i in range(n)]
    center = base + drift * (n - 1)
    if kind == "BUY":       # 押し目（レンジ下限付近）
        s[-1] = center - amp * 0.95
    elif kind == "SELL":
        s[-1] = center + amp * 0.95
    elif kind == "RISING":
        s[-1] = center + amp * 1.6
    elif kind == "FALLING":
        s[-1] = center - amp * 1.6
    elif kind == "NEUTRAL":
        s[-1] = center
    return s


ds = dates(1320)
# 銘柄ごとに状態を割り当て（デモ用にばらけさせる）
states = ["BUY", "SELL", "NEUTRAL", "RISING", "FALLING"]
instruments = []
bases = {"NVDA":1200,"AAPL":230,"MSFT":480,"GOOGL":190,"AMZN":210,"META":600,"TSLA":250,
         "AMD":170,"AVGO":1700,"PLTR":45,"XOM":115,"CVX":160,"COP":110,"SLB":48,"NEE":80,
         "LMT":470,"RTX":120,"NOC":480,"BA":190,"RKLB":12}
for i, st in enumerate(cfg["stocks"]):
    kind = states[i % len(states)]
    base = bases.get(st["ticker"], 100)
    closes = make(kind, base)
    series = list(zip(ds, closes))
    instruments.append(build_instrument(st, series, S, "stock"))

for m, base in zip(cfg["metals"], [2650, 31]):
    kind = "BUY" if base > 1000 else "SELL"
    instruments.append(build_instrument(m, list(zip(ds, make(kind, base))), S, "metal"))
for c, base in zip(cfg["crypto"], [98000, 3500]):
    kind = "NEUTRAL" if base > 50000 else "RISING"
    instruments.append(build_instrument(c, list(zip(ds, make(kind, base))), S, "crypto"))

vix = E.vix_status([16.0, 17.2], S)
for inst in instruments:
    inst["rec"] = E.recommend(inst, vix, S)
alerts = []
for inst in instruments:
    if inst.get("actionable"):
        rec = inst.get("rec", {})
        vlevel = vix.get("level") if inst["state"] in ("SUPER_BUY","SUPER_SELL") else None
        alerts.append({"key": inst["key"], "name": inst["name"], "ticker": inst.get("ticker"),
                       "state": inst["state"], "label": inst["stateLabel"], "side": inst["side"],
                       "score": rec.get("score",0), "stars": rec.get("stars",0), "level": rec.get("level","-"),
                       "expReturnPct": rec.get("expReturnPct"), "expPeriodLabel": rec.get("expPeriodLabel"),
                       "annualizedPct": rec.get("annualizedPct"), "vixLevel": vlevel,
                       "message": f"{inst['name']}が「{inst['stateLabel']}」"})
alerts.sort(key=lambda a: a.get("score",0), reverse=True)

now = datetime.datetime.now(datetime.timezone.utc)
jst = now.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
out = {"updated": now.isoformat(), "updatedJST": jst.strftime("%Y-%m-%d %H:%M JST") + "（サンプル）",
       "settings": S, "targets": cfg["targets"], "vix": vix, "fx": {"usdjpy": 150},
       "instruments": instruments, "alerts": alerts, "errors": [],
       "sample": True}
with open(os.path.join(HERE, "data.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)
print(f"サンプル data.json 生成: {len(instruments)}資産, アラート{len(alerts)}件")
for inst in instruments:
    print(f"  {inst['ticker']:6} {inst['stateLabel']:6} price={inst.get('price')}")
