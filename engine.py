# -*- coding: utf-8 -*-
"""
engine.py v2 — 判定ロジック（純関数のみ・I/Oなし）

v1からの継承:
  - 1年/5年 回帰チャネル(ピボット法) + 6状態判定 + 20/50クロス + 200日トレンド
  - 逆張りフィルタ / マルチTF押し目 / VIX / 推奨度 / 見込みリターン

v2で追加:
  - RSI / ATR / 52週高値からの下落率 / 対S&P500相対強度 / 出来高確認
  - Value多因子スコア(0-100) + 4段階Tier(監視/購入検討/強い買い/絶対的買い場)
  - マクロ暴落 vs 個別悪材料の判別 (macroDriven)
  - モメンタム売買シグナル(trade銘柄): ブレイクアウトエントリー / 損切り / 部分利確 / タイムストップ
  - hold銘柄のテーゼ崩壊(見直し)判定・部分利確(トリム)判定
  - プランナー用の銘柄別推奨ウェイト
  - IPO等イベントのフェーズ管理とアラート

入力 history: [{"d": "YYYY-MM-DD", "c": close, "h": high, "l": low, "v": volume}, ...]
h/l/v は無い場合 None 可（ATRはclose近似、出来高確認はスキップ）
"""
from __future__ import annotations
import math
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 基本統計

def sma(vals, n, idx=None):
    if idx is None:
        idx = len(vals) - 1
    if idx + 1 < n:
        return None
    window = vals[idx + 1 - n: idx + 1]
    return sum(window) / n


def linreg(ys):
    """最小二乗 y = a + b*x, x = 0..n-1。(a, b) を返す。"""
    n = len(ys)
    if n < 2:
        return (ys[0] if ys else 0.0), 0.0
    sx = n * (n - 1) / 2.0
    sxx = (n - 1) * n * (2 * n - 1) / 6.0
    sy = sum(ys)
    sxy = sum(i * y for i, y in enumerate(ys))
    denom = n * sxx - sx * sx
    if denom == 0:
        return sy / n, 0.0
    b = (n * sxy - sx * sy) / denom
    a = (sy - b * sx) / n
    return a, b


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0.0))
        losses.append(max(-ch, 0.0))
    # Wilder平滑
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100 - 100 / (1 + rs), 1)


def atr(history, period=14):
    """ATR。h/l欠損時はclose-to-close近似。"""
    n = len(history)
    if n < period + 1:
        return None
    trs = []
    for i in range(1, n):
        c_prev = history[i - 1]["c"]
        h = history[i].get("h") or history[i]["c"]
        l = history[i].get("l") or history[i]["c"]
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        trs.append(tr)
    a = sum(trs[:period]) / period
    for t in trs[period:]:
        a = (a * (period - 1) + t) / period
    return a


def pct(a, b):
    return None if (a is None or b in (None, 0)) else round((a / b - 1) * 100, 2)

# ---------------------------------------------------------------- チャネル(v1継承)

def _pivots(closes, k=5):
    highs, lows = [], []
    for i in range(k, len(closes) - k):
        w = closes[i - k: i + k + 1]
        if closes[i] == max(w):
            highs.append((i, closes[i]))
        if closes[i] == min(w):
            lows.append((i, closes[i]))
    return highs, lows


def channel(closes, lookback):
    """回帰チャネル。ピボット十分ならピボット回帰、不足なら全体回帰+最大乖離オフセット。
    戻り: dict(top, bottom, method, slope) 最終バーで評価。"""
    seg = closes[-lookback:] if len(closes) > lookback else closes[:]
    n = len(seg)
    if n < 30:
        return None
    highs, lows = _pivots(seg)
    x_last = n - 1
    if len(highs) >= 3 and len(lows) >= 3:
        ah, bh = linreg([v for _, v in highs])
        # ピボットのx位置で回帰し直す(等間隔でないため加重)
        ah, bh = _linreg_xy(highs)
        al, bl = _linreg_xy(lows)
        top = ah + bh * x_last
        bottom = al + bl * x_last
        method = "pivot"
        slope = (bh + bl) / 2
    else:
        a, b = linreg(seg)
        resid = [seg[i] - (a + b * i) for i in range(n)]
        top = a + b * x_last + max(resid)
        bottom = a + b * x_last + min(resid)
        method = "regression"
        slope = b
    if top < bottom:
        top, bottom = bottom, top
    mid = (top + bottom) / 2 or 1
    return {"top": top, "bottom": bottom, "method": method,
            "slope": slope, "slopePctPerDay": slope / mid * 100}


def _linreg_xy(pts):
    n = len(pts)
    sx = sum(p[0] for p in pts); sy = sum(p[1] for p in pts)
    sxx = sum(p[0] ** 2 for p in pts); sxy = sum(p[0] * p[1] for p in pts)
    denom = n * sxx - sx * sx
    if denom == 0:
        return sy / n, 0.0
    b = (n * sxy - sx * sy) / denom
    return (sy - b * sx) / n, b


def cycle_half_days(closes, lookback=252):
    """ピボット間隔の中央値から半サイクル(底→天井)日数を推定。"""
    seg = closes[-lookback:] if len(closes) > lookback else closes
    highs, lows = _pivots(seg)
    idxs = sorted([i for i, _ in highs] + [i for i, _ in lows])
    if len(idxs) < 3:
        return None
    gaps = [idxs[i + 1] - idxs[i] for i in range(len(idxs) - 1)]
    gaps.sort()
    return gaps[len(gaps) // 2]

# ---------------------------------------------------------------- 状態判定(v1継承+整理)

def base_state(price, ch, sma_s, sma_l, closes, band_pct, breakout_lookback=90):
    """6状態: SUPER_BUY/BUY/UP/NEUTRAL/DOWN/SELL/SUPER_SELL"""
    rng = ch["top"] - ch["bottom"]
    band = rng * band_pct if rng > 0 else 0
    broke_below = any(c < ch["bottom"] - band for c in closes[-breakout_lookback:])
    broke_above = any(c > ch["top"] + band for c in closes[-breakout_lookback:])
    cross = _recent_cross(closes)
    golden = cross == "golden"
    dead = cross == "dead"

    if broke_below and golden:
        st = "SUPER_BUY"
    elif broke_above and dead:
        st = "SUPER_SELL"
    elif price <= ch["bottom"] + band:
        st = "BUY"
    elif price >= ch["top"] - band:
        st = "SELL"
    elif sma_s and sma_l and sma_s > sma_l:
        st = "UP"
    elif sma_s and sma_l and sma_s < sma_l:
        st = "DOWN"
    else:
        st = "NEUTRAL"
    return st, band, broke_above, broke_below, cross


def _recent_cross(closes, short_n=20, long_n=50, within=10):
    """直近withinバー以内の20/50クロス。"""
    if len(closes) < long_n + within + 1:
        return None
    for back in range(0, within):
        i = len(closes) - 1 - back
        s_now, l_now = sma(closes, short_n, i), sma(closes, long_n, i)
        s_prev, l_prev = sma(closes, short_n, i - 1), sma(closes, long_n, i - 1)
        if None in (s_now, l_now, s_prev, l_prev):
            continue
        if s_prev <= l_prev and s_now > l_now:
            return "golden"
        if s_prev >= l_prev and s_now < l_now:
            return "dead"
    return None


STATE_META = {
    "SUPER_BUY":  {"label": "⭐超・買い場", "emoji": "⭐🟢", "color": "#059669", "side": "buy"},
    "BUY":        {"label": "買い場",       "emoji": "🟦",   "color": "#2563eb", "side": "buy"},
    "UP":         {"label": "上昇中",       "emoji": "🟢",   "color": "#16a34a", "side": "none"},
    "NEUTRAL":    {"label": "レンジ内",     "emoji": "⬜",   "color": "#6b7280", "side": "none"},
    "DOWN":       {"label": "下落中",       "emoji": "🔻",   "color": "#d97706", "side": "none"},
    "SELL":       {"label": "売り場",       "emoji": "🟥",   "color": "#dc2626", "side": "sell"},
    "SUPER_SELL": {"label": "⭐超・売り場", "emoji": "⭐🔴", "color": "#991b1b", "side": "sell"},
}

# ---------------------------------------------------------------- マクロ/個別 判別

def macro_driven(closes, bench_closes, vix_value, window=60, vix_threshold=25):
    """下落がマクロ要因(市場全体)か個別要因かの判別。
    True=マクロ主導(Value買い候補) / False=個別主導(falling knife注意) / None=下落局面でない"""
    if len(closes) < window + 1:
        return None
    ret_1m = pct(closes[-1], closes[-21]) if len(closes) > 21 else None
    if ret_1m is None or ret_1m > -5:
        return None  # 有意な下落でない
    # 相関: 直近windowの日次リターン
    n = min(window, len(closes) - 1, len(bench_closes) - 1) if bench_closes else 0
    corr = None
    if n >= 20:
        r1 = [closes[-i] / closes[-i - 1] - 1 for i in range(1, n + 1)]
        r2 = [bench_closes[-i] / bench_closes[-i - 1] - 1 for i in range(1, n + 1)]
        corr = _pearson(r1, r2)
    bench_ret_1m = pct(bench_closes[-1], bench_closes[-21]) if bench_closes and len(bench_closes) > 21 else None
    vix_high = vix_value is not None and vix_value >= vix_threshold
    market_down = bench_ret_1m is not None and bench_ret_1m < -3
    correlated = corr is not None and corr > 0.5
    if (vix_high or market_down) and (correlated or corr is None):
        return True
    if bench_ret_1m is not None and ret_1m < bench_ret_1m - 10:
        return False  # 市場より10pt以上悪い→個別要因
    return None


def _pearson(a, b):
    n = len(a)
    if n < 3:
        return None
    ma_, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma_) * (b[i] - mb) for i in range(n))
    va = math.sqrt(sum((x - ma_) ** 2 for x in a))
    vb = math.sqrt(sum((x - mb) ** 2 for x in b))
    if va == 0 or vb == 0:
        return None
    return cov / (va * vb)

# ---------------------------------------------------------------- Value スコア

def value_score(inst_calc, vp):
    """多因子Valueスコア 0-100 と根拠。買い方向のみ(hold/accumulate銘柄向け)。
    inst_calc: analyze_instrument内の中間値dict"""
    f = []
    score = 0.0
    pos1 = inst_calc.get("pctInRange")          # 1年チャネル内位置 0-100
    posL = inst_calc.get("pctInRangeLong")      # 5年
    dd = inst_calc.get("drawdownPct") or 0      # 52週高値からの下落率(正の値)
    r = inst_calc.get("rsi")
    macro = inst_calc.get("macroDriven")
    trendL = inst_calc.get("trendLong")
    trend200_up = inst_calc.get("trend") == "up"
    state = inst_calc.get("state")

    # 1) 1年チャネル位置 (最大25点): 下限=25点, 上限=0点
    if pos1 is not None:
        p = max(0.0, min(100.0, pos1))
        pts = (100 - p) / 100 * 25
        score += pts
        if p <= 15:
            f.append(f"1年レンジ下限圏({p:.0f}%位置) +{pts:.0f}")
    # 2) 5年チャネル位置 (最大20点)
    if posL is not None:
        p = max(0.0, min(100.0, posL))
        pts = (100 - p) / 100 * 20
        score += pts
        if p <= 25:
            f.append(f"5年レンジ下段({p:.0f}%位置) +{pts:.0f}")
    # 3) 高値からの下落率 (最大20点)
    if dd >= vp["ddAbsolute"]:
        score += 20; f.append(f"高値-{dd:.0f}%の暴落水準 +20")
    elif dd >= vp["ddStrong"]:
        score += 14; f.append(f"高値-{dd:.0f}% +14")
    elif dd >= vp["ddConsider"]:
        score += 8; f.append(f"高値-{dd:.0f}% +8")
    # 4) RSI (最大15点)
    if r is not None:
        if r <= vp["rsiOversold"]:
            score += 15; f.append(f"RSI{r:.0f} 売られ過ぎ +15")
        elif r <= 40:
            score += 8; f.append(f"RSI{r:.0f} +8")
    # 5) マクロ主導の下落か (最大10点 / 個別要因は減点)
    if macro is True:
        score += 10; f.append("マクロ要因の下落(Value好機) +10")
    elif macro is False:
        score -= 15; f.append("個別要因の下落の疑い(falling knife) -15")
    # 6) 長期トレンド健全性 (最大10点)
    if trendL == "up":
        score += 6; f.append("5年トレンド上向き +6")
    if trend200_up:
        score += 4; f.append("200日線上向き +4")
    # 7) 状態ボーナス
    if state == "SUPER_BUY":
        score += 10; f.append("⭐超・買い場(GC確認) +10")
    elif state == "BUY":
        score += 5; f.append("買い場帯 +5")

    score = max(0, min(100, round(score)))
    if score >= vp["tierAbsolute"]:
        tier, label = "absolute", "🚨絶対的買い場"
    elif score >= vp["tierStrong"]:
        tier, label = "strong", "🔥強い買い"
    elif score >= vp["tierConsider"]:
        tier, label = "consider", "🟦購入検討"
    elif score >= vp["tierWatch"]:
        tier, label = "watch", "👀監視"
    else:
        tier, label = "none", "-"
    return {"score": score, "tier": tier, "tierLabel": label, "factors": f}

# ---------------------------------------------------------------- モメンタム

def momentum_signal(history, closes, calc, bench_closes, mp):
    """trade銘柄用モメンタム判定。現物のみ・買いエントリー→売り管理。
    サーバー側はシグナルと推奨水準のみ。実ポジションの損切り/利確はクライアントが取得単価から計算。"""
    n = len(closes)
    out = {"signal": "none", "note": ""}
    if n < mp["breakoutLookback"] + 5:
        out["note"] = "データ不足"
        return out
    price = closes[-1]
    hh = max(closes[-mp["breakoutLookback"] - 1:-1])  # 直近N日高値(当日除く)
    sma20 = calc.get("smaShort"); sma50 = calc.get("smaLong")
    r = calc.get("rsi")
    a = calc.get("atr")
    # 出来高確認
    vols = [h.get("v") for h in history if h.get("v")]
    vol_ok = None
    if len(vols) > mp["volAvgDays"] + 1:
        v_avg = sum(vols[-mp["volAvgDays"] - 1:-1]) / mp["volAvgDays"]
        vol_ok = vols[-1] >= v_avg * mp["volConfirmMult"] if v_avg > 0 else None
    # 相対強度(3ヶ月 vs S&P500)
    rs = None
    w = mp["relStrengthWindow"]
    if bench_closes and len(closes) > w and len(bench_closes) > w:
        rs = round((closes[-1] / closes[-w] - bench_closes[-1] / bench_closes[-w]) * 100, 1)
    out["relStrength3m"] = rs
    out["breakoutHigh"] = round(hh, 4)
    out["volConfirmed"] = vol_ok

    ma_ok = sma20 is not None and sma50 is not None and sma20 > sma50 and price > sma20
    rsi_ok = r is None or (mp["rsiMin"] <= r <= mp["rsiMax"])
    rs_ok = rs is None or rs >= mp["relStrengthMinPct"]
    breakout = price > hh

    if breakout and ma_ok and rsi_ok and rs_ok and (vol_ok is not False):
        out["signal"] = "entry"
        init_stop = price * (1 - mp["stopInitPct"] / 100)
        atr_stop = price - a * mp["atrMult"] if a else None
        stop = max(init_stop, atr_stop) if atr_stop else init_stop
        out.update({
            "entryPrice": round(price, 4),
            "stopSuggest": round(stop, 4),
            "stopPct": round((1 - stop / price) * 100, 1),
            "tp1": round(price * (1 + mp["tp1GainPct"] / 100), 4),
            "tp2": round(price * (1 + mp["tp2GainPct"] / 100), 4),
            "note": f"{mp['breakoutLookback']}日高値ブレイク"
                    + ("・出来高確認" if vol_ok else "")
                    + (f"・RS+{rs}%" if rs and rs > 0 else ""),
        })
    else:
        # 保有中の可能性に備えた売り管理シグナル(クライアントが取得単価と突合)
        exit_flags = []
        if sma50 and price < sma50 and closes[-2] >= (sma(closes, 50, n - 2) or price):
            exit_flags.append("50日線割れ")
        if len(closes) >= 2:
            gap = pct(closes[-1], closes[-2])
            if gap is not None and gap <= -mp["gapDownAlertPct"]:
                exit_flags.append(f"急落{gap:.1f}%")
        if calc.get("cross") == "dead":
            exit_flags.append("デッドクロス")
        if exit_flags:
            out["signal"] = "exit_warning"
            out["note"] = "・".join(exit_flags)
        # トレーリングストップの現在推奨値(保有者向け)
        if a:
            out["trailStop"] = round(price - a * mp["atrMult"], 4)
            out["atrPct"] = round(a / price * 100, 2)
    return out

# ---------------------------------------------------------------- モメンタム画面表示用の状態・推奨

def momentum_status(calc, mom, val, rec):
    """モメンタム画面(trade銘柄)表示用の状態と推奨。購入(エントリー)と売却(手仕舞い)を
    明確に分離し、両者が同時に立つことはない。Value指標はここでは判定に用いない
    (モメンタムのエントリーは 50日高値ブレイク+MA+RSI+相対強度+出来高+ATR のみ)。
      momState: ENTRY(買い) / EXIT(売り) / HOLD(保有継続・様子見) / WAIT(対象外) / NODATA
      momSide : buy / sell / none
      momRec  : {action, score, stars, label}  ← モメンタム独自の推奨度(★はValueと別軸)
    さらに、モメンタム買いが未成立でもValue的に激安なら『逆張り(Value)候補』を別枠フラグで保持。"""
    if mom.get("note") == "データ不足":
        return {"momState": "NODATA", "momStateLabel": "❔ データ不足", "momEmoji": "❔",
                "momColor": "#6b7280", "momSide": "none",
                "momRec": {"action": "none", "score": 0, "stars": 0, "label": "データ蓄積中"},
                "contrarianValue": False, "contrarianLabel": None, "contrarianScore": None}
    price = calc.get("price"); s20 = calc.get("smaShort"); s50 = calc.get("smaLong")
    sig = mom.get("signal")
    if sig == "entry":
        st = {"momState": "ENTRY", "momStateLabel": "🟢 買い場(エントリー成立)",
              "momEmoji": "🟢", "momColor": "#059669", "momSide": "buy"}
    elif sig == "exit_warning":
        st = {"momState": "EXIT", "momStateLabel": "🔴 売り場(手仕舞い検討)",
              "momEmoji": "🔴", "momColor": "#dc2626", "momSide": "sell"}
    elif s20 and s50 and price and s20 > s50 and price > s50:
        st = {"momState": "HOLD", "momStateLabel": "🟡 保有継続(新規は押し目待ち)",
              "momEmoji": "🟡", "momColor": "#16a34a", "momSide": "none"}
    else:
        st = {"momState": "WAIT", "momStateLabel": "⚪ 待機(モメンタム対象外)",
              "momEmoji": "⚪", "momColor": "#6b7280", "momSide": "none"}
    # モメンタム推奨度: 購入と売却で別軸(同時に立たない)
    if st["momSide"] == "buy":
        st["momRec"] = {"action": "buy", "score": rec.get("score", 0),
                        "stars": rec.get("stars", 1), "label": "⚡ モメンタム買い推奨"}
    elif st["momSide"] == "sell":
        flags = len([x for x in (mom.get("note") or "").split("・") if x])
        sell_score = min(90, 55 + flags * 12)   # 手仕舞いシグナルの数で緊急度を段階化
        st["momRec"] = {"action": "sell", "score": sell_score,
                        "stars": min(1 + sell_score // 20, 5), "label": "⚡ 手仕舞い(売り)推奨"}
    else:
        st["momRec"] = {"action": "none", "score": 0, "stars": 0, "label": "アクションなし(待機)"}
    # 逆張り(Value)候補: モメンタム買い未成立だが Value が 強い買い/絶対的買い場
    contra = sig != "entry" and val.get("tier") in ("strong", "absolute")
    st["contrarianValue"] = contra
    st["contrarianLabel"] = val.get("tierLabel") if contra else None
    st["contrarianScore"] = val.get("score") if contra else None
    return st

# ---------------------------------------------------------------- hold銘柄の売り管理

def hold_management(calc, vp):
    """hold銘柄: 損切りではなく (1)テーゼ崩壊見直し (2)過熱トリム のみ。"""
    out = {"thesisBreak": False, "trim": False, "note": ""}
    posL = calc.get("pctInRangeLong")
    trend200_down_days = calc.get("trend200DownDays") or 0
    below_long_bottom = (calc.get("bottomLong") is not None and
                         calc.get("price") is not None and
                         calc["price"] < calc["bottomLong"] * 0.97)
    deep_dd = (calc.get("drawdownPct") or 0) >= 25   # 高値から-25%以上も必須条件(誤検知抑制)
    if below_long_bottom and deep_dd and trend200_down_days >= vp["thesisBreakDays"]:
        out["thesisBreak"] = True
        out["note"] = "5年チャネル下抜け+200日線が下向き継続。前提(テーゼ)の再検証を推奨"
    pos1 = calc.get("pctInRange")
    r = calc.get("rsi")
    if pos1 is not None and pos1 >= vp["trimChannelPos"] and r is not None and r >= vp["trimRsi"]:
        out["trim"] = True
        out["note"] = (out["note"] + " / " if out["note"] else "") + \
            f"1年チャネル上限+RSI{r:.0f}過熱。目標比率超過分の部分利確(10-20%)検討"
    return out

# ---------------------------------------------------------------- 推奨度(rec)統合

def build_rec(calc, value, mom, policy, vp):
    """v1互換のrec + v2要素統合。"""
    score = 0
    action = "hold"
    note = ""
    if policy in ("hold", "accumulate"):
        score = value["score"]
        if value["tier"] in ("consider", "strong", "absolute"):
            action = "buy"
            note = value["tierLabel"]
    else:  # trade
        if mom.get("signal") == "entry":
            score = 70
            if calc.get("state") in ("BUY", "SUPER_BUY"):
                score += 10
            if mom.get("relStrength3m") and mom["relStrength3m"] > 5:
                score += 10
            if mom.get("volConfirmed"):
                score += 5
            action = "buy"
            note = "モメンタム・エントリー"
        elif mom.get("signal") == "exit_warning":
            score = 60
            action = "sell_check"
            note = "売り管理: " + mom.get("note", "")
        else:
            score = max(0, value["score"] - 10)  # trade銘柄も暴落時のvalue買いは可
            if value["tier"] in ("strong", "absolute"):
                action = "buy"
                note = value["tierLabel"] + "(Value)"
    score = max(0, min(100, score))
    stars = 1 + int(score // 20)
    level = "S" if score >= 90 else "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "-"

    # 見込みリターン(買い時のみ): 1年チャネル天井まで
    exp_ret = exp_days = ann = None
    plabel = None
    if action == "buy" and calc.get("top") and calc.get("price"):
        exp_ret = pct(calc["top"], calc["price"])
        if exp_ret is not None and exp_ret <= 0:
            exp_ret = None  # ブレイクアウト時などはチャネル天井が下にあり無意味
        half = calc.get("cycleHalfDays")
        if half and exp_ret is not None:
            exp_days = int(half)
            plabel = f"約{exp_days}営業日"
            if exp_days > 0:
                ann = round(exp_ret * 252 / exp_days, 1)
    return {"score": int(score), "stars": min(stars, 5), "level": level, "action": action,
            "expReturnPct": exp_ret, "expDays": exp_days, "expPeriodLabel": plabel,
            "annualizedPct": ann, "note": note}

# ---------------------------------------------------------------- 銘柄分析メイン

def analyze_instrument(meta, history, bench_closes, vix_value, cfg):
    """1銘柄の全分析。metaはconfigのエントリ。historyは古→新。"""
    s = cfg["settings"]; vp = cfg["valueParams"]; mp = cfg["momentumParams"]
    closes = [h["c"] for h in history]
    n = len(closes)
    key = meta.get("key") or meta.get("ticker")
    out = {
        "key": key, "ticker": meta.get("ticker", key), "name": meta.get("name", key),
        "sector": meta.get("sector", "その他"), "subSector": meta.get("subSector"),
        "class": meta.get("class", "stock"),
        "conviction": meta.get("conviction", 3),
        "tradePolicy": meta.get("tradePolicy", "hold"),
        "history": [{"d": h["d"], "c": h["c"]} for h in history[-160:]],
    }
    if n < 60:
        out.update({"state": "NO_DATA", "stateLabel": "データ不足", "emoji": "❔",
                    "color": "#6b7280", "side": "none", "actionable": False,
                    "price": closes[-1] if closes else None,
                    "rec": {"score": 0, "stars": 1, "level": "-", "action": "hold",
                            "expReturnPct": None, "expDays": None, "expPeriodLabel": None,
                            "annualizedPct": None, "note": "データ蓄積中"},
                    "value": {"score": 0, "tier": "none", "tierLabel": "-", "factors": []},
                    "momentum": {"signal": "none", "note": "データ不足"},
                    "holdMgmt": {"thesisBreak": False, "trim": False, "note": ""}})
        return out

    price = closes[-1]
    ch1 = channel(closes, s["lookbackDays"])
    chL = channel(closes, s["lookbackLongDays"]) if n >= s["longRangeMinDays"] else None
    sma_s = sma(closes, s["maShort"]); sma_l = sma(closes, s["maLong"]); sma_t = sma(closes, s["maTrend"])
    state, band, broke_above, broke_below, cross = base_state(
        price, ch1, sma_s, sma_l, closes, s["bandPctOfRange"], s["breakoutLookbackDays"])

    rng1 = ch1["top"] - ch1["bottom"]
    pct_in = round((price - ch1["bottom"]) / rng1 * 100, 1) if rng1 > 0 else 50.0
    # 200日線の向き & 下向き継続日数
    trend = None
    t_down_days = 0
    if sma_t is not None and n > s["maTrend"] + 25:
        prev_t = sma(closes, s["maTrend"], n - 21)
        trend = "up" if prev_t is not None and sma_t >= prev_t else "down"
        for back in range(0, 60):
            i = n - 1 - back
            cur = sma(closes, s["maTrend"], i); prv = sma(closes, s["maTrend"], i - 5)
            if cur is None or prv is None or cur >= prv:
                break
            t_down_days = back + 1
    high52 = max(closes[-252:]) if n >= 20 else max(closes)
    dd = round((1 - price / high52) * 100, 1) if high52 else None
    r = rsi(closes, vp["rsiPeriod"])
    a = atr(history, mp["atrPeriod"])
    macro = macro_driven(closes, bench_closes, vix_value,
                         vp["macroCorrWindow"], vp["macroVixThreshold"])

    calc = {
        "state": state, "price": round(price, 4),
        "top": round(ch1["top"], 4), "bottom": round(ch1["bottom"], 4),
        "band": round(band, 4), "pctInRange": pct_in,
        "distToBottomPct": pct(price, ch1["bottom"]),
        "distToTopPct": pct(ch1["top"], price),
        "smaShort": round(sma_s, 4) if sma_s else None,
        "smaLong": round(sma_l, 4) if sma_l else None,
        "smaTrend": round(sma_t, 4) if sma_t else None,
        "trend": trend, "trend200DownDays": t_down_days, "cross": cross,
        "brokeAboveRecently": broke_above, "brokeBelowRecently": broke_below,
        "trendMethod": ch1["method"], "cycleHalfDays": cycle_half_days(closes, s["lookbackDays"]),
        "rsi": r, "atr": round(a, 4) if a else None,
        "atrPct": round(a / price * 100, 2) if a else None,
        "high52w": round(high52, 4), "drawdownPct": dd, "macroDriven": macro,
    }
    if chL:
        rngL = chL["top"] - chL["bottom"]
        calc.update({
            "topLong": round(chL["top"], 4), "bottomLong": round(chL["bottom"], 4),
            "pctInRangeLong": round((price - chL["bottom"]) / rngL * 100, 1) if rngL > 0 else 50.0,
            "trendLong": "up" if chL["slope"] > 0 else "down",
            "longRangeAvailable": True,
            "longTermLow": (price - chL["bottom"]) / rngL * 100 <= s["longTermLowPct"] if rngL > 0 else False,
        })
    else:
        calc.update({"topLong": None, "bottomLong": None, "pctInRangeLong": None,
                     "trendLong": None, "longRangeAvailable": False, "longTermLow": False})

    # 逆張りフィルタ(v1): 本物の5年下降トレンド中の通常買い場のみ抑制
    counter = False
    if s.get("counterTrendFilter") and state == "BUY" and calc["trendLong"] == "down" \
            and chL and chL.get("slopePctPerDay", 0) < -0.05 and macro is not True:
        counter = True
        state = "DOWN"
    calc["counterTrend"] = counter
    calc["state"] = state

    policy = out["tradePolicy"]
    val = value_score(calc, vp)
    mom = momentum_signal(history, closes, calc, bench_closes, mp) \
        if policy == "trade" else {"signal": "none", "note": "hold銘柄"}
    hm = hold_management(calc, vp) if policy in ("hold", "accumulate") else \
        {"thesisBreak": False, "trim": False, "note": ""}
    rec = build_rec(calc, val, mom, policy, vp)
    if policy == "trade":
        mom.update(momentum_status(calc, mom, val, rec))  # モメンタム画面用の状態・推奨(購入/売却分離)

    meta_s = STATE_META.get(state, STATE_META["NEUTRAL"])
    side = meta_s["side"]
    if policy in ("hold", "accumulate") and side == "sell":
        side = "none"  # hold銘柄に売りシグナルは出さない(v1仕様)

    out.update(calc)
    out.update({
        "stateLabel": meta_s["label"], "emoji": meta_s["emoji"], "color": meta_s["color"],
        "side": side,
        "actionable": rec["action"] in ("buy", "sell_check") or hm["thesisBreak"] or hm["trim"],
        "value": val, "momentum": mom, "holdMgmt": hm, "rec": rec,
    })
    return out

# ---------------------------------------------------------------- ファンダ品質統合

def apply_quality(inst, q, vp):
    """fundamentals.jsonの品質データをValue判定に統合(取得失敗時は無補正)。
    ルール: Q<40 → Value減点-10&Tier1段階格下げ(質の悪い安さを弾く) / Q>=70 → +5
    警告(売上マイナス等)はテーゼ見直しの補足情報に追加。"""
    if not q or q.get("score") is None or inst.get("state") == "NO_DATA":
        return
    score_q = q["score"]
    v = inst["value"]
    inst["quality"] = {"score": score_q, "warnings": q.get("warnings", []),
                       "metrics": q.get("metrics", {}), "parts": q.get("parts", {})}
    delta = 0
    if score_q < 40:
        delta = -10
        v["score"] = max(0, v["score"] - 10)
        v["factors"].append(f"品質スコア{score_q}(低) -10")
        order = ["none", "watch", "consider", "strong", "absolute"]
        i = order.index(v["tier"]) if v["tier"] in order else 0
        if i > 0:
            v["tier"] = order[i - 1]
            labels = {"absolute": "🚨絶対的買い場", "strong": "🔥強い買い",
                      "consider": "🟦購入検討", "watch": "👀監視", "none": "-"}
            v["tierLabel"] = labels[v["tier"]] + "(質↓)"
    elif score_q >= 70:
        delta = 5
        v["score"] = min(100, v["score"] + 5)
        v["factors"].append(f"品質スコア{score_q}(高) +5")
    # 総合推奨(rec=★)にも同じ補正を反映(ランキングとの整合)
    rec = inst.get("rec")
    if rec and delta:
        rec["score"] = max(0, min(100, rec["score"] + delta))
        rec["stars"] = min(1 + int(rec["score"] // 20), 5)
        rec["level"] = ("S" if rec["score"] >= 90 else "A" if rec["score"] >= 80
                        else "B" if rec["score"] >= 65 else "C" if rec["score"] >= 50 else "-")
        if delta < 0 and rec.get("action") == "buy" and v["tier"] in ("none", "watch"):
            rec["action"] = "hold"
            rec["note"] = (rec.get("note") or "") + "(品質低下により見送り)"
    warns = q.get("warnings", [])
    if warns and inst.get("holdMgmt", {}).get("thesisBreak"):
        inst["holdMgmt"]["note"] += " / 財務面の警告: " + "・".join(warns)


# ---------------------------------------------------------------- プランナー用ウェイト

def planner_weights(instruments, cfg):
    """銘柄別の推奨ウェイト(株スリーブ内%)。conviction×サブセクター枠×シグナル係数。"""
    sec_t = cfg["portfolio"]["stockSectorTargets"]
    stocks = [i for i in instruments if i.get("class") == "stock" and i.get("state") != "NO_DATA"]
    by_sec = {}
    for i in stocks:
        by_sec.setdefault(i.get("subSector") or "その他モート", []).append(i)
    weights = {}
    for sec, members in by_sec.items():
        sec_pct = sec_t.get(sec, 0)
        if sec_pct <= 0 or not members:
            continue
        raw = {}
        for m in members:
            base = float(m.get("conviction", 3)) ** 1.5   # モート重視の逓増
            sig = 1.0 + (m["value"]["score"] / 200.0)      # 買い場ほど+最大50%
            if m["value"]["tier"] in ("strong", "absolute"):
                sig += 0.25
            if m.get("holdMgmt", {}).get("trim"):
                sig -= 0.3
            raw[m["ticker"]] = max(base * sig, 0.1)
        tot = sum(raw.values())
        for t, w in raw.items():
            weights[t] = round(w / tot * sec_pct, 2)
    return weights

# ---------------------------------------------------------------- イベント(IPO)

def analyze_events(cfg, instruments, today=None):
    """IPO等イベント: フェーズ・残日数・仕込みウィンドウ・恩恵銘柄の現況を統合。"""
    today = today or datetime.utcnow().date()
    inst_map = {i["ticker"]: i for i in instruments if i.get("ticker")}
    out = []
    for ev in cfg.get("events", []):
        e = {k: ev.get(k) for k in ("id", "name", "phase", "phaseLabels", "filedDate",
                                    "expectedListing", "exchange", "valuationUsd",
                                    "summary", "risks", "playbook", "lockupDays")}
        e["phaseLabel"] = ev.get("phaseLabels", {}).get(ev.get("phase"), ev.get("phase"))
        alerts = []
        try:
            listing = datetime.strptime(ev["expectedListing"], "%Y-%m-%d").date()
            days = (listing - today).days
            e["daysToListing"] = days
            if ev.get("phase") in ("rumor", "s1_filed", "s1_public"):
                if 0 < days <= 30:
                    alerts.append(f"上場観測まで約{days}日: 新規仕込みは原則停止フェーズへ")
                elif 30 < days <= 90:
                    alerts.append(f"上場観測まで約{days}日: 恩恵銘柄の仕込みウィンドウ(押し目優先)")
            if ev.get("phase") == "listed" and ev.get("lockupDays"):
                unlock = listing + timedelta(days=ev["lockupDays"])
                du = (unlock - today).days
                if 0 < du <= 21:
                    alerts.append(f"ロックアップ解除まで{du}日: 需給悪化での押し目を監視")
        except Exception:
            e["daysToListing"] = None
        bens = []
        for b in ev.get("beneficiaries", []):
            bb = dict(b)
            inst = inst_map.get(b["ticker"])
            if inst:
                bb.update({"state": inst.get("stateLabel"), "emoji": inst.get("emoji"),
                           "valueTier": inst["value"]["tierLabel"],
                           "valueScore": inst["value"]["score"],
                           "price": inst.get("price"), "inWatchlist": True})
                if inst["value"]["tier"] in ("consider", "strong", "absolute"):
                    alerts.append(f"{b['ticker']}: {inst['value']['tierLabel']}"
                                  f"(スコア{inst['value']['score']}) — イベント仕込み好機")
            else:
                bb["inWatchlist"] = False
            bens.append(bb)
        e["beneficiaries"] = bens
        e["alerts"] = alerts
        out.append(e)
    return out

# ---------------------------------------------------------------- アラート集約(プッシュ用)

def build_alerts(instruments, events, vix, cfg, prev_thesis=None, remind=False):
    """サーバー発報アラート。位置情報(取得単価)依存のものはクライアント側で生成。
    prev_thesis: 前回すでにテーゼ見直しが出ていた銘柄集合。継続中の再通知は週1(remind=True)のみ。"""
    min_score = cfg["settings"].get("minScoreToAlert", 60)
    prev_thesis = prev_thesis or set()
    alerts = []
    for i in instruments:
        if i.get("state") == "NO_DATA":
            continue
        v = i["value"]; m = i.get("momentum", {}); hm = i.get("holdMgmt", {})
        t = i["ticker"]
        if v["tier"] == "absolute":
            alerts.append({"type": "BUY_ABSOLUTE", "ticker": t, "priority": 1,
                           "title": f"🚨 {t} 絶対的買い場 (スコア{v['score']})",
                           "detail": " / ".join(v["factors"][:4])})
        elif v["tier"] == "strong" and v["score"] >= min_score:
            alerts.append({"type": "BUY_STRONG", "ticker": t, "priority": 2,
                           "title": f"🔥 {t} 強い買い (スコア{v['score']})",
                           "detail": " / ".join(v["factors"][:3])})
        elif v["tier"] == "consider" and v["score"] >= min_score:
            alerts.append({"type": "BUY_CONSIDER", "ticker": t, "priority": 3,
                           "title": f"🟦 {t} 購入検討 (スコア{v['score']})",
                           "detail": " / ".join(v["factors"][:3])})
        if m.get("signal") == "entry":
            alerts.append({"type": "MOM_ENTRY", "ticker": t, "priority": 2,
                           "title": f"🚀 {t} モメンタム・エントリー",
                           "detail": f"{m.get('note','')} 損切り目安{m.get('stopSuggest')} "
                                     f"(-{m.get('stopPct')}%) TP1 {m.get('tp1')} / TP2 {m.get('tp2')}"})
        if m.get("signal") == "exit_warning":
            alerts.append({"type": "MOM_EXIT_WARN", "ticker": t, "priority": 2,
                           "title": f"⚠️ {t} 売り管理シグナル", "detail": m.get("note", "")})
        if hm.get("thesisBreak") and (t not in prev_thesis or remind):
            label = "テーゼ見直し(新規)" if t not in prev_thesis else "テーゼ見直し(継続中・週次リマインド)"
            alerts.append({"type": "THESIS_REVIEW", "ticker": t, "priority": 2,
                           "title": f"🧐 {t} {label}", "detail": hm.get("note", "")})
        if hm.get("trim"):
            alerts.append({"type": "TRIM", "ticker": t, "priority": 3,
                           "title": f"✂️ {t} 部分利確検討", "detail": hm.get("note", "")})
    for e in events:
        for a in e.get("alerts", []):
            alerts.append({"type": "IPO_EVENT", "ticker": e["name"], "priority": 2,
                           "title": f"🔔 {e['name']}", "detail": a})
    if vix and vix.get("level"):
        alerts.append({"type": "VIX", "ticker": "VIX", "priority": 1,
                       "title": f"🌡️ VIX {vix.get('value')} ({vix['level']})",
                       "detail": vix.get("reason", "")})
    alerts.sort(key=lambda x: x["priority"])
    return alerts

# ---------------------------------------------------------------- VIX判定(v1継承)

def analyze_vix(value, prev_value, s):
    level = reason = None
    chg = pct(value, prev_value) if (value and prev_value) else None
    if value is not None:
        if value >= s["vixAbsUrgent"] or (chg is not None and abs(chg) >= s["vixUrgentPct"]):
            level = "緊急"
            reason = f"VIX {value:.1f} / 前日比{chg:+.1f}%" if chg is not None else f"VIX {value:.1f}"
        elif value >= s["vixAbsWarn"] or (chg is not None and abs(chg) >= s["vixWarnPct"]):
            level = "警戒"
            reason = f"VIX {value:.1f} / 前日比{chg:+.1f}%" if chg is not None else f"VIX {value:.1f}"
    return {"value": value, "changePct": chg, "level": level, "reason": reason}
