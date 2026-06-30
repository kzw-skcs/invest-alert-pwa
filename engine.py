"""
投資シグナル判定エンジン（純ロジック部）
- 外部通信なし。価格系列(closes)を渡すと、自動トレンドライン・状態判定・MAクロスを計算する。
- ここはネットワークに依存しないので単体テスト可能（test_engine.py 参照）。
"""
from __future__ import annotations

# ---- 状態の定義 -------------------------------------------------------------
STATE = {
    "SUPER_BUY":  {"label": "超買い場", "emoji": "⭐🟢", "color": "#15803d", "actionable": True,  "side": "buy"},
    "SUPER_SELL": {"label": "超売り場", "emoji": "⭐🔴", "color": "#b91c1c", "actionable": True,  "side": "sell"},
    "BUY":        {"label": "買い場",   "emoji": "🟦",   "color": "#2563eb", "actionable": True,  "side": "buy"},
    "SELL":       {"label": "売り場",   "emoji": "🟥",   "color": "#dc2626", "actionable": True,  "side": "sell"},
    "RISING":     {"label": "上昇中",   "emoji": "🟢",   "color": "#16a34a", "actionable": False, "side": "up"},
    "FALLING":    {"label": "下落中",   "emoji": "🔻",   "color": "#ea580c", "actionable": False, "side": "down"},
    "NEUTRAL":    {"label": "レンジ内", "emoji": "⬜",   "color": "#6b7280", "actionable": False, "side": "none"},
    "UNKNOWN":    {"label": "データ不足","emoji": "❓",  "color": "#9ca3af", "actionable": False, "side": "none"},
}


def linregress(xs, ys):
    """最小二乗法で (slope, intercept) を返す。"""
    n = len(xs)
    if n == 0:
        return 0.0, 0.0
    sx = sum(xs); sy = sum(ys)
    sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return 0.0, sy / n
    slope = (n * sxy - sx * sy) / denom
    intercept = (sy - slope * sx) / n
    return slope, intercept


def sma(series, n):
    """単純移動平均の系列を返す（最初の n-1 個は None）。"""
    out = [None] * len(series)
    if len(series) < n:
        return out
    s = sum(series[:n])
    out[n - 1] = s / n
    for i in range(n, len(series)):
        s += series[i] - series[i - n]
        out[i] = s / n
    return out


def find_pivots(values, span=5, kind="high"):
    """局所的な高値/安値（ピボット）を [(index, value), ...] で返す。"""
    pivots = []
    n = len(values)
    for i in range(span, n - span):
        window = values[i - span:i + span + 1]
        if kind == "high" and values[i] == max(window):
            pivots.append((i, values[i]))
        elif kind == "low" and values[i] == min(window):
            pivots.append((i, values[i]))
    return pivots


def trendline_channel(series, span=5):
    """
    1年分の終値系列から自動トレンドライン（天井/底ライン）を算出。
    ピボット高値どうし・安値どうしを線形回帰で結ぶ（＝手描きトレンドラインの自動近似）。
    返り値: dict(top_now, bottom_now, top_slope, top_int, bot_slope, bot_int, method)
    """
    n = len(series)
    last_x = n - 1
    highs = find_pivots(series, span, "high")
    lows = find_pivots(series, span, "low")

    method = "pivot"
    if len(highs) >= 2:
        ts, tb = linregress([p[0] for p in highs], [p[1] for p in highs])
    else:
        ts, tb = 0.0, max(series)
        method = "fallback"
    if len(lows) >= 2:
        bs, bb = linregress([p[0] for p in lows], [p[1] for p in lows])
    else:
        bs, bb = 0.0, min(series)
        method = "fallback"

    top_now = ts * last_x + tb
    bottom_now = bs * last_x + bb

    # ガード：線が交差/反転した場合は水平の高値・安値レンジにフォールバック
    if not (top_now > bottom_now) or top_now <= 0 or bottom_now <= 0:
        top_now = max(series)
        bottom_now = min(series)
        ts = bs = 0.0
        tb = top_now; bb = bottom_now
        method = "fallback"

    return {
        "top_now": top_now, "bottom_now": bottom_now,
        "top_slope": ts, "top_int": tb,
        "bot_slope": bs, "bot_int": bb,
        "method": method,
    }


def detect_cross(short_arr, long_arr):
    """最新日に発生したクロスを返す: 'golden' / 'dead' / None"""
    if len(short_arr) < 2 or len(long_arr) < 2:
        return None
    s_now, s_prev = short_arr[-1], short_arr[-2]
    l_now, l_prev = long_arr[-1], long_arr[-2]
    if None in (s_now, s_prev, l_now, l_prev):
        return None
    if s_prev <= l_prev and s_now > l_now:
        return "golden"
    if s_prev >= l_prev and s_now < l_now:
        return "dead"
    return None


def classify(closes, settings):
    """
    終値系列(closes)と設定から、最新時点の状態を判定する。
    返り値: dict（state キー・各種数値・トレンドライン値など）
    """
    s = settings
    lookback = s["lookbackDays"]
    band_pct = s["bandPctOfRange"]
    ma_short = s["maShort"]
    ma_long = s["maLong"]
    ma_trend = s.get("maTrend", 200)
    bk = s["breakoutLookbackDays"]

    # 長期トレンド線(200日) + 直近1年トレンドライン + クロス検出に十分なデータが必要
    need = max(ma_long, ma_trend) + 5
    if len(closes) < need:
        return {"state": "UNKNOWN", "reason": f"データ不足(必要{need}日/現在{len(closes)}日)"}

    price = closes[-1]
    window = closes[-lookback:] if len(closes) >= lookback else closes[:]
    ch = trendline_channel(window)
    top = ch["top_now"]; bottom = ch["bottom_now"]
    rng = top - bottom
    band = band_pct * rng

    # トレンドライン系列（ブレイクアウト履歴の判定に使用）
    wn = len(window)
    top_arr = [ch["top_slope"] * x + ch["top_int"] for x in range(wn)]
    bot_arr = [ch["bot_slope"] * x + ch["bot_int"] for x in range(wn)]
    start = max(0, wn - bk)
    broke_above_recently = any(window[i] > top_arr[i] for i in range(start, wn))
    broke_below_recently = any(window[i] < bot_arr[i] for i in range(start, wn))

    # 移動平均クロス（短期/長期：既定20/50）
    sma_s = sma(closes, ma_short)
    sma_l = sma(closes, ma_long)
    cross = detect_cross(sma_s, sma_l)
    sma_s_now = sma_s[-1]; sma_l_now = sma_l[-1]

    # 長期トレンド（逆張りフィルタ用：200日線）
    sma_t = sma(closes, ma_trend)
    sma_t_now = sma_t[-1]
    sma_t_prev = sma_t[-6] if len(sma_t) >= 6 and sma_t[-6] is not None else None
    if sma_t_now is None:
        trend = None
    else:
        rising = (sma_t_prev is None) or (sma_t_now >= sma_t_prev)
        trend = "up" if (price >= sma_t_now and rising) else "down"

    # 状態判定（優先度順）
    if cross == "golden" and broke_below_recently:
        state = "SUPER_BUY"
    elif cross == "dead" and broke_above_recently:
        state = "SUPER_SELL"
    elif price > top:
        state = "RISING"
    elif price < bottom:
        state = "FALLING"
    elif price <= bottom + band:
        state = "BUY"
    elif price >= top - band:
        state = "SELL"
    else:
        state = "NEUTRAL"

    pct_in_range = ((price - bottom) / rng * 100) if rng > 0 else None

    # ===== 長期（5年）タイムフレームのチャネル =====
    lb_long = s.get("lookbackLongDays", 1260)
    min_long = s.get("longRangeMinDays", 504)
    lt_low_pct = s.get("longTermLowPct", 50)
    top_long = bottom_long = pir_long = trend_long = None
    long_avail = False
    long_low = False
    if len(closes) >= min_long:
        wlong = closes[-lb_long:] if len(closes) >= lb_long else closes[:]
        lch = trendline_channel(wlong)
        top_long = lch["top_now"]; bottom_long = lch["bottom_now"]
        rng_long = top_long - bottom_long
        pir_long = ((price - bottom_long) / rng_long * 100) if rng_long > 0 else None
        trend_long = "up" if lch["bot_slope"] >= 0 else "down"
        long_avail = True
        long_low = (pir_long is not None and pir_long <= lt_low_pct)

    # サイクル推定：底→天井のおおよその所要（取引日）。ピボット間隔の中央値。
    merged = sorted([i for i, _ in find_pivots(window, 5, "high")] +
                    [i for i, _ in find_pivots(window, 5, "low")])
    cyc = None
    if len(merged) >= 2:
        gaps = sorted(merged[k + 1] - merged[k] for k in range(len(merged) - 1))
        cyc = gaps[len(gaps) // 2]

    return {
        "state": state,
        "price": round(price, 4),
        "top": round(top, 4),
        "bottom": round(bottom, 4),
        "band": round(band, 4),
        "pctInRange": round(pct_in_range, 1) if pct_in_range is not None else None,
        "distToBottomPct": round((price - bottom) / bottom * 100, 2),
        "distToTopPct": round((top - price) / price * 100, 2),
        "smaShort": round(sma_s_now, 4) if sma_s_now else None,
        "smaLong": round(sma_l_now, 4) if sma_l_now else None,
        "smaTrend": round(sma_t_now, 4) if sma_t_now else None,
        "trend": trend,
        "cross": cross,
        "brokeAboveRecently": broke_above_recently,
        "brokeBelowRecently": broke_below_recently,
        "trendMethod": ch["method"],
        "cycleHalfDays": cyc,
        "topLong": round(top_long, 4) if top_long is not None else None,
        "bottomLong": round(bottom_long, 4) if bottom_long is not None else None,
        "pctInRangeLong": round(pir_long, 1) if pir_long is not None else None,
        "trendLong": trend_long,
        "longRangeAvailable": long_avail,
        "longTermLow": long_low,
    }


def _period_label(cal_days):
    if cal_days is None:
        return None
    if cal_days < 14:
        return f"約{cal_days}日"
    if cal_days < 60:
        return f"約{round(cal_days / 7)}週間"
    return f"約{round(cal_days / 30)}ヶ月"


def recommend(inst, vix, settings):
    """
    資産ごとの推奨度を算出（機械的スコア。投資助言ではない）。
    返り値: score(0-100), stars(1-5), level(S/A/B/C/-), action(buy/sell/hold),
            買い推奨には expReturnPct(天井までの見込み%), expDays(想定日数),
            expPeriodLabel, annualizedPct(年率換算) を付与。
    """
    out = {"score": 0, "stars": 0, "level": "-", "action": "hold",
           "expReturnPct": None, "expDays": None, "expPeriodLabel": None,
           "annualizedPct": None, "note": ""}
    state = inst.get("state", "UNKNOWN")
    price = inst.get("price"); top = inst.get("top"); bottom = inst.get("bottom")
    if state == "UNKNOWN" or price is None:
        return out
    vlevel = vix.get("level") if state in ("SUPER_BUY", "SUPER_SELL") else None
    pir = inst.get("pctInRange")
    pir_long = inst.get("pctInRangeLong")
    long_low = inst.get("longTermLow", False)
    trend_long = inst.get("trendLong")
    trend = inst.get("trend")
    policy = inst.get("tradePolicy", "trade")
    counter = inst.get("counterTrend", False)

    # 長期保有方針(accumulate/hold)：売りシグナルは推奨しない
    if policy in ("accumulate", "hold") and state in ("SELL", "SUPER_SELL"):
        out["action"] = "hold"
        out["note"] = "長期保有方針のため売りシグナルは無効（押し目買い専用）"
        out["score"] = 10; out["stars"] = 1; out["level"] = "-"
        return out
    # 逆張りフィルタ：下降トレンド中の通常「買い場」は非推奨
    if counter:
        out["action"] = "avoid"
        out["note"] = "下降トレンド中の逆張り買い（フィルタ対象・非推奨）"
        out["score"] = 12; out["stars"] = 1; out["level"] = "-"
        if top and price and top > price:
            out["expReturnPct"] = round((top - price) / price * 100, 1)
        return out

    if state in ("SUPER_BUY", "BUY"):
        out["action"] = "buy"
        base = 80 if state == "SUPER_BUY" else 55
        prox = 0.0
        if pir is not None:
            prox = 20.0 if pir < 0 else max(0.0, min(20.0, 15 - pir))
        exp = ((top - price) / price * 100) if (top and price) else 0.0
        if exp > 0:  # 天井ラインまでの上値余地（プラスのときだけ見込みとして提示）
            upside = max(0.0, min(20.0, exp / 2.0))
            out["expReturnPct"] = round(exp, 1)
            cyc = inst.get("cycleHalfDays")
            if cyc and cyc > 0:
                cal = round(cyc * 7 / 5)
                out["expDays"] = cal
                out["expPeriodLabel"] = _period_label(cal)
                try:
                    out["annualizedPct"] = round(((1 + exp / 100) ** (252.0 / cyc) - 1) * 100, 1)
                except Exception:  # noqa
                    pass
        else:  # 既に天井ライン超過（上昇局面）→ 目標は提示せず注記
            upside = 5.0
            out["note"] = "価格が天井ライン超過（上昇局面）。明確な上値目標なし"
        vbonus = 10 if vlevel == "緊急" else (5 if vlevel == "警戒" else 0)
        tbonus = 8 if trend == "up" else 0  # 順張り（上昇トレンド）の買いを優遇
        # 長期(5年)でも下段なら押し目買いとして加点（マルチTFの合致）
        ltbonus = 0.0
        if pir_long is not None:
            ltbonus = max(0.0, min(12.0, (50 - pir_long) * 0.4)) if pir_long < 50 else 0.0
            if long_low and trend_long == "up":
                ltbonus += 4
                out["note"] = (out["note"] + " ／ " if out["note"] else "") + \
                    f"押し目買い：長期(5年)も上昇トレンドの下段（長期位置 {pir_long:.0f}%）"
        score = base + prox + upside + vbonus + tbonus + ltbonus
    elif state in ("SUPER_SELL", "SELL"):
        out["action"] = "sell"
        base = 80 if state == "SUPER_SELL" else 55
        prox = 0.0
        if pir is not None:
            prox = 20.0 if pir > 100 else max(0.0, min(20.0, pir - 85))
        vbonus = 10 if vlevel == "緊急" else (5 if vlevel == "警戒" else 0)
        tbonus = 8 if trend == "down" else 0  # 下降トレンド中の売りを優遇
        # 長期(5年)が上昇トレンドの下〜中段での売りは格下げ（長期上昇の押し目を売らない）
        ltpenalty = 0.0
        if long_low and trend_long == "up":
            ltpenalty = 20.0
            out["note"] = "長期(5年)は上昇トレンドの下段。短期の売りは弱め（長期はホールド寄り）"
        score = base + prox + vbonus + tbonus - ltpenalty
    else:
        out["action"] = "hold"
        score = {"RISING": 35, "FALLING": 25, "NEUTRAL": 15}.get(state, 10)

    score = max(0.0, min(100.0, score))
    out["score"] = round(score)
    out["stars"] = 5 if score >= 80 else 4 if score >= 65 else 3 if score >= 50 else 2 if score >= 35 else 1
    out["level"] = "S" if score >= 80 else "A" if score >= 65 else "B" if score >= 50 else "C" if score >= 35 else "-"
    return out


def vix_status(vix_closes, settings):
    """VIXの前日比変化『または』絶対水準から警戒レベルを返す。"""
    if len(vix_closes) < 2:
        return {"value": None, "changePct": None, "level": None, "reason": None}
    now, prev = vix_closes[-1], vix_closes[-2]
    change = (now - prev) / prev * 100 if prev else 0.0
    a = abs(change)
    abs_warn = settings.get("vixAbsWarn", 30)
    abs_urgent = settings.get("vixAbsUrgent", 40)
    level, reason = None, None
    if a >= settings["vixUrgentPct"]:
        level, reason = "緊急", f"前日比{change:+.0f}%"
    elif now >= abs_urgent:
        level, reason = "緊急", f"VIX水準{now:.0f}（≥{abs_urgent}）"
    elif a >= settings["vixWarnPct"]:
        level, reason = "警戒", f"前日比{change:+.0f}%"
    elif now >= abs_warn:
        level, reason = "警戒", f"VIX水準{now:.0f}（≥{abs_warn}）"
    return {"value": round(now, 2), "changePct": round(change, 1), "level": level, "reason": reason}
