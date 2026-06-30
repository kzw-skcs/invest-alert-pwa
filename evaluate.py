"""
毎日の判定処理（GitHub Actions が 6:00 JST に実行）。
 1) config.json を読む
 2) Stooq（米国株・金銀・VIX）と CoinGecko（BTC/ETH）から日次終値を取得
 3) engine.classify で各資産の状態を判定
 4) data.json を書き出す（PWA がこれを表示）
 5) アラート対象があれば Web Push を送信（VAPID鍵と subscriptions.json がある場合のみ）

ネットワークは GitHub Actions 上では自由に使えます。各資産は取得失敗しても全体は止めません。
"""
from __future__ import annotations
import csv, io, json, os, time, datetime, urllib.request, urllib.error
import engine as E

HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (invest-alert-pwa)"}
HIST_KEEP = 150  # PWA のスパークライン用に残す日数


def http_get(url, tries=3, timeout=30):
    last = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as e:  # noqa
            last = e
            time.sleep(2 * (k + 1))
    raise last


def fetch_stooq(symbol):
    """Stooq から日次終値 [(date, close), ...] を取得（古い→新しい順）。"""
    url = f"https://stooq.com/q/d/l/?s={symbol}&i=d"
    text = http_get(url)
    rows = list(csv.DictReader(io.StringIO(text)))
    out = []
    for r in rows:
        c = r.get("Close") or r.get("close")
        d = r.get("Date") or r.get("date")
        if c and c not in ("N/D", "") and d:
            try:
                out.append((d, float(c)))
            except ValueError:
                pass
    if not out:
        raise RuntimeError(f"Stooq: データ空 ({symbol}) / 応答先頭: {text[:80]!r}")
    return out


def fetch_coingecko(coin_id, days=1825):
    """CoinGecko から日次終値 [(date, close), ...] を取得。
    無料枠で長期(1825日)が拒否される場合は365日にフォールバックする。"""
    last_err = None
    for d_param in (days, 365):
        try:
            url = (f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
                   f"?vs_currency=usd&days={d_param}")
            data = json.loads(http_get(url))
            prices = data.get("prices") or []
            out = []
            for ts, price in prices:
                d = datetime.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                out.append((d, float(price)))
            if out:
                return out
            last_err = RuntimeError(f"CoinGecko: データ空 ({coin_id}, days={d_param})")
        except Exception as e:  # noqa
            last_err = e
    raise last_err or RuntimeError(f"CoinGecko: 取得失敗 ({coin_id})")


def build_instrument(meta, series, settings, asset_class):
    closes = [c for _, c in series]
    res = E.classify(closes, settings)
    st = E.STATE.get(res.get("state", "UNKNOWN"), E.STATE["UNKNOWN"])
    state = res.get("state", "UNKNOWN")
    policy = meta.get("tradePolicy", "trade")
    actionable = st["actionable"]
    counter = False
    # 逆張りフィルタ：本物の長期(5年)下降トレンド中の通常「買い場」のみ抑制。
    # 横ばいボックスや上昇チャネルの下限買いは残す（5年トレンドが取れない時のみ200日線で代替）。
    ltrend = res.get("trendLong")
    downtrend = (ltrend == "down") if ltrend is not None else (res.get("trend") == "down")
    if state == "BUY" and downtrend and settings.get("counterTrendFilter", True):
        actionable = False; counter = True
    # 長期保有方針(accumulate/hold)：売りシグナルは出さない
    if policy in ("accumulate", "hold") and st["side"] == "sell":
        actionable = False
    hist = [{"d": d, "c": round(c, 4)} for d, c in series[-HIST_KEEP:]]
    inst = {
        "key": meta.get("key") or meta.get("ticker"),
        "ticker": meta.get("ticker") or meta.get("key"),
        "name": meta["name"],
        "sector": meta.get("sector", ""),
        "class": asset_class,
        "stateLabel": st["label"], "emoji": st["emoji"], "color": st["color"],
        "side": st["side"], "actionable": actionable,
        "history": hist,
    }
    inst.update(res)
    inst["tradePolicy"] = policy
    inst["counterTrend"] = counter
    return inst


def main():
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    settings = cfg["settings"]
    instruments, errors = [], []

    def add(meta, fetch_fn, asset_class):
        try:
            series = fetch_fn()
            instruments.append(build_instrument(meta, series, settings, asset_class))
        except Exception as e:  # noqa
            errors.append({"name": meta.get("name"), "error": str(e)})
            instruments.append({
                "key": meta.get("key") or meta.get("ticker"),
                "ticker": meta.get("ticker") or meta.get("key"),
                "name": meta["name"], "sector": meta.get("sector", ""),
                "class": asset_class, "state": "UNKNOWN",
                "stateLabel": "取得失敗", "emoji": "⚠️", "color": "#9ca3af",
                "side": "none", "actionable": False, "history": [], "error": str(e),
            })

    for s in cfg["stocks"]:
        add(s, lambda s=s: fetch_stooq(s["stooq"]), "stock")
    for m in cfg["metals"]:
        add(m, lambda m=m: fetch_stooq(m["stooq"]), "metal")
    for c in cfg["crypto"]:
        add(c, lambda c=c: fetch_coingecko(c["coingeckoId"]), "crypto")

    # VIX
    vix = {"value": None, "changePct": None, "level": None}
    try:
        vseries = fetch_stooq(cfg["vix"]["stooq"])
        vix = E.vix_status([c for _, c in vseries], settings)
    except Exception as e:  # noqa
        errors.append({"name": "VIX", "error": str(e)})

    # USD/JPY（円建て評価額・損益の計算に使用）
    usdjpy = None
    try:
        fxseries = fetch_stooq("usdjpy")
        usdjpy = round(fxseries[-1][1], 3)
    except Exception as e:  # noqa
        errors.append({"name": "USDJPY", "error": str(e)})

    # 各資産に推奨度を付与（VIX確定後）
    for inst in instruments:
        inst["rec"] = E.recommend(inst, vix, settings)

    # アラート抽出（actionable な状態のみ）＋推奨度・見込みリターン
    min_score = settings.get("minScoreToAlert", 0)
    alerts = []
    for inst in instruments:
        if inst.get("actionable") and inst.get("rec", {}).get("score", 0) >= min_score:
            rec = inst.get("rec", {})
            vlevel = vix.get("level") if inst["state"] in ("SUPER_BUY", "SUPER_SELL") else None
            stars = "★" * rec.get("stars", 0)
            msg = f"{inst['name']}（{inst.get('ticker','')}）が「{inst['stateLabel']}」 推奨度{stars}"
            if rec.get("action") == "buy" and rec.get("expReturnPct") is not None:
                period = rec.get("expPeriodLabel") or "—"
                msg += f" 見込み+{rec['expReturnPct']}%・{period}"
            if vlevel:
                msg += f" ＋VIX{vlevel}({vix.get('reason') or ''})"
            alerts.append({
                "key": inst["key"], "name": inst["name"], "ticker": inst.get("ticker"),
                "state": inst["state"], "label": inst["stateLabel"], "side": inst["side"],
                "score": rec.get("score", 0), "stars": rec.get("stars", 0),
                "level": rec.get("level", "-"),
                "expReturnPct": rec.get("expReturnPct"),
                "expPeriodLabel": rec.get("expPeriodLabel"),
                "annualizedPct": rec.get("annualizedPct"),
                "vixLevel": vlevel, "message": msg,
            })
    # 推奨度が高い順
    alerts.sort(key=lambda a: a.get("score", 0), reverse=True)

    now = datetime.datetime.now(datetime.timezone.utc)
    jst = now.astimezone(datetime.timezone(datetime.timedelta(hours=9)))
    out = {
        "updated": now.isoformat(),
        "updatedJST": jst.strftime("%Y-%m-%d %H:%M JST"),
        "settings": settings,
        "targets": cfg["targets"],
        "vix": vix,
        "fx": {"usdjpy": usdjpy},
        "instruments": instruments,
        "alerts": alerts,
        "errors": errors,
    }
    with open(os.path.join(HERE, "data.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"data.json 書き出し完了: {len(instruments)}資産, アラート{len(alerts)}件, エラー{len(errors)}件")

    # 予測ログを追記（後日のアルゴリズム再分析＝的中率計算に使用）
    log_predictions(jst.strftime("%Y-%m-%d"), instruments, vix)

    # Web Push（任意：VAPID鍵 + subscriptions.json がある時だけ）
    try:
        send_push(alerts, vix)
    except Exception as e:  # noqa
        print("Push送信スキップ/失敗:", e)


def log_predictions(date_str, instruments, vix):
    """その日のアクション可能シグナルを predictions.jsonl に追記（同日同銘柄は重複させない）。"""
    path = os.path.join(HERE, "predictions.jsonl")
    existing = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    o = json.loads(line)
                    existing.add((o["date"], o["key"]))
                except Exception:  # noqa
                    pass
    added = 0
    with open(path, "a", encoding="utf-8") as f:
        for inst in instruments:
            if not inst.get("actionable"):
                continue
            if (date_str, inst["key"]) in existing:
                continue
            rec = inst.get("rec", {})
            f.write(json.dumps({
                "date": date_str, "key": inst["key"], "ticker": inst.get("ticker"),
                "state": inst["state"], "side": inst["side"],
                "price": inst.get("price"), "top": inst.get("top"), "bottom": inst.get("bottom"),
                "expReturnPct": rec.get("expReturnPct"), "expDays": rec.get("expDays"),
                "score": rec.get("score"), "evaluated": False, "hit": None,
            }, ensure_ascii=False) + "\n")
            added += 1
    print(f"予測ログ追記: {added}件 (predictions.jsonl)")


def send_push(alerts, vix):
    if not alerts:
        print("アラートなし → Push送信なし"); return
    priv = os.environ.get("VAPID_PRIVATE_KEY")
    email = os.environ.get("VAPID_CLAIMS_EMAIL", "mailto:kazawa@skclinicalsupport.com")
    sub_path = os.path.join(HERE, "subscriptions.json")
    if not priv or not os.path.exists(sub_path):
        print("VAPID鍵 or subscriptions.json 未設定 → Push送信なし（アプリ内表示のみ）"); return
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        print("pywebpush 未インストール → Push送信なし"); return

    with open(sub_path, encoding="utf-8") as f:
        subs = json.load(f)
    # 強い順に並べて先頭を通知タイトルに
    order = {"SUPER_BUY": 0, "SUPER_SELL": 0, "BUY": 1, "SELL": 1}
    alerts_sorted = sorted(alerts, key=lambda a: order.get(a["state"], 9))
    head = alerts_sorted[0]
    title = f"投資シグナル：{head['label']} {head['name']}"
    body = "／".join(a["message"] for a in alerts_sorted[:4])
    if len(alerts_sorted) > 4:
        body += f" ほか{len(alerts_sorted)-4}件"
    payload = json.dumps({"title": title, "body": body, "alerts": alerts}, ensure_ascii=False)

    still_valid = []
    for sub in subs:
        try:
            webpush(subscription_info=sub, data=payload,
                    vapid_private_key=priv, vapid_claims={"sub": email})
            still_valid.append(sub)
        except WebPushException as ex:
            code = getattr(ex.response, "status_code", None)
            if code in (404, 410):
                print("期限切れ購読を削除"); continue
            still_valid.append(sub); print("Push失敗(保持):", code)
    if len(still_valid) != len(subs):
        with open(sub_path, "w", encoding="utf-8") as f:
            json.dump(still_valid, f, ensure_ascii=False, indent=2)
    print(f"Push送信: {len(still_valid)}/{len(subs)} 件")


if __name__ == "__main__":
    main()
