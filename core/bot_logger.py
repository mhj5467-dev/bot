"""
Bot Logger — نظام تسجيل مركزي لكل أحداث البوت.

يكتب في data/bot.log مع rotation تلقائي (5MB × 3 ملفات).
يُستخدم بدلاً من print() في المسارات الحيوية.

مستويات التسجيل:
  INFO  — أحداث عادية (بداية دورة، صفقة، رفض إشارة)
  WARN  — تحذيرات لا تمنع التشغيل (بيانات ناقصة، IV مقدّر)
  ERROR — أخطاء تستحق الانتباه (استثناءات، فشل API)
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta

# ── المسارات ─────────────────────────────────────────────────────────────────
try:
    from core.app_paths import get_app_root, get_data_dir
    _BOT_DIR = str(get_app_root())
    _DATA_DIR = str(get_data_dir())
except Exception:
    _BOT_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _DATA_DIR = os.path.join(_BOT_DIR, "data")
    os.makedirs(_DATA_DIR, exist_ok=True)
LOG_PATH  = os.path.join(_DATA_DIR, "bot.log")

# ── إعداد الـ Logger ─────────────────────────────────────────────────────────
_logger = logging.getLogger("abu_hassan_bot")
_logger.setLevel(logging.DEBUG)

if not _logger.handlers:
    # ملف: rotation 5MB × 3
    _fh = RotatingFileHandler(
        LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3,
        encoding="utf-8",
    )
    _fh.setLevel(logging.DEBUG)
    _fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _logger.addHandler(_fh)

    # Console: WARNING فقط حتى لا يزعج الـ terminal
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.WARNING)
    _ch.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    _logger.addHandler(_ch)


# ── وقت ET ───────────────────────────────────────────────────────────────────
def _et_str() -> str:
    now_utc = datetime.now(timezone.utc)
    m, d = now_utc.month, now_utc.day
    is_edt = 3 < m < 11 or (m == 3 and d >= 8) or (m == 11 and d < 8)
    et = now_utc + timedelta(hours=-4 if is_edt else -5)
    return et.strftime("%H:%M ET")


def _safe_text(value, default="?") -> str:
    """Return a printable string for logger fields that may be None."""
    if value is None:
        return default
    try:
        text = str(value)
    except Exception:
        return default
    return text if text else default


def _safe_float(value, default=0.0) -> float:
    """Return a float for logger numeric fields that may be None/invalid."""
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


# ── API العامة ────────────────────────────────────────────────────────────────

def log_cycle_start(symbol: str = "ALL") -> None:
    """بداية دورة تحليل."""
    _logger.info(f"=== CYCLE START | {symbol} | {_et_str()} ===")


def log_cycle_end(symbol: str, duration_ms: int = 0) -> None:
    """نهاية دورة تحليل."""
    _logger.info(f"=== CYCLE END   | {symbol} | {duration_ms}ms | {_et_str()} ===")


def log_signal(symbol: str, strategy: str, score: float,
               mode: str, exec_mode: str, reasons: list = None) -> None:
    """إشارة مؤهلة — تم القبول."""
    symbol = _safe_text(symbol)
    strategy = _safe_text(strategy, "None")
    score = _safe_float(score)
    mode = _safe_text(mode, "-")
    exec_mode = _safe_text(exec_mode, "-")
    reasons_str = " | ".join([_safe_text(r, "") for r in reasons[:3]]) if reasons else ""
    _logger.info(
        f"SIGNAL OK  | {symbol:4} {strategy:20} score={score:5.1f} "
        f"mode={mode:5} exec={exec_mode:6} | {_et_str()} | {reasons_str}"
    )


def log_rejection(symbol: str, strategy: str, score: float,
                  reason: str) -> None:
    """إشارة مرفوضة. Safe against None strategy/score."""
    symbol = _safe_text(symbol)
    strategy = _safe_text(strategy, "None")
    score = _safe_float(score)
    reason = _safe_text(reason, "-")
    _logger.info(
        f"REJECTED   | {symbol:4} {strategy:20} score={score:5.1f} "
        f"| {reason[:60]} | {_et_str()}"
    )


def log_trade_open(symbol: str, strategy: str, exec_mode: str,
                   trade_mode: str, val: float, score: float,
                   trade_id=None) -> None:
    """فتح صفقة (Paper أو Auto)."""
    symbol = _safe_text(symbol)
    strategy = _safe_text(strategy, "None")
    exec_mode = _safe_text(exec_mode, "-")
    trade_mode = _safe_text(trade_mode, "-")
    val = _safe_float(val)
    score = _safe_float(score)
    tid = f"#{trade_id}" if trade_id else ""
    _logger.info(
        f"TRADE OPEN | {symbol:4} {strategy:20} {exec_mode:6} {trade_mode:5} "
        f"val={val:.2f} score={score:.0f} {tid} | {_et_str()}"
    )


def log_trade_close(symbol: str, strategy: str, result: str,
                    pnl_pct: float, reason: str, trade_id=None) -> None:
    """إغلاق صفقة."""
    symbol = _safe_text(symbol)
    strategy = _safe_text(strategy, "None")
    result = _safe_text(result, "-")
    pnl_pct = _safe_float(pnl_pct)
    reason = _safe_text(reason, "-")
    tid  = f"#{trade_id}" if trade_id else ""
    sign = "+" if pnl_pct >= 0 else ""
    _logger.info(
        f"TRADE CLOSE| {symbol:4} {strategy:20} {result:12} "
        f"P&L={sign}{pnl_pct:.1f}% {tid} | {reason[:40]} | {_et_str()}"
    )


def log_price_refresh(symbol: str, current_val: float,
                      pnl_pct: float, trade_id=None) -> None:
    """تحديث سعر صفقة مفتوحة."""
    tid  = f"#{trade_id}" if trade_id else ""
    sign = "+" if pnl_pct >= 0 else ""
    _logger.debug(
        f"PRICE UPD  | {symbol:4} {tid:5} val={current_val:.2f} "
        f"P&L={sign}{pnl_pct:.1f}% | {_et_str()}"
    )


def log_error(context: str, error: Exception) -> None:
    """تسجيل استثناء."""
    _logger.error(f"ERROR | {context} | {type(error).__name__}: {error}")


def log_warning(context: str, msg: str) -> None:
    """تحذير."""
    _logger.warning(f"WARN  | {context} | {msg}")


def log_market_closed(et_time: str) -> None:
    _logger.info(f"MARKET CLOSED | {et_time} — analysis only")


def log_info(msg: str) -> None:
    """رسالة عامة."""
    _logger.info(msg)


# ── Watchdog State ────────────────────────────────────────────────────────────
import time as _time
import threading as _threading

_wd_lock           = _threading.Lock()
_last_cycle_ts: float   = 0.0     # time.time() لآخر دورة ناجحة
_last_cycle_str: str    = ""      # نص للعرض
_last_error_str: str    = ""      # آخر خطأ
_cycle_count: int        = 0      # عدد الدورات منذ البدء


def wd_beat(symbol: str = "") -> None:
    """يُستدعى عند نهاية كل دورة تحليل ناجحة."""
    global _last_cycle_ts, _last_cycle_str, _cycle_count
    with _wd_lock:
        _last_cycle_ts  = _time.time()
        _last_cycle_str = f"{datetime.now().strftime('%H:%M:%S')}  {symbol}"
        _cycle_count   += 1


def wd_error(msg: str) -> None:
    """يُستدعى عند حدوث خطأ في الدورة."""
    global _last_error_str
    with _wd_lock:
        _last_error_str = f"{datetime.now().strftime('%H:%M:%S')}  {msg[:80]}"
    _logger.error(f"WD_ERROR | {msg}")


def wd_status(max_gap_seconds: int = 420) -> dict:
    """
    يعيد حالة الـ Watchdog للعرض في الواجهة.
    max_gap_seconds: الحد الأقصى بين دورتين قبل إطلاق التحذير (افتراضي 7 دقائق).
    """
    with _wd_lock:
        ts   = _last_cycle_ts
        last = _last_cycle_str
        err  = _last_error_str
        cnt  = _cycle_count

    if ts == 0:
        return {
            "ok":       False,
            "status":   "لم تبدأ بعد",
            "last_run": "—",
            "last_err": err,
            "cycles":   cnt,
            "age_sec":  None,
            "warning":  True,
        }

    age = _time.time() - ts
    ok  = age < max_gap_seconds

    if age < 60:
        age_str = f"{int(age)}s"
    elif age < 3600:
        age_str = f"{int(age//60)}m {int(age%60)}s"
    else:
        age_str = f"{int(age//3600)}h {int((age%3600)//60)}m"

    return {
        "ok":       ok,
        "status":   "يعمل" if ok else f"متوقف منذ {age_str}",
        "last_run": last,
        "last_err": err,
        "cycles":   cnt,
        "age_sec":  round(age),
        "warning":  not ok,
    }
