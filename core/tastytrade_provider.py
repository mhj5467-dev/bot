"""
core/tastytrade_provider.py
===========================
مزود بيانات Tastytrade الرئيسي - M1 Shadow Mode
يتصل بـ Tastytrade API لجلب:
- سلسلة الخيارات الحقيقية
- IV Rank و IV Percentile
- Greeks (Delta, Gamma, Theta, Vega)
- الأسعار الحية

ملاحظة مهمة:
- LIVE_TRADING_ENABLED = false دائماً
- هذا المزود للبيانات فقط، لا تنفيذ أوامر
- SPX مباشرة (ليس SPY×10)
- لا يعتمد على Yahoo Finance
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from loguru import logger

try:
    from dotenv import load_dotenv
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

from config import settings


# ============================================================
# حالة الاتصال العالمية
# ============================================================
_SESSION_TOKEN: Optional[str] = None
_SESSION_EXPIRY: float = 0.0
_SESSION_LOCK_TS: float = 0.0
_SESSION_ERROR: str = ""


class TastytradeProvider:
    """
    مزود بيانات Tastytrade
    وضع M1: Shadow Mode - بيانات حقيقية بدون تنفيذ صفقات
    """

    BASE_URL = os.getenv("TASTYTRADE_BASE_URL", "https://api.tastytrade.com")
    TIMEOUT = float(os.getenv("TASTY_SHADOW_TIMEOUT_SEC", "2.5"))
    PRICE_MISMATCH_THRESHOLD = float(
        os.getenv("TASTY_SHADOW_PRICE_MISMATCH_PTS",
                  str(settings.TASTYTRADE_PRICE_MISMATCH_THRESHOLD))
    )

    def __init__(self):
        # التحقق من الأمان
        assert not settings.LIVE_TRADING_ENABLED, "LIVE_TRADING_ENABLED يجب أن يكون false!"
        self._headers: Dict[str, str] = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        logger.debug("تم تهيئة مزود Tastytrade (Shadow Mode)")

    # ──────────────────────────────────────────────────────
    # المصادقة
    # ──────────────────────────────────────────────────────

    def authenticate(self) -> Tuple[bool, str]:
        """المصادقة مع Tastytrade API"""
        global _SESSION_TOKEN, _SESSION_EXPIRY, _SESSION_ERROR

        username = os.getenv("TASTYTRADE_USERNAME", "")
        password = os.getenv("TASTYTRADE_PASSWORD", "")

        if not username or not password:
            _SESSION_ERROR = "credentials_missing"
            return False, "TASTYTRADE_USERNAME أو TASTYTRADE_PASSWORD غير مُعيّن"

        # إعادة استخدام الجلسة الحالية إذا كانت صالحة
        if _SESSION_TOKEN and time.time() < _SESSION_EXPIRY:
            return True, "session_reused"

        try:
            resp = requests.post(
                f"{self.BASE_URL}/sessions",
                json={"login": username, "password": password},
                headers=self._headers,
                timeout=self.TIMEOUT,
            )
            if resp.status_code == 201:
                data = resp.json().get("data", {})
                token = data.get("session-token") or data.get("token")
                if token:
                    _SESSION_TOKEN = token
                    _SESSION_EXPIRY = time.time() + 3600  # ساعة واحدة
                    _SESSION_ERROR = ""
                    self._headers["Authorization"] = token
                    logger.info("تم الاتصال بـ Tastytrade بنجاح")
                    return True, "authenticated"
                return False, "no_token_in_response"
            else:
                _SESSION_ERROR = f"http_{resp.status_code}"
                return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
        except requests.Timeout:
            _SESSION_ERROR = "timeout"
            return False, "timeout"
        except Exception as exc:
            _SESSION_ERROR = str(exc)[:100]
            return False, str(exc)[:100]

    def get_token(self) -> Optional[str]:
        """الحصول على رمز الجلسة الحالي"""
        if _SESSION_TOKEN and time.time() < _SESSION_EXPIRY:
            return _SESSION_TOKEN
        ok, reason = self.authenticate()
        return _SESSION_TOKEN if ok else None

    # ──────────────────────────────────────────────────────
    # بيانات الأسعار
    # ──────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """
        جلب سعر رمز معين
        SPX يُجلب مباشرة (ليس SPY×10)
        """
        token = self.get_token()
        if not token:
            return {"symbol": symbol, "error": _SESSION_ERROR, "price": None}

        # رمز DXLink لـ SPX
        event_symbol = self._to_event_symbol(symbol)

        try:
            from core.dxlink_client import fetch_market_data_snapshot
            snap = fetch_market_data_snapshot(
                token, [event_symbol],
                timeout_seconds=self.TIMEOUT,
                max_symbols=1,
            )
            rec = snap.get(event_symbol) or (next(iter(snap.values())) if snap else {})
            if not rec:
                return {"symbol": symbol, "event_symbol": event_symbol, "price": None, "error": "no_data"}

            bid = self._safe_float(rec.get("bid"))
            ask = self._safe_float(rec.get("ask"))
            last = self._safe_float(rec.get("last") or rec.get("price"))
            mid = None
            if bid and ask and bid > 0 and ask > 0:
                mid = round((bid + ask) / 2.0, 2)
            price = mid or last

            return {
                "symbol": symbol,
                "event_symbol": event_symbol,
                "price": price,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "last": last,
                "timestamp": datetime.now().isoformat(),
                "source": "tastytrade_dxlink",
                "error": None,
            }
        except Exception as exc:
            logger.warning(f"خطأ في جلب سعر {symbol}: {exc}")
            return {"symbol": symbol, "price": None, "error": str(exc)[:100]}

    def get_quotes_batch(self, symbols: List[str]) -> Dict[str, Dict]:
        """جلب أسعار متعددة دفعة واحدة"""
        results = {}
        for sym in symbols:
            results[sym] = self.get_quote(sym)
        return results

    # ──────────────────────────────────────────────────────
    # بيانات الخيارات
    # ──────────────────────────────────────────────────────

    def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        جلب سلسلة الخيارات لرمز معين
        GET /option-chains/{symbol}/nested
        """
        token = self.get_token()
        if not token:
            return {"symbol": symbol, "error": _SESSION_ERROR, "chain": []}

        tt_symbol = self._to_tt_symbol(symbol)
        url = f"{self.BASE_URL}/option-chains/{tt_symbol}/nested"
        headers = {**self._headers, "Authorization": token}

        try:
            resp = requests.get(url, headers=headers, timeout=self.TIMEOUT)
            if resp.status_code == 200:
                data = resp.json().get("data", {})
                items = data.get("items", [])
                chain = self._parse_chain(items, symbol, expiry)
                return {
                    "symbol": symbol,
                    "chain": chain,
                    "expiries": list({c["expiry"] for c in chain if c.get("expiry")}),
                    "error": None,
                    "source": "tastytrade_api",
                }
            else:
                return {
                    "symbol": symbol,
                    "chain": [],
                    "error": f"HTTP {resp.status_code}",
                }
        except requests.Timeout:
            return {"symbol": symbol, "chain": [], "error": "timeout"}
        except Exception as exc:
            return {"symbol": symbol, "chain": [], "error": str(exc)[:100]}

    def get_iv_data(self, symbol: str) -> Dict[str, Any]:
        """
        جلب بيانات IV (Rank و Percentile)
        GET /market-metrics?symbols=SYMBOL
        """
        token = self.get_token()
        if not token:
            return {"symbol": symbol, "iv_rank": None, "iv_percentile": None, "error": _SESSION_ERROR}

        tt_symbol = self._to_tt_symbol(symbol)
        url = f"{self.BASE_URL}/market-metrics"
        headers = {**self._headers, "Authorization": token}

        try:
            resp = requests.get(
                url,
                params={"symbols": tt_symbol},
                headers=headers,
                timeout=self.TIMEOUT,
            )
            if resp.status_code == 200:
                items = resp.json().get("data", {}).get("items", [])
                for item in items:
                    if item.get("symbol") == tt_symbol:
                        return {
                            "symbol": symbol,
                            "iv_rank": self._safe_float(
                                item.get("implied-volatility-index-rank")
                                or item.get("iv_rank")
                            ),
                            "iv_percentile": self._safe_float(
                                item.get("implied-volatility-percentile")
                                or item.get("iv_percentile")
                            ),
                            "iv30": self._safe_float(item.get("implied-volatility-30-day")),
                            "hv30": self._safe_float(item.get("historical-volatility-30-day")),
                            "error": None,
                            "source": "tastytrade_market_metrics",
                        }
            return {"symbol": symbol, "iv_rank": None, "iv_percentile": None, "error": f"HTTP {resp.status_code}"}
        except Exception as exc:
            return {"symbol": symbol, "iv_rank": None, "iv_percentile": None, "error": str(exc)[:100]}

    def get_full_symbol_data(self, symbol: str) -> Dict[str, Any]:
        """
        جلب كامل البيانات لرمز: سعر + IV + Greeks
        يُستخدم في كل دورة تحليل
        """
        quote = self.get_quote(symbol)
        iv_data = self.get_iv_data(symbol)

        result = {
            "symbol": symbol,
            "price": quote.get("price"),   # السعر الخاص بهذا الرمز، ليس SPX!
            "bid": quote.get("bid"),
            "ask": quote.get("ask"),
            "last": quote.get("last"),
            "iv_rank": iv_data.get("iv_rank"),
            "iv_percentile": iv_data.get("iv_percentile"),
            "iv30": iv_data.get("iv30"),
            "hv30": iv_data.get("hv30"),
            "timestamp": datetime.now().isoformat(),
            "source": "tastytrade",
            "quote_error": quote.get("error"),
            "iv_error": iv_data.get("error"),
            "shadow_mode": True,           # M1: وضع المظلة
            "live_trading": False,         # أبداً!
        }

        # حساب IV Regime
        iv_rank = result["iv_rank"]
        if iv_rank is not None:
            if iv_rank >= settings.IV_HIGH_THRESHOLD:
                result["iv_regime"] = "high"
            elif iv_rank <= settings.IV_LOW_THRESHOLD:
                result["iv_regime"] = "low"
            else:
                result["iv_regime"] = "neutral"
        else:
            result["iv_regime"] = "unknown"

        return result

    # ──────────────────────────────────────────────────────
    # Shadow Mode: مقارنة مع التحليل الرئيسي
    # ──────────────────────────────────────────────────────

    def compare_with_main(
        self,
        symbol: str,
        main_price: float,
        shadow_price: Optional[float],
    ) -> Dict[str, Any]:
        """
        مقارنة سعر Shadow مع السعر الرئيسي
        يُسجّل التفاوتات لأغراض التشخيص فقط، لا يؤثر على التداول
        """
        result = {
            "symbol": symbol,
            "main_price": main_price,
            "shadow_price": shadow_price,
            "diff_points": None,
            "diff_pct": None,
            "mismatch": False,
            "warning": "",
        }

        if shadow_price is None or main_price <= 0:
            result["warning"] = "shadow_price_unavailable"
            return result

        diff = shadow_price - main_price
        diff_pct = (diff / main_price) * 100 if main_price > 0 else 0
        result["diff_points"] = round(diff, 4)
        result["diff_pct"] = round(diff_pct, 4)

        if abs(diff) > self.PRICE_MISMATCH_THRESHOLD:
            result["mismatch"] = True
            result["warning"] = (
                f"DATA_MISMATCH: {symbol} diff={diff:.2f}pts "
                f"(threshold={self.PRICE_MISMATCH_THRESHOLD}pts)"
            )
            logger.warning(result["warning"])

        return result

    # ──────────────────────────────────────────────────────
    # أدوات مساعدة
    # ──────────────────────────────────────────────────────

    def _to_event_symbol(self, symbol: str) -> str:
        """تحويل الرمز إلى رمز DXLink"""
        s = str(symbol or "").upper().strip()
        if s == "SPX":
            return os.getenv("TASTY_SHADOW_SPX_EVENT_SYMBOL", "$SPX.X")
        return s

    def _to_tt_symbol(self, symbol: str) -> str:
        """تحويل الرمز إلى رمز Tastytrade"""
        s = str(symbol or "").upper().strip()
        if s == "SPX":
            return "SPX"  # Tastytrade يستخدم SPX مباشرة
        return s

    def _safe_float(self, value: Any) -> Optional[float]:
        try:
            if value is None or value == "":
                return None
            return float(value)
        except Exception:
            return None

    def _parse_chain(
        self,
        items: List[Dict],
        symbol: str,
        target_expiry: Optional[str],
    ) -> List[Dict]:
        """تحليل سلسلة الخيارات من استجابة API"""
        contracts = []
        for expiry_group in items:
            expiry = expiry_group.get("expiration-date") or expiry_group.get("expiry", "")
            if target_expiry and expiry != target_expiry:
                continue
            strikes = expiry_group.get("strikes", [])
            for strike_data in strikes:
                strike = self._safe_float(strike_data.get("strike-price") or strike_data.get("strike"))
                if strike is None:
                    continue
                # Call
                call = strike_data.get("call", {})
                put = strike_data.get("put", {})
                contracts.append({
                    "symbol": symbol,
                    "expiry": expiry,
                    "strike": strike,
                    "call_bid": self._safe_float(call.get("bid")),
                    "call_ask": self._safe_float(call.get("ask")),
                    "call_oi": self._safe_float(call.get("open-interest") or call.get("oi")),
                    "call_volume": self._safe_float(call.get("volume")),
                    "call_delta": self._safe_float(call.get("delta")),
                    "call_gamma": self._safe_float(call.get("gamma")),
                    "call_theta": self._safe_float(call.get("theta")),
                    "call_vega": self._safe_float(call.get("vega")),
                    "call_iv": self._safe_float(call.get("implied-volatility") or call.get("iv")),
                    "put_bid": self._safe_float(put.get("bid")),
                    "put_ask": self._safe_float(put.get("ask")),
                    "put_oi": self._safe_float(put.get("open-interest") or put.get("oi")),
                    "put_volume": self._safe_float(put.get("volume")),
                    "put_delta": self._safe_float(put.get("delta")),
                    "put_gamma": self._safe_float(put.get("gamma")),
                    "put_theta": self._safe_float(put.get("theta")),
                    "put_vega": self._safe_float(put.get("vega")),
                    "put_iv": self._safe_float(put.get("implied-volatility") or put.get("iv")),
                })
        return contracts

    def is_connected(self) -> bool:
        """هل الاتصال نشط"""
        return bool(_SESSION_TOKEN) and time.time() < _SESSION_EXPIRY

    def get_status(self) -> Dict[str, Any]:
        """حالة الاتصال"""
        return {
            "connected": self.is_connected(),
            "shadow_mode": True,
            "live_trading": False,
            "session_error": _SESSION_ERROR,
            "expires_in": max(0, int(_SESSION_EXPIRY - time.time())) if _SESSION_TOKEN else 0,
        }
