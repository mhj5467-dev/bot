"""
0DTE SMC Multi-Timeframe Diagnostics (RC12g).

Scope:
    SPY / QQQ / IWM / DIA / AAPL / NVDA / GLD diagnostics.

Timeframes:
    1H  = directional bias / higher-timeframe structure
    15m = zones / setup context: demand, supply, order blocks
    5m  = entry trigger context: BOS/CHoCH, fresh breakdown/breakout, liquidity sweep

Design principle:
    Diagnostics only. This module must not change score, reject trades, or affect scoring directly; Swing filters consume it explicitly.

RC12c adds LuxAlgo-style Order Block selection:
    - after BOS/CHoCH, choose the strongest parsed extreme inside the movement, not only the last opposite candle
    - de-emphasize high-volatility/news candles using a 2x mean-range volatility filter
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

_ALLOWED_SYMBOLS = {"SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD"}
# RC15f performance cache:
# - Candle fetches are cached per symbol/timeframe so Why?/dashboard refreshes do not refetch
#   DXLink candle sets repeatedly.
# - Final ICT/SMC output is intentionally not cached. The RC15f decision is price-sensitive
#   (premium/discount, zone touch, EMA alignment, retest), so caching the final output can
#   produce stale PASS/BLOCKED results.
_CANDLE_CACHE_TTL_SECONDS = {"5m": 60, "15m": 180, "1h": 900}
_CANDLE_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, float]]]] = {}


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def _normalize_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for c in candles or []:
        o = _safe_float(c.get("open"))
        h = _safe_float(c.get("high"))
        l = _safe_float(c.get("low"))
        cl = _safe_float(c.get("close"))
        t = c.get("time")
        if o is None or h is None or l is None or cl is None:
            continue
        # RC15i: reject NaN/inf before SMC/EMA calculations.
        if not all(math.isfinite(x) for x in (o, h, l, cl)):
            continue
        if h <= 0 or l <= 0 or cl <= 0:
            continue
        out.append({"time": float(t or 0), "open": o, "high": h, "low": l, "close": cl})
    out.sort(key=lambda x: x.get("time", 0))
    return out


def _fetch_dx_candles(access_token: str, symbol: str, period: str, days_back: int) -> List[Dict[str, float]]:
    """Fetch DXLink candles with a small per-timeframe cache (RC12g)."""
    sym = str(symbol or "").upper().strip()
    per = str(period or "").lower().strip()
    key = (sym, per)
    now = time.time()
    ttl = _CANDLE_CACHE_TTL_SECONDS.get(per, 60)
    cached = _CANDLE_CACHE.get(key)
    if cached and now - cached[0] <= ttl and cached[1]:
        return list(cached[1])
    from core.dxlink_client import fetch_dxlink_candles_snapshot
    candles = _normalize_candles(fetch_dxlink_candles_snapshot(access_token, sym, period=per, days_back=days_back, timeout_seconds=6.0))
    if candles:
        _CANDLE_CACHE[key] = (now, list(candles))
    return candles


def _pivots(candles: List[Dict[str, float]], length: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    if len(candles) < length * 2 + 5:
        return highs, lows
    for i in range(length, len(candles) - length):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        left = candles[i - length:i]
        right = candles[i + 1:i + length + 1]
        if h >= max(float(x["high"]) for x in left + right):
            highs.append({"idx": i, "price": h, "time": candles[i].get("time")})
        if l <= min(float(x["low"]) for x in left + right):
            lows.append({"idx": i, "price": l, "time": candles[i].get("time")})
    return highs, lows


def _atr(candles: List[Dict[str, float]], length: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        pc = float(candles[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    vals = trs[-length:] if len(trs) >= length else trs
    return sum(vals) / len(vals) if vals else 0.0


def _mean_range_until(candles: List[Dict[str, float]], idx: int, length: int = 50) -> float:
    """Mean candle range before/at idx, used to flag news/wick distortion in OB selection."""
    if not candles:
        return 0.0
    start = max(0, idx - length + 1)
    vals = [max(0.0, float(c["high"]) - float(c["low"])) for c in candles[start:idx + 1]]
    return sum(vals) / len(vals) if vals else 0.0


def _parsed_extremes(candles: List[Dict[str, float]], range_len: int = 50, multiplier: float = 2.0) -> Tuple[List[float], List[float], List[bool], List[float]]:
    """
    LuxAlgo-style OB sanitation.

    A high-volatility candle can distort Supply/Demand by creating an oversized wick.
    For such bars, invert the selectable extreme (parsedHigh=low, parsedLow=high) so the
    bar is unlikely to be chosen as the strongest OB candidate, while still preserving
    the raw candle range for diagnostics.
    """
    parsed_highs: List[float] = []
    parsed_lows: List[float] = []
    high_vol_flags: List[bool] = []
    mean_ranges: List[float] = []
    for i, c in enumerate(candles):
        h = float(c["high"]); l = float(c["low"])
        rng = max(0.0, h - l)
        avg_rng = _mean_range_until(candles, i, range_len)
        high_vol = bool(avg_rng > 0 and rng >= multiplier * avg_rng)
        parsed_highs.append(l if high_vol else h)
        parsed_lows.append(h if high_vol else l)
        high_vol_flags.append(high_vol)
        mean_ranges.append(avg_rng)
    return parsed_highs, parsed_lows, high_vol_flags, mean_ranges


def _structure_and_blocks(candles: List[Dict[str, float]], pivot_len: int, max_blocks: int = 5) -> Dict[str, Any]:
    highs, lows = _pivots(candles, pivot_len)
    high_by_idx = {int(p["idx"]): p for p in highs}
    low_by_idx = {int(p["idx"]): p for p in lows}
    atr_val = _atr(candles)
    parsed_highs, parsed_lows, high_vol_flags, mean_ranges = _parsed_extremes(candles, range_len=50, multiplier=2.0)

    last_high: Optional[Dict[str, Any]] = None
    last_low: Optional[Dict[str, Any]] = None
    bias = "neutral"
    last_event: Dict[str, Any] = {"type": "None", "bias": "neutral", "level": None, "idx": None}
    blocks: List[Dict[str, Any]] = []

    for i, bar in enumerate(candles):
        if i in high_by_idx:
            last_high = dict(high_by_idx[i])
            last_high["crossed"] = False
        if i in low_by_idx:
            last_low = dict(low_by_idx[i])
            last_low["crossed"] = False

        close = float(bar["close"])
        if last_high and not last_high.get("crossed") and close > float(last_high["price"]):
            pivot_idx = int(last_high["idx"])
            start, end = min(pivot_idx, i), max(pivot_idx, i)
            if end > start:
                # Bullish OB/Demand = LuxAlgo-style lowest parsed-low candle between pivot and bullish structure break.
                # High-volatility candles are de-emphasized via parsed_lows so news/wick bars do not dominate the zone.
                ob_idx = min(range(start, end + 1), key=lambda k: float(parsed_lows[k]))
                ob_low = float(candles[ob_idx]["low"])
                ob_high = float(candles[ob_idx]["high"])
                event_type = "Bullish BOS" if bias == "bullish" else "Bullish CHoCH"
                blocks.append({
                    "type": "demand",
                    "ob_type": "bullish_order_block",
                    "bias": "bullish",
                    "structure_event": event_type,
                    "break_level": round(float(last_high["price"]), 2),
                    "break_idx": i,
                    "idx": ob_idx,
                    "created_time": candles[ob_idx].get("time"),
                    "low": round(min(ob_low, ob_high), 2),
                    "high": round(max(ob_low, ob_high), 2),
                    "mid": round((ob_low + ob_high) / 2.0, 2),
                    "age_bars": len(candles) - 1 - ob_idx,
                    "displacement": round(close - float(last_high["price"]), 2),
                    "selection_method": "luxalgo_style_lowest_parsed_low",
                    "raw_low": round(ob_low, 2),
                    "raw_high": round(ob_high, 2),
                    "parsed_low": round(float(parsed_lows[ob_idx]), 2),
                    "parsed_high": round(float(parsed_highs[ob_idx]), 2),
                    "candidate_high_volatility": bool(high_vol_flags[ob_idx]),
                    "candidate_range": round(max(0.0, ob_high - ob_low), 3),
                    "candidate_mean_range": round(float(mean_ranges[ob_idx]), 3),
                    "volatility_filter": "range>=2x_mean_range_deemphasized" if high_vol_flags[ob_idx] else "normal",
                })
            prev_bias = bias
            bias = "bullish"
            last_event = {"type": "Bullish BOS" if prev_bias == "bullish" else "Bullish CHoCH", "bias": "bullish", "level": round(float(last_high["price"]), 2), "idx": i}
            last_high["crossed"] = True
            last_high = None

        if last_low and not last_low.get("crossed") and close < float(last_low["price"]):
            pivot_idx = int(last_low["idx"])
            start, end = min(pivot_idx, i), max(pivot_idx, i)
            if end > start:
                # Bearish OB/Supply = LuxAlgo-style highest parsed-high candle between pivot and bearish structure break.
                # High-volatility candles are de-emphasized via parsed_highs so news/wick bars do not dominate the zone.
                ob_idx = max(range(start, end + 1), key=lambda k: float(parsed_highs[k]))
                ob_low = float(candles[ob_idx]["low"])
                ob_high = float(candles[ob_idx]["high"])
                event_type = "Bearish BOS" if bias == "bearish" else "Bearish CHoCH"
                blocks.append({
                    "type": "supply",
                    "ob_type": "bearish_order_block",
                    "bias": "bearish",
                    "structure_event": event_type,
                    "break_level": round(float(last_low["price"]), 2),
                    "break_idx": i,
                    "idx": ob_idx,
                    "created_time": candles[ob_idx].get("time"),
                    "low": round(min(ob_low, ob_high), 2),
                    "high": round(max(ob_low, ob_high), 2),
                    "mid": round((ob_low + ob_high) / 2.0, 2),
                    "age_bars": len(candles) - 1 - ob_idx,
                    "displacement": round(float(last_low["price"]) - close, 2),
                    "selection_method": "luxalgo_style_highest_parsed_high",
                    "raw_low": round(ob_low, 2),
                    "raw_high": round(ob_high, 2),
                    "parsed_low": round(float(parsed_lows[ob_idx]), 2),
                    "parsed_high": round(float(parsed_highs[ob_idx]), 2),
                    "candidate_high_volatility": bool(high_vol_flags[ob_idx]),
                    "candidate_range": round(max(0.0, ob_high - ob_low), 3),
                    "candidate_mean_range": round(float(mean_ranges[ob_idx]), 3),
                    "volatility_filter": "range>=2x_mean_range_deemphasized" if high_vol_flags[ob_idx] else "normal",
                })
            prev_bias = bias
            bias = "bearish"
            last_event = {"type": "Bearish BOS" if prev_bias == "bearish" else "Bearish CHoCH", "bias": "bearish", "level": round(float(last_low["price"]), 2), "idx": i}
            last_low["crossed"] = True
            last_low = None

    # Classify blocks using simple mitigation/invalidation after creation.
    def classify(block: Dict[str, Any]) -> Dict[str, Any]:
        lo = float(block["low"])
        hi = float(block["high"])
        mid = (lo + hi) / 2.0
        touches = 0
        mitigated = False
        invalidated = False
        for bar in candles[int(block["idx"]) + 1:]:
            bh, bl, bc = float(bar["high"]), float(bar["low"]), float(bar["close"])
            if block["bias"] == "bullish":
                if bl <= hi and bh >= lo:
                    touches += 1
                    if bl <= mid:
                        mitigated = True
                if bl < lo or bc < lo:
                    invalidated = True
                    break
            else:
                if bh >= lo and bl <= hi:
                    touches += 1
                    if bh >= mid:
                        mitigated = True
                if bh > hi or bc > hi:
                    invalidated = True
                    break
        status = "invalidated" if invalidated else ("mitigated" if mitigated or touches >= 2 else ("tested" if touches else "fresh"))
        age = int(block.get("age_bars", 999) or 999)
        strength = 1
        if age <= 40: strength += 1
        if age <= 12: strength += 1
        if atr_val > 0 and abs(float(block.get("displacement", 0) or 0)) >= 0.8 * atr_val: strength += 1
        if status in ("mitigated", "invalidated"): strength -= 2
        elif status == "tested": strength -= 1
        out = dict(block)
        out.update({"status": status, "touches": touches, "strength": max(1, min(5, strength))})
        return out

    blocks = [classify(b) for b in blocks]
    demand = [b for b in blocks if b.get("bias") == "bullish" and b.get("status") in ("fresh", "tested")]
    supply = [b for b in blocks if b.get("bias") == "bearish" and b.get("status") in ("fresh", "tested")]
    demand = sorted(demand, key=lambda b: (int(b.get("strength", 0)), -int(b.get("age_bars", 999))), reverse=True)[:max_blocks]
    supply = sorted(supply, key=lambda b: (int(b.get("strength", 0)), -int(b.get("age_bars", 999))), reverse=True)[:max_blocks]

    return {
        "bias": bias,
        "last_event": last_event,
        "pivots_high": highs,
        "pivots_low": lows,
        "demand": demand,
        "supply": supply,
        "ob_selection_method": "luxalgo_style_parsed_extremes",
        "ob_volatility_filter": "range>=2x_mean_range_deemphasized",
    }


def _nearest_zone(price: float, zones: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not zones or price <= 0:
        return None
    def distance(z: Dict[str, Any]) -> float:
        lo, hi = float(z["low"]), float(z["high"])
        if lo <= price <= hi:
            return 0.0
        return min(abs(price - lo), abs(price - hi))
    z = min(zones, key=distance)
    d = distance(z)
    out = dict(z)
    out["distance"] = round(d, 2)
    out["distance_pct"] = round((d / price) * 100.0, 3)
    out["contains_price"] = bool(float(z["low"]) <= price <= float(z["high"]))
    return out


def _recent_sweep(candles: List[Dict[str, float]], highs: List[Dict[str, Any]], lows: List[Dict[str, Any]], lookback_bars: int = 8) -> Dict[str, Any]:
    """Detect a strict liquidity sweep.

    RC15f tightening:
    - A sweep is not just touching a prior swing. Price must break the prior swing by a
      small buffer, then close back inside the level.
    - Return the sweep candle index/time so MSS can be required after the sweep.
    """
    if not candles:
        return {"type": "None", "bias": "neutral", "level": None, "idx": None, "time": None}
    start = max(0, len(candles) - lookback_bars)
    prior_highs = [p for p in highs if int(p["idx"]) < start]
    prior_lows = [p for p in lows if int(p["idx"]) < start]
    last_hi = prior_highs[-1] if prior_highs else None
    last_lo = prior_lows[-1] if prior_lows else None
    atr_val = _atr(candles, 14)
    out = {"type": "None", "bias": "neutral", "level": None, "idx": None, "time": None}
    for idx in range(start, len(candles)):
        bar = candles[idx]
        close = float(bar["close"])
        # Buffer: small enough for liquid ETFs/stocks, but avoids false PASS from a 1-tick wick.
        ref_price = close if close > 0 else max(float(bar["high"]), float(bar["low"]), 1.0)
        buffer = max(ref_price * 0.0003, atr_val * 0.05 if atr_val > 0 else 0.0)
        if last_hi:
            level = float(last_hi["price"])
            if float(bar["high"]) > (level + buffer) and close < level:
                out = {
                    "type": "BSL Sweep", "bias": "bearish", "level": round(level, 2),
                    "idx": idx, "time": bar.get("time"), "break_buffer": round(buffer, 4),
                    "definition": "prior swing high broken by buffer, then candle closed back below it",
                }
        if last_lo:
            level = float(last_lo["price"])
            if float(bar["low"]) < (level - buffer) and close > level:
                out = {
                    "type": "SSL Sweep", "bias": "bullish", "level": round(level, 2),
                    "idx": idx, "time": bar.get("time"), "break_buffer": round(buffer, 4),
                    "definition": "prior swing low broken by buffer, then candle closed back above it",
                }
    return out


def _fresh_break(candles: List[Dict[str, float]], lookback: int = 5, buffer_pct: float = 0.03) -> Dict[str, Any]:
    if len(candles) < lookback + 2:
        return {"fresh_bearish_breakdown": False, "fresh_bullish_breakout": False, "fresh_break_reason": "insufficient 5m candles"}
    latest = candles[-1]
    prev = candles[-(lookback + 1):-1]
    close = float(latest["close"])
    swing_low = min(float(x["low"]) for x in prev)
    swing_high = max(float(x["high"]) for x in prev)
    buffer = close * (buffer_pct / 100.0)
    bearish = close < (swing_low - buffer)
    bullish = close > (swing_high + buffer)
    if bearish:
        reason = f"5m close {close:.2f} below recent low {swing_low:.2f} - buffer {buffer:.2f}"
    elif bullish:
        reason = f"5m close {close:.2f} above recent high {swing_high:.2f} + buffer {buffer:.2f}"
    else:
        reason = f"no 5m close break: close={close:.2f}, recent_low={swing_low:.2f}, recent_high={swing_high:.2f}, buffer={buffer:.2f}"
    return {
        "fresh_bearish_breakdown": bool(bearish),
        "fresh_bullish_breakout": bool(bullish),
        "fresh_break_reason": reason,
        "latest_5m_close": round(close, 2),
        "recent_5m_swing_low": round(swing_low, 2),
        "recent_5m_swing_high": round(swing_high, 2),
        "fresh_break_buffer_pct": buffer_pct,
        "fresh_break_buffer_points": round(buffer, 3),
    }


def _direction_from_strategy(strategy_name: Optional[str]) -> str:
    name = (strategy_name or "").lower()
    if any(k in name for k in ("put debit", "bear call", "call credit")):
        return "bearish"
    if any(k in name for k in ("call debit", "bull put", "put credit")):
        return "bullish"
    return "neutral"



def _ema_values(values: List[float], period: int) -> Optional[float]:
    """Small local EMA helper for diagnostics only."""
    vals = [float(v) for v in values if v is not None]
    if len(vals) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(vals[:period]) / period
    for v in vals[period:]:
        ema = (v * k) + (ema * (1.0 - k))
    return ema


def _ema_slope_pct(values: List[float], period: int = 20, lookback: int = 4) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    # يحتاج على الأقل period*2 + lookback شمعة حتى يكون EMA السابق موثوقاً
    if len(vals) < period * 2 + lookback:
        return None
    now_val = _ema_values(vals, period)
    prev_val = _ema_values(vals[:-lookback], period)
    if not now_val or not prev_val:
        return None
    return (now_val - prev_val) / prev_val * 100.0


def _ema_status_from_candles(candles: List[Dict[str, float]], price: float) -> Dict[str, Any]:
    """Return EMA20/EMA50 alignment diagnostics; does not affect scoring."""
    closes = []
    for c in candles or []:
        try:
            closes.append(float(c.get("close")))
        except Exception:
            pass
    if len(closes) < 50 or not price:
        return {
            "ema_available": False,
            "ema_status": f"insufficient_bars {len(closes)}/50",
            "ema20": None,
            "ema50": None,
        }
    ema20 = _ema_values(closes, 20)
    ema50 = _ema_values(closes, 50)
    if not ema20 or not ema50:
        return {"ema_available": False, "ema_status": "ema_unavailable", "ema20": ema20, "ema50": ema50}
    ema20_slope_pct = _ema_slope_pct(closes, 20, 4)
    if price > ema20 > ema50:
        status = "bullish: Price > EMA20 > EMA50"
    elif price < ema20 < ema50:
        status = "bearish: Price < EMA20 < EMA50"
    elif price > ema20 and ema20 < ema50:
        status = "bullish_pressure: Price > EMA20 but EMA20 < EMA50"
    elif price < ema20 and ema20 > ema50:
        status = "bearish_pressure: Price < EMA20 but EMA20 > EMA50"
    else:
        status = "mixed/neutral EMA alignment"
    return {
        "ema_available": True,
        "ema_status": status,
        "ema20": round(float(ema20), 4),
        "ema50": round(float(ema50), 4),
        "ema20_slope_pct_4bars": round(float(ema20_slope_pct), 4) if ema20_slope_pct is not None else None,
        "price_vs_ema20_pct": round((price - ema20) / ema20 * 100.0, 3),
        "price_vs_ema50_pct": round((price - ema50) / ema50 * 100.0, 3),
        "ema20_vs_ema50_pct": round((ema20 - ema50) / ema50 * 100.0, 3),
    }


def _dealing_range_from_pivots(highs: List[Dict[str, Any]], lows: List[Dict[str, Any]], price: float) -> Dict[str, Any]:
    ordered = sorted(
        [dict(p, kind="high") for p in highs] + [dict(p, kind="low") for p in lows],
        key=lambda x: int(x.get("idx", 0)),
    )
    if len(ordered) < 2 or price <= 0:
        return {"available": False, "reason": "insufficient_1h_pivots"}
    last_high = None
    last_low = None
    for p in reversed(ordered):
        if p.get("kind") == "high" and last_high is None:
            last_high = p
        if p.get("kind") == "low" and last_low is None:
            last_low = p
        if last_high and last_low:
            break
    if not last_high or not last_low:
        return {"available": False, "reason": "missing_high_or_low_pivot"}
    high = float(last_high["price"])
    low = float(last_low["price"])
    if high <= low:
        high, low = low, high
    mid = (high + low) / 2.0
    return {
        "available": True,
        "high": round(high, 4),
        "low": round(low, 4),
        "mid": round(mid, 4),
        "zone": "premium" if price > mid else "discount",
        "price_vs_mid_pct": round((price - mid) / mid * 100.0, 3) if mid else None,
        "last_high_idx": int(last_high.get("idx", 0)),
        "last_low_idx": int(last_low.get("idx", 0)),
    }


def _recent_fvg_with_displacement(candles: List[Dict[str, float]], direction: str,
                                  lookback: int = 14) -> Dict[str, Any]:
    if len(candles) < 20:
        return {"pass": False, "reason": f"insufficient_15m_candles {len(candles)}/20"}
    atr_val = _atr(candles, 14)
    start = max(1, len(candles) - lookback)
    found: Optional[Dict[str, Any]] = None
    for i in range(start, len(candles) - 1):
        prev = candles[i - 1]
        cur = candles[i]
        nxt = candles[i + 1]
        body = abs(float(cur["close"]) - float(cur["open"]))
        rng = max(0.0001, float(cur["high"]) - float(cur["low"]))
        displacement_ok = bool(atr_val > 0 and rng >= 0.8 * atr_val and body >= 0.45 * rng)
        if direction == "bullish":
            fvg_ok = float(nxt["low"]) > float(prev["high"])
            if fvg_ok and displacement_ok and float(cur["close"]) > float(cur["open"]):
                found = {
                    "pass": True, "direction": direction, "idx": i,
                    "low": round(float(prev["high"]), 4),
                    "high": round(float(nxt["low"]), 4),
                    "candle_range": round(rng, 4),
                    "atr14": round(atr_val, 4),
                }
        elif direction == "bearish":
            fvg_ok = float(nxt["high"]) < float(prev["low"])
            if fvg_ok and displacement_ok and float(cur["close"]) < float(cur["open"]):
                found = {
                    "pass": True, "direction": direction, "idx": i,
                    "low": round(float(nxt["high"]), 4),
                    "high": round(float(prev["low"]), 4),
                    "candle_range": round(rng, 4),
                    "atr14": round(atr_val, 4),
                }
    return found or {"pass": False, "reason": "no_recent_displacement_fvg_15m"}


def _retest_rejection(candles: List[Dict[str, float]], direction: str,
                      fvg: Dict[str, Any], zones: List[Dict[str, Any]]) -> Dict[str, Any]:
    if len(candles) < 3:
        return {"pass": False, "reason": "insufficient_15m_retest_candles"}
    latest = candles[-1]
    prev = candles[-2]
    close = float(latest["close"])
    open_ = float(latest["open"])
    high = float(latest["high"])
    low = float(latest["low"])
    fvg_low = _safe_float(fvg.get("low"))
    fvg_high = _safe_float(fvg.get("high"))
    touched_fvg = False
    if fvg_low is not None and fvg_high is not None:
        touched_fvg = bool(low <= fvg_high and high >= fvg_low)
    touched_ob = False
    touched_zone = None
    for z in zones or []:
        zlo = _safe_float(z.get("low"))
        zhi = _safe_float(z.get("high"))
        if zlo is None or zhi is None:
            continue
        if low <= zhi and high >= zlo:
            touched_ob = True
            touched_zone = {"low": round(zlo, 4), "high": round(zhi, 4), "status": z.get("status")}
            break
    if direction == "bullish":
        rejected = close > open_ and close > float(prev["close"])
    else:
        rejected = close < open_ and close < float(prev["close"])
    passed = bool((touched_fvg or touched_ob) and rejected)
    return {
        "pass": passed,
        "touched_fvg": touched_fvg,
        "touched_order_block": touched_ob,
        "touched_zone": touched_zone,
        "latest_close": round(close, 4),
        "latest_open": round(open_, 4),
        "reason": "retest_rejection_pass" if passed else "no_fvg_ob_retest_rejection",
    }


def _ict_smc_confirmation(direction: str, price: float, h1: Dict[str, Any],
                          m15: Dict[str, Any], candles15: List[Dict[str, float]]) -> Dict[str, Any]:
    details: Dict[str, Any] = {}
    checks: Dict[str, bool] = {
        "premium_discount": False,
        "liquidity_sweep": False,
        "mss": False,
        "displacement_fvg": False,
        "fvg_ob_retest": False,
    }
    if direction not in ("bullish", "bearish"):
        return {
            "score": 0,
            "pass": False,
            "confidence": "BLOCKED",
            "decision": "BLOCKED",
            "reason": "non_directional_strategy",
            "checks": checks,
            "details": details,
        }

    dr = h1.get("dealing_range") or {}
    zone = str(dr.get("zone") or "")
    checks["premium_discount"] = bool(
        (direction == "bullish" and zone == "discount") or
        (direction == "bearish" and zone == "premium")
    )
    details["premium_discount"] = dr

    sweep = (m15.get("recent_sweep") or "")
    sweep_bias = (m15.get("sweep_bias") or "")
    sweep_idx = m15.get("sweep_idx")
    checks["liquidity_sweep"] = bool(
        (direction == "bullish" and sweep_bias == "bullish" and "SSL" in sweep and sweep_idx is not None) or
        (direction == "bearish" and sweep_bias == "bearish" and "BSL" in sweep and sweep_idx is not None)
    )
    details["liquidity_sweep"] = {
        "recent_sweep": sweep,
        "sweep_bias": sweep_bias,
        "sweep_level": m15.get("sweep_level"),
        "sweep_idx": sweep_idx,
        "sweep_time": m15.get("sweep_time"),
        "break_buffer": m15.get("sweep_break_buffer"),
        "definition": "prior swing high/low broken by buffer, then candle closes back inside",
    }

    structure_bias = (m15.get("structure_bias") or m15.get("bias") or "").lower()
    last_structure = str(m15.get("last_structure") or "")
    mss_idx = m15.get("structure_idx")
    mss_after_sweep = bool(
        checks["liquidity_sweep"] and
        mss_idx is not None and sweep_idx is not None and
        int(mss_idx) > int(sweep_idx)
    )
    checks["mss"] = bool(
        structure_bias == direction and
        (("CHoCH" in last_structure) or ("BOS" in last_structure)) and
        mss_after_sweep
    )
    details["mss"] = {
        "structure_bias": structure_bias,
        "last_structure": last_structure,
        "structure_level": m15.get("structure_level"),
        "structure_idx": mss_idx,
        "mss_after_sweep": mss_after_sweep,
        "sweep_idx": sweep_idx,
        "definition": "clear BOS/CHoCH after the liquidity sweep, not before it",
    }

    fvg = _recent_fvg_with_displacement(candles15, direction)
    fvg_idx = fvg.get("idx")
    fvg_after_mss = bool(
        fvg.get("pass") and checks["mss"] and
        mss_idx is not None and fvg_idx is not None and
        int(fvg_idx) >= int(mss_idx)
    )
    checks["displacement_fvg"] = bool(fvg.get("pass") and fvg_after_mss)
    details["displacement_fvg"] = {
        **fvg,
        "fvg_after_mss": fvg_after_mss,
        "mss_idx": mss_idx,
        "definition": "valid imbalance created by displacement after MSS",
    }

    zones = m15.get("demand") if direction == "bullish" else m15.get("supply")
    retest = _retest_rejection(candles15, direction, fvg if checks["displacement_fvg"] else {}, zones or [])
    checks["fvg_ob_retest"] = bool(checks["displacement_fvg"] and retest.get("pass"))
    details["fvg_ob_retest"] = {
        **retest,
        "definition": "price returns into FVG/OB and closes away in trade direction",
    }

    score = sum(1 for v in checks.values() if v)
    if score >= 5:
        confidence = "HIGH"
        decision = "ALLOW"
    elif score >= 4:
        confidence = "MODERATE"
        decision = "ALLOW"
    elif score == 3:
        confidence = "WATCHLIST"
        decision = "WATCHLIST"
    else:
        confidence = "BLOCKED"
        decision = "BLOCKED"
    return {
        "score": int(score),
        "pass": bool(score >= 4),
        "confidence": confidence,
        "decision": decision,
        "checks": checks,
        "details": details,
        "reason": f"ict_smc_{score}_of_5_{decision.lower()}",
    }


def _swing_ema_confirmation(direction: str, m15: Dict[str, Any], price: float) -> Dict[str, Any]:
    ema20 = _safe_float(m15.get("ema20"))
    ema50 = _safe_float(m15.get("ema50"))
    slope = _safe_float(m15.get("ema20_slope_pct_4bars"))
    px = _safe_float(price) or 0.0
    details = {
        "timeframe": "15m",
        "price": round(px, 4) if px else None,
        "ema20": ema20,
        "ema50": ema50,
        "ema20_slope_pct_4bars": slope,
        "ema_status": m15.get("ema_status"),
        "definition": "15m Price/EMA20/EMA50 alignment plus EMA20 slope over last 3-5 candles",
    }
    if direction not in ("bullish", "bearish"):
        return {"pass": False, "bias": "neutral", "reason": "non_directional_strategy", "details": details}
    if not px or not ema20 or not ema50 or slope is None:
        return {"pass": False, "bias": "unavailable", "reason": "ema_15m_unavailable", "details": details}
    if direction == "bullish":
        checks = {
            "price_above_ema20": px > ema20,
            "price_above_ema50": px > ema50,
            "ema20_above_ema50": ema20 > ema50,
            "ema20_slope_rising": slope > 0,
        }
        passed = all(checks.values())
        reason = "Price > EMA20 > EMA50 and EMA20 slope rising" if passed else "call_swing_ema_alignment_failed"
        bias = "bullish" if passed else "mixed"
    else:
        checks = {
            "price_below_ema20": px < ema20,
            "price_below_ema50": px < ema50,
            "ema20_below_ema50": ema20 < ema50,
            "ema20_slope_falling": slope < 0,
        }
        passed = all(checks.values())
        reason = "Price < EMA20 < EMA50 and EMA20 slope falling" if passed else "put_swing_ema_alignment_failed"
        bias = "bearish" if passed else "mixed"
    details["checks"] = checks
    return {"pass": bool(passed), "bias": bias, "reason": reason, "details": details}


def _tf_context(label: str, timeframe: str, candles: List[Dict[str, float]], pivot_len: int, price: float) -> Dict[str, Any]:
    if not candles or len(candles) < pivot_len * 2 + 10:
        return {"available": False, "label": label, "timeframe": timeframe, "candles": len(candles or []), "reason": "insufficient candles"}
    sb = _structure_and_blocks(candles, pivot_len)
    sweep = _recent_sweep(candles, sb.get("pivots_high", []), sb.get("pivots_low", []), 8)
    demand = sb.get("demand", [])
    supply = sb.get("supply", [])
    ctx = {
        "available": True,
        "label": label,
        "timeframe": timeframe,
        "candles": len(candles),
        "bias": sb.get("bias", "neutral"),
        "last_structure": (sb.get("last_event") or {}).get("type", "None"),
        "structure_bias": (sb.get("last_event") or {}).get("bias", "neutral"),
        "structure_level": (sb.get("last_event") or {}).get("level"),
        "structure_idx": (sb.get("last_event") or {}).get("idx"),
        "demand": demand,
        "supply": supply,
        "nearest_demand": _nearest_zone(price, demand),
        "nearest_supply": _nearest_zone(price, supply),
        "recent_sweep": sweep.get("type", "None"),
        "sweep_bias": sweep.get("bias", "neutral"),
        "sweep_level": sweep.get("level"),
        "sweep_idx": sweep.get("idx"),
        "sweep_time": sweep.get("time"),
        "sweep_break_buffer": sweep.get("break_buffer"),
        "dealing_range": _dealing_range_from_pivots(sb.get("pivots_high", []), sb.get("pivots_low", []), price),
        "ob_selection_method": sb.get("ob_selection_method"),
        "ob_volatility_filter": sb.get("ob_volatility_filter"),
    }
    ctx.update(_ema_status_from_candles(candles, price))
    if timeframe == "5m":
        ctx.update(_fresh_break(candles))
    return ctx


def _verdict(direction: str, h1: Dict[str, Any], m15: Dict[str, Any], m5: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    supports: List[str] = []
    warnings: List[str] = []
    if direction not in ("bullish", "bearish"):
        return "neutral_directional_context", supports, warnings

    h1_bias = h1.get("bias", "neutral")
    m15_bias = m15.get("bias", "neutral")
    m5_bias = m5.get("bias", "neutral")
    if h1_bias == direction:
        supports.append(f"1H bias {h1_bias} supports {direction}")
    elif h1_bias in ("bullish", "bearish"):
        warnings.append(f"1H bias {h1_bias} conflicts with {direction}")

    if m15_bias == direction:
        supports.append(f"15m structure {m15_bias} supports setup")
    elif m15_bias in ("bullish", "bearish"):
        warnings.append(f"15m structure {m15_bias} conflicts with setup")

    if m5_bias == direction:
        supports.append(f"5m structure {m5_bias} supports trigger")
    elif m5_bias in ("bullish", "bearish"):
        warnings.append(f"5m structure {m5_bias} conflicts with trigger")

    if direction == "bearish":
        if m15.get("nearest_supply") and (m15["nearest_supply"].get("contains_price") or (m15["nearest_supply"].get("distance_pct") is not None and m15["nearest_supply"].get("distance_pct") <= 0.70)):
            supports.append("15m price near supply/bearish OB supports bearish setup")
        if m15.get("nearest_demand") and (m15["nearest_demand"].get("contains_price") or (m15["nearest_demand"].get("distance_pct") is not None and m15["nearest_demand"].get("distance_pct") <= 0.70)):
            warnings.append("15m price near demand/bullish OB warns against bearish setup")
        if m5.get("fresh_bearish_breakdown"):
            supports.append("5m fresh breakdown confirmed")
        if m5.get("sweep_bias") == "bullish":
            warnings.append("5m SSL sweep may warn of bullish reversal against bearish setup")
        if m5.get("sweep_bias") == "bearish":
            supports.append("5m BSL sweep supports bearish reversal")
    else:
        if m15.get("nearest_demand") and (m15["nearest_demand"].get("contains_price") or (m15["nearest_demand"].get("distance_pct") is not None and m15["nearest_demand"].get("distance_pct") <= 0.70)):
            supports.append("15m price near demand/bullish OB supports bullish setup")
        if m15.get("nearest_supply") and (m15["nearest_supply"].get("contains_price") or (m15["nearest_supply"].get("distance_pct") is not None and m15["nearest_supply"].get("distance_pct") <= 0.70)):
            warnings.append("15m price near supply/bearish OB warns against bullish setup")
        if m5.get("fresh_bullish_breakout"):
            supports.append("5m fresh breakout confirmed")
        if m5.get("sweep_bias") == "bearish":
            warnings.append("5m BSL sweep may warn of bearish reversal against bullish setup")
        if m5.get("sweep_bias") == "bullish":
            supports.append("5m SSL sweep supports bullish reversal")

    if warnings and not supports:
        verdict = "warns_trade"
    elif supports and not warnings:
        verdict = "supports_trade"
    elif supports and warnings:
        verdict = "mixed_context"
    else:
        verdict = "neutral_context"
    return verdict, supports, warnings


def analyze_0dte_smc_mtf(
    symbol: str,
    price: Optional[float],
    strategy_name: Optional[str] = None,
    access_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Return 1H/15m/5m SMC diagnostics for supported Swing/0DTE symbols."""
    sym = str(symbol or "").upper().strip()
    px = _safe_float(price) or 0.0
    direction = _direction_from_strategy(strategy_name)
    if sym not in _ALLOWED_SYMBOLS:
        return {
            "available": False, "enabled": False, "diagnostics_only": True,
            "reason": "not_applicable_symbol", "symbol": sym,
            "ict_smc_score": 0, "ict_smc_pass": False, "ict_smc_confidence": "BLOCKED",
            "ict_smc_details": {"score": 0, "pass": False, "reason": "insufficient_ict_smc_data"},
            "ema_alignment_pass": False,
            "ema_details": {"pass": False, "reason": "insufficient_ict_smc_data"},
        }
    if px <= 0:
        return {
            "available": False, "enabled": True, "diagnostics_only": True,
            "reason": "missing_price", "symbol": sym,
            "ict_smc_score": 0, "ict_smc_pass": False, "ict_smc_confidence": "BLOCKED",
            "ict_smc_details": {"score": 0, "pass": False, "reason": "insufficient_ict_smc_data"},
            "ema_alignment_pass": False,
            "ema_details": {"pass": False, "reason": "insufficient_ict_smc_data"},
        }

    # RC15f: do not cache final output; the Swing ICT/SMC+EMA decision is price-sensitive.
    try:
        tok = access_token
        if not tok:
            from core.analyzer import _get_access_token
            tok = _get_access_token()
        c1h = _fetch_dx_candles(tok, sym, "1h", 30)
        c15 = _fetch_dx_candles(tok, sym, "15m", 10)
        c5 = _fetch_dx_candles(tok, sym, "5m", 4)
        h1 = _tf_context("1H Bias", "1h", c1h, 5, px)
        m15 = _tf_context("15m Zones", "15m", c15, 5, px)
        m5 = _tf_context("5m Trigger", "5m", c5, 5, px)
        verdict, supports, warnings = _verdict(direction, h1, m15, m5)
        ict_smc = _ict_smc_confirmation(direction, px, h1, m15, c15)
        ema_conf = _swing_ema_confirmation(direction, m15, px)
        out: Dict[str, Any] = {
            "available": bool(h1.get("available") or m15.get("available") or m5.get("available")),
            "enabled": True,
            "diagnostics_only": True,
            "scope": "SPY/QQQ/IWM/DIA/AAPL/NVDA/GLD Swing/0DTE diagnostics",
            "source": "DXLink/Tastytrade candles",
            "symbol": sym,
            "price": round(px, 4),
            "direction": direction,
            "timeframes": "1H/15m/5m",
            "h1": h1,
            "m15": m15,
            "m5": m5,
            "bias_1h": h1.get("bias", "neutral"),
            "structure_15m": m15.get("last_structure", "None"),
            "trigger_5m": m5.get("last_structure", "None"),
            "nearest_15m_demand": m15.get("nearest_demand"),
            "nearest_15m_supply": m15.get("nearest_supply"),
            "liquidity_sweep_5m": m5.get("recent_sweep", "None"),
            "fresh_bearish_breakdown_5m": m5.get("fresh_bearish_breakdown"),
            "fresh_bullish_breakout_5m": m5.get("fresh_bullish_breakout"),
            "fresh_break_reason_5m": m5.get("fresh_break_reason"),
            "verdict": verdict,
            "supports": supports,
            "warnings": warnings,
            "score_adjustment": 0,
            "hard_reject": False,
            "ict_smc_score": ict_smc.get("score", 0),
            "ict_smc_pass": ict_smc.get("pass", False),
            "ict_smc_confidence": ict_smc.get("confidence", "BLOCKED"),
            "ict_smc_details": ict_smc,
            "ema_alignment_pass": ema_conf.get("pass", False),
            "ema_details": ema_conf,
        }
    except Exception as exc:
        out = {
            "available": False,
            "enabled": True,
            "diagnostics_only": True,
            "scope": "SPY/QQQ/IWM/DIA/AAPL/NVDA/GLD Swing/0DTE diagnostics",
            "source": "DXLink/Tastytrade candles",
            "symbol": sym,
            "price": round(px, 4),
            "direction": direction,
            "reason": f"smc_mtf_diagnostics_error:{type(exc).__name__}: {str(exc)[:160]}",
            "score_adjustment": 0,
            "hard_reject": False,
            "ict_smc_score": 0,
            "ict_smc_pass": False,
            "ict_smc_confidence": "BLOCKED",
            "ict_smc_details": {"score": 0, "pass": False, "reason": "diagnostics_error"},
            "ema_alignment_pass": False,
            # RC15i: network/diagnostics exceptions mean EMA is unavailable,
            # not a true bearish/bullish EMA alignment failure.
            "ema_details": {"pass": False, "reason": "diagnostics_error", "bias": "unavailable"},
        }

    out["cache_status"] = "final_output_not_cached_rc15f"
    return out
