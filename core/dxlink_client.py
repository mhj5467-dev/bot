"""
DXLink client for tastytrade market data.

This module fetches a tastytrade API quote token, opens a DXLink websocket,
and collects Quote/Greeks/Summary events for a short period. It is designed
for snapshot-style enrichment of an option chain, not continuous trading.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    import websocket  # websocket-client
except Exception:  # pragma: no cover
    websocket = None

API_BASE = "https://api.tastyworks.com"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "AbuHassanBot/1.2",
}

QUOTE_FIELDS = ["eventType", "eventSymbol", "bidPrice", "askPrice", "bidSize", "askSize"]
GREEKS_FIELDS = ["eventType", "eventSymbol", "volatility", "delta", "gamma", "theta", "rho", "vega"]
SUMMARY_FIELDS = ["eventType", "eventSymbol", "openInterest", "dayOpenPrice", "dayHighPrice", "dayLowPrice", "prevDayClosePrice"]
TRADE_FIELDS = ["eventType", "eventSymbol", "price", "dayVolume", "size"]
CANDLE_FIELDS = ["eventType", "eventSymbol", "time", "sequence", "count", "open", "high", "low", "close", "volume"]

FIELD_MAP = {
    "Quote": QUOTE_FIELDS,
    "Greeks": GREEKS_FIELDS,
    "Summary": SUMMARY_FIELDS,
    "Trade": TRADE_FIELDS,
}

# رموز DXLink للشموع. SPX يحتاج غالباً root خاص؛ SPY/QQQ يستخدمان نفس الرمز مع الفترة المطلوبة.
_CANDLE_SYMBOL_ROOT: Dict[str, str] = {
    "SPY": "SPY",
    "QQQ": "QQQ",
    "IWM": "IWM",
    "DIA": "DIA",
    "GLD": "GLD",
    "SPX": "$SPX.X",
}
# توافق خلفي مع RC6: بعض المواضع القديمة قد تشير للاسم السابق.
_CANDLE_SYMBOL_4H: Dict[str, str] = {
    "SPY": "SPY{=4h}",
    "QQQ": "QQQ{=4h}",
    "IWM": "IWM{=4h}",
    "DIA": "DIA{=4h}",
    "GLD": "GLD{=4h}",
    "SPX": "$SPX.X{=4h}",
}

def _candle_symbol_for(symbol: str, period: str) -> str:
    sym_upper = str(symbol or "").upper().strip()
    root = _CANDLE_SYMBOL_ROOT.get(sym_upper, sym_upper)
    per = str(period or "4h").strip()
    return f"{root}{{={per}}}"


# Cache: ticker -> streamer-symbol (e.g. "GLD" -> "GLD:ARCX")
_STREAMER_SYMBOL_CACHE: Dict[str, str] = {}


def _auth_headers(access_token: str) -> Dict[str, str]:
    return {**HEADERS, "Authorization": f"Bearer {access_token}"}


def get_equity_streamer_symbol(access_token: str, ticker: str) -> Optional[str]:
    """Return the DXFeed streamer-symbol for an equity ticker from Tastytrade instruments API.

    Example: "GLD" -> "GLD:ARCX"
    Returns None on failure (caller should fall back to raw ticker).
    Cached in-process so subsequent calls are free.
    """
    ticker_up = ticker.upper()
    if ticker_up in _STREAMER_SYMBOL_CACHE:
        return _STREAMER_SYMBOL_CACHE[ticker_up]
    try:
        resp = requests.get(
            f"{API_BASE}/instruments/equities/{ticker_up}",
            headers=_auth_headers(access_token),
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        data = resp.json().get("data", {})
        # Tastytrade returns streamer-symbol at top level or inside instrument
        sym = (
            data.get("streamer-symbol")
            or data.get("streamerSymbol")
            or (data.get("instrument") or {}).get("streamer-symbol")
        )
        if sym:
            _STREAMER_SYMBOL_CACHE[ticker_up] = str(sym)
            return str(sym)
    except Exception:
        pass
    return None


def get_api_quote_token(access_token: str) -> Dict[str, str]:
    """Return {'token': ..., 'dxlink-url': ...}."""
    resp = requests.get(
        f"{API_BASE}/api-quote-tokens",
        headers=_auth_headers(access_token),
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"api-quote-tokens status={resp.status_code}: {resp.text[:250]}")
    data = resp.json().get("data", {})
    token = data.get("token")
    url = data.get("dxlink-url")
    if not token or not url:
        raise RuntimeError(f"api-quote-tokens missing token/url: {resp.text[:250]}")
    return {"token": token, "dxlink-url": url}


def _send(ws: Any, payload: Dict[str, Any]) -> None:
    ws.send(json.dumps(payload, separators=(",", ":")))


def _wait_for(ws: Any, predicate, timeout: float = 8.0) -> Optional[Dict[str, Any]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        # RC15i.3: make the websocket receive loop respect the local deadline.
        # create_connection(timeout=...) is not enough for all recv() paths.
        remaining = max(0.1, deadline - time.time())
        try:
            ws.settimeout(min(1.0, remaining))
        except Exception:
            pass
        try:
            raw = ws.recv()
        except Exception as e:
            if "timeout" in e.__class__.__name__.lower():
                continue
            raise
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if predicate(msg):
            return msg
    return None


def _safe_float(v: Any) -> Optional[float]:
    if v is None or v == "NaN" or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _parse_compact_feed_data(msg: Dict[str, Any], out: Dict[str, Dict[str, Any]]) -> None:
    """Parse DXLink COMPACT FEED_DATA messages into out[eventSymbol]."""
    data = msg.get("data")
    if not isinstance(data, list):
        return

    def consume_flat(flat: List[Any]) -> None:
        i = 0
        while i < len(flat):
            event_type = flat[i]
            if event_type not in FIELD_MAP:
                i += 1
                continue
            fields = FIELD_MAP[event_type]
            n = len(fields)
            values = flat[i:i+n]
            if len(values) < n:
                break
            rec = dict(zip(fields, values))
            symbol = rec.get("eventSymbol")
            if symbol:
                slot = out.setdefault(str(symbol), {})
                if event_type == "Quote":
                    slot.update({
                        "bid": _safe_float(rec.get("bidPrice")),
                        "ask": _safe_float(rec.get("askPrice")),
                        "bid-size": _safe_float(rec.get("bidSize")),
                        "ask-size": _safe_float(rec.get("askSize")),
                    })
                elif event_type == "Greeks":
                    slot.update({
                        "iv": _safe_float(rec.get("volatility")),
                        "delta": _safe_float(rec.get("delta")),
                        "gamma": _safe_float(rec.get("gamma")),
                        "theta": _safe_float(rec.get("theta")),
                        "rho": _safe_float(rec.get("rho")),
                        "vega": _safe_float(rec.get("vega")),
                    })
                elif event_type == "Summary":
                    slot.update({
                        "open-interest": _safe_float(rec.get("openInterest")),
                        "prev-close": _safe_float(rec.get("prevDayClosePrice")),
                    })
                elif event_type == "Trade":
                    slot.update({
                        "last": _safe_float(rec.get("price")),
                        "volume": _safe_float(rec.get("dayVolume")),
                    })
            i += n

    # Common format: ["Greeks", ["Greeks", sym, ...], "Quote", [...]]
    i = 0
    while i < len(data):
        if isinstance(data[i], str) and i + 1 < len(data) and isinstance(data[i + 1], list):
            consume_flat(data[i + 1])
            i += 2
        elif isinstance(data[i], list):
            consume_flat(data[i])
            i += 1
        else:
            i += 1


def fetch_market_data_snapshot(
    access_token: str,
    streamer_symbols: Iterable[str],
    timeout_seconds: float = 6.0,
    max_symbols: int = 120,
) -> Dict[str, Dict[str, Any]]:
    """Fetch Quote/Greeks/Summary/Trade snapshot for streamer symbols.

    Returns mapping: streamer_symbol -> standardized fields.
    """
    symbols = [s for s in dict.fromkeys(streamer_symbols) if s]
    if not symbols:
        return {}
    symbols = symbols[:max_symbols]
    if websocket is None:
        raise RuntimeError("Missing dependency: pip install websocket-client")

    qt = get_api_quote_token(access_token)
    ws = websocket.create_connection(qt["dxlink-url"], timeout=10)
    out: Dict[str, Dict[str, Any]] = {}
    try:
        _send(ws, {"type":"SETUP","channel":0,"version":"0.1-DXF-JS/0.3.0","keepaliveTimeout":60,"acceptKeepaliveTimeout":60})
        _wait_for(ws, lambda m: m.get("type") == "AUTH_STATE" and m.get("state") == "UNAUTHORIZED", timeout=8)
        _send(ws, {"type":"AUTH","channel":0,"token":qt["token"]})
        auth_msg = _wait_for(ws, lambda m: m.get("type") == "AUTH_STATE" and m.get("state") == "AUTHORIZED", timeout=8)
        if not auth_msg:
            raise RuntimeError("DXLink authorization failed or timed out")

        channel = 3
        _send(ws, {"type":"CHANNEL_REQUEST","channel":channel,"service":"FEED","parameters":{"contract":"AUTO"}})
        opened = _wait_for(ws, lambda m: m.get("type") == "CHANNEL_OPENED" and m.get("channel") == channel, timeout=8)
        if not opened:
            raise RuntimeError("DXLink feed channel did not open")

        _send(ws, {
            "type":"FEED_SETUP",
            "channel":channel,
            "acceptAggregationPeriod":0.1,
            "acceptDataFormat":"COMPACT",
            "acceptEventFields":{
                "Quote": QUOTE_FIELDS,
                "Greeks": GREEKS_FIELDS,
                "Summary": SUMMARY_FIELDS,
                "Trade": TRADE_FIELDS,
            },
        })
        _wait_for(ws, lambda m: m.get("type") == "FEED_CONFIG" and m.get("channel") == channel, timeout=8)

        add = []
        for sym in symbols:
            add.extend([
                {"type":"Quote","symbol":sym},
                {"type":"Greeks","symbol":sym},
                {"type":"Summary","symbol":sym},
                {"type":"Trade","symbol":sym},
            ])
        _send(ws, {"type":"FEED_SUBSCRIPTION","channel":channel,"reset":True,"add":add})

        deadline      = time.time() + timeout_seconds
        last_data_t   = time.time()
        min_wait      = min(3.0, timeout_seconds * 0.3)  # انتظر على الأقل 30% من الـ timeout
        received_data = False

        while time.time() < deadline:
            try:
                ws.settimeout(min(2.0, deadline - time.time()))
                raw = ws.recv()
            except Exception:
                # timeout على recv — تحقق إذا انتهى الـ deadline
                if time.time() >= deadline:
                    break
                # إذا لم تصل بيانات بعد: استمر
                if received_data and (time.time() - last_data_t) > 2.0:
                    break
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"[dxlink] bad json frame skipped (market_data): {e}")
                continue
            if msg.get("type") == "FEED_DATA":
                prev_len = len(out)
                _parse_compact_feed_data(msg, out)
                if len(out) > prev_len:
                    received_data = True
                    last_data_t   = time.time()
                # early exit: استلمنا بيانات لمعظم الرموز المطلوبة + مضت min_wait
                if (received_data
                        and time.time() - (deadline - timeout_seconds) >= min_wait
                        and len(out) >= len(symbols) * 0.7):
                    break
            elif msg.get("type") == "KEEPALIVE":
                _send(ws, {"type":"KEEPALIVE","channel":0})
        return out
    finally:
        try:
            ws.close()
        except Exception:
            pass


def fetch_dxlink_candles_snapshot(
    access_token: str,
    symbol: str,
    period: str = "4h",
    days_back: int = 90,
    timeout_seconds: float = 18.0,
) -> List[Dict[str, Any]]:
    """Fetch historical 4H candles from DXLink for a symbol.

    Returns list of candle dicts sorted by time ascending.
    Each dict: {time, open, high, low, close, volume}.
    Empty list on failure.
    """
    if websocket is None:
        raise RuntimeError("Missing dependency: pip install websocket-client")

    sym_upper = symbol.upper()
    candle_sym = _candle_symbol_for(sym_upper, period)
    from_time_ms = int((time.time() - days_back * 86400) * 1000)

    qt = get_api_quote_token(access_token)
    ws = websocket.create_connection(qt["dxlink-url"], timeout=10)
    candles_dict: Dict[int, Dict[str, Any]] = {}

    try:
        _send(ws, {"type": "SETUP", "channel": 0, "version": "0.1-DXF-JS/0.3.0",
                   "keepaliveTimeout": 60, "acceptKeepaliveTimeout": 60})
        _wait_for(ws, lambda m: m.get("type") == "AUTH_STATE" and m.get("state") == "UNAUTHORIZED", timeout=8)
        _send(ws, {"type": "AUTH", "channel": 0, "token": qt["token"]})
        auth_msg = _wait_for(ws, lambda m: m.get("type") == "AUTH_STATE" and m.get("state") == "AUTHORIZED", timeout=8)
        if not auth_msg:
            raise RuntimeError("DXLink authorization failed")

        channel = 5
        _send(ws, {"type": "CHANNEL_REQUEST", "channel": channel, "service": "FEED",
                   "parameters": {"contract": "AUTO"}})
        opened = _wait_for(ws, lambda m: m.get("type") == "CHANNEL_OPENED" and m.get("channel") == channel, timeout=8)
        if not opened:
            raise RuntimeError("DXLink candle feed channel did not open")

        _send(ws, {
            "type": "FEED_SETUP",
            "channel": channel,
            "acceptAggregationPeriod": 0.1,
            "acceptDataFormat": "COMPACT",
            "acceptEventFields": {"Candle": CANDLE_FIELDS},
        })
        _wait_for(ws, lambda m: m.get("type") == "FEED_CONFIG" and m.get("channel") == channel, timeout=8)

        _send(ws, {
            "type": "FEED_SUBSCRIPTION",
            "channel": channel,
            "reset": True,
            "add": [{"type": "Candle", "symbol": candle_sym, "fromTime": from_time_ms}],
        })

        deadline = time.time() + timeout_seconds
        last_data_t = time.time()
        n_fields = len(CANDLE_FIELDS)

        def _parse_candle_flat(flat: List[Any]) -> None:
            j = 0
            while j < len(flat):
                if flat[j] == "Candle" and j + n_fields <= len(flat):
                    rec = dict(zip(CANDLE_FIELDS, flat[j:j + n_fields]))
                    t = rec.get("time")
                    c = _safe_float(rec.get("close"))
                    if t is not None and c and c > 0:
                        candles_dict[int(t)] = {
                            "time": int(t),
                            "open":   _safe_float(rec.get("open")),
                            "high":   _safe_float(rec.get("high")),
                            "low":    _safe_float(rec.get("low")),
                            "close":  c,
                            "volume": _safe_float(rec.get("volume")),
                        }
                    j += n_fields
                else:
                    j += 1

        while time.time() < deadline:
            try:
                ws.settimeout(min(2.0, deadline - time.time()))
                raw = ws.recv()
            except Exception:
                if time.time() >= deadline:
                    break
                if len(candles_dict) >= 50 and (time.time() - last_data_t) > 2.5:
                    break
                continue
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as e:
                print(f"[dxlink] bad json frame skipped (candles): {e}")
                continue
            if msg.get("type") == "FEED_DATA":
                data = msg.get("data", [])
                i = 0
                while i < len(data):
                    if isinstance(data[i], str) and i + 1 < len(data) and isinstance(data[i + 1], list):
                        _parse_candle_flat(data[i + 1])
                        i += 2
                    elif isinstance(data[i], list):
                        _parse_candle_flat(data[i])
                        i += 1
                    else:
                        i += 1
                last_data_t = time.time()
            elif msg.get("type") == "KEEPALIVE":
                _send(ws, {"type": "KEEPALIVE", "channel": 0})

        return sorted(candles_dict.values(), key=lambda x: x["time"])
    finally:
        try:
            ws.close()
        except Exception:
            pass
