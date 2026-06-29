"""
SMC Lite Engine (Phase 1.1 - Multi-Timeframe)

Purpose:
    Adds a lightweight Smart Money Concepts filter to the paper bot without
    making it a hard entry/exit rule. It computes:
      - BOS / CHoCH style market-structure bias
      - Premium / Discount location
      - simple liquidity sweep detection
      - multi-timeframe confirmation for higher-quality context
      - an SMC score adjustment for option strategies

Profiles:
    0DTE  : HTF=1H trend, LTF=15m confirmation
    Swing : HTF=1D trend, LTF=4H confirmation (built by resampling Yahoo 1H candles)

Design principle:
    SMC Lite should adjust score only. It should not reject trades by itself.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
import time
import requests

try:
    from core.circuit_breaker import yahoo_breaker, CircuitOpenError
except Exception:  # pragma: no cover
    yahoo_breaker = None
    class CircuitOpenError(Exception):
        pass


_YAHOO_OHLC_CACHE: Dict[Tuple[str, str, str], Tuple[float, List[Dict[str, float]]]] = {}
_CACHE_TTL_SECONDS = 180
_YAHOO_STATUS: Dict[str, Any] = {
    "ok": None,
    "state": "UNKNOWN",
    "last_success": None,
    "last_error": None,
    "last_ticker": None,
    "last_period": None,
    "last_interval": None,
    "last_attempt": None,
}


def get_yahoo_ohlc_status() -> Dict[str, Any]:
    """Return diagnostic status for Yahoo OHLC candles used by SMC/Order Blocks."""
    out = dict(_YAHOO_STATUS)
    try:
        if out.get("last_success"):
            out["last_success_age_seconds"] = round(time.time() - float(out["last_success"]), 1)
        else:
            out["last_success_age_seconds"] = None
    except Exception:
        out["last_success_age_seconds"] = None
    return out


def _set_yahoo_status(ok: bool, state: str, ticker: str, period: str, interval: str, error: Optional[str] = None) -> None:
    now = time.time()
    _YAHOO_STATUS.update({
        "ok": bool(ok),
        "state": state,
        "last_ticker": ticker,
        "last_period": period,
        "last_interval": interval,
        "last_attempt": now,
    })
    if ok:
        _YAHOO_STATUS["last_success"] = now
        _YAHOO_STATUS["last_error"] = None
    elif error:
        _YAHOO_STATUS["last_error"] = error



def _to_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _yahoo_ticker(symbol: str) -> str:
    s = (symbol or "").upper()
    if s == "SPX":
        return "^GSPC"
    return s


def fetch_yahoo_ohlc(symbol: str, period: str, interval: str) -> List[Dict[str, float]]:
    """Fetch OHLC candles from Yahoo chart API. Returns list of dicts."""
    ticker = _yahoo_ticker(symbol)
    key = (ticker, period, interval)
    now = time.time()
    if key in _YAHOO_OHLC_CACHE and now - _YAHOO_OHLC_CACHE[key][0] < _CACHE_TTL_SECONDS:
        return _YAHOO_OHLC_CACHE[key][1]

    candles: List[Dict[str, float]] = []
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker, safe='')}"
        def _request():
            return requests.get(
                url,
                params={"range": period, "interval": interval},
                timeout=8,
                headers={"User-Agent": ua, "Accept": "application/json"},
            )
        if yahoo_breaker is not None:
            with yahoo_breaker():
                resp = _request()
        else:
            resp = _request()
        if resp.status_code == 200:
            result = (resp.json().get("chart", {}).get("result") or [None])[0]
            if result:
                timestamps = result.get("timestamp") or []
                q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
                opens = q.get("open") or []
                highs = q.get("high") or []
                lows = q.get("low") or []
                closes = q.get("close") or []
                n = min(len(timestamps), len(opens), len(highs), len(lows), len(closes))
                for i in range(n):
                    o, h, l, c = map(_to_float, (opens[i], highs[i], lows[i], closes[i]))
                    if None in (o, h, l, c):
                        continue
                    candles.append({"time": float(timestamps[i]), "open": o, "high": h, "low": l, "close": c})
            if candles:
                _set_yahoo_status(True, "OK", ticker, period, interval)
            else:
                _set_yahoo_status(False, "EMPTY", ticker, period, interval, "empty OHLC response")
        else:
            _set_yahoo_status(False, f"HTTP_{resp.status_code}", ticker, period, interval, f"HTTP {resp.status_code}")
    except CircuitOpenError as exc:
        candles = []
        _set_yahoo_status(False, "CIRCUIT_OPEN", ticker, period, interval, str(exc))
    except Exception as exc:
        candles = []
        _set_yahoo_status(False, "FAILED", ticker, period, interval, f"{type(exc).__name__}: {str(exc)[:120]}")
    _YAHOO_OHLC_CACHE[key] = (now, candles)
    return candles


def _resample_candles(candles: List[Dict[str, float]], seconds: int) -> List[Dict[str, float]]:
    """Resample timestamped candles to a larger fixed interval, e.g. 1H -> 4H."""
    if not candles or seconds <= 0:
        return []
    buckets: Dict[int, Dict[str, float]] = {}
    for b in candles:
        ts = int(b.get("time", 0))
        bucket = ts - (ts % seconds)
        if bucket not in buckets:
            buckets[bucket] = {
                "time": float(bucket),
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
            }
        else:
            x = buckets[bucket]
            x["high"] = max(x["high"], float(b["high"]))
            x["low"] = min(x["low"], float(b["low"]))
            x["close"] = float(b["close"])
    return [buckets[k] for k in sorted(buckets)]


def _pivots(candles: List[Dict[str, float]], length: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    highs: List[Dict[str, Any]] = []
    lows: List[Dict[str, Any]] = []
    if len(candles) < length * 2 + 5:
        return highs, lows
    for i in range(length, len(candles) - length):
        h = candles[i]["high"]
        l = candles[i]["low"]
        left = candles[i - length:i]
        right = candles[i + 1:i + length + 1]
        if h >= max(x["high"] for x in left + right):
            highs.append({"idx": i, "price": h, "time": candles[i].get("time")})
        if l <= min(x["low"] for x in left + right):
            lows.append({"idx": i, "price": l, "time": candles[i].get("time")})
    return highs, lows


def _last_structure_event(candles: List[Dict[str, float]], highs: List[Dict[str, Any]], lows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Simplified BOS/CHoCH detector based on confirmed pivots and close breaks."""
    piv_hi_by_idx = {p["idx"]: p for p in highs}
    piv_lo_by_idx = {p["idx"]: p for p in lows}
    last_high: Optional[Dict[str, Any]] = None
    last_low: Optional[Dict[str, Any]] = None
    bias = "neutral"
    event = {"type": "None", "bias": "neutral", "level": None, "idx": None}

    for i, bar in enumerate(candles):
        if i in piv_hi_by_idx:
            last_high = piv_hi_by_idx[i]
        if i in piv_lo_by_idx:
            last_low = piv_lo_by_idx[i]

        c = bar["close"]
        if last_high and c > last_high["price"]:
            tag = "Bullish BOS" if bias == "bullish" else "Bullish CHoCH"
            bias = "bullish"
            event = {"type": tag, "bias": "bullish", "level": round(last_high["price"], 2), "idx": i}
            last_high = None
        elif last_low and c < last_low["price"]:
            tag = "Bearish BOS" if bias == "bearish" else "Bearish CHoCH"
            bias = "bearish"
            event = {"type": tag, "bias": "bearish", "level": round(last_low["price"], 2), "idx": i}
            last_low = None

    return event


def _premium_discount(candles: List[Dict[str, float]], lookback: int) -> Dict[str, Any]:
    subset = candles[-lookback:] if len(candles) >= lookback else candles
    if not subset:
        return {"zone": "unknown", "range_high": None, "range_low": None, "equilibrium": None}
    hi = max(x["high"] for x in subset)
    lo = min(x["low"] for x in subset)
    eq = (hi + lo) / 2.0
    price = candles[-1]["close"]
    width = max(hi - lo, 1e-9)
    dist_eq = abs(price - eq) / width
    if dist_eq < 0.08:
        zone = "equilibrium"
    elif price > eq:
        zone = "premium"
    else:
        zone = "discount"
    return {"zone": zone, "range_high": round(hi, 2), "range_low": round(lo, 2), "equilibrium": round(eq, 2)}


def _recent_sweep(candles: List[Dict[str, float]], highs: List[Dict[str, Any]], lows: List[Dict[str, Any]], lookback_bars: int) -> Dict[str, Any]:
    if not candles:
        return {"type": "None", "bias": "neutral", "level": None}
    start = max(0, len(candles) - lookback_bars)
    prior_highs = [p for p in highs if p["idx"] < start]
    prior_lows = [p for p in lows if p["idx"] < start]
    if not prior_highs and not prior_lows:
        return {"type": "None", "bias": "neutral", "level": None}

    recent = candles[start:]
    last_hi = prior_highs[-1] if prior_highs else None
    last_lo = prior_lows[-1] if prior_lows else None
    out = {"type": "None", "bias": "neutral", "level": None}
    for bar in recent:
        if last_hi and bar["high"] > last_hi["price"] and bar["close"] < last_hi["price"]:
            out = {"type": "BSL Sweep", "bias": "bearish", "level": round(last_hi["price"], 2)}
        if last_lo and bar["low"] < last_lo["price"] and bar["close"] > last_lo["price"]:
            out = {"type": "SSL Sweep", "bias": "bullish", "level": round(last_lo["price"], 2)}
    return out


def _load_candles_for_tf(symbol: str, period: str, interval: str) -> List[Dict[str, float]]:
    """Load candles. Supports synthetic 4h via 1h Yahoo data."""
    if interval in ("4h", "4H"):
        # Yahoo does not reliably support 4h directly. Build it from 1h.
        return _resample_candles(fetch_yahoo_ohlc(symbol, period, "1h"), 4 * 60 * 60)
    return fetch_yahoo_ohlc(symbol, period, interval)


def _analyze_single_tf(symbol: str, period: str, interval: str, pivot_len: int,
                       pd_lookback: int, sweep_lookback: int, label: str) -> Dict[str, Any]:
    candles = _load_candles_for_tf(symbol, period, interval)
    if not candles or len(candles) < pivot_len * 2 + 10:
        ys = get_yahoo_ohlc_status()
        reason = "insufficient OHLC data"
        source = "yahoo_empty"
        if ys.get("ok") is False:
            reason = f"Yahoo OHLC unavailable: {ys.get('state')}"
            source = "yahoo_failed"
        return {
            "available": False,
            "label": label,
            "timeframe": interval,
            "bias": "neutral",
            "last_structure": "Unavailable",
            "zone": "unknown",
            "recent_sweep": "None",
            "reason": reason,
            "source": source,
            "yahoo_status": ys,
            "candles": len(candles or []),
        }
    highs, lows = _pivots(candles, pivot_len)
    struct = _last_structure_event(candles, highs, lows)
    pd = _premium_discount(candles, pd_lookback)
    sw = _recent_sweep(candles, highs, lows, sweep_lookback)
    bias = struct.get("bias") or "neutral"
    if bias not in ("bullish", "bearish"):
        bias = "neutral"
    return {
        "available": True,
        "label": label,
        "timeframe": interval,
        "pivot_length": pivot_len,
        "bias": bias,
        "last_structure": struct.get("type") or "None",
        "structure_level": struct.get("level"),
        "zone": pd.get("zone", "unknown"),
        "range_high": pd.get("range_high"),
        "range_low": pd.get("range_low"),
        "equilibrium": pd.get("equilibrium"),
        "recent_sweep": sw.get("type") or "None",
        "sweep_bias": sw.get("bias", "neutral"),
        "sweep_level": sw.get("level"),
        "candles": len(candles),
    }


def _combined_bias(htf: Dict[str, Any], ltf: Dict[str, Any]) -> str:
    hb = htf.get("bias", "neutral")
    lb = ltf.get("bias", "neutral")
    if hb in ("bullish", "bearish") and lb == hb:
        return hb
    if hb in ("bullish", "bearish") and lb == "neutral":
        return hb
    if hb == "neutral" and lb in ("bullish", "bearish"):
        return lb
    if hb in ("bullish", "bearish") and lb in ("bullish", "bearish") and hb != lb:
        return "mixed"
    return "neutral"


def analyze_smc_lite(symbol: str, mode: str = "0DTE", price: Optional[float] = None) -> Dict[str, Any]:
    """Return SMC context for symbol/mode using higher timeframe + confirmation timeframe."""
    mode_u = (mode or "0DTE").upper()
    if mode_u == "SWING":
        profile = "SMC_Swing_MTF_Profile"
        # Higher timeframe = Daily trend. Lower timeframe = 4H confirmation.
        htf = _analyze_single_tf(symbol, "1y", "1d", 20, 160, 8, "HTF")
        ltf = _analyze_single_tf(symbol, "120d", "4h", 12, 100, 8, "LTF")
        htf_weight, ltf_weight = 20, 10
    else:
        profile = "SMC_0DTE_MTF_Profile"
        # Higher timeframe = 1H trend. Lower timeframe = 15m confirmation.
        htf = _analyze_single_tf(symbol, "30d", "1h", 12, 120, 8, "HTF")
        ltf = _analyze_single_tf(symbol, "10d", "15m", 10, 80, 8, "LTF")
        htf_weight, ltf_weight = 15, 10

    available = bool(htf.get("available") or ltf.get("available"))
    if not available:
        return {
            "available": False,
            "profile": profile,
            "bias": "neutral",
            "last_structure": "Unavailable",
            "zone": "unknown",
            "recent_sweep": "None",
            "smc_score": 0,
            "reason": f"HTF: {htf.get('reason')}; LTF: {ltf.get('reason')}",
            "source": "yahoo_failed" if (htf.get("source") == "yahoo_failed" or ltf.get("source") == "yahoo_failed") else "neutral_fallback",
            "yahoo_status": get_yahoo_ohlc_status(),
            "htf": htf,
            "ltf": ltf,
        }

    combo = _combined_bias(htf, ltf)
    # Use HTF zone for strategic location when available; otherwise fallback to LTF.
    zone = htf.get("zone") if htf.get("zone") not in (None, "unknown") else ltf.get("zone", "unknown")
    # Sweeps are more useful on the confirmation timeframe. HTF sweep is kept for diagnostics.
    sweep_bias = ltf.get("sweep_bias", "neutral") if ltf.get("available") else htf.get("sweep_bias", "neutral")
    recent_sweep = ltf.get("recent_sweep", "None") if ltf.get("available") else htf.get("recent_sweep", "None")
    sweep_level = ltf.get("sweep_level") if ltf.get("available") else htf.get("sweep_level")

    return {
        "available": True,
        "profile": profile,
        "timeframe": f"{htf.get('timeframe','?')}/{ltf.get('timeframe','?')}",
        "bias": combo,
        "htf_bias": htf.get("bias", "neutral"),
        "ltf_bias": ltf.get("bias", "neutral"),
        "htf_timeframe": htf.get("timeframe"),
        "ltf_timeframe": ltf.get("timeframe"),
        "htf_last_structure": htf.get("last_structure"),
        "ltf_last_structure": ltf.get("last_structure"),
        "last_structure": f"HTF {htf.get('last_structure','—')} / LTF {ltf.get('last_structure','—')}",
        "zone": zone,
        "htf_zone": htf.get("zone", "unknown"),
        "ltf_zone": ltf.get("zone", "unknown"),
        "range_high": htf.get("range_high") or ltf.get("range_high"),
        "range_low": htf.get("range_low") or ltf.get("range_low"),
        "equilibrium": htf.get("equilibrium") or ltf.get("equilibrium"),
        "recent_sweep": recent_sweep,
        "sweep_bias": sweep_bias,
        "sweep_level": sweep_level,
        "htf_recent_sweep": htf.get("recent_sweep", "None"),
        "ltf_recent_sweep": ltf.get("recent_sweep", "None"),
        "htf_weight": htf_weight,
        "ltf_weight": ltf_weight,
        "smc_score": 0,
        "source": "yahoo",
        "yahoo_status": get_yahoo_ohlc_status(),
        "htf": htf,
        "ltf": ltf,
    }


def _strategy_direction(strategy_name: str) -> str:
    name = (strategy_name or "").lower()
    if any(k in name for k in ("call debit", "bull put", "put credit")):
        return "bullish"
    if any(k in name for k in ("put debit", "bear call", "call credit")):
        return "bearish"
    if "iron condor" in name:
        return "neutral"
    return "neutral"


def _bias_score(bias: str, direction: str, weight: int, label: str, reasons: List[str], warnings: List[str]) -> int:
    if direction not in ("bullish", "bearish"):
        return 0
    if bias == direction:
        reasons.append(f"SMC MTF: {label} bias {bias} supports setup (+{weight})")
        return weight
    if bias in ("bullish", "bearish") and bias != direction:
        warnings.append(f"SMC MTF: {label} bias {bias} conflicts with setup (-{weight})")
        return -weight
    return 0


def apply_smc_to_strategy(strategy: Optional[Dict[str, Any]], smc: Dict[str, Any]) -> Dict[str, Any]:
    """Attach SMC data and adjust strategy score. Never rejects by itself."""
    if not strategy:
        return {}
    smc = smc or {"available": False, "bias": "neutral", "smc_score": 0}
    name = strategy.get("strategy", "")
    direction = _strategy_direction(name)
    zone = smc.get("zone", "unknown")
    sweep_bias = smc.get("sweep_bias", "neutral")
    htf_bias = smc.get("htf_bias", smc.get("bias", "neutral"))
    ltf_bias = smc.get("ltf_bias", "neutral")
    htf_weight = int(smc.get("htf_weight", 15) or 15)
    ltf_weight = int(smc.get("ltf_weight", 10) or 10)
    adj = 0
    reasons: List[str] = []
    warnings: List[str] = []

    if direction in ("bullish", "bearish"):
        adj += _bias_score(htf_bias, direction, htf_weight, "HTF", reasons, warnings)
        adj += _bias_score(ltf_bias, direction, ltf_weight, "LTF", reasons, warnings)

        # If HTF and LTF conflict, add a small extra caution penalty.
        if htf_bias in ("bullish", "bearish") and ltf_bias in ("bullish", "bearish") and htf_bias != ltf_bias:
            adj -= 5
            warnings.append("SMC MTF: HTF/LTF conflict; lower confidence (-5)")

        if direction == "bullish":
            if zone == "discount":
                adj += 7
                reasons.append("SMC MTF: price in Discount supports bullish setup (+7)")
            elif zone == "premium":
                adj -= 7
                warnings.append("SMC MTF: price in Premium may be late for bullish setup (-7)")
        if direction == "bearish":
            if zone == "premium":
                adj += 7
                reasons.append("SMC MTF: price in Premium supports bearish setup (+7)")
            elif zone == "discount":
                adj -= 7
                warnings.append("SMC MTF: price in Discount may be late for bearish setup (-7)")

        if sweep_bias == direction:
            adj += 8
            reasons.append(f"SMC MTF: recent {smc.get('recent_sweep')} supports setup (+8)")
        elif sweep_bias in ("bullish", "bearish") and sweep_bias != direction:
            adj -= 8
            warnings.append(f"SMC MTF: recent {smc.get('recent_sweep')} conflicts with setup (-8)")

    elif direction == "neutral":
        combo = smc.get("bias", "neutral")
        if combo == "neutral" and zone == "equilibrium" and sweep_bias == "neutral":
            adj += 10
            reasons.append("SMC MTF: neutral structure near equilibrium supports Iron Condor (+10)")
        elif combo in ("bullish", "bearish", "mixed") or sweep_bias in ("bullish", "bearish"):
            adj -= 10
            warnings.append("SMC MTF: directional or mixed structure weakens Iron Condor (-10)")

    old_score = strategy.get("score", 0) or 0
    try:
        old_score = float(old_score)
    except Exception:
        old_score = 0
    new_score = int(max(0, min(100, round(old_score + adj))))

    strategy["smc"] = smc
    strategy["smc_score_adjustment"] = adj
    strategy["score_before_smc"] = int(old_score)
    strategy["score"] = new_score
    strategy["smc_bias"] = smc.get("bias")
    strategy["smc_htf_bias"] = htf_bias
    strategy["smc_ltf_bias"] = ltf_bias
    strategy["smc_zone"] = smc.get("zone")
    strategy["smc_last_structure"] = smc.get("last_structure")
    strategy["smc_recent_sweep"] = smc.get("recent_sweep")

    if reasons:
        strategy["reasons"] = (strategy.get("reasons") or []) + reasons
    if warnings:
        strategy["warnings"] = (strategy.get("warnings") or []) + warnings
    breakdown = strategy.get("score_breakdown") or {}
    breakdown["SMC MTF"] = adj
    strategy["score_breakdown"] = breakdown
    smc["smc_score"] = adj
    return strategy
