"""
SPX Analyzer - Tastytrade OAuth2 Edition (Enhanced)

ما تم تحسينه في هذه النسخة:
1) قراءة bid/ask/mid/delta/gamma/iv إن كانت متاحة من Tastytrade.
2) حساب مستويات Call/Put Wall باستخدام OI + Volume + Gamma Exposure تقريبي.
3) حساب Expected Move تقريبي من ATM straddle عند توفر الأسعار.
4) Pin Score أكثر تحفظاً: قرب السعر + تركّز OI + توازن Call/Put + ضغط Gamma تقريبي.
5) اقتراح Iron Condor 0DTE بناءً على Delta المستهدفة أو مسافة Expected Move عند غياب Delta.

ملاحظة: GEX هنا تقديري وليس بديلاً عن نموذج Dealer Positioning احترافي، لأن اتجاه مراكز dealers غير معلوم من Option Chain وحده.
"""
from __future__ import annotations

import math
import time
import json
import threading
from urllib.parse import quote
from datetime import datetime, date, timezone, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from core.dxlink_client import fetch_market_data_snapshot
from core.database import (
    get_setting, save_analysis_log,
    save_iv_history, get_iv_history,
    save_oi_cache, get_oi_cache,
)

API_BASE = "https://api.tastyworks.com"
OAUTH_URL = "https://api.tastyworks.com/oauth/token"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "AbuHassanBot/1.2",
}

CONTRACT_MULTIPLIER = 100
DEFAULT_WING_WIDTH = 5
TARGET_SHORT_DELTA = 0.12
MIN_CREDIT = 0.50
SQRT_TRADING_DAYS = math.sqrt(252)

_access_token: Optional[str] = None
_token_expires = 0.0
_token_lock = threading.Lock()
_LAST_DEBUG: Dict[str, Any] = {
    "auth": "not_tested",
    "spx_price": "not_tested",
    "price_source": None,
    "chain": "not_tested",
    "chain_calls": 0,
    "chain_puts": 0,
    "greeks": {},
    "errors": [],
    "last_analysis_time": None,
    "chain_root": None,
    "chain_raw_preview": None,
    "chain_response_keys": None,
    "oi_cache_age_minutes": None,
}


def _debug_set(key: str, value: Any) -> None:
    _LAST_DEBUG[key] = value


def _debug_error(message: str) -> None:
    errors = _LAST_DEBUG.setdefault("errors", [])
    errors.append(f"{datetime.now().strftime('%H:%M:%S')} - {message}")
    del errors[:-12]


def get_last_debug() -> Dict[str, Any]:
    return dict(_LAST_DEBUG)


def invalidate_session() -> None:
    global _access_token, _token_expires
    _access_token = None
    _token_expires = 0.0


def is_us_market_open() -> bool:
    """فحص ساعات السوق الأمريكي بتوقيت نيويورك (ET) بغض النظر عن توقيت الجهاز."""
    # نيويورك = UTC-4 (EDT صيفاً) أو UTC-5 (EST شتاءً)
    # نستخدم UTC-4 للـ EDT (أبريل-أكتوبر) ونتحقق تلقائياً
    now_utc = datetime.now(timezone.utc)
    # تحديد EDT/EST: EDT من الأحد الثاني في مارس إلى الأحد الأول في نوفمبر
    month = now_utc.month
    is_edt = 3 < month < 11 or (month == 3 and now_utc.day >= 8) or (month == 11 and now_utc.day < 8)
    et_offset = timedelta(hours=-4 if is_edt else -5)
    now_et = now_utc + et_offset

    # السوق مغلق في عطلة نهاية الأسبوع
    if now_et.weekday() >= 5:
        return False

    # ساعات التداول: 9:30 AM - 4:00 PM ET
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close


def get_et_time() -> datetime:
    """الوقت الحالي بتوقيت نيويورك — نفس منطق EDT/EST كـ is_us_market_open."""
    now_utc = datetime.now(timezone.utc)
    month = now_utc.month
    is_edt = 3 < month < 11 or (month == 3 and now_utc.day >= 8) or (month == 11 and now_utc.day < 8)
    return now_utc + timedelta(hours=-4 if is_edt else -5)


_YAHOO_HISTORY_CACHE: Dict[Tuple[str, str, str], List[float]] = {}
_MARKET_CONTEXT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_PIN_SCORE_CACHE: Dict[str, Any] = {}  # آخر Pin Score صحيح مع chain حقيقي

# ── Chain Cache — يحفظ آخر chain ناجح لكل رمز لمدة 5 دقائق ──────────────────
# يمنع LIMITED عند فشل DXLink مؤقتاً في دورة واحدة
_CHAIN_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}   # symbol → (timestamp, chain)
_CHAIN_CACHE_TTL = 300  # 5 دقائق

def _chain_cache_get(symbol: str) -> Optional[Dict[str, Any]]:
    entry = _CHAIN_CACHE.get(symbol.upper())
    if entry and time.time() - entry[0] < _CHAIN_CACHE_TTL:
        return entry[1]
    return None

def _chain_cache_set(symbol: str, chain: Dict[str, Any]) -> None:
    opts = chain.get("calls", []) + chain.get("puts", [])
    has_data = any(o.get("bid") is not None or o.get("delta") is not None for o in opts)
    if has_data:
        _CHAIN_CACHE[symbol.upper()] = (time.time(), chain)

# ── Timeout constants ─────────────────────────────────────────────────────────
_T_AUTH   = 15   # OAuth token refresh
_T_API    = 12   # Tastytrade REST (market-metrics, option-chains)
_T_YAHOO  = 10   # Yahoo Finance endpoints
_T_STOOQ  = 8    # Stooq CSV fallback


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    if value in (None, "", "None"):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    if value in (None, "", "None"):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _mid_from_bid_ask(bid: Optional[float], ask: Optional[float], fallback: Optional[float] = None) -> Optional[float]:
    if bid is not None and ask is not None and bid >= 0 and ask >= 0 and ask >= bid:
        return round((bid + ask) / 2, 4)
    return fallback


def _first_number(item: Dict[str, Any], keys: Iterable[str]) -> Optional[float]:
    for k in keys:
        v = _to_float(item.get(k))
        if v is not None:
            return v
    return None



def _bs_d1(spot: float, strike: float, iv: float, t: float) -> float:
    """d1 من نموذج Black-Scholes (بدون rate لتبسيط 0DTE)"""
    if t <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    return (math.log(spot / strike) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))


def _bs_pdf(x: float) -> float:
    """دالة الكثافة الاحتمالية الطبيعية"""
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _estimate_vanna_charm(
    opt_type: str,
    delta: Optional[float],
    gamma: Optional[float],
    iv: Optional[float],
    strike: float,
    spot: float,
    hours_to_expiry: float = 6.5,
) -> Tuple[Optional[float], Optional[float], bool]:
    """
    Vanna وCharm بصيغة Black-Scholes مع time-to-expiry حقيقي.

    - t = نسبة السنة المتبقية (ساعات ÷ 8736 ساعة/سنة)
    - Vanna = -pdf(d1) * d2 / (S * σ)  — حساسية Delta للتقلب
    - Charm = -pdf(d1) * (2rt - d2*σ*√t) / (2t*σ*√t)  — حساسية Delta للزمن
    عند توفر الحقول من مزود البيانات نستخدمها بدلاً من الحساب.
    """
    if delta is None or gamma is None:
        return None, None, False
    iv_safe = max(abs(iv or 0.20), 0.01)
    t = max(hours_to_expiry / 8736.0, 1 / 8736.0)
    sqrt_t = math.sqrt(t)
    try:
        d1 = _bs_d1(spot, strike, iv_safe, t)
        d2 = d1 - iv_safe * sqrt_t
        pdf_d1 = _bs_pdf(d1)
        # Vanna: حساسية Delta للتقلب — يُعكس لـ puts
        vanna = -pdf_d1 * d2 / (max(spot, 1) * iv_safe)
        if opt_type == "put":
            vanna = -vanna
        # Charm: معدل تآكل دلتا — مُعبَّر عنه بوحدة "لكل ساعة" لتجنب القيم الضخمة في 0DTE
        charm_annualized = -pdf_d1 * (d2 / (2 * t * iv_safe * sqrt_t)) if t > 0 else 0.0
        charm = charm_annualized * t / max(hours_to_expiry, 0.25)
        if opt_type == "put":
            charm = charm
        return round(vanna, 8), round(charm, 8), True
    except Exception:
        return None, None, False

def _get_access_token(client_secret: Optional[str] = None, refresh_token: Optional[str] = None, force: bool = False) -> str:
    """الحصول على Access Token من Refresh Token."""
    global _access_token, _token_expires

    client_secret = client_secret or get_setting("tasty_client_secret")
    refresh_token = refresh_token or get_setting("tasty_refresh_token")

    if not client_secret or not refresh_token:
        raise ValueError("يرجى إدخال Client Secret و Refresh Token في الإعدادات")

    # RC15i.3: Fast path before the lock, then re-check inside the lock.
    # This prevents multiple worker threads from refreshing the same token concurrently.
    if not force and _access_token and time.time() < _token_expires - 60:
        return _access_token

    with _token_lock:
        if not force and _access_token and time.time() < _token_expires - 60:
            return _access_token

        from core.circuit_breaker import tastytrade_breaker, CircuitOpenError
        try:
            with tastytrade_breaker()():
                resp = requests.post(
                    OAUTH_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_secret": client_secret,
                    },
                    headers=HEADERS,
                    timeout=_T_AUTH,
                )
        except CircuitOpenError as _coe:
            raise RuntimeError(str(_coe)) from _coe

        if resp.status_code == 401:
            _debug_set("auth", "failed_401")
            _debug_error("OAuth 401: Refresh Token أو Client Secret غير صحيح")
            raise ValueError("❌ Refresh Token أو Client Secret غير صحيح\nتحقق من الإعدادات")
        if resp.status_code == 400:
            _debug_set("auth", "failed_400")
            _debug_error("OAuth 400: بيانات OAuth غير صحيحة")
            raise ValueError("❌ بيانات OAuth غير صحيحة\nتحقق من Client Secret و Refresh Token")
        if resp.status_code not in (200, 201):
            _debug_set("auth", f"failed_{resp.status_code}")
            _debug_error(f"OAuth failed: status={resp.status_code}")
            raise ValueError(f"❌ فشل الاتصال بـ Tastytrade (خطأ {resp.status_code})")

        data = resp.json()
        token = data.get("access_token")
        expires_in = _to_float(data.get("expires_in"), 900) or 900
        if not token:
            raise ValueError("❌ فشل جلب Access Token — تحقق من البيانات")

        _access_token = token
        _token_expires = time.time() + expires_in
        _debug_set("auth", "ok")
        return _access_token


def _auth_headers(token: str) -> Dict[str, str]:
    return {**HEADERS, "Authorization": f"Bearer {token}"}


# ── Price ─────────────────────────────────────────────────────────────────────

def get_spx_price(token: Optional[str] = None) -> Optional[float]:
    """جلب سعر SPX من Tastytrade مع تسجيل تشخيص واضح."""
    import http.client as _hc
    import ssl as _ssl2
    from urllib.parse import quote as _uq

    def _mm_get(sym: str, tok: str) -> Optional[dict]:
        path = f"/market-metrics?symbols={_uq(sym, safe='')}"  # RC15i.5: symbols= فقط
        try:
            ctx = _ssl2.create_default_context()
            conn = _hc.HTTPSConnection("api.tastyworks.com", timeout=_T_API, context=ctx)
            conn.request("GET", path, headers=_auth_headers(tok))
            r = conn.getresponse()
            if r.status == 200:
                return json.loads(r.read().decode("utf-8", errors="replace"))
            conn.close()
        except Exception:
            pass
        return None

    tok = token or _get_access_token()
    _debug_set("spx_price", "trying_tastytrade")

    candidates = ["SPX", "$SPX.X", "SPXW"]
    for symbol in candidates:
        try:
            payload = _mm_get(symbol, tok)
            if payload is not None:
                items = payload.get("data", {}).get("items", [])
                for item in items:
                    price = _first_number(item, ["mark", "last-price", "last", "close", "underlying-price"])
                    if price and price > 0:
                        _debug_set("spx_price", "ok")
                        _debug_set("price_source", f"tastytrade:{symbol}")
                        return price
        except Exception as exc:
            _debug_error(f"market-metrics {symbol}: {exc}")
    _debug_set("spx_price", "failed_tastytrade")
    return None


def _normalize_percent_metric(value: Any) -> Optional[float]:
    """Normalize broker percentage metrics to 0–100 scale.

    Tastytrade-like endpoints may return 35.6 or 0.356 depending on the field.
    We keep None for missing/invalid values and cap only display-scale values.
    """
    v = _to_float(value)
    if v is None:
        return None
    if 0 <= v <= 1.5:
        v *= 100.0
    if v < 0:
        return None
    return round(max(0.0, min(100.0, v)), 2)


def _recursive_first_number(obj: Any, keys: Iterable[str]) -> Optional[float]:
    """Find the first numeric value for any key, recursively, in dict/list API payloads."""
    keyset = {k.lower() for k in keys}
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in keyset:
                n = _to_float(v)
                if n is not None:
                    return n
        for v in obj.values():
            n = _recursive_first_number(v, keys)
            if n is not None:
                return n
    elif isinstance(obj, list):
        for item in obj:
            n = _recursive_first_number(item, keys)
            if n is not None:
                return n
    return None


def fetch_tastytrade_market_metrics(symbol: str, token: Optional[str] = None) -> Dict[str, Any]:
    """Fetch broker-provided IV Rank / IV Percentile from Tastytrade market metrics.

    This is used only as a data-source improvement. It does not alter entry/exit rules.
    If the broker endpoint does not expose the values, returns available=False with a reason.
    """
    symbol_up = str(symbol or "SPX").upper()
    candidates = [symbol_up]
    if symbol_up == "SPX":
        candidates += ["$SPX.X", "SPXW"]
    # Deduplicate while preserving order
    candidates = list(dict.fromkeys(candidates))

    iv_rank_keys = [
        "implied-volatility-index-rank", "implied-volatility-rank",
        "implied-volatility-rank-percent", "iv-rank", "iv_rank",
        "ivr", "volatility-rank", "volatility-rank-percent",
    ]
    iv_pct_keys = [
        "implied-volatility-percentile", "implied-volatility-index-percentile",
        "iv-percentile", "iv_percentile", "iv-perc", "iv_perc",
        "iv-pct", "iv_pct", "iv%tile", "volatility-percentile",
    ]
    iv_index_keys = [
        "implied-volatility-index", "implied-volatility-index-value",
        "iv-index", "iv_index", "ivx", "ivx-index", "volatility-index",
    ]

    try:
        tok = token or _get_access_token()
    except Exception as exc:
        return {"available": False, "source": "tastytrade_market_metrics", "reason": f"auth_failed:{exc.__class__.__name__}"}

    import http.client as _http_client
    import ssl as _ssl
    from urllib.parse import quote as _urlquote
    from core.circuit_breaker import tastytrade_breaker, CircuitOpenError

    # RC15i.5: اللوق أثبت أن symbols= (بدون brackets) هي الصيغة الصحيحة — 200 دائماً.
    # الحل: http.client مباشرة — يُرسل الـ path كما هو بدون إعادة ترميز.
    def _market_metrics_raw(symbol_cand: str, auth_tok: str, timeout: float) -> tuple:
        """Returns (status_code, body_str). Bypasses requests URL encoding."""
        # RC15i.5: اللوق أثبت أن symbols= (بدون []) هي الصيغة الوحيدة التي ترجع 200
        path = f"/market-metrics?symbols={_urlquote(symbol_cand, safe='')}"
        hdrs = _auth_headers(auth_tok)
        try:
            ctx = _ssl.create_default_context()
            conn = _http_client.HTTPSConnection("api.tastyworks.com", timeout=timeout, context=ctx)
            conn.request("GET", path, headers=hdrs)
            r = conn.getresponse()
            body = r.read().decode("utf-8", errors="replace")
            conn.close()
            return r.status, body
        except Exception as exc:
            return 0, str(exc)

    last_reason = None
    raw_keys_list: List[str] = []
    for cand in candidates:
        path_used = f"/market-metrics?symbols={_urlquote(cand, safe='')}"  # RC15i.5
        try:
            with tastytrade_breaker()():
                status_code, body_text = _market_metrics_raw(cand, tok, float(_T_API))
        except CircuitOpenError as exc:
            last_reason = f"circuit_open:{exc}"
            _debug_error(f"market-metrics IV {cand}: circuit_open")
            break
        except Exception as exc:
            last_reason = f"request_failed:{exc.__class__.__name__}"
            _debug_error(f"market-metrics IV {cand}: {exc}")
            continue

        if status_code != 200:
            last_reason = f"status_{status_code}"
            print(f"[IV DEBUG] market-metrics {cand}: status={status_code} path={path_used}")
            print(f"[IV DEBUG] body: {body_text[:400]}")
            _debug_error(f"market-metrics IV {cand}: status={status_code}")
            continue

        try:
            payload = json.loads(body_text)
        except Exception as exc:
            last_reason = f"bad_json:{exc.__class__.__name__}:{body_text[:100]}"
            continue

        items = payload.get("data", {}).get("items", [])
        print(f"[IV DEBUG] {cand}: status=200 items={len(items)}")
        if not items:
            last_reason = "empty_items"
            continue

        # Prefer the matching item if the endpoint returns multiple symbols.
        item = None
        for it in items:
            sym = str(it.get("symbol") or it.get("underlying-symbol") or it.get("root-symbol") or "").upper()
            if sym in {cand.upper(), symbol_up}:
                item = it
                break
        item = item or items[0]
        print(f"[IV DEBUG] {cand} keys: {sorted(item.keys())}")

        rank_raw = _recursive_first_number(item, iv_rank_keys)
        pct_raw = _recursive_first_number(item, iv_pct_keys)
        ivx_raw = _recursive_first_number(item, iv_index_keys)
        iv_rank = _normalize_percent_metric(rank_raw)
        iv_pct = _normalize_percent_metric(pct_raw)
        iv_index = _to_float(ivx_raw)
        if iv_index is not None and 0 <= iv_index <= 1.5:
            iv_index = round(iv_index * 100.0, 2)

        raw_keys_list = sorted(str(k) for k in item.keys())
        if iv_rank is not None or iv_pct is not None or iv_index is not None:
            _debug_set("market_metrics_iv", f"ok:{cand}")
            return {
                "available": True,
                "source": f"tastytrade_market_metrics:{cand}",
                "symbol": cand,
                "iv_rank": iv_rank,
                "iv_percentile": iv_pct,
                "iv_index": iv_index,
                "raw_keys": raw_keys_list[:80],
            }
        # حقول IV غير موجودة — نُرجع raw_keys للتشخيص
        last_reason = f"metrics_missing_fields:{','.join(raw_keys_list[:40])}"
        _debug_set("market_metrics_iv", f"missing_fields:{cand}:keys={raw_keys_list[:20]}")

    _debug_set("market_metrics_iv", f"failed:{last_reason}")
    _last_raw: List[str] = locals().get("raw_keys_list") or []
    return {
        "available": False,
        "source": "tastytrade_market_metrics",
        "reason": last_reason or "unavailable",
        "raw_keys": _last_raw[:80],
    }


def get_spx_price_yahoo() -> Optional[float]:
    """Fallback مجاني لآخر سعر SPX — يعمل حتى في عطلة نهاية الأسبوع."""

    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"

    # Endpoint 1: Yahoo Finance v8 مع User-Agent كامل
    try:
        session = requests.Session()
        session.headers.update({"User-Agent": ua, "Accept": "application/json",
                                "Accept-Language": "en-US,en;q=0.9"})
        # أولاً نجلب cookie
        session.get("https://finance.yahoo.com", timeout=8)
        resp = session.get(
            "https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC",
            params={"range": "5d", "interval": "1d"},
            timeout=12
        )
        if resp.status_code == 200:
            result = (resp.json().get("chart", {}).get("result") or [None])[0]
            if result:
                meta = result.get("meta", {})
                price = _to_float(meta.get("regularMarketPrice") or meta.get("previousClose")
                                  or meta.get("chartPreviousClose"))
                if price and price > 1000:
                    _debug_set("spx_price", "ok")
                    _debug_set("price_source", "yahoo_v8:^GSPC")
                    return price
    except Exception as exc:
        _debug_error(f"Yahoo v8 error: {exc}")

    # Endpoint 2: Yahoo Finance v7
    try:
        resp2 = requests.get(
            "https://query1.finance.yahoo.com/v7/finance/quote",
            params={"symbols": "^GSPC"},
            timeout=_T_YAHOO,
            headers={"User-Agent": ua}
        )
        if resp2.status_code == 200:
            result2 = resp2.json().get("quoteResponse", {}).get("result", [])
            if result2:
                price = _to_float(result2[0].get("regularMarketPrice")
                                  or result2[0].get("postMarketPrice")
                                  or result2[0].get("regularMarketPreviousClose"))
                if price and price > 1000:
                    _debug_set("spx_price", "ok")
                    _debug_set("price_source", "yahoo_v7:^GSPC")
                    return price
    except Exception as exc:
        _debug_error(f"Yahoo v7 error: {exc}")

    # Endpoint 3: stooq (بديل موثوق مجاني)
    try:
        resp3 = requests.get(
            "https://stooq.com/q/l/?s=%5Espx&f=sd2t2ohlcv&h&e=csv",
            timeout=_T_STOOQ,
            headers={"User-Agent": ua}
        )
        if resp3.status_code == 200:
            lines = resp3.text.strip().split("\n")
            if len(lines) >= 2:
                parts = lines[1].split(",")
                if len(parts) >= 5:
                    price = _to_float(parts[4])  # Close price
                    if price and price > 1000:
                        _debug_set("spx_price", "ok")
                        _debug_set("price_source", "stooq:^SPX")
                        return price
    except Exception as exc:
        _debug_error(f"stooq error: {exc}")

    return None


# ── Market Context: VIX / EMA20-50 / IV Rank ────────────────────────────────

def _fetch_yahoo_closes(symbol: str, range_: str = "1y", interval: str = "1d") -> List[float]:
    """Return daily closes from Yahoo chart API. Used for EMA and IV-rank proxy."""
    key = (symbol, range_, interval)
    if key in _YAHOO_HISTORY_CACHE:
        return _YAHOO_HISTORY_CACHE[key]
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
    closes: List[float] = []
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
        resp = requests.get(url, params={"range": range_, "interval": interval}, timeout=_T_YAHOO,
                            headers={"User-Agent": ua, "Accept": "application/json"})
        if resp.status_code == 200:
            result = (resp.json().get("chart", {}).get("result") or [None])[0]
            if result:
                quote_block = ((result.get("indicators") or {}).get("quote") or [{}])[0]
                raw_closes = quote_block.get("close") or []
                closes = [float(x) for x in raw_closes if x is not None and _to_float(x) is not None]
    except Exception as exc:
        _debug_error(f"Yahoo closes {symbol}: {exc}")
    _YAHOO_HISTORY_CACHE[key] = closes
    return closes


def _ema(values: List[float], period: int) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(vals[:period]) / period
    for v in vals[period:]:
        ema = (v * k) + (ema * (1 - k))
    return round(ema, 2)


def _last_value(values: List[float]) -> Optional[float]:
    return round(values[-1], 2) if values else None


def _atm_iv_percent(chain_data: Optional[Dict[str, Any]], current_price: float) -> Optional[float]:
    """Estimate current IV from nearest ATM options. Returns percent, e.g. 18.4."""
    if not chain_data:
        return None
    opts = chain_data.get("calls", []) + chain_data.get("puts", [])
    candidates = []
    for opt in opts:
        iv = _to_float(opt.get("iv"))
        strike = _to_float(opt.get("strike"))
        if iv is None or strike is None or iv <= 0:
            continue
        iv_pct = iv * 100 if iv <= 2.5 else iv
        # Ignore obviously broken values.
        if 1 <= iv_pct <= 250:
            candidates.append((abs(strike - current_price), iv_pct))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    top = [x[1] for x in candidates[:8]]
    top.sort()
    mid = len(top) // 2
    if len(top) % 2:
        return round(top[mid], 2)
    return round((top[mid - 1] + top[mid]) / 2, 2)


def _rank_value(current: Optional[float], history: List[float]) -> Optional[float]:
    """IV Rank = (current - min) / (max - min) * 100"""
    if current is None or not history:
        return None
    lo, hi = min(history), max(history)
    if hi <= lo:
        return None
    return round(max(0, min(100, (current - lo) / (hi - lo) * 100)), 1)


def _percentile_value(current: Optional[float], history: List[float]) -> Optional[float]:
    """
    IV Percentile = نسبة الأيام التي كان فيها IV أقل من الحالي.
    أدق من IV Rank لأنه يعتمد على التوزيع الفعلي لا على القمة/القاع فقط.
    """
    if current is None or not history:
        return None
    below = sum(1 for h in history if h < current)
    return round(below / len(history) * 100, 1)


def classify_iv_regime(iv_percentile: Optional[float]) -> dict:
    """
    حالتان فقط:
      < 60%  → Debit-favored  | Call/Put Debit | DTE 30-45
      >= 60% → Credit-favored | Bull Put / Bear Call | DTE 7-14
    """
    if iv_percentile is None:
        return {
            "regime":        "Unknown",
            "favors":        "debit",
            "credit_ok":     False,
            "debit_ok":      True,
            "credit_strong": False,
            "debit_strong":  False,
            "reason":        "IV Percentile unavailable — defaulting to Debit-favored",
        }
    p = iv_percentile
    if p < 60:
        return {
            "regime":        "Debit-favored",
            "favors":        "debit",
            "credit_ok":     False,
            "debit_ok":      True,
            "credit_strong": False,
            "debit_strong":  False,
            "reason":        f"IV Percentile={p:.0f}% (<60%) — favors Debit (DTE 30-45)",
        }
    return {
        "regime":        "Credit-favored",
        "favors":        "credit",
        "credit_ok":     True,
        "debit_ok":      False,
        "credit_strong": True,
        "debit_strong":  False,
        "reason":        f"IV Percentile={p:.0f}% (>=60%) — favors Credit (DTE 7-14)",
    }


def _combine_daily_intraday_trend(daily: str, intraday: str) -> str:
    """يدمج اتجاه Daily مع اتجاه 15m — Daily هو التحيز، 15m هو فلتر الدخول."""
    daily    = daily    or "neutral"
    intraday = intraday or "neutral"
    bull = {"strong_bullish", "bullish", "slightly_bullish"}
    bear = {"strong_bearish", "bearish", "slightly_bearish"}
    if intraday == "neutral":
        return daily
    if daily == "neutral":
        return intraday
    if daily in bull and intraday in bull:
        return "strong_bullish" if daily == "strong_bullish" else "bullish"
    if daily in bear and intraday in bear:
        return "strong_bearish" if daily == "strong_bearish" else "bearish"
    if daily in bull and intraday in bear:
        return "bullish_pullback"   # يومي صاعد لكن 15m هابط — انتظر
    if daily in bear and intraday in bull:
        return "bearish_bounce"     # يومي هابط لكن 15m صاعد — انتظر
    return "neutral"


def _ema_extension_warning(price: float, ema20: Optional[float],
                           closes: Optional[list] = None) -> str:
    """
    يكتشف إذا كان السعر ممتداً بشكل غير طبيعي فوق/تحت EMA20.
    لا يغير قرار الدخول — تحذير فقط.

    المنطق:
      dist_pct = (price - EMA20) / EMA20 × 100
      إذا > 2%  → تحذير امتداد (قد يكون دخول متأخر)
      إذا > 4%  → تحذير قوي (التصحيح محتمل)

    يعيد: رسالة تحذير أو "" إذا لا تحذير
    """
    if not ema20 or not price:
        return ""
    dist_pct = (price - ema20) / ema20 * 100

    if abs(dist_pct) > 4.0:
        direction = "فوق" if dist_pct > 0 else "تحت"
        return (f"Price extended {direction} EMA20 by {abs(dist_pct):.1f}% — "
                f"pullback risk elevated, entry may be late")
    elif abs(dist_pct) > 2.0:
        direction = "فوق" if dist_pct > 0 else "تحت"
        return (f"Price {abs(dist_pct):.1f}% {direction} EMA20 — "
                f"monitor for pullback before entry")
    return ""


def _trend_from_ema(price: float, ema20: Optional[float], ema50: Optional[float]) -> str:
    if not ema20 or not ema50:
        return "neutral"
    dist_pct = (price - ema20) / ema20 * 100 if ema20 else 0
    if price > ema20 > ema50:
        return "strong_bullish" if dist_pct > 0.5 else "bullish"
    if price < ema20 < ema50:
        return "strong_bearish" if dist_pct < -0.5 else "bearish"
    if price > ema20 and ema20 <= ema50:
        return "slightly_bullish"
    if price < ema20 and ema20 >= ema50:
        return "slightly_bearish"
    return "neutral"


def _fetch_dxlink_equity_price(tok: Optional[str], symbol: str) -> Tuple[Optional[float], str, str]:
    """
    v3.33.7 RC15 — جلب السعر الحي للـ Swing من DXLink/Tastytrade.

    الترتيب:
    1. جرّب الرموز المعروفة من _SYMBOL_DX
    2. إذا رجع سعر خارج النطاق (instrument خاطئ) → استعلم عن streamer-symbol الحقيقي
       من /instruments/equities/{ticker} وجرّب مرة أخرى، ثم حدّث _SYMBOL_DX
    """
    if not tok:
        return None, "", "no_token"
    symbol_up = symbol.upper()
    min_price = _SYMBOL_MIN_PRICE.get(symbol_up, 1)
    max_price = _SYMBOL_MAX_PRICE.get(symbol_up, 1_000_000)

    def _try_syms(syms: List[str]) -> Tuple[Optional[float], str, str]:
        try:
            snap = fetch_market_data_snapshot(tok, syms, timeout_seconds=6.0, max_symbols=5)
        except Exception as exc:
            return None, "", f"dxlink_price_failed:{exc.__class__.__name__}"
        for sym in syms:
            entry = snap.get(sym, {}) or {}
            bid = entry.get("bid")
            ask = entry.get("ask")
            last = entry.get("last")
            prev = entry.get("prev-close")
            if bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                if min_price < mid <= max_price:
                    return round(mid, 2), f"DXLink/Tastytrade bid/ask:{sym}", ""
                # سعر خارج النطاق → الرمز يشير لأداة خاطئة، أبلغ بذلك
                return None, "", f"dxlink_price_out_of_range:{sym}:{mid:.2f}"
            if last and min_price < float(last) <= max_price:
                return round(float(last), 2), f"DXLink/Tastytrade last:{sym}", ""
            if prev and min_price < float(prev) <= max_price:
                return round(float(prev), 2), f"DXLink/Tastytrade prev_close:{sym}", ""
        return None, "", "dxlink_snapshot_no_valid_price"

    # المحاولة الأولى بالرموز المعروفة
    dx_syms = list(_SYMBOL_DX.get(symbol_up, [symbol_up]))
    price, source, err = _try_syms(dx_syms)
    if price is not None:
        return price, source, err

    # إذا كان الخطأ بسبب نطاق خاطئ → استعلم عن streamer-symbol الحقيقي
    if "out_of_range" in err or err == "dxlink_snapshot_no_valid_price":
        try:
            from core.dxlink_client import get_equity_streamer_symbol
            real_sym = get_equity_streamer_symbol(tok, symbol_up)
        except Exception:
            real_sym = None
        if real_sym and real_sym not in dx_syms:
            # جرّب بالرمز الحقيقي
            price2, source2, err2 = _try_syms([real_sym])
            if price2 is not None:
                # حدّث الخريطة لتسريع الاستدعاءات القادمة
                _SYMBOL_DX[symbol_up] = [real_sym]
                return price2, source2, err2
            err = err2 or err

    return None, "", err


def get_4h_trend(symbol: str, tok: Optional[str] = None) -> Dict[str, Any]:
    """
    يحسب اتجاه 4H بناءً على EMA20 و EMA50.
    يُستخدم حصراً لـ Swing ETFs (SPY/QQQ/IWM/DIA).

    v3.33.6a RC6:
    - يحاول جلب شموع 4H الحقيقية من DXLink/Tastytrade أولاً.
    - إذا وصلت >= 50 شمعة، يحسب EMA20/EMA50 منها مباشرة (ema_source = DXLink Candle).
    - إذا فشل DXLink أو الشموع غير كافية، يرجع إلى Yahoo chart API (fallback).
    - سعر القرار يبقى من DXLink live price عند توفره.
    """
    yahoo_map = {"SPX": "^GSPC", "SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "DIA": "DIA", "AAPL": "AAPL", "NVDA": "NVDA", "GLD": "GLD"}
    ticker = yahoo_map.get(symbol.upper(), symbol)
    live_price, live_price_source, live_price_error = _fetch_dxlink_equity_price(tok, symbol)

    base = {
        "source": "",
        "price_source": live_price_source or "Yahoo 4H proxy close",
        "ema_source": "",
        "ticker": ticker,
        "interval_requested": "4h",
        "range_requested": "90d",
        "resample_method": "DXLink Candle 4H native",
        "raw_bars": 0,
        "bars_4h": 0,
        "live_price_error": live_price_error,
        "trend_updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "decision_rule": "RC6b strict Swing: bullish if Price > EMA20 > EMA50 and Price vs EMA20 >= +0.30%; bearish if Price < EMA20 < EMA50 and Price vs EMA20 <= -0.30%; otherwise neutral/mixed",
    }

    closes_4h: Optional[List[float]] = None
    yahoo_proxy_price: Optional[float] = None
    candle_price_source: str = "Yahoo 4H proxy close"
    dxlink_candle_error: str = ""

    # ── 1. محاولة DXLink Candle ────────────────────────────────────────────────
    if tok:
        try:
            from core.dxlink_client import fetch_dxlink_candles_snapshot
            candles = fetch_dxlink_candles_snapshot(tok, symbol, period="4h", days_back=90)
            if candles and len(candles) >= 50:
                closes_4h = [c["close"] for c in candles if c.get("close")]
                base["raw_bars"] = len(candles)
                base["bars_4h"] = len(closes_4h)
                base["ema_source"] = "DXLink Candle 4H"
                base["resample_method"] = "DXLink Candle 4H native"
                candle_price_source = f"DXLink Candle 4H | {symbol}"
            else:
                dxlink_candle_error = f"DXLink candles insufficient: got {len(candles) if candles else 0} < 50"
        except Exception as exc:
            dxlink_candle_error = f"DXLink candle failed: {exc.__class__.__name__}: {exc}"

    # ── 2. Fallback: Yahoo chart API ──────────────────────────────────────────
    if not closes_4h:
        base["ema_source"] = "Yahoo chart API (fallback)"
        base["resample_method"] = "1h closes sampled every 4 bars as 4H proxy"
        base["interval_requested"] = "1h"
        base["range_requested"] = "60d"
        candle_price_source = "Yahoo 4H proxy close"
        try:
            closes_1h = _fetch_yahoo_closes(ticker, "60d", "1h")
            base["raw_bars"] = len(closes_1h or [])
            closes_4h_resampled = closes_1h[::4] if closes_1h and len(closes_1h) >= 4 else closes_1h
            base["bars_4h"] = len(closes_4h_resampled or [])
            if closes_4h_resampled and len(closes_4h_resampled) >= 50:
                closes_4h = closes_4h_resampled
                yahoo_proxy_price = closes_4h[-1]
        except Exception as exc:
            err = f"Yahoo 4H fetch failed: {exc}"
            base["source"] = "error"
            return {**base, "trend": "neutral", "ema20": None, "ema50": None,
                    "price": live_price, "dxlink_candle_error": dxlink_candle_error,
                    "error": err}

    if not closes_4h or len(closes_4h) < 50:
        base["source"] = "DXLink + Yahoo — insufficient data"
        return {**base, "trend": "neutral", "ema20": None, "ema50": None,
                "price": live_price, "dxlink_candle_error": dxlink_candle_error,
                "error": f"بيانات 4H غير كافية: bars_4h={base['bars_4h']} < 50"}

    ema20 = _ema(closes_4h, 20)
    ema50 = _ema(closes_4h, 50)
    if yahoo_proxy_price is None:
        yahoo_proxy_price = closes_4h[-1]

    # سعر القرار: DXLink Quote إذا صحيح، وإلا آخر إغلاق Candle (نفس المصدر الذي منه EMA)
    if live_price:
        price = live_price
        effective_price_source = live_price_source
    else:
        price = yahoo_proxy_price
        effective_price_source = candle_price_source
    base["price_source"] = effective_price_source

    if not ema20 or not ema50:
        base["source"] = base["ema_source"]
        return {**base, "trend": "neutral", "ema20": ema20, "ema50": ema50,
                "price": price, "dxlink_candle_error": dxlink_candle_error,
                "error": "تعذّر حساب EMA"}

    base["source"] = (
        f"DXLink live price + {base['ema_source']}" if live_price
        else base["ema_source"]
    )

    # v3.33.6a RC6b — Strict Swing EMA Trend Classification
    # Swing must remain conservative: a confirmed trend requires both EMA order
    # alignment and a minimum distance from EMA20. Transitional states are
    # labelled as pressure only, and remain neutral for Swing qualification.
    price_vs_ema20_pct = ((price - ema20) / ema20 * 100) if ema20 else 0
    price_vs_ema50_pct = ((price - ema50) / ema50 * 100) if ema50 else 0
    ema20_vs_ema50_pct = ((ema20 - ema50) / ema50 * 100) if ema50 else 0
    min_price_ema20_dist_pct = 0.30

    def _ema_order_label(price_val: float, ema20_val: float, ema50_val: float) -> str:
        vals = [(price_val, "Price"), (ema20_val, "EMA20"), (ema50_val, "EMA50")]
        vals.sort(key=lambda x: x[0])
        return " < ".join(label for _, label in vals)

    ema_order = _ema_order_label(price, ema20, ema50)
    confirmed_trend = False
    trend_pressure_label = ""

    if price < ema20 and ema20 < ema50 and price_vs_ema20_pct <= -min_price_ema20_dist_pct:
        trend = "bearish"
        confirmed_trend = True
        neutral_reason = ""
        trend_strength_reason = (
            f"confirmed bearish: Price < EMA20 < EMA50 and "
            f"Price vs EMA20 {price_vs_ema20_pct:+.3f}% <= -{min_price_ema20_dist_pct:.2f}%"
        )
    elif price > ema20 and ema20 > ema50 and price_vs_ema20_pct >= min_price_ema20_dist_pct:
        trend = "bullish"
        confirmed_trend = True
        neutral_reason = ""
        trend_strength_reason = (
            f"confirmed bullish: Price > EMA20 > EMA50 and "
            f"Price vs EMA20 {price_vs_ema20_pct:+.3f}% >= +{min_price_ema20_dist_pct:.2f}%"
        )
    else:
        trend = "neutral"
        if price < ema20 and price < ema50 and ema20 > ema50:
            trend_pressure_label = "bearish pressure"
            neutral_reason = (
                "Price below EMA20/EMA50 but EMA20 still above EMA50 — "
                "bearish pressure, not confirmed bearish trend"
            )
        elif price > ema20 and price > ema50 and ema20 < ema50:
            trend_pressure_label = "bullish pressure"
            neutral_reason = (
                "Price above EMA20/EMA50 but EMA20 still below EMA50 — "
                "bullish pressure, not confirmed bullish trend"
            )
        elif abs(price_vs_ema20_pct) < min_price_ema20_dist_pct:
            trend_pressure_label = "weak trend"
            neutral_reason = (
                f"Price too close to EMA20 ({price_vs_ema20_pct:+.3f}%, "
                f"minimum ±{min_price_ema20_dist_pct:.2f}%) — weak trend, not confirmed"
            )
        else:
            trend_pressure_label = "mixed"
            neutral_reason = "EMA order does not meet strict Swing confirmation rule"
        trend_strength_reason = neutral_reason

    return {
        **base,
        "trend": trend,
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "price": round(price, 2),
        "yahoo_proxy_price": round(yahoo_proxy_price, 2),
        "price_vs_ema20_pct": round(price_vs_ema20_pct, 3),
        "price_vs_ema50_pct": round(price_vs_ema50_pct, 3),
        "ema20_vs_ema50_pct": round(ema20_vs_ema50_pct, 3),
        "ema_order": ema_order,
        "trend_strength_reason": trend_strength_reason,
        "trend_pressure_label": trend_pressure_label,
        "confirmed_trend": confirmed_trend,
        "min_price_ema20_dist_pct": min_price_ema20_dist_pct,
        "neutral_reason": neutral_reason,
        "dxlink_candle_error": dxlink_candle_error,
        "error": None,
    }

def trend_label(trend: str) -> str:
    """تحويل trend string إلى تسمية واضحة للعرض"""
    mapping = {
        "strong_bullish":   "صاعد قوي",
        "bullish":          "صاعد",
        "slightly_bullish": "صاعد ضعيف",
        "bullish_pullback": "صاعد / 15m تراجع",
        "neutral":          "محايد",
        "slightly_bearish": "هابط ضعيف",
        "bearish":          "هابط",
        "strong_bearish":   "هابط قوي",
        "bearish_bounce":   "هابط / 15m ارتداد",
    }
    return mapping.get(trend, trend)


def get_market_context(chain_data: Optional[Dict[str, Any]], current_price: float, symbol: str = "SPX", token: Optional[str] = None) -> Dict[str, Any]:
    """Fetch VIX, EMA20/50 and IV Rank.

    IV Now يأتي من ATM option IV في الـ chain.
    IV Rank/Percentile لا تُعرض إلا إذا كان لدينا سجل ATM IV كافٍ.
    لا نستخدم VIX كبديل لحساب IV Rank حتى لا يظهر رقم مضلل.
    """
    # Cache key: تقريب لأقرب 5 نقاط (يمنع miss عند كل تغيير طفيف في السعر)
    # TTL: 15 دقيقة — context لا يتغير بسرعة كبيرة
    _CTX_CACHE_TTL = 300   # 5 دقائق
    price_bucket   = round(current_price / 5) * 5
    cache_key      = f"ctx:{symbol.upper()}:{price_bucket}:{date.today().isoformat()}"
    cached         = _MARKET_CONTEXT_CACHE.get(cache_key)
    if cached and time.time() - cached[0] < _CTX_CACHE_TTL:
        return dict(cached[1])

    # تنظيف الإدخالات القديمة (يمنع تراكم الذاكرة في الجلسات الطويلة)
    now_ts = time.time()
    stale  = [k for k, (ts, _) in _MARKET_CONTEXT_CACHE.items()
              if now_ts - ts > _CTX_CACHE_TTL * 2]
    for k in stale:
        _MARKET_CONTEXT_CACHE.pop(k, None)

    spx_closes    = _fetch_yahoo_closes("^GSPC", "6mo", "1d")
    spx_15m_closes = _fetch_yahoo_closes("^GSPC", "5d", "15m")
    vix_closes    = _fetch_yahoo_closes("^VIX", "1y", "1d")

    ema20     = _ema(spx_closes, 20)
    ema50     = _ema(spx_closes, 50)
    ema20_15m = _ema(spx_15m_closes, 20)
    ema50_15m = _ema(spx_15m_closes, 50)
    vix       = _last_value(vix_closes)
    atm_iv    = _atm_iv_percent(chain_data, current_price)

    # الاتجاه اليومي + 15m + المدمج
    daily_trend    = _trend_from_ema(current_price, ema20, ema50)
    intraday_trend = _trend_from_ema(current_price, ema20_15m, ema50_15m)
    combined_trend = _combine_daily_intraday_trend(daily_trend, intraday_trend)

    # IV Rank + IV Percentile
    # مبدأ v3.26:
    # - IV Now يؤخذ من ATM option IV في الـ chain.
    # - IV Rank/Percentile تؤخذ أولاً من tastytrade market metrics إذا وفرها الـ API.
    # - إذا لم يوفرها broker، نستخدم تاريخ ATM IV المحلي فقط إذا كان كافياً.
    # - لا نستخدم VIX proxy ولا سجل قصير جدًا حتى لا يظهر رقم مضلل.
    iv_rank        = None
    iv_percentile  = None
    iv_rank_source = "N/A — broker IV Rank unavailable; need >=20 stored ATM IV days"
    current_iv     = atm_iv

    broker_metrics = fetch_tastytrade_market_metrics(symbol, token=token)
    if broker_metrics.get("available"):
        broker_rank = broker_metrics.get("iv_rank")
        broker_pct  = broker_metrics.get("iv_percentile")
        broker_ivx  = broker_metrics.get("iv_index")
        if broker_rank is not None:
            iv_rank = broker_rank
        if broker_pct is not None:
            iv_percentile = broker_pct
        if current_iv is None and broker_ivx is not None:
            current_iv = broker_ivx
        iv_rank_source = broker_metrics.get("source", "tastytrade_market_metrics")
        if iv_rank is None and iv_percentile is None:
            iv_rank_source = f"N/A — broker metrics present but IVR/IV%tile fields missing ({broker_metrics.get('source')})"
    elif atm_iv is not None:
        try:
            iv_records = get_iv_history(252, symbol=symbol)
            hist_iv    = [r["atm_iv"] for r in iv_records if r.get("atm_iv")]
            if len(hist_iv) >= 20:
                iv_rank        = _rank_value(atm_iv, hist_iv)
                iv_percentile  = _percentile_value(atm_iv, hist_iv)
                iv_rank_source = f"real_atm_iv_history ({len(hist_iv)} days)"
            else:
                iv_rank        = None
                iv_percentile  = None
                iv_rank_source = f"N/A — insufficient ATM IV history ({len(hist_iv)}/20 days); broker metrics unavailable: {broker_metrics.get('reason')}"
        except Exception as exc:
            iv_rank        = None
            iv_percentile  = None
            iv_rank_source = f"N/A — IV history unavailable ({exc.__class__.__name__}); broker metrics unavailable: {broker_metrics.get('reason')}"
    else:
        iv_rank_source = f"N/A — ATM option IV unavailable; broker metrics unavailable: {broker_metrics.get('reason')}"

    if atm_iv is not None:
        try:
            save_iv_history(date.today().isoformat(), atm_iv, vix or 0.0, current_price, symbol=symbol)
        except Exception:
            pass

    # إذا iv_percentile مفقودة لكن iv_rank متاح، استخدم iv_rank كبديل للـ regime
    iv_percentile_for_regime = iv_percentile
    iv_regime_proxy_used = False
    if iv_percentile_for_regime is None and iv_rank is not None:
        iv_percentile_for_regime = iv_rank
        iv_regime_proxy_used = True

    iv_regime_data = classify_iv_regime(iv_percentile_for_regime)
    if iv_regime_proxy_used:
        iv_regime_data["regime_note"] = f"IV Percentile unavailable — IV Rank ({iv_rank:.1f}) used as proxy"

    context = {
        "ema20":           ema20,
        "ema50":           ema50,
        "daily_trend":     daily_trend,
        "ema20_15m":       ema20_15m,
        "ema50_15m":       ema50_15m,
        "intraday_trend":  intraday_trend,
        "trend":           combined_trend,
        "combined_trend":  combined_trend,
        "vix":             vix,
        "iv_current":      round(current_iv, 2) if current_iv is not None else None,
        "iv_rank":                  iv_rank,
        "iv_percentile":            iv_percentile,
        "iv_percentile_for_regime": iv_percentile_for_regime,
        "iv_regime_proxy_used":     iv_regime_proxy_used,
        "iv_rank_source":           iv_rank_source,
        "iv_regime":                iv_regime_data["regime"],
        "iv_regime_data":           iv_regime_data,
        "broker_metrics_raw_keys":  broker_metrics.get("raw_keys", []),
    }
    _MARKET_CONTEXT_CACHE[cache_key] = (time.time(), dict(context))
    return context



def _compact_preview(data: Any, max_chars: int = 6000) -> str:
    """مختصر آمن لأول جزء من استجابة API لعرضه في فحص النظام/PowerShell."""
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2)
    except Exception:
        text = str(data)
    return text[:max_chars]


def _extract_expirations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Tastytrade قد يغيّر شكل JSON بين:
    data.items = [expirations]
    أو data.items[0].expirations = [...]
    أو data.items[0].nested-option-chain = [...]
    لذلك نبحث بشكل مرن عن أي dict يحتوي strikes.
    """
    found: List[Dict[str, Any]] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if isinstance(obj.get("strikes"), list):
                found.append(obj)
            # مفاتيح محتملة تحتوي expirations
            for key in ("items", "expirations", "nested-option-chain", "option-chains", "chains", "data"):
                if key in obj:
                    walk(obj[key])
            # fallback عام حتى لا نعتمد على أسماء مفاتيح محددة فقط
            for v in obj.values():
                if isinstance(v, (list, dict)):
                    walk(v)
        elif isinstance(obj, list):
            for x in obj:
                walk(x)

    walk(payload)

    # إزالة التكرار مع الحفاظ على الترتيب
    unique: List[Dict[str, Any]] = []
    seen = set()
    for exp in found:
        key = (exp.get("expiration-date"), exp.get("expiration-type"), len(exp.get("strikes") or []))
        if key not in seen:
            seen.add(key)
            unique.append(exp)
    return unique


def _leg_symbol(leg: Any) -> Optional[str]:
    """Return the tradable/API option symbol when possible."""
    sym, _streamer = _leg_symbol_and_streamer(leg)
    return sym


def _leg_symbol_and_streamer(leg: Any) -> Tuple[Optional[str], Optional[str]]:
    """Extract both option symbol and DXLink streamer-symbol from a chain leg.

    Tastytrade nested chains may return call/put as a string or as an object.
    DXLink needs streamer-symbol, while other REST endpoints use symbol.
    """
    if not leg:
        return None, None
    if isinstance(leg, str):
        return leg, None
    if isinstance(leg, dict):
        sym = None
        streamer = None
        for k in ("symbol", "option-symbol", "occ-symbol"):
            if leg.get(k):
                sym = str(leg.get(k))
                break
        for k in ("streamer-symbol", "streamerSymbol", "dxlink-symbol"):
            if leg.get(k):
                streamer = str(leg.get(k))
                break
        return sym or streamer, streamer
    return None, None



def _leg_metadata(leg: Any, strike_row: Dict[str, Any], side: str) -> Dict[str, Any]:
    """Extract OI/volume and any static greek/price fields available in nested chain.

    Some tastytrade chain responses provide only symbols; others include
    open-interest/volume or quote fields inside the call/put object or strike row.
    This keeps those values instead of losing them when DXLink has no Summary event.
    """
    meta: Dict[str, Any] = {}
    sources: List[Dict[str, Any]] = []
    if isinstance(leg, dict):
        sources.append(leg)
    sources.append(strike_row)

    prefixes = [side, side.replace("-", "_")]
    field_candidates = {
        "open-interest": ["open-interest", "openInterest", "open_interest", f"{side}-open-interest", f"{side}OpenInterest", f"{side}_open_interest"],
        "volume": ["volume", f"{side}-volume", f"{side}Volume", f"{side}_volume"],
        "bid": ["bid", "bid-price", "bidPrice", f"{side}-bid", f"{side}Bid"],
        "ask": ["ask", "ask-price", "askPrice", f"{side}-ask", f"{side}Ask"],
        "last": ["last", "last-price", "lastPrice", "mark", f"{side}-last", f"{side}Last"],
        "delta": ["delta", "greeks-delta", "greeksDelta", f"{side}-delta", f"{side}Delta"],
        "gamma": ["gamma", "greeks-gamma", "greeksGamma", f"{side}-gamma", f"{side}Gamma"],
        "implied-volatility": ["implied-volatility", "impliedVolatility", "iv", "volatility", f"{side}-iv", f"{side}Iv"],
    }
    for out_key, keys in field_candidates.items():
        for src in sources:
            for k in keys:
                if k in src and src.get(k) not in (None, "", "NaN"):
                    meta[out_key] = src.get(k)
                    break
            if out_key in meta:
                break
    return meta

def _select_target_expiration(expirations: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    today = date.today().strftime("%Y-%m-%d")
    for exp in expirations:
        if exp.get("expiration-date") == today:
            return exp
    return expirations[0] if expirations else None


def _select_swing_expiration(
    expirations: List[Dict[str, Any]],
    min_dte: int = 12,
    max_dte: int = 17,
    target_expiry_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    يختار أقرب expiry ضمن نطاق DTE المحدد.
    في v3.18 لا يوجد مسار Swing ثابت قديم؛ يتم تمرير النطاق من analyze_swing حسب نوع الاستراتيجية.
    يعيد None إذا لا يوجد expiry في النطاق.
    """
    today = date.today()
    if target_expiry_date:
        # تطبيع التنسيق: YYYYMMDD ↔ YYYY-MM-DD قبل المقارنة
        def _nd(d):
            s = str(d or "").replace("-", "")
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else str(d or "")
        target_norm = _nd(target_expiry_date)
        for exp in expirations:
            if _nd(exp.get("expiration-date")) == target_norm:
                return exp
        return None

    candidates: List[Tuple[int, Dict[str, Any]]] = []
    for exp in expirations:
        exp_date_str = exp.get("expiration-date")
        if not exp_date_str:
            continue
        try:
            exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
            dte = (exp_date - today).days
            if min_dte <= dte <= max_dte:
                candidates.append((dte, exp))
        except Exception:
            continue
    if not candidates:
        return None
    # v3.21: اختر أقرب DTE إلى منتصف النطاق المسموح.
    # Credit 12-17 → target≈14.5
    # Debit  30-45 → target≈37.5 (يفضل عملياً 35-40 عند توفرها)
    target_dte = (min_dte + max_dte) / 2.0
    candidates.sort(key=lambda x: (abs(x[0] - target_dte), x[0]))
    return candidates[0][1]


# ── SPX Settlement Type ───────────────────────────────────────────────────────

def classify_spx_expiry(expiry_date: str,
                        expiration_type: str = "") -> Dict[str, str]:
    """
    يميّز بين نوعين من expirations لـ SPX:

    AM-settled  (SPX standard):
      - الجمعة الثالثة من كل شهر
      - يتسوى بسعر الافتتاح (SOQ) — لا يمكن تداوله بعد 09:15 ET
      - root = "SPX"

    PM-settled  (SPXW weekly):
      - كل يوم اثنين / أربعاء / جمعة (غير الثالثة)
      - يتسوى عند إغلاق السوق 16:00 ET
      - root = "SPXW"

    يعيد dict:
      settlement_type : "AM" | "PM"
      root_symbol     : "SPX" | "SPXW"
      last_trade_time : "09:15 ET" | "15:55 ET"
    """
    try:
        d = datetime.strptime(expiry_date, "%Y-%m-%d").date()
    except Exception:
        return {"settlement_type": "PM", "root_symbol": "SPXW",
                "last_trade_time": "15:55 ET"}

    # تحقق من مصدر API أولاً إذا كان متاحاً
    if expiration_type and expiration_type.lower() == "standard":
        return {"settlement_type": "AM", "root_symbol": "SPX",
                "last_trade_time": "09:15 ET"}
    if expiration_type and expiration_type.lower() in ("weekly", "quarterly"):
        return {"settlement_type": "PM", "root_symbol": "SPXW",
                "last_trade_time": "15:55 ET"}

    # الجمعة الثالثة من الشهر = يوم رقمه بين 15 و21 وهو جمعة (weekday=4)
    is_am = (d.weekday() == 4) and (15 <= d.day <= 21)

    if is_am:
        return {"settlement_type": "AM", "root_symbol": "SPX",
                "last_trade_time": "09:15 ET"}
    return {"settlement_type": "PM", "root_symbol": "SPXW",
            "last_trade_time": "15:55 ET"}


# ── Options Chain ─────────────────────────────────────────────────────────────

# roots افتراضية لكل رمز
_SYMBOL_ROOTS: Dict[str, List[str]] = {
    "SPX": ["SPX", "$SPX.X", "SPXW", ".SPX", "^SPX"],
    "SPY": ["SPY"],
    "QQQ": ["QQQ"],
    "IWM": ["IWM"],
    "DIA": ["DIA"],
    "AAPL": ["AAPL"],
    "NVDA": ["NVDA"],
    "GLD": ["GLD"],
}

# رموز DXLink لكل رمز (للسعر الحي)
_SYMBOL_DX: Dict[str, List[str]] = {
    "SPX": [".SPX", "SPX", "$SPX.X"],
    "SPY": ["SPY"],
    "QQQ": ["QQQ"],
    "IWM": ["IWM"],
    "DIA": ["DIA"],
    "AAPL": ["AAPL"],
    "NVDA": ["NVDA"],
    "GLD": ["GLD"],
}

# رموز Yahoo Finance لكل رمز
_SYMBOL_YAHOO: Dict[str, str] = {
    "SPX": "^GSPC",
    "SPY": "SPY",
    "QQQ": "QQQ",
    "IWM": "IWM",
    "DIA": "DIA",
    "AAPL": "AAPL",
    "NVDA": "NVDA",
    "GLD": "GLD",
}

# الحد الأدنى للسعر (للتحقق من صحة الاستجابة)
_SYMBOL_MIN_PRICE: Dict[str, float] = {
    "SPX": 1000,
    "SPY": 100,
    "QQQ": 50,
    "IWM": 50,
    "DIA": 100,
    "AAPL": 50,
    "NVDA": 50,
    "GLD": 150,
}

# الحد الأقصى للسعر — يمنع قبول قيم DXLink خاطئة (مثل سعر ذهب futures بدلاً من GLD ETF)
_SYMBOL_MAX_PRICE: Dict[str, float] = {
    "SPX": 20000,
    "SPY": 2000,
    "QQQ": 2000,
    "IWM": 500,
    "DIA": 1000,
    "AAPL": 1000,
    "NVDA": 2000,
    "GLD": 800,   # GLD ETF لا يتجاوز 800 حتى مع ذهب فوق $8000/oz
}

# RC13d — Swing-only watchlist expansion. AAPL/NVDA use the same Swing rules as ETFs,
# but never 0DTE. Manual announcement/earnings block is controlled by setting
# swing_manual_block_symbols, e.g. "AAPL,NVDA".
SWING_ONLY_SYMBOLS: Tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD")
SWING_EQUITY_SYMBOLS: Tuple[str, ...] = ("AAPL", "NVDA")

def _manual_swing_blocked(symbol: str) -> Tuple[bool, str]:
    """Return True when AAPL/NVDA Swing is manually disabled in Settings.

    RC13e UI semantics:
      - enable_aapl_swing = 1: AAPL Swing allowed
      - enable_aapl_swing = 0: AAPL Swing blocked
      - enable_nvda_swing = 1: NVDA Swing allowed
      - enable_nvda_swing = 0: NVDA Swing blocked

    Legacy comma-separated/manual-disable settings remain readable for safe migration.
    """
    sym = str(symbol or "").upper().strip()
    if sym not in SWING_EQUITY_SYMBOLS:
        return False, ""
    try:
        from core.database import get_setting

        enabled_raw = str(get_setting(f"enable_{sym.lower()}_swing", "") or "").strip().lower()
        if enabled_raw != "":
            enabled = enabled_raw in ("1", "true", "yes", "on")
            if not enabled:
                return True, f"{sym} Swing disabled manually in Settings (announcements/earnings/news)"
            return False, ""

        # Backward compatibility with RC13d.
        raw = str(get_setting("swing_manual_block_symbols", "") or "")
        blocked = {x.strip().upper() for x in raw.replace(";", ",").replace(" ", ",").split(",") if x.strip()}
        legacy_flag = str(get_setting(f"disable_{sym.lower()}_swing", "0") or "0").strip().lower()
        if sym in blocked or legacy_flag in ("1", "true", "yes", "on"):
            return True, f"{sym} Swing disabled manually in Settings (legacy announcement/earnings block)"
    except Exception as exc:
        # Safety setting: if the permission cannot be read, block the single-name
        # Swing path rather than accidentally trading through an earnings/news pause.
        return True, f"{sym} Swing settings unavailable — blocked safely: {exc}"
    return False, ""


def get_options_chain(
    token: Optional[str] = None,
    symbol: str = "SPX",
    target_dte_range: Optional[Tuple[int, int]] = None,
    target_expiry_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    جلب سلسلة خيارات لأي رمز (SPX, SPY, QQQ, ...) بشكل مرن.

    target_dte_range: (min_dte, max_dte) لاختيار expiry ضمن نطاق DTE محدد.
    None (افتراضي) = يوم اليوم (0DTE).
    """
    try:
        tok = token or _get_access_token()
        today = date.today().strftime("%Y-%m-%d")

        default_roots = _SYMBOL_ROOTS.get(symbol.upper(), [symbol])
        custom_root = get_setting("option_chain_root", "").strip() if symbol.upper() == "SPX" else ""
        roots = ([custom_root] if custom_root else []) + default_roots
        roots = [r for i, r in enumerate(roots) if r and r not in roots[:i]]

        selected_payload: Optional[Dict[str, Any]] = None
        selected_expirations: List[Dict[str, Any]] = []
        selected_root: Optional[str] = None

        for root in roots:
            try:
                from core.circuit_breaker import tastytrade_breaker, CircuitOpenError
                url = f"{API_BASE}/option-chains/{root}/nested"
                with tastytrade_breaker()():
                    resp = requests.get(url, headers=_auth_headers(tok), timeout=_T_API * 2)
                status = resp.status_code
                if status != 200:
                    _debug_error(f"option-chains/{root}/nested status={status}")
                    continue

                payload = resp.json()
                preview = _compact_preview(payload, 6000)
                _debug_set("chain_raw_preview", preview)
                _debug_set("chain_response_keys", list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)

                expirations = _extract_expirations(payload)
                strikes_count = sum(len(e.get("strikes") or []) for e in expirations)
                _debug_error(f"option-chains/{root}/nested OK: expirations={len(expirations)}, strikes={strikes_count}")

                # اطبع preview في PowerShell عند الفحص حتى يمكن إرساله عند الحاجة.
                print(f"[Tastytrade option-chain root={root}] expirations={len(expirations)} strikes={strikes_count}")
                if strikes_count == 0:
                    print("[Tastytrade raw preview - no strikes]")
                    print(preview[:2500])

                if expirations and strikes_count > 0:
                    selected_payload = payload
                    selected_expirations = expirations
                    selected_root = root
                    break
            except Exception as exc:
                _debug_error(f"option-chains/{root}/nested exception: {exc}")

        if not selected_expirations:
            _debug_set("chain", "failed_no_strikes")
            _debug_set("chain_calls", 0)
            _debug_set("chain_puts", 0)
            return None

        _debug_set("chain_root", selected_root)
        if target_expiry_date:
            target_exp = _select_swing_expiration(
                selected_expirations,
                target_expiry_date=target_expiry_date,
            )
            if not target_exp:
                available = [e.get("expiration-date","?") for e in selected_expirations[:12]]
                _debug_set("chain", f"failed_no_expiry_{target_expiry_date}")
                _debug_error(
                    f"Swing: requested expiry {target_expiry_date} not found. "
                    f"Available: {available}"
                )
                return None
        elif target_dte_range is not None:
            min_dte, max_dte = target_dte_range
            target_exp = _select_swing_expiration(selected_expirations, min_dte, max_dte)
            if not target_exp:
                available = [e.get("expiration-date","?") for e in selected_expirations[:8]]
                _debug_set("chain", f"failed_no_swing_expiry_{min_dte}-{max_dte}DTE")
                _debug_error(
                    f"Swing: لا يوجد expiry في نطاق {min_dte}-{max_dte} DTE. "
                    f"المتاح: {available}"
                )
                return None
        else:
            target_exp = _select_target_expiration(selected_expirations)
            if not target_exp:
                _debug_set("chain", "failed_no_expiration")
                return None

        call_symbols: List[str] = []
        put_symbols: List[str] = []
        strike_map: Dict[str, float] = {}
        streamer_map: Dict[str, str] = {}
        chain_static_data: Dict[str, Dict[str, Any]] = {}

        for s in target_exp.get("strikes", []):
            strike = _to_float(s.get("strike-price") or s.get("strike"), 0) or 0
            call, call_streamer = _leg_symbol_and_streamer(s.get("call"))
            put, put_streamer = _leg_symbol_and_streamer(s.get("put"))
            # Some payloads place streamer symbols at strike level.
            call_streamer = call_streamer or s.get("call-streamer-symbol") or s.get("callStreamerSymbol")
            put_streamer = put_streamer or s.get("put-streamer-symbol") or s.get("putStreamerSymbol")
            if call:
                call_symbols.append(call)
                strike_map[call] = strike
                chain_static_data[call] = _leg_metadata(s.get("call"), s, "call")
                if call_streamer:
                    streamer_map[call] = str(call_streamer)
            if put:
                put_symbols.append(put)
                strike_map[put] = strike
                chain_static_data[put] = _leg_metadata(s.get("put"), s, "put")
                if put_streamer:
                    streamer_map[put] = str(put_streamer)

        all_symbols = call_symbols + put_symbols
        if not all_symbols:
            _debug_set("chain", "failed_no_option_symbols")
            _debug_set("chain_calls", 0)
            _debug_set("chain_puts", 0)
            print("[Tastytrade target expiration has strikes but no call/put symbols]")
            print(_compact_preview(target_exp, 3000))
            return None

        # Build base rows from the nested option chain.  The old
        # /market-data/options route returns 404 in the current tastytrade API,
        # so DXLink is the source of Quote/Greeks/Summary enrichment.
        all_data: List[Dict[str, Any]] = []
        for sym in all_symbols:
            row = {"symbol": sym}
            row.update(chain_static_data.get(sym, {}))
            row.setdefault("open-interest", 0)
            row.setdefault("volume", 0)
            all_data.append(row)

        # احسب ATM strike الآن من strike_map المتاح
        _all_strikes_now = sorted(set(strike_map.values()))
        _atm_now = _all_strikes_now[len(_all_strikes_now) // 2] if _all_strikes_now else 0

        dx_data: Dict[str, Dict[str, Any]] = {}
        if streamer_map:
            try:
                sym_to_api = {streamer: api for api, streamer in streamer_map.items()}
                # رتّب: ATM أولاً — لكن نأخذ كل الرموز ضمن نطاق ±50 نقطة من ATM أولاً
                def _sort_key(ss):
                    strike = strike_map.get(sym_to_api.get(ss, ""), _atm_now)
                    dist   = abs(strike - _atm_now)
                    # أولوية قصوى للنطاق ±50 نقطة (يشمل كل strikes الاستراتيجيات)
                    priority = 0 if dist <= 50 else 1
                    return (priority, dist)

                streamer_symbols = sorted(list(streamer_map.values()), key=_sort_key)

                # نطاق الـ fetch = 2.5% من السعر — يضمن تغطية strikes الاستراتيجية (1.5σ)
                # مثال: SPX 7467 → 7467×0.025 = 186 نقطة → يشمل Bull Put عند 7360 ✓
                # مثال: SPY 745  → max(50, 18) = 50 نقطة (كافٍ لـ SPY/QQQ)
                _near_range = max(50, int(_atm_now * 0.025))

                near_symbols = [ss for ss in streamer_symbols
                                if abs(strike_map.get(sym_to_api.get(ss,""), _atm_now) - _atm_now) <= _near_range]
                _debug_error(f"DXLink fetch: near_range=±{_near_range} near={len(near_symbols)} total={len(streamer_symbols)}")

                dx_data = fetch_market_data_snapshot(tok, near_symbols, timeout_seconds=25.0,
                                                     max_symbols=len(near_symbols) + 10)
                # إذا رجع أقل من النصف → اجلب كل الرموز
                if not dx_data or len(dx_data) < len(near_symbols) // 2:
                    dx_data = fetch_market_data_snapshot(tok, streamer_symbols, timeout_seconds=15.0,
                                                         max_symbols=300)
                if not dx_data:
                    dx_data = fetch_market_data_snapshot(tok, streamer_symbols[:100],
                                                         timeout_seconds=10.0, max_symbols=100)
                # retry إذا رجعت بيانات بدون bid/ask (LIMITED) — انتظر ثانيتين وأعد المحاولة
                _has_bids = any(
                    v.get("bid") is not None
                    for v in (dx_data or {}).values()
                )
                if dx_data and not _has_bids:
                    _debug_error("DXLink: rows returned but no bid prices — retrying after 2s")
                    time.sleep(2)
                    dx_data2 = fetch_market_data_snapshot(tok, near_symbols, timeout_seconds=20.0,
                                                          max_symbols=len(near_symbols) + 10)
                    if dx_data2 and any(v.get("bid") is not None for v in dx_data2.values()):
                        dx_data = dx_data2
                        _debug_error(f"DXLink retry: {len(dx_data)} rows with bids")
                    else:
                        _debug_error("DXLink retry: still no bids — using what we have")
                _debug_error(f"DXLink result: {len(dx_data) if dx_data else 0} rows received  has_bids={_has_bids}")
            except Exception as exc:
                _debug_error(f"DXLink snapshot failed: {exc}")

        # Merge DXLink fields into base rows.
        if dx_data:
            for row in all_data:
                sym = row.get("symbol")
                streamer = streamer_map.get(sym)
                dx = dx_data.get(streamer or "") or dx_data.get(sym or "")
                if not dx:
                    continue
                if dx.get("bid") is not None: row["bid"] = dx.get("bid")
                if dx.get("ask") is not None: row["ask"] = dx.get("ask")
                if dx.get("last") is not None: row["last"] = dx.get("last")
                if dx.get("volume") is not None: row["volume"] = dx.get("volume")
                # Keep chain OI if DXLink Summary does not provide it.
                if dx.get("open-interest") is not None: row["open-interest"] = dx.get("open-interest")
                if dx.get("delta") is not None: row["delta"] = dx.get("delta")
                if dx.get("gamma") is not None: row["gamma"] = dx.get("gamma")
                if dx.get("iv") is not None: row["implied-volatility"] = dx.get("iv")
                if dx.get("theta") is not None: row["theta"] = dx.get("theta")
                if dx.get("vega") is not None: row["vega"] = dx.get("vega")

        calls: List[Dict[str, Any]] = []
        puts: List[Dict[str, Any]] = []

        expiry_date      = target_exp.get("expiration-date", today)
        expiration_type  = target_exp.get("expiration-type", "")

        # تصنيف AM / PM settlement (SPX فقط)
        _settle = (classify_spx_expiry(expiry_date, expiration_type)
                   if symbol.upper() == "SPX"
                   else {"settlement_type": "PM", "root_symbol": symbol,
                         "last_trade_time": "15:55 ET"})

        # حساب ساعات انتهاء الصلاحية:
        #   AM-settled: يتسوى عند 09:30 ET (13:30 UTC) ← نستخدم 14:00 UTC للسلامة
        #   PM-settled: يتسوى عند 16:00 ET (20:00 UTC)
        try:
            if _settle["settlement_type"] == "AM":
                exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(hour=14, minute=0)
            else:
                exp_dt = datetime.strptime(expiry_date, "%Y-%m-%d").replace(hour=21, minute=0)
            hours_to_expiry = max((exp_dt - datetime.now()).total_seconds() / 3600, 0.25)
        except Exception:
            hours_to_expiry = 6.5

        # جلب OI من cache إذا كان DXLink لم يعطِ OI
        cached_oi, cache_age = get_oi_cache(expiry_date)
        oi_cache_map: Dict[str, int] = {}
        if cached_oi:
            for row in cached_oi:
                key = f"{row['strike']}_{row['option_type']}"
                oi_cache_map[key] = row.get("oi", 0)
            _debug_set("oi_cache_age_minutes", cache_age)

        for item in all_data:
            sym = item.get("symbol", "")
            strike = strike_map.get(sym)
            if strike is None:
                continue

            bid = _first_number(item, ["bid", "bid-price"])
            ask = _first_number(item, ["ask", "ask-price"])
            last = _first_number(item, ["last", "last-price", "mark"])
            mid = _mid_from_bid_ask(bid, ask, last)
            delta = _first_number(item, ["delta", "greeks-delta"])
            gamma = _first_number(item, ["gamma", "greeks-gamma"])
            iv = _first_number(item, ["implied-volatility", "iv", "volatility"])
            vanna_raw = _first_number(item, ["vanna", "greeks-vanna"])
            charm_raw = _first_number(item, ["charm", "greeks-charm"])

            opt_type = "call" if sym in call_symbols else "put"
            oi = _to_int(item.get("open-interest"), 0)
            volume = _to_int(item.get("volume"), 0)

            # إذا DXLink لم يعطِ OI، استخدم الـ cache
            if oi <= 0:
                cached_val = oi_cache_map.get(f"{strike}_{opt_type}", 0)
                if cached_val > 0:
                    oi = cached_val

            exposure_contracts = oi if oi > 0 else max(volume, 0)
            exposure_is_proxy = oi <= 0 and exposure_contracts > 0

            # Gamma-only proxy: إذا لم يتوفر OI أو Volume
            # نستخدم gamma كـ unit-weight لحساب GEX النسبي بين الـ strikes
            # القيمة المطلقة غير موثوقة لكن الإشارة (+/-) والترتيب النسبي صحيحان
            gamma_abs = abs(gamma or 0.0)
            if exposure_contracts > 0:
                gex = gamma_abs * exposure_contracts * CONTRACT_MULTIPLIER * strike
                signed_gex = (gamma or 0.0) * exposure_contracts * CONTRACT_MULTIPLIER * strike * (1 if opt_type == "call" else -1)
                dex = (delta or 0.0) * exposure_contracts * CONTRACT_MULTIPLIER * strike
                gex_is_gamma_proxy = False
            elif gamma_abs > 0:
                # proxy نسبي: نعيّر على 100 عقد افتراضي لإظهار الاتجاه فقط
                proxy_contracts = 100
                gex = gamma_abs * proxy_contracts * CONTRACT_MULTIPLIER * strike
                signed_gex = (gamma or 0.0) * proxy_contracts * CONTRACT_MULTIPLIER * strike * (1 if opt_type == "call" else -1)
                dex = (delta or 0.0) * proxy_contracts * CONTRACT_MULTIPLIER * strike
                exposure_contracts = proxy_contracts
                exposure_is_proxy = True
                gex_is_gamma_proxy = True
            else:
                gex = signed_gex = dex = 0.0
                gex_is_gamma_proxy = False

            # Vanna/Charm بـ Black-Scholes مع time-to-expiry الحقيقي
            vanna_est, charm_est, greeks_estimated = _estimate_vanna_charm(
                opt_type, delta, gamma, iv, strike, strike, hours_to_expiry
            )
            vanna = vanna_raw if vanna_raw is not None else vanna_est
            charm = charm_raw if charm_raw is not None else charm_est
            eff_contracts = exposure_contracts if exposure_contracts > 0 else 0
            vanna_exposure = (vanna or 0.0) * eff_contracts * CONTRACT_MULTIPLIER * strike
            charm_exposure = (charm or 0.0) * eff_contracts * CONTRACT_MULTIPLIER * strike

            entry = {
                "symbol": sym,
                "streamer_symbol": streamer_map.get(sym),
                "type": opt_type,
                "strike": strike,
                "oi": oi,
                "volume": volume,
                "exposure_contracts": exposure_contracts,
                "exposure_is_proxy": exposure_is_proxy,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "delta": delta,
                "gamma": gamma,
                "iv": iv,
                "gex": gex,
                "signed_gex": signed_gex,
                "dex": dex,
                "vanna": vanna,
                "charm": charm,
                "vanna_exposure": vanna_exposure,
                "charm_exposure": charm_exposure,
                "greeks_estimated": bool(greeks_estimated and (vanna_raw is None or charm_raw is None)),
                "gex_is_gamma_proxy": gex_is_gamma_proxy,
            }
            if opt_type == "call":
                calls.append(entry)
            else:
                puts.append(entry)

        # احفظ OI في cache لاستخدامه في التحليلات القادمة إذا كان DXLink بطيئاً
        all_opts_for_cache = calls + puts
        if any((o.get("oi") or 0) > 0 for o in all_opts_for_cache):
            try:
                save_oi_cache(all_opts_for_cache, expiry_date)
            except Exception:
                pass

        all_strikes = sorted(set(strike_map.values()))
        mid_strike = all_strikes[len(all_strikes) // 2] if all_strikes else 0

        _debug_set("chain", "ok")
        _debug_set("chain_calls", len(calls))
        _debug_set("chain_puts", len(puts))
        _debug_set("greeks", {
            "delta": any(o.get("delta") is not None for o in calls + puts),
            "gamma": any(o.get("gamma") is not None for o in calls + puts),
            "iv": any(o.get("iv") is not None for o in calls + puts),
            "prices": any(o.get("mid") is not None for o in calls + puts),
            "dxlink_rows": len(dx_data) if 'dx_data' in locals() else 0,
            "streamer_symbols": len(streamer_map),
            "oi_available": any((o.get("oi") or 0) > 0 for o in calls + puts),
            "exposure_proxy": any(o.get("exposure_is_proxy") for o in calls + puts),
            "oi_from_cache": bool(oi_cache_map),
            "oi_cache_age_min": cache_age,
        })

        return {
            "calls":            sorted(calls, key=lambda x: x["strike"]),
            "puts":             sorted(puts,  key=lambda x: x["strike"]),
            "expiry":           expiry_date,
            "mid_strike":       mid_strike,
            "raw_expiration":   target_exp,
            "chain_root":       selected_root,
            "hours_to_expiry":  hours_to_expiry,
            "settlement_type":  _settle["settlement_type"],
            "root_symbol":      _settle["root_symbol"],
            "last_trade_time":  _settle["last_trade_time"],
        }
    except (ValueError, KeyError):
        raise
    except Exception as exc:
        _debug_set("chain", "failed_exception")
        _debug_error(f"options chain exception: {exc}")
        print(f"[options chain error] {exc}")
        return None

def get_swing_options_chain(
    token: Optional[str] = None,
    symbol: str = "SPX",
    min_dte: int = 12,
    max_dte: int = 17,
    target_expiry_date: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    جلب chain خاص بـ Swing حسب نطاق DTE المُمرّر من analyze_swing.
    يعيد None إذا لا يوجد expiry في النطاق أو فشل الجلب.
    الأسعار والـ Greeks فيه حقيقية لذلك الـ expiry، وليست من chain اليوم.
    """
    return get_options_chain(
        token              = token,
        symbol             = symbol,
        target_dte_range   = None if target_expiry_date else (min_dte, max_dte),
        target_expiry_date = target_expiry_date,
    )


def _infer_spot_from_chain(chain: Optional[Dict[str, Any]]) -> Optional[float]:
    if not chain:
        return None
    calls = chain.get("calls", [])
    puts = chain.get("puts", [])
    by_call = {c["strike"]: c for c in calls}
    by_put = {p["strike"]: p for p in puts}
    common = sorted(set(by_call) & set(by_put))
    best: Optional[Tuple[float, float]] = None
    for strike in common:
        c_mid = by_call[strike].get("mid")
        p_mid = by_put[strike].get("mid")
        if c_mid is None or p_mid is None:
            continue
        # عند ATM يكون فرق call-put قريباً من صفر نسبياً في 0DTE.
        score = abs(c_mid - p_mid)
        if best is None or score < best[0]:
            best = (score, strike)
    if best:
        return best[1]
    return chain.get("mid_strike")


def _em_from_iv(price: float, iv_pct: float, hours_remaining: float) -> float:
    """
    حساب Expected Move من IVx مباشرة.
    IVx = نسبة مئوية سنوية (مثل 25.9%)
    Formula: EM = Price × (IVx/100) × √(hours / 8736)
    8736 = 365 × 24  ← الـ IV السنوي مُبنى على الوقت التقويمي الكامل

    مثال: SPX=7453, IVx=25.9%, 2 ساعات متبقية
      EM = 7453 × 0.259 × √(2/8736) = 29.2 ✓ (قريب من straddle method)
    """
    if iv_pct <= 0 or hours_remaining <= 0 or price <= 0:
        return 0.0
    t = hours_remaining / 8736.0   # 365 × 24 ساعة
    return round(price * (iv_pct / 100) * (t ** 0.5), 1)


def calculate_expected_move(chain_data: Optional[Dict[str, Any]], current_price: float) -> Dict[str, Any]:
    """
    Expected Move من ATM Straddle (مطابق لحساب Tastytrade):
      EM = (ATM_Call_mid + ATM_Put_mid) × 0.90

    إذا لم يتوفر الـ straddle → يستخدم ATM IVx من DXLink Greeks:
      EM = Price × IVx × √(hours_remaining / 1638)
    """
    try:
        now_et_hour   = get_et_time().hour
        now_et_minute = get_et_time().minute
        hours_remaining = max(16 - now_et_hour - now_et_minute / 60, 0.25)
    except Exception:
        hours_remaining = 3.0

    fallback = max(round(current_price * 0.0045, 0), 5)
    if not chain_data:
        return {"move": fallback, "upper": round(current_price + fallback, 0),
                "lower": round(current_price - fallback, 0), "source": "fallback"}

    calls  = chain_data.get("calls", [])
    puts   = chain_data.get("puts", [])
    by_put = {p["strike"]: p for p in puts}

    # ── محاولة 1: Straddle mid × 0.90 (نفس Tastytrade) ──────────────────────
    best = None
    for c in calls:
        strike = c["strike"]
        p = by_put.get(strike)
        if not p:
            continue
        c_mid = c.get("mid")
        p_mid = p.get("mid")
        if c_mid is None or p_mid is None:
            continue
        straddle = c_mid + p_mid
        if straddle < current_price * 0.001:
            continue
        distance = abs(strike - current_price)
        if best is None or distance < best[0]:
            best = (distance, strike, straddle)

    if best:
        atm_strike   = best[1]
        straddle_mid = best[2]
        move = max(round(straddle_mid * 0.90, 1), 1)
        return {
            "move":         move,
            "upper":        round(current_price + move, 0),
            "lower":        round(current_price - move, 0),
            "source":       f"straddle×0.90 (ATM={atm_strike:.0f})",
            "atm_strike":   atm_strike,
            "straddle_mid": round(straddle_mid, 2),
        }

    # ── محاولة 2: ATM IVx من DXLink Greeks ───────────────────────────────────
    # IVx متاح حتى عندما bid/ask ناقص
    atm_candidates = sorted(calls, key=lambda c: abs(c["strike"] - current_price))
    for c in atm_candidates[:5]:
        atm_iv = c.get("iv")
        if not atm_iv:
            continue
        iv_pct = atm_iv * 100 if atm_iv < 2 else atm_iv   # normalize
        if not (5 < iv_pct < 200):
            continue
        move = _em_from_iv(current_price, iv_pct, hours_remaining)
        if move > 0:
            return {
                "move":       move,
                "upper":      round(current_price + move, 0),
                "lower":      round(current_price - move, 0),
                "source":     f"IVx={iv_pct:.1f}% (DXLink)",
                "atm_strike": c["strike"],
            }

    # ── Fallback: 0.45% من السعر ─────────────────────────────────────────────
    return {"move": fallback, "upper": round(current_price + fallback, 0),
            "lower": round(current_price - fallback, 0), "source": "fallback_0.45%"}

    # (هذا السطر لن يُنفَّذ — الـ fallback يُرجع في الفرع أعلاه)
    move = max(round(fallback, 1), 1)
    return {"move": move, "upper": round(current_price + move, 0),
            "lower": round(current_price - move, 0),
            "source": "atm_straddle", "atm_strike": atm_strike,
            "straddle_mid": round(straddle_mid, 2)}



def enrich_exposures_with_spot(chain_data: Optional[Dict[str, Any]], current_price: float) -> None:
    """تحديث Vanna/Charm بـ Black-Scholes بعد معرفة سعر SPX الفعلي."""
    if not chain_data:
        return
    hours_to_expiry = chain_data.get("hours_to_expiry", 6.5)
    for opt in chain_data.get("calls", []) + chain_data.get("puts", []):
        oi = opt.get("exposure_contracts", opt.get("oi", 0)) or 0
        strike = opt.get("strike", current_price) or current_price
        opt_type = opt.get("type", "call")
        delta, gamma, iv = opt.get("delta"), opt.get("gamma"), opt.get("iv")
        vanna_est, charm_est, estimated = _estimate_vanna_charm(
            opt_type, delta, gamma, iv, strike, current_price, hours_to_expiry
        )
        if opt.get("vanna") is None or opt.get("greeks_estimated"):
            opt["vanna"] = vanna_est
        if opt.get("charm") is None or opt.get("greeks_estimated"):
            opt["charm"] = charm_est
        opt["vanna_exposure"] = (opt.get("vanna") or 0.0) * oi * CONTRACT_MULTIPLIER * current_price
        opt["charm_exposure"] = (opt.get("charm") or 0.0) * oi * CONTRACT_MULTIPLIER * current_price
        opt["dex"] = (opt.get("delta") or 0.0) * oi * CONTRACT_MULTIPLIER * current_price
        opt["signed_gex"] = (opt.get("gamma") or 0.0) * oi * CONTRACT_MULTIPLIER * current_price * (1 if opt_type == "call" else -1)
        opt["gex"] = abs(opt.get("gamma") or 0.0) * oi * CONTRACT_MULTIPLIER * current_price
        opt["greeks_estimated"] = bool(opt.get("greeks_estimated") or estimated)

# ── Levels & Pin Score ────────────────────────────────────────────────────────

def calculate_levels(chain_data: Optional[Dict[str, Any]], current_price: float, symbol: str = "SPX") -> Dict[str, Any]:
    symbol_up = str(symbol or "SPX").upper()
    if not chain_data:
        return _dummy_levels(current_price)

    calls = chain_data["calls"]
    puts = chain_data["puts"]
    margin = max(current_price * 0.03, 100)

    calls_near = [c for c in calls if current_price - margin <= c["strike"] <= current_price + margin * 2]
    puts_near = [p for p in puts if current_price - margin * 2 <= p["strike"] <= current_price + margin]

    levels: Dict[str, Any] = {}

    def max_by(lst: List[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
        return max(lst, key=lambda x: x.get(key) or 0, default=None) if lst else None

    cw_oi = max_by(calls_near, "oi")
    cw_vol = max_by(calls_near, "volume")
    cw_gex = max_by(calls_near, "gex")
    pw_oi = max_by(puts_near, "oi")
    pw_vol = max_by(puts_near, "volume")
    pw_gex = max_by(puts_near, "gex")

    if cw_oi: levels["call_wall_oi"] = cw_oi["strike"]
    if cw_vol: levels["call_wall_vol"] = cw_vol["strike"]
    if cw_gex and cw_gex.get("gex", 0) > 0: levels["call_wall_gex"] = cw_gex["strike"]
    if pw_oi: levels["put_wall_oi"] = pw_oi["strike"]
    if pw_vol: levels["put_wall_vol"] = pw_vol["strike"]
    if pw_gex and pw_gex.get("gex", 0) > 0: levels["put_wall_gex"] = pw_gex["strike"]

    em = calculate_expected_move(chain_data, current_price)
    levels["em_upper"]              = em["upper"]
    levels["em_lower"]              = em["lower"]
    levels["expected_move"]         = em["move"]
    levels["expected_move_source"]  = em["source"]
    levels["straddle_mid"]          = em.get("straddle_mid")
    levels["em_atm_strike"]         = em.get("atm_strike")

    # Magnetic score: OI + Volume + approximate GEX, with a distance penalty.
    max_oi = max([x.get("oi", 0) for x in calls_near + puts_near] or [1]) or 1
    max_vol = max([x.get("volume", 0) for x in calls_near + puts_near] or [1]) or 1
    max_gex = max([x.get("gex", 0) for x in calls_near + puts_near] or [1]) or 1
    combined: Dict[float, float] = {}
    for item in calls_near + puts_near:
        distance = abs(item["strike"] - current_price)
        distance_weight = max(0.15, 1 - distance / (margin * 2.5))
        score = (
            0.50 * (item.get("oi", 0) / max_oi) +
            0.25 * (item.get("volume", 0) / max_vol) +
            0.25 * ((item.get("gex", 0) or 0) / max_gex)
        ) * distance_weight
        combined[item["strike"]] = combined.get(item["strike"], 0) + score

    levels["magnetic"] = round(max(combined, key=combined.get) / 5) * 5 if combined else round(current_price / 50) * 50
    # ── GEX Breakdown ────────────────────────────────────────────────────────
    call_signed_gex = sum(x.get("signed_gex", 0) for x in calls_near)
    put_signed_gex  = sum(x.get("signed_gex", 0) for x in puts_near)
    net_gex_raw     = call_signed_gex + put_signed_gex
    gross_gex       = sum(x.get("gex", 0) for x in calls_near + puts_near)

    levels["max_call_gex"]    = round(sum(x.get("gex", 0) for x in calls_near), 0)
    levels["max_put_gex"]     = round(sum(x.get("gex", 0) for x in puts_near), 0)
    levels["call_signed_gex"] = round(call_signed_gex, 0)
    levels["put_signed_gex"]  = round(put_signed_gex, 0)
    levels["gross_gex"]       = round(gross_gex, 0)

    # GEX per Strike — لإثبات صحة الحساب وعرض التوزيع
    gex_by_strike: Dict[float, float] = {}
    for item in calls_near + puts_near:
        s = item.get("strike")
        if s is not None:
            gex_by_strike[s] = gex_by_strike.get(s, 0.0) + item.get("signed_gex", 0)
    # أعلى 10 strikes بمطلق signed_gex
    levels["gex_by_strike"] = sorted(
        [{"strike": k, "net_signed_gex": round(v, 0)} for k, v in gex_by_strike.items()],
        key=lambda x: abs(x["net_signed_gex"]), reverse=True
    )[:10]

    # gamma_source: هل جاءت Gamma من DXLink الحقيقي أم proxy؟
    real_gamma_count  = sum(1 for x in calls_near+puts_near if not x.get("gex_is_gamma_proxy") and x.get("gamma") is not None)
    proxy_gamma_count = sum(1 for x in calls_near+puts_near if x.get("gex_is_gamma_proxy"))
    levels["gamma_source"] = {
        "real_dxlink": real_gamma_count,
        "proxy_100":   proxy_gamma_count,
        "note": "scale ~500x smaller than professional tools (proxy OI=100 vs real OI~50K)"
    }
    levels["gex_balance_ratio"] = round(
        abs(call_signed_gex) / max(abs(put_signed_gex), 1), 3
    ) if put_signed_gex else None

    # هل GEX قريب من الصفر بسبب التوازن أم لأن البيانات فعلاً صفر؟
    levels["gex_near_zero_reason"] = (
        "balanced"     if gross_gex > 1000 and abs(net_gex_raw) < gross_gex * 0.05
        else "no_data" if gross_gex < 100
        else "directional"
    )

    levels["net_gex"]   = round(net_gex_raw, 0)
    levels["net_dex"]   = round(sum(x.get("dex", 0) for x in calls_near + puts_near), 0)
    levels["net_vanna"] = round(sum(x.get("vanna_exposure", 0) for x in calls_near + puts_near), 0)
    levels["net_charm"] = round(sum(x.get("charm_exposure", 0) for x in calls_near + puts_near), 0)

    # تشخيص: عدد الخيارات التي لديها gamma حقيقي
    has_any_gex = any(x.get("gex", 0) > 0 for x in calls_near + puts_near)
    _debug_set("gex_diagnostic", {
        "call_signed_gex": round(call_signed_gex),
        "put_signed_gex":  round(put_signed_gex),
        "net_gex":         round(net_gex_raw),
        "gross_gex":       round(gross_gex),
        "balance_ratio":   levels["gex_balance_ratio"],
        "reason":          levels["gex_near_zero_reason"],
        "calls_near_count": len(calls_near),
        "puts_near_count":  len(puts_near),
        "has_any_gex":     has_any_gex,
    })

    # Zero Gamma تقريبي: مستوى strike الذي يقترب عنده cumulative signed GEX من الصفر.
    cumulative = 0.0
    zero_gamma = None
    best_abs = None
    for strike in sorted({i["strike"] for i in calls_near + puts_near}):
        cumulative += sum(i.get("signed_gex", 0) for i in calls_near + puts_near if i["strike"] == strike)
        abs_cum = abs(cumulative)
        if best_abs is None or abs_cum < best_abs:
            best_abs = abs_cum
            zero_gamma = strike
    if zero_gamma is not None:
        levels["zero_gamma"] = zero_gamma

    # VIX / EMA / IV Rank context for Strategy Engine
    try:
        ctx = get_market_context(chain_data, current_price, symbol=symbol_up)
        levels.update(ctx)
        # تأكد أن combined_trend وصل — بدونه Swing لن يُختار أبداً
        if not levels.get("combined_trend") or levels.get("combined_trend") == "neutral":
            if ctx.get("combined_trend") and ctx["combined_trend"] != "neutral":
                levels["combined_trend"] = ctx["combined_trend"]
                levels["trend"]          = ctx["combined_trend"]
    except Exception as exc:
        _debug_error(f"market context: {exc}")
        # حتى عند الخطأ: حاول جلب EMA مباشرة لتحديد الـ trend
        try:
            closes = _fetch_yahoo_closes("^GSPC", "6mo", "1d")
            ema20  = _ema(closes, 20)
            ema50  = _ema(closes, 50)
            if ema20 and ema50:
                fallback_trend = _trend_from_ema(current_price, ema20, ema50)
                levels["combined_trend"] = fallback_trend
                levels["trend"]          = fallback_trend
                levels["ema20"]          = ema20
                levels["ema50"]          = ema50
        except Exception:
            pass
        levels.setdefault("iv_rank", None)
        levels.setdefault("trend", "neutral")
        levels.setdefault("combined_trend", "neutral")

    levels["top_call_gex"] = _top_strike_exposures(calls_near, "gex", 10)
    levels["top_put_gex"] = _top_strike_exposures(puts_near, "gex", 10)
    levels["top_call_oi"] = _top_strike_exposures(calls_near, "oi", 10)
    levels["top_put_oi"] = _top_strike_exposures(puts_near, "oi", 10)
    return levels


def calculate_pin_score(current_price: float, magnetic_level: float, chain_data: Optional[Dict[str, Any]], levels: Optional[Dict[str, Any]] = None) -> int:
    if not chain_data or not magnetic_level:
        distance = abs(current_price - magnetic_level) if magnetic_level else 999
        return round(max(0, 100 - distance * 0.5))

    try:
        all_items = chain_data["calls"] + chain_data["puts"]
        total_oi = sum(i.get("oi", 0) for i in all_items) or 1
        mag_items = [i for i in all_items if round(i["strike"], 4) == round(magnetic_level, 4)]
        mag_oi = sum(i.get("oi", 0) for i in mag_items)
        mag_gex = sum(i.get("gex", 0) for i in mag_items)
        total_gex = sum(i.get("gex", 0) for i in all_items) or 1
        distance = abs(current_price - magnetic_level)

        em_raw = (levels or {}).get("expected_move") or max(current_price * 0.0045, 5)
        # الحد الأدنى للـ EM في Pin Score = 0.4% من السعر
        # يمنع تقلب Pin Score الناتج عن EM صغير (straddle متأخر في اليوم)
        em_floor = max(current_price * 0.004, 20)
        em = max(em_raw, em_floor)

        distance_score = max(0, 40 * (1 - distance / max(em, 1)))
        oi_score       = min((mag_oi / total_oi) * 250, 25)
        gex_score      = min((mag_gex / total_gex) * 250, 20)

        near = [i for i in all_items if abs(i["strike"] - current_price) <= max(em, 20)]
        call_oi = sum(i.get("oi", 0) for i in near if i.get("type") == "call")
        put_oi  = sum(i.get("oi", 0) for i in near if i.get("type") == "put")
        balance = 1 - abs(call_oi - put_oi) / max(call_oi + put_oi, 1)
        balance_score  = max(0, min(balance * 15, 15))

        total = int(min(max(round(distance_score + oi_score + gex_score + balance_score), 0), 100))

        # احفظ التفصيل في الـ debug
        _debug_set("pin_breakdown", {
            "distance_to_magnet": round(distance, 1),
            "em_used":            round(em, 1),
            "distance_score":     round(distance_score, 1),
            "oi_concentration":   round(oi_score, 1),
            "gex_support":        round(gex_score, 1),
            "balance_score":      round(balance_score, 1),
            "total":              total,
            "magnetic":           magnetic_level,
        })
        return total
    except Exception as exc:
        print(f"[pin score warning] {exc}")
        return round(max(0, 60 - abs(current_price - magnetic_level) * 0.2))




def _top_strike_exposures(options: List[Dict[str, Any]], key: str, n: int = 10) -> List[Dict[str, Any]]:
    """Return top strikes by a numeric field, aggregated by strike."""
    agg: Dict[float, Dict[str, Any]] = {}
    for opt in options:
        strike = opt.get("strike")
        if strike is None:
            continue
        slot = agg.setdefault(float(strike), {"strike": float(strike), "value": 0.0, "oi": 0, "volume": 0})
        slot["value"] += float(opt.get(key) or 0.0)
        slot["oi"] += int(opt.get("oi") or 0)
        slot["volume"] += int(opt.get("volume") or 0)
    ranked = sorted(agg.values(), key=lambda x: abs(x["value"]), reverse=True)
    return ranked[:n]


def _normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _estimate_pop_from_delta(short_put_delta: Optional[float], short_call_delta: Optional[float], fallback: float = 0.70) -> float:
    """Approximate POP using short strike deltas when available."""
    if short_put_delta is None or short_call_delta is None:
        return round(fallback * 100, 1)
    prob_touch_or_finish_outside = min(abs(short_put_delta) + abs(short_call_delta), 0.95)
    return round(max(0.0, min(1.0, 1.0 - prob_touch_or_finish_outside)) * 100, 1)


def _decision_filter(pin_score: int, levels: Dict[str, Any], condor: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Combine pin/flow/condor diagnostics into one user-facing decision."""
    reasons: List[str] = []
    if condor is None:
        return {"decision": "❌ لا تدخل", "grade": "reject", "reasons": ["لا توجد صفقة Iron Condor صالحة من البيانات الحالية"]}

    score = condor.get("score", 0)
    credit = condor.get("credit") or 0
    pop = condor.get("pop") or 0
    net_gex = levels.get("net_gex") or 0
    zero_gamma = levels.get("zero_gamma")

    if pin_score < 50:
        reasons.append("Pin Score ضعيف")
    if score < 60:
        reasons.append("درجة الصفقة منخفضة")
    if credit <= 0:
        reasons.append("الكريدت غير صالح أو غير متاح")
    if pop and pop < 65:
        reasons.append("POP أقل من 65%")
    if net_gex < 0:
        reasons.append("Net GEX سلبي: بيئة أكثر قابلية للحركة")
    if zero_gamma is not None:
        spot = condor.get("spot")
        em = levels.get("expected_move") or 1
        if spot is not None and abs(float(spot) - float(zero_gamma)) < max(float(em) * 0.35, 10):
            reasons.append("السعر قريب من Zero Gamma")

    if reasons:
        if pin_score >= 50 and score >= 60 and credit > 0:
            return {"decision": "⚠️ مراقبة فقط", "grade": "watch", "reasons": reasons[:5]}
        return {"decision": "❌ لا تدخل", "grade": "reject", "reasons": reasons[:5]}
    return {"decision": "✅ مطابق مبدئياً للخطة", "grade": "accept", "reasons": ["Pin/IC/Flow ضمن الحدود المبدئية"]}


# ── Iron Condor Scanner ───────────────────────────────────────────────────────

def _nearest_by_delta(options: List[Dict[str, Any]], target_abs_delta: float, side: str, current_price: float, expected_move: float) -> Optional[Dict[str, Any]]:
    candidates = []
    for opt in options:
        strike = opt["strike"]
        if side == "call" and strike <= current_price:
            continue
        if side == "put" and strike >= current_price:
            continue
        delta = opt.get("delta")
        if delta is not None:
            score = abs(abs(delta) - target_abs_delta)
        else:
            # fallback: اختر خارج نطاق expected move تقريباً.
            ideal = current_price + expected_move if side == "call" else current_price - expected_move
            score = abs(strike - ideal) / max(expected_move, 1)
        liquidity_penalty = 0 if (opt.get("bid") is not None and opt.get("ask") is not None) else 0.5
        candidates.append((score + liquidity_penalty, opt))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])
    return candidates[0][1]


def _long_leg_for_short(options: List[Dict[str, Any]], short: Dict[str, Any], side: str, width: int = DEFAULT_WING_WIDTH) -> Optional[Dict[str, Any]]:
    target = short["strike"] + width if side == "call" else short["strike"] - width
    exact = [o for o in options if abs(o["strike"] - target) < 0.001]
    if exact:
        return exact[0]
    # إن لم يوجد strike بالضبط، اختر أقرب عقد في الاتجاه الصحيح.
    if side == "call":
        candidates = [o for o in options if o["strike"] > short["strike"]]
    else:
        candidates = [o for o in options if o["strike"] < short["strike"]]
    if not candidates:
        return None
    return min(candidates, key=lambda o: abs(abs(o["strike"] - short["strike"]) - width))


def find_iron_condor(chain_data: Optional[Dict[str, Any]], current_price: float, levels: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not chain_data:
        return None

    calls = chain_data.get("calls", [])
    puts = chain_data.get("puts", [])
    expected_move = levels.get("expected_move") or max(current_price * 0.0045, 5)
    target_delta = _to_float(get_setting("target_short_delta"), TARGET_SHORT_DELTA) or TARGET_SHORT_DELTA
    wing_width = int(_to_float(get_setting("wing_width"), DEFAULT_WING_WIDTH) or DEFAULT_WING_WIDTH)

    short_call = _nearest_by_delta(calls, target_delta, "call", current_price, expected_move)
    short_put = _nearest_by_delta(puts, target_delta, "put", current_price, expected_move)
    if not short_call or not short_put:
        return None

    long_call = _long_leg_for_short(calls, short_call, "call", wing_width)
    long_put = _long_leg_for_short(puts, short_put, "put", wing_width)
    if not long_call or not long_put:
        return None

    # Credit: sell shorts, buy longs. استخدم mid إن وجد، وإلا bid/ask كحل تحفظي.
    sc_mid = short_call.get("mid") or short_call.get("bid") or 0
    sp_mid = short_put.get("mid") or short_put.get("bid") or 0
    lc_mid = long_call.get("mid") or long_call.get("ask") or 0
    lp_mid = long_put.get("mid") or long_put.get("ask") or 0
    credit = round((sc_mid + sp_mid) - (lc_mid + lp_mid), 2)
    width_call = abs(long_call["strike"] - short_call["strike"])
    width_put = abs(short_put["strike"] - long_put["strike"])
    width = max(width_call, width_put)
    max_loss = round(width - credit, 2) if credit is not None else None

    breakeven_low = round(short_put["strike"] - max(credit, 0), 2)
    breakeven_high = round(short_call["strike"] + max(credit, 0), 2)
    safe_low = levels.get("em_lower")
    safe_high = levels.get("em_upper")

    warnings: List[str] = []
    if credit < MIN_CREDIT:
        warnings.append("الكريدت منخفض")
    if short_call["strike"] < (safe_high or current_price):
        warnings.append("Short Call داخل/قريب من Expected Move")
    if short_put["strike"] > (safe_low or current_price):
        warnings.append("Short Put داخل/قريب من Expected Move")
    if levels.get("call_wall_oi") and abs(short_call["strike"] - levels["call_wall_oi"]) <= wing_width:
        warnings.append("Short Call قريب من Call Wall")
    if levels.get("put_wall_oi") and abs(short_put["strike"] - levels["put_wall_oi"]) <= wing_width:
        warnings.append("Short Put قريب من Put Wall")

    score = 100
    score -= 35 if credit < MIN_CREDIT else 0
    score -= 15 * sum(1 for w in warnings if "Expected" in w)
    score -= 10 * sum(1 for w in warnings if "Wall" in w)
    score = max(0, min(score, 100))

    pop = _estimate_pop_from_delta(short_put.get("delta"), short_call.get("delta"), fallback=0.70)
    return_on_risk = round((credit / max_loss) * 100, 1) if max_loss and max_loss > 0 and credit > 0 else 0.0

    if score >= 75:
        decision = "صالح للدراسة — تحقق من السعر الحي والسبريد"
    elif score >= 50:
        decision = "متوسط — يحتاج فلترة يدوية"
    else:
        decision = "غير مناسب حالياً"

    return {
        "strategy": "Iron Condor",
        "short_call": short_call["strike"],
        "long_call": long_call["strike"],
        "short_put": short_put["strike"],
        "long_put": long_put["strike"],
        "credit": credit,
        "max_loss": max_loss,
        "width": width,
        "breakeven_low": breakeven_low,
        "breakeven_high": breakeven_high,
        "target_delta": target_delta,
        "pop": pop,
        "return_on_risk": return_on_risk,
        "spot": current_price,
        "short_call_delta": short_call.get("delta"),
        "short_put_delta": short_put.get("delta"),
        "score": score,
        "decision": decision,
        "warnings": warnings,
    }


# ── Full Analysis ─────────────────────────────────────────────────────────────

def _liquidity_status_from_strat(strat: Dict) -> str:
    """
    حالة سيولة مبسطة للعرض.
    التسجيل الورقي نفسه يستخدم gate صارماً في trade_monitor:
      0DTE spread≤20%، Swing spread≤25%، و bid/ask كامل لكل الأرجل.
    """
    if not strat:
        return "N/A"
    legs = strat.get("legs_detail", [])
    if not legs:
        return "N/A"
    mode = str(strat.get("trade_mode") or strat.get("selected_mode") or "0DTE").lower()
    max_spread = 25.0 if mode == "swing" else 20.0
    status = "PASS"
    for leg in legs:
        if not leg:
            continue
        bid = leg.get("bid")
        ask = leg.get("ask")
        try:
            bid_f = float(bid); ask_f = float(ask)
        except Exception:
            return "FAIL"
        if bid_f <= 0 or ask_f <= 0 or ask_f <= bid_f:
            return "FAIL"
        sp = leg.get("spread_pct")
        if sp is None:
            mid = (bid_f + ask_f) / 2.0
            sp = ((ask_f - bid_f) / mid * 100.0) if mid > 0 else 999.0
        try:
            sp = float(sp)
        except Exception:
            sp = 999.0
        if sp > max_spread:
            return "FAIL"
        if sp > max_spread * 0.75:
            status = "WARN"
    return status


def _pick_best_opportunity(*analyses: Dict[str, Any]) -> Dict[str, Any]:
    """
    يقارن SPX / SPY / QQQ ويختار أفضل فرصة.
    قواعد:
    - Liquidity FAIL → يُستبعد من المنافسة الحقيقية (no_trade=True قسراً)
    - Liquidity WARN → خصم 5 نقاط
    - يفوز فقط الرمز الذي تجاوز العتبة وسيولته مقبولة
    """
    candidates = []
    for a in analyses:
        if not a or "error" in a:
            continue
        sym      = a.get("symbol", "?")
        strat    = a.get("strategy") or {}
        no_trade = strat.get("no_trade", True) if strat else True
        name     = strat.get("strategy", "No Trade") if strat else "No Trade"

        # فحص السيولة
        liq = _liquidity_status_from_strat(strat) if strat else "N/A"
        liq_fail = (liq == "FAIL")

        # Liquidity FAIL → لا تدخل في المنافسة الحقيقية
        if liq_fail and not no_trade:
            no_trade  = True
            name      = name  # تبقى الاستراتيجية للعرض لكن no_trade=True
            liq_note  = " [FAIL Liquidity]"
        else:
            liq_note  = ""

        if no_trade:
            all_scores = strat.get("all_scores", {}) if strat else {}
            raw_score  = max(all_scores.values(), default=0) if all_scores else 0
            best_name  = max(all_scores, key=all_scores.get, default="No Trade") if all_scores else "No Trade"
            display_score = raw_score
            display_name  = f"No Trade ({best_name} {raw_score}){liq_note}" if raw_score > 0 else f"No Trade{liq_note}"
        else:
            raw_score     = strat.get("score", 0) if strat else 0
            # Liquidity WARN → خصم 5 نقاط من الترتيب فقط
            display_score = max(raw_score - (5 if liq == "WARN" else 0), 0)
            display_name  = name
            best_name     = name

        candidates.append({
            "symbol":      sym,
            "strategy":    display_name,
            "best_strat":  best_name,
            "score":       display_score,
            "raw_score":   raw_score,
            "no_trade":    no_trade,
            "liquidity":   liq,
            "emoji":       strat.get("emoji", "🚫") if strat else "🚫",
            "decision":    strat.get("decision", "") if strat else "",
            "price":       a.get("price", 0),
        })

    candidates.sort(key=lambda x: x["score"], reverse=True)

    # الفائز: تجاوز العتبة + سيولة مقبولة
    tradeable = [c for c in candidates if not c["no_trade"]]
    best_candidate = candidates[0] if candidates else None
    real_winner = tradeable[0] if tradeable else None

    return {
        "winner":          real_winner,
        "best_candidate":  best_candidate,
        "all_candidates":  candidates,
        "has_opportunity": bool(tradeable),
    }


def _fetch_price_for_symbol(tok: str, symbol: str) -> Optional[float]:
    """جلب سعر أي رمز: DXLink أولاً ثم Yahoo."""
    symbol_up = symbol.upper()
    dx_syms = _SYMBOL_DX.get(symbol_up, [symbol_up])
    min_price = _SYMBOL_MIN_PRICE.get(symbol_up, 1)
    max_price = _SYMBOL_MAX_PRICE.get(symbol_up, 1_000_000)

    # محاولة DXLink
    try:
        from core.dxlink_client import fetch_market_data_snapshot
        snap = fetch_market_data_snapshot(tok, dx_syms, timeout_seconds=6.0, max_symbols=5)
        for sym in dx_syms:
            entry = snap.get(sym, {})
            bid, ask = entry.get("bid"), entry.get("ask")
            last, prev = entry.get("last"), entry.get("prev-close")
            if bid and ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2
                if min_price < mid <= max_price:
                    _debug_set("price_source", f"dxlink:bid_ask:{sym}")
                    return round(mid, 2)
            if last and min_price < float(last) <= max_price:
                _debug_set("price_source", f"dxlink:last:{sym}")
                return last
            if prev and min_price < float(prev) <= max_price:
                _debug_set("price_source", f"dxlink:prev_close:{sym}")
                return prev
    except Exception as exc:
        _debug_error(f"dxlink {symbol} price: {exc}")

    # محاولة Yahoo
    yahoo_sym = _SYMBOL_YAHOO.get(symbol_up, symbol_up)
    closes = _fetch_yahoo_closes(yahoo_sym, "5d", "1d")
    if closes:
        price = _last_value(closes)
        if price and price > min_price:
            _debug_set("price_source", f"yahoo:{yahoo_sym}")
            return price

    return None


def _check_event_risk(days_ahead: int = 30) -> Dict[str, Any]:
    """
    فحص الأحداث الكبرى القادمة (FOMC, CPI, NFP).
    يعيد {"has_event": bool, "events": [...], "nearest_days": int}
    """
    # ── الأحداث الاقتصادية الكبرى 2026 ──────────────────────────────────────
    # FOMC: تواريخ رسمية من الفيدرالي
    # CPI:  تقريبية (BLS تُعلن مسبقاً) — تحقق شهرياً
    # NFP:  أول جمعة كل شهر (محسوبة)
    known_events = [
        # ── FOMC 2026 (اجتماعات الفيدرالي) ──────────────────────────────
        {"name": "FOMC", "date": "2026-01-28"},
        {"name": "FOMC", "date": "2026-03-18"},
        {"name": "FOMC", "date": "2026-05-06"},
        {"name": "FOMC", "date": "2026-06-17"},
        {"name": "FOMC", "date": "2026-07-29"},
        {"name": "FOMC", "date": "2026-09-16"},
        {"name": "FOMC", "date": "2026-11-04"},
        {"name": "FOMC", "date": "2026-12-16"},
        # ── CPI 2026 (تقريبية) ───────────────────────────────────────────
        {"name": "CPI",  "date": "2026-01-14"},
        {"name": "CPI",  "date": "2026-02-11"},
        {"name": "CPI",  "date": "2026-03-11"},
        {"name": "CPI",  "date": "2026-04-15"},
        {"name": "CPI",  "date": "2026-05-13"},
        {"name": "CPI",  "date": "2026-06-10"},
        {"name": "CPI",  "date": "2026-07-14"},
        {"name": "CPI",  "date": "2026-08-12"},
        {"name": "CPI",  "date": "2026-09-10"},
        {"name": "CPI",  "date": "2026-10-14"},
        {"name": "CPI",  "date": "2026-11-12"},
        {"name": "CPI",  "date": "2026-12-10"},
        # ── NFP 2026 (أول جمعة كل شهر) ──────────────────────────────────
        {"name": "NFP",  "date": "2026-01-02"},
        {"name": "NFP",  "date": "2026-02-06"},
        {"name": "NFP",  "date": "2026-03-06"},
        {"name": "NFP",  "date": "2026-04-03"},
        {"name": "NFP",  "date": "2026-05-01"},
        {"name": "NFP",  "date": "2026-06-05"},
        {"name": "NFP",  "date": "2026-07-03"},
        {"name": "NFP",  "date": "2026-08-07"},
        {"name": "NFP",  "date": "2026-09-04"},
        {"name": "NFP",  "date": "2026-10-02"},
        {"name": "NFP",  "date": "2026-11-06"},
        {"name": "NFP",  "date": "2026-12-04"},
    ]
    today = date.today()
    upcoming = []
    for ev in known_events:
        try:
            ev_date = datetime.strptime(ev["date"], "%Y-%m-%d").date()
            diff = (ev_date - today).days
            if 0 <= diff <= days_ahead:
                upcoming.append({**ev, "days_ahead": diff})
        except Exception:
            pass
    upcoming.sort(key=lambda x: x["days_ahead"])
    return {
        "has_event":    bool(upcoming),
        "events":       upcoming,
        "nearest_days": upcoming[0]["days_ahead"] if upcoming else 999,
    }



def _apply_rc15f_swing_diag_to_strategy(strat: Dict[str, Any], smc: Dict[str, Any]) -> Dict[str, Any]:
    """
    RC15f UI/diagnostic enforcement for Swing candidates produced by analyze_swing().

    This mirrors the registration-time hard guard: a Swing candidate is only
    registration-eligible when ICT/SMC score >= 4/5 AND EMA alignment passes.
    It is intentionally conservative: unavailable ICT/SMC data blocks Swing.
    """
    if not isinstance(strat, dict):
        return strat
    _smc = smc if isinstance(smc, dict) else {}
    strategy_name = str(strat.get("strategy") or "")
    directional_swing_debit = strategy_name in ("Call Debit Spread", "Put Debit Spread")
    if not directional_swing_debit:
        diag = {
            "applied": False,
            "not_applicable": True,
            "scope": "Swing directional debit only",
            "decision": "NOT_APPLICABLE_DIRECTIONAL_DEBIT_ONLY",
            "allowed": True,
            "strategy": strategy_name,
            "reason": "RC15f applies only to Swing Call Debit Spread / Put Debit Spread",
        }
        strat["rc15f_swing_confirmation"] = diag
        strat["rc15f_not_applicable"] = True
        return strat

    ict_details = _smc.get("ict_smc_details") if isinstance(_smc.get("ict_smc_details"), dict) else {}
    ema_details = _smc.get("ema_details") if isinstance(_smc.get("ema_details"), dict) else {}
    try:
        ict_score = int(_smc.get("ict_smc_score") if _smc.get("ict_smc_score") is not None else ict_details.get("score", 0))
    except Exception:
        ict_score = 0
    ict_pass = bool(_smc.get("ict_smc_pass")) and ict_score >= 4
    ema_pass = bool(_smc.get("ema_alignment_pass"))

    insufficient = (
        not bool(_smc.get("available"))
        or str(_smc.get("reason") or "").startswith("smc_mtf_diagnostics_error")
        or str(ict_details.get("reason") or "") in ("insufficient_ict_smc_data", "diagnostics_error")
        or str(ema_details.get("reason") or "") in ("insufficient_ict_smc_data", "diagnostics_error")
    )

    if insufficient:
        confidence = "BLOCKED"
        decision = "BLOCKED"
        block_reason = "insufficient_ict_smc_data"
        watch_reason = None
        allowed = False
    elif ict_score >= 5 and ema_pass:
        confidence = "HIGH"
        decision = "ALLOWED_HIGH_CONFIDENCE"
        block_reason = None
        watch_reason = None
        allowed = True
    elif ict_score >= 4 and ema_pass:
        confidence = "MODERATE"
        decision = "ALLOWED_MODERATE_CONFIDENCE"
        block_reason = None
        watch_reason = None
        allowed = True
    elif ict_score == 3 and ema_pass:
        confidence = "WATCHLIST"
        decision = "WATCHLIST_ONLY"
        block_reason = None
        watch_reason = "ict_smc_score_3_of_5_watchlist_only"
        allowed = False
    elif not ema_pass:
        confidence = "BLOCKED" if ict_score < 3 else ("WATCHLIST" if ict_score == 3 else "BLOCKED")
        decision = "BLOCKED_EMA_ALIGNMENT"
        block_reason = "ema_alignment_failed"
        watch_reason = None
        allowed = False
    else:
        confidence = "BLOCKED"
        decision = "BLOCKED_ICT_SMC_SCORE"
        block_reason = f"ict_smc_score_below_4 ({ict_score}/5)"
        watch_reason = None
        allowed = False

    diag = {
        "applied": True,
        "scope": "Swing only",
        "decision": decision,
        "allowed": bool(allowed),
        "ict_smc_score": ict_score,
        "ict_smc_pass": bool(ict_pass),
        "ict_smc_confidence": confidence,
        "ema_alignment_pass": bool(ema_pass),
        "swing_block_reason": block_reason,
        "swing_watchlist_reason": watch_reason,
        "ict_smc_details": ict_details,
        "ema_details": ema_details,
    }

    strat["ict_smc_score"] = ict_score
    strat["ict_smc_pass"] = bool(ict_pass)
    strat["ict_smc_confidence"] = confidence
    strat["ema_alignment_pass"] = bool(ema_pass)
    strat["swing_block_reason"] = block_reason
    strat["swing_watchlist_reason"] = watch_reason
    strat["ict_smc_details"] = ict_details
    strat["ema_details"] = ema_details
    strat["rc15f_swing_confirmation"] = diag

    if not allowed:
        reason = block_reason or watch_reason or "rc15f_swing_not_allowed"
        strat["no_trade"] = True
        strat["qualified"] = False
        strat["smc_swing_block"] = True
        strat["decision"] = "REJECTED_SWING_RC15F" if block_reason else "WATCHLIST_SWING_RC15F"
        strat["reject_reason"] = reason
        strat.setdefault("reasons", []).append(f"RC15f Swing not registered: {reason}")
    return strat

def _apply_swing_smc_filter(
    strat:  Dict[str, Any],
    smc:    Dict[str, Any],
    ds_1h:  Optional[Dict[str, Any]] = None,
    ds_15m: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    RC13 Swing SMC + Demand/Supply Confirmation Filter (Refined).

    D/S logic (smart, not rigid):
      - Demand near ALONE       → warning only (no reject, no score change)
      - Demand + 15m bullish    → hard reject (REJECTED_SWING_DEMAND_BOUNCE)
    Supply mirror for bull trades.

    SMC score adj (penalty always; boost only if base_score >= 45):
      - 1H against trade             → -10 penalty (always)
      - 1H aligned + 15m aligned     → +8  boost
      - 1H aligned only              → +6  boost
      - D/S location confirmation    → +4  extra boost (total capped at +12)
    """
    strategy_name = strat.get("strategy", "")
    is_bear_trade = strategy_name in ("Bear Call Spread", "Put Debit Spread")
    is_bull_trade = strategy_name in ("Bull Put Spread",  "Call Debit Spread")
    if not (is_bear_trade or is_bull_trade):
        return strat

    _smc_d = smc if isinstance(smc, dict) else {}
    h1  = _smc_d.get("h1")  or {}
    m15 = _smc_d.get("m15") or {}
    m5  = _smc_d.get("m5")  or {}
    smc_available = bool(_smc_d.get("available"))

    # RC15i.5: bias قد يكون dict من smc_0dte_mtf — نحوّله لـ str أولاً
    def _bias_str(val: Any) -> str:
        if isinstance(val, dict):
            val = val.get("bias") or val.get("direction") or ""
        return str(val or "neutral").lower()

    h1_bias  = _bias_str(h1.get("bias"))
    m15_bias = _bias_str(m15.get("bias"))
    m5_bias  = _bias_str(m5.get("bias"))

    h1_bullish  = h1_bias  in ("bullish",  "strong_bullish")
    h1_bearish  = h1_bias  in ("bearish",  "strong_bearish")
    m15_bullish = m15_bias in ("bullish",  "strong_bullish")
    m15_bearish = m15_bias in ("bearish",  "strong_bearish")
    m5_bullish  = m5_bias  in ("bullish",  "strong_bullish")
    m5_bearish  = m5_bias  in ("bearish",  "strong_bearish")

    ds1  = ds_1h  or {}
    nd1h = ds1.get("nearest_demand") or {}
    ns1h = ds1.get("nearest_supply") or {}
    inside_demand_1h = bool(ds1.get("inside_demand") or (nd1h and nd1h.get("inside")))
    near_demand_1h   = bool(ds1.get("near_demand")   or (nd1h and isinstance(nd1h.get("distance_pct"), (int, float)) and nd1h["distance_pct"] <= 0.50))
    inside_supply_1h = bool(ds1.get("inside_supply") or (ns1h and ns1h.get("inside")))
    near_supply_1h   = bool(ds1.get("near_supply")   or (ns1h and isinstance(ns1h.get("distance_pct"), (int, float)) and ns1h["distance_pct"] <= 0.50))

    demand_confidence = int(nd1h.get("confidence") or ds1.get("nearest_demand_confidence") or 0) if nd1h else 0
    supply_confidence = int(ns1h.get("confidence") or ds1.get("nearest_supply_confidence") or 0) if ns1h else 0
    demand_hard_reject_allowed = bool(ds1.get("demand_hard_reject_allowed") or nd1h.get("hard_reject_allowed"))
    supply_hard_reject_allowed = bool(ds1.get("supply_hard_reject_allowed") or ns1h.get("hard_reject_allowed"))
    demand_actionable = bool(nd1h.get("actionable") or demand_confidence >= 40)
    supply_actionable = bool(ns1h.get("actionable") or supply_confidence >= 40)

    base_score     = strat.get("score", 0)
    MIN_BASE_SCORE = 45  # boost cannot apply if base score is below this

    smc_adj      = 0
    smc_reason   = ""
    ds_reason    = ""
    ds_warning   = False
    final_action = "NO_CHANGE"

    def _reject(reason_short: str, detail: str, action_key: str) -> Dict[str, Any]:
        strat["no_trade"]        = True
        strat["qualified"]       = False
        strat["smc_swing_block"] = True
        strat["decision"]        = action_key if action_key.startswith("REJECTED_SWING_") else "REJECTED_SWING_SMC_CONFLICT"
        strat["reject_reason"]   = reason_short
        strat.setdefault("reasons", []).append(detail)
        strat["smc_swing_filter"] = {
            "applied":          True,
            "h1_bias":          h1_bias,
            "m15_bias":         m15_bias,
            "m5_bias":          m5_bias,
            "ema15_status":     m15.get("ema_status", "unavailable"),
            "ema5_status":      m5.get("ema_status", "unavailable"),
            "base_score":       base_score,
            "adj":              0,
            "action":           "hard_reject",
            "final_action":     action_key,
            "detail":           reason_short,
            "ds_block":         True,
            "ds_warning":       False,
            "nd_1h":            nd1h,
            "ns_1h":            ns1h,
            "inside_demand_1h": inside_demand_1h,
            "near_demand_1h":   near_demand_1h,
            "inside_supply_1h": inside_supply_1h,
            "near_supply_1h":   near_supply_1h,
            "demand_confidence": demand_confidence,
            "supply_confidence": supply_confidence,
            "demand_hard_reject_allowed": demand_hard_reject_allowed,
            "supply_hard_reject_allowed": supply_hard_reject_allowed,
        }
        print(f"[smc_swing_filter] {strategy_name} REJECTED ({action_key}): {reason_short}")
        return strat

    if is_bear_trade:
        # ── D/S: smart two-level check ──────────────────────────────
        if inside_demand_1h or near_demand_1h:
            if (m15_bullish or m5_bullish) and demand_hard_reject_allowed:
                nd_txt = f"D={nd1h.get('low','?')}-{nd1h.get('high','?')} dist={nd1h.get('distance_pct','?')}% conf={demand_confidence}"
                return _reject(
                    f"Price at/near strong 1H Demand ({nd_txt}) + confirmation 15m={m15_bias}, 5m={m5_bias}",
                    f"D/S Swing Block: {strategy_name} رُفض — strong Demand 1H confidence={demand_confidence} + bullish confirmation 15m={m15_bias}, 5m={m5_bias}",
                    action_key="REJECTED_SWING_DEMAND_BOUNCE",
                )
            else:
                # Demand near but not strong enough for hard reject, or no bullish confirm → warning only
                ds_warning = True
                ds_reason  = f"⚠ Demand 1H near/inside; confidence={demand_confidence}; hard_reject_allowed={demand_hard_reject_allowed}"

        # ── SMC: 1H + 15m both against → hard reject ────────────────
        if smc_available and h1_bullish and m15_bullish:
            return _reject(
                f"1H={h1_bias} & 15m={m15_bias} -> bullish conflict",
                f"SMC Swing Block: {strategy_name} رُفض — 1H={h1_bias} + 15m={m15_bias}",
                action_key="REJECTED_SWING_SMC_CONFLICT",
            )

        # ── Score adjustments ─────────────────────────────────────────
        if smc_available:
            if h1_bullish:
                smc_adj      = -10   # penalty always (regardless of base score)
                smc_reason   = f"SMC: -10 | 1H={h1_bias} ضد {strategy_name}"
                final_action = "PENALTY_SMC_AGAINST"
            elif h1_bearish and m15_bearish and base_score >= MIN_BASE_SCORE:
                smc_adj      = +8
                smc_reason   = f"SMC: +8 | 1H={h1_bias}+15m={m15_bias} يدعم"
                final_action = "BOOST_SMC_FULL"
            elif h1_bearish and base_score >= MIN_BASE_SCORE:
                smc_adj      = +6
                smc_reason   = f"SMC: +6 | 1H={h1_bias} يدعم"
                final_action = "BOOST_SMC_PARTIAL"
            elif base_score < MIN_BASE_SCORE and (h1_bearish):
                smc_reason   = f"SMC: boost skipped (base score {base_score} < {MIN_BASE_SCORE})"
                final_action = "BOOST_SKIPPED_LOW_BASE"

        # D/S boost: near Supply (aligns with bear) + 15m bearish
        if (inside_supply_1h or near_supply_1h) and supply_actionable and m15_bearish and smc_adj > 0 and base_score >= MIN_BASE_SCORE:
            ds_boost    = min(4, 12 - smc_adj)  # cap total at +12
            smc_adj    += ds_boost
            ds_reason   = f"D/S: +{ds_boost} Supply 1H near + 15m={m15_bias} confidence={supply_confidence}"
            final_action = "BOOST_SMC_DS"

    elif is_bull_trade:
        # ── D/S: smart two-level check ──────────────────────────────
        if inside_supply_1h or near_supply_1h:
            if (m15_bearish or m5_bearish) and supply_hard_reject_allowed:
                ns_txt = f"S={ns1h.get('low','?')}-{ns1h.get('high','?')} dist={ns1h.get('distance_pct','?')}% conf={supply_confidence}"
                return _reject(
                    f"Price at/near strong 1H Supply ({ns_txt}) + confirmation 15m={m15_bias}, 5m={m5_bias}",
                    f"D/S Swing Block: {strategy_name} رُفض — strong Supply 1H confidence={supply_confidence} + bearish confirmation 15m={m15_bias}, 5m={m5_bias}",
                    action_key="REJECTED_SWING_SUPPLY_REJECTION",
                )
            else:
                ds_warning = True
                ds_reason  = f"⚠ Supply 1H near/inside; confidence={supply_confidence}; hard_reject_allowed={supply_hard_reject_allowed}"

        if smc_available and h1_bearish and m15_bearish:
            return _reject(
                f"1H={h1_bias} & 15m={m15_bias} -> bearish conflict",
                f"SMC Swing Block: {strategy_name} رُفض — 1H={h1_bias} + 15m={m15_bias}",
                action_key="REJECTED_SWING_SMC_CONFLICT",
            )

        if smc_available:
            if h1_bearish:
                smc_adj      = -10
                smc_reason   = f"SMC: -10 | 1H={h1_bias} ضد {strategy_name}"
                final_action = "PENALTY_SMC_AGAINST"
            elif h1_bullish and m15_bullish and base_score >= MIN_BASE_SCORE:
                smc_adj      = +8
                smc_reason   = f"SMC: +8 | 1H={h1_bias}+15m={m15_bias} يدعم"
                final_action = "BOOST_SMC_FULL"
            elif h1_bullish and base_score >= MIN_BASE_SCORE:
                smc_adj      = +6
                smc_reason   = f"SMC: +6 | 1H={h1_bias} يدعم"
                final_action = "BOOST_SMC_PARTIAL"
            elif base_score < MIN_BASE_SCORE and h1_bullish:
                smc_reason   = f"SMC: boost skipped (base score {base_score} < {MIN_BASE_SCORE})"
                final_action = "BOOST_SKIPPED_LOW_BASE"

        if (inside_demand_1h or near_demand_1h) and demand_actionable and m15_bullish and smc_adj > 0 and base_score >= MIN_BASE_SCORE:
            ds_boost    = min(4, 12 - smc_adj)
            smc_adj    += ds_boost
            ds_reason   = f"D/S: +{ds_boost} Demand 1H near + 15m={m15_bias} confidence={demand_confidence}"
            final_action = "BOOST_SMC_DS"

    if final_action == "NO_CHANGE" and ds_warning:
        final_action = "WARNING_DS_ONLY"

    if smc_adj != 0:
        old_score      = strat.get("score", 0)
        strat["score"] = max(0, min(100, old_score + smc_adj))
        strat["smc_swing_adj"] = smc_adj
        full_reason = smc_reason + (f" | {ds_reason}" if ds_reason else "")
        strat.setdefault("reasons", []).append(full_reason)
        print(f"[smc_swing_filter] {strategy_name} score {old_score}->{strat['score']} adj={smc_adj:+d} action={final_action}")

    strat["smc_swing_filter"] = {
        "applied":          True,
        "h1_bias":          h1_bias,
        "m15_bias":         m15_bias,
        "m5_bias":          m5_bias,
        "ema15_status":     m15.get("ema_status", "unavailable"),
        "ema5_status":      m5.get("ema_status", "unavailable"),
        "base_score":       base_score,
        "adj":              smc_adj,
        "action":           "score_adj" if smc_adj != 0 else ("warning" if ds_warning else "no_change"),
        "final_action":     final_action,
        "detail":           smc_reason + (f" | {ds_reason}" if ds_reason else ""),
        "ds_block":         False,
        "ds_warning":       ds_warning,
        "nd_1h":            nd1h,
        "ns_1h":            ns1h,
        "inside_demand_1h": inside_demand_1h,
        "near_demand_1h":   near_demand_1h,
        "inside_supply_1h": inside_supply_1h,
        "near_supply_1h":   near_supply_1h,
        "demand_confidence": demand_confidence,
        "supply_confidence": supply_confidence,
        "demand_hard_reject_allowed": demand_hard_reject_allowed,
        "supply_hard_reject_allowed": supply_hard_reject_allowed,
    }
    return strat


def analyze_swing(tok: str, symbol: str, now: datetime,
                  min_dte_credit: int = 12, max_dte_credit: int = 17,
                  min_dte_debit:  int = 30, max_dte_debit:  int = 45) -> Dict[str, Any]:
    """
    تحليل Swing مستقل لـ SPY / QQQ / IWM / DIA / AAPL / NVDA / GLD.
    لا يُستخدم على SPX. AAPL/NVDA/GLD Swing-only ولا تدخل في 0DTE.

    المسارين:
      Credit Spread: DTE 12-17  | Single credit short strike ≈ 1.0σ | Wing=5
      Debit Spread:  DTE 30-45  | Preferred DTE≈35-40 | Long Leg delta 0.36-0.45 | Wing=5

    الشروط الأساسية:
      1. Trend 4H: bullish أو bearish (neutral = رفض)
      2. سيولة: bid/ask spread مقبول
      3. IV: مناسب لنوع الاستراتيجية
      4. Event Filter: FOMC/CPI/NFP
    """
    symbol_up = symbol.upper()

    from core.swing_pipeline_codes import ds_eval_from_context

    # SPX ممنوع من Swing
    if symbol_up == "SPX":
        return {
            "qualified": False, "score": 0, "symbol": symbol_up,
            "rejected": ["Swing rejected: SPX محجوز لـ 0DTE فقط"],
            "strategy": None, "expiry_date": "", "dte": 0,
            "pipeline_stop_code":    "STOPPED_AT_SPX_GUARD",
            "smc_evaluation":        "NOT_EVALUATED",
            "ds_evaluation":         "NOT_EVALUATED",
            "ds_unavailable_reason": None,
            "entry_protection":      "NOT_EVALUATED",
            "diagnostic_code":       "SMC_NOT_EVALUATED",
        }

    # RC13d — manual kill switch for single-name announcements/earnings/news.
    # This does not affect SPY/QQQ/IWM/DIA unless the symbol is explicitly listed.
    _blocked, _block_reason = _manual_swing_blocked(symbol_up)
    if _blocked:
        return {
            "qualified": False, "score": 0, "symbol": symbol_up,
            "rejected": [f"REJECTED_SWING_MANUAL_BLOCK: {_block_reason}"],
            "reasons": ["Manual Swing block active"],
            "strategy": None, "expiry_date": "", "dte": 0,
            "manual_swing_block": True,
            "manual_swing_block_reason": _block_reason,
            "pipeline_stop_code":    "STOPPED_AT_MANUAL_BLOCK",
            "smc_evaluation":        "NOT_EVALUATED",
            "ds_evaluation":         "NOT_EVALUATED",
            "ds_unavailable_reason": None,
            "entry_protection":      "NOT_EVALUATED",
            "diagnostic_code":       "SMC_NOT_EVALUATED",
        }

    rejected: list = []
    reasons:  list = []

    # ── 1. Trend 4H — الشرط الأساسي ─────────────────────────────────────────
    trend_data = get_4h_trend(symbol_up, tok)
    trend_4h   = trend_data.get("trend", "neutral")
    ema20_4h   = trend_data.get("ema20")
    ema50_4h   = trend_data.get("ema50")

    if trend_4h == "neutral":
        err = trend_data.get("error", "")
        neutral_reason = trend_data.get("neutral_reason", "")
        extra = err or neutral_reason
        rejected.append(f"Swing rejected: neutral trend (4H){' — ' + extra if extra else ''}")

        # RC13f: IV remains diagnostic even when the strict 4H gate rejects the trade.
        # This does not permit a trade; it only keeps Swing Diagnostics informative.
        _diag_price = trend_data.get("price") or _fetch_price_for_symbol(tok, symbol_up)
        _iv_rank = None
        _iv_pct = None
        _iv_pct_regime = None
        _iv_proxy = False
        _iv_source = "skipped — price unavailable"
        _iv_regime_data = classify_iv_regime(None)
        _vix = 0
        if _diag_price:
            try:
                _ctx = get_market_context(None, _diag_price, symbol=symbol_up, token=tok)
                _iv_rank = _ctx.get("iv_rank")
                _iv_pct = _ctx.get("iv_percentile")
                _iv_pct_regime = _ctx.get("iv_percentile_for_regime")
                _iv_proxy = bool(_ctx.get("iv_regime_proxy_used"))
                _iv_source = _ctx.get("iv_rank_source") or "market_context"
                _iv_regime_data = _ctx.get("iv_regime_data") or classify_iv_regime(_iv_pct_regime or _iv_pct)
                _vix = _ctx.get("vix") or 0
            except Exception as _iv_exc:
                _iv_source = f"IV diagnostics unavailable: {_iv_exc}"

        return {
            "qualified": False, "score": 0, "symbol": symbol_up,
            "rejected": rejected, "strategy": None,
            "expiry_date": "", "dte": 0,
            "pipeline_stage":        "stopped_at_4h_trend_filter",
            "pipeline_stop_code":    "STOPPED_AT_4H_TREND",
            "smc_evaluation":        "NOT_EVALUATED",
            "ds_evaluation":         "NOT_EVALUATED",
            "ds_unavailable_reason": None,
            "entry_protection":      "NOT_EVALUATED",
            "diagnostic_code":       "SMC_NOT_EVALUATED",
            "iv_diagnostics_only": True,
            "iv_rank": _iv_rank,
            "iv_percentile": _iv_pct,
            "iv_percentile_for_regime": _iv_pct_regime,
            "iv_regime_proxy_used": _iv_proxy,
            "iv_regime": (_iv_regime_data or {}).get("regime", "Unknown"),
            "iv_regime_data": _iv_regime_data,
            "iv_rank_source": _iv_source,
            "vix": _vix,
            "trend_4h": trend_4h, "ema20_4h": ema20_4h, "ema50_4h": ema50_4h,
            "trend_source": trend_data.get("source"),
            "trend_price_source": trend_data.get("price_source"),
            "trend_ema_source": trend_data.get("ema_source"),
            "trend_updated_at": trend_data.get("trend_updated_at"),
            "trend_live_price_error": trend_data.get("live_price_error"),
            "trend_yahoo_proxy_price": trend_data.get("yahoo_proxy_price"),
            "trend_ticker": trend_data.get("ticker"),
            "trend_interval": trend_data.get("interval_requested"),
            "trend_range": trend_data.get("range_requested"),
            "trend_resample": trend_data.get("resample_method"),
            "trend_raw_bars": trend_data.get("raw_bars"),
            "trend_4h_bars": trend_data.get("bars_4h"),
            "trend_price": trend_data.get("price"),
            "price_vs_ema20_pct": trend_data.get("price_vs_ema20_pct"),
            "price_vs_ema50_pct": trend_data.get("price_vs_ema50_pct"),
            "ema20_vs_ema50_pct": trend_data.get("ema20_vs_ema50_pct"),
            "ema_order": trend_data.get("ema_order"),
            "trend_strength_reason": trend_data.get("trend_strength_reason"),
            "trend_pressure_label": trend_data.get("trend_pressure_label"),
            "confirmed_trend": trend_data.get("confirmed_trend"),
            "min_price_ema20_dist_pct": trend_data.get("min_price_ema20_dist_pct"),
            "trend_neutral_reason": trend_data.get("neutral_reason"),
            "trend_error": trend_data.get("error"),
            "trend_decision_rule": trend_data.get("decision_rule"),
            "trend_dxlink_candle_error": trend_data.get("dxlink_candle_error"),
        }

    reasons.append(f"Trend 4H: {trend_4h} ✅")

    # فحص الامتداد عن EMA20 — تحذير فقط لا رفض
    ext_warn = _ema_extension_warning(trend_data.get("price") or 0, ema20_4h)
    if ext_warn:
        reasons.append(f"⚠️ {ext_warn}")

    # ── 2. Event Filter — تحذير وتخفيض Score فقط (لا رفض تلقائي) ───────────
    # الحدث وحده لا يرفض الصفقة — فقط يضيف warning ويخفض الـ Score
    event_check          = _check_event_risk(days_ahead=max_dte_debit)
    event_warning        = ""
    event_score_penalty  = 0
    event_info           = {}      # للعرض في الـ UI
    credit_event_blocked = False   # لا يُستخدم للرفض — للتحذير فقط

    if event_check["has_event"]:
        ev      = event_check["events"][0]
        days    = ev["days_ahead"]
        name_ev = ev["name"]
        ev_date = ev["date"]

        if days <= 1:
            event_score_penalty  = -25
            event_warning        = f"High Event Risk: {name_ev} غداً!"
            credit_event_blocked = True   # تحذير Credit فقط
            reasons.append(f"⚠️ High Event Risk: {name_ev} في {days} يوم (-25 نقطة)")
        elif days <= 3:
            event_score_penalty  = -15
            event_warning        = f"Event Risk: {name_ev} خلال {days} أيام"
            credit_event_blocked = True
            reasons.append(f"⚠️ Event Risk: {name_ev} في {days} أيام (-15 نقطة)")
        elif days <= 5:
            event_score_penalty  = -8
            event_warning        = f"Event Warning: {name_ev} خلال {days} أيام"
            reasons.append(f"⚠️ Event Warning: {name_ev} في {days} أيام (-8 نقطة)")
        elif days <= 10:
            event_score_penalty  = -3
            event_warning        = f"Event Notice: {name_ev} خلال {days} أيام"
            reasons.append(f"📅 Event Notice: {name_ev} في {days} أيام (-3 نقطة)")

        event_info = {
            "name":    name_ev,
            "date":    ev_date,
            "days":    days,
            "penalty": event_score_penalty,
            "warning": event_warning,
        }

    # ── 3. السعر الحالي ───────────────────────────────────────────────────────
    price = _fetch_price_for_symbol(tok, symbol_up)
    if not price:
        return {
            "qualified": False, "score": 0, "symbol": symbol_up,
            "rejected": ["Swing rejected: تعذّر جلب السعر"],
            "strategy": None, "expiry_date": "", "dte": 0,
            "pipeline_stop_code":    "PRICE_UNAVAILABLE",
            "smc_evaluation":        "NOT_EVALUATED",
            "ds_evaluation":         "NOT_EVALUATED",
            "ds_unavailable_reason": None,
            "entry_protection":      "NOT_EVALUATED",
            "diagnostic_code":       "SMC_NOT_EVALUATED",
        }

    # ── 4. IV Percentile + VIX ───────────────────────────────────────────────
    iv_rank                  = 0
    iv_percentile            = None
    iv_percentile_for_regime = None
    iv_regime_proxy_used     = False
    iv_rank_source           = "N/A"
    iv_regime_data           = {}
    vix                      = 0
    try:
        ctx                      = get_market_context(None, price, symbol=symbol_up, token=tok)
        iv_rank                  = ctx.get("iv_rank") or 0
        iv_percentile            = ctx.get("iv_percentile")
        iv_percentile_for_regime = ctx.get("iv_percentile_for_regime")
        iv_regime_proxy_used     = ctx.get("iv_regime_proxy_used", False)
        iv_rank_source           = ctx.get("iv_rank_source", "N/A")
        iv_regime_data           = ctx.get("iv_regime_data") or {}
        vix                      = ctx.get("vix") or 0
    except Exception:
        pass

    iv_regime_data = iv_regime_data or classify_iv_regime(iv_percentile_for_regime or iv_percentile)

    # ── 5. اختيار نوع الصفقة بناءً على IV Percentile ─────────────────────────
    # Credit DTE 12-17 : يسمح عند IV Percentile >= 50%, قوي >= 60%
    # Debit  DTE 30-45 : يسمح عند IV Percentile <= 40%, قوي <= 30%
    # Neutral 40-50%   : لا افضلية لا نفتح Swing
    use_credit = iv_regime_data.get("credit_ok", False)
    use_debit  = iv_regime_data.get("debit_ok",  False)
    iv_regime  = iv_regime_data.get("regime", "Unknown")
    iv_reason  = iv_regime_data.get("reason", "")

    reasons.append(f"IV Percentile={iv_percentile:.0f}% | IV Regime={iv_regime}"
                   if iv_percentile is not None
                   else "IV Percentile: unavailable")

    reasons.append(iv_reason)

    if credit_event_blocked and use_credit:
        reasons.append(f"⚠️ {event_warning} — Credit allowed but watch Premium")

    # ── 6. جلب الـ chain الصحيح حسب IV ──────────────────────────────────────────
    # IV >= 25 → Credit chain (12-17 DTE)
    # IV <  25 → Debit chain  (30-45 DTE, preferred midpoint≈37.5) — IV منخفض يعني Credit ضعيف
    # المبدأ: DTE يجب أن يطابق نوع الاستراتيجية دائماً
    strategy_result  = None
    expiry_date      = ""
    dte              = 0
    swing_em         = round(price * 0.004 * (18 ** 0.5), 1)  # افتراضي
    chain_credit     = chain_debit = None
    active_chain     = None
    _chain_stop_code = "NO_VALID_EXPIRY"   # يُحدَّث عند فشل الجلب

    def _fetch_and_validate(min_dte, max_dte, label):
        """يجلب chain ويتحقق من السيولة. يعيد (chain, expiry, dte, em) أو None."""
        nonlocal _chain_stop_code
        try:
            ch = get_swing_options_chain(tok, symbol_up,
                                         min_dte=min_dte, max_dte=max_dte)
            if not ch:
                # السلسلة لم تُجلب أصلاً (network/API failure)
                _chain_stop_code = "CHAIN_UNAVAILABLE"
                rejected.append(f"Swing rejected: chain unavailable ({min_dte}-{max_dte} DTE) [{label}]")
                return None
            enrich_exposures_with_spot(ch, price)
            exp = ch.get("expiry", "")
            if not exp:
                # السلسلة جُلبت لكن لا يوجد تاريخ انتهاء مناسب
                _chain_stop_code = "NO_VALID_EXPIRY"
                rejected.append(f"Swing rejected: no valid expiry in {min_dte}-{max_dte} DTE [{label}]")
                return None
            d    = (datetime.strptime(exp, "%Y-%m-%d").date() - now.date()).days
            em_d = calculate_expected_move(ch, price)
            em   = em_d.get("move") or swing_em
            # فحص سيولة
            opts = ch.get("calls", []) + ch.get("puts", [])
            wide = sum(1 for o in opts
                       if (o.get("ask") or 0) > 0 and
                       (o.get("ask", 0) - o.get("bid", 0)) / o["ask"] > 0.40)
            if opts and wide > len(opts) * 0.5:
                rejected.append(f"Swing rejected: poor liquidity على {label} chain")
                return None
            print(f"[analyze_swing] {symbol_up} {label} chain ✅ expiry={exp} DTE={d}")
            return ch, exp, d, em
        except Exception as _e:
            _chain_stop_code = "CHAIN_UNAVAILABLE"
            rejected.append(f"Swing rejected: خطأ في {label} chain ({_e})")
            return None

    if not rejected:
        if use_credit:
            # IV كافٍ للـ Credit → جرّب Credit chain أولاً
            result = _fetch_and_validate(min_dte_credit, max_dte_credit, "Credit")
            if result:
                chain_credit, expiry_date, dte, swing_em = result

        if not chain_credit:
            # IV منخفض أو Credit chain فشل → Debit chain (30-45 DTE)
            result = _fetch_and_validate(min_dte_debit, max_dte_debit, "Debit")
            if result:
                chain_debit, expiry_date, dte, swing_em = result

    # تحديد الـ chain النشط مع تأكيد DTE المناسب للنوع
    if chain_credit:
        active_chain   = chain_credit
        chain_type_tag = "Credit"
    elif chain_debit:
        active_chain   = chain_debit
        chain_type_tag = "Debit"
    else:
        chain_type_tag = "None"
    if not active_chain:
        if not rejected:
            rejected.append("Swing rejected: chain unavailable")
        return {
            "qualified": False, "score": 0, "symbol": symbol_up,
            "rejected": rejected, "reasons": reasons,
            "strategy": None, "expiry_date": expiry_date, "dte": dte,
            "trend_4h": trend_4h, "iv_rank": iv_rank,
            "iv_percentile": iv_percentile, "iv_regime": iv_regime,
            "iv_regime_reason": iv_reason, "vix": vix,
            "pipeline_stop_code":    _chain_stop_code,
            "smc_evaluation":        "NOT_EVALUATED",
            "ds_evaluation":         "NOT_EVALUATED",
            "ds_unavailable_reason": None,
            "entry_protection":      "NOT_EVALUATED",
            "diagnostic_code":       "SMC_NOT_EVALUATED",
        }

    # ── 7. تشغيل Swing Engine بالاستراتيجيات المناسبة للـ trend ──────────────
    if not rejected:
        try:
            from core.strategy_engine import run_swing_engine
            swing_levels = {
                "combined_trend":   trend_4h,
                "trend":            trend_4h,
                "daily_trend":      trend_4h,
                "expected_move":    swing_em,
                "iv_rank":          iv_rank,
                "iv_percentile":    iv_percentile,
                "iv_regime":        iv_regime,
                "iv_regime_data":   iv_regime_data,
                "vix":              vix,
                "ema20":            ema20_4h,
                "ema50":            ema50_4h,
            }
            all_o = active_chain.get("calls", []) + active_chain.get("puts", [])
            dq = {
                "has_delta":  bool(any(o.get("delta") for o in all_o)),
                "has_prices": bool(any(o.get("mid")   for o in all_o)),
            }
            strategy_result = run_swing_engine(
                price          = price,
                iv_rank        = iv_rank,
                iv_percentile  = iv_percentile,
                iv_regime      = iv_regime,
                iv_regime_data = iv_regime_data,
                expected_move = swing_em,
                ema20         = ema20_4h,
                ema50         = ema50_4h,
                chain_data    = active_chain,
                levels        = swing_levels,
                data_quality  = dq,
                vix           = vix,
                symbol        = symbol_up,
            )
            if strategy_result and not strategy_result.get("no_trade"):
                strat_type = strategy_result.get("strategy", "")
                is_debit_strat = strat_type in ("Call Debit Spread", "Put Debit Spread")

                # ── تحقق مطابقة DTE للاستراتيجية ─────────────────────────────
                # Debit يجب DTE 30-45 — إذا استُخدم Credit chain (14 DTE) نُعيد
                if is_debit_strat and chain_type_tag == "Credit":
                    print(f"[analyze_swing] ⚠️ {symbol_up}: {strat_type} على Credit chain "
                          f"(DTE={dte}) → إعادة التشغيل بـ Debit chain (30-45 DTE)")
                    result2 = _fetch_and_validate(min_dte_debit, max_dte_debit, "Debit-retry")
                    if result2:
                        chain_debit, expiry_date, dte, swing_em = result2
                        # أعد تشغيل الـ engine بالـ chain الصحيح
                        swing_levels["expected_move"] = swing_em
                        all_o2 = chain_debit.get("calls",[]) + chain_debit.get("puts",[])
                        dq2 = {
                            "has_delta":  bool(any(o.get("delta") for o in all_o2)),
                            "has_prices": bool(any(o.get("mid")   for o in all_o2)),
                        }
                        strategy_result = run_swing_engine(
                            price=price, iv_rank=iv_rank,
                            expected_move=swing_em, ema20=ema20_4h, ema50=ema50_4h,
                            chain_data=chain_debit, levels=swing_levels,
                            data_quality=dq2, vix=vix, symbol=symbol_up,
                        )
                        if strategy_result and strategy_result.get("no_trade"):
                            rejected.append("Swing rejected: Debit chain لا تعطي استراتيجية صالحة")
                            strategy_result = None
                    else:
                        # فشل جلب Debit chain
                        strategy_result = None

                if strategy_result and not strategy_result.get("no_trade"):
                    strategy_result["trade_mode"]    = "Swing"
                    strategy_result["expiry_date"]   = expiry_date
                    strategy_result["dte_at_entry"]  = dte
                    strategy_result["selected_mode"] = "Swing"
                    strategy_result["chain_type"]    = "Debit" if is_debit_strat else "Credit"
                    if is_debit_strat:
                        strategy_result["dte_rule"] = "Allowed 30-45; preferred near 35-40"
                        strategy_result["delta_rule"] = "Long leg delta 0.36-0.45"
                    else:
                        strategy_result["dte_rule"] = "Credit DTE 12-17"
                        strategy_result["sigma_rule"] = "Single credit 1.0σ; IC 1.5σ"
                    reasons.append(f"Strategy: {strategy_result.get('strategy','?')} "
                                   f"DTE={dte} score={strategy_result.get('score',0)}/100")
            elif strategy_result:
                rejected.append(
                    f"Swing rejected: {' | '.join(strategy_result.get('reasons', ['no valid strategy']))}")
                strategy_result = None
        except Exception as _e:
            rejected.append(f"Swing rejected: خطأ في Swing Engine ({_e})")
            strategy_result = None

    # ── 8. RC13d Swing SMC + LuxAlgo-style D/S filters inside unified analyze_swing ──
    # This ensures the Swing cache and AAPL/NVDA use the same entry-protection logic,
    # not only the hybrid analyze_symbol() path.
    swing_ds_1h_context: Dict[str, Any] = {}
    swing_ds_15m_context: Dict[str, Any] = {}
    smc_0dte_mtf_context: Dict[str, Any] = {
        "available": False, "diagnostics_only": True, "reason": "not_evaluated"
    }

    # حقول التشخيص — تُحسب هنا ثم تُدمج في الناتج النهائي
    # _pipeline_stop_code يُحدَّد بناءً على ما وصلت إليه Pipeline فعلياً:
    #   - لم تنتج استراتيجية     → NO_STRATEGY_CANDIDATE
    #   - رُفضت قبل SMC بفلتر  → REJECTED_BY_DELTA / REJECTED_BY_SCORE / ...
    #   - وصلت إلى SMC/DS        → SMC_EVALUATED (يُبقى حتى النهاية أو يُعدَّل بالـ REJECTED_BY_SMC/DS)
    if not strategy_result:
        # استنتج سبب التوقف من قائمة rejected
        _rej_text = " ".join(rejected).lower()
        if "delta" in _rej_text:
            _pipeline_stop_code = "REJECTED_BY_DELTA"
        elif "liquidity" in _rej_text or "spread" in _rej_text:
            _pipeline_stop_code = "REJECTED_BY_LIQUIDITY"
        elif "score" in _rej_text or "threshold" in _rej_text:
            _pipeline_stop_code = "REJECTED_BY_SCORE"
        elif "credit" in _rej_text and "width" in _rej_text:
            _pipeline_stop_code = "REJECTED_BY_CREDIT_WIDTH"
        elif "event" in _rej_text:
            _pipeline_stop_code = "REJECTED_BY_EVENT"
        else:
            _pipeline_stop_code = "NO_STRATEGY_CANDIDATE"
    else:
        _pipeline_stop_code = "SMC_EVALUATED"   # سيُعدَّل أدناه عند REJECTED_BY_SMC/DS

    _smc_evaluation      = "NOT_EVALUATED"
    _ds_evaluation_1h    = "NOT_EVALUATED"
    _ds_unavail_reason   = None
    _entry_protection    = "NOT_EVALUATED"
    _diagnostic_code     = "SMC_NOT_EVALUATED"

    if strategy_result and not strategy_result.get("no_trade"):
        # ── SMC ──
        try:
            from core.smc_0dte_mtf import analyze_0dte_smc_mtf
            smc_0dte_mtf_context = analyze_0dte_smc_mtf(
                symbol_up, price, strategy_name=strategy_result.get("strategy"), access_token=tok
            )
            _smc_evaluation = "PASSED" if smc_0dte_mtf_context.get("available") else "UNAVAILABLE"
        except Exception as _smc_swing_err:
            smc_0dte_mtf_context = {
                "available": False, "diagnostics_only": True,
                "error": str(_smc_swing_err), "symbol": symbol_up,
            }
            _smc_evaluation = "UNAVAILABLE"
            print(f"[swing_smc error] {symbol_up}: {_smc_swing_err}")

        # ── Demand/Supply ──
        try:
            from core.demand_supply import detect_swing_demand_supply_zones
            swing_ds_1h_context  = detect_swing_demand_supply_zones(symbol_up, price, "1H")
            swing_ds_15m_context = detect_swing_demand_supply_zones(symbol_up, price, "15M")
        except Exception as _ds_swing_err:
            swing_ds_1h_context  = {"available": False, "error": str(_ds_swing_err), "symbol": symbol_up}
            swing_ds_15m_context = {"available": False, "error": str(_ds_swing_err), "symbol": symbol_up}
            print(f"[swing_ds error] {symbol_up}: {_ds_swing_err}")

        _ds_evaluation_1h, _ds_unavail_reason = ds_eval_from_context(swing_ds_1h_context)

        # ── RC15f hard confirmation for Swing diagnostics ─────────────────
        # This mirrors the final paper-registration guard so the Swing Diag UI
        # never shows a candidate as fully Qualified unless RC15f also allows it.
        strategy_result = _apply_rc15f_swing_diag_to_strategy(strategy_result, smc_0dte_mtf_context)

        if not strategy_result.get("no_trade"):
            # ── Apply legacy combined SMC/D-S filter only after RC15f passes ──
            strategy_result = _apply_swing_smc_filter(
                strategy_result, smc_0dte_mtf_context,
                ds_1h=swing_ds_1h_context, ds_15m=swing_ds_15m_context,
            )

        strategy_result["smc_0dte_mtf"]      = smc_0dte_mtf_context
        strategy_result["smc_0dte_mtf_full"] = smc_0dte_mtf_context
        strategy_result["swing_ds_1h"]       = swing_ds_1h_context
        strategy_result["swing_ds_15m"]      = swing_ds_15m_context

        final_action = (strategy_result.get("smc_swing_filter") or {}).get("final_action", "")

        if strategy_result.get("no_trade"):
            _rr = strategy_result.get("reject_reason") or strategy_result.get("decision") or "Swing rejected by RC15f/SMC/Demand-Supply filter"
            rejected.append(_rr)
            if strategy_result.get("decision") in ("REJECTED_SWING_RC15F", "WATCHLIST_SWING_RC15F"):
                _pipeline_stop_code = "REJECTED_BY_RC15F"
                _smc_evaluation     = "FAILED"
                _entry_protection   = "HARD_REJECTED"
                _diagnostic_code    = "RC15F_BLOCKED" if strategy_result.get("swing_block_reason") else "RC15F_WATCHLIST"
            else:
                _pipeline_stop_code = "REJECTED_BY_SMC" if "SMC" in (strategy_result.get("decision") or "") else "REJECTED_BY_DS"
                _smc_evaluation     = "FAILED" if "SMC" in (strategy_result.get("decision") or "") else _smc_evaluation
                _entry_protection   = "HARD_REJECTED"
                _diagnostic_code    = "SMC_CONFLICT" if "SMC" in (strategy_result.get("decision") or "") else "DS_CONFLICT"
        else:
            # تحديد entry_protection بناءً على حالة D/S
            if _ds_evaluation_1h == "UNAVAILABLE":
                _entry_protection = "PASSED_WITH_WARNING"
                _diagnostic_code  = "DS_UNAVAILABLE"
            elif _ds_evaluation_1h == "NO_ACTIVE_ZONE":
                _entry_protection = "PASSED"
                _diagnostic_code  = "DS_NO_ACTIVE_ZONE"
            elif _ds_evaluation_1h == "WARNING_ZONE":
                _entry_protection = "PASSED_WITH_WARNING"
                _diagnostic_code  = "DS_WARNING_ZONE"
            elif final_action == "BOOST_SMC_DS":
                _entry_protection = "PASSED"
                _diagnostic_code  = "DS_PASSED"
            else:
                _entry_protection = "PASSED"
                _diagnostic_code  = "DS_PASSED"

            # SMC evaluation نهائي — يعتمد على تشغيل الفلتر لا على اسم final_action
            # إذا تشغّل الفلتر ولم يحدث رفض hard (الرفض عُولج أعلاه) → الصفقة اجتازت SMC
            _smc_filter_result = (strategy_result.get("smc_swing_filter") or {})
            if _smc_filter_result.get("applied"):
                _smc_evaluation = "PASSED"
            # else: يبقى PASSED أو UNAVAILABLE كما حُدد من smc_0dte_mtf_context

    # ── 9. IV — رسالة واضحة حسب نوع الاستراتيجية ───────────────────────────
    if strategy_result:
        strat_name = strategy_result.get("strategy", "")
        is_credit_strat = strat_name in ("Bull Put Spread", "Bear Call Spread", "Iron Condor")
        strategy_result.setdefault("warnings", [])
        if iv_rank < 20:
            if is_credit_strat:
                strategy_result["warnings"].append(
                    f"IV Rank منخفض ({iv_rank:.0f}) — غير مناسب لـ Credit (Premium ضعيف)")
            else:
                strategy_result["warnings"].append(
                    f"IV Rank منخفض ({iv_rank:.0f}) ✓ مناسب لـ Debit (Premium رخيص)")
        elif iv_rank > 70:
            if not is_credit_strat:
                strategy_result["warnings"].append(
                    f"IV Rank مرتفع ({iv_rank:.0f}) — غير مثالي لـ Debit (Premium غالٍ)")
            else:
                strategy_result["warnings"].append(
                    f"IV Rank مرتفع ({iv_rank:.0f}) ✓ مناسب لـ Credit (Premium جيد)")

    # ── 10. تطبيق Event Score Penalty فعلياً ──────────────────────────────────
    if strategy_result and event_score_penalty != 0:
        old_score = strategy_result.get("score", 0)
        new_score = max(0, min(100, old_score + event_score_penalty))
        strategy_result["score"]          = new_score
        strategy_result["event_penalty"]  = event_score_penalty
        strategy_result["score_breakdown"] = (strategy_result.get("score_breakdown") or {})
        strategy_result["score_breakdown"]["Event"] = event_score_penalty

    qualified              = bool(strategy_result) and not rejected and not bool((strategy_result or {}).get("no_trade"))
    # D/S unavailable يُجبر الحالة على warning حتى بدون event
    _ds_forces_warning    = qualified and _ds_evaluation_1h == "UNAVAILABLE"
    qualified_with_warning = qualified and (bool(event_warning) or _ds_forces_warning)

    # ── Log ──────────────────────────────────────────────────────────────────
    strat_name = (strategy_result or {}).get("strategy", "—")
    status_tag = ("Qualified with Warning" if qualified_with_warning
                  else "Qualified" if qualified else "Rejected")
    print(f"[analyze_swing] {symbol_up} | {status_tag} | "
          f"trend={trend_4h} IV={iv_rank:.0f} DTE={dte} strategy={strat_name}")
    if event_warning:
        print(f"  📅 {event_warning} (penalty={event_score_penalty})")
    for r in rejected:
        print(f"  ⚠️ {r}")

    return {
        "qualified":              qualified,
        "qualified_with_warning": qualified_with_warning,
        "status":                 status_tag,
        "score":                  (strategy_result or {}).get("score", 0),
        "reasons":                reasons,
        "rejected":               rejected,
        "strategy":               strategy_result,
        "expiry_date":            expiry_date,
        "dte":                    dte,
        "price":                  price,
        "ema20_4h":               ema20_4h,
        "ema50_4h":               ema50_4h,
        # ── Pipeline diagnostic codes (RC14) ──────────────────────────
        "pipeline_stop_code":      _pipeline_stop_code,
        "smc_evaluation":          _smc_evaluation,
        "ds_evaluation":           _ds_evaluation_1h,
        "ds_unavailable_reason":   _ds_unavail_reason,
        "entry_protection":        _entry_protection,
        "diagnostic_code":         _diagnostic_code,
        "iv_rank":                    iv_rank,
        "iv_percentile":              iv_percentile,
        "iv_percentile_for_regime":   iv_percentile_for_regime,
        "iv_regime_proxy_used":       iv_regime_proxy_used,
        "iv_regime":                  iv_regime,
        "iv_regime_data":             iv_regime_data,
        "iv_rank_source":             iv_rank_source,
        "vix":                        vix,
        "swing_em":                   swing_em,
        "trend_4h":                   trend_4h,
        "trend_source":           trend_data.get("source"),
        "trend_price_source":     trend_data.get("price_source"),
        "trend_ema_source":       trend_data.get("ema_source"),
        "trend_updated_at":       trend_data.get("trend_updated_at"),
        "trend_live_price_error": trend_data.get("live_price_error"),
        "trend_yahoo_proxy_price": trend_data.get("yahoo_proxy_price"),
        "trend_ticker":           trend_data.get("ticker"),
        "trend_interval":         trend_data.get("interval_requested"),
        "trend_range":            trend_data.get("range_requested"),
        "trend_resample":         trend_data.get("resample_method"),
        "trend_raw_bars":         trend_data.get("raw_bars"),
        "trend_4h_bars":          trend_data.get("bars_4h"),
        "trend_price":            trend_data.get("price"),
        "price_vs_ema20_pct":     trend_data.get("price_vs_ema20_pct"),
        "price_vs_ema50_pct":     trend_data.get("price_vs_ema50_pct"),
        "ema20_vs_ema50_pct":     trend_data.get("ema20_vs_ema50_pct"),
        "ema_order":              trend_data.get("ema_order"),
        "trend_strength_reason":  trend_data.get("trend_strength_reason"),
        "trend_pressure_label":   trend_data.get("trend_pressure_label"),
        "confirmed_trend":        trend_data.get("confirmed_trend"),
        "min_price_ema20_dist_pct": trend_data.get("min_price_ema20_dist_pct"),
        "trend_neutral_reason":        trend_data.get("neutral_reason"),
        "trend_error":                 trend_data.get("error"),
        "trend_decision_rule":         trend_data.get("decision_rule"),
        "trend_dxlink_candle_error":   trend_data.get("dxlink_candle_error"),
        "symbol":                      symbol_up,
        "event_info":             event_info,
        "event_warning":          event_warning,
        "_chain":                 active_chain,
    }


def _print_sigma_debug_table(
    symbol: str,
    price: float,
    price_source: Optional[str],
    chain: Optional[Dict[str, Any]],
    levels: Dict[str, Any],
    strategy_result: Optional[Dict[str, Any]],
) -> None:
    """
    يطبع جدولاً تشخيصياً تفصيلياً لحساب 1.5σ وبيانات العقود المختارة.
    يُستدعى بعد اكتمال التحليل لأي رمز — يُساعد في مقارنة النتائج مع Tastytrade يدوياً.
    """
    sep  = "=" * 72
    sep2 = "-" * 72

    em_raw       = levels.get("expected_move") or 0
    em_source    = levels.get("expected_move_source", "?")
    atm_strike   = levels.get("em_atm_strike")
    straddle_mid = levels.get("straddle_mid")
    expiry       = (chain or {}).get("expiry", "?")
    hours_to_exp = (chain or {}).get("hours_to_expiry", 0)

    # ATM call/put data
    atm_call_data: Optional[Dict] = None
    atm_put_data:  Optional[Dict] = None
    if chain and atm_strike:
        atm_call_data = next(
            (c for c in chain.get("calls", []) if c.get("strike") == atm_strike), None)
        atm_put_data  = next(
            (p for p in chain.get("puts",  []) if p.get("strike") == atm_strike), None)

    sigma_mult = 1.5
    distance   = round(em_raw * sigma_mult, 1)
    bull_put_target  = round(price - distance, 1)
    bear_call_target = round(price + distance, 1)

    def _fv(v: Any, decimals: int = 2) -> str:
        if v is None: return "N/A"
        try: return f"{float(v):.{decimals}f}"
        except Exception: return str(v)

    def _leg_row(leg: Optional[Dict], label: str) -> str:
        if not leg:
            return f"  {label:<28} N/A"
        sym    = leg.get("symbol", "?")
        strike = _fv(leg.get("strike"), 0)
        bid    = _fv(leg.get("bid"))
        ask    = _fv(leg.get("ask"))
        mid    = _fv(leg.get("mid"))
        delta  = _fv(leg.get("delta"), 3)
        return (f"  {label:<28} sym={sym}\n"
                f"  {'':28} strike={strike}  bid={bid}  ask={ask}  mid={mid}  delta={delta}")

    print(sep)
    print(f"  [1.5σ DEBUG TABLE]  {symbol}  @  {price:.2f}  |  {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(sep)

    # ── 1. السعر الحالي ──────────────────────────────────────────────────────
    print("  ── 1. UNDERLYING PRICE ──")
    print(f"  underlying_price   = {price:.2f}")
    print(f"  price_source       = {price_source or 'unknown'}")
    print(sep2)

    # ── 2. Expected Move ─────────────────────────────────────────────────────
    print("  ── 2. EXPECTED MOVE CALCULATION ──")
    print(f"  expiry             = {expiry}")
    print(f"  hours_to_expiry    = {_fv(hours_to_exp, 2)}")
    print(f"  atm_strike         = {_fv(atm_strike, 0)}")
    if atm_call_data:
        c_bid = _fv(atm_call_data.get("bid"))
        c_ask = _fv(atm_call_data.get("ask"))
        c_mid = _fv(atm_call_data.get("mid"))
        print(f"  ATM Call           bid={c_bid}  ask={c_ask}  mid={c_mid}")
    else:
        print("  ATM Call           N/A")
    if atm_put_data:
        p_bid = _fv(atm_put_data.get("bid"))
        p_ask = _fv(atm_put_data.get("ask"))
        p_mid = _fv(atm_put_data.get("mid"))
        print(f"  ATM Put            bid={p_bid}  ask={p_ask}  mid={p_mid}")
    else:
        print("  ATM Put            N/A")
    if straddle_mid is not None:
        raw_straddle = straddle_mid
        computed_em  = round(raw_straddle * 0.90, 2)
        print(f"  straddle_mid       = {raw_straddle:.2f}  (call_mid + put_mid)")
        print(f"  EM = straddle×0.90 = {computed_em:.2f}")
    else:
        print("  straddle_mid       = N/A")
    print(f"  EM (used)          = {em_raw:.2f}")
    print(f"  EM source          = {em_source}")
    print(sep2)

    # ── 3. حساب 1.5σ ────────────────────────────────────────────────────────
    print("  ── 3. 1.5σ CALCULATION ──")
    print(f"  sigma_multiplier   = {sigma_mult}")
    print(f"  EM                 = {em_raw:.2f}")
    print(f"  distance (EM×1.5)  = {distance:.2f}")
    print(sep2)

    # ── 4. مستويات الـ short strike ──────────────────────────────────────────
    print("  ── 4. SHORT STRIKE TARGETS ──")
    print(f"  Bull Put  short target = {price:.2f} - {distance:.2f} = {bull_put_target:.2f}")
    print(f"  Bear Call short target = {price:.2f} + {distance:.2f} = {bear_call_target:.2f}")

    # أقرب strike متاح
    if chain:
        put_strikes  = sorted([p["strike"] for p in chain.get("puts",  [])], reverse=True)
        call_strikes = sorted([c["strike"] for c in chain.get("calls", [])])
        nearest_put  = min(put_strikes,  key=lambda s: abs(s - bull_put_target),  default=None)
        nearest_call = min(call_strikes, key=lambda s: abs(s - bear_call_target), default=None)
        print(f"  nearest put strike     = {_fv(nearest_put,  0)}  (target {bull_put_target:.0f})")
        print(f"  nearest call strike    = {_fv(nearest_call, 0)}  (target {bear_call_target:.0f})")
    print(sep2)

    # ── 5. بيانات العقود المختارة ────────────────────────────────────────────
    strat = strategy_result or {}
    strat_name = strat.get("strategy", "?")
    print(f"  ── 5. SELECTED CONTRACTS  ({strat_name}) ──")

    def _opt_data(opt_type: str, strike: Optional[float]) -> Optional[Dict]:
        if not chain or strike is None: return None
        pool = chain.get("calls" if opt_type == "call" else "puts", [])
        return next((o for o in pool if o.get("strike") == strike), None)

    # Bull Put legs
    sp_strike = strat.get("short_put")
    lp_strike = strat.get("long_put")
    sp_d = _opt_data("put",  sp_strike)
    lp_d = _opt_data("put",  lp_strike)

    if sp_d or sp_strike:
        sp_bid = _fv((sp_d or {}).get("bid")) if sp_d else "N/A"
        sp_ask = _fv((sp_d or {}).get("ask")) if sp_d else "N/A"
        sp_mid = _fv((sp_d or {}).get("mid")) if sp_d else "N/A"
        sp_dlt = _fv((sp_d or {}).get("delta"), 3) if sp_d else "N/A"
        lp_bid = _fv((lp_d or {}).get("bid")) if lp_d else "N/A"
        lp_ask = _fv((lp_d or {}).get("ask")) if lp_d else "N/A"
        lp_mid = _fv((lp_d or {}).get("mid")) if lp_d else "N/A"
        sym_sp = (sp_d or {}).get("symbol", _fv(sp_strike, 0))
        sym_lp = (lp_d or {}).get("symbol", _fv(lp_strike, 0))

        bp_credit = strat.get("credit") if strat_name == "Bull Put Spread" else None
        wing      = abs((sp_strike or 0) - (lp_strike or 0)) or 1
        cw_ratio  = round((bp_credit or 0) / wing * 100, 1) if bp_credit and wing else None

        print(f"  Bull Put:")
        print(f"    short_put  sym={sym_sp}  strike={_fv(sp_strike,0)}")
        print(f"               bid={sp_bid}  ask={sp_ask}  mid={sp_mid}  delta={sp_dlt}")
        print(f"    long_put   sym={sym_lp}  strike={_fv(lp_strike,0)}")
        print(f"               bid={lp_bid}  ask={lp_ask}  mid={lp_mid}")
        print(f"    credit (short_bid - long_ask) = {_fv(bp_credit)}")
        print(f"    credit/width ratio = {_fv(cw_ratio, 1)}%  (wing={_fv(wing,0)})")
    else:
        print("  Bull Put: N/A (no short_put in strategy result)")

    # Bear Call legs
    sc_strike = strat.get("short_call")
    lc_strike = strat.get("long_call")
    sc_d = _opt_data("call", sc_strike)
    lc_d = _opt_data("call", lc_strike)

    if sc_d or sc_strike:
        sc_bid = _fv((sc_d or {}).get("bid")) if sc_d else "N/A"
        sc_ask = _fv((sc_d or {}).get("ask")) if sc_d else "N/A"
        sc_mid = _fv((sc_d or {}).get("mid")) if sc_d else "N/A"
        sc_dlt = _fv((sc_d or {}).get("delta"), 3) if sc_d else "N/A"
        lc_bid = _fv((lc_d or {}).get("bid")) if lc_d else "N/A"
        lc_ask = _fv((lc_d or {}).get("ask")) if lc_d else "N/A"
        lc_mid = _fv((lc_d or {}).get("mid")) if lc_d else "N/A"
        sym_sc = (sc_d or {}).get("symbol", _fv(sc_strike, 0))
        sym_lc = (lc_d or {}).get("symbol", _fv(lc_strike, 0))

        bc_credit = strat.get("credit") if strat_name == "Bear Call Spread" else None
        wing_c    = abs((lc_strike or 0) - (sc_strike or 0)) or 1
        cw_ratio_c = round((bc_credit or 0) / wing_c * 100, 1) if bc_credit and wing_c else None

        print(f"  Bear Call:")
        print(f"    short_call sym={sym_sc}  strike={_fv(sc_strike,0)}")
        print(f"               bid={sc_bid}  ask={sc_ask}  mid={sc_mid}  delta={sc_dlt}")
        print(f"    long_call  sym={sym_lc}  strike={_fv(lc_strike,0)}")
        print(f"               bid={lc_bid}  ask={lc_ask}  mid={lc_mid}")
        print(f"    credit (short_bid - long_ask) = {_fv(bc_credit)}")
        print(f"    credit/width ratio = {_fv(cw_ratio_c, 1)}%  (wing={_fv(wing_c,0)})")
    else:
        print("  Bear Call: N/A (no short_call in strategy result)")

    # Iron Condor (يظهر الجانبين)
    if strat_name == "Iron Condor":
        ic_credit = strat.get("credit")
        put_wing_dbg = abs((sp_strike or 0) - (lp_strike or 0)) if sp_strike and lp_strike else None
        call_wing_dbg = abs((lc_strike or 0) - (sc_strike or 0)) if sc_strike and lc_strike else None
        wing_candidates = [w for w in (put_wing_dbg, call_wing_dbg, strat.get("width")) if w]
        wing_ic = max(wing_candidates) if wing_candidates else 1
        cw_ic = round((ic_credit or 0) / (wing_ic or 1) * 100, 1) if ic_credit else None
        print(f"  Iron Condor credit = {_fv(ic_credit)}  width={_fv(wing_ic,0)}  cw_ratio={_fv(cw_ic, 1)}%")

    print(sep2)

    # ── 6. فحوصات القبول / الرفض ─────────────────────────────────────────────
    print("  ── 6. DECISION FILTERS ──")
    warnings = strat.get("warnings", [])
    reasons  = strat.get("reasons",  [])
    no_trade = strat.get("no_trade", True)
    score    = strat.get("score",    0)
    decision = strat.get("decision", "?")

    # Delta check for short put
    sp_delta_abs = abs(float((sp_d or {}).get("delta") or 0))
    if sp_d and sp_d.get("delta") is not None:
        sp_delta_pass = "PASS ✅" if 0.05 <= sp_delta_abs <= 0.20 else "FAIL ❌"
    else:
        sp_delta_pass = "N/A (no delta)"
    sc_delta_abs = abs(float((sc_d or {}).get("delta") or 0))
    if sc_d and sc_d.get("delta") is not None:
        sc_delta_pass = "PASS ✅" if 0.05 <= sc_delta_abs <= 0.20 else "FAIL ❌"
    else:
        sc_delta_pass = "N/A (no delta)"

    print(f"  allowed_delta_range  = 0.05 – 0.20")
    print(f"  short_put_delta_check  = {sp_delta_pass}  (|delta|={_fv(sp_delta_abs, 3)})")
    print(f"  short_call_delta_check = {sc_delta_pass}  (|delta|={_fv(sc_delta_abs, 3)})")

    credit_val = strat.get("credit")
    credit_check = ("PASS ✅" if credit_val and credit_val > 0 else "FAIL ❌") if credit_val is not None else "N/A"
    print(f"  credit_check         = {credit_check}  (credit={_fv(credit_val)})")

    liq_warns  = [w for w in warnings if any(kw in w for kw in ["سيولة", "bid/ask", "spread", "Spread"])]
    try:
        liq_status = _liquidity_status_from_strat(strat)
    except Exception:
        liq_status = "FAIL" if liq_warns else "PASS"
    liq_failed = (liq_status == "FAIL") or bool(liq_warns)
    if liq_failed:
        liq_check = "FAIL ❌"
    elif liq_status == "WARN":
        liq_check = "WARN ⚠️"
    else:
        liq_check = "PASS ✅"
    print(f"  liquidity_check      = {liq_check}")

    effective_no_trade = bool(no_trade or liq_failed)
    if liq_failed:
        decision_label = "REJECTED_BY_LIQUIDITY — no paper registration"
    else:
        decision_label = decision
    print(f"  score                = {score}/100")
    print(f"  final_decision       = {'NO TRADE ❌' if effective_no_trade else 'TRADE ✅'}  ({decision_label})")

    if warnings:
        print(f"  rejection_reasons:")
        for w in warnings[:6]:
            print(f"    ⚠️  {w}")
    if reasons:
        print(f"  acceptance_reasons:")
        for r in reasons[:4]:
            print(f"    ✅  {r}")

    print(sep)
    print()


def analyze_symbol(tok: str, symbol: str, now: datetime) -> Dict[str, Any]:
    """
    تحليل Hybrid (0DTE / Swing) لأي رمز (SPX, SPY, QQQ). IWM/DIA Swing-only.
    يُقيّم evaluate_trade_mode لكل رمز ويختار المحرك المناسب.
    """
    symbol_up = symbol.upper()

    # ── جلب 0DTE chain (دائماً لحساب Pin/GEX/Levels) ────────────────────────
    chain = get_options_chain(tok, symbol_up)
    # cache fallback إذا DXLink فارغ
    _sym_opts = (chain.get("calls",[]) + chain.get("puts",[])) if chain else []
    _sym_live  = chain and any(o.get("bid") is not None or o.get("delta") is not None for o in _sym_opts)
    if not _sym_live:
        _cached = _chain_cache_get(symbol_up)
        if _cached:
            _cage = round((time.time() - _CHAIN_CACHE.get(symbol_up,(0,))[0]) / 60, 1)
            print(f"[chain_cache] {symbol_up} DXLink فارغ — cache ({_cage} دق)")
            chain = _cached
    else:
        _chain_cache_set(symbol_up, chain)
    price = _fetch_price_for_symbol(tok, symbol_up)
    if not price:
        price = _infer_spot_from_chain(chain)
        if price:
            _debug_set("price_source", "option_chain_inference")
    if not price:
        return {"error": f"تعذّر جلب سعر {symbol}", "symbol": symbol_up}

    enrich_exposures_with_spot(chain, price)
    levels    = calculate_levels(chain, price, symbol=symbol_up)
    magnetic  = levels.get("magnetic", round(price / 5) * 5)
    pin_score = calculate_pin_score(price, magnetic, chain, levels)

    if pin_score >= 70:
        pin_label, pin_color = "🟢 قوي", "green"
    elif pin_score >= 50:
        pin_label, pin_color = "🟡 متوسط", "orange"
    else:
        pin_label, pin_color = "🔴 ضعيف", "red"

    all_opts    = chain.get("calls", []) + chain.get("puts", []) if chain else []
    has_real_oi = any((o.get("oi") or 0) > 0 for o in all_opts)
    has_volume  = any((o.get("volume") or 0) > 0 for o in all_opts)
    gex_quality = "real_oi" if has_real_oi else ("volume_proxy" if has_volume else "gamma_proxy")
    dq = {
        "has_delta":   bool(any(o.get("delta") is not None for o in all_opts)),
        "has_gamma":   bool(any(o.get("gamma") is not None for o in all_opts)),
        "has_prices":  bool(any(o.get("mid")   is not None for o in all_opts)),
        "gex_quality": gex_quality,
    }

    # ── اختيار Mode: 0DTE أم Swing ───────────────────────────────────────────
    trade_mode_eval = evaluate_trade_mode(levels, pin_score, price)
    selected_mode   = trade_mode_eval.get("selected_mode", "0DTE")
    strategy_result = None

    try:
        from core.strategy_engine import run_strategy_engine, run_swing_engine
        is_open = is_us_market_open()

        if selected_mode == "Swing":
            # v3.18: مسار Swing موحّد فقط عبر analyze_swing().
            # لا يوجد أي استخدام لمسار Swing القديم هنا.
            strategy_result = analyze_swing(tok, symbol_up, now)
            if strategy_result:
                strategy_result.setdefault("trade_mode", "Swing")
                strategy_result.setdefault("swing_path", "unified_analyze_swing")
                strategy_result.setdefault("reasons", [])
                strategy_result["reasons"].append("Swing path used: unified_analyze_swing")
            if not strategy_result:
                strategy_result = {
                    "no_trade":   True,
                    "qualified":  False,
                    "strategy":   "No Trade",
                    "score":      0,
                    "trade_mode": "Swing",
                    "swing_path": "unified_analyze_swing",
                    "reasons":    ["Swing Engine لم يُعد نتيجة"],
                    "warnings":   [],
                }

        else:
            # 0DTE
            strategy_result = run_strategy_engine(
                price          = price,
                pin_score      = pin_score,
                net_gex        = levels.get("net_gex") or 0,
                iv_rank        = levels.get("iv_rank") or 0,
                expected_move  = levels.get("expected_move") or abs(price * 0.004),
                zero_gamma     = levels.get("zero_gamma"),
                ema20          = levels.get("ema20"),
                ema50          = levels.get("ema50"),
                chain_data     = chain,
                levels         = levels,
                data_quality   = dq,
                vix            = levels.get("vix") or 0,
                is_market_open = is_open,
                symbol         = symbol_up,
            )

        # أضف mode metadata للنتيجة
        if strategy_result:
            from core.trade_monitor import _swing_expiry
            if selected_mode == "Swing":
                expiry_date = strategy_result.get("expiry_date") or ""
                dte_entry   = strategy_result.get("dte") or strategy_result.get("dte_at_entry") or 0
                if not expiry_date and dte_entry:
                    expiry_date = _swing_expiry(dte_entry)
            else:
                dte_entry   = 0
                expiry_date = now.strftime("%Y-%m-%d")

            strategy_result["trade_mode"]       = selected_mode
            strategy_result["dte_at_entry"]     = dte_entry
            strategy_result["expiry_date"]      = expiry_date
            strategy_result["mode_score_0dte"]  = trade_mode_eval.get("score_0dte", 0)
            strategy_result["mode_score_swing"] = trade_mode_eval.get("score_swing", 0)

        # ── SMC Lite MTF: HTF direction + LTF confirmation + Premium/Discount + Sweep ──
        smc_context = {"available": False, "bias": "neutral", "smc_score": 0}
        try:
            from core.smc_engine import analyze_smc_lite, apply_smc_to_strategy
            smc_context = analyze_smc_lite(symbol_up, selected_mode, price)
            if strategy_result:
                strategy_result = apply_smc_to_strategy(strategy_result, smc_context)
            _ys = smc_context.get("yahoo_status") or {}
            _ys_state = _ys.get("state", "UNKNOWN")
            _ys_ok = _ys.get("ok")
            _ys_age = _ys.get("last_success_age_seconds")
            _ys_txt = f"Yahoo={'OK' if _ys_ok else 'FAILED'}:{_ys_state}" if _ys_ok is not None else "Yahoo=UNKNOWN"
            if isinstance(_ys_age, (int, float)):
                _ys_txt += f" age={_ys_age:.0f}s"
            print(
                f"[smc_mtf] {symbol_up} | mode={selected_mode} | "
                f"bias={smc_context.get('bias')} | "
                f"structure={smc_context.get('last_structure')} | "
                f"zone={smc_context.get('zone')} | "
                f"sweep={smc_context.get('recent_sweep')} | "
                f"adj={smc_context.get('smc_score',0)} | {_ys_txt} | "
                f"source={smc_context.get('source','yahoo')}"
            )
        except Exception as _smc_err:
            smc_context = {"available": False, "bias": "neutral", "smc_score": 0, "error": str(_smc_err)}
            print(f"[smc_mtf error] {symbol_up}: {_smc_err}")

        # ── Order Block / Demand-Supply MTF: non-blocking score adjustment only ──
        demand_supply_context = {"available": False, "score": 0}
        try:
            from core.demand_supply import analyze_demand_supply, apply_demand_supply_to_strategy
            demand_supply_context = analyze_demand_supply(symbol_up, selected_mode, price)
            _ds_src = str((demand_supply_context or {}).get("source") or "").lower()
            # RC15i-1: Yahoo/Fallback D/S remains diagnostics only.
            # D/S may change score only when the source is clean DXLink, not Yahoo
            # and not an insufficient/error/fallback state. Tastytrade remains the
            # options-chain/Greeks source; this guard only protects technical D/S scoring.
            _ds_can_apply_score = (
                "dxlink" in _ds_src
                and "yahoo" not in _ds_src
                and "fallback" not in _ds_src
                and "insufficient" not in _ds_src
                and "failed" not in _ds_src
                and "error" not in _ds_src
            )
            if strategy_result and _ds_can_apply_score:
                strategy_result = apply_demand_supply_to_strategy(strategy_result, demand_supply_context, price)
                try:
                    strategy_result["demand_supply_score_applied"] = True
                    strategy_result["demand_supply_diagnostic_only"] = False
                    strategy_result["demand_supply_source"] = _ds_src
                except Exception:
                    pass
            else:
                if strategy_result is not None:
                    try:
                        strategy_result["demand_supply_score_applied"] = False
                        strategy_result["demand_supply_diagnostic_only"] = True
                        strategy_result["demand_supply_source"] = _ds_src
                        if "yahoo" in _ds_src:
                            strategy_result["demand_supply_yahoo_diagnostic_only"] = True
                    except Exception:
                        pass
            _ds_htf = demand_supply_context.get("htf", {})
            _ds_ltf = demand_supply_context.get("ltf", {})
            _htf_d = _ds_htf.get("nearest_demand") or {}
            _htf_s = _ds_htf.get("nearest_supply") or {}
            _dys = demand_supply_context.get("yahoo_status") or {}
            _dys_state = _dys.get("state", "UNKNOWN")
            _dys_ok = _dys.get("ok")
            _dys_age = _dys.get("last_success_age_seconds")
            _dys_txt = f"Yahoo={'OK' if _dys_ok else 'FAILED'}:{_dys_state}" if _dys_ok is not None else "Yahoo=UNKNOWN"
            if isinstance(_dys_age, (int, float)):
                _dys_txt += f" age={_dys_age:.0f}s"
            print(
                f"[order_block] {symbol_up} | mode={selected_mode} | "
                f"HTF_bullish_OB={_htf_d.get('low')}–{_htf_d.get('high')} status={_htf_d.get('status')} | "
                f"HTF_bearish_OB={_htf_s.get('low')}–{_htf_s.get('high')} status={_htf_s.get('status')} | "
                f"adj={demand_supply_context.get('score',0)} | {_dys_txt} | "
                f"source={demand_supply_context.get('source','yahoo')}"
            )
            # ── RC15j Phase 2A — D/S DXLink Diagnostic Audit ─────────────────
            # يملأ حقول journal بتفاصيل DXLink لـ SPX و غيره — لا block هنا
            if strategy_result is not None:
                try:
                    _ds_src  = demand_supply_context.get("source", "")
                    _htf_ctx = demand_supply_context.get("htf", {})
                    _ltf_ctx = demand_supply_context.get("ltf", {})
                    _htf_nd  = _htf_ctx.get("nearest_demand") or _htf_ctx.get("nearest_bullish_order_block") or {}
                    _htf_ns  = _htf_ctx.get("nearest_supply") or _htf_ctx.get("nearest_bearish_order_block") or {}
                    _ltf_nd  = _ltf_ctx.get("nearest_demand") or _ltf_ctx.get("nearest_bullish_order_block") or {}
                    _ltf_ns  = _ltf_ctx.get("nearest_supply") or _ltf_ctx.get("nearest_bearish_order_block") or {}
                    # Prefer 15m (LTF) for nearest zones; fallback to 1H (HTF)
                    _nd = _ltf_nd if _ltf_nd else _htf_nd
                    _ns = _ltf_ns if _ltf_ns else _htf_ns
                    _atr_15m = _ltf_ctx.get("atr")
                    # near_threshold = max(5, 0.15% price, 0.25 * atr_15m)
                    _near_thr = None
                    if price and price > 0:
                        _t1 = 5.0
                        _t2 = price * 0.0015
                        _t3 = (_atr_15m * 0.25) if _atr_15m else 0
                        _near_thr = round(max(_t1, _t2, _t3), 2)
                    _ds_not_eval_reason = ""
                    if not demand_supply_context.get("available"):
                        _htf_r = _htf_ctx.get("reason", "")
                        _ltf_r = _ltf_ctx.get("reason", "")
                        _ds_not_eval_reason = f"htf={_htf_r} ltf={_ltf_r}" if (_htf_r or _ltf_r) else \
                                              demand_supply_context.get("reason", "unavailable")
                    _ds_decision = "DS_LOCATION_NOT_EVALUATED_PASS" if not demand_supply_context.get("available") else \
                                   "DS_DIAGNOSTIC_ONLY"
                    strategy_result.update({
                        "ds_source":                _ds_src,
                        "ds_symbol_requested":      symbol_up,
                        "ds_symbol_resolved":       f"$SPX.X" if symbol_up == "SPX" else symbol_up,
                        "ds_1h_available":          str(_htf_ctx.get("available", False)),
                        "ds_15m_available":         str(_ltf_ctx.get("available", False)),
                        "ds_1h_candles_count":      _htf_ctx.get("candles"),
                        "ds_15m_candles_count":     _ltf_ctx.get("candles"),
                        "atr_15m":                  _atr_15m,
                        "nearest_demand_low":       _nd.get("low"),
                        "nearest_demand_high":      _nd.get("high"),
                        "distance_to_demand_points": _nd.get("distance"),
                        "nearest_supply_low":       _ns.get("low"),
                        "nearest_supply_high":      _ns.get("high"),
                        "distance_to_supply_points": _ns.get("distance"),
                        "near_threshold":           _near_thr,
                        "ds_not_evaluated_reason":  _ds_not_eval_reason,
                        "ds_location_decision":     _ds_decision,
                    })
                except Exception as _diag_err:
                    print(f"[ds_diag] failed to attach diagnostics {symbol_up}: {_diag_err}")
        except Exception as _ds_err:
            demand_supply_context = {"available": False, "score": 0, "error": str(_ds_err)}
            print(f"[order_block error] {symbol_up}: {_ds_err}")

        # ── RC12l SMC MTF Diagnostics: 1H bias + 15m zones + 5m trigger ──
        # RC12l change: run for SPY/QQQ/IWM regardless of selected_mode.
        # This is diagnostics_only — it never affects strategy selection or score.
        # Previously guarded by selected_mode=="0DTE" which blocked Why? card data
        # whenever the mode evaluator chose Swing (e.g. high pin score days).
        smc_0dte_mtf_context = {"available": False, "diagnostics_only": True, "reason": "not_evaluated"}
        try:
            if symbol_up in SWING_ONLY_SYMBOLS:
                from core.smc_0dte_mtf import analyze_0dte_smc_mtf
                _strategy_name_for_smc = (strategy_result or {}).get("strategy")
                smc_0dte_mtf_context = analyze_0dte_smc_mtf(
                    symbol_up, price, strategy_name=_strategy_name_for_smc, access_token=tok
                )
                if strategy_result is not None:
                    strategy_result["smc_0dte_mtf"]      = smc_0dte_mtf_context
                    strategy_result["smc_0dte_mtf_full"] = smc_0dte_mtf_context
                print(
                    f"[smc_0dte_mtf] {symbol_up} | mode={selected_mode} | "
                    f"dir={smc_0dte_mtf_context.get('direction')} | "
                    f"1H={smc_0dte_mtf_context.get('bias_1h')} | "
                    f"15m={smc_0dte_mtf_context.get('structure_15m')} | "
                    f"5m={smc_0dte_mtf_context.get('trigger_5m')} | "
                    f"sweep={smc_0dte_mtf_context.get('liquidity_sweep_5m')} | "
                    f"verdict={smc_0dte_mtf_context.get('verdict')} | diagnostics_only=True"
                )
            else:
                smc_0dte_mtf_context = {
                    "available": False,
                    "diagnostics_only": True,
                    "enabled": False,
                    "reason": "not_applicable_scope",
                    "scope": "SPY/QQQ/IWM/DIA/AAPL/NVDA/GLD only",
                }
        except Exception as _smc0_err:
            smc_0dte_mtf_context = {
                "available": False,
                "diagnostics_only": True,
                "error": str(_smc0_err),
                "scope": "SPY/QQQ/IWM/DIA/AAPL/NVDA/GLD diagnostics",
            }
            if strategy_result is not None:
                strategy_result["smc_0dte_mtf"] = smc_0dte_mtf_context
            print(f"[smc_0dte_mtf error] {symbol_up}: {_smc0_err}")

        # ── RC13f Swing contexts are produced once inside analyze_swing() ─────────
        # Avoid a second SMC/Demand-Supply fetch and a second score adjustment here.
        swing_ds_1h_context  = (strategy_result or {}).get("swing_ds_1h") or {}
        swing_ds_15m_context = (strategy_result or {}).get("swing_ds_15m") or {}
        if strategy_result is not None:
            _inner_smc = strategy_result.get("smc_0dte_mtf_full") or strategy_result.get("smc_0dte_mtf")
            if isinstance(_inner_smc, dict):
                smc_0dte_mtf_context = _inner_smc

        if (selected_mode == "Swing"
                and symbol_up in SWING_ONLY_SYMBOLS
                and strategy_result
                and not strategy_result.get("no_trade")):
            # ── RC13 Swing Repetition Check ───────────────────────────────────
            # منع: أكثر من صفقة Swing واحدة مفتوحة لنفس الرمز
            # منع: فتح صفقة Swing ثانية بنفس الاتجاه قبل مرور 120 دقيقة
            if strategy_result and not strategy_result.get("no_trade"):
                try:
                    from core.database import get_connection
                    from datetime import timezone
                    _strat_name = strategy_result.get("strategy", "")
                    _direction  = "bearish" if _strat_name in ("Bear Call Spread", "Put Debit Spread") else "bullish"
                    with get_connection() as _conn:
                        # 0. حد إجمالي مستقل لصفقات Swing المفتوحة.
                        from core.database import get_setting as _get_setting_swing
                        try:
                            _max_open_swing = int(_get_setting_swing("max_open_swing_total", "2") or 2)
                        except Exception:
                            _max_open_swing = 2
                        _open_swing_total = _conn.execute(
                            "SELECT COUNT(*) as cnt FROM open_trades "
                            "WHERE trade_mode='Swing' AND status='open'"
                        ).fetchone()["cnt"]
                        if _max_open_swing > 0 and _open_swing_total >= _max_open_swing:
                            strategy_result["no_trade"] = True
                            strategy_result["qualified"] = False
                            strategy_result["decision"] = "REJECTED_SWING_MAX_OPEN"
                            strategy_result["reject_reason"] = (
                                f"Maximum open Swing trades reached ({_open_swing_total}/{_max_open_swing})"
                            )
                            strategy_result.setdefault("reasons", []).append(
                                f"Swing Portfolio Limit: {_open_swing_total}/{_max_open_swing} open"
                            )
                            print(f"[swing_max_open] {symbol_up} BLOCKED — {_open_swing_total}/{_max_open_swing}")

                        # 1. صفقة Swing مفتوحة بنفس الرمز (trade_mode هو الاسم الصحيح للعمود)
                        _open_same = _conn.execute(
                            "SELECT COUNT(*) as cnt FROM open_trades "
                            "WHERE symbol=? AND trade_mode='Swing' AND status='open'",
                            (symbol_up,)
                        ).fetchone()["cnt"]
                        if not strategy_result.get("no_trade") and _open_same >= 1:
                            strategy_result["no_trade"]      = True
                            strategy_result["qualified"]     = False
                            strategy_result["decision"]      = "REJECTED_SWING_EXPOSURE"
                            strategy_result["reject_reason"] = f"Swing already open for {symbol_up} ({_open_same} open)"
                            strategy_result.setdefault("reasons", []).append(
                                f"Swing Exposure Block: {symbol_up} already has {_open_same} Swing trade(s) open"
                            )
                            print(f"[swing_exposure] {symbol_up} BLOCKED — {_open_same} open Swing trades")
                        elif not strategy_result.get("no_trade"):
                            # 2. فتح صفقة بنفس الاتجاه خلال آخر 120 دقيقة
                            _cutoff = (now - timedelta(minutes=120)).strftime("%Y-%m-%d %H:%M:%S")
                            _s1, _s2 = (
                                ("Bear Call Spread", "Put Debit Spread") if _direction == "bearish"
                                else ("Bull Put Spread", "Call Debit Spread")
                            )
                            _recent = _conn.execute(
                                "SELECT COUNT(*) as cnt FROM open_trades "
                                "WHERE symbol=? AND trade_mode='Swing' "
                                "AND entry_time >= ? AND strategy IN (?,?)",
                                (symbol_up, _cutoff, _s1, _s2)
                            ).fetchone()["cnt"]
                            if _recent >= 1:
                                strategy_result["no_trade"]      = True
                                strategy_result["qualified"]     = False
                                strategy_result["decision"]      = "REJECTED_SWING_COOLDOWN"
                                strategy_result["reject_reason"] = f"Swing cooldown: {_direction} trade in last 120min for {symbol_up}"
                                strategy_result.setdefault("reasons", []).append(
                                    f"Swing Cooldown: {symbol_up} {_direction} within 120min"
                                )
                                print(f"[swing_cooldown] {symbol_up} BLOCKED — {_direction} trade within 120min")
                except Exception as _exp_err:
                    print(f"[swing_exposure error] {symbol_up}: {_exp_err}")
            # ── RC13 Cross-Symbol Direction Guard ────────────────────────────
            # إذا كان enable_swing_correlation_guard=true: منع فتح Bear Call على QQQ
            # إذا كان SPY Bear Call مفتوح بالفعل (والعكس صحيح).
            if strategy_result and not strategy_result.get("no_trade"):
                try:
                    from core.database import get_setting as _gs, get_connection as _gc2
                    _guard_on = str(_gs("enable_swing_correlation_guard", "true")).lower() in ("true", "1", "yes")
                    if _guard_on:
                        _strat_name2 = strategy_result.get("strategy", "")
                        _dir2 = "bearish" if _strat_name2 in ("Bear Call Spread", "Put Debit Spread") else "bullish"
                        _b1, _b2 = (
                            ("Bear Call Spread", "Put Debit Spread") if _dir2 == "bearish"
                            else ("Bull Put Spread", "Call Debit Spread")
                        )
                        # RC13f: use explicit correlation peers instead of treating every
                        # Swing symbol as equally correlated.
                        _correlation_peers = {
                            "QQQ": ("AAPL", "NVDA"),
                            "AAPL": ("QQQ", "NVDA"),
                            "NVDA": ("QQQ", "AAPL"),
                            "SPY": ("DIA",),
                            "DIA": ("SPY",),
                            "IWM": (),
                            "GLD": (),
                        }
                        _other_syms = list(_correlation_peers.get(symbol_up, ()))
                        _cross_cnt = 0
                        if _other_syms:
                            with _gc2() as _c2:
                                _cross_cnt = _c2.execute(
                                    "SELECT COUNT(*) as cnt FROM open_trades "
                                    "WHERE symbol IN ({}) AND trade_mode='Swing' "
                                    "AND status='open' AND strategy IN (?,?)".format(
                                        ",".join("?" * len(_other_syms))
                                    ),
                                    (*_other_syms, _b1, _b2),
                                ).fetchone()["cnt"]
                        if _cross_cnt >= 1:
                            strategy_result["no_trade"]      = True
                            strategy_result["qualified"]     = False
                            strategy_result["decision"]      = "REJECTED_SWING_CORRELATION"
                            strategy_result["reject_reason"] = (
                                f"Correlation guard: {_dir2} Swing already open on another symbol"
                            )
                            strategy_result.setdefault("reasons", []).append(
                                f"Swing Correlation Block: {_dir2} already open on {_other_syms}"
                            )
                            print(f"[swing_correlation] {symbol_up} BLOCKED — {_dir2} open on correlated symbol")
                except Exception as _corr_err:
                    print(f"[swing_correlation error] {symbol_up}: {_corr_err}")
            # تحديث smc_0dte_mtf_full + D/S context
            if strategy_result is not None:
                strategy_result["smc_0dte_mtf"]      = smc_0dte_mtf_context
                strategy_result["smc_0dte_mtf_full"] = smc_0dte_mtf_context
                strategy_result["swing_ds_1h"]       = swing_ds_1h_context
                strategy_result["swing_ds_15m"]      = swing_ds_15m_context

        # RC12l — Mirror DXLink SMC into demand_supply for SPY/QQQ/IWM (all modes).
        # RC12e was restricted to selected_mode=="0DTE" which blocked Why? card OB data
        # on Swing days. Since SMC now runs for all modes, mirror always.
        try:
            if symbol_up in SWING_ONLY_SYMBOLS and isinstance(smc_0dte_mtf_context, dict):
                _m15 = smc_0dte_mtf_context.get("m15") or {}
                _h1 = smc_0dte_mtf_context.get("h1") or {}
                _m5 = smc_0dte_mtf_context.get("m5") or {}
                # RC12k: mirror even when available=False (insufficient candles or DXLink error).
                # Previous condition required available=True which silently skipped DXLink data.
                # enabled=True means DXLink was attempted; h1/m15 keys mean candles were fetched.
                if _h1 or _m15 or _m5 or smc_0dte_mtf_context.get("enabled") or smc_0dte_mtf_context.get("h1") is not None:
                    _demand = _m15.get("demand") or []
                    _supply = _m15.get("supply") or []
                    demand_supply_context = {
                        "available": bool(_m15.get("available")),
                        "profile": "0DTE",
                        "source": "dxlink_smc_0dte_mtf",
                        "candle_source_policy": "DXLink/Tastytrade candles only for Swing/0DTE diagnostics",
                        "engine": "RC12e DXLink SMC 15m Order Blocks",
                        "price": price,
                        "score": 0,
                        "reason": "mirrored from smc_0dte_mtf DXLink diagnostics",
                        "htf": {
                            "available": bool(_h1.get("available")),
                            "label": "HTF",
                            "timeframe": "1h",
                            "source": "dxlink_smc_0dte_mtf",
                            "candles": _h1.get("candles", 0),
                            "bias": _h1.get("bias"),
                            "last_structure": _h1.get("last_structure"),
                            "reason": _h1.get("reason", "available" if _h1.get("available") else "unavailable"),
                            "demand": _h1.get("demand") or [],
                            "supply": _h1.get("supply") or [],
                            "active_bullish_order_blocks": _h1.get("demand") or [],
                            "active_bearish_order_blocks": _h1.get("supply") or [],
                            "nearest_demand": _h1.get("nearest_demand"),
                            "nearest_supply": _h1.get("nearest_supply"),
                            "nearest_bullish_order_block": _h1.get("nearest_demand"),
                            "nearest_bearish_order_block": _h1.get("nearest_supply"),
                        },
                        "ltf": {
                            "available": bool(_m15.get("available")),
                            "label": "LTF",
                            "timeframe": "15m",
                            "source": "dxlink_smc_0dte_mtf",
                            "candles": _m15.get("candles", 0),
                            "bias": _m15.get("bias"),
                            "last_structure": _m15.get("last_structure"),
                            "reason": _m15.get("reason", "available" if _m15.get("available") else "unavailable"),
                            "demand": _demand,
                            "supply": _supply,
                            "active_bullish_order_blocks": _demand,
                            "active_bearish_order_blocks": _supply,
                            "nearest_demand": _m15.get("nearest_demand"),
                            "nearest_supply": _m15.get("nearest_supply"),
                            "nearest_bullish_order_block": _m15.get("nearest_demand"),
                            "nearest_bearish_order_block": _m15.get("nearest_supply"),
                        },
                    }
                    if strategy_result is not None:
                        strategy_result["demand_supply"] = demand_supply_context
                        strategy_result["demand_supply_score_adjustment"] = 0
                        strategy_result["order_block_score_adjustment"] = 0
                    print(f"[order_block_dxlink_ui] {symbol_up} | source=dxlink_smc_0dte_mtf | 15m_demand={len(_demand)} | 15m_supply={len(_supply)}")
        except Exception as _ds_dx_ui_err:
            print(f"[order_block_dxlink_ui error] {symbol_up}: {_ds_dx_ui_err}")

        # Diagnostic
        _s_name  = (strategy_result or {}).get("strategy", "?")
        _s_score = (strategy_result or {}).get("score", 0)
        _s_no    = (strategy_result or {}).get("no_trade", True)
        _swing_rej = (strategy_result or {}).get("reasons", []) if selected_mode == "Swing" and _s_no else []
        print(
            f"[mode_diag] {symbol_up} | 0DTE={trade_mode_eval.get('score_0dte',0)}/100"
            f" | Swing={trade_mode_eval.get('score_swing',0)}/100"
            f" | selected={selected_mode} | strategy={_s_name} score={_s_score}"
            f" | no_trade={_s_no}"
            + (f" | swing_reject: {'; '.join(_swing_rej[:2])}" if _swing_rej else "")
        )

    except Exception as _se:
        _debug_error(f"strategy_engine {symbol}: {_se}")

    # ── طباعة جدول 1.5σ التشخيصي ─────────────────────────────────────────────
    try:
        if symbol_up == "SPX":
            _print_sigma_debug_table(
                symbol         = symbol_up,
                price          = price,
                price_source   = _LAST_DEBUG.get("price_source"),
                chain          = chain,
                levels         = levels,
                strategy_result= strategy_result,
            )
    except Exception as _tbl_err:
        print(f"[sigma_debug_table error] {_tbl_err}")

    # ── إرسال جدول 1.5σ عبر Telegram إذا كان Debug مُفعَّلاً ───────────────
    try:
        _sigma_debug_tg = get_setting("sigma_debug_telegram", "0")
        if str(_sigma_debug_tg).strip() in ("1", "true", "yes") and symbol_up == "SPX":
            from core.telegram_bot import send_sigma_debug
            _partial_result = {
                "symbol":   symbol_up,
                "price":    price,
                "levels":   levels,
                "_chain":   chain,
                "strategy": strategy_result,
            }
            send_sigma_debug(_partial_result)
    except Exception as _tg_err:
        print(f"[sigma_debug_telegram error] {_tg_err}")


    # ── M1 Tastytrade Provider Shadow — diagnostics only, no trade impact ─────
    # This compares a lightweight Tastytrade/DXLink quote with the RC15j price.
    # It is disabled by default via TASTY_SHADOW_ENABLED=false and never routes orders.
    tastytrade_shadow: Dict[str, Any] = {
        "tasty_shadow_enabled": False,
        "tasty_shadow_ok": False,
        "tasty_shadow_reason": "not_evaluated",
    }
    try:
        from core.tastytrade_shadow_provider import collect_tastytrade_shadow
        tastytrade_shadow = collect_tastytrade_shadow(
            symbol_up,
            price=price,
            chain=chain,
            strategy=strategy_result if isinstance(strategy_result, dict) else None,
        )
        if isinstance(strategy_result, dict):
            strategy_result.update(tastytrade_shadow)
        _ts_reason = tastytrade_shadow.get("tasty_shadow_reason")
        _ts_mid = tastytrade_shadow.get("tasty_shadow_underlying_mid")
        _ts_diff = tastytrade_shadow.get("tasty_shadow_price_diff_points")
        print(f"[tasty_shadow] {symbol_up} enabled={tastytrade_shadow.get('tasty_shadow_enabled')} ok={tastytrade_shadow.get('tasty_shadow_ok')} mid={_ts_mid} diff={_ts_diff} reason={_ts_reason}")
    except Exception as _tasty_shadow_err:
        tastytrade_shadow = {
            "tasty_shadow_enabled": False,
            "tasty_shadow_ok": False,
            "tasty_shadow_reason": f"shadow_error:{_tasty_shadow_err.__class__.__name__}:{str(_tasty_shadow_err)[:80]}",
        }
        if isinstance(strategy_result, dict):
            strategy_result.update(tastytrade_shadow)
        print(f"[tasty_shadow error] {symbol_up}: {_tasty_shadow_err}")

    dq_report = compute_data_quality_report(chain, price, locals().get("spx_price_source", _LAST_DEBUG.get("price_source")), levels)

    def pct(target: float) -> float:
        return round((target - price) / price * 100, 2)

    targets_up:   List[Dict[str, Any]] = []
    targets_down: List[Dict[str, Any]] = []
    for key, label, strength in [
        ("call_wall_gex", "Call Wall (GEX)", "قوي"),
        ("call_wall_oi",  "Call Wall (OI)",  "مؤكد"),
        ("em_upper",      "EM Upper",         "إحصائي"),
    ]:
        level = levels.get(key)
        if level and level > price and all(abs(level - x["price"]) > 0.001 for x in targets_up):
            targets_up.append({"price": level, "pct": pct(level), "labels": [label], "strength": strength})
    for key, label, strength in [
        ("put_wall_gex", "Put Wall (GEX)", "قوي"),
        ("put_wall_oi",  "Put Wall (OI)",  "مؤكد"),
        ("em_lower",     "EM Lower",        "إحصائي"),
    ]:
        level = levels.get(key)
        if level and level < price and all(abs(level - x["price"]) > 0.001 for x in targets_down):
            targets_down.append({"price": level, "pct": pct(level), "labels": [label], "strength": strength})

    return {
        "symbol":           symbol_up,
        "timestamp":        now.strftime("%Y-%m-%d %H:%M"),
        "price":            price,
        "magnetic":         magnetic,
        "pin_score":        pin_score,
        "pin_label":        pin_label,
        "pin_color":        pin_color,
        "is_market_open":   is_us_market_open(),
        "targets_up":       sorted(targets_up,   key=lambda x: x["price"]),
        "targets_down":     sorted(targets_down, key=lambda x: x["price"], reverse=True),
        "levels":           levels,
        "expected_move":    levels.get("expected_move"),
        "net_gex":          levels.get("net_gex"),
        "gross_gex":        levels.get("gross_gex"),
        "zero_gamma":       levels.get("zero_gamma"),
        "vix":              levels.get("vix"),
        "iv_rank":          levels.get("iv_rank"),
        "trend":            levels.get("trend"),
        "trend_label":      trend_label(levels.get("trend", "neutral")),
        "ema20":            levels.get("ema20"),
        "ema50":            levels.get("ema50"),
        "net_gex_label": (
            f"+{levels.get('net_gex',0):,.0f} (إيجابي)" if (levels.get("net_gex") or 0) > 0
            else f"{levels.get('net_gex',0):,.0f} (سلبي)"
        ),
        "gex_quality":         gex_quality,
        "data_quality_report": dq_report,
        "trade_mode_eval":     trade_mode_eval,
        "selected_trade_mode": selected_mode,
        "top_call_gex":    levels.get("top_call_gex", []),
        "top_put_gex":     levels.get("top_put_gex",  []),
        "_chain":          chain,
        "strategy":            strategy_result,
        "tastytrade_shadow":   tastytrade_shadow,
        "data_quality":        dq,
        "smc":                 smc_context,
        "demand_supply":       demand_supply_context,
        "smc_0dte_mtf":        smc_0dte_mtf_context,
        # RC12l: explicit full snapshot key for Why? card — avoids re-derivation
        "smc_0dte_mtf_full":   smc_0dte_mtf_context,
    }


def evaluate_trade_mode(levels: Dict[str, Any], pin_score: int, price: float) -> Dict[str, Any]:
    """
    يُقيّم أي Mode مناسب للسوق الحالي:
      - 0DTE  : يوم التداول الحالي (Same-Day Expiry)
      - Swing : unified analyze_swing path

    يُعيد:
      selected_mode : "0DTE" أو "Swing"
      score_0dte    : درجة 0DTE (0-100)
      score_swing   : درجة Swing (0-100)
      reasons_0dte  : أسباب اختيار 0DTE
      reasons_swing : أسباب اختيار Swing
    """
    score_0dte  = 0
    score_swing = 0
    reasons_0dte:  list = []
    reasons_swing: list = []

    net_gex    = levels.get("net_gex") or 0
    iv_rank    = levels.get("iv_rank") or 0
    vix        = levels.get("vix") or 0
    em         = levels.get("expected_move") or (price * 0.004)
    trend      = levels.get("combined_trend") or levels.get("trend") or "neutral"
    ema20      = levels.get("ema20")
    ema50      = levels.get("ema50")

    # ── 0DTE Score ────────────────────────────────────────────────────────────
    # Pin Score — أهم عامل لـ 0DTE
    if pin_score >= 70:
        score_0dte += 35
        reasons_0dte.append(f"Pin Score قوي ({pin_score}) — تثبيت قوي")
    elif pin_score >= 50:
        score_0dte += 20
        reasons_0dte.append(f"Pin Score متوسط ({pin_score})")
    else:
        score_0dte -= 10
        reasons_0dte.append(f"Pin Score ضعيف ({pin_score}) — السوق متحرك")

    # GEX إيجابي — صانعو السوق يكبحون الحركة (مثالي لـ 0DTE)
    if net_gex > 500_000_000:
        score_0dte += 25
        reasons_0dte.append("GEX إيجابي قوي — كبح الحركة")
    elif net_gex > 0:
        score_0dte += 10
        reasons_0dte.append("GEX إيجابي — استقرار نسبي")
    else:
        score_0dte -= 15
        reasons_0dte.append("GEX سلبي — خطر تضخيم الحركة")

    # VIX منخفض = بيئة هادئة مناسبة لـ 0DTE
    if vix and vix < 15:
        score_0dte += 20
        reasons_0dte.append(f"VIX منخفض ({vix:.1f}) — بيئة هادئة")
    elif vix and vix < 20:
        score_0dte += 10
        reasons_0dte.append(f"VIX معتدل ({vix:.1f})")
    elif vix and vix > 25:
        score_0dte -= 20
        reasons_0dte.append(f"VIX مرتفع ({vix:.1f}) — خطر على 0DTE")

    # EM صغير نسبياً = حركة يومية ضيقة
    em_pct = em / price * 100 if price else 0
    if em_pct < 0.5:
        score_0dte += 20
        reasons_0dte.append(f"EM ضيق ({em_pct:.2f}%) — مثالي لـ 0DTE")
    elif em_pct < 0.8:
        score_0dte += 10
        reasons_0dte.append(f"EM معتدل ({em_pct:.2f}%)")
    else:
        score_0dte -= 10
        reasons_0dte.append(f"EM واسع ({em_pct:.2f}%) — خطر على 0DTE")

    # ── Swing Score ──────────────────────────────────────────────────────────
    # اتجاه واضح — أهم عامل لـ Swing
    strong_trend = trend in ("strong_bullish", "strong_bearish", "bullish", "bearish")
    if trend in ("strong_bullish", "strong_bearish"):
        score_swing += 35
        reasons_swing.append(f"Trend قوي جداً ({trend}) — مثالي لـ Swing")
    elif trend in ("bullish", "bearish"):
        score_swing += 22
        reasons_swing.append(f"Trend واضح ({trend}) — مناسب لـ Swing")
    elif trend in ("bullish_pullback", "bearish_bounce"):
        score_swing += 12
        reasons_swing.append(f"Trend مع تصحيح ({trend})")
    else:
        score_swing -= 10
        reasons_swing.append("Trend محايد — لا يدعم Swing")

    # IV Rank مناسب للبيع
    if 30 <= iv_rank <= 70:
        score_swing += 25
        reasons_swing.append(f"IV Rank مثالي للبيع ({iv_rank:.0f})")
    elif iv_rank > 70:
        score_swing += 15
        reasons_swing.append(f"IV Rank مرتفع ({iv_rank:.0f}) — Premium غالٍ")
    elif iv_rank < 20:
        score_swing -= 10
        reasons_swing.append(f"IV Rank منخفض ({iv_rank:.0f}) — Premium رخيص")

    # EMA200 — هيكل السوق متعدد الأيام (إذا متاح)
    if ema20 and ema50 and price:
        if price > ema20 > ema50:
            score_swing += 20
            reasons_swing.append("السعر فوق EMA20 وEMA50 — هيكل صاعد")
        elif price < ema20 < ema50:
            score_swing += 20
            reasons_swing.append("السعر تحت EMA20 وEMA50 — هيكل هابط")
        else:
            score_swing += 5
            reasons_swing.append("EMA متشابك — Swing بحذر")

    # VIX مرتفع = IV مرتفع = Swing أفضل (Premium أغلى)
    if vix and vix > 20:
        score_swing += 20
        reasons_swing.append(f"VIX مرتفع ({vix:.1f}) — يفضل Swing على 0DTE")
    elif vix and vix > 15:
        score_swing += 10
        reasons_swing.append(f"VIX معتدل ({vix:.1f}) — Swing مقبول")

    # EM واسع = حركة متوقعة كبيرة = Swing أفضل
    if em_pct >= 0.8:
        score_swing += 15
        reasons_swing.append(f"EM واسع ({em_pct:.2f}%) — يدعم Swing")

    # ── القرار النهائي ────────────────────────────────────────────────────────
    score_0dte  = max(0, min(100, score_0dte))
    score_swing = max(0, min(100, score_swing))

    if score_0dte >= score_swing:
        selected = "0DTE"
    else:
        selected = "Swing"

    return {
        "selected_mode": selected,
        "score_0dte":    score_0dte,
        "score_swing":   score_swing,
        "reasons_0dte":  reasons_0dte,
        "reasons_swing": reasons_swing,
    }


def _full_analysis_impl(
    client_secret: Optional[str] = None,
    refresh_token: Optional[str] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """RC15i.5: progress_cb(msg) يُستدعى من worker thread لتحديث الواجهة."""
    from core.trade_monitor import _set_analysis_running  # RC15i.5
    _set_analysis_running(True)
    def _prog(msg: str) -> None:
        if callable(progress_cb):
            try:
                progress_cb(msg)
            except Exception:
                pass

    now = datetime.now()
    try:
        _prog("● جاري تحميل التوكن...")
        tok = _get_access_token(client_secret, refresh_token, force=True)
    except ValueError as exc:
        _set_analysis_running(False)
        return {"error": str(exc)}

    _prog("● تحميل بيانات SPX...")
    chain = get_options_chain(tok, "SPX")
    # إذا DXLink فشل (لا bid/delta) → استخدم آخر chain ناجح من الـ cache
    _spx_opts = (chain.get("calls",[]) + chain.get("puts",[])) if chain else []
    _chain_live = chain and any(o.get("bid") is not None or o.get("delta") is not None for o in _spx_opts)
    if not _chain_live:
        _cached_chain = _chain_cache_get("SPX")
        if _cached_chain:
            _cache_age = round((time.time() - _CHAIN_CACHE.get("SPX",(0,))[0]) / 60, 1)
            print(f"[chain_cache] DXLink فارغ — استخدام cache ({_cache_age} دق)")
            chain = _cached_chain
        else:
            print("[chain_cache] DXLink فارغ ولا يوجد cache — LIMITED")
    else:
        _chain_cache_set("SPX", chain)
        print(f"[chain_cache] SPX chain محفوظ في cache")

    # 1. أفضل مصدر: DXLink (بيانات لحظية من Tastytrade)
    price = None
    try:
        from core.dxlink_client import fetch_market_data_snapshot, get_api_quote_token
        spx_symbols = [".SPX", "SPX", "$SPX.X"]
        snap = fetch_market_data_snapshot(tok, spx_symbols, timeout_seconds=6.0, max_symbols=5)
        for sym in spx_symbols:
            entry = snap.get(sym, {})
            bid = entry.get("bid")
            ask = entry.get("ask")
            last = entry.get("last")
            prev = entry.get("prev-close")
            if bid and ask and bid > 0 and ask > 0:
                price = round((bid + ask) / 2, 2)
                _debug_set("price_source", f"dxlink:bid_ask:{sym}")
                _debug_set("spx_price", "ok")
                break
            elif last and last > 1000:
                price = last
                _debug_set("price_source", f"dxlink:last:{sym}")
                _debug_set("spx_price", "ok")
                break
            elif prev and prev > 1000:
                price = prev
                _debug_set("price_source", f"dxlink:prev_close:{sym}")
                _debug_set("spx_price", "ok")
                break
    except Exception as exc:
        _debug_error(f"dxlink SPX price: {exc}")

    # 2. Fallback: Yahoo Finance (يعطي آخر سعر إغلاق حتى في العطلة)
    if not price:
        price = get_spx_price_yahoo()

    # 3. Fallback: market-metrics API
    if not price:
        price = get_spx_price(tok)

    # 4. Fallback: option chain inference (أقل دقة — آخر خيار)
    if not price:
        price = _infer_spot_from_chain(chain)
        if price:
            _debug_set("price_source", "option_chain_inference")
            _debug_set("spx_price", "ok")

    if not price:
        _debug_error("full_analysis stopped: no SPX price from all sources")
        _set_analysis_running(False)
        return {"error": "تعذّر جلب سعر SPX من Tastytrade أو Option Chain أو Yahoo fallback."}

    # Freeze the SPX price source now. _LAST_DEBUG is global and can later be
    # overwritten by SPY/QQQ/IWM diagnostics, which caused SPX tables to show
    # labels such as dxlink:bid_ask:QQQ while the numeric price was SPX.
    spx_price_source = _LAST_DEBUG.get("price_source") or "unknown"

    enrich_exposures_with_spot(chain, price)
    levels = calculate_levels(chain, price, symbol="SPX")
    magnetic = levels.get("magnetic", round(price / 50) * 50)
    pin_score = calculate_pin_score(price, magnetic, chain, levels)

    # Pin Score Cache: احفظ آخر قيمة صحيحة عند توفر chain حقيقي
    # وارجع إليها عند فشل DXLink بدلاً من الـ fallback المتذبذب
    dq_check = compute_data_quality_report(chain, price, locals().get("spx_price_source", _LAST_DEBUG.get("price_source")), levels)
    if dq_check.get("mode") in ("LIVE", "PARTIAL") and dq_check.get("checks", {}).get("gamma"):
        _PIN_SCORE_CACHE["last_valid"] = pin_score
        _PIN_SCORE_CACHE["last_magnetic"] = magnetic
        _PIN_SCORE_CACHE["timestamp"] = datetime.now().strftime("%H:%M")
    elif not dq_check.get("checks", {}).get("gamma") and _PIN_SCORE_CACHE.get("last_valid"):
        # استخدم الـ cache — لكن عدّل بحسب المسافة الجديدة من المغناطيس
        cached_pin = _PIN_SCORE_CACHE["last_valid"]
        cached_mag = _PIN_SCORE_CACHE.get("last_magnetic", magnetic)
        dist_change = abs(price - cached_mag) - abs(price - magnetic)
        pin_score = max(0, min(100, cached_pin + round(dist_change * 0.5)))
        _debug_set("pin_score_source", f"cached ({_PIN_SCORE_CACHE['timestamp']})")
    condor = find_iron_condor(chain, price, levels)
    trade_filter = _decision_filter(pin_score, levels, condor)

    # ── تقييم Mode (0DTE vs Swing) ───────────────────────────────────────────
    trade_mode_eval = evaluate_trade_mode(levels, pin_score, price)

    if pin_score >= 70:
        pin_label, pin_color = "🟢 قوي — نطاق التثبيت مقبول", "green"
    elif pin_score >= 50:
        pin_label, pin_color = "🟡 متوسط — تحقق من الاختراقات", "orange"
    else:
        pin_label, pin_color = "🔴 ضعيف — لا تركّب ميكانيكياً", "red"

    call_wall = levels.get("call_wall_gex") or levels.get("call_wall_oi") or levels.get("call_wall_vol") or levels.get("em_upper")
    put_wall = levels.get("put_wall_gex") or levels.get("put_wall_oi") or levels.get("put_wall_vol") or levels.get("em_lower")
    em_upper = levels.get("em_upper", price * 1.0045)
    em_lower = levels.get("em_lower", price * 0.9955)

    def pct(target: float) -> float:
        return round((target - price) / price * 100, 2)

    targets_up: List[Dict[str, Any]] = []
    targets_down: List[Dict[str, Any]] = []

    for key, label, strength in [
        ("call_wall_gex", "Call Wall (GEX تقديري)", "قوي"),
        ("call_wall_oi", "Call Wall (OI)", "مؤكد"),
        ("call_wall_vol", "Call Wall (Volume)", "متغير"),
        ("em_upper", "EM Upper", "إحصائي"),
    ]:
        level = levels.get(key)
        if level and level > price and all(abs(level - x["price"]) > 0.001 for x in targets_up):
            targets_up.append({"price": level, "pct": pct(level), "labels": [label], "strength": strength})

    for key, label, strength in [
        ("put_wall_gex", "Put Wall (GEX تقديري)", "قوي"),
        ("put_wall_oi", "Put Wall (OI)", "مؤكد"),
        ("put_wall_vol", "Put Wall (Volume)", "متغير"),
        ("em_lower", "EM Lower", "إحصائي"),
    ]:
        level = levels.get(key)
        if level and level < price and all(abs(level - x["price"]) > 0.001 for x in targets_down):
            targets_down.append({"price": level, "pct": pct(level), "labels": [label], "strength": strength})

    # ── Strategy Engine — Hybrid (0DTE / Swing) ──────────────────────────────
    _prog("● تحليل SPX...")
    strategy_result = None
    try:
        from core.strategy_engine import run_strategy_engine, run_swing_engine
        all_opts_dq = chain.get("calls",[]) + chain.get("puts",[]) if chain else []
        has_real_oi = any((o.get("oi") or 0) > 0 for o in all_opts_dq)
        has_volume  = any((o.get("volume") or 0) > 0 for o in all_opts_dq)
        gex_quality_dq = "real_oi" if has_real_oi else ("volume_proxy" if has_volume else "gamma_proxy")
        dq = {
            "has_delta":   bool(any(o.get("delta") is not None for o in all_opts_dq)),
            "has_gamma":   bool(any(o.get("gamma") is not None for o in all_opts_dq)),
            "has_prices":  bool(any(o.get("mid")   is not None for o in all_opts_dq)),
            "gex_quality": gex_quality_dq,
        }
        is_open = is_us_market_open()

        # v3.18: full_analysis خاص بـ SPX، و SPX = 0DTE فقط.
        # تم تعطيل مسار Swing القديم نهائياً هنا.
        selected_mode = "0DTE"

        if selected_mode == "Swing":
            # v3.18: disabled legacy SPX Swing branch.
            # full_analysis is SPX-only and SPX is restricted to 0DTE.
            strategy_result = {
                "no_trade":   True,
                "strategy":   "No Trade",
                "score":      0,
                "trade_mode": "Swing",
                "reasons":    ["SPX Swing disabled; SPX uses 0DTE only"],
                "warnings":   [],
                "swing_path": "disabled_spx_full_analysis",
            }

        else:
            # ── 0DTE Mode ───────────────────────────────────────────────────
            # يعتمد على: Pin Score + GEX + Gamma Walls + Same-Day EM
            strategy_result = run_strategy_engine(
                price          = price,
                pin_score      = pin_score,
                net_gex        = levels.get("net_gex") or 0,
                iv_rank        = levels.get("iv_rank") or 0,
                expected_move  = levels.get("expected_move") or abs(price * 0.004),
                zero_gamma     = levels.get("zero_gamma"),
                ema20          = levels.get("ema20"),
                ema50          = levels.get("ema50"),
                chain_data     = chain,
                levels         = levels,
                data_quality   = dq,
                vix            = levels.get("vix") or 0,
                is_market_open = is_open,
                symbol         = "SPX",
            )

        # ── Diagnostic: Mode Selection + Swing Rejection ─────────────────────
        _score_0dte  = trade_mode_eval.get("score_0dte",  0)
        _score_swing = trade_mode_eval.get("score_swing", 0)
        _strat_name  = (strategy_result or {}).get("strategy", "?")
        _strat_score = (strategy_result or {}).get("score", 0)
        _strat_no    = (strategy_result or {}).get("no_trade", True)
        _swing_reasons = (strategy_result or {}).get("reasons", []) if selected_mode == "Swing" and _strat_no else []
        print(
            f"[mode_diag] SPX | 0DTE={_score_0dte}/100 | Swing={_score_swing}/100 "
            f"| selected={selected_mode} | strategy={_strat_name} score={_strat_score} "
            f"| no_trade={_strat_no}"
            + (f" | swing_reject: {'; '.join(_swing_reasons[:3])}" if _swing_reasons else "")
        )

        # أضف trade_mode + dte + expiry + أسباب اختيار الـ Mode لكل نتيجة
        if strategy_result:
            from core.trade_monitor import _swing_expiry
            # لـ Swing: استخدم expiry الحقيقي من swing_chain إذا وُجد
            if selected_mode == "Swing" and swing_chain and swing_chain.get("expiry"):
                expiry_date = swing_chain["expiry"]
                dte_entry   = (
                    datetime.strptime(expiry_date, "%Y-%m-%d").date() - date.today()
                ).days
            elif selected_mode == "Swing":
                dte_entry   = strategy_result.get("target_dte", 0)
                expiry_date = _swing_expiry(dte_entry)
            else:
                dte_entry   = 0
                expiry_date = now.strftime("%Y-%m-%d")

            strategy_result["trade_mode"]      = selected_mode
            strategy_result["dte_at_entry"]    = dte_entry
            strategy_result["expiry_date"]     = expiry_date
            strategy_result["mode_score_0dte"] = trade_mode_eval.get("score_0dte", 0)
            strategy_result["mode_score_swing"]= trade_mode_eval.get("score_swing", 0)
            strategy_result["mode_reasons"]    = (
                trade_mode_eval.get("reasons_swing", [])
                if selected_mode == "Swing"
                else trade_mode_eval.get("reasons_0dte", [])
            )

    except Exception as _se:
        _debug_error(f"strategy_engine: {_se}")
        strategy_result = {
            "no_trade":   True,
            "strategy":   "No Trade",
            "score":      0,
            "trade_mode": "0DTE",
            "reasons":    [f"خطأ في Strategy Engine: {_se}"],
            "warnings":   [],
        }

    # ── RC15j Phase 2B — SPX D/S via SPY DXLink Proxy ───────────────────────────
    # DXLink لا يوفر OHLC لـ SPX ($SPX.X) — نستخدم SPY كـ proxy مع تحويل النسبة
    # هذا فلتر حماية فقط (hard block). لا يُفتح دخول بناءً عليه.
    demand_supply_context = {"available": False, "score": 0}
    try:
        from core.demand_supply import analyze_demand_supply_spy_proxy

        # نحتاج سعر SPY اللحظي للنسبة — نجلبه من cache أو DXLink
        _spy_price_proxy = None
        try:
            _spy_analysis = spy_analysis  # محسوب مسبقاً في full_analysis
            _spy_price_proxy = _spy_analysis.get("price") if isinstance(_spy_analysis, dict) else None
        except Exception:
            pass
        if not _spy_price_proxy or _spy_price_proxy <= 0:
            try:
                from core.dxlink_client import fetch_market_data_snapshot
                _spy_snap = fetch_market_data_snapshot(tok, ["SPY"], timeout_seconds=5.0, max_symbols=2)
                _spy_e = _spy_snap.get("SPY", {})
                _spy_price_proxy = (
                    round((_spy_e["bid"] + _spy_e["ask"]) / 2, 2)
                    if _spy_e.get("bid") and _spy_e.get("ask")
                    else _spy_e.get("last")
                )
            except Exception:
                pass

        if _spy_price_proxy and price and _spy_price_proxy > 0:
            demand_supply_context = analyze_demand_supply_spy_proxy(price, _spy_price_proxy)
        else:
            demand_supply_context = {
                "available": False, "score": 0,
                "source": "dxlink_spy_proxy_failed",
                "proxy_source_symbol": "SPY", "proxy_target_symbol": "SPX",
                "proxy_reason": f"SPY_PRICE_UNAVAILABLE spy={_spy_price_proxy}",
            }

        _ds_src_fa  = str(demand_supply_context.get("source") or "").lower()
        _ds_avail   = demand_supply_context.get("available", False)
        _ds_htf_fa  = demand_supply_context.get("htf", {})
        _ds_ltf_fa  = demand_supply_context.get("ltf", {})
        _htf_d_fa   = _ds_htf_fa.get("nearest_demand") or _ds_htf_fa.get("nearest_bullish_order_block") or {}
        _htf_s_fa   = _ds_htf_fa.get("nearest_supply") or _ds_htf_fa.get("nearest_bearish_order_block") or {}
        _ltf_d_fa   = _ds_ltf_fa.get("nearest_demand") or _ds_ltf_fa.get("nearest_bullish_order_block") or {}
        _ltf_s_fa   = _ds_ltf_fa.get("nearest_supply") or _ds_ltf_fa.get("nearest_bearish_order_block") or {}
        _nd_fa = _ltf_d_fa if _ltf_d_fa else _htf_d_fa
        _ns_fa = _ltf_s_fa if _ltf_s_fa else _htf_s_fa
        _atr_15m_fa = _ds_ltf_fa.get("atr")
        _near_thr_fa = None
        if price and price > 0:
            _near_thr_fa = round(max(5.0, price * 0.0015, (_atr_15m_fa * 0.25) if _atr_15m_fa else 0), 2)
        _bounce_thr_fa = None
        if price and price > 0:
            _bounce_thr_fa = round(max(5.0, price * 0.0015, (_atr_15m_fa * 0.50) if _atr_15m_fa else 0), 2)

        _ds_not_eval_reason_fa = ""
        if not _ds_avail:
            _ds_not_eval_reason_fa = demand_supply_context.get("proxy_reason") or \
                                     demand_supply_context.get("reason", "unavailable")
        _ds_decision_fa = "DS_LOCATION_NOT_EVALUATED_PASS" if not _ds_avail else "DS_PROXY_ACTIVE"

        print(
            f"[ds_proxy_spx] SPX | source={_ds_src_fa} | ratio={demand_supply_context.get('proxy_ratio','?')} | "
            f"1h_avail={_ds_htf_fa.get('available',False)} | 15m_avail={_ds_ltf_fa.get('available',False)} | "
            f"demand={_nd_fa.get('low')}–{_nd_fa.get('high')} | supply={_ns_fa.get('low')}–{_ns_fa.get('high')} | "
            f"atr15m={_atr_15m_fa} | near_thr={_near_thr_fa}"
        )

        if strategy_result is not None:
            strategy_result.update({
                "ds_source":                 _ds_src_fa,
                "ds_symbol_requested":       "SPX",
                "ds_symbol_resolved":        "SPY",
                "ds_proxy_source_symbol":    demand_supply_context.get("proxy_source_symbol", "SPY"),
                "ds_proxy_target_symbol":    demand_supply_context.get("proxy_target_symbol", "SPX"),
                "ds_proxy_ratio":            demand_supply_context.get("proxy_ratio"),
                "ds_proxy_reason":           demand_supply_context.get("proxy_reason", ""),
                "ds_1h_available":           str(_ds_htf_fa.get("available", False)),
                "ds_15m_available":          str(_ds_ltf_fa.get("available", False)),
                "ds_1h_candles_count":       _ds_htf_fa.get("candles"),
                "ds_15m_candles_count":      _ds_ltf_fa.get("candles"),
                "atr_15m":                   _atr_15m_fa,
                "nearest_demand_low":        _nd_fa.get("low"),
                "nearest_demand_high":       _nd_fa.get("high"),
                "distance_to_demand_points": _nd_fa.get("distance"),
                "nearest_supply_low":        _ns_fa.get("low"),
                "nearest_supply_high":       _ns_fa.get("high"),
                "distance_to_supply_points": _ns_fa.get("distance"),
                "near_threshold":            _near_thr_fa,
                "ds_not_evaluated_reason":   _ds_not_eval_reason_fa,
                "ds_location_decision":      _ds_decision_fa,
                # SPY raw zones (قبل التحويل) — للتدقيق
                "spy_nearest_demand_low":    demand_supply_context.get("spy_nearest_demand_low"),
                "spy_nearest_demand_high":   demand_supply_context.get("spy_nearest_demand_high"),
                "spy_nearest_supply_low":    demand_supply_context.get("spy_nearest_supply_low"),
                "spy_nearest_supply_high":   demand_supply_context.get("spy_nearest_supply_high"),
                # Pass proxy context to strategy_engine for hard block evaluation
                "_ds_proxy_context":         demand_supply_context,
                "_ds_near_threshold":        _near_thr_fa,
                "_ds_bounce_threshold":      _bounce_thr_fa,
            })

    except Exception as _ds_fa_err:
        demand_supply_context = {"available": False, "score": 0, "error": str(_ds_fa_err)}
        print(f"[ds_proxy_spx error] {_ds_fa_err}")

    # ── RC15j Phase 2B.1 — Structural Zone Shadow (SPY 1H + 15m) ────────────
    # Shadow mode: logs structural BOS zones to journal, no trade decisions changed.
    try:
        from core.demand_supply import detect_structural_zones_shadow
        _struct_shadow = detect_structural_zones_shadow(
            symbol="SPY",
            spx_price=price,                  # SPX last price
            spy_price=_spy_price_proxy,       # SPY last price (from Phase 2B fetch)
        )
        print(
            f"[struct_shadow] 1h_demand={_struct_shadow.get('struct_1h_demand_found')} "
            f"1h_supply={_struct_shadow.get('struct_1h_supply_found')} "
            f"15m_demand={_struct_shadow.get('struct_15m_demand_found')} "
            f"15m_supply={_struct_shadow.get('struct_15m_supply_found')}"
        )
        if strategy_result is not None:
            strategy_result.update(_struct_shadow)
    except Exception as _ss_err:
        print(f"[struct_shadow error] {_ss_err}")
        if strategy_result is not None:
            strategy_result["struct_shadow_available"] = False
            strategy_result["struct_shadow_error"]     = str(_ss_err)[:120]

    # ── M1 Tastytrade Provider Shadow — SPX full_analysis path ───────────────
    # Diagnostics only. This must run in full_analysis as well as analyze_symbol,
    # otherwise SPX journal rows keep tasty_shadow_* columns blank.
    tastytrade_shadow: Dict[str, Any] = {
        "tasty_shadow_enabled": False,
        "tasty_shadow_ok": False,
        "tasty_shadow_reason": "not_evaluated",
    }
    try:
        from core.tastytrade_shadow_provider import collect_tastytrade_shadow
        tastytrade_shadow = collect_tastytrade_shadow(
            "SPX",
            price=price,
            chain=chain,
            strategy=strategy_result if isinstance(strategy_result, dict) else None,
        )
        if isinstance(strategy_result, dict):
            strategy_result.update(tastytrade_shadow)
        _ts_reason = tastytrade_shadow.get("tasty_shadow_reason")
        _ts_mid = tastytrade_shadow.get("tasty_shadow_underlying_mid")
        _ts_diff = tastytrade_shadow.get("tasty_shadow_price_diff_points")
        print(
            f"[tasty_shadow] SPX enabled={tastytrade_shadow.get('tasty_shadow_enabled')} "
            f"ok={tastytrade_shadow.get('tasty_shadow_ok')} mid={_ts_mid} "
            f"diff={_ts_diff} reason={_ts_reason}"
        )
    except Exception as _tasty_shadow_err:
        tastytrade_shadow = {
            "tasty_shadow_enabled": False,
            "tasty_shadow_ok": False,
            "tasty_shadow_reason": f"shadow_error:{_tasty_shadow_err.__class__.__name__}:{str(_tasty_shadow_err)[:80]}",
        }
        if isinstance(strategy_result, dict):
            strategy_result.update(tastytrade_shadow)
        print(f"[tasty_shadow error] SPX: {_tasty_shadow_err}")

    # ── طباعة جدول 1.5σ التشخيصي (full_analysis path) ───────────────────────
    try:
        _print_sigma_debug_table(
            symbol         = "SPX",
            price          = price,
            price_source   = locals().get("spx_price_source", _LAST_DEBUG.get("price_source")),
            chain          = chain,
            levels         = levels,
            strategy_result= strategy_result,
        )
    except Exception as _tbl_err:
        print(f"[sigma_debug_table error] {_tbl_err}")

    # ── إرسال جدول 1.5σ عبر Telegram إذا كان Debug مُفعَّلاً ─────────────────
    try:
        _sigma_debug_tg = get_setting("sigma_debug_telegram", "0")
        print(f"[sigma_debug] setting={_sigma_debug_tg!r}  price={price}  chain={'ok' if chain else 'None'}")
        if str(_sigma_debug_tg).strip() in ("1", "true", "yes"):
            from core.telegram_bot import send_sigma_debug
            _ok, _msg = send_sigma_debug({
                "symbol":   "SPX",
                "price":    price,
                "levels":   levels,
                "_chain":   chain,
                "strategy": strategy_result,
            })
            print(f"[sigma_debug] Telegram send: ok={_ok}  msg={_msg}")
    except Exception as _tg_err:
        print(f"[sigma_debug_telegram error] {_tg_err}")

    # ── Best Opportunity Engine ───────────────────────────────────────────────
    spy_analysis: Dict[str, Any] = {}
    qqq_analysis: Dict[str, Any] = {}
    iwm_analysis: Dict[str, Any] = {}
    try:
        _prog("● تحليل SPY...")
        spy_analysis = analyze_symbol(tok, "SPY", now)
    except Exception as _e:
        _debug_error(f"SPY analysis: {_e}")
        spy_analysis = {"error": str(_e), "symbol": "SPY"}
    try:
        _prog("● تحليل QQQ...")
        qqq_analysis = analyze_symbol(tok, "QQQ", now)
    except Exception as _e:
        _debug_error(f"QQQ analysis: {_e}")
        qqq_analysis = {"error": str(_e), "symbol": "QQQ"}
    try:
        _prog("● تحليل IWM...")
        iwm_analysis = analyze_symbol(tok, "IWM", now)
    except Exception as _e:
        _debug_error(f"IWM analysis: {_e}")
        iwm_analysis = {"error": str(_e), "symbol": "IWM"}

    # watchlist بدون AAPL (بيانات أسعار فقط للرموز التي لا تحتاج option chain)
    watchlist = {}

    _prog("● تحديث التقرير...")
    _set_analysis_running(False)
    return {
        "timestamp": now.strftime("%Y-%m-%d %H:%M"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "expiry": chain["expiry"] if chain else now.strftime("%Y-%m-%d"),
        "price": price,
        "magnetic": magnetic,
        "pin_score": pin_score,
        "pin_label": pin_label,
        "pin_color": pin_color,
        "is_market_open": is_us_market_open(),
        "targets_up": sorted(targets_up, key=lambda x: x["price"]),
        "targets_down": sorted(targets_down, key=lambda x: x["price"], reverse=True),
        "levels": levels,
        "expected_move": levels.get("expected_move"),
        "expected_move_source": levels.get("expected_move_source"),
        "net_gex":             levels.get("net_gex"),
        "call_signed_gex":     levels.get("call_signed_gex"),
        "put_signed_gex":      levels.get("put_signed_gex"),
        "gross_gex":           levels.get("gross_gex"),
        "gex_near_zero_reason": levels.get("gex_near_zero_reason"),
        "gex_by_strike":        levels.get("gex_by_strike", []),
        "gamma_source":         levels.get("gamma_source", {}),
        "net_dex": levels.get("net_dex"),
        "net_vanna": levels.get("net_vanna"),
        "net_charm": levels.get("net_charm"),
        "zero_gamma": levels.get("zero_gamma"),
        "iron_condor": condor,
        "trade_filter": trade_filter,
        "top_call_gex": levels.get("top_call_gex", []),
        "top_put_gex": levels.get("top_put_gex", []),
        "top_call_oi": levels.get("top_call_oi", []),
        "top_put_oi": levels.get("top_put_oi", []),
        "vix": levels.get("vix"),
        "iv_rank": levels.get("iv_rank"),
        "iv_current": levels.get("iv_current"),
        "iv_rank_source": levels.get("iv_rank_source"),
        "ema20": levels.get("ema20"),
        "ema50": levels.get("ema50"),
        "trend":       levels.get("trend"),
        "trend_label": trend_label(levels.get("trend", "neutral")),
        "daily_trend_label":    trend_label(levels.get("daily_trend", "neutral")),
        "intraday_trend_label": trend_label(levels.get("intraday_trend", "neutral")),
        "net_gex_label": (
            f"+{levels.get('net_gex', 0):,.0f} (إيجابي — كبح الحركة)" if (levels.get("net_gex") or 0) > 0
            else f"{levels.get('net_gex', 0):,.0f} (سلبي — تضخيم الحركة)"
        ),
        "gex_quality": compute_data_quality_report(chain, price, locals().get("spx_price_source", _LAST_DEBUG.get("price_source")), levels)["gex_quality"],
        "data_quality_report": compute_data_quality_report(chain, price, locals().get("spx_price_source", _LAST_DEBUG.get("price_source")), levels),
        "mode": compute_data_quality_report(chain, price, locals().get("spx_price_source", _LAST_DEBUG.get("price_source")), levels)["mode"],
        "chain_calls": len(chain["calls"]) if chain else 0,
        "chain_puts": len(chain["puts"]) if chain else 0,
        "price_source": locals().get("spx_price_source", _LAST_DEBUG.get("price_source")),
        "_chain": chain,
        "strategy": strategy_result,
        "tastytrade_shadow": tastytrade_shadow,
        "trade_mode_eval": trade_mode_eval,
        "selected_trade_mode": trade_mode_eval.get("selected_mode", "0DTE"),
        "watchlist": watchlist,
        "spy": spy_analysis,
        "qqq": qqq_analysis,
        "iwm": iwm_analysis,
        "best_opportunity": _pick_best_opportunity(
            {"symbol": "SPX", "strategy": strategy_result, "price": price},
            spy_analysis,
            qqq_analysis,
            iwm_analysis,
        ),
        "data_quality": {
            "has_delta": bool(chain and any(o.get("delta") is not None for o in chain.get("calls", []) + chain.get("puts", []))),
            "has_gamma": bool(chain and any(o.get("gamma") is not None for o in chain.get("calls", []) + chain.get("puts", []))),
            "has_vanna": bool(chain and any(o.get("vanna") is not None for o in chain.get("calls", []) + chain.get("puts", []))),
            "has_charm": bool(chain and any(o.get("charm") is not None for o in chain.get("calls", []) + chain.get("puts", []))),
            "estimated_greeks": bool(chain and any(o.get("greeks_estimated") for o in chain.get("calls", []) + chain.get("puts", []))),
            "has_prices": bool(chain and any(o.get("mid") is not None for o in chain.get("calls", []) + chain.get("puts", []))),
            "has_open_interest": bool(chain and any((o.get("oi") or 0) > 0 for o in chain.get("calls", []) + chain.get("puts", []))),
            "exposure_uses_volume_proxy": bool(chain and any(o.get("exposure_is_proxy") for o in chain.get("calls", []) + chain.get("puts", []))),
        },
    }




_FULL_ANALYSIS_LOCK = threading.Lock()


def full_analysis(
    client_secret: Optional[str] = None,
    refresh_token: Optional[str] = None,
    progress_cb: Optional[Any] = None,
) -> Dict[str, Any]:
    """Single-flight wrapper around the heavy analysis pipeline.

    Prevents duplicate manual/auto clicks from starting overlapping analysis
    runs, which previously caused interleaved debug tables and repeated
    FINAL_ACTION/session_journal lines.
    """
    if not _FULL_ANALYSIS_LOCK.acquire(blocking=False):
        msg = "analysis_already_running — duplicate request skipped"
        print(f"[analysis_guard] {msg}")
        if callable(progress_cb):
            try:
                progress_cb("● تحليل جارٍ بالفعل — تم تجاهل الطلب المكرر")
            except Exception:
                pass
        return {"error": msg, "_analysis_skipped_duplicate": True}
    try:
        return _full_analysis_impl(
            client_secret=client_secret,
            refresh_token=refresh_token,
            progress_cb=progress_cb,
        )
    finally:
        try:
            from core.trade_monitor import _set_analysis_running
            _set_analysis_running(False)
        except Exception:
            pass
        try:
            _FULL_ANALYSIS_LOCK.release()
        except RuntimeError:
            pass

def compute_data_quality_report(chain: Optional[Dict], price: Optional[float],
                                price_source: Optional[str], levels: Optional[Dict]) -> Dict[str, Any]:
    """تقرير موحد لجودة البيانات — يُستخدم في full_analysis و run_diagnostics."""
    opts = (chain.get("calls", []) + chain.get("puts", [])) if chain else []
    has_price   = bool(price and price > 0)
    has_chain   = bool(chain and opts)
    has_delta   = bool(any(o.get("delta")  is not None for o in opts))
    has_gamma   = bool(any(o.get("gamma")  is not None for o in opts))
    has_prices  = bool(any(o.get("mid")    is not None for o in opts))
    has_oi      = bool(any((o.get("oi") or 0) > 0 for o in opts))
    has_volume  = bool(any((o.get("volume") or 0) > 0 for o in opts))
    has_vix     = bool(levels and levels.get("vix"))
    has_ema     = bool(levels and levels.get("ema20"))

    # جودة GEX — تصحيح التناقض
    if not has_gamma or not has_chain:
        gex_quality = "unavailable"
    elif has_oi:
        gex_quality = "real_oi"
    elif has_volume:
        gex_quality = "volume_proxy"
    elif has_gamma:
        gex_quality = "gamma_proxy"
    else:
        gex_quality = "unavailable"

    # مصدر السعر
    live_price = bool(price_source and "dxlink" in str(price_source))

    # نقاط الجودة
    checks = {
        "price":    has_price,
        "chain":    has_chain,
        "delta":    has_delta,
        "gamma":    has_gamma,
        "prices":   has_prices,
        "vix":      has_vix,
        "ema":      has_ema,
        "oi":       has_oi,
    }
    score = sum(1 for v in checks.values() if v)
    quality_pct = round(score / len(checks) * 100)

    # وضع التشغيل
    if has_chain and has_delta and has_prices and live_price:
        mode = "LIVE"
    elif has_chain and (has_delta or has_prices):
        mode = "PARTIAL"
    elif has_price and has_vix:
        mode = "FALLBACK"
    else:
        mode = "LIMITED"

    return {
        "mode":         mode,
        "quality_pct":  quality_pct,
        "gex_quality":  gex_quality,
        "checks":       checks,
        "live_price":   live_price,
        "price_source": price_source,
    }


def run_diagnostics(client_secret: Optional[str] = None, refresh_token: Optional[str] = None) -> Dict[str, Any]:
    """اختبار مستقل يعطي حالة تسجيل الدخول، السعر، chain، والـ Greeks."""
    _LAST_DEBUG["errors"] = []
    started = datetime.now()
    out: Dict[str, Any] = {"timestamp": started.strftime("%Y-%m-%d %H:%M:%S")}
    symbol_up = "SPX"
    try:
        tok = _get_access_token(client_secret, refresh_token, force=True)
        out["auth"] = "OK"
    except Exception as exc:
        out["auth"] = f"FAILED: {exc}"
        out["debug"] = get_last_debug()
        return out

    chain = get_options_chain(tok)
    out["chain"] = "OK" if chain else "FAILED"
    out["chain_calls"] = len(chain.get("calls", [])) if chain else 0
    out["chain_puts"] = len(chain.get("puts", [])) if chain else 0

    # نفس ترتيب full_analysis: DXLink → Yahoo → Stooq → inference
    price = None
    try:
        from core.dxlink_client import fetch_market_data_snapshot
        spx_symbols = [".SPX", "SPX", "$SPX.X"]
        snap = fetch_market_data_snapshot(tok, spx_symbols, timeout_seconds=6.0, max_symbols=5)
        for sym in spx_symbols:
            entry = snap.get(sym, {})
            bid = entry.get("bid")
            ask = entry.get("ask")
            last = entry.get("last")
            prev = entry.get("prev-close")
            if bid and ask and bid > 0 and ask > 0:
                price = round((bid + ask) / 2, 2)
                _debug_set("price_source", f"dxlink:bid_ask:{sym}")
                break
            elif last and last > 1000:
                price = last
                _debug_set("price_source", f"dxlink:last:{sym}")
                break
            elif prev and prev > 1000:
                price = prev
                _debug_set("price_source", f"dxlink:prev_close:{sym}")
                break
    except Exception as exc:
        _debug_error(f"dxlink SPX price diag: {exc}")

    if not price:
        price = get_spx_price_yahoo()
    if not price:
        price = get_spx_price(tok)
    if not price:
        price = _infer_spot_from_chain(chain)
        if price:
            _debug_set("price_source", "option_chain_inference")

    out["spx_price"] = price
    out["price_source"] = _LAST_DEBUG.get("price_source")

    all_opts = (chain.get("calls", []) + chain.get("puts", [])) if chain else []
    out["greeks_available"] = {
        "delta": any(o.get("delta") is not None for o in all_opts),
        "gamma": any(o.get("gamma") is not None for o in all_opts),
        "iv": any(o.get("iv") is not None for o in all_opts),
        "prices": any(o.get("mid") is not None for o in all_opts),
    }
    if price:
        try:
            ctx = get_market_context(chain, price, symbol=symbol_up)
            out.update({
                "vix":            ctx.get("vix"),
                "iv_current":     ctx.get("iv_current"),
                "iv_rank":        ctx.get("iv_rank"),
                "iv_rank_source": ctx.get("iv_rank_source"),
                "ema20":          ctx.get("ema20"),
                "ema50":          ctx.get("ema50"),
                "daily_trend":    ctx.get("daily_trend"),
                "ema20_15m":      ctx.get("ema20_15m"),
                "ema50_15m":      ctx.get("ema50_15m"),
                "intraday_trend": ctx.get("intraday_trend"),
                "trend":          ctx.get("trend"),
                "combined_trend": ctx.get("combined_trend"),
            })
        except Exception as exc:
            _debug_error(f"diagnostics market context: {exc}")
    # احسب Pin Score وEM كما في full_analysis
    if price and chain:
        try:
            enrich_exposures_with_spot(chain, price)
            levels = calculate_levels(chain, price, symbol="SPX")
            magnetic = levels.get("magnetic", round(price / 50) * 50)
            pin_score = calculate_pin_score(price, magnetic, chain, levels)
            em_val = levels.get("expected_move")
            em_src = levels.get("expected_move_source", "")
            straddle = levels.get("straddle_mid")
            atm_k = levels.get("em_atm_strike")
            out["pin_score"]            = pin_score
            out["expected_move"]        = em_val
            out["expected_move_source"] = em_src
            out["straddle_mid"]         = straddle
            out["em_atm_strike"]        = atm_k
            out["magnetic"]             = magnetic
            out["levels"]               = levels
            out["_chain"]               = chain
            out["gex_quality"] = (
                "real_oi"      if any((o.get("oi") or 0) > 0 for o in chain.get("calls",[]) + chain.get("puts",[]))
                else "volume_proxy" if any((o.get("volume") or 0) > 0 for o in chain.get("calls",[]) + chain.get("puts",[]))
                else "gamma_proxy"  if any(o.get("gamma") is not None for o in chain.get("calls",[]) + chain.get("puts",[]))
                else "unavailable"
            )
        except Exception as exc:
            _debug_error(f"diagnostics levels/pin: {exc}")
            out["pin_score"] = None
            out["expected_move"] = None

    # تقرير جودة البيانات الموحد
    dq_report = compute_data_quality_report(
        chain, price, _LAST_DEBUG.get("price_source"),
        out.get("levels")
    )
    out["data_quality_report"] = dq_report
    out["gex_quality"] = dq_report["gex_quality"]
    out["mode"] = dq_report["mode"]

    out["chain_root"] = _LAST_DEBUG.get("chain_root")
    out["chain_raw_preview"] = _LAST_DEBUG.get("chain_raw_preview")
    out["debug"] = get_last_debug()
    return out

WATCHLIST_SYMBOLS: Dict[str, Any] = {}  # SPY/QQQ الآن تحليل كامل وليس watchlist

_WATCHLIST_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def fetch_watchlist_snapshot() -> Dict[str, Dict[str, Any]]:
    """
    جلب سعر + EMA20/50 + اتجاه لـ SPY, QQQ, AAPL من Yahoo Finance.
    يُخزّن في cache لمدة 5 دقائق لتجنب طلبات متعددة.
    """
    results: Dict[str, Dict[str, Any]] = {}
    now = time.time()

    for ticker, meta in WATCHLIST_SYMBOLS.items():
        cached = _WATCHLIST_CACHE.get(ticker)
        if cached and now - cached[0] < 300:
            results[ticker] = dict(cached[1])
            continue

        yahoo_sym = meta["yahoo"]
        min_price = meta["min_price"]
        closes = _fetch_yahoo_closes(yahoo_sym, "6mo", "1d")
        closes_15m = _fetch_yahoo_closes(yahoo_sym, "5d", "15m")

        price_val: Optional[float] = None
        if closes:
            price_val = _last_value(closes)
        if not price_val or price_val < min_price:
            price_val = None

        ema20 = _ema(closes, 20) if len(closes) >= 20 else None
        ema50 = _ema(closes, 50) if len(closes) >= 50 else None
        ema20_15m = _ema(closes_15m, 20) if len(closes_15m) >= 20 else None
        ema50_15m = _ema(closes_15m, 50) if len(closes_15m) >= 50 else None

        daily_trend = _trend_from_ema(price_val, ema20, ema50) if price_val else "neutral"
        intraday = _trend_from_ema(price_val, ema20_15m, ema50_15m) if price_val else "neutral"
        combined = _combine_daily_intraday_trend(daily_trend, intraday)

        # تغيّر السعر عن الإغلاق السابق
        change_pct: Optional[float] = None
        if price_val and len(closes) >= 2 and closes[-2]:
            change_pct = round((price_val - closes[-2]) / closes[-2] * 100, 2)

        entry = {
            "ticker":        ticker,
            "name":          meta["name"],
            "price":         price_val,
            "change_pct":    change_pct,
            "ema20":         ema20,
            "ema50":         ema50,
            "daily_trend":   daily_trend,
            "intraday":      intraday,
            "combined":      combined,
            "trend_label":   trend_label(combined),
        }
        _WATCHLIST_CACHE[ticker] = (now, entry)
        results[ticker] = dict(entry)

    return results


def _dummy_levels(price: float) -> Dict[str, Any]:
    em = max(round(price * 0.0045, 0), 5)
    return {
        "call_wall_oi": round((price + 10) / 5) * 5,
        "call_wall_vol": round((price + 8) / 5) * 5,
        "put_wall_oi": round((price - 10) / 5) * 5,
        "put_wall_vol": round((price - 8) / 5) * 5,
        "em_upper": round(price + em, 0),
        "em_lower": round(price - em, 0),
        "expected_move": em,
        "expected_move_source": "fallback",
        "magnetic": round(price / 50) * 50,
        "iv_rank": 0,
        "trend": "neutral",
        "daily_trend": "neutral",
        "intraday_trend": "neutral",
        "combined_trend": "neutral",
        "vix": None,
        "ema20": None,
        "ema50": None,
        "ema20_15m": None,
        "ema50_15m": None,
    }
