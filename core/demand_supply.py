"""
Order Block / Demand-Supply scoring layer for SPX Options Paper Trading v3.12.

Design principle:
    Order Blocks are context only. They never hard-reject a trade.
    This module upgrades the previous basic Pivot+Impulse Demand/Supply logic into
    BOS/CHoCH-driven Order Block scoring, with mitigation/invalidation handling.

Profiles:
    0DTE  : HTF=1H,  LTF=15m
    Swing : HTF=1D,  LTF=4H

Notes:
    - This is a proprietary Python implementation inspired conceptually by SMC
      market-structure logic. It does not copy Pine Script implementation.
    - Positive score is given only to active/fresh or lightly-tested blocks.
    - Mitigated/invalidated blocks do not provide positive score.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

try:
    from core.smc_engine import _load_candles_for_tf, _pivots
except Exception:  # pragma: no cover
    _load_candles_for_tf = None
    _pivots = None

def get_yahoo_ohlc_status():
    return {"ok": False, "state": "DISABLED", "yahoo_used": False}


def _normalize_dx_candles(candles: List[Dict[str, Any]]) -> List[Dict[str, float]]:
    out: List[Dict[str, float]] = []
    for c in candles or []:
        try:
            o = float(c.get("open")); h = float(c.get("high")); l = float(c.get("low")); cl = float(c.get("close"))
        except Exception:
            continue
        # RC15i: reject NaN/inf before Demand/Supply and OB calculations.
        # In Python, float("nan") <= 0 is False, so the old guard allowed bad candles
        # to pass and produced empty/neutral zones silently.
        if not all(math.isfinite(x) for x in (o, h, l, cl)):
            continue
        if h <= 0 or l <= 0 or cl <= 0:
            continue
        t = c.get("time") or c.get("datetime") or 0
        try:
            t = float(t)
        except Exception:
            t = 0.0
        out.append({"time": t, "open": o, "high": h, "low": l, "close": cl})
    out.sort(key=lambda x: x.get("time", 0))
    return out


def _load_dxlink_candles_for_tf(symbol: str, interval: str, days_back: int) -> Tuple[List[Dict[str, float]], Dict[str, Any]]:
    """RC12d: Primary candle source for SPY/QQQ/IWM 0DTE Demand/Supply diagnostics."""
    try:
        from core.analyzer import _get_access_token
        from core.dxlink_client import fetch_dxlink_candles_snapshot
        tok = _get_access_token()
        raw = fetch_dxlink_candles_snapshot(tok, symbol, period=interval, days_back=days_back, timeout_seconds=14.0)
        candles = _normalize_dx_candles(raw)
        return candles, {"ok": bool(candles), "state": "OK" if candles else "EMPTY", "source": "dxlink", "candles": len(candles)}
    except Exception as exc:
        return [], {"ok": False, "state": f"FAILED:{type(exc).__name__}: {str(exc)[:120]}", "source": "dxlink"}


def _load_orderblock_candles(symbol: str, mode: str, period: str, interval: str) -> Tuple[List[Dict[str, float]], str, Dict[str, Any]]:
    """
    Candle source policy:
      - Swing symbols SPY/QQQ/IWM/DIA/AAPL/NVDA/GLD: DXLink for 1H/15m/5m/4H.
      - 0DTE ETF diagnostics SPY/QQQ/IWM: DXLink.
      - Other profiles/symbols: legacy Yahoo fallback.
    """
    sym = str(symbol or "").upper().strip()
    mode_u = str(mode or "0DTE").upper()
    swing_symbols = {"SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD"}
    # جميع الرموز تستخدم DXLink فقط — لا Yahoo fallback
    iv = interval.lower()
    if iv in ("4h", "240m"):
        days_back = 120
    elif iv in ("1h", "60m"):
        days_back = 45
    elif iv in ("15m",):
        days_back = 12
    else:
        days_back = 5
    candles, status = _load_dxlink_candles_for_tf(sym, interval, days_back)
    if candles:
        return candles, "dxlink", {**status, "yahoo_used": False, "dxlink_candles_ok": True}
    return [], "dxlink_unavailable", {"ok": False, "state": "dxlink_unavailable",
                                      "yahoo_used": False, "dxlink_candles_ok": False,
                                      "diagnostics_only": True}


MAX_OB_SCORE_ADJUSTMENT = 15


def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _strategy_direction(strategy_name: str) -> str:
    """Map option strategy names to directional intent."""
    name = (strategy_name or "").lower()
    if any(k in name for k in ("bull put", "put credit", "call debit")):
        return "bullish"
    if any(k in name for k in ("bear call", "call credit", "put debit")):
        return "bearish"
    if "iron condor" in name:
        return "neutral"
    return "neutral"


def _atr(candles: List[Dict[str, float]], lookback: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs: List[float] = []
    subset = candles[-(lookback + 1):]
    for i in range(1, len(subset)):
        cur = subset[i]
        prev = subset[i - 1]
        tr = max(
            float(cur["high"]) - float(cur["low"]),
            abs(float(cur["high"]) - float(prev["close"])),
            abs(float(cur["low"]) - float(prev["close"])),
        )
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0.0


def _true_ranges(candles: List[Dict[str, float]]) -> List[float]:
    if len(candles) < 2:
        return []
    out: List[float] = []
    for i in range(1, len(candles)):
        cur = candles[i]
        prev = candles[i - 1]
        out.append(max(
            float(cur["high"]) - float(cur["low"]),
            abs(float(cur["high"]) - float(prev["close"])),
            abs(float(cur["low"]) - float(prev["close"])),
        ))
    return out


def _parsed_high_low(candles: List[Dict[str, float]]) -> Tuple[List[float], List[float]]:
    """
    Parse high/low values with a volatility guard.

    For unusually wide bars, wicks are de-emphasised so one abnormal candle is less
    likely to create an over-important Order Block.
    """
    trs = _true_ranges(candles)
    if not candles:
        return [], []
    mean_range = (sum(trs) / len(trs)) if trs else 0.0
    atr200 = _atr(candles, min(200, max(2, len(candles) - 1)))
    vol = atr200 or mean_range or 0.0
    parsed_highs: List[float] = []
    parsed_lows: List[float] = []
    for bar in candles:
        high = float(bar["high"])
        low = float(bar["low"])
        high_vol_bar = bool(vol > 0 and (high - low) >= 2.0 * vol)
        parsed_highs.append(low if high_vol_bar else high)
        parsed_lows.append(high if high_vol_bar else low)
    return parsed_highs, parsed_lows


def _block_status(block: Dict[str, Any], candles: List[Dict[str, float]], mitigation_source: str = "highlow") -> Dict[str, Any]:
    """
    Classify an OB as fresh/tested/mitigated/invalidated after its creation.

    Bullish OB:
        - tested: later price trades into the block.
        - mitigated: price reaches/passes midpoint or repeated tests occur.
        - invalidated: price breaks below the block low.
    Bearish OB:
        - tested: later price trades into the block.
        - mitigated: price reaches/passes midpoint or repeated tests occur.
        - invalidated: price breaks above the block high.
    """
    bias = block.get("bias")
    low = float(block.get("low", 0.0))
    high = float(block.get("high", 0.0))
    mid = (low + high) / 2.0
    created_idx = int(block.get("created_idx", 0) or 0)
    future = candles[created_idx + 1:]

    touches = 0
    first_touch_idx: Optional[int] = None
    mitigated = False
    invalidated = False
    invalidation_idx: Optional[int] = None

    for offset, bar in enumerate(future, start=created_idx + 1):
        b_high = float(bar["high"])
        b_low = float(bar["low"])
        b_close = float(bar["close"])

        if bias == "bullish":
            touched = b_low <= high and b_high >= low
            if touched:
                touches += 1
                if first_touch_idx is None:
                    first_touch_idx = offset
                if b_low <= mid:
                    mitigated = True
            if mitigation_source == "close":
                invalidated = b_close < low
            else:
                invalidated = b_low < low or b_close < low
        elif bias == "bearish":
            touched = b_high >= low and b_low <= high
            if touched:
                touches += 1
                if first_touch_idx is None:
                    first_touch_idx = offset
                if b_high >= mid:
                    mitigated = True
            if mitigation_source == "close":
                invalidated = b_close > high
            else:
                invalidated = b_high > high or b_close > high
        else:
            touched = False

        if touches >= 2:
            mitigated = True
        if invalidated:
            invalidation_idx = offset
            break

    if invalidated:
        status = "invalidated"
    elif mitigated:
        status = "mitigated"
    elif touches > 0:
        status = "tested"
    else:
        status = "fresh"

    out = dict(block)
    out.update({
        "status": status,
        "touches": touches,
        "mitigated": bool(mitigated),
        "invalidated": bool(invalidated),
        "first_touch_idx": first_touch_idx,
        "invalidation_idx": invalidation_idx,
    })
    return out


def _block_strength(block: Dict[str, Any], candles: List[Dict[str, float]], atr_val: float) -> int:
    """Compact 1..5 strength rating using recency, break displacement and status."""
    age = max(0, len(candles) - 1 - int(block.get("created_idx", 0) or 0))
    impulse = abs(float(block.get("displacement", 0.0) or 0.0))
    status = block.get("status", "fresh")
    strength = 1
    if age <= 40:
        strength += 1
    if age <= 12:
        strength += 1
    if atr_val > 0 and impulse >= 0.8 * atr_val:
        strength += 1
    if atr_val > 0 and impulse >= 1.5 * atr_val:
        strength += 1
    if status == "tested":
        strength -= 1
    elif status in ("mitigated", "invalidated"):
        strength -= 2
    return max(1, min(5, strength))


def _detect_order_blocks_for_tf(
    candles: List[Dict[str, float]],
    pivot_len: int,
    max_blocks: int = 5,
    mitigation_source: str = "highlow",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Detect BOS/CHoCH-driven Order Blocks.

    Simplified implementation:
      1. Confirm pivot highs/lows.
      2. A close above the last pivot high = Bullish BOS/CHoCH.
      3. A close below the last pivot low  = Bearish BOS/CHoCH.
      4. Bullish OB = lowest parsed-low bar between pivot and break.
      5. Bearish OB = highest parsed-high bar between pivot and break.
      6. Classify each block as fresh/tested/mitigated/invalidated.
    """
    if not candles or len(candles) < pivot_len * 2 + 10 or not _pivots:
        return {"bullish": [], "bearish": [], "active_bullish": [], "active_bearish": []}

    highs, lows = _pivots(candles, pivot_len)
    high_by_idx = {int(p["idx"]): p for p in highs}
    low_by_idx = {int(p["idx"]): p for p in lows}
    parsed_highs, parsed_lows = _parsed_high_low(candles)
    atr_val = _atr(candles)

    last_high: Optional[Dict[str, Any]] = None
    last_low: Optional[Dict[str, Any]] = None
    bias = "neutral"
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
                window = parsed_lows[start:end + 1]
                rel_idx = min(range(len(window)), key=lambda k: window[k])
                ob_idx = start + rel_idx
                ob_high = float(parsed_highs[ob_idx])
                ob_low = float(parsed_lows[ob_idx])
                if ob_high < ob_low:
                    ob_high, ob_low = ob_low, ob_high
                event_type = "Bullish BOS" if bias == "bullish" else "Bullish CHoCH"
                displacement = close - float(last_high["price"])
                block = {
                    "type": "demand",
                    "ob_type": "bullish_order_block",
                    "bias": "bullish",
                    "structure_event": event_type,
                    "break_level": round(float(last_high["price"]), 2),
                    "break_idx": i,
                    "idx": ob_idx,
                    "created_idx": ob_idx,
                    "created_time": candles[ob_idx].get("time"),
                    "low": round(ob_low, 2),
                    "high": round(ob_high, 2),
                    "mid": round((ob_low + ob_high) / 2.0, 2),
                    "age_bars": len(candles) - 1 - ob_idx,
                    "displacement": round(displacement, 2),
                }
                block = _block_status(block, candles, mitigation_source)
                block["strength"] = _block_strength(block, candles, atr_val)
                blocks.append(block)
            bias = "bullish"
            last_high["crossed"] = True
            last_high = None

        if last_low and not last_low.get("crossed") and close < float(last_low["price"]):
            pivot_idx = int(last_low["idx"])
            start, end = min(pivot_idx, i), max(pivot_idx, i)
            if end > start:
                window = parsed_highs[start:end + 1]
                rel_idx = max(range(len(window)), key=lambda k: window[k])
                ob_idx = start + rel_idx
                ob_high = float(parsed_highs[ob_idx])
                ob_low = float(parsed_lows[ob_idx])
                if ob_high < ob_low:
                    ob_high, ob_low = ob_low, ob_high
                event_type = "Bearish BOS" if bias == "bearish" else "Bearish CHoCH"
                displacement = float(last_low["price"]) - close
                block = {
                    "type": "supply",
                    "ob_type": "bearish_order_block",
                    "bias": "bearish",
                    "structure_event": event_type,
                    "break_level": round(float(last_low["price"]), 2),
                    "break_idx": i,
                    "idx": ob_idx,
                    "created_idx": ob_idx,
                    "created_time": candles[ob_idx].get("time"),
                    "low": round(ob_low, 2),
                    "high": round(ob_high, 2),
                    "mid": round((ob_low + ob_high) / 2.0, 2),
                    "age_bars": len(candles) - 1 - ob_idx,
                    "displacement": round(displacement, 2),
                }
                block = _block_status(block, candles, mitigation_source)
                block["strength"] = _block_strength(block, candles, atr_val)
                blocks.append(block)
            bias = "bearish"
            last_low["crossed"] = True
            last_low = None

    bullish = [b for b in blocks if b.get("bias") == "bullish"]
    bearish = [b for b in blocks if b.get("bias") == "bearish"]

    def sort_key(b: Dict[str, Any]) -> Tuple[int, int, int]:
        active_rank = 1 if b.get("status") in ("fresh", "tested") else 0
        return (active_rank, int(b.get("strength", 0) or 0), -int(b.get("age_bars", 999) or 999))

    bullish = sorted(bullish, key=sort_key, reverse=True)[:max_blocks]
    bearish = sorted(bearish, key=sort_key, reverse=True)[:max_blocks]
    active_bullish = [b for b in bullish if b.get("status") in ("fresh", "tested")]
    active_bearish = [b for b in bearish if b.get("status") in ("fresh", "tested")]

    return {
        "bullish": bullish,
        "bearish": bearish,
        "active_bullish": active_bullish,
        "active_bearish": active_bearish,
    }


def _nearest_zone(price: float, zones: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not zones:
        return None

    def dist(z: Dict[str, Any]) -> float:
        lo, hi = float(z["low"]), float(z["high"])
        if lo <= price <= hi:
            return 0.0
        return min(abs(price - lo), abs(price - hi))

    z = min(zones, key=dist)
    out = dict(z)
    out["distance"] = round(dist(z), 2)
    out["distance_pct"] = round((dist(z) / price) * 100.0, 3) if price else None
    out["contains_price"] = bool(float(z["low"]) <= price <= float(z["high"]))
    return out


def _profile(mode: str) -> Tuple[str, Tuple[str, str, str, int], Tuple[str, str, str, int]]:
    mode_u = (mode or "0DTE").upper()
    if mode_u == "SWING":
        return (
            "OrderBlock_Swing_MTF_Profile",
            ("HTF", "1y", "1d", 20),
            ("LTF", "120d", "4h", 12),
        )
    return (
        "OrderBlock_0DTE_MTF_Profile",
        ("HTF", "30d", "1h", 12),
        ("LTF", "10d", "15m", 10),
    )


def analyze_demand_supply(symbol: str, mode: str = "0DTE", price: Optional[float] = None) -> Dict[str, Any]:
    """Return HTF/LTF Order Block context under the existing demand_supply key."""
    if not _load_candles_for_tf:
        return {"available": False, "score": 0, "reason": "OHLC loader unavailable"}
    px = _to_float(price)
    profile_name, htf_spec, ltf_spec = _profile(mode)
    contexts: Dict[str, Dict[str, Any]] = {}

    for label, period, interval, pivot_len in (htf_spec, ltf_spec):
        candles, candle_source, candle_status = _load_orderblock_candles(symbol, mode, period, interval)
        if not candles or len(candles) < pivot_len * 2 + 10:
            ys = get_yahoo_ohlc_status()
            reason = "insufficient OHLC data"
            source = f"{candle_source}_empty"
            if candle_status.get("ok") is False:
                reason = f"{candle_source.upper()} OHLC unavailable: {candle_status.get('state')}"
                source = f"{candle_source}_failed"
            contexts[label.lower()] = {
                "available": False,
                "label": label,
                "timeframe": interval,
                "reason": reason,
                "source": source,
                "candle_status": candle_status,
                "yahoo_status": ys,
                "candles": len(candles or []),
                "demand": [],
                "supply": [],
                "bullish_order_blocks": [],
                "bearish_order_blocks": [],
                "active_bullish_order_blocks": [],
                "active_bearish_order_blocks": [],
            }
            continue
        blocks = _detect_order_blocks_for_tf(candles, pivot_len)
        current_price = px if px is not None else float(candles[-1]["close"])
        active_bullish = blocks["active_bullish"]
        active_bearish = blocks["active_bearish"]
        bullish_all = blocks["bullish"]
        bearish_all = blocks["bearish"]
        contexts[label.lower()] = {
            "available": True,
            "label": label,
            "timeframe": interval,
            "source": candle_source,
            "candle_status": candle_status,
            "candles": len(candles),
            "atr": round(_atr(candles), 2),
            "engine": "BOS/CHoCH Order Block",
            # Backward-compatible names for the UI and older logs:
            "demand": active_bullish,
            "supply": active_bearish,
            "nearest_demand": _nearest_zone(current_price, active_bullish),
            "nearest_supply": _nearest_zone(current_price, active_bearish),
            # Explicit v3.12 names:
            "bullish_order_blocks": bullish_all,
            "bearish_order_blocks": bearish_all,
            "active_bullish_order_blocks": active_bullish,
            "active_bearish_order_blocks": active_bearish,
            "nearest_bullish_order_block": _nearest_zone(current_price, active_bullish),
            "nearest_bearish_order_block": _nearest_zone(current_price, active_bearish),
        }

    available = bool(contexts.get("htf", {}).get("available") or contexts.get("ltf", {}).get("available"))
    srcs = {contexts.get("htf", {}).get("source"), contexts.get("ltf", {}).get("source")}
    if "dxlink" in srcs:
        top_source = "dxlink"
    elif available:
        top_source = "yahoo"
    elif any(str(x or "").endswith("_failed") for x in srcs):
        top_source = ",".join(sorted(str(x) for x in srcs if x))
    else:
        top_source = "neutral_fallback"
    return {
        "available": available,
        "profile": profile_name,
        "source": top_source,
        "candle_source_policy": "DXLink for SPY/QQQ/IWM 0DTE; Yahoo fallback otherwise",
        "yahoo_status": get_yahoo_ohlc_status(),
        "engine": "BOS/CHoCH Order Block Scoring",
        "price": px,
        "score": 0,
        "htf": contexts.get("htf", {}),
        "ltf": contexts.get("ltf", {}),
    }


def _block_support_bonus(block: Optional[Dict[str, Any]], weight: int) -> int:
    if not block:
        return 0
    status = block.get("status")
    if status == "fresh":
        return weight + (1 if int(block.get("strength", 1) or 1) >= 4 else 0)
    if status == "tested":
        return max(1, weight - 2)
    return 0


def _block_negative_penalty(block: Optional[Dict[str, Any]], weight: int) -> int:
    if not block:
        return 0
    status = block.get("status")
    if status == "invalidated":
        return weight
    if status == "mitigated":
        return max(1, weight - 2)
    return 0


def _zone_alignment_score(
    direction: str,
    htf: Dict[str, Any],
    ltf: Dict[str, Any],
    price: float,
    reasons: List[str],
    warnings: List[str],
) -> int:
    """Convert Order Block position/status into bounded non-blocking score adjustment."""
    adj = 0

    # Thresholds are percentage distance from current price.
    near_pct = 0.45 if price >= 1000 else 0.70

    htf_bull = htf.get("nearest_bullish_order_block") or htf.get("nearest_demand")
    htf_bear = htf.get("nearest_bearish_order_block") or htf.get("nearest_supply")
    ltf_bull = ltf.get("nearest_bullish_order_block") or ltf.get("nearest_demand")
    ltf_bear = ltf.get("nearest_bearish_order_block") or ltf.get("nearest_supply")

    def add_for_block(tf_label: str, block: Optional[Dict[str, Any]], wanted: str, weight: int) -> int:
        if not block:
            return 0
        status = block.get("status", "unknown")
        if status not in ("fresh", "tested"):
            warnings.append(
                f"OrderBlock: {tf_label} {wanted} block {block.get('low')}–{block.get('high')} is {status}; no positive score"
            )
            return 0
        dist_pct = block.get("distance_pct")
        near = block.get("contains_price") or (dist_pct is not None and dist_pct <= near_pct)
        if not near:
            return 0
        bonus = _block_support_bonus(block, weight)
        reasons.append(
            f"OrderBlock: {tf_label} {wanted} {status} block {block.get('low')}–{block.get('high')} "
            f"from {block.get('structure_event')} supports setup (+{bonus})"
        )
        return bonus

    def penalty_if_conflict(tf_label: str, block: Optional[Dict[str, Any]], kind: str, weight: int) -> int:
        if not block:
            return 0
        dist_pct = block.get("distance_pct")
        near = block.get("contains_price") or (dist_pct is not None and dist_pct <= near_pct)
        status = block.get("status", "unknown")
        if status in ("mitigated", "invalidated"):
            return 0
        if near:
            warnings.append(f"OrderBlock: setup is close to opposing {tf_label} {kind} block (-{weight})")
            return -weight
        return 0

    def penalty_invalidated_support(tf_label: str, block: Optional[Dict[str, Any]], wanted: str, weight: int) -> int:
        if not block:
            return 0
        status = block.get("status")
        if status == "invalidated":
            warnings.append(f"OrderBlock: {tf_label} {wanted} block is invalidated; weak setup (-{weight})")
            return -weight
        if status == "mitigated":
            warnings.append(f"OrderBlock: {tf_label} {wanted} block is mitigated/consumed; no support")
        return 0

    if direction == "bullish":
        adj += add_for_block("HTF", htf_bull, "bullish/demand", 8)
        adj += add_for_block("LTF", ltf_bull, "bullish/demand", 5)
        adj += penalty_invalidated_support("HTF", htf_bull, "bullish/demand", 5)
        adj += penalty_invalidated_support("LTF", ltf_bull, "bullish/demand", 3)
        adj += penalty_if_conflict("HTF", htf_bear, "bearish/supply", 5)
    elif direction == "bearish":
        adj += add_for_block("HTF", htf_bear, "bearish/supply", 8)
        adj += add_for_block("LTF", ltf_bear, "bearish/supply", 5)
        adj += penalty_invalidated_support("HTF", htf_bear, "bearish/supply", 5)
        adj += penalty_invalidated_support("LTF", ltf_bear, "bearish/supply", 3)
        adj += penalty_if_conflict("HTF", htf_bull, "bullish/demand", 5)
    elif direction == "neutral":
        demand = htf_bull or ltf_bull
        supply = htf_bear or ltf_bear
        if demand and supply:
            demand_active = demand.get("status") in ("fresh", "tested")
            supply_active = supply.get("status") in ("fresh", "tested")
            if demand_active and supply_active and float(demand["high"]) < price < float(supply["low"]):
                reasons.append("OrderBlock: price is balanced between active bullish and bearish blocks; supports Iron Condor (+6)")
                adj += 6
            elif demand.get("contains_price"):
                warnings.append("OrderBlock: Iron Condor opened inside bullish demand block; bounce risk (-4)")
                adj -= 4
            elif supply.get("contains_price"):
                warnings.append("OrderBlock: Iron Condor opened inside bearish supply block; rejection risk (-4)")
                adj -= 4

    return int(max(-MAX_OB_SCORE_ADJUSTMENT, min(MAX_OB_SCORE_ADJUSTMENT, adj)))


def apply_demand_supply_to_strategy(
    strategy: Optional[Dict[str, Any]],
    demand_supply: Dict[str, Any],
    price: Optional[float] = None,
) -> Dict[str, Any]:
    """Attach Order Block context and adjust score. Never rejects by itself."""
    if not strategy:
        return {}
    ds = demand_supply or {"available": False, "score": 0}
    name = strategy.get("strategy", "")
    direction = _strategy_direction(name)
    px = _to_float(price) or _to_float(ds.get("price"))
    if px is None or px <= 0:
        ds["score"] = 0
        strategy["demand_supply"] = ds
        return strategy

    reasons: List[str] = []
    warnings: List[str] = []
    adj = 0
    if ds.get("available"):
        adj = _zone_alignment_score(direction, ds.get("htf", {}), ds.get("ltf", {}), px, reasons, warnings)
    else:
        warnings.append("OrderBlock: unavailable; no score adjustment")

    old_score = strategy.get("score", 0) or 0
    try:
        old_score = float(old_score)
    except Exception:
        old_score = 0.0
    new_score = int(max(0, min(100, round(old_score + adj))))

    ds["score"] = adj
    ds["direction"] = direction
    ds["engine"] = ds.get("engine") or "BOS/CHoCH Order Block Scoring"
    strategy["demand_supply"] = ds
    strategy["demand_supply_score_adjustment"] = adj
    strategy["order_block_score_adjustment"] = adj
    strategy["score_before_demand_supply"] = int(old_score)
    strategy["score"] = new_score

    if reasons:
        strategy["reasons"] = (strategy.get("reasons") or []) + reasons
    if warnings:
        strategy["warnings"] = (strategy.get("warnings") or []) + warnings
    breakdown = strategy.get("score_breakdown") or {}
    breakdown["Order Block / Demand-Supply"] = adj
    # Keep old key for backward compatibility in any UI/table code.
    breakdown["Demand/Supply"] = adj
    strategy["score_breakdown"] = breakdown
    return strategy


# ── RC15j Phase 2B — SPX Demand/Supply via SPY DXLink Proxy ─────────────────

def analyze_demand_supply_spy_proxy(
    spx_price: float,
    spy_price: float,
) -> Dict[str, Any]:
    """
    RC15j Phase 2B: SPX Demand/Supply derived from SPY DXLink candles.

    DXLink does not provide OHLC candles for $SPX.X, but does for SPY.
    We compute D/S zones on SPY then scale to SPX units using:
        ratio = spx_price / spy_price
        spx_zone = spy_zone * ratio

    Returns a dict compatible with the standard demand_supply_context shape,
    with extra proxy metadata fields. Used ONLY as a protective hard block
    filter — never as an opening signal.

    Fail-safe: if any required input is missing/invalid, returns
    {"available": False, "proxy_reason": "..."} so downstream uses
    DS_LOCATION_NOT_EVALUATED_PASS.
    """
    FAIL = {"available": False, "source": "dxlink_spy_proxy_failed",
            "proxy_source_symbol": "SPY", "proxy_target_symbol": "SPX", "score": 0}

    # ── Validate inputs ───────────────────────────────────────────────────────
    try:
        spx = float(spx_price)
        spy = float(spy_price)
    except Exception:
        return {**FAIL, "proxy_reason": "INVALID_PRICES"}
    if spx <= 0 or spy <= 0:
        return {**FAIL, "proxy_reason": "ZERO_PRICES"}
    ratio = spx / spy
    # Sanity check: SPX/SPY ratio should be between 8 and 12 normally
    if not (7.0 <= ratio <= 14.0):
        return {**FAIL, "proxy_reason": f"RATIO_OUT_OF_RANGE:{ratio:.3f}"}

    # ── Fetch SPY D/S context from DXLink ────────────────────────────────────
    try:
        spy_ds = analyze_demand_supply("SPY", "0DTE", spy_price)
    except Exception as exc:
        return {**FAIL, "proxy_reason": f"SPY_DS_ERROR:{exc}"}

    if not spy_ds.get("available"):
        htf_r = spy_ds.get("htf", {}).get("reason", "")
        ltf_r = spy_ds.get("ltf", {}).get("reason", "")
        return {**FAIL,
                "proxy_reason": f"SPY_DS_UNAVAILABLE htf={htf_r} ltf={ltf_r}",
                "spy_ds_raw": spy_ds}

    # ── Scale SPY zones → SPX units ──────────────────────────────────────────
    def _scale_zone(zone: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not zone:
            return None
        out = dict(zone)
        for k in ("low", "high", "distance"):
            v = zone.get(k)
            if v is not None:
                try:
                    out[k] = round(float(v) * ratio, 2)
                except Exception:
                    pass
        return out

    def _scale_ctx(ctx: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(ctx)
        out["atr"] = round(float(ctx["atr"] or 0) * ratio, 2) if ctx.get("atr") else None
        out["nearest_demand"] = _scale_zone(ctx.get("nearest_demand") or ctx.get("nearest_bullish_order_block"))
        out["nearest_supply"] = _scale_zone(ctx.get("nearest_supply") or ctx.get("nearest_bearish_order_block"))
        out["nearest_bullish_order_block"] = out["nearest_demand"]
        out["nearest_bearish_order_block"] = out["nearest_supply"]
        return out

    spy_htf = spy_ds.get("htf", {})
    spy_ltf = spy_ds.get("ltf", {})
    spx_htf = _scale_ctx(spy_htf)
    spx_ltf = _scale_ctx(spy_ltf)

    # ── Preserve original SPY zones for journal audit ─────────────────────────
    spy_ltf_nd = spy_ltf.get("nearest_demand") or spy_ltf.get("nearest_bullish_order_block") or {}
    spy_ltf_ns = spy_ltf.get("nearest_supply") or spy_ltf.get("nearest_bearish_order_block") or {}
    spy_htf_nd = spy_htf.get("nearest_demand") or spy_htf.get("nearest_bullish_order_block") or {}
    spy_htf_ns = spy_htf.get("nearest_supply") or spy_htf.get("nearest_bearish_order_block") or {}
    _spy_nd = spy_ltf_nd if spy_ltf_nd else spy_htf_nd
    _spy_ns = spy_ltf_ns if spy_ltf_ns else spy_htf_ns

    return {
        "available":             True,
        "source":                "dxlink_spy_proxy",
        "proxy_source_symbol":   "SPY",
        "proxy_target_symbol":   "SPX",
        "proxy_ratio":           round(ratio, 6),
        "proxy_reason":          "SPX_DXLINK_OHLC_EMPTY_USE_SPY_PROXY",
        "score":                 0,
        "htf":                   spx_htf,
        "ltf":                   spx_ltf,
        # Original SPY zone values (before scaling) — for journal audit
        "spy_nearest_demand_low":  _spy_nd.get("low"),
        "spy_nearest_demand_high": _spy_nd.get("high"),
        "spy_nearest_supply_low":  _spy_ns.get("low"),
        "spy_nearest_supply_high": _spy_ns.get("high"),
    }


# ── RC13b LuxAlgo-derived Swing Demand/Supply Detector Helpers ───────────────

def _luxalgo_like_pivot_events(candles: List[Dict[str, float]], size: int) -> List[Dict[str, Any]]:
    """
    Approximate LuxAlgo's leg(size) / startOfNewLeg() pivot logic in Python.

    Pine reference concept:
      - newLegHigh: high[size] > highest(size)  -> start of bearish leg / pivot high
      - newLegLow : low[size]  < lowest(size)   -> start of bullish leg / pivot low

    This implementation intentionally does not copy PineScript code. It translates
    the public market-structure concept into bot-native Python for OHLC arrays.
    """
    if not candles or len(candles) <= size + 2:
        return []
    events: List[Dict[str, Any]] = []
    leg_state = 0  # 1 bullish leg, 0 bearish leg; mirrors LuxAlgo constants conceptually

    for i in range(size, len(candles)):
        pivot_idx = i - size
        pivot_high = float(candles[pivot_idx]["high"])
        pivot_low = float(candles[pivot_idx]["low"])
        right_window = candles[pivot_idx + 1:i + 1]
        if not right_window:
            continue
        future_highest = max(float(b["high"]) for b in right_window)
        future_lowest = min(float(b["low"]) for b in right_window)

        new_leg = leg_state
        pivot_type: Optional[str] = None
        if pivot_high > future_highest:
            new_leg = 0
            pivot_type = "high"
        elif pivot_low < future_lowest:
            new_leg = 1
            pivot_type = "low"

        if pivot_type is not None and new_leg != leg_state:
            bar = candles[pivot_idx]
            events.append({
                "idx": pivot_idx,
                "type": pivot_type,
                "price": pivot_high if pivot_type == "high" else pivot_low,
                "time": bar.get("time"),
                "leg": new_leg,
            })
            leg_state = new_leg
    return events


def _luxalgo_block_status(
    block: Dict[str, Any],
    candles: List[Dict[str, float]],
    mitigation_source: str = "highlow",
) -> Dict[str, Any]:
    """
    Status check after the structure-break bar, matching the LuxAlgo idea that an
    OB becomes relevant only after the break event stores it.
    """
    bias = block.get("bias")
    low = float(block.get("low", 0.0))
    high = float(block.get("high", 0.0))
    mid = (low + high) / 2.0
    break_idx = int(block.get("break_idx", block.get("created_idx", 0)) or 0)
    future = candles[break_idx + 1:]

    touches = 0
    first_touch_idx: Optional[int] = None
    mitigated = False
    invalidated = False
    invalidation_idx: Optional[int] = None

    for offset, bar in enumerate(future, start=break_idx + 1):
        b_high = float(bar["high"])
        b_low = float(bar["low"])
        b_close = float(bar["close"])

        if bias == "bullish":
            touched = b_low <= high and b_high >= low
            if touched:
                touches += 1
                if first_touch_idx is None:
                    first_touch_idx = offset
                if b_low <= mid:
                    mitigated = True
            invalidated = (b_close < low) if mitigation_source == "close" else (b_low < low or b_close < low)
        elif bias == "bearish":
            touched = b_high >= low and b_low <= high
            if touched:
                touches += 1
                if first_touch_idx is None:
                    first_touch_idx = offset
                if b_high >= mid:
                    mitigated = True
            invalidated = (b_close > high) if mitigation_source == "close" else (b_high > high or b_close > high)
        else:
            touched = False

        if touches >= 2:
            mitigated = True
        if invalidated:
            invalidation_idx = offset
            break

    if invalidated:
        status = "invalidated"
    elif mitigated:
        status = "mitigated"
    elif touches > 0:
        status = "tested"
    else:
        status = "fresh"

    out = dict(block)
    out.update({
        "status": status,
        "touches": touches,
        "mitigated": bool(mitigated),
        "invalidated": bool(invalidated),
        "first_touch_idx": first_touch_idx,
        "invalidation_idx": invalidation_idx,
    })
    return out


def _detect_luxalgo_smc_order_blocks_for_tf(
    candles: List[Dict[str, float]],
    structure_size: int,
    max_blocks: int = 8,
    mitigation_source: str = "highlow",
) -> Dict[str, List[Dict[str, Any]]]:
    """
    RC13b: LuxAlgo-style SMC Order Block detector for Swing Demand/Supply.

    Translated concepts from the supplied indicator:
      1. Detect swing/internal pivots using leg(size)-style delayed pivots.
      2. close crossing above pivot high => Bullish BOS/CHoCH.
      3. close crossing below pivot low  => Bearish BOS/CHoCH.
      4. Bullish OB = bar with minimum parsed low between pivot and break.
      5. Bearish OB = bar with maximum parsed high between pivot and break.
      6. Delete/disable blocks when mitigated/invalidated by high/low or close.

    The function returns bullish blocks as Demand and bearish blocks as Supply.
    """
    if not candles or len(candles) < structure_size + 15:
        return {"bullish": [], "bearish": [], "active_bullish": [], "active_bearish": []}

    events = _luxalgo_like_pivot_events(candles, structure_size)
    events_by_idx: Dict[int, List[Dict[str, Any]]] = {}
    for ev in events:
        events_by_idx.setdefault(int(ev["idx"]), []).append(ev)

    parsed_highs, parsed_lows = _parsed_high_low(candles)
    atr_val = _atr(candles)

    swing_high: Optional[Dict[str, Any]] = None
    swing_low: Optional[Dict[str, Any]] = None
    trend_bias = "neutral"
    blocks: List[Dict[str, Any]] = []

    for i, bar in enumerate(candles):
        for ev in events_by_idx.get(i, []):
            if ev.get("type") == "high":
                swing_high = {"idx": ev["idx"], "price": ev["price"], "crossed": False, "time": ev.get("time")}
            elif ev.get("type") == "low":
                swing_low = {"idx": ev["idx"], "price": ev["price"], "crossed": False, "time": ev.get("time")}

        close = float(bar["close"])

        if swing_high and not swing_high.get("crossed") and close > float(swing_high["price"]):
            pivot_idx = int(swing_high["idx"])
            start, end = min(pivot_idx, i), max(pivot_idx, i)
            if end > start:
                window = parsed_lows[start:end + 1]
                rel_idx = min(range(len(window)), key=lambda k: window[k])
                ob_idx = start + rel_idx
                ob_high = float(parsed_highs[ob_idx])
                ob_low = float(parsed_lows[ob_idx])
                if ob_high < ob_low:
                    ob_high, ob_low = ob_low, ob_high
                event_type = "Bullish CHoCH" if trend_bias == "bearish" else "Bullish BOS"
                displacement = close - float(swing_high["price"])
                block = {
                    "type": "demand",
                    "ob_type": "luxalgo_style_bullish_order_block",
                    "bias": "bullish",
                    "structure_event": event_type,
                    "break_level": round(float(swing_high["price"]), 2),
                    "break_idx": i,
                    "idx": ob_idx,
                    "created_idx": ob_idx,
                    "created_time": candles[ob_idx].get("time"),
                    "low": round(ob_low, 2),
                    "high": round(ob_high, 2),
                    "mid": round((ob_low + ob_high) / 2.0, 2),
                    "age_bars": len(candles) - 1 - ob_idx,
                    "displacement": round(displacement, 2),
                    "structure_size": structure_size,
                    "method": "LuxAlgo-style BOS/CHoCH Order Block",
                }
                block = _luxalgo_block_status(block, candles, mitigation_source)
                block["strength"] = _block_strength(block, candles, atr_val)
                blocks.append(block)
            trend_bias = "bullish"
            swing_high["crossed"] = True
            swing_high = None

        if swing_low and not swing_low.get("crossed") and close < float(swing_low["price"]):
            pivot_idx = int(swing_low["idx"])
            start, end = min(pivot_idx, i), max(pivot_idx, i)
            if end > start:
                window = parsed_highs[start:end + 1]
                rel_idx = max(range(len(window)), key=lambda k: window[k])
                ob_idx = start + rel_idx
                ob_high = float(parsed_highs[ob_idx])
                ob_low = float(parsed_lows[ob_idx])
                if ob_high < ob_low:
                    ob_high, ob_low = ob_low, ob_high
                event_type = "Bearish CHoCH" if trend_bias == "bullish" else "Bearish BOS"
                displacement = float(swing_low["price"]) - close
                block = {
                    "type": "supply",
                    "ob_type": "luxalgo_style_bearish_order_block",
                    "bias": "bearish",
                    "structure_event": event_type,
                    "break_level": round(float(swing_low["price"]), 2),
                    "break_idx": i,
                    "idx": ob_idx,
                    "created_idx": ob_idx,
                    "created_time": candles[ob_idx].get("time"),
                    "low": round(ob_low, 2),
                    "high": round(ob_high, 2),
                    "mid": round((ob_low + ob_high) / 2.0, 2),
                    "age_bars": len(candles) - 1 - ob_idx,
                    "displacement": round(displacement, 2),
                    "structure_size": structure_size,
                    "method": "LuxAlgo-style BOS/CHoCH Order Block",
                }
                block = _luxalgo_block_status(block, candles, mitigation_source)
                block["strength"] = _block_strength(block, candles, atr_val)
                blocks.append(block)
            trend_bias = "bearish"
            swing_low["crossed"] = True
            swing_low = None

    bullish = [b for b in blocks if b.get("bias") == "bullish"]
    bearish = [b for b in blocks if b.get("bias") == "bearish"]

    def sort_key(b: Dict[str, Any]) -> Tuple[int, int, int]:
        active_rank = 1 if b.get("status") in ("fresh", "tested") else 0
        return (active_rank, int(b.get("strength", 0) or 0), -int(b.get("age_bars", 999) or 999))

    bullish = sorted(bullish, key=sort_key, reverse=True)[:max_blocks]
    bearish = sorted(bearish, key=sort_key, reverse=True)[:max_blocks]
    return {
        "bullish": bullish,
        "bearish": bearish,
        "active_bullish": [b for b in bullish if b.get("status") in ("fresh", "tested")],
        "active_bearish": [b for b in bearish if b.get("status") in ("fresh", "tested")],
    }


def _luxalgo_structure_size_for_tf(tf: str) -> Tuple[int, str]:
    """
    Map bot timeframes to a conservative LuxAlgo-style structure profile.

    LuxAlgo's visual Swing Structure default is 50 bars. For an automated Swing
    entry filter, 50 bars on 1H is too delayed and can anchor decisions to stale
    order blocks. RC13c keeps the LuxAlgo BOS/CHoCH -> OB concept but uses a
    trading-oriented profile:
      - 1H  : 14-bar swing structure
      - 4H  : 20-bar swing structure
      - 15m : 5-bar internal structure
      - 5m  : 3-bar fast internal confirmation
    """
    tf_u = str(tf or "1H").upper()
    if tf_u == "4H":
        return 20, "Swing Structure (conservative 20-bar)"
    if tf_u == "1H":
        return 14, "Swing Structure (conservative 14-bar)"
    if tf_u == "5M":
        return 3, "Fast Internal Structure (3-bar)"
    return 5, "Internal Structure (5-bar)"


def _luxalgo_max_ob_age_for_tf(tf: str) -> int:
    """Maximum age, in candles, for an OB to be actionable in RC13c."""
    tf_u = str(tf or "1H").upper()
    if tf_u == "4H":
        return 30
    if tf_u == "1H":
        return 30
    if tf_u == "15M":
        return 80
    if tf_u == "5M":
        return 120
    return 30


def _zone_confidence(price: float, zone: Dict[str, Any], max_age_bars: int, atr_val: float = 0.0) -> Dict[str, Any]:
    """
    RC13c confidence score for LuxAlgo-style OB zones.

    The score is intentionally conservative. Hard rejection in analyzer.py should
    require confidence >= 70 plus 15m/5m confirmation. Lower-confidence zones are
    diagnostics/warnings only.
    """
    out = dict(zone or {})
    age = int(out.get("age_bars", 999) or 999)
    status = str(out.get("status") or "unknown").lower()
    event = str(out.get("structure_event") or "")
    displacement = abs(float(out.get("displacement", 0.0) or 0.0))
    distance_pct = out.get("distance_pct")
    inside = bool(out.get("inside") or out.get("contains_price"))

    score = 0
    components: Dict[str, Any] = {}

    # Freshness / age, capped hard by stale status.
    if age <= max(1, max_age_bars // 3):
        freshness = 30
    elif age <= max_age_bars:
        freshness = 20
    else:
        freshness = 0
    score += freshness
    components["freshness"] = freshness

    # Status: fresh is strongest; tested is useful but weaker; mitigated/invalidated not actionable.
    if status == "fresh":
        status_score = 25
    elif status == "tested":
        status_score = 15
    else:
        status_score = 0
    score += status_score
    components["status"] = status_score

    # Price relevance. Inside/near zones are what matter for entry blocking.
    try:
        d = float(distance_pct) if distance_pct is not None else 999.0
    except Exception:
        d = 999.0
    if inside:
        distance_score = 25
    elif d <= 0.25:
        distance_score = 20
    elif d <= 0.50:
        distance_score = 15
    elif d <= 1.00:
        distance_score = 5
    else:
        distance_score = 0
    score += distance_score
    components["distance"] = distance_score

    # BOS/CHoCH event quality. CHoCH is valuable as a reversal warning.
    event_score = 10 if "CHOCH" in event.upper() else 7 if "BOS" in event.upper() else 0
    score += event_score
    components["structure_event"] = event_score

    # Displacement vs ATR, if available.
    if atr_val and displacement >= 1.5 * atr_val:
        disp_score = 10
    elif atr_val and displacement >= 0.8 * atr_val:
        disp_score = 6
    elif displacement > 0:
        disp_score = 3
    else:
        disp_score = 0
    score += disp_score
    components["displacement"] = disp_score

    stale = age > max_age_bars
    actionable = bool((not stale) and status in ("fresh", "tested") and score >= 40)
    hard_reject_allowed = bool(actionable and score >= 70)

    out["max_age_bars"] = max_age_bars
    out["stale"] = stale
    out["confidence"] = int(max(0, min(100, score)))
    out["confidence_components"] = components
    out["confidence_grade"] = "strong" if score >= 70 else "warning" if score >= 40 else "diagnostic_only"
    out["actionable"] = actionable
    out["hard_reject_allowed"] = hard_reject_allowed
    out["decision_impact"] = "hard_reject_allowed" if hard_reject_allowed else "warning_only" if actionable else "diagnostics_only"
    return out


def _annotate_nearest_zone(price: float, zone: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not zone:
        return None
    out = dict(zone)
    low = float(out.get("low", 0.0))
    high = float(out.get("high", 0.0))
    if low <= price <= high:
        distance = 0.0
    else:
        distance = min(abs(price - low), abs(price - high))
    out["distance"] = round(distance, 2)
    out["distance_pct"] = round((distance / price) * 100.0, 3) if price else None
    inside = bool(low <= price <= high)
    out["inside"] = inside
    out["contains_price"] = inside
    return out


# ── RC13 Swing Demand/Supply Zone Detector ───────────────────────────────────

def _avg_range(candles: List[Dict[str, float]], lookback: int = 20) -> float:
    """Average (high - low) over last `lookback` candles."""
    vals = [c["high"] - c["low"] for c in candles[-lookback:] if c.get("high") and c.get("low")]
    return sum(vals) / len(vals) if vals else 0.0


def _detect_impulse_zones(
    candles: List[Dict[str, float]],
    avg_rng: float,
    price: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Detect Demand (bullish impulse base) and Supply (bearish impulse base) zones.

    A bullish impulse = candle whose body >= 60% of range AND range >= 1.2 × avg_range AND close > open.
    The base candle (the last bearish or small candle before the impulse) defines the Demand zone.

    A bearish impulse = same criteria reversed.
    The base candle (the last bullish or small candle before the impulse) defines the Supply zone.
    """
    demand_zones: List[Dict[str, Any]] = []
    supply_zones: List[Dict[str, Any]] = []

    if avg_rng <= 0 or len(candles) < 3:
        return demand_zones, supply_zones

    for i in range(1, len(candles)):
        c = candles[i]
        rng  = c["high"] - c["low"]
        body = abs(c["close"] - c["open"])
        if rng < avg_rng * 1.2 or body < rng * 0.6:
            continue

        # Find base candle: the last candle before i with opposite or small body
        base_idx = i - 1
        while base_idx >= 0:
            b = candles[base_idx]
            b_body = abs(b["close"] - b["open"])
            b_rng  = b["high"] - b["low"]
            if b_body < b_rng * 0.5:  # small / doji candle = valid base
                break
            if c["close"] > c["open"] and b["close"] < b["open"]:
                break   # bullish impulse + bearish base → Demand
            if c["close"] < c["open"] and b["close"] > b["open"]:
                break   # bearish impulse + bullish base → Supply
            base_idx -= 1
            if i - base_idx > 5:   # don't look more than 5 candles back
                break

        if base_idx < 0:
            continue

        b = candles[base_idx]
        ts = b.get("time", 0)

        if c["close"] > c["open"]:   # bullish impulse → Demand
            zone_low  = round(b["low"],  2)
            zone_high = round(max(b["open"], b["close"]), 2)
            if zone_high <= zone_low:
                zone_high = round(b["high"], 2)
            # Validate: was this zone broken afterward?
            broken = any(
                candles[j]["close"] < zone_low
                for j in range(i + 1, len(candles))
            )
            if not broken:
                dist_pct = abs(price - zone_high) / price * 100 if price > 0 else 999
                inside   = zone_low <= price <= zone_high
                demand_zones.append({
                    "low":         zone_low,
                    "high":        zone_high,
                    "created_at":  ts,
                    "distance_pct": round(dist_pct, 3),
                    "inside":      inside,
                    "valid":       True,
                    "base_idx":    base_idx,
                    "impulse_idx": i,
                })

        else:   # bearish impulse → Supply
            zone_high = round(b["high"], 2)
            zone_low  = round(min(b["open"], b["close"]), 2)
            if zone_low >= zone_high:
                zone_low = round(b["low"], 2)
            broken = any(
                candles[j]["close"] > zone_high
                for j in range(i + 1, len(candles))
            )
            if not broken:
                dist_pct = abs(zone_low - price) / price * 100 if price > 0 else 999
                inside   = zone_low <= price <= zone_high
                supply_zones.append({
                    "low":         zone_low,
                    "high":        zone_high,
                    "created_at":  ts,
                    "distance_pct": round(dist_pct, 3),
                    "inside":      inside,
                    "valid":       True,
                    "base_idx":    base_idx,
                    "impulse_idx": i,
                })

    # Sort by distance to price
    demand_zones.sort(key=lambda z: z["distance_pct"])
    supply_zones.sort(key=lambda z: z["distance_pct"])
    return demand_zones, supply_zones


def detect_swing_demand_supply_zones(
    symbol: str,
    price: float,
    timeframe: str = "1H",
) -> Dict[str, Any]:
    """
    RC13c — Conservative LuxAlgo-style Swing Demand/Supply detector.

    Important mapping:
      - Demand = active Bullish Order Block after Bullish BOS/CHoCH.
      - Supply = active Bearish Order Block after Bearish BOS/CHoCH.
      - 1H uses 14-bar swing structure instead of visual 50-bar default.
      - 15m uses 5-bar internal structure; 5m uses 3-bar fast confirmation.
      - OBs receive age/status/distance/displacement confidence.

    This is an independent Python implementation for DXLink/Yahoo OHLC arrays; it does
    not copy PineScript drawing/UI code.
    """
    NEAR_THRESHOLD_PCT = 0.50
    sym = str(symbol or "").upper().strip()
    tf = str(timeframe or "1H").upper()

    interval_map = {"1H": "1h", "15M": "15m", "4H": "4h", "5M": "5m"}
    days_map = {"1H": 45, "15M": 12, "4H": 120, "5M": 5}
    interval = interval_map.get(tf, "1h")
    days = days_map.get(tf, 45)
    structure_size, structure_label = _luxalgo_structure_size_for_tf(tf)
    max_ob_age_bars = _luxalgo_max_ob_age_for_tf(tf)

    empty = {
        "available": False,
        "timeframe": tf,
        "engine": "LuxAlgo-style BOS/CHoCH Order Blocks",
        "structure": structure_label,
        "structure_size": structure_size,
        "max_ob_age_bars": max_ob_age_bars,
        "confidence_threshold_hard_reject": 70,
        "confidence_threshold_warning": 40,
        "nearest_demand": None,
        "nearest_supply": None,
        "all_demand_zones": [],
        "all_supply_zones": [],
        "bullish_order_blocks": [],
        "bearish_order_blocks": [],
        "active_bullish_order_blocks": [],
        "active_bearish_order_blocks": [],
        "near_demand": False,
        "near_supply": False,
        "inside_demand": False,
        "inside_supply": False,
        "price": price,
        "reason": "not_computed",
    }

    try:
        # Swing Demand/Supply uses DXLink for all supported Swing symbols, with Yahoo fallback.
        candles, candle_source, candle_status = _load_orderblock_candles(sym, "SWING", f"{days}d", interval)
        min_required = structure_size + 15
        if not candles or len(candles) < min_required:
            empty["reason"] = f"insufficient_candles ({len(candles) if candles else 0}, need>={min_required})"
            empty["source"] = candle_source
            empty["candle_status"] = candle_status
            empty["candles"] = len(candles or [])
            return empty

        blocks = _detect_luxalgo_smc_order_blocks_for_tf(
            candles,
            structure_size=structure_size,
            max_blocks=8,
            mitigation_source="highlow",
        )
        atr_val = _atr(candles)
        all_demand = [_zone_confidence(price, _annotate_nearest_zone(price, z) or z, max_ob_age_bars, atr_val) for z in blocks.get("bullish", [])]
        all_supply = [_zone_confidence(price, _annotate_nearest_zone(price, z) or z, max_ob_age_bars, atr_val) for z in blocks.get("bearish", [])]

        # Actionable means fresh/tested, not stale, and confidence >= 40. Hard rejection still
        # requires confidence >= 70 inside analyzer.py plus 15m/5m confirmation.
        active_demand = [z for z in all_demand if z.get("actionable")]
        active_supply = [z for z in all_supply if z.get("actionable")]

        nearest_demand = _zone_confidence(price, _annotate_nearest_zone(price, _nearest_zone(price, active_demand)) or {}, max_ob_age_bars, atr_val) if active_demand else None
        nearest_supply = _zone_confidence(price, _annotate_nearest_zone(price, _nearest_zone(price, active_supply)) or {}, max_ob_age_bars, atr_val) if active_supply else None

        # Prefer zones on the correct side of price unless price is inside the zone.
        if nearest_demand and not nearest_demand.get("inside") and float(nearest_demand.get("high", 0.0)) > price:
            below = [z for z in active_demand if float(z.get("high", 0.0)) <= price]
            nearest_demand = _zone_confidence(price, _annotate_nearest_zone(price, _nearest_zone(price, below)) or {}, max_ob_age_bars, atr_val) if below else nearest_demand
        if nearest_supply and not nearest_supply.get("inside") and float(nearest_supply.get("low", 0.0)) < price:
            above = [z for z in active_supply if float(z.get("low", 0.0)) >= price]
            nearest_supply = _zone_confidence(price, _annotate_nearest_zone(price, _nearest_zone(price, above)) or {}, max_ob_age_bars, atr_val) if above else nearest_supply

        near_demand = bool(nearest_demand and nearest_demand.get("distance_pct") is not None and nearest_demand["distance_pct"] <= NEAR_THRESHOLD_PCT)
        near_supply = bool(nearest_supply and nearest_supply.get("distance_pct") is not None and nearest_supply["distance_pct"] <= NEAR_THRESHOLD_PCT)
        inside_demand = bool(nearest_demand and nearest_demand.get("inside"))
        inside_supply = bool(nearest_supply and nearest_supply.get("inside"))
        demand_hard_reject_allowed = bool(nearest_demand and nearest_demand.get("hard_reject_allowed"))
        supply_hard_reject_allowed = bool(nearest_supply and nearest_supply.get("hard_reject_allowed"))

        return {
            "available": True,
            "timeframe": tf,
            "source": candle_source,
            "candle_status": candle_status,
            "candles": len(candles),
            "atr": round(atr_val, 2),
            "engine": "Conservative LuxAlgo-style BOS/CHoCH Order Blocks",
            "structure": structure_label,
            "structure_size": structure_size,
            "max_ob_age_bars": max_ob_age_bars,
            "confidence_threshold_hard_reject": 70,
            "confidence_threshold_warning": 40,
            "mitigation_source": "High/Low",
            "nearest_demand": nearest_demand,
            "nearest_supply": nearest_supply,
            "all_demand_zones": active_demand[:5],
            "all_supply_zones": active_supply[:5],
            "bullish_order_blocks": all_demand,
            "bearish_order_blocks": all_supply,
            "active_bullish_order_blocks": active_demand,
            "active_bearish_order_blocks": active_supply,
            "near_demand": near_demand,
            "near_supply": near_supply,
            "inside_demand": inside_demand,
            "inside_supply": inside_supply,
            "demand_hard_reject_allowed": demand_hard_reject_allowed,
            "supply_hard_reject_allowed": supply_hard_reject_allowed,
            "nearest_demand_confidence": nearest_demand.get("confidence") if nearest_demand else None,
            "nearest_supply_confidence": nearest_supply.get("confidence") if nearest_supply else None,
            "price": price,
            "near_threshold_pct": NEAR_THRESHOLD_PCT,
            "reason": "ok_rc13c_conservative_luxalgo_style_ob",
        }
    except Exception as exc:
        empty["reason"] = f"error: {type(exc).__name__}: {str(exc)[:120]}"
        empty["source"] = locals().get("candle_source", "unknown")
        empty["candle_status"] = locals().get("candle_status", {})
        empty["candles"] = len(locals().get("candles", []) or [])
        return empty


# ── RC15j Phase 2B.1 — Structural Supply/Demand (Shadow Mode) ────────────────
# Definitions:
#   Demand = last bearish candle before bullish BOS (close above a confirmed swing high)
#            Left side must have ≥2 swing highs + ≥2 swing lows before the base candle.
#   Supply = last bullish candle before bearish BOS (close below a confirmed swing low)
#            Left side must have ≥2 swing lows + ≥2 swing highs before the base candle.
#   BOS is confirmed by candle close only — never wick.
#   Shadow mode: results are logged in journal only. No trade blocking.
# ─────────────────────────────────────────────────────────────────────────────

_ZONE_METHOD = "structural_last_opposite_candle_before_close_BOS_with_left_structure"


def _struct_swing_highs(candles: List[Dict[str, float]], n: int = 2) -> List[int]:
    """Indices where candle[i].high strictly exceeds n neighbors on each side."""
    result: List[int] = []
    for i in range(n, len(candles) - n):
        h = candles[i]["high"]
        if (all(candles[i - k]["high"] < h for k in range(1, n + 1)) and
                all(candles[i + k]["high"] < h for k in range(1, n + 1))):
            result.append(i)
    return result


def _struct_swing_lows(candles: List[Dict[str, float]], n: int = 2) -> List[int]:
    """Indices where candle[i].low strictly less than n neighbors on each side."""
    result: List[int] = []
    for i in range(n, len(candles) - n):
        lo = candles[i]["low"]
        if (all(candles[i - k]["low"] > lo for k in range(1, n + 1)) and
                all(candles[i + k]["low"] > lo for k in range(1, n + 1))):
            result.append(i)
    return result


def _ts_to_str(ts: Any) -> str:
    """Convert epoch-ms timestamp to readable string for journal."""
    try:
        import datetime as _dt
        ms = float(ts)
        if ms > 1e12:
            ms /= 1000.0
        return _dt.datetime.utcfromtimestamp(ms).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _infer_structure_bias(
    candles: List[Dict[str, float]],
    swing_high_indices: List[int],
    swing_low_indices: List[int],
) -> str:
    """Infer simple pre-BOS structural bias from the last two swing highs/lows.

    Shadow diagnostic only. This is intentionally conservative and does not
    affect trading decisions.
    """
    try:
        if len(swing_high_indices) < 2 or len(swing_low_indices) < 2:
            return "UNKNOWN"
        sh1, sh2 = swing_high_indices[-2], swing_high_indices[-1]
        sl1, sl2 = swing_low_indices[-2], swing_low_indices[-1]
        h1, h2 = float(candles[sh1]["high"]), float(candles[sh2]["high"])
        l1, l2 = float(candles[sl1]["low"]), float(candles[sl2]["low"])
        if h2 > h1 and l2 > l1:
            return "BULLISH"
        if h2 < h1 and l2 < l1:
            return "BEARISH"
        return "MIXED"
    except Exception:
        return "UNKNOWN"


def _classify_structure_event(direction: str, bias_before: str) -> str:
    """Classify a close-based break as BOS/CHoCH for journal logging only."""
    d = str(direction or "").upper()
    b = str(bias_before or "").upper()
    if d == "BULLISH":
        if b == "BEARISH":
            return "CHoCH"
        if b == "BULLISH":
            return "BOS"
    elif d == "BEARISH":
        if b == "BULLISH":
            return "CHoCH"
        if b == "BEARISH":
            return "BOS"
    return "UNCLEAR"


def _zone_lifecycle(
    candles: List[Dict[str, float]],
    zone_low: Optional[float],
    zone_high: Optional[float],
    bos_idx: Optional[int],
    bias: str,
) -> Dict[str, Any]:
    """Fresh/tested/mitigated/invalidated shadow diagnostics.

    Uses candles AFTER the BOS candle only. This avoids marking the base/BOS
    creation sequence itself as a revisit. No trading decisions are changed.
    """
    out = {
        "tested": False,
        "mitigated": False,
        "invalidated": False,
        "touches": 0,
        "last_touch_idx": None,
        "last_touch_time": "",
        "invalidated_idx": None,
        "invalidated_time": "",
        "mitigation_status": "fresh",
    }
    try:
        if zone_low is None or zone_high is None or bos_idx is None:
            return out
        zlo = float(zone_low); zhi = float(zone_high)
        if zhi < zlo:
            zlo, zhi = zhi, zlo
        mid = (zlo + zhi) / 2.0
        direction = str(bias or "").upper()
        for i in range(int(bos_idx) + 1, len(candles)):
            c = candles[i]
            hi = float(c["high"]); lo = float(c["low"]); cl = float(c["close"])
            touched = (hi >= zlo and lo <= zhi)
            if touched:
                out["touches"] += 1
                out["tested"] = True
                out["last_touch_idx"] = i
                out["last_touch_time"] = _ts_to_str(c.get("time", 0))
                if lo <= mid <= hi:
                    out["mitigated"] = True
            if direction == "BULLISH" and cl < zlo:
                out["invalidated"] = True
                out["invalidated_idx"] = i
                out["invalidated_time"] = _ts_to_str(c.get("time", 0))
                break
            if direction == "BEARISH" and cl > zhi:
                out["invalidated"] = True
                out["invalidated_idx"] = i
                out["invalidated_time"] = _ts_to_str(c.get("time", 0))
                break
        if out["invalidated"]:
            out["mitigation_status"] = "invalidated"
        elif out["mitigated"]:
            out["mitigation_status"] = "mitigated"
        elif out["tested"]:
            out["mitigation_status"] = "tested"
        else:
            out["mitigation_status"] = "fresh"
    except Exception as exc:
        out["mitigation_status"] = f"error:{type(exc).__name__}"
    return out


def _signed_distance_to_zone(price: Optional[float], low: Optional[float], high: Optional[float]) -> Optional[float]:
    """Signed distance to zone in SPX points. 0 means price is inside zone."""
    try:
        if price is None or low is None or high is None:
            return None
        px = float(price); lo = float(low); hi = float(high)
        if hi < lo:
            lo, hi = hi, lo
        if lo <= px <= hi:
            return 0.0
        if px < lo:
            return round(lo - px, 2)
        return round(hi - px, 2)  # negative = price above zone
    except Exception:
        return None


def _detect_structural_demand(
    candles: List[Dict[str, float]],
    n: int = 2,
    max_bos_lookback_bars: int = 40,
    local_structure_lookback: int = 100,
) -> Dict[str, Any]:
    """
    RC15j Phase 2B.1 — corrected Demand detection order:

    1. Find candidate bullish BOS candle (scan right-to-left).
    2. Find base_candle = last bearish candle BEFORE bos_idx.
    3. Compute swing highs/lows strictly before base_candle_idx
       (both-side confirmation: high[i] > n neighbors on left AND right).
    4. Require ≥2 swing highs and ≥2 swing lows before base_candle_idx.
    5. BOS level must be one of those pre-base swing highs:
       bos_cls > swing_high[j]  where j < base_candle_idx.
       (A swing high that formed BETWEEN base and BOS does not qualify.)
    6. Zone: low/high of base candle (in raw SPY units; caller scales to SPX).

    Phase 2B.2 — BOS locality:
    The broken swing high must be recent relative to the base candle:
        bos_level_age_bars = base_candle_idx - bos_level_idx
        valid only if 0 < bos_level_age_bars <= max_bos_lookback_bars
    If stale, the candidate is skipped and the next most recent BOS is tried.
    """
    min_len = n * 2 + 4
    if len(candles) < min_len:
        return {"found": False, "reason": f"need≥{min_len}_candles_got_{len(candles)}"}

    all_sh = _struct_swing_highs(candles, n)
    all_sl = _struct_swing_lows(candles, n)

    _any_locality_rejected = False

    for bos_idx in range(len(candles) - 1, n * 2 + 2, -1):
        bos_c   = candles[bos_idx]
        bos_cls = bos_c["close"]

        # Step 1: base candle = last bearish candle before BOS
        base_idx: Optional[int] = None
        for j in range(bos_idx - 1, -1, -1):
            if candles[j]["close"] < candles[j]["open"]:
                base_idx = j
                break
        if base_idx is None:
            continue

        # Step 2: structure strictly before base_candle_idx
        sh_before_base = [i for i in all_sh if i < base_idx]
        sl_before_base = [i for i in all_sl if i < base_idx]
        left_sh_count  = len(sh_before_base)
        left_sl_count  = len(sl_before_base)

        if left_sh_count < 2 or left_sl_count < 2:
            continue

        # Step 3: BOS level must come from pre-base swing highs only.
        # Phase 2B.2+ semantic fix: first restrict candidates to local/recent
        # swing levels, then choose the *tightest* broken level by price.
        # For bullish BOS this is the highest broken swing high below close.
        broken_all = [i for i in sh_before_base if bos_cls > candles[i]["high"]]
        if not broken_all:
            continue   # close did not break any pre-base swing high

        broken_local = [
            i for i in broken_all
            if 0 < (base_idx - i) <= max_bos_lookback_bars
        ]
        if not broken_local:
            _any_locality_rejected = True
            continue   # all broken levels were too old — try next candidate

        bos_level_idx = max(broken_local, key=lambda i: (candles[i]["high"], i))
        bos_level = candles[bos_level_idx]["high"]
        bos_level_age_bars: Optional[int] = base_idx - bos_level_idx
        _bos_local_valid = True

        base_c = candles[base_idx]

        # Last 2 swing high/low times before base (for visual confirmation)
        last2_sh = sh_before_base[-2:] if len(sh_before_base) >= 2 else sh_before_base
        last2_sl = sl_before_base[-2:] if len(sl_before_base) >= 2 else sl_before_base

        # Local structure counts within lookback window before base_idx
        _local_lb = int(local_structure_lookback or 100)
        _local_start = max(0, base_idx - _local_lb)
        sh_local = [i for i in sh_before_base if i >= _local_start]
        sl_local = [i for i in sl_before_base if i >= _local_start]
        _bias_before = _infer_structure_bias(candles, sh_before_base, sl_before_base)
        _structure_event = _classify_structure_event("BULLISH", _bias_before)

        return {
            "found":                               True,
            "zone_low":                            base_c["low"],
            "zone_high":                           base_c["high"],
            "base_candle_time":                    _ts_to_str(base_c.get("time", 0)),
            "base_candle_idx":                     base_idx,
            "bos_candle_time":                     _ts_to_str(bos_c.get("time", 0)),
            "bos_candle_idx":                      bos_idx,
            "bos_candle_close":                    round(bos_cls, 2),
            "bos_level_broken":                    round(bos_level, 2),
            "bos_level_idx":                       bos_level_idx,
            "bos_level_time":                      _ts_to_str(candles[bos_level_idx].get("time", 0)) if bos_level_idx is not None else "",
            "bos_level_age_bars":                  bos_level_age_bars,
            "max_bos_lookback_bars":               max_bos_lookback_bars,
            "bos_local_valid":                     True,
            "zone_status":                         "valid",
            "zone_reject_reason":                  "",
            "structure_direction":                   "bullish",
            "structure_bias_before":                 _bias_before,
            "structure_event":                       _structure_event,
            "left_structure_high_count_before_base": left_sh_count,
            "left_structure_low_count_before_base":  left_sl_count,
            "left_structure_valid_before_base":      True,
            "left_sh_local_count":                 len(sh_local),
            "left_sl_local_count":                 len(sl_local),
            "left_sh_local_lookback":              _local_lb,
            "last2_sh_times": [_ts_to_str(candles[i].get("time", 0)) for i in last2_sh],
            "last2_sl_times": [_ts_to_str(candles[i].get("time", 0)) for i in last2_sl],
            "zone_method":                         _ZONE_METHOD,
        }

    _rej_reason = (
        "BOS_LEVEL_OUTSIDE_LOCAL_LOOKBACK"
        if _any_locality_rejected
        else "no_valid_bullish_bos_with_pre_base_swing_high"
    )
    return {
        "found": False,
        "reason": _rej_reason,
        "zone_status": "rejected",
        "zone_reject_reason": _rej_reason,
        "bos_local_valid": False,
        "max_bos_lookback_bars": max_bos_lookback_bars,
    }


def _detect_structural_supply(
    candles: List[Dict[str, float]],
    n: int = 2,
    max_bos_lookback_bars: int = 40,
    local_structure_lookback: int = 100,
) -> Dict[str, Any]:
    """
    RC15j Phase 2B.1 — corrected Supply detection order:

    1. Find candidate bearish BOS candle (scan right-to-left).
    2. Find base_candle = last bullish candle BEFORE bos_idx.
    3. Compute swing lows/highs strictly before base_candle_idx
       (both-side confirmation: low[i] < n neighbors on left AND right).
    4. Require ≥2 swing lows and ≥2 swing highs before base_candle_idx.
    5. BOS level must be one of those pre-base swing lows:
       bos_cls < swing_low[j]  where j < base_candle_idx.
       (A swing low that formed BETWEEN base and BOS does not qualify.)
    6. Zone: low/high of base candle (in raw SPY units; caller scales to SPX).

    Phase 2B.2 — BOS locality:
    The broken swing low must be recent relative to the base candle:
        bos_level_age_bars = base_candle_idx - bos_level_idx
        valid only if 0 < bos_level_age_bars <= max_bos_lookback_bars
    If stale, the candidate is skipped and the next most recent BOS is tried.
    """
    min_len = n * 2 + 4
    if len(candles) < min_len:
        return {"found": False, "reason": f"need≥{min_len}_candles_got_{len(candles)}"}

    all_sh = _struct_swing_highs(candles, n)
    all_sl = _struct_swing_lows(candles, n)

    _any_locality_rejected = False

    for bos_idx in range(len(candles) - 1, n * 2 + 2, -1):
        bos_c   = candles[bos_idx]
        bos_cls = bos_c["close"]

        # Step 1: base candle = last bullish candle before BOS
        base_idx: Optional[int] = None
        for j in range(bos_idx - 1, -1, -1):
            if candles[j]["close"] > candles[j]["open"]:
                base_idx = j
                break
        if base_idx is None:
            continue

        # Step 2: structure strictly before base_candle_idx
        sh_before_base = [i for i in all_sh if i < base_idx]
        sl_before_base = [i for i in all_sl if i < base_idx]
        left_sh_count  = len(sh_before_base)
        left_sl_count  = len(sl_before_base)

        if left_sl_count < 2 or left_sh_count < 2:
            continue

        # Step 3: BOS level must come from pre-base swing lows only.
        # Phase 2B.2+ semantic fix: first restrict candidates to local/recent
        # swing levels, then choose the *tightest* broken level by price.
        # For bearish BOS this is the lowest broken swing low above close.
        broken_all = [i for i in sl_before_base if bos_cls < candles[i]["low"]]
        if not broken_all:
            continue   # close did not break any pre-base swing low

        broken_local = [
            i for i in broken_all
            if 0 < (base_idx - i) <= max_bos_lookback_bars
        ]
        if not broken_local:
            _any_locality_rejected = True
            continue   # all broken levels were too old — try next candidate

        bos_level_idx = min(broken_local, key=lambda i: (candles[i]["low"], -i))
        bos_level = candles[bos_level_idx]["low"]
        bos_level_age_bars: Optional[int] = base_idx - bos_level_idx
        _bos_local_valid = True

        base_c = candles[base_idx]

        # Last 2 swing high/low times before base (for visual confirmation)
        last2_sh = sh_before_base[-2:] if len(sh_before_base) >= 2 else sh_before_base
        last2_sl = sl_before_base[-2:] if len(sl_before_base) >= 2 else sl_before_base

        # Local structure counts within lookback window before base_idx
        _local_lb = int(local_structure_lookback or 100)
        _local_start = max(0, base_idx - _local_lb)
        sh_local = [i for i in sh_before_base if i >= _local_start]
        sl_local = [i for i in sl_before_base if i >= _local_start]
        _bias_before = _infer_structure_bias(candles, sh_before_base, sl_before_base)
        _structure_event = _classify_structure_event("BEARISH", _bias_before)

        return {
            "found":                               True,
            "zone_low":                            base_c["low"],
            "zone_high":                           base_c["high"],
            "base_candle_time":                    _ts_to_str(base_c.get("time", 0)),
            "base_candle_idx":                     base_idx,
            "bos_candle_time":                     _ts_to_str(bos_c.get("time", 0)),
            "bos_candle_idx":                      bos_idx,
            "bos_candle_close":                    round(bos_cls, 2),
            "bos_level_broken":                    round(bos_level, 2),
            "bos_level_idx":                       bos_level_idx,
            "bos_level_time":                      _ts_to_str(candles[bos_level_idx].get("time", 0)) if bos_level_idx is not None else "",
            "bos_level_age_bars":                  bos_level_age_bars,
            "max_bos_lookback_bars":               max_bos_lookback_bars,
            "bos_local_valid":                     True,
            "zone_status":                         "valid",
            "zone_reject_reason":                  "",
            "structure_direction":                   "bearish",
            "structure_bias_before":                 _bias_before,
            "structure_event":                       _structure_event,
            "left_structure_high_count_before_base": left_sh_count,
            "left_structure_low_count_before_base":  left_sl_count,
            "left_structure_valid_before_base":      True,
            "left_sh_local_count":                 len(sh_local),
            "left_sl_local_count":                 len(sl_local),
            "left_sh_local_lookback":              _local_lb,
            "last2_sh_times": [_ts_to_str(candles[i].get("time", 0)) for i in last2_sh],
            "last2_sl_times": [_ts_to_str(candles[i].get("time", 0)) for i in last2_sl],
            "zone_method":                         _ZONE_METHOD,
        }

    _rej_reason = (
        "BOS_LEVEL_OUTSIDE_LOCAL_LOOKBACK"
        if _any_locality_rejected
        else "no_valid_bearish_bos_with_pre_base_swing_low"
    )
    return {
        "found": False,
        "reason": _rej_reason,
        "zone_status": "rejected",
        "zone_reject_reason": _rej_reason,
        "bos_local_valid": False,
        "max_bos_lookback_bars": max_bos_lookback_bars,
    }


def detect_structural_zones_shadow(
    symbol: str = "SPY",
    spx_price: Optional[float] = None,
    spy_price: Optional[float] = None,
) -> Dict[str, Any]:
    """
    RC15j Phase 2B.1 — Shadow-mode structural zone detection for 1H and 15m.

    Zones are detected on SPY candles (DXLink).
    If spx_price and spy_price are provided, zone boundaries are also stored
    in SPX-scaled units using ratio = spx_price / spy_price.
    Raw SPY zone values are kept alongside as _spy_low/_spy_high for audit.

    Shadow mode: logged in journal ONLY. No trade decisions are affected.
    Never raises; any failure is captured in the returned dict.
    """
    # Compute SPX/SPY scaling ratio — must be within safety range [7.0, 14.0]
    _raw_ratio: Optional[float] = None
    _ratio:     Optional[float] = None
    _converted  = False
    _reason     = ""

    if spx_price and spy_price and spy_price > 0:
        _raw_ratio = spx_price / spy_price
        if 7.0 <= _raw_ratio <= 14.0:
            _ratio     = _raw_ratio
            _converted = True
        else:
            _reason = f"INVALID_SPX_SPY_PROXY_RATIO ratio={_raw_ratio:.3f} expected=[7.0,14.0]"
    elif not spx_price or not spy_price:
        _reason = "SPX_OR_SPY_PRICE_MISSING"
    else:
        _reason = "SPY_PRICE_ZERO"

    out: Dict[str, Any] = {
        "struct_shadow_available":  False,
        "struct_shadow_converted":  _converted,
        "struct_shadow_reason":     _reason,
        "struct_shadow_symbol":     symbol,
        "struct_shadow_spy_price":  spy_price,
        "struct_shadow_spx_price":  spx_price,
        "struct_shadow_ratio":      round(_raw_ratio, 4) if _raw_ratio else None,
    }

    def _scale(spy_val: Optional[float]) -> Optional[float]:
        """Scale SPY price → SPX units. Returns None if ratio invalid or missing.
        Never allows raw SPY prices to appear in SPX structural zone fields."""
        if spy_val is None or not _converted or _ratio is None:
            return None
        return round(spy_val * _ratio, 2)

    for tf_label, tf_interval in [("1h", "1h"), ("15m", "15m")]:
        p = f"struct_{tf_label}_"
        try:
            candles, src, _status = _load_orderblock_candles(symbol, "0DTE", "0DTE", tf_interval)
            out[f"{p}candle_count"]  = len(candles)
            out[f"{p}candle_source"] = src

            if not candles:
                out[f"{p}demand_found"] = False
                out[f"{p}supply_found"] = False
                out[f"{p}error"]        = "no_candles"
                continue

            # Phase 2B.2: per-timeframe BOS locality limits
            _max_bos_lb = 40 if tf_label == "15m" else 30
            _local_struct_lb = 100 if tf_label == "15m" else 80
            demand = _detect_structural_demand(
                candles,
                max_bos_lookback_bars=_max_bos_lb,
                local_structure_lookback=_local_struct_lb,
            )
            supply = _detect_structural_supply(
                candles,
                max_bos_lookback_bars=_max_bos_lb,
                local_structure_lookback=_local_struct_lb,
            )

            # Phase 2B.3: mitigation / invalidation shadow lifecycle
            _d_life = _zone_lifecycle(
                candles, demand.get("zone_low"), demand.get("zone_high"),
                demand.get("bos_candle_idx"), "BULLISH",
            ) if demand.get("found", False) else {}
            _s_life = _zone_lifecycle(
                candles, supply.get("zone_low"), supply.get("zone_high"),
                supply.get("bos_candle_idx"), "BEARISH",
            ) if supply.get("found", False) else {}

            # ── Demand ──────────────────────────────────────────────────────
            _d_lo = demand.get("zone_low")
            _d_hi = demand.get("zone_high")
            _d_bos = demand.get("bos_level_broken")
            out[f"{p}demand_found"]       = demand.get("found", False)
            # SPX-scaled (primary for analysis)
            out[f"{p}demand_low"]         = _scale(_d_lo)
            out[f"{p}demand_high"]        = _scale(_d_hi)
            out[f"{p}demand_bos_lvl"]     = _scale(_d_bos)
            # Raw SPY values for audit
            out[f"{p}demand_spy_low"]     = _d_lo
            out[f"{p}demand_spy_high"]    = _d_hi
            out[f"{p}demand_spy_bos_lvl"] = _d_bos
            # Timing / structure diagnostics
            out[f"{p}demand_base_t"]      = demand.get("base_candle_time", "")
            out[f"{p}demand_base_idx"]    = demand.get("base_candle_idx")
            out[f"{p}demand_bos_t"]       = demand.get("bos_candle_time", "")
            out[f"{p}demand_bos_idx"]     = demand.get("bos_candle_idx")
            out[f"{p}demand_sh_cnt"]      = demand.get("left_structure_high_count_before_base")
            out[f"{p}demand_sl_cnt"]      = demand.get("left_structure_low_count_before_base")
            out[f"{p}demand_struct"]      = demand.get("left_structure_valid_before_base", False)

            # ── Demand extra diagnostics ─────────────────────────────────
            out[f"{p}demand_bos_candle_close"]  = demand.get("bos_candle_close")
            out[f"{p}demand_sh_local_cnt"]  = demand.get("left_sh_local_count")
            out[f"{p}demand_sl_local_cnt"]  = demand.get("left_sl_local_count")
            out[f"{p}demand_local_lookback"] = demand.get("left_sh_local_lookback")
            out[f"{p}demand_last2_sh_t"]    = "|".join(demand.get("last2_sh_times", []))
            out[f"{p}demand_last2_sl_t"]    = "|".join(demand.get("last2_sl_times", []))

            # ── Demand Phase 2B.2 — BOS locality diagnostics ─────────────
            out[f"{p}demand_bos_level_idx"]        = demand.get("bos_level_idx")
            out[f"{p}demand_bos_level_time"]       = demand.get("bos_level_time", "")
            out[f"{p}demand_bos_level_age_bars"]   = demand.get("bos_level_age_bars")
            out[f"{p}demand_max_bos_lookback_bars"]= demand.get("max_bos_lookback_bars", _max_bos_lb)
            out[f"{p}demand_bos_local_valid"]      = demand.get("bos_local_valid", False)
            out[f"{p}demand_zone_status"]          = demand.get("zone_status", "")
            out[f"{p}demand_zone_reject_reason"]   = demand.get("zone_reject_reason", "")

            # ── Demand Phase 2B.3 / 2B.4 — lifecycle + BOS/CHoCH logging ───
            out[f"{p}demand_mitigation_status"]    = _d_life.get("mitigation_status", "")
            out[f"{p}demand_tested"]               = _d_life.get("tested", False)
            out[f"{p}demand_mitigated"]            = _d_life.get("mitigated", False)
            out[f"{p}demand_invalidated"]          = _d_life.get("invalidated", False)
            out[f"{p}demand_touches"]              = _d_life.get("touches")
            out[f"{p}demand_last_touch_idx"]       = _d_life.get("last_touch_idx")
            out[f"{p}demand_last_touch_time"]      = _d_life.get("last_touch_time", "")
            out[f"{p}demand_invalidated_idx"]      = _d_life.get("invalidated_idx")
            out[f"{p}demand_invalidated_time"]     = _d_life.get("invalidated_time", "")
            out[f"{p}demand_structure_event"]      = demand.get("structure_event", "")
            out[f"{p}demand_structure_direction"]  = demand.get("structure_direction", "")
            out[f"{p}demand_structure_bias_before"]= demand.get("structure_bias_before", "")

            # ── Supply ──────────────────────────────────────────────────────
            _s_lo = supply.get("zone_low")
            _s_hi = supply.get("zone_high")
            _s_bos = supply.get("bos_level_broken")
            out[f"{p}supply_found"]       = supply.get("found", False)
            out[f"{p}supply_low"]         = _scale(_s_lo)
            out[f"{p}supply_high"]        = _scale(_s_hi)
            out[f"{p}supply_bos_lvl"]     = _scale(_s_bos)
            out[f"{p}supply_spy_low"]     = _s_lo
            out[f"{p}supply_spy_high"]    = _s_hi
            out[f"{p}supply_spy_bos_lvl"] = _s_bos
            out[f"{p}supply_base_t"]      = supply.get("base_candle_time", "")
            out[f"{p}supply_base_idx"]    = supply.get("base_candle_idx")
            out[f"{p}supply_bos_t"]       = supply.get("bos_candle_time", "")
            out[f"{p}supply_bos_idx"]     = supply.get("bos_candle_idx")
            out[f"{p}supply_sh_cnt"]      = supply.get("left_structure_high_count_before_base")
            out[f"{p}supply_sl_cnt"]      = supply.get("left_structure_low_count_before_base")
            out[f"{p}supply_struct"]      = supply.get("left_structure_valid_before_base", False)

            # ── Supply extra diagnostics ─────────────────────────────────
            out[f"{p}supply_bos_candle_close"]  = supply.get("bos_candle_close")
            out[f"{p}supply_sh_local_cnt"]  = supply.get("left_sh_local_count")
            out[f"{p}supply_sl_local_cnt"]  = supply.get("left_sl_local_count")
            out[f"{p}supply_local_lookback"] = supply.get("left_sh_local_lookback")
            out[f"{p}supply_last2_sh_t"]    = "|".join(supply.get("last2_sh_times", []))
            out[f"{p}supply_last2_sl_t"]    = "|".join(supply.get("last2_sl_times", []))

            # ── Supply Phase 2B.2 — BOS locality diagnostics ─────────────
            out[f"{p}supply_bos_level_idx"]        = supply.get("bos_level_idx")
            out[f"{p}supply_bos_level_time"]       = supply.get("bos_level_time", "")
            out[f"{p}supply_bos_level_age_bars"]   = supply.get("bos_level_age_bars")
            out[f"{p}supply_max_bos_lookback_bars"]= supply.get("max_bos_lookback_bars", _max_bos_lb)
            out[f"{p}supply_bos_local_valid"]      = supply.get("bos_local_valid", False)
            out[f"{p}supply_zone_status"]          = supply.get("zone_status", "")
            out[f"{p}supply_zone_reject_reason"]   = supply.get("zone_reject_reason", "")

            # ── Supply Phase 2B.3 / 2B.4 — lifecycle + BOS/CHoCH logging ───
            out[f"{p}supply_mitigation_status"]    = _s_life.get("mitigation_status", "")
            out[f"{p}supply_tested"]               = _s_life.get("tested", False)
            out[f"{p}supply_mitigated"]            = _s_life.get("mitigated", False)
            out[f"{p}supply_invalidated"]          = _s_life.get("invalidated", False)
            out[f"{p}supply_touches"]              = _s_life.get("touches")
            out[f"{p}supply_last_touch_idx"]       = _s_life.get("last_touch_idx")
            out[f"{p}supply_last_touch_time"]      = _s_life.get("last_touch_time", "")
            out[f"{p}supply_invalidated_idx"]      = _s_life.get("invalidated_idx")
            out[f"{p}supply_invalidated_time"]     = _s_life.get("invalidated_time", "")
            out[f"{p}supply_structure_event"]      = supply.get("structure_event", "")
            out[f"{p}supply_structure_direction"]  = supply.get("structure_direction", "")
            out[f"{p}supply_structure_bias_before"]= supply.get("structure_bias_before", "")

            # ── Conflict diagnostics — 4 filters (shadow only) ──────────
            # Shadow mode: these flags do NOT affect any trade decision yet.
            # Future activation: any conflict = PUT_DEBIT_BLOCKED_STRUCTURAL_CONFLICT
            _d_found  = demand.get("found", False)
            _s_found  = supply.get("found", False)
            _d_lo_spx = out.get(f"{p}demand_low")
            _d_hi_spx = out.get(f"{p}demand_high")
            _s_lo_spx = out.get(f"{p}supply_low")
            _s_hi_spx = out.get(f"{p}supply_high")
            _px       = spx_price if spx_price else 0.0

            # Per-timeframe thresholds
            _min_clean_gap = 20.0 if tf_label == "15m" else 35.0   # SPX points
            _max_bos_dist  = 50.0 if tf_label == "15m" else 125.0  # SPX points

            conflict_reasons: List[str] = []
            zone_state_reasons: List[str] = []

            # ── BOS stale/distance diagnostics first ─────────────────────
            # These are zone-quality diagnostics. They are not a trade block by
            # themselves, but stale zones are not considered active for future
            # activation readiness checks.
            _d_bos_cls_spx = _scale(demand.get("bos_candle_close"))
            _s_bos_cls_spx = _scale(supply.get("bos_candle_close"))
            _d_bos_lvl_spx = out.get(f"{p}demand_bos_lvl")
            _s_bos_lvl_spx = out.get(f"{p}supply_bos_lvl")

            _d_stale = False
            _s_stale = False
            if (_d_found and _d_bos_cls_spx is not None
                    and _d_bos_lvl_spx is not None):
                _dist_d = abs(_d_bos_cls_spx - _d_bos_lvl_spx)
                if _dist_d > _max_bos_dist:
                    _d_stale = True
                    zone_state_reasons.append(
                        f"STALE_DEMAND_BOS dist={_dist_d:.0f}>{_max_bos_dist:.0f}pts"
                    )
            if (_s_found and _s_bos_cls_spx is not None
                    and _s_bos_lvl_spx is not None):
                _dist_s = abs(_s_bos_cls_spx - _s_bos_lvl_spx)
                if _dist_s > _max_bos_dist:
                    _s_stale = True
                    zone_state_reasons.append(
                        f"STALE_SUPPLY_BOS dist={_dist_s:.0f}>{_max_bos_dist:.0f}pts"
                    )

            _d_invalid = bool(_d_life.get("invalidated", False))
            _s_invalid = bool(_s_life.get("invalidated", False))
            if _d_found and _d_invalid:
                zone_state_reasons.append("DEMAND_ZONE_INVALIDATED")
            if _s_found and _s_invalid:
                zone_state_reasons.append("SUPPLY_ZONE_INVALIDATED")

            _d_active = bool(
                _d_found
                and demand.get("zone_status", "") == "valid"
                and demand.get("bos_local_valid", False)
                and not _d_invalid
                and not _d_stale
                and _d_lo_spx is not None
                and _d_hi_spx is not None
            )
            _s_active = bool(
                _s_found
                and supply.get("zone_status", "") == "valid"
                and supply.get("bos_local_valid", False)
                and not _s_invalid
                and not _s_stale
                and _s_lo_spx is not None
                and _s_hi_spx is not None
            )

            # ── Raw price location diagnostics (legacy/audit) ──────────────
            _price_in_d = bool(
                _d_found and _d_lo_spx is not None and _d_hi_spx is not None
                and _d_lo_spx <= _px <= _d_hi_spx
            )
            _price_in_s = bool(
                _s_found and _s_lo_spx is not None and _s_hi_spx is not None
                and _s_lo_spx <= _px <= _s_hi_spx
            )

            # ── Active-zone price location diagnostics for future activation
            _price_in_active_d = bool(
                _d_active and _d_lo_spx is not None and _d_hi_spx is not None
                and _d_lo_spx <= _px <= _d_hi_spx
            )
            _price_in_active_s = bool(
                _s_active and _s_lo_spx is not None and _s_hi_spx is not None
                and _s_lo_spx <= _px <= _s_hi_spx
            )

            # ── Filter 1: active Demand/Supply overlap ────────────────────
            _overlap  = False
            _ovlp_lo: Optional[float] = None
            _ovlp_hi: Optional[float] = None
            if _d_active and _s_active:
                _ovlp_lo_c = max(_d_lo_spx, _s_lo_spx)  # type: ignore[arg-type]
                _ovlp_hi_c = min(_d_hi_spx, _s_hi_spx)  # type: ignore[arg-type]
                if _ovlp_lo_c < _ovlp_hi_c:
                    _overlap = True
                    _ovlp_lo = round(_ovlp_lo_c, 2)
                    _ovlp_hi = round(_ovlp_hi_c, 2)
                    conflict_reasons.append("OVERLAPPING_ACTIVE_DEMAND_SUPPLY")

            if _price_in_active_d and _price_in_active_s:
                conflict_reasons.append("PRICE_INSIDE_ACTIVE_DEMAND_AND_SUPPLY")
            elif _price_in_active_d:
                conflict_reasons.append("PRICE_INSIDE_ACTIVE_DEMAND")

            # ── Filter 2: active-zone compression ─────────────────────────
            _clean_gap: Optional[float] = None
            if (_d_active and _s_active and _d_hi_spx is not None
                    and _s_lo_spx is not None and not _overlap):
                _clean_gap = round(_s_lo_spx - _d_hi_spx, 2)
                if _clean_gap < _min_clean_gap:
                    conflict_reasons.append(
                        f"COMPRESSED_ACTIVE_DEMAND_SUPPLY gap={_clean_gap:.1f}<{_min_clean_gap:.0f}pts"
                    )

            # ── Filter 3: same BOS candle between two active zones ─────────
            _d_bos_t = demand.get("bos_candle_time", "")
            _s_bos_t = supply.get("bos_candle_time", "")
            if _d_active and _s_active and _d_bos_t and _d_bos_t == _s_bos_t:
                conflict_reasons.append("SAME_BOS_CANDLE")

            _conflict        = len(conflict_reasons) > 0
            _conflict_reason = "|".join(conflict_reasons)
            _zone_state_reason = "|".join(zone_state_reasons)

            out[f"{p}demand_supply_overlap"]         = _overlap
            out[f"{p}overlap_low"]                   = _ovlp_lo
            out[f"{p}overlap_high"]                  = _ovlp_hi
            out[f"{p}price_in_demand"]               = _price_in_d
            out[f"{p}price_in_supply"]               = _price_in_s
            out[f"{p}price_in_active_demand"]        = _price_in_active_d
            out[f"{p}price_in_active_supply"]        = _price_in_active_s
            out[f"{p}demand_active_valid"]           = _d_active
            out[f"{p}supply_active_valid"]           = _s_active
            out[f"{p}zone_state_reason"]             = _zone_state_reason
            out[f"{p}clean_gap"]                     = _clean_gap
            out[f"{p}min_clean_gap_pts"]             = _min_clean_gap
            out[f"{p}demand_bos_close_spx"]          = _d_bos_cls_spx
            out[f"{p}supply_bos_close_spx"]          = _s_bos_cls_spx
            out[f"{p}demand_bos_stale"]              = _d_stale
            out[f"{p}supply_bos_stale"]              = _s_stale
            out[f"{p}max_bos_dist_pts"]              = _max_bos_dist

            # Shadow entry-location diagnostics. These do not affect trading yet.
            _entry_thr = 25.0 if tf_label == "15m" else 50.0
            _dist_d_zone = _signed_distance_to_zone(_px, _d_lo_spx, _d_hi_spx)
            _dist_s_zone = _signed_distance_to_zone(_px, _s_lo_spx, _s_hi_spx)
            out[f"{p}entry_distance_threshold_pts"]  = _entry_thr
            out[f"{p}distance_to_demand_zone"]       = _dist_d_zone
            out[f"{p}distance_to_supply_zone"]       = _dist_s_zone
            out[f"{p}price_at_demand_valid"]         = bool(_d_found and _dist_d_zone is not None and abs(_dist_d_zone) <= _entry_thr)
            out[f"{p}price_at_supply_valid"]         = bool(_s_found and _dist_s_zone is not None and abs(_dist_s_zone) <= _entry_thr)
            out[f"{p}price_at_active_demand_valid"]  = bool(_d_active and _dist_d_zone is not None and abs(_dist_d_zone) <= _entry_thr)
            out[f"{p}price_at_active_supply_valid"]  = bool(_s_active and _dist_s_zone is not None and abs(_dist_s_zone) <= _entry_thr)

            # Future-activation preview for Put Debit only; still shadow only.
            # Priority: conflict > no active supply > not at active supply > location OK.
            if _conflict:
                _pd_allowed = False
                _pd_code = "PUT_DEBIT_BLOCKED_STRUCTURAL_CONFLICT"
                _pd_reason = _conflict_reason
            elif not _s_active:
                _pd_allowed = False
                _pd_code = "PUT_DEBIT_BLOCKED_NO_ACTIVE_SUPPLY"
                _pd_reason = _zone_state_reason or "no_valid_active_supply_zone"
            elif not bool(_s_active and _dist_s_zone is not None and abs(_dist_s_zone) <= _entry_thr):
                _pd_allowed = False
                _pd_code = "PUT_DEBIT_BLOCKED_NOT_AT_ACTIVE_SUPPLY"
                _pd_reason = f"distance_to_supply_zone={_dist_s_zone}; threshold={_entry_thr}"
            else:
                _pd_allowed = True
                _pd_code = "STRUCTURAL_LOCATION_OK"
                _pd_reason = "active_supply_near_price_and_no_active_demand_conflict"

            out[f"{p}put_debit_shadow_allowed"]      = _pd_allowed
            out[f"{p}put_debit_shadow_block_code"]   = _pd_code
            out[f"{p}put_debit_shadow_block_reason"] = _pd_reason

            out[f"{p}structural_conflict"]           = _conflict
            out[f"{p}structural_conflict_reason"]    = _conflict_reason

            out["struct_shadow_available"] = True

        except Exception as exc:
            out[f"{p}error"] = f"{type(exc).__name__}:{str(exc)[:80]}"

    # Overall SPX 0DTE Put Debit structural preview (shadow only).
    # 15m is the execution timeframe; 1H is protective context.
    try:
        _summary_allowed = False
        _summary_code = "STRUCTURAL_SHADOW_UNAVAILABLE"
        _summary_reason = "structural shadow fields unavailable"
        _primary_tf = "15m"
        _context_tf = "1h"

        if not out.get("struct_shadow_available", False):
            _summary_code = "STRUCTURAL_SHADOW_UNAVAILABLE"
            _summary_reason = out.get("struct_shadow_reason") or "no_structural_shadow"
        elif not out.get("struct_shadow_converted", False):
            _summary_code = "STRUCTURAL_SHADOW_NOT_CONVERTED"
            _summary_reason = out.get("struct_shadow_reason") or "SPX/SPY conversion unavailable"
        elif out.get("struct_15m_structural_conflict", False):
            _summary_code = "PUT_DEBIT_BLOCKED_STRUCTURAL_CONFLICT_15M"
            _summary_reason = out.get("struct_15m_structural_conflict_reason") or "15m structural conflict"
        elif out.get("struct_1h_price_in_active_demand", False):
            _summary_code = "PUT_DEBIT_BLOCKED_1H_ACTIVE_DEMAND"
            _summary_reason = "SPX price is inside active 1H Demand"
        elif not out.get("struct_15m_supply_active_valid", False):
            _summary_code = "PUT_DEBIT_BLOCKED_NO_ACTIVE_15M_SUPPLY"
            _summary_reason = out.get("struct_15m_zone_state_reason") or "no active 15m Supply"
        elif not out.get("struct_15m_price_at_active_supply_valid", False):
            _summary_code = "PUT_DEBIT_BLOCKED_NOT_AT_ACTIVE_15M_SUPPLY"
            _summary_reason = (
                f"distance_to_supply_zone={out.get('struct_15m_distance_to_supply_zone')}; "
                f"threshold={out.get('struct_15m_entry_distance_threshold_pts')}"
            )
        else:
            _summary_allowed = True
            _summary_code = "STRUCTURAL_PUT_DEBIT_LOCATION_OK"
            _summary_reason = "15m active Supply is near price and no active 15m Demand conflict"

        out["struct_put_debit_shadow_allowed"] = _summary_allowed
        out["struct_put_debit_shadow_block_code"] = _summary_code
        out["struct_put_debit_shadow_block_reason"] = _summary_reason
        out["struct_put_debit_shadow_primary_tf"] = _primary_tf
        out["struct_put_debit_shadow_context_tf"] = _context_tf
    except Exception as exc:
        out["struct_put_debit_shadow_allowed"] = False
        out["struct_put_debit_shadow_block_code"] = "STRUCTURAL_SHADOW_SUMMARY_ERROR"
        out["struct_put_debit_shadow_block_reason"] = f"{type(exc).__name__}:{str(exc)[:80]}"
        out["struct_put_debit_shadow_primary_tf"] = "15m"
        out["struct_put_debit_shadow_context_tf"] = "1h"

    out["struct_shadow_zone_method"] = _ZONE_METHOD
    return out

