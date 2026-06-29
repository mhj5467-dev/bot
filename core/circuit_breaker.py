"""
Circuit Breaker — يحمي البوت من الفشل المتكرر في الـ API.

المنطق:
  CLOSED  (طبيعي)   : كل الطلبات تمر
  OPEN    (محجوب)   : الطلبات ترفض فوراً — انتظر reset_after ثانية
  HALF    (اختبار)  : طلب واحد يمر — نجح؟ → CLOSED  |  فشل؟ → OPEN

عتبات الفتح (CLOSED → OPEN):
  فشل 3 مرات متتالية خلال آخر 5 دقائق → يفتح الـ Circuit
  يُرسَل تنبيه Telegram عند الفتح والإغلاق.

الاستخدام:
    from core.circuit_breaker import api_breaker

    with api_breaker("tastytrade"):
        resp = requests.get(...)    # إذا كان Circuit مفتوحاً → CircuitOpenError
"""
from __future__ import annotations

import time
import threading
from typing import Dict, Optional
from contextlib import contextmanager


class CircuitOpenError(Exception):
    """يُرفع عندما يكون Circuit مفتوحاً — لا تحاول الاتصال."""
    pass


class CircuitBreaker:
    """Circuit Breaker لـ API واحد."""

    CLOSED   = "CLOSED"
    OPEN     = "OPEN"
    HALF     = "HALF_OPEN"

    def __init__(self, name: str,
                 fail_threshold: int = 3,
                 window_seconds: int = 300,
                 reset_after: int   = 120):
        self.name           = name
        self.fail_threshold = fail_threshold   # عدد الفشل قبل الفتح
        self.window_seconds = window_seconds   # نافذة العدّ (5 دقائق)
        self.reset_after    = reset_after      # ثوانٍ قبل نصف-الفتح (2 دقيقة)
        self._lock              = threading.Lock()
        self._failures:  list[float] = []      # timestamps للفشل
        self._state:     str         = self.CLOSED
        self._opened_at: float       = 0.0
        self._last_err:  str         = ""
        self._half_open_inflight: bool = False  # يمنع أكثر من probe واحد في HALF_OPEN

    # ── State queries ─────────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._get_state()

    def _get_state(self) -> str:
        """يحدّث الحالة بناءً على الوقت (بدون lock — يُستدعى داخل lock)."""
        if self._state == self.OPEN:
            if time.time() - self._opened_at >= self.reset_after:
                self._state = self.HALF
        return self._state

    def status(self) -> dict:
        with self._lock:
            s    = self._get_state()
            now  = time.time()
            recent = [t for t in self._failures if now - t < self.window_seconds]
            age  = round(now - self._opened_at) if self._opened_at else None
            return {
                "name":          self.name,
                "state":         s,
                "recent_fails":  len(recent),
                "threshold":     self.fail_threshold,
                "last_error":    self._last_err,
                "open_since":    age,
                "ok":            s == self.CLOSED,
            }

    # ── Context manager ───────────────────────────────────────────────────────

    @contextmanager
    def __call__(self):
        with self._lock:
            state = self._get_state()
            if state == self.OPEN:
                age = round(time.time() - self._opened_at)
                raise CircuitOpenError(
                    f"[{self.name}] Circuit OPEN — "
                    f"API مغلق مؤقتاً (منذ {age}s) | آخر خطأ: {self._last_err}"
                )
            if state == self.HALF:
                if self._half_open_inflight:
                    raise CircuitOpenError(
                        f"[{self.name}] Circuit HALF_OPEN — probe جارٍ بالفعل"
                    )
                self._half_open_inflight = True

        try:
            yield
            # نجاح
            with self._lock:
                if self._state in (self.HALF, self.OPEN):
                    _notify_recovery(self.name)
                self._state              = self.CLOSED
                self._failures           = []
                self._opened_at          = 0.0
                self._half_open_inflight = False
        except CircuitOpenError:
            raise
        except Exception as exc:
            with self._lock:
                self._half_open_inflight = False
                now = time.time()
                self._last_err = f"{type(exc).__name__}: {str(exc)[:80]}"
                # حذف الفشل القديم خارج النافذة
                self._failures = [t for t in self._failures
                                  if now - t < self.window_seconds]
                self._failures.append(now)

                was_closed = self._state == self.CLOSED
                if len(self._failures) >= self.fail_threshold:
                    if self._state != self.OPEN:
                        self._state     = self.OPEN
                        self._opened_at = now
                        if was_closed:
                            _notify_open(self.name, self._last_err,
                                         len(self._failures))
            raise


# ── Global breakers (واحد لكل API رئيسي) ────────────────────────────────────

_breakers: Dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def get_breaker(name: str,
                fail_threshold: int = 3,
                reset_after: int    = 120) -> CircuitBreaker:
    """يعيد الـ breaker المقابل للاسم أو ينشئه."""
    with _breakers_lock:
        if name not in _breakers:
            _breakers[name] = CircuitBreaker(
                name, fail_threshold=fail_threshold, reset_after=reset_after)
        return _breakers[name]


# Breakers جاهزة للاستخدام المباشر
def tastytrade_breaker() -> CircuitBreaker:
    return get_breaker("tastytrade", fail_threshold=3, reset_after=120)

def yahoo_breaker() -> CircuitBreaker:
    return get_breaker("yahoo",      fail_threshold=5, reset_after=60)


def all_status() -> list:
    """حالة كل الـ breakers — للعرض في الواجهة."""
    with _breakers_lock:
        return [b.status() for b in _breakers.values()]


# ── Notifications ─────────────────────────────────────────────────────────────

def _notify_open(name: str, error: str, count: int) -> None:
    """إرسال تنبيه Telegram عند فتح الـ Circuit."""
    try:
        from core.bot_logger import log_warning
        log_warning("CircuitBreaker", f"{name} OPEN after {count} failures: {error}")
    except Exception:
        pass
    try:
        from core.telegram_bot import send_message
        send_message(
            f"🔴 <b>Circuit Breaker OPEN</b>\n"
            f"API: {name}\n"
            f"فشل {count} مرات متتالية\n"
            f"آخر خطأ: {error}\n"
            f"البوت متوقف مؤقتاً — سيحاول مجدداً خلال 2 دقيقة"
        )
    except Exception:
        pass


def _notify_recovery(name: str) -> None:
    """تنبيه عند استعادة الاتصال."""
    try:
        from core.bot_logger import log_info
        log_info(f"CircuitBreaker: {name} RECOVERED → CLOSED")
    except Exception:
        pass
    try:
        from core.telegram_bot import send_message
        send_message(f"🟢 <b>Circuit Breaker CLOSED</b>\nAPI: {name} — تم استعادة الاتصال")
    except Exception:
        pass
