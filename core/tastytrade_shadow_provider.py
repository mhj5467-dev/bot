"""Tastytrade shadow data provider for RC15j.

This module is diagnostics-only. It never places orders and it is disabled by
default. When enabled, it can compare a Tastytrade/DXLink quote against the
price already used by RC15j and optionally sample quote mids for the selected
strategy legs.

Environment variables:
    TASTY_SHADOW_ENABLED=true|false        (default: false)
    TASTYTRADE_USERNAME=<email>
    TASTYTRADE_PASSWORD=<password>
    TASTYTRADE_SANDBOX=true|false          (default: true)
    TASTY_SHADOW_TIMEOUT_SEC=2.5
    TASTY_SHADOW_PRICE_MISMATCH_PTS=3.0

No TT_SECRET / TT_REFRESH variables are required; this module reuses RC15j OAuth/DXLink.
"""
from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

# Load bot .env files explicitly for this provider.
# The RC15j GUI/settings layer does not automatically load config/.env.
# config/.env is loaded last so it wins if both files exist.
try:
    from dotenv import load_dotenv  # type: ignore
    try:
        from core.app_paths import get_env_paths
        _ENV_PATHS = get_env_paths()
    except Exception:
        _ROOT = Path(__file__).resolve().parents[1]
        _ENV_PATHS = (_ROOT / ".env", _ROOT / "config" / ".env")
    for _env_path in _ENV_PATHS:
        if _env_path.exists():
            load_dotenv(_env_path, override=True)
except Exception:
    pass

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

_SESSION: Any = None
_SESSION_OK: bool = False
_SESSION_REASON: str = "not_connected"
_SESSION_TS: float = 0.0
_QUOTE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_DEFAULT_TTL = 8.0


def _bool_env(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return default


def _is_enabled() -> bool:
    return _bool_env("TASTY_SHADOW_ENABLED", False)


def _event_symbol(symbol: str) -> str:
    """Map RC symbol to a DXLink event symbol for quote sampling."""
    s = str(symbol or "").upper().strip()
    if s == "SPX":
        return os.getenv("TASTY_SHADOW_SPX_EVENT_SYMBOL", "$SPX.X")
    return s


def _as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_mid(bid: Any, ask: Any, last: Any = None) -> Optional[float]:
    b = _as_float(bid)
    a = _as_float(ask)
    if b is not None and a is not None and b > 0 and a > 0:
        return round((b + a) / 2.0, 4)
    l = _as_float(last)
    if l is not None and l > 0:
        return round(l, 4)
    return None


def _base_result(symbol: str, reason: str = "") -> Dict[str, Any]:
    return {
        "tasty_shadow_enabled": _is_enabled(),
        "tasty_shadow_ok": False,
        "tasty_shadow_source": "tastytrade_dxlink_shadow",
        "tasty_shadow_reason": reason or "not_evaluated",
        "tasty_shadow_underlying_requested_symbol": str(symbol or "").upper(),
        "tasty_shadow_underlying_event_symbol": _event_symbol(symbol),
        "tasty_shadow_underlying_bid": None,
        "tasty_shadow_underlying_ask": None,
        "tasty_shadow_underlying_mid": None,
        "tasty_shadow_underlying_last": None,
        "tasty_shadow_price_diff_points": None,
        "tasty_shadow_price_diff_pct": None,
        "tasty_shadow_price_mismatch": False,
        "tasty_shadow_warning": "",
        "tasty_shadow_mismatch_threshold_pts": _float_env("TASTY_SHADOW_PRICE_MISMATCH_PTS", 1.0),
        "tasty_shadow_option_quote_count": 0,
        "tasty_shadow_leg_symbols": "",
        "tasty_shadow_note": "diagnostics_only_no_trade_impact",
    }


def _get_session() -> Tuple[Any, bool, str]:
    """Create/reuse the existing RC15j Tastytrade OAuth access token.

    Important: this provider must NOT use the external tastytrade.Session class,
    because some installed versions expect TT_SECRET / TT_REFRESH environment
    variables. RC15j already has a working OAuth/DXLink path via
    core.analyzer._get_access_token(), so the shadow provider reuses that path.
    """
    global _SESSION, _SESSION_OK, _SESSION_REASON, _SESSION_TS
    if not _is_enabled():
        return None, False, "disabled"

    if _SESSION is not None and _SESSION_OK:
        return _SESSION, True, "connected_cached"
    if _SESSION_TS and time.time() - _SESSION_TS < 60 and not _SESSION_OK:
        return None, False, _SESSION_REASON

    _SESSION_TS = time.time()
    try:
        # Import lazily to avoid affecting normal RC15j startup and to reuse
        # the same settings source as the rest of the bot: Tastytrade Client
        # Secret + Refresh Token from the GUI/config layer.
        from core.analyzer import _get_access_token  # type: ignore
        token = _get_access_token()
        if not token:
            raise RuntimeError("empty_access_token")
        _SESSION = {"access_token": token}
        _SESSION_OK = True
        _SESSION_REASON = "connected_rc15_oauth"
        return _SESSION, True, _SESSION_REASON
    except Exception as exc:
        _SESSION = None
        _SESSION_OK = False
        _SESSION_REASON = f"connect_failed:{exc.__class__.__name__}:{str(exc)[:100]}"
        return None, False, _SESSION_REASON


def _fetch_quote_sync(session: Any, event_symbol: str, timeout: float) -> Dict[str, Any]:
    """Fetch one Quote snapshot through RC15j's existing DXLink client."""
    from core.dxlink_client import fetch_market_data_snapshot  # type: ignore

    token = (session or {}).get("access_token") if isinstance(session, dict) else None
    if not token:
        raise RuntimeError("missing_access_token")

    snap = fetch_market_data_snapshot(token, [event_symbol], timeout_seconds=timeout, max_symbols=1)
    rec = snap.get(event_symbol)
    if rec is None and snap:
        # DXLink may return a normalized event symbol. Use the first record but
        # keep the returned symbol for diagnostics.
        ret_sym, rec = next(iter(snap.items()))
    else:
        ret_sym = event_symbol
    if not rec:
        raise RuntimeError("quote_not_found")

    bid = _as_float(rec.get("bid"))
    ask = _as_float(rec.get("ask"))
    last = _as_float(rec.get("last") or rec.get("price"))
    mid = _safe_mid(bid, ask, last)
    return {
        "event_symbol": ret_sym,
        "bid": bid,
        "ask": ask,
        "last": last,
        "mid": mid,
    }


def _cached_quote(session: Any, event_symbol: str, timeout: float) -> Dict[str, Any]:
    ttl = _float_env("TASTY_SHADOW_QUOTE_TTL_SEC", _DEFAULT_TTL)
    now = time.time()
    cached = _QUOTE_CACHE.get(event_symbol)
    if cached and now - cached[0] <= ttl:
        q = dict(cached[1])
        q["cache_hit"] = True
        return q
    q = _fetch_quote_sync(session, event_symbol, timeout)
    q["cache_hit"] = False
    _QUOTE_CACHE[event_symbol] = (now, q)
    return q


def _extract_leg_symbols(strategy: Optional[Dict[str, Any]], limit: int = 4) -> List[str]:
    if not isinstance(strategy, dict):
        return []
    found: List[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            txt = value.strip()
            if txt and len(txt) >= 3 and txt not in found:
                found.append(txt)
        elif isinstance(value, dict):
            for k in ("symbol", "option_symbol", "event_symbol", "dx_symbol"):
                if value.get(k):
                    add(value.get(k))
        elif isinstance(value, (list, tuple)):
            for item in value:
                add(item)

    for key in (
        "legs", "selected_legs", "option_legs", "order_legs", "long_leg", "short_leg",
        "long_put", "short_put", "long_call", "short_call", "buy_leg", "sell_leg",
    ):
        add(strategy.get(key))
        if len(found) >= limit:
            break
    return found[:limit]


def collect_tastytrade_shadow(
    symbol: str,
    *,
    price: Optional[float] = None,
    chain: Optional[Dict[str, Any]] = None,
    strategy: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return Tastytrade shadow diagnostics without changing trading decisions."""
    result = _base_result(symbol)
    if not _is_enabled():
        result["tasty_shadow_reason"] = "disabled"
        return result

    session, ok, reason = _get_session()
    result["tasty_shadow_reason"] = reason
    if not ok or session is None:
        return result

    timeout = _float_env("TASTY_SHADOW_TIMEOUT_SEC", 2.5)
    try:
        event_symbol = result["tasty_shadow_underlying_event_symbol"]
        q = _cached_quote(session, event_symbol, timeout)
        mid = q.get("mid")
        result.update({
            "tasty_shadow_ok": bool(mid),
            "tasty_shadow_reason": reason if mid else "quote_missing_mid",
            "tasty_shadow_underlying_event_symbol": q.get("event_symbol") or event_symbol,
            "tasty_shadow_underlying_bid": q.get("bid"),
            "tasty_shadow_underlying_ask": q.get("ask"),
            "tasty_shadow_underlying_mid": mid,
            "tasty_shadow_underlying_last": q.get("last"),
        })
        ref_price = _as_float(price)
        if mid is not None and ref_price is not None and ref_price > 0:
            diff = round(float(mid) - ref_price, 4)
            diff_pct = round(diff / ref_price * 100.0, 4)
            max_pts = _float_env("TASTY_SHADOW_PRICE_MISMATCH_PTS", 1.0)
            result["tasty_shadow_price_diff_points"] = diff
            result["tasty_shadow_price_diff_pct"] = diff_pct
            result["tasty_shadow_mismatch_threshold_pts"] = max_pts
            mismatch = abs(diff) > max_pts
            result["tasty_shadow_price_mismatch"] = mismatch
            if mismatch:
                result["tasty_shadow_warning"] = f"DATA_MISMATCH_WARNING: diff={diff}pts > threshold={max_pts}pts"
                result["tasty_shadow_note"] = "diagnostics_only_no_trade_impact | DATA_MISMATCH_WARNING"
                result["tasty_shadow_reason"] = f"{result.get('tasty_shadow_reason') or reason}|DATA_MISMATCH_WARNING diff={diff}pts>{max_pts}pts"
    except Exception as exc:
        result["tasty_shadow_ok"] = False
        result["tasty_shadow_reason"] = f"quote_failed:{exc.__class__.__name__}:{str(exc)[:80]}"

    # Optional leg sampling. Kept deliberately light; never blocks strategy logic.
    try:
        leg_symbols = _extract_leg_symbols(strategy)
        result["tasty_shadow_leg_symbols"] = "|".join(leg_symbols)
        quote_count = 0
        for leg_sym in leg_symbols:
            try:
                _cached_quote(session, leg_sym, min(timeout, 1.5))
                quote_count += 1
            except Exception:
                pass
        result["tasty_shadow_option_quote_count"] = quote_count
    except Exception:
        pass

    return result
