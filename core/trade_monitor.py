"""
Trade Monitor — Paper Signal Logger + 50% Profit Rule + Market Close Rule
يسجل الصفقات الورقية فقط عند ظهور إشارة مؤهلة ويتابعها حتى الإغلاق.
"""
from __future__ import annotations
import json
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

_qualify_lock = threading.Lock()  # منع race condition عند فتح صفقتين بالتزامن

# RC15i.5: يمنع refresh_paper_trade_prices من التنافس مع Full Analysis
_analysis_running: bool = False

def _set_analysis_running(state: bool) -> None:
    global _analysis_running
    _analysis_running = state

# ── Swing Cache — يُحدَّث في background كل 5 دقائق ───────────────────────────
SWING_ETF_SYMBOLS: Tuple[str, ...] = ("SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD")  # GLD follows the same Swing pipeline; AAPL/NVDA remain single-name Swing-only symbols
_swing_cache:          Dict[str, Any] = {}    # Swing cache keyed by symbol
_swing_cache_time:     float          = 0.0   # وقت آخر تحديث ناجح (time.time())
_swing_cache_last_ok:  str            = ""    # HH:MM لآخر تحديث ناجح
_swing_cache_err:      str            = ""    # رسالة آخر فشل
_swing_cache_loading:  bool           = True  # True حتى أول تحديث ناجح
_SWING_CACHE_TTL:      int            = 55    # v3.21: تحديث قبل حد quote-age 60s
_SWING_CACHE_MAX_AGE:  int            = 60    # v3.21: لا نفتح Swing من بيانات أقدم من 60s
_swing_cache_lock                     = threading.Lock()
_swing_update_running: bool           = False  # منع تشغيل update متوازي

def _update_swing_cache_bg() -> None:
    """يُحدِّث Swing cache في thread منفصل — لا يبطئ التحليل الرئيسي."""
    import time
    global _swing_cache, _swing_cache_time, _swing_cache_last_ok
    global _swing_cache_err, _swing_cache_loading, _swing_update_running

    with _swing_cache_lock:
        if _swing_update_running:
            return   # تحديث قيد التشغيل بالفعل
        _swing_update_running = True

    try:
        from core.analyzer import analyze_swing, _get_access_token
        tok = _get_access_token()
        now = datetime.now()
        new_cache = {}
        errors    = []

        for sym in SWING_ETF_SYMBOLS:
            try:
                new_cache[sym] = analyze_swing(tok, sym, now)
            except Exception as _e:
                errors.append(f"{sym}: {_e}")
                # احتفظ بالنتيجة القديمة إذا وجدت — لا تمسحها
                if sym in _swing_cache:
                    new_cache[sym] = _swing_cache[sym]

        success_time = now.strftime("%H:%M")
        with _swing_cache_lock:
            _swing_cache         = new_cache
            _swing_cache_time    = time.time()
            _swing_cache_loading = False
            if errors:
                _swing_cache_err = f"تحديث جزئي {success_time}: {' | '.join(errors)}"
                print(f"[swing_cache] ⚠️ Swing cache update failed: {_swing_cache_err}")
            else:
                _swing_cache_last_ok = success_time
                _swing_cache_err     = ""
                print(f"[swing_cache] ✅ تحديث ناجح {success_time} — "
                      " | ".join(f"{_s}={new_cache.get(_s,{}).get('qualified')}" for _s in SWING_ETF_SYMBOLS))
    except Exception as _e:
        err_msg = str(_e)
        with _swing_cache_lock:
            _swing_cache_err    = f"فشل كامل: {err_msg}"
            # RC15i.4: clear loading flag even on total failure so age-stale guard
            # takes over instead of blocking Swing entries with "جاري التحميل" forever.
            _swing_cache_loading = False
        print(f"[swing_cache] ❌ Swing cache update failed: {err_msg}")
        print(f"[swing_cache]    Last successful update: {_swing_cache_last_ok or 'لم يتم بعد'}")
    finally:
        with _swing_cache_lock:
            _swing_update_running = False


def get_swing_cache() -> Dict[str, Any]:
    """
    يعيد Swing cache مع معلومات الحالة.
    يُطلق تحديثاً في background إذا انتهت الصلاحية.
    """
    import time
    age = time.time() - _swing_cache_time

    if age > _SWING_CACHE_TTL and not _swing_update_running:
        threading.Thread(target=_update_swing_cache_bg, daemon=True).start()

    return {
        "data":         dict(_swing_cache),
        "age_seconds":  round(age),
        "last_ok":      _swing_cache_last_ok,
        "error":        _swing_cache_err,
        "loading":      _swing_cache_loading,
        "stale":        age > _SWING_CACHE_MAX_AGE,
    }


def _swing_cache_age_ok() -> tuple:
    """
    يتحقق هل Cache صالح لفتح صفقة Swing.
    يعيد (ok: bool, reason: str)
    """
    import time
    age = time.time() - _swing_cache_time
    if _swing_cache_loading:
        return False, "Swing cache: جاري التحميل — انتظر أول تحديث"
    if age > _SWING_CACHE_MAX_AGE:
        mins = round(age / 60, 1)
        return False, f"Swing rejected: cache stale ({mins} دقيقة) — لن يُفتح بيانات قديمة"
    return True, ""

from core.database import (
    get_open_trades, close_open_trade,
    open_trade_exists_today, save_trade, get_open_trades_count, get_setting,
    update_open_trade_live, get_open_count_for_strategy, get_open_count_for_symbol,
    log_mode_performance,
)
from core.telegram_bot import send_message
from core.bot_logger import (
    log_cycle_start, log_cycle_end, log_signal, log_rejection,
    log_trade_open, log_trade_close, log_price_refresh,
    log_error, log_warning, log_market_closed, log_info,
    wd_beat, wd_error,
)

# ── ضوابط الدخول ──────────────────────────────────────────────────────────────
MIN_SCORE_AUTO    = 50       # حد أدنى لتسجيل الصفقة الورقية


def _record_paper_insert_status(analysis: Dict, symbol: str, strategy: str = "?", score: float = 0,
                                selected: bool = False, insert_attempted: bool = False,
                                inserted: bool = False, inserted_trade_id: Any = None,
                                block_reason: str = "", final_action: str = "",
                                trade_mode: str = "0DTE") -> None:
    """
    RC12d UI diagnostics: preserve the exact reason a selected/qualified setup
    was or was not inserted into paper_trades. This is diagnostics only.
    """
    try:
        analysis.setdefault("_paper_insert_status", []).append({
            "symbol": str(symbol or "").upper(),
            "strategy": strategy or "?",
            "score": float(score or 0),
            "selected": bool(selected),
            "insert_attempted": bool(insert_attempted),
            "inserted": bool(inserted),
            "inserted_trade_id": inserted_trade_id,
            "block_reason": block_reason or "",
            "final_action": final_action or "",
            "trade_mode": trade_mode or "0DTE",
            "timestamp_et": _et_now().strftime("%Y-%m-%d %H:%M:%S"),
        })
    except Exception:
        pass
PROFIT_TARGET_PCT = 0.50     # 0DTE: إغلاق عند 50% ربح فقط
DEBIT_STOP_PCT    = -0.50     # v3.33.4: 0DTE Debit Spreads stop loss عند -50%
# RC15c: tolerance for option-spread TP checks. Tastytrade often shows a mid/limit
# target that can differ by 0.01-0.02 from the executable/natural close value.
# This does not change the strategy target; it prevents missing a TP due to quote rounding.
TP_HIT_TOLERANCE = 0.02
SWING_PROFIT_PCT  = 0.60      # Deprecated for RC13: لا يُستخدم لإغلاق Swing
SWING_STOP_PCT    = -0.60     # Deprecated for RC13: لا يُستخدم لإغلاق Swing

# v3.33.6a RC7 — SPY/QQQ/IWM 0DTE Intraday EMA Alignment + Exposure Cap
# خاص بـ SPY/QQQ/IWM 0DTE فقط. لا يغير Score/Delta/IV/GEX/Credit-Width/Liquidity.
SPY_QQQ_IWM_0DTE_EMA_ALIGNMENT_ENABLED = True
MAX_OPEN_SAME_SYMBOL_DIRECTION_0DTE = 2
_INTRADAY_EMA_ALIGNMENT_CACHE_TTL_SEC = 60
_INTRADAY_EMA_ALIGNMENT_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_INTRADAY_EMA_ALIGNMENT_CACHE_LOCK = threading.Lock()

# v3.33.6a RC11 — Light Re-entry Control for SPY/QQQ/IWM 0DTE only.
# هدفه منع الدخول المتأخر داخل نفس الحركة، بدون تغيير Score/Delta/IV/GEX/Credit/Liquidity.
INTRADAY_SIGNAL_AGE_REQUIRE_FRESH_MINUTES = 30
# RC11b: if EMA alignment disappears and later returns, reset signal age instead of
# continuing from an old first-detected time. 10 minutes = two completed 5m bars.
INTRADAY_ALIGNMENT_RESET_GAP_MINUTES = 10
FRESH_5M_BREAK_LOOKBACK_CANDLES = 5
FRESH_5M_BREAK_BUFFER_PCT = 0.03   # 0.03% buffer beyond the recent 5m swing low/high
MARKET_CLOSE_ET   = (15, 30)  # 0DTE: إغلاق إجباري قبل نهاية التداول بنصف ساعة


# ── Execution Quality Gate v3.21 ──────────────────────────────────────────────
# هذه القواعد تمنع تسجيل Paper Trade إذا كانت أسعار الأرجل غير واقعية.
QUOTE_AGE_MAX_SECONDS_0DTE  = 60
QUOTE_AGE_MAX_SECONDS_SWING = 60
REQUIRE_COMPLETE_BID_ASK    = True
MAX_BID_ASK_SPREAD_PCT_0DTE = 20.0
MAX_BID_ASK_SPREAD_PCT_SWING = 25.0
REALISTIC_LIQUIDITY_GATE    = True

# ── وضع Swing ─────────────────────────────────────────────────────────────────
# للتنفيذ الحقيقي يجب تغيير الاثنين معاً وإلا يبقى Paper:
#   SWING_PAPER_ONLY = False
#   ENABLE_LIVE_SWING = True
#
# هذا الشرط المزدوج يمنع التفعيل العرضي بتغيير متغير واحد فقط.
SWING_PAPER_ONLY  = True   # مرحلة الاختبار — لا تغيّر وحده
ENABLE_LIVE_SWING = False  # تأكيد ثانٍ — يجب تغييره معاً مع ما فوقه


# ── v3.33.4 — 0DTE Debit Risk Control ────────────────────────────────────────
SPX_0DTE_NO_NEW_ENTRY_MINUTES_TO_CLOSE = 180   # لا فتح SPX 0DTE جديد آخر 3 ساعات
SPX_0DTE_DEBIT_MAX_DEBIT_WIDTH_RATIO   = 0.35  # debit / width <= 35%
MAX_DAILY_SPX_PUT_DEBIT_SPREADS        = 3     # حد يومي لصفقات SPX Put Debit Spread
EOD_SOFT_EXIT_ET                       = (15, 0)   # بعد 15:00 إذا P&L <= 0
EOD_FORCE_DEBIT_EXIT_ET                = (15, 20)  # v3.33.6: بعد 15:20 أغلق أي 0DTE Debit لم يصل الهدف على SPX/SPY/QQQ/IWM

# ── v3.33.5 — Intraday Reversal Guard (EMA20 + ROC4) ───────────────────────
# الهدف: منع 0DTE Debit من الدخول عكس الحركة اللحظية الحالية.
# لا يغيّر Score/Delta/GEX/SMC؛ فقط Final Reality Check قبل Paper Execution.
INTRADAY_GUARD_ENABLED          = True
INTRADAY_GUARD_ROC_LOOKBACK_MIN = 20      # ROC4 على 5m = آخر 20 دقيقة
INTRADAY_GUARD_ROC_THRESHOLD    = 0.10    # +/-0.10%
INTRADAY_GUARD_MIN_AGE_MIN      = 15      # لا نمنع حتى يوجد تاريخ قريب من 20 دقيقة
INTRADAY_GUARD_SAMPLE_MIN_SEC   = 55      # عينة سعر واحدة تقريباً كل دقيقة
_INTRADAY_PRICE_HISTORY: Dict[str, List[Tuple[datetime, float]]] = {}
_INTRADAY_LOCK = threading.Lock()


def resolve_trade_mode(strat=None, analysis=None) -> str:
    """مصدر واحد لتحديد trade_mode — trade_mode الصريح يسبق selected_mode."""
    strat    = strat    or {}
    analysis = analysis or {}
    return (
        strat.get("trade_mode")
        or strat.get("selected_mode")
        or analysis.get("selected_trade_mode")
        or analysis.get("trade_mode")
        or "0DTE"
    )


def resolve_expiry_date(strat=None, analysis=None) -> str:
    """مصدر واحد لتحديد expiry_date — يقرأ من كل المصادر بالأولوية الصحيحة."""
    strat    = strat    or {}
    analysis = analysis or {}
    return (
        strat.get("expiry_date")
        or analysis.get("expiry_date")
        or analysis.get("selected_expiry_date")
        or ""
    )


def _dte_remaining(expiry_date: str) -> int:
    """أيام متبقية حتى انتهاء العقد."""
    try:
        from datetime import date
        exp = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        return (exp - datetime.now().date()).days
    except Exception:
        return 99


def _swing_expiry(dte: int = 8) -> str:
    """يحسب تاريخ انتهاء Swing (أقرب يوم جمعة بعد N أيام)."""
    from datetime import date, timedelta
    today = date.today()
    target = today + timedelta(days=dte)
    # إذا لم يكن جمعة، اذهب لأقرب جمعة
    days_to_friday = (4 - target.weekday()) % 7
    expiry = target + timedelta(days=days_to_friday)
    return expiry.strftime("%Y-%m-%d")


def _parse_trade_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value[:19], fmt)
        except Exception:
            pass
    return None


def _trade_age_minutes(t: Dict) -> Optional[float]:
    ts = (
        _parse_trade_timestamp(t.get("created_at"))
        or _parse_trade_timestamp(t.get("timestamp"))
    )
    if not ts:
        entry_date = t.get("entry_date")
        entry_time = t.get("entry_time")
        ts = _parse_trade_timestamp(f"{entry_date} {entry_time}:00") if entry_date and entry_time else None
    if not ts:
        return None
    return max((datetime.now() - ts).total_seconds() / 60.0, 0.0)


def _swing_stop_allowed(t: Dict, pnl_pct: float, min_minutes: int = 45) -> bool:
    age = _trade_age_minutes(t)
    if age is not None and age < min_minutes:
        print(
            f"[swing_guard] skip SL #{t.get('id','?')}: "
            f"age={age:.1f}m pnl={pnl_pct:.1f}%"
        )
        return False
    return True


def _et_now() -> datetime:
    now_utc = datetime.now(timezone.utc)
    is_edt  = 3 < now_utc.month < 11 or (now_utc.month == 3 and now_utc.day >= 8) \
              or (now_utc.month == 11 and now_utc.day < 8)
    return now_utc + timedelta(hours=-4 if is_edt else -5)


def _is_near_close() -> bool:
    """True من 15:30 ET وأي وقت بعد إغلاق السوق — يعالج حالة عدم تشغيل البوت عند 15:30 بالضبط."""
    et = _et_now()
    if et.weekday() >= 5:
        return False
    return (et.hour == MARKET_CLOSE_ET[0] and et.minute >= MARKET_CLOSE_ET[1]) or et.hour >= 16


def _is_market_open() -> bool:
    et = _et_now()
    if et.weekday() >= 5:
        return False
    return (9, 30) <= (et.hour, et.minute) < (16, 0)




def _minutes_to_market_close() -> Optional[int]:
    """عدد الدقائق المتبقية حتى إغلاق السوق الأمريكي 16:00 ET."""
    try:
        et = _et_now()
        close_dt = et.replace(hour=16, minute=0, second=0, microsecond=0)
        return int((close_dt - et).total_seconds() // 60)
    except Exception:
        return None


def _et_at_or_after(hour: int, minute: int = 0) -> bool:
    try:
        et = _et_now()
        return (et.hour, et.minute) >= (hour, minute)
    except Exception:
        return False


def _is_0dte_debit_strategy(strategy: str) -> bool:
    return str(strategy or "") in ("Put Debit Spread", "Call Debit Spread")


def _spread_width_from_strategy(strat: Dict, legs: Dict = None) -> Optional[float]:
    """عرض السبريد من strikes سواء من strat أو legs."""
    try:
        sp = strat.get("short_put")
        lp = strat.get("long_put")
        sc = strat.get("short_call")
        lc = strat.get("long_call")
        if sp is not None and lp is not None:
            return abs(float(sp) - float(lp))
        if sc is not None and lc is not None:
            return abs(float(lc) - float(sc))
        legs = legs or {}
        if legs.get("short_put") is not None and legs.get("long_put") is not None:
            return abs(float(legs.get("short_put")) - float(legs.get("long_put")))
        if legs.get("short_call") is not None and legs.get("long_call") is not None:
            return abs(float(legs.get("long_call")) - float(legs.get("short_call")))
    except Exception:
        return None
    return None


def _count_daily_spx_put_debit_paper_trades() -> int:
    """عدد صفقات SPX 0DTE Put Debit Spread المسجلة اليوم بتوقيت نيويورك، مفتوحة أو مغلقة."""
    try:
        from core.database import get_connection
        ny_date = _et_now().strftime("%Y-%m-%d")
        with get_connection() as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()}
            if "entry_time_ny" in cols:
                row = conn.execute(
                    """SELECT COUNT(*) AS n FROM paper_trades
                       WHERE symbol='SPX' AND selected_mode='0DTE' AND strategy='Put Debit Spread'
                         AND substr(entry_time_ny,1,10)=?""",
                    (ny_date,)
                ).fetchone()
            else:
                row = conn.execute(
                    """SELECT COUNT(*) AS n FROM paper_trades
                       WHERE symbol='SPX' AND selected_mode='0DTE' AND strategy='Put Debit Spread'
                         AND date(timestamp)=date(?)""",
                    (ny_date,)
                ).fetchone()
            return int(row["n"] if row else 0)
    except Exception as e:
        print(f"[risk_control] daily SPX Put Debit count error: {e}")
        return 0



def _record_intraday_price(symbol: str, price: float) -> None:
    """Store a lightweight intraday price sample for ROC/EMA diagnostics.

    مصدر السعر هو نفس سعر البوت من DXLink/Tastytrade. لا نعتمد على TradingView.
    """
    try:
        sym = str(symbol or "").upper().strip()
        px = float(price or 0)
        if sym not in ("SPX", "SPY", "QQQ") or px <= 0:
            return
        now = _et_now()
        with _INTRADAY_LOCK:
            hist = _INTRADAY_PRICE_HISTORY.setdefault(sym, [])
            if hist and (now - hist[-1][0]).total_seconds() < INTRADAY_GUARD_SAMPLE_MIN_SEC:
                hist[-1] = (now, px)
            else:
                hist.append((now, px))
            cutoff = now - timedelta(hours=8)
            _INTRADAY_PRICE_HISTORY[sym] = [(t, v) for t, v in hist if t >= cutoff]
    except Exception as e:
        print(f"[intraday_guard] record error {symbol}: {e}")


def _ema_from_values(values: List[float], period: int = 20) -> Optional[float]:
    try:
        vals = [float(v) for v in values if v is not None and float(v) > 0]
        if len(vals) < 2:
            return None
        vals = vals[-period:]
        k = 2 / (period + 1)
        ema = vals[0]
        for v in vals[1:]:
            ema = v * k + ema * (1 - k)
        return float(ema)
    except Exception:
        return None


def _get_symbol_analysis_data(analysis: Dict, symbol: str) -> Dict:
    sym = str(symbol or "").upper()
    if sym == "SPX":
        return analysis or {}
    return (analysis or {}).get(sym.lower()) or {}


def _extract_intraday_ema20(analysis: Dict, symbol: str) -> Tuple[Optional[float], str]:
    """أفضل EMA متاح للـ guard: EMA intraday من التحليل، ثم internal history."""
    try:
        data = _get_symbol_analysis_data(analysis, symbol)
        levels = data.get("levels") or {}
        for key, src in (("ema20_15m", "analysis_ema20_15m"),
                         ("ema20", "analysis_ema20")):
            val = data.get(key) or levels.get(key)
            try:
                if val is not None and float(val) > 0:
                    return float(val), src
            except Exception:
                pass
        sym = str(symbol or "").upper()
        with _INTRADAY_LOCK:
            hist = list(_INTRADAY_PRICE_HISTORY.get(sym, []))
        ema = _ema_from_values([v for _, v in hist], 20)
        if ema:
            return ema, "internal_price_history"
    except Exception as e:
        print(f"[intraday_guard] ema extract error {symbol}: {e}")
    return None, "unavailable"


def _intraday_roc20(symbol: str, current_price: float) -> Tuple[Optional[float], str]:
    """Return ROC over about 20 minutes using the bot's stored price samples."""
    try:
        sym = str(symbol or "").upper()
        now = _et_now()
        px = float(current_price or 0)
        if px <= 0:
            return None, "no_current_price"
        with _INTRADAY_LOCK:
            hist = list(_INTRADAY_PRICE_HISTORY.get(sym, []))
        if not hist:
            return None, "no_price_history"
        target = now - timedelta(minutes=INTRADAY_GUARD_ROC_LOOKBACK_MIN)
        older = [(t, v) for t, v in hist if t <= target and v and v > 0]
        if older:
            base_t, base_px = older[-1]
        else:
            base_t, base_px = hist[0]
            age_min = (now - base_t).total_seconds() / 60.0
            if age_min < INTRADAY_GUARD_MIN_AGE_MIN:
                return None, f"insufficient_history_{age_min:.1f}m/{INTRADAY_GUARD_ROC_LOOKBACK_MIN}m"
        base_px = float(base_px or 0)
        if base_px <= 0:
            return None, "bad_base_price"
        roc = (px - base_px) / base_px * 100.0
        age_min = (now - base_t).total_seconds() / 60.0
        return round(roc, 3), f"base_age={age_min:.1f}m base={base_px:.2f}"
    except Exception as e:
        return None, f"roc_error:{e}"


def _intraday_reversal_guard(analysis: Dict, symbol: str, strategy: str,
                             price: float, trade_mode: str) -> Tuple[bool, str, Dict[str, Any]]:
    """Final Reality Check قبل Paper Execution.

    blocked=True يعني لا تسجل الصفقة الورقية.
    القاعدة v1 بدون VWAP: EMA20 + ROC20 من بيانات البوت.
    """
    diag: Dict[str, Any] = {
        "symbol": symbol, "strategy": strategy, "trade_mode": trade_mode,
        "enabled": INTRADAY_GUARD_ENABLED,
    }
    try:
        if not INTRADAY_GUARD_ENABLED:
            return False, "intraday_guard_disabled", diag
        sym = str(symbol or "").upper()
        name = str(strategy or "")
        if sym not in ("SPX", "SPY", "QQQ"):
            return False, "symbol_not_guarded", diag
        if str(trade_mode or "") != "0DTE" or not _is_0dte_debit_strategy(name):
            return False, "not_0dte_debit", diag
        px = float(price or 0)
        if px <= 0:
            return False, "no_price_guard_pass", diag

        _record_intraday_price(sym, px)
        ema20, ema_src = _extract_intraday_ema20(analysis, sym)
        roc20, roc_src = _intraday_roc20(sym, px)
        diag.update({
            "price": round(px, 2),
            "ema20": round(ema20, 2) if ema20 else None,
            "ema_source": ema_src,
            "roc20_pct": roc20,
            "roc_source": roc_src,
            "roc_threshold": INTRADAY_GUARD_ROC_THRESHOLD,
        })

        if ema20 is None or roc20 is None:
            return False, f"intraday_guard_pass_unavailable ema={ema_src} roc={roc_src}", diag

        if name == "Put Debit Spread" and px > ema20 and roc20 > INTRADAY_GUARD_ROC_THRESHOLD:
            reason = (f"intraday_bullish_reversal: price={px:.2f} > EMA20={ema20:.2f} "
                      f"and ROC20={roc20:+.2f}% > +{INTRADAY_GUARD_ROC_THRESHOLD:.2f}%")
            return True, reason, diag

        if name == "Call Debit Spread" and px < ema20 and roc20 < -INTRADAY_GUARD_ROC_THRESHOLD:
            reason = (f"intraday_bearish_reversal: price={px:.2f} < EMA20={ema20:.2f} "
                      f"and ROC20={roc20:+.2f}% < -{INTRADAY_GUARD_ROC_THRESHOLD:.2f}%")
            return True, reason, diag

        return False, (f"intraday_guard_pass: price={px:.2f} EMA20={ema20:.2f} "
                       f"ROC20={roc20:+.2f}%"), diag
    except Exception as e:
        diag["error"] = str(e)
        return False, f"intraday_guard_error_pass:{e}", diag


def _strategy_direction(strategy: str) -> Optional[str]:
    """Return bullish/bearish for directional option strategies."""
    name = str(strategy or "")
    if name in ("Put Debit Spread", "Bear Call Spread"):
        return "bearish"
    if name in ("Call Debit Spread", "Bull Put Spread"):
        return "bullish"
    return None


def _fmt_pct(v: Optional[float]) -> str:
    try:
        return f"{float(v):+.2f}%"
    except Exception:
        return "N/A"


def _ema_alignment_from_closes(symbol: str, period: str, closes: List[float], price: float) -> Dict[str, Any]:
    """Compute EMA20/EMA50 alignment diagnostics from candle closes."""
    vals = [float(x) for x in closes if x is not None and float(x) > 0]
    out: Dict[str, Any] = {"period": period, "bars": len(vals), "source": "DXLink Candle"}
    if len(vals) < 50:
        out.update({"ok": False, "reason": f"insufficient_{period}_bars {len(vals)}/50"})
        return out
    ema20 = _ema_from_values(vals, 20)
    ema50 = _ema_from_values(vals, 50)
    px = float(price or 0)
    if not ema20 or not ema50 or px <= 0:
        out.update({"ok": False, "reason": f"bad_{period}_ema_or_price"})
        return out
    p20 = (px - ema20) / ema20 * 100.0
    p50 = (px - ema50) / ema50 * 100.0
    e2050 = (ema20 - ema50) / ema50 * 100.0
    if px < ema20 and ema20 < ema50:
        bias = "bearish"
        reason = f"Price < EMA20_{period} < EMA50_{period}"
    elif px > ema20 and ema20 > ema50:
        bias = "bullish"
        reason = f"Price > EMA20_{period} > EMA50_{period}"
    else:
        bias = "mixed"
        if px < ema20 and px < ema50 and ema20 > ema50:
            reason = f"bearish pressure on {period}: price below EMA20/EMA50 but EMA20 still above EMA50"
        elif px > ema20 and px > ema50 and ema20 < ema50:
            reason = f"bullish pressure on {period}: price above EMA20/EMA50 but EMA20 still below EMA50"
        else:
            reason = f"mixed EMA order on {period}"
    out.update({
        "ok": True,
        "bias": bias,
        "ema20": round(ema20, 4),
        "ema50": round(ema50, 4),
        "price_vs_ema20_pct": round(p20, 3),
        "price_vs_ema50_pct": round(p50, 3),
        "ema20_vs_ema50_pct": round(e2050, 3),
        "reason": reason,
    })
    return out



def _completed_candles(candles: List[Dict[str, Any]], period_minutes: int = 5) -> List[Dict[str, Any]]:
    """Return candles that are probably completed; exclude the currently forming candle when needed."""
    vals: List[Dict[str, Any]] = []
    for c in candles or []:
        try:
            if c.get("time") is None or c.get("close") is None:
                continue
            vals.append({
                "time": int(c.get("time")),
                "open": float(c.get("open") or c.get("close") or 0),
                "high": float(c.get("high") or c.get("close") or 0),
                "low": float(c.get("low") or c.get("close") or 0),
                "close": float(c.get("close") or 0),
            })
        except Exception:
            continue
    vals = sorted(vals, key=lambda x: x["time"])
    if not vals:
        return vals
    try:
        period_ms = int(period_minutes * 60 * 1000)
        now_ms = int(time.time() * 1000)
        # DXLink candle time is treated as the candle start time. If the latest candle is still forming,
        # use the previous candle for a true 5m close-based breakdown/breakout decision.
        if now_ms - int(vals[-1]["time"]) < period_ms:
            vals = vals[:-1]
    except Exception:
        pass
    return vals


def _fresh_5m_break_diagnostics(candles5: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Detect a fresh close-based 5m breakdown/breakout using completed candles only.

    bearish fresh breakdown:
        latest completed 5m close < lowest low of previous N completed 5m candles - buffer
    bullish fresh breakout:
        latest completed 5m close > highest high of previous N completed 5m candles + buffer
    """
    out: Dict[str, Any] = {
        "fresh_break_source": "DXLink Candle 5m completed-close",
        "fresh_break_lookback_candles": FRESH_5M_BREAK_LOOKBACK_CANDLES,
        "fresh_break_buffer_pct": FRESH_5M_BREAK_BUFFER_PCT,
        "fresh_bearish_breakdown": False,
        "fresh_bullish_breakout": False,
        "fresh_break_reason": "not_evaluated",
    }
    completed = _completed_candles(candles5, 5)
    out["completed_5m_bars_for_fresh_break"] = len(completed)
    need = FRESH_5M_BREAK_LOOKBACK_CANDLES + 1
    if len(completed) < need:
        out["fresh_break_reason"] = f"insufficient_completed_5m_bars {len(completed)}/{need}"
        return out
    latest = completed[-1]
    prior = completed[-(FRESH_5M_BREAK_LOOKBACK_CANDLES + 1):-1]
    try:
        latest_close = float(latest["close"])
        recent_low = min(float(c["low"]) for c in prior)
        recent_high = max(float(c["high"]) for c in prior)
        buffer_points = max(latest_close * (FRESH_5M_BREAK_BUFFER_PCT / 100.0), 0.01)
        bearish_threshold = recent_low - buffer_points
        bullish_threshold = recent_high + buffer_points
        fresh_bearish = latest_close < bearish_threshold
        fresh_bullish = latest_close > bullish_threshold
        out.update({
            "latest_completed_5m_close": round(latest_close, 4),
            "latest_completed_5m_high": round(float(latest["high"]), 4),
            "latest_completed_5m_low": round(float(latest["low"]), 4),
            "latest_completed_5m_time_ms": int(latest["time"]),
            "recent_5m_swing_low": round(recent_low, 4),
            "recent_5m_swing_high": round(recent_high, 4),
            "fresh_break_buffer_points": round(buffer_points, 4),
            "bearish_break_threshold": round(bearish_threshold, 4),
            "bullish_break_threshold": round(bullish_threshold, 4),
            "fresh_bearish_breakdown": bool(fresh_bearish),
            "fresh_bullish_breakout": bool(fresh_bullish),
        })
        if fresh_bearish:
            out["fresh_break_reason"] = "fresh_bearish_5m_close_breakdown"
        elif fresh_bullish:
            out["fresh_break_reason"] = "fresh_bullish_5m_close_breakout"
        else:
            out["fresh_break_reason"] = "no_fresh_5m_close_breakout_or_breakdown"
    except Exception as e:
        out["fresh_break_reason"] = f"fresh_break_error:{e}"
    return out


def _format_signal_age_local(first_time: str, current_time: str) -> str:
    """Public-local RC11b helper for signal age formatting.

    Avoids importing the private database._format_signal_age helper into trade_monitor.py.
    """
    try:
        fmt = "%Y-%m-%d %H:%M:%S"
        a = datetime.strptime(str(first_time), fmt)
        b = datetime.strptime(str(current_time), fmt)
        minutes = max(0, int((b - a).total_seconds() // 60))
        h, m = divmod(minutes, 60)
        if h and m:
            return f"{h}h {m}m"
        if h:
            return f"{h}h"
        return f"{m}m"
    except Exception:
        return "0m"


def _parse_dt_et(text: Any) -> Optional[datetime]:
    try:
        if not text:
            return None
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _parse_signal_age_minutes(text: Any) -> float:
    try:
        raw = str(text or "0m").strip().lower()
        if not raw:
            return 0.0
        total = 0.0
        parts = raw.replace("hours", "h").replace("hour", "h").replace("minutes", "m").replace("minute", "m").split()
        for part in parts:
            if part.endswith("h"):
                total += float(part[:-1] or 0) * 60.0
            elif part.endswith("m"):
                total += float(part[:-1] or 0)
        if total == 0.0 and raw.endswith("m"):
            total = float(raw[:-1] or 0)
        return max(total, 0.0)
    except Exception:
        return 0.0


def _update_intraday_alignment_signal_age(symbol: str, direction: str) -> Dict[str, Any]:
    """Track continuous same-symbol same-direction EMA alignment age.

    RC11b behavior:
    - The age is continuous only while the alignment keeps being seen.
    - If alignment disappeared and later returned after INTRADAY_ALIGNMENT_RESET_GAP_MINUTES,
      first_detected_time is reset to now. This prevents stale age from an old trend window.
    """
    out: Dict[str, Any] = {"alignment_signal_age_minutes": 0.0, "alignment_signal_age": "0m"}
    try:
        from core.database import get_connection
        sym = str(symbol or "").upper().strip()
        direction = str(direction or "").lower().strip()
        now_dt = _et_now().replace(tzinfo=None)
        now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        today = now_str[:10]
        key = f"INTRADAY_EMA_ALIGNMENT|{sym}|0DTE|{direction}"
        reset_reason = "new_or_new_day"
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM signal_tracking WHERE signal_key=?", (key,)).fetchone()
            if row and str(row["first_detected_time"] or "")[:10] == today:
                first_time = row["first_detected_time"]
                last_seen = row["last_seen_time"]
                last_seen_dt = _parse_dt_et(last_seen)
                gap_minutes = 0.0
                if last_seen_dt is not None:
                    gap_minutes = max(0.0, (now_dt - last_seen_dt).total_seconds() / 60.0)
                # If the alignment was not seen recently, it likely disappeared and returned.
                # Reset the first-detected time so Signal Age reflects the current alignment leg.
                if last_seen_dt is None or gap_minutes > float(INTRADAY_ALIGNMENT_RESET_GAP_MINUTES):
                    first_time = now_str
                    reset_reason = f"reset_after_alignment_gap_{gap_minutes:.1f}m"
                    conn.execute("""
                        UPDATE signal_tracking
                        SET first_detected_time=?, last_seen_time=?, current_score=?, current_price=?
                        WHERE signal_key=?
                    """, (now_str, now_str, 0, 0, key))
                else:
                    reset_reason = f"continuous_alignment_gap_{gap_minutes:.1f}m"
                    conn.execute("UPDATE signal_tracking SET last_seen_time=?, current_score=?, current_price=? WHERE signal_key=?",
                                 (now_str, 0, 0, key))
            else:
                first_time = now_str
                conn.execute("""
                    INSERT OR REPLACE INTO signal_tracking
                    (signal_key, symbol, selected_mode, strategy, first_detected_time, last_seen_time,
                     initial_score, current_score, peak_score, price_first_detected, current_price)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """, (key, sym, "0DTE", f"EMA_ALIGNMENT_{direction}", now_str, now_str, 0, 0, 0, 0, 0))
            conn.commit()
        age_text = _format_signal_age_local(first_time, now_str)
        out.update({
            "alignment_signal_key": key,
            "alignment_first_detected_time": first_time,
            "alignment_current_signal_time": now_str,
            "alignment_signal_age": age_text,
            "alignment_signal_age_minutes": _parse_signal_age_minutes(age_text),
            "alignment_age_reset_gap_minutes": INTRADAY_ALIGNMENT_RESET_GAP_MINUTES,
            "alignment_age_reset_reason": reset_reason,
        })
    except Exception as e:
        out["alignment_signal_age_error"] = str(e)
    return out


def _latest_closed_same_symbol_direction_0dte(symbol: str, direction: str) -> Dict[str, Any]:
    """Return latest closed same-symbol/same-direction 0DTE paper trade today, if any."""
    out: Dict[str, Any] = {"previous_same_direction_closed_today": False}
    try:
        sym = str(symbol or "").upper().strip()
        direction = str(direction or "").lower().strip()
        if direction == "bearish":
            strategies = ("Put Debit Spread", "Bear Call Spread")
        elif direction == "bullish":
            strategies = ("Call Debit Spread", "Bull Put Spread")
        else:
            return out
        from core.database import get_connection
        placeholders = ",".join("?" for _ in strategies)
        today = _et_now().strftime("%Y-%m-%d")
        with get_connection() as conn:
            row = conn.execute(
                f"""SELECT id, timestamp, exit_date, last_updated, close_reason, profit_pct
                    FROM paper_trades
                    WHERE status='closed'
                      AND symbol=?
                      AND selected_mode='0DTE'
                      AND strategy IN ({placeholders})
                      AND date(timestamp)=date(?)
                    ORDER BY id DESC LIMIT 1""",
                (sym, *strategies, today),
            ).fetchone()
        if row:
            out.update({
                "previous_same_direction_closed_today": True,
                "previous_same_direction_trade_id": row["id"],
                "previous_same_direction_timestamp": row["timestamp"],
                "previous_same_direction_exit_date": row["exit_date"],
                "previous_same_direction_last_updated": row["last_updated"],
                "previous_same_direction_close_reason": row["close_reason"],
                "previous_same_direction_profit_pct": row["profit_pct"],
            })
    except Exception as e:
        out["previous_same_direction_error"] = str(e)
    return out


def _spy_qqq_iwm_light_reentry_guard(symbol: str, direction: str, ema_diag: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """RC11 guard: signal age + fresh 5m close breakdown/breakout + max exposure support.

    It does not replace EMA alignment. It only applies after EMA alignment already passed.
    """
    diag: Dict[str, Any] = {"symbol": symbol, "direction": direction, "rc11_light_reentry_control": True}
    try:
        sym = str(symbol or "").upper().strip()
        direction = str(direction or "").lower().strip()
        if sym not in ("SPY", "QQQ", "IWM") or direction not in ("bearish", "bullish"):
            return False, "rc11_reentry_not_applicable", diag
        age_diag = _update_intraday_alignment_signal_age(sym, direction)
        prev_diag = _latest_closed_same_symbol_direction_0dte(sym, direction)
        diag.update(age_diag)
        diag.update(prev_diag)
        age_minutes = float(age_diag.get("alignment_signal_age_minutes") or 0.0)
        stale = age_minutes > float(INTRADAY_SIGNAL_AGE_REQUIRE_FRESH_MINUTES)
        previous_closed = bool(prev_diag.get("previous_same_direction_closed_today"))
        require_fresh = bool(stale or previous_closed)
        diag["fresh_break_required"] = require_fresh
        diag["fresh_break_required_due_to_signal_age"] = bool(stale)
        diag["fresh_break_required_due_to_previous_closed_trade"] = bool(previous_closed)
        diag["fresh_break_required_after_minutes"] = INTRADAY_SIGNAL_AGE_REQUIRE_FRESH_MINUTES
        if not require_fresh:
            return False, (f"rc11_reentry_pass: fresh_break_not_required; "
                           f"signal_age={age_minutes:.1f}m <= {INTRADAY_SIGNAL_AGE_REQUIRE_FRESH_MINUTES}m; "
                           f"no previous same-direction closed trade today"), diag
        if direction == "bearish":
            fresh = bool(ema_diag.get("fresh_bearish_breakdown"))
            reason_code = "stale_bearish_signal_no_fresh_breakdown" if stale else "reentry_requires_fresh_breakdown"
            pass_code = "fresh_bearish_breakdown_confirmed"
        else:
            fresh = bool(ema_diag.get("fresh_bullish_breakout"))
            reason_code = "stale_bullish_signal_no_fresh_breakout" if stale else "reentry_requires_fresh_breakout"
            pass_code = "fresh_bullish_breakout_confirmed"
        diag.update({
            "fresh_bearish_breakdown": ema_diag.get("fresh_bearish_breakdown"),
            "fresh_bullish_breakout": ema_diag.get("fresh_bullish_breakout"),
            "fresh_break_reason": ema_diag.get("fresh_break_reason"),
            "latest_completed_5m_close": ema_diag.get("latest_completed_5m_close"),
            "recent_5m_swing_low": ema_diag.get("recent_5m_swing_low"),
            "recent_5m_swing_high": ema_diag.get("recent_5m_swing_high"),
            "fresh_break_buffer_points": ema_diag.get("fresh_break_buffer_points"),
        })
        if fresh:
            return False, (f"rc11_reentry_pass: {pass_code}; "
                           f"signal_age={age_minutes:.1f}m; previous_closed={previous_closed}; "
                           f"{ema_diag.get('fresh_break_reason')}"), diag
        reason = (f"{reason_code}: signal_age={age_minutes:.1f}m; "
                  f"previous_closed={previous_closed}; {ema_diag.get('fresh_break_reason')}")
        return True, reason, diag
    except Exception as e:
        diag["error"] = str(e)
        return True, f"rc11_reentry_guard_error_reject:{e}", diag

def _fetch_intraday_ema_alignment(symbol: str, price: float) -> Dict[str, Any]:
    """Fetch 15m and 5m DXLink candles and classify SPY/QQQ/IWM 0DTE EMA alignment.

    Cached briefly because this can open two DXLink websocket candle snapshots.
    """
    sym = str(symbol or "").upper().strip()
    px = float(price or 0)
    cache_key = f"{sym}:{round(px, 2)}"
    now = time.time()
    with _INTRADAY_EMA_ALIGNMENT_CACHE_LOCK:
        cached = _INTRADAY_EMA_ALIGNMENT_CACHE.get(cache_key)
        if cached and (now - cached[0]) <= _INTRADAY_EMA_ALIGNMENT_CACHE_TTL_SEC:
            return dict(cached[1])

    diag: Dict[str, Any] = {
        "symbol": sym,
        "price": round(px, 4) if px else None,
        "enabled": SPY_QQQ_IWM_0DTE_EMA_ALIGNMENT_ENABLED,
        "source": "DXLink/Tastytrade Candle 15m+5m",
        "intraday_ema_bias": "unavailable",
        "intraday_ema_reason": "not_evaluated",
    }
    try:
        if sym not in ("SPY", "QQQ", "IWM") or px <= 0:
            diag["intraday_ema_reason"] = "symbol_or_price_not_applicable"
            return diag
        from core.analyzer import _get_access_token
        from core.dxlink_client import fetch_dxlink_candles_snapshot
        tok = _get_access_token()
        c15 = fetch_dxlink_candles_snapshot(tok, sym, period="15m", days_back=8, timeout_seconds=14.0)
        c5 = fetch_dxlink_candles_snapshot(tok, sym, period="5m", days_back=4, timeout_seconds=14.0)
        closes15 = [c.get("close") for c in (c15 or []) if c.get("close")]
        closes5 = [c.get("close") for c in (c5 or []) if c.get("close")]
        d15 = _ema_alignment_from_closes(sym, "15m", closes15, px)
        d5 = _ema_alignment_from_closes(sym, "5m", closes5, px)
        fresh5 = _fresh_5m_break_diagnostics(c5 or [])
        diag.update(fresh5)
        diag.update({
            "ema20_15m": d15.get("ema20"),
            "ema50_15m": d15.get("ema50"),
            "ema20_5m": d5.get("ema20"),
            "ema50_5m": d5.get("ema50"),
            "price_vs_ema20_15m": d15.get("price_vs_ema20_pct"),
            "price_vs_ema20_5m": d5.get("price_vs_ema20_pct"),
            "price_vs_ema50_15m": d15.get("price_vs_ema50_pct"),
            "price_vs_ema50_5m": d5.get("price_vs_ema50_pct"),
            "ema20_vs_ema50_15m": d15.get("ema20_vs_ema50_pct"),
            "ema20_vs_ema50_5m": d5.get("ema20_vs_ema50_pct"),
            "bars_15m": d15.get("bars"),
            "bars_5m": d5.get("bars"),
            "reason_15m": d15.get("reason"),
            "reason_5m": d5.get("reason"),
        })
        if not d15.get("ok") or not d5.get("ok"):
            diag["intraday_ema_bias"] = "unavailable"
            diag["intraday_ema_reason"] = f"ema_alignment_unavailable: 15m={d15.get('reason')} | 5m={d5.get('reason')}"
        elif d15.get("bias") == "bearish" and d5.get("bias") == "bearish":
            diag["intraday_ema_bias"] = "bearish"
            diag["intraday_ema_reason"] = "15m and 5m both bearish: Price < EMA20 < EMA50"
        elif d15.get("bias") == "bullish" and d5.get("bias") == "bullish":
            diag["intraday_ema_bias"] = "bullish"
            diag["intraday_ema_reason"] = "15m and 5m both bullish: Price > EMA20 > EMA50"
        else:
            diag["intraday_ema_bias"] = "mixed"
            diag["intraday_ema_reason"] = f"15m={d15.get('bias')} | 5m={d5.get('bias')}"
    except Exception as e:
        diag["intraday_ema_bias"] = "unavailable"
        diag["intraday_ema_reason"] = f"ema_alignment_error:{e}"

    with _INTRADAY_EMA_ALIGNMENT_CACHE_LOCK:
        _INTRADAY_EMA_ALIGNMENT_CACHE[cache_key] = (time.time(), dict(diag))
    return diag


def _spy_qqq_intraday_ema_alignment_guard(
    analysis: Dict, symbol: str, strategy: str, price: float, trade_mode: str
) -> Tuple[bool, str, Dict[str, Any]]:
    """Hard confirmation filter for SPY/QQQ/IWM 0DTE directional strategies only."""
    diag: Dict[str, Any] = {"symbol": symbol, "strategy": strategy, "trade_mode": trade_mode}
    try:
        if not SPY_QQQ_IWM_0DTE_EMA_ALIGNMENT_ENABLED:
            return False, "intraday_ema_alignment_disabled", diag
        sym = str(symbol or "").upper().strip()
        if sym not in ("SPY", "QQQ", "IWM") or str(trade_mode or "") != "0DTE":
            return False, "intraday_ema_alignment_not_applicable", diag
        direction = _strategy_direction(strategy)
        if direction is None:
            return False, "intraday_ema_alignment_not_directional_strategy", diag
        ema_diag = _fetch_intraday_ema_alignment(sym, price)
        diag.update(ema_diag)
        bias = ema_diag.get("intraday_ema_bias")
        if bias != direction:
            reason = (f"intraday_ema_alignment_failed: strategy_direction={direction} "
                      f"but intraday_ema_bias={bias}; {ema_diag.get('intraday_ema_reason')}")
            return True, reason, diag
        return False, (f"intraday_ema_alignment_pass: {direction}; "
                       f"{ema_diag.get('intraday_ema_reason')}"), diag
    except Exception as e:
        diag["error"] = str(e)
        return True, f"intraday_ema_alignment_error_reject:{e}", diag



def _rc15f_final_action_from_diag(diag: Optional[Dict[str, Any]]) -> str:
    """RC15i: distinguish WATCHLIST from hard BLOCK in paper-registration diagnostics."""
    try:
        decision = str((diag or {}).get("decision") or "").strip().upper()
        if decision == "WATCHLIST":
            return "watchlist_swing_ict_smc_ema"
    except Exception:
        pass
    return "skip_swing_ict_smc_ema"

def _swing_ict_smc_ema_guard(
    analysis: Dict, strat: Dict[str, Any], symbol: str, strategy: str, price: float, trade_mode: str
) -> Tuple[bool, str, Dict[str, Any]]:
    """RC15f hard Swing-only ICT/SMC + EMA confirmation layer."""
    diag: Dict[str, Any] = {
        "symbol": str(symbol or "").upper().strip(),
        "strategy": strategy or "",
        "trade_mode": trade_mode or "",
        "rc15f_swing_ict_smc_ema": True,
    }
    try:
        if str(trade_mode or "").strip().upper() != "SWING":
            diag.update({"applied": False, "not_applicable": True, "decision": "NOT_APPLICABLE_NON_SWING"})
            return False, "rc15f_not_applicable_non_swing", diag

        # RC15f is intentionally limited to directional Swing Debit strategies.
        # Credit/range strategies (Bull Put, Bear Call, Iron Condor) need a separate
        # RC15g-style Range/IV/Sigma confirmation layer and must not be blocked by
        # missing ICT/SMC displacement data.
        if str(strategy or "").strip() not in ("Call Debit Spread", "Put Debit Spread"):
            diag.update({
                "applied": False,
                "not_applicable": True,
                "decision": "NOT_APPLICABLE_DIRECTIONAL_DEBIT_ONLY",
                "allowed": True,
                "reason": "RC15f applies only to Swing Call Debit Spread / Put Debit Spread",
            })
            try:
                strat["rc15f_swing_confirmation"] = dict(diag)
                strat["rc15f_not_applicable"] = True
            except Exception:
                pass
            return False, "rc15f_not_applicable_directional_debit_only", diag

        direction = _strategy_direction(strategy)
        if direction not in ("bullish", "bearish"):
            return True, "RC15f Swing blocked: non_directional_strategy", diag

        smc = (
            (strat or {}).get("smc_0dte_mtf_full")
            or (strat or {}).get("smc_0dte_mtf")
            or (analysis or {}).get("smc_0dte_mtf_full")
            or (analysis or {}).get("smc_0dte_mtf")
            or {}
        )
        if not isinstance(smc, dict) or not smc:
            try:
                from core.smc_0dte_mtf import analyze_0dte_smc_mtf
                smc = analyze_0dte_smc_mtf(symbol, price, strategy_name=strategy)
            except Exception as exc:
                smc = {"available": False, "reason": f"smc_fetch_error:{type(exc).__name__}:{str(exc)[:160]}"}

        h1 = smc.get("h1") if isinstance(smc.get("h1"), dict) else {}
        m15 = smc.get("m15") if isinstance(smc.get("m15"), dict) else {}
        insufficient_smc_data = (
            smc.get("available") is not True
            or smc.get("reason") in ("missing_price", "not_applicable_symbol")
            or str(smc.get("reason") or "").startswith("smc_fetch_error:")
            or str(smc.get("reason") or "").startswith("smc_mtf_diagnostics_error:")
            or h1.get("available") is not True
            or m15.get("available") is not True
        )
        if insufficient_smc_data:
            ict_details = smc.get("ict_smc_details") if isinstance(smc.get("ict_smc_details"), dict) else {}
            ema_details = smc.get("ema_details") if isinstance(smc.get("ema_details"), dict) else {}
            # RC15i: missing/failed DXLink 1H or 15m is a data sufficiency block,
            # not an EMA alignment failure. Yahoo remains diagnostic only and is not used here.
            source_reason = str(smc.get("reason") or "")
            h1_reason = h1.get("reason")
            m15_reason = m15.get("reason")
            data_reason = "insufficient_dxlink_candles"
            ict_details = {
                **ict_details,
                "score": 0,
                "pass": False,
                "reason": data_reason,
                "h1_reason": h1_reason,
                "m15_reason": m15_reason,
                "source_reason": source_reason,
            }
            ema_details = {
                **ema_details,
                "pass": False,
                "bias": "unavailable",
                "reason": data_reason,
            }
            diag.update({
                "ict_smc_score": 0,
                "ict_smc_pass": False,
                "ict_smc_confidence": "BLOCKED",
                "ema_alignment_pass": False,
                "swing_block_reason": data_reason,
                "swing_watchlist_reason": "",
                "ict_smc_details": ict_details,
                "ema_details": ema_details,
                "decision": "BLOCKED",
                "direction": direction,
                "smc_source": smc.get("source"),
            })
            strat["ict_smc_score"] = 0
            strat["ict_smc_pass"] = False
            strat["ict_smc_confidence"] = "BLOCKED"
            strat["ema_alignment_pass"] = False
            strat["swing_block_reason"] = data_reason
            strat["swing_watchlist_reason"] = ""
            strat["ict_smc_details"] = ict_details
            strat["ema_details"] = ema_details
            strat["rc15f_swing_confirmation"] = dict(diag)
            strat["smc_0dte_mtf"] = smc
            strat["smc_0dte_mtf_full"] = smc
            strat["no_trade"] = True
            strat["qualified"] = False
            strat["decision"] = "REJECTED_SWING_ICT_SMC_EMA"
            strat["reject_reason"] = data_reason
            strat["smc_swing_block"] = True
            strat.setdefault("reasons", []).append(data_reason)
            return True, f"RC15f Swing blocked: {data_reason}", diag

        ict_score = int(smc.get("ict_smc_score") or ((smc.get("ict_smc_details") or {}).get("score") or 0))
        ict_pass = bool(smc.get("ict_smc_pass") or ict_score >= 4)
        ict_conf = smc.get("ict_smc_confidence") or ("HIGH" if ict_score >= 5 else "MODERATE" if ict_score >= 4 else "WATCHLIST" if ict_score == 3 else "BLOCKED")
        ema_pass = bool(smc.get("ema_alignment_pass"))
        ict_details = smc.get("ict_smc_details") or {}
        ema_details = smc.get("ema_details") or {}

        if ict_score >= 5 and ema_pass:
            confidence = "HIGH"
        elif ict_score >= 4 and ema_pass:
            confidence = "MODERATE"
        elif ict_score == 3:
            confidence = "WATCHLIST"
        else:
            confidence = "BLOCKED"

        ema_bias = str((ema_details or {}).get("bias") or "").strip().lower()
        ema_reason = str((ema_details or {}).get("reason") or "").strip().lower()
        ema_unavailable = (
            ema_bias == "unavailable"
            or "unavailable" in ema_reason
            or "insufficient" in ema_reason
            or ema_reason in ("diagnostics_error", "ema_15m_unavailable")
        )

        if not ema_pass and ema_unavailable:
            decision = "WATCHLIST"
            block_reason = ""
            watch_reason = "Swing watchlist only — EMA unavailable, no hard EMA failure"
            blocked = True
        elif not ema_pass:
            decision = "BLOCKED"
            block_reason = "Swing blocked — EMA alignment failed"
            watch_reason = ""
            blocked = True
        elif ict_score == 3:
            decision = "WATCHLIST"
            block_reason = ""
            watch_reason = "Swing watchlist only — ICT/SMC score 3/5"
            blocked = True
        elif ict_score <= 2:
            decision = "BLOCKED"
            block_reason = f"Swing blocked — incomplete ICT/SMC score {ict_score}/5"
            watch_reason = ""
            blocked = True
        elif ict_pass and ema_pass:
            decision = "ALLOW"
            block_reason = ""
            watch_reason = ""
            blocked = False
        else:
            decision = "BLOCKED"
            block_reason = "Swing blocked — incomplete ICT/SMC + EMA confirmation"
            watch_reason = ""
            blocked = True

        diag.update({
            "ict_smc_score": ict_score,
            "ict_smc_pass": bool(ict_pass),
            "ict_smc_confidence": confidence,
            "ema_alignment_pass": bool(ema_pass),
            "swing_block_reason": block_reason,
            "swing_watchlist_reason": watch_reason,
            "ict_smc_details": ict_details,
            "ema_details": ema_details,
            "decision": decision,
            "direction": direction,
            "smc_source": smc.get("source"),
        })
        strat["ict_smc_score"] = ict_score
        strat["ict_smc_pass"] = bool(ict_pass)
        strat["ict_smc_confidence"] = confidence
        strat["ema_alignment_pass"] = bool(ema_pass)
        strat["swing_block_reason"] = block_reason
        strat["swing_watchlist_reason"] = watch_reason
        strat["ict_smc_details"] = ict_details
        strat["ema_details"] = ema_details
        strat["rc15f_swing_confirmation"] = dict(diag)
        strat["smc_0dte_mtf"] = smc
        strat["smc_0dte_mtf_full"] = smc
        if blocked:
            reason = block_reason or watch_reason or "Swing blocked — incomplete ICT/SMC + EMA confirmation"
            strat["no_trade"] = True
            strat["qualified"] = False
            strat["decision"] = "REJECTED_SWING_ICT_SMC_EMA" if decision == "BLOCKED" else "WATCHLIST_SWING_ICT_SMC"
            strat["reject_reason"] = reason
            strat["smc_swing_block"] = True
            strat.setdefault("reasons", []).append(reason)
            return True, reason, diag
        return False, f"RC15f Swing allowed — ICT/SMC {ict_score}/5 {confidence}, EMA PASS", diag
    except Exception as e:
        diag["error"] = str(e)
        return True, f"RC15f Swing blocked: ict_smc_ema_guard_error:{e}", diag


def _count_open_same_symbol_direction_0dte(symbol: str, direction: str) -> int:
    """Open paper exposure count for SPY/QQQ/IWM by same symbol+direction+0DTE."""
    try:
        sym = str(symbol or "").upper().strip()
        direction = str(direction or "").lower().strip()
        if direction == "bearish":
            strategies = ("Put Debit Spread", "Bear Call Spread")
        elif direction == "bullish":
            strategies = ("Call Debit Spread", "Bull Put Spread")
        else:
            return 0
        from core.database import get_connection
        placeholders = ",".join("?" for _ in strategies)
        with get_connection() as conn:
            row = conn.execute(
                f"""SELECT COUNT(*) AS n FROM paper_trades
                    WHERE status='open'
                      AND symbol=?
                      AND selected_mode='0DTE'
                      AND strategy IN ({placeholders})""",
                (sym, *strategies),
            ).fetchone()
            return int(row["n"] if row else 0)
    except Exception as e:
        print(f"[exposure_guard] count error {symbol} {direction}: {e}")
        return 0

def _paper_quality_tag(strat: Dict, market_open: bool = None, extra_notes=None) -> str:
    """
    وسم جودة للصفقة الورقية.
    الهدف: لا نمنع تسجيل الإشارة في Paper، لكن نوضح هل هي واقعية أم للدراسة فقط.
    """
    notes = []
    if market_open is None:
        market_open = _is_market_open()
    if not market_open:
        notes.append("market_closed")
    try:
        from core.analyzer import _liquidity_status_from_strat
        if _liquidity_status_from_strat(strat) == "FAIL":
            notes.append("liquidity_fail")
    except Exception:
        pass
    if extra_notes:
        if isinstance(extra_notes, (list, tuple, set)):
            notes.extend(str(x) for x in extra_notes if x)
        else:
            notes.append(str(extra_notes))
    # إزالة التكرار مع الحفاظ على الترتيب
    clean = []
    for n in notes:
        if n and n not in clean:
            clean.append(n)
    return "REALISTIC_PAPER" if not clean else "STUDY_ONLY: " + ", ".join(clean)




def _execution_quality_gate(strat: Dict, dq_report: Optional[Dict] = None) -> Tuple[bool, str]:
    """
    v3.21 hard gate قبل تسجيل Paper Trade.
    يطبق على كل الاستراتيجيات وكل الأرجل:
      - bid/ask يجب أن يكونا موجودين وموجبين.
      - أقصى spread: 0DTE=20%، Swing=25%.
      - quote age إذا كان متاحاً يجب ألا يتجاوز 60s.
    """
    dq_report = dq_report or {}
    mode = resolve_trade_mode(strat)
    is_swing = str(mode).lower() == "swing"
    max_spread = MAX_BID_ASK_SPREAD_PCT_SWING if is_swing else MAX_BID_ASK_SPREAD_PCT_0DTE
    max_age = QUOTE_AGE_MAX_SECONDS_SWING if is_swing else QUOTE_AGE_MAX_SECONDS_0DTE

    # quote age: نرفض فقط إذا وصلنا عمر صريح من الكاش/المزود.
    age = (strat.get("quote_age_seconds") or strat.get("data_age_seconds") or
           dq_report.get("quote_age_seconds") or dq_report.get("age_seconds"))
    try:
        if age is not None and float(age) > max_age:
            return False, f"SKIPPED_STALE_DATA: quote_age={float(age):.0f}s > {max_age}s"
    except Exception:
        pass

    legs = strat.get("legs_detail") or []
    if not legs:
        return False, "REJECTED_NO_LEGS: no legs_detail for execution-quality check"

    for idx, leg in enumerate(legs, start=1):
        strike = leg.get("strike", "?")
        bid = leg.get("bid")
        ask = leg.get("ask")
        try:
            bid_f = float(bid)
            ask_f = float(ask)
        except Exception:
            return False, f"REJECTED_NO_QUOTES: leg#{idx} strike={strike} missing bid/ask"
        if REQUIRE_COMPLETE_BID_ASK and (bid_f <= 0 or ask_f <= 0 or ask_f <= bid_f):
            return False, f"REJECTED_NO_QUOTES: leg#{idx} strike={strike} invalid bid/ask bid={bid} ask={ask}"
        mid = (bid_f + ask_f) / 2.0
        if mid <= 0:
            return False, f"REJECTED_NO_QUOTES: leg#{idx} strike={strike} invalid mid"
        spread_pct = leg.get("spread_pct")
        try:
            spread_pct = float(spread_pct) if spread_pct is not None else ((ask_f - bid_f) / mid * 100.0)
        except Exception:
            spread_pct = ((ask_f - bid_f) / mid * 100.0)
        if REALISTIC_LIQUIDITY_GATE and spread_pct > max_spread:
            return False, (f"REJECTED_LIQUIDITY: leg#{idx} strike={strike} "
                           f"spread={spread_pct:.1f}% > max {max_spread:.0f}% ({mode})")

    return True, f"execution_quality_pass: max_spread≤{max_spread:.0f}% quote_age≤{max_age}s"

# ── تحقق من مؤهلات الإشارة ───────────────────────────────────────────────────

def _qualify(strat: Dict, dq_report: Dict, symbol: str,
             _verbose: bool = False,
             price: float = 0,
             price_source: str = "") -> Tuple[bool, str]:
    """
    يتحقق هل الإشارة مؤهلة للتسجيل التلقائي.
    _verbose=True → يطبع كل خطوة على الـ console لتسهيل التشخيص.
    price / price_source → يُسجَّلان في اللوج لتشخيص مشاكل السعر.
    """
    name  = (strat or {}).get("strategy", "?")
    score = (strat or {}).get("score", 0)

    def _log(msg: str) -> None:
        if _verbose:
            try:
                print(f"[qualify] {symbol} {name} score={score}: {msg}")
            except UnicodeEncodeError:
                print(f"[qualify] {symbol} {name} score={score}: {msg.encode('ascii', 'replace').decode()}")

    # ── تسجيل بيانات السعر دائماً (بغض النظر عن النتيجة) ─────────────────────
    dq_mode    = dq_report.get("mode", "UNKNOWN")
    dq_quality = dq_report.get("quality_pct", 0)
    checks     = dq_report.get("checks", {})
    has_price  = bool(checks.get("price") or price > 0)
    has_chain  = bool(checks.get("chain"))

    log_info(
        f"QUALIFY {symbol} {name} | "
        f"raw_price={price:.2f} | source={price_source or 'unknown'} | "
        f"has_price={has_price} | has_chain={has_chain} | "
        f"dq_mode={dq_mode} ({dq_quality}%) | score={score}"
    )

    et_q   = _et_now()
    et_str = et_q.strftime("%H:%M:%S")

    def _fa_reject(final_action: str, reason: str):
        print(f"[FINAL_ACTION] {et_str} | {symbol} | {name} | score={score} | "
              f"final_action={final_action} | reason={reason}")
        _log(f"رُفض ← {reason}")
        return False, reason

    if not strat or strat.get("no_trade"):
        return _fa_reject("study_only", "no_trade=True — study only")

    if score < MIN_SCORE_AUTO:
        return _fa_reject("rejected", f"score_below_auto_threshold ({score}<{MIN_SCORE_AUTO})")

    if dq_mode == "LIMITED":
        if not has_price or not has_chain:
            return _fa_reject("rejected",
                f"Data=LIMITED_no_price_chain | price={price:.2f} src={price_source or 'unknown'}")
        if score < 65:
            return _fa_reject("rejected",
                f"Data_partial_score_below_65 ({score}<65 required when LIMITED)")
        _log(f"⚠️ Data=LIMITED لكن has_price+has_chain — مكمل بحد 65")

    # v3.21 Execution Quality Gate — أصبح Liquidity FAIL رفضاً صارماً قبل التسجيل الورقي.
    from core.analyzer import _liquidity_status_from_strat
    liq = _liquidity_status_from_strat(strat)
    q_ok, q_reason = _execution_quality_gate(strat, dq_report)
    if not q_ok:
        _qr = str(q_reason or "").upper()
        _action = "rejected_liquidity" if ("REJECTED_NO_QUOTES" in _qr or "REJECTED_LIQUIDITY" in _qr) else "rejected_execution_quality"
        return _fa_reject(_action, q_reason)
    _log(f"✅ Execution quality OK — {q_reason}")

    is_credit = name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
    val = strat.get("credit") if is_credit else strat.get("debit")
    if not val or val <= 0:
        legs_d = strat.get("legs_detail", [])
        if not legs_d:
            return _fa_reject("rejected", "credit_debit_zero_no_legs")
        _log(f"⚠️ Credit/Debit=0 لكن توجد أرجل — مكمل مع تحذير")

    # فحص وقت السوق — Auto Paper Register لا يعمل إلا أثناء السوق.
    # خارج السوق يبقى التحليل Study Only فقط ولا تُسجّل Paper Trades تلقائياً.
    market_open = _is_market_open()
    _log(f"ET الآن = {et_str} — السوق {'مفتوح' if market_open else 'مغلق'}")
    if not market_open:
        return _fa_reject("market_closed_study_only",
            f"market_closed_ET_{et_q.strftime('%H:%M')} — analysis only, no auto paper registration")

    # ── SPX AM-settled block ──────────────────────────────────────────────────
    trade_mode_check = resolve_trade_mode(strat)
    if market_open and symbol.upper() == "SPX" and trade_mode_check == "0DTE":
        try:
            from core.analyzer import classify_spx_expiry
            expiry_for_check = strat.get("expiry_date") or _et_now().strftime("%Y-%m-%d")
            settle = classify_spx_expiry(expiry_for_check)
            if settle["settlement_type"] == "AM":
                et_h, et_m = et_q.hour, et_q.minute
                if (et_h, et_m) >= (9, 15):
                    return _fa_reject("market_time_restriction",
                        f"SPX_AM_settled_cutoff_09:15 (now {et_h:02d}:{et_m:02d} ET)")
        except Exception as _se:
            _log(f"⚠️ classify_spx_expiry خطأ: {_se}")

    # ── فلتر آخر ساعتين للـ 0DTE Iron Condor فقط ───────────────────────────────
    # بعد 14:00 ET: يمنع فتح Iron Condor فقط، وتبقى بقية الاستراتيجيات مسموحة
    # إذا اجتازت شروطها.
    _IRON_CONDOR_CUTOFF_HOUR   = 14
    _IRON_CONDOR_CUTOFF_MINUTE = 0

    if market_open and trade_mode_check == "0DTE" and name == "Iron Condor":
        et_h, et_m = et_q.hour, et_q.minute
        if (et_h, et_m) >= (_IRON_CONDOR_CUTOFF_HOUR, _IRON_CONDOR_CUTOFF_MINUTE):
            return _fa_reject("market_time_restriction",
                f"iron_condor_cutoff_14:00 "
                f"(Iron Condor not allowed in last 2 hours — now {et_q.strftime('%H:%M')} ET)")

    # فحص حالة الاستراتيجية (مراحل التعطيل + Probation)
    try:
        from core.database import is_strategy_allowed
        trade_mode = resolve_trade_mode(strat)
        allowed, weight, strat_reason = is_strategy_allowed(
            trade_mode, name, symbol, paper_only=True)
        if not allowed:
            return _fa_reject("rejected_strategy_status", f"استراتيجية معطّلة/Probation: {strat_reason}")
        if weight < 1.0:
            _log(f"⚠️ وزن مخفّض {weight:.0%}")
    except Exception as _e:
        _log(f"⚠️ is_strategy_allowed خطأ: {_e}")

    # Duplicate/slot controls. 0 = prevent duplicate open symbol+strategy by default.
    # RC15i keeps paper testing safer unless the setting is explicitly enabled.
    try:
        allow_duplicates = str(get_setting("allow_duplicate_open_strategies", "0")).strip() in ("1", "true", "True", "yes", "YES", "نعم")
        if not allow_duplicates:
            per_strategy = get_open_count_for_strategy(symbol, name)
            if per_strategy >= 1:
                return _fa_reject("duplicate", f"already_open_{symbol}_{name}")
    except Exception:
        pass

    max_per_symbol = int(get_setting("max_per_symbol", "0") or 0)
    per_symbol = get_open_count_for_symbol(symbol)
    if max_per_symbol > 0 and per_symbol >= max_per_symbol:
        return _fa_reject("rejected", f"max_per_symbol_reached ({per_symbol}/{max_per_symbol})")

    max_open     = int(get_setting("max_open_trades", "0") or 0)
    current_open = get_open_trades_count()
    if max_open > 0 and current_open >= max_open:
        return _fa_reject("rejected", f"max_open_trades_reached ({current_open}/{max_open})")

    print(f"[FINAL_ACTION] {et_str} | {symbol} | {name} | score={score} | "
          f"final_action=qualified_passing_to_execute | liq={liq} | val={val}")
    _log("✅ مؤهل")
    return True, "مؤهل"


def _build_legs(strat: Dict) -> Dict:
    """يبني dict للأرجل من نتيجة الاستراتيجية."""
    name = strat.get("strategy", "")
    legs: Dict[str, Any] = {}
    if name == "Iron Condor":
        legs = {
            "short_call": strat.get("short_call"),
            "long_call":  strat.get("long_call"),
            "short_put":  strat.get("short_put"),
            "long_put":   strat.get("long_put"),
        }
    elif name == "Bull Put Spread":
        legs = {"short_put": strat.get("short_put"), "long_put": strat.get("long_put")}
    elif name == "Bear Call Spread":
        legs = {"short_call": strat.get("short_call"), "long_call": strat.get("long_call")}
    elif name == "Call Debit Spread":
        legs = {"long_call": strat.get("long_call"), "short_call": strat.get("short_call")}
    elif name == "Put Debit Spread":
        legs = {"long_put": strat.get("long_put"), "short_put": strat.get("short_put")}
    return legs


def _legs_display(legs: Dict, strat: Dict) -> str:
    """نص مختصر للأرجل للتيليغرام."""
    parts = []
    if legs.get("short_call") and legs.get("long_call"):
        parts.append(f"Call: Sell {legs['short_call']:,.0f} / Buy {legs['long_call']:,.0f}")
    if legs.get("short_put") and legs.get("long_put"):
        parts.append(f"Put:  Sell {legs['short_put']:,.0f} / Buy {legs['long_put']:,.0f}")
    if legs.get("long_call") and not legs.get("short_call"):
        parts.append(f"Buy {legs['long_call']:,.0f} Call")
    if legs.get("long_put") and not legs.get("short_put"):
        parts.append(f"Buy {legs['long_put']:,.0f} Put")
    return "\n  ".join(parts) if parts else "—"


def _norm_sig_value(v) -> str:
    """Normalize strikes/values so 5280 and 5280.0 produce the same signature."""
    if v is None or v == "":
        return "-"
    try:
        f = float(v)
        if f.is_integer():
            return str(int(f))
        return (f"{f:.4f}").rstrip("0").rstrip(".")
    except Exception:
        return str(v).strip() or "-"


def _build_trade_signature(symbol: str, trade_mode: str, strategy: str,
                           expiry_date: str, legs: Dict) -> str:
    """
    Universal exact paper-trade fingerprint for every strategy.

    Same signature = same symbol/mode/strategy/expiry and same legs.
    Different strikes or expiry = distinct opportunity, even if strategy repeats.
    """
    parts = [
        str(symbol or "").upper().strip(),
        str(trade_mode or "").strip(),
        str(strategy or "").upper().replace(" ", "_").strip(),
        str(expiry_date or "").strip(),
        f"SP={_norm_sig_value(legs.get('short_put'))}",
        f"LP={_norm_sig_value(legs.get('long_put'))}",
        f"SC={_norm_sig_value(legs.get('short_call'))}",
        f"LC={_norm_sig_value(legs.get('long_call'))}",
    ]
    return "|".join(parts)



def _check_open_symbol_strategy_duplicate(symbol: str, strategy: str, trade_mode: str) -> Tuple[bool, str]:
    """
    RC15i-1 final same symbol+strategy+mode duplicate guard for Paper trades.
    Only a real boolean setting value that enables duplicates bypasses this check.
    This function is intended to be called inside _qualify_lock immediately before INSERT.
    """
    try:
        from core.database import get_setting, get_paper_open_count_for_strategy
        allow_dup = str(get_setting("allow_duplicate_open_strategies", "0")).strip() in (
            "1", "true", "True", "yes", "YES", "نعم"
        )
        if allow_dup:
            return False, "duplicate_open_strategies_allowed"
        open_count = int(get_paper_open_count_for_strategy(symbol, strategy, trade_mode) or 0)
        if open_count >= 1:
            return True, (
                f"SKIP_DUPLICATE_OPEN symbol_strategy_mode open_count={open_count} "
                f"symbol={symbol} strategy={strategy} mode={trade_mode}"
            )
        return False, "no_open_symbol_strategy_duplicate"
    except Exception as _e:
        print(f"[dedup] open symbol/strategy duplicate check error: {_e}")
        return False, "open_symbol_strategy_dedup_check_error_allow"

def _check_exact_duplicate_signature(trade_signature: str) -> Tuple[bool, str]:
    """
    Returns (blocked, reason).
    Blocks exact duplicate paper trades according to settings while preserving
    unlimited distinct valid trades.
    """
    try:
        from core.database import get_setting, find_paper_trade_by_signature, count_paper_trades_by_signature

        # prevent_exact_duplicate_open_trade now supports either:
        #   0 = disabled
        #   1 = legacy boolean: block if one identical open trade exists
        #   N = allow up to N identical open trades, then block
        raw_prevent = str(get_setting("prevent_exact_duplicate_open_trade", "3")).strip()
        try:
            max_exact_open = int(float(raw_prevent or 0))
        except Exception:
            max_exact_open = 1 if raw_prevent in ("true", "True", "yes", "YES", "نعم") else 0
        prevent_open = max_exact_open > 0
        same_day = str(get_setting("prevent_same_trade_same_day", "0")).strip() in (
            "1", "true", "True", "yes", "YES", "نعم"
        )
        try:
            cooldown = int(float(get_setting("duplicate_signal_cooldown_minutes", "0") or 0))
        except Exception:
            cooldown = 0

        if prevent_open:
            open_count = count_paper_trades_by_signature(trade_signature, open_only=True)
            if open_count >= max_exact_open:
                row = find_paper_trade_by_signature(trade_signature, open_only=True)
                return True, (
                    f"SKIP_DUPLICATE_OPEN exact_signature open_count={open_count} "
                    f"limit={max_exact_open} latest_paper_id={row.get('id') if row else '-'}"
                )

        if same_day:
            row = find_paper_trade_by_signature(trade_signature, same_day=True)
            if row:
                return True, f"SKIP_DUPLICATE_SAME_DAY exact_signature paper_id={row.get('id')}"

        if cooldown > 0:
            row = find_paper_trade_by_signature(trade_signature, cooldown_minutes=cooldown)
            if row:
                return True, f"SKIP_DUPLICATE_COOLDOWN {cooldown}min exact_signature paper_id={row.get('id')}"

        return False, "distinct_or_allowed"
    except Exception as _e:
        print(f"[dedup] duplicate signature check error: {_e}")
        return False, "dedup_check_error_allow"


# ── Smart Paper Trade Duplicate Filter ────────────────────────────────────────

# نسبة تحرك السعر التي تُعتبر "تغيير مؤثر"
_PRICE_MOVE_SPX = 0.005   # 0.5%
_PRICE_MOVE_ETF = 0.003   # 0.3%
_SCORE_DELTA_MIN = 10     # فرق score يستحق التسجيل
_MAX_GAP_HOURS   = 2.0    # أقصى فجوة قبل التسجيل الإجباري


def _strikes_changed(prev: dict, curr: dict) -> bool:
    """True إذا تغيرت أي ضربة."""
    for k in ("short_put", "long_put", "short_call", "long_call"):
        if prev.get(k) != curr.get(k):
            return True
    return False


def _price_moved(prev_price: float, curr_price: float, symbol: str) -> bool:
    """True إذا تحرك السعر فوق الحد المسموح."""
    if not prev_price or not curr_price:
        return False
    threshold = _PRICE_MOVE_SPX if symbol.upper() == "SPX" else _PRICE_MOVE_ETF
    return abs(curr_price - prev_price) / prev_price >= threshold


def _should_log_new_paper_trade(
    symbol: str,
    strat: dict,
    current_price: float,
    last_trade: dict,          # آخر paper_trade مسجّلة لهذا (symbol, strategy)
) -> Tuple[bool, str]:
    """
    يقرر هل نُسجّل Paper Trade جديدة أم نكتفي بتحديث Signal Tracking.

    يُعيد (should_log: bool, reason: str)
    """
    if not last_trade:
        return True, "أول إشارة لهذه الاستراتيجية اليوم"

    # 1. تغيّر الضربات؟
    if _strikes_changed(last_trade, strat):
        return True, (
            f"تغيّرت الضربات: "
            f"prev({last_trade.get('short_call') or last_trade.get('short_put')})"
            f" → curr({strat.get('short_call') or strat.get('short_put')})"
        )

    # 2. تغيّر Mode؟
    prev_mode = last_trade.get("selected_mode", "0DTE")
    curr_mode = resolve_trade_mode(strat)
    if prev_mode != curr_mode:
        return True, f"تغيّر Mode: {prev_mode} → {curr_mode}"

    # 3. تغيّر Score بـ ≥ 10 نقاط؟
    prev_score = float(last_trade.get("score") or 0)
    curr_score = float(strat.get("score") or 0)
    if abs(curr_score - prev_score) >= _SCORE_DELTA_MIN:
        return True, f"تغيّر Score: {prev_score:.0f} → {curr_score:.0f} (Δ{curr_score-prev_score:+.0f})"

    # 4. تغيّر Quality؟
    prev_q = (last_trade.get("setup_quality") or "").strip().lower()
    curr_q = (strat.get("setup_quality") or "").strip().lower()
    if prev_q and curr_q and prev_q != curr_q:
        return True, f"تغيّر Quality: {prev_q} → {curr_q}"

    # 5. تحرّك السعر بشكل مؤثر؟
    prev_price = float(last_trade.get("current_price") or last_trade.get("price_first_detected") or 0)
    if _price_moved(prev_price, current_price, symbol):
        pct = abs(current_price - prev_price) / prev_price * 100
        return True, f"تحرّك السعر: {prev_price:.1f} → {current_price:.1f} ({pct:+.2f}%)"

    # 6. مرّت أكثر من ساعتين؟
    try:
        from datetime import datetime as _dt
        last_time_str = last_trade.get("created_at") or last_trade.get("timestamp") or ""
        if last_time_str:
            last_dt = _dt.strptime(last_time_str[:19], "%Y-%m-%d %H:%M:%S")
            gap_hours = ((_dt.now() - last_dt).total_seconds()) / 3600
            if gap_hours >= _MAX_GAP_HOURS:
                return True, f"مرّت {gap_hours:.1f} ساعة منذ آخر تسجيل (حد: {_MAX_GAP_HOURS}h)"
    except Exception:
        pass

    # لا شيء تغيّر — فقط حدّث Signal Tracking
    return False, (
        f"لا تغيير مؤثر (Strikes={strat.get('short_call') or strat.get('short_put')}, "
        f"Score={curr_score:.0f}, Quality={curr_q})"
    )


# ── تسجيل كل إشارة مؤهلة في Paper Trading ────────────────────────────────────

def _log_all_qualified_signals(candidates: list, analysis: Dict) -> None:
    """
    يُسجّل Paper Trade جديدة فقط عند تغيير فعلي في الإشارة:
      - تغيّرت الضربات / Mode / Quality
      - تغيّر Score بـ ≥ 10 نقاط
      - تحرّك السعر > 0.5% (SPX) أو 0.3% (SPY/QQQ/IWM)
      - مرّت > ساعتين من آخر تسجيل
    وإلا: يُحدّث Signal Tracking فقط (Current Score / Peak Score / Price).

    يتخطى أي (symbol, mode) له Paper path مفعّل في الإعدادات
    لأن _execute_trade() ستتولى تسجيله — لمنع التكرار.
    """
    market_open_for_scan = _is_market_open()

    # جمع المسارات التي لها Paper مفعّل → لا نسجّل هنا لأن _execute_trade ستتولاها
    try:
        from core.database import get_enabled_trade_paths
        _paper_paths = {
            (p["symbol"], p["trade_mode"])
            for p in get_enabled_trade_paths()
            if p["execution_mode"] == "Paper"
        }
    except Exception:
        _paper_paths = set()

    try:
        from core.database import log_paper_trade, get_connection
        from datetime import date
        today = date.today().isoformat()

        # جلب آخر paper_trade لكل (symbol, strategy) اليوم — كل الحقول للمقارنة
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT symbol, strategy, status, short_put, long_put, short_call, long_call,
                          score, setup_quality, selected_mode, current_price,
                          price_first_detected, created_at, timestamp
                   FROM paper_trades
                   WHERE date(created_at)=?
                   ORDER BY created_at DESC""",
                (today,)
            ).fetchall()

        # نُخزّن آخر سجل لكل (symbol, strategy) — الأحدث أولاً
        last_trade: Dict[tuple, dict] = {}
        for r in rows:
            key = (r["symbol"], r["strategy"])
            if key not in last_trade:
                last_trade[key] = dict(r)

        for symbol, strat, dq, price, *_rest in candidates:
            if not strat or strat.get("no_trade"):
                continue
            score = strat.get("score", 0)
            if score < MIN_SCORE_AUTO:
                continue
            strat_name = strat.get("strategy", "?")

            # تحقق من السيولة والقيمة
            val = strat.get("credit") if strat_name in (
                "Iron Condor","Bull Put Spread","Bear Call Spread") else strat.get("debit")
            if not val or val <= 0:
                continue

            selected_mode = strat.get("trade_mode") or \
                            (analysis.get("selected_trade_mode") if symbol == "SPX"
                             else "0DTE")

            # تخطّ إذا كان هذا المسار له Paper path مفعّل — _execute_trade ستتولاه
            if (symbol, selected_mode) in _paper_paths:
                print(f"[paper_all] ⏭ SKIP {symbol} {selected_mode}: Paper path enabled → handled by _execute_trade")
                continue

            # فحص الحالة — نسمح لـ probation بالتسجيل في Paper فقط
            try:
                from core.database import is_strategy_allowed, increment_probation_count
                ok, _, _ = is_strategy_allowed(selected_mode, strat_name, symbol,
                                                paper_only=True)
                if not ok:
                    continue
                # زيادة عداد Probation
                increment_probation_count(selected_mode, strat_name, symbol)
            except Exception:
                pass

            try:
                from core.database import update_signal_tracking
                dte_entry   = strat.get("target_dte", 0)
                expiry_date = _swing_expiry(dte_entry) if selected_mode == "Swing" \
                              else _et_now().strftime("%Y-%m-%d")
                enriched = {
                    **strat,
                    "expiry_date":  strat.get("expiry_date") or expiry_date,
                    "dte_at_entry": dte_entry,
                    "trade_mode":   selected_mode,
                    "setup_quality": _paper_quality_tag(strat, market_open_for_scan),
                }
                now_str = _et_now().strftime("%Y-%m-%d %H:%M:%S")

                # ── v3.33.6a RC7 — SPY/QQQ/IWM 0DTE EMA Alignment + Exposure Cap
                if selected_mode == "0DTE" and str(symbol or "").upper() in ("SPY", "QQQ", "IWM"):
                    blocked_ema, ema_reason, ema_diag = _spy_qqq_intraday_ema_alignment_guard(
                        analysis, symbol, strat_name, price, selected_mode
                    )
                    analysis.setdefault("_intraday_ema_alignment", []).append({
                        **ema_diag, "blocked": bool(blocked_ema), "reason": ema_reason,
                    })
                    if blocked_ema:
                        print(f"[paper_all] ⏭ EMA ALIGNMENT {symbol} {strat_name} | {ema_reason}")
                        try:
                            from core.database import log_signal_rejection
                            log_signal_rejection(symbol, strat_name, int(score or 0), ema_reason)
                        except Exception:
                            pass
                        continue
                    direction = _strategy_direction(strat_name)
                    if direction in ("bearish", "bullish"):
                        blocked_reentry, reentry_reason, reentry_diag = _spy_qqq_iwm_light_reentry_guard(
                            symbol, direction, ema_diag
                        )
                        analysis.setdefault("_rc11_reentry_control", []).append({
                            **reentry_diag, "blocked": bool(blocked_reentry), "reason": reentry_reason,
                        })
                        if blocked_reentry:
                            print(f"[paper_all] ⏭ RC11 REENTRY {symbol} {strat_name} | {reentry_reason}")
                            try:
                                from core.database import log_signal_rejection
                                log_signal_rejection(symbol, strat_name, int(score or 0), reentry_reason)
                            except Exception:
                                pass
                            continue

                        open_dir_count = _count_open_same_symbol_direction_0dte(symbol, direction)
                        if open_dir_count >= MAX_OPEN_SAME_SYMBOL_DIRECTION_0DTE:
                            reason = (f"same_symbol_direction_0dte_cap_reached: {symbol} {direction} "
                                      f"open_count={open_dir_count}/{MAX_OPEN_SAME_SYMBOL_DIRECTION_0DTE}")
                            print(f"[paper_all] ⏭ EXPOSURE CAP {symbol} {strat_name} | {reason}")
                            try:
                                from core.database import log_signal_rejection
                                log_signal_rejection(symbol, strat_name, int(score or 0), reason)
                            except Exception:
                                pass
                            continue

                # ── Universal exact de-duplication applies to every strategy ──
                scan_legs = _build_legs(enriched)
                trade_signature = _build_trade_signature(
                    symbol, selected_mode, strat_name, enriched.get("expiry_date"), scan_legs
                )
                blocked_dup, dup_reason = _check_exact_duplicate_signature(trade_signature)
                if blocked_dup:
                    print(f"[paper_all] ⏭ DUPLICATE {symbol} {strat_name} | {dup_reason} | signature={trade_signature}")
                    try:
                        analysis.setdefault("_paper_skipped_duplicates", []).append({
                            "symbol": symbol, "strategy": strat_name, "trade_mode": selected_mode,
                            "expiry_date": enriched.get("expiry_date"), "score": score,
                            "trade_signature": trade_signature, "reason": dup_reason,
                        })
                    except Exception:
                        pass
                    continue
                enriched["trade_signature"] = trade_signature
                enriched["duplicate_note"] = "registered_distinct_signal"

                # ── قرار: تسجيل جديد أم تحديث Signal Tracking فقط ────────────
                prev = last_trade.get((symbol, strat_name))
                # RC14 hotfix: a closed Swing trade must not block a fresh qualified setup.
                # The atomic open-Swing guard in log_paper_trade() remains the final source of truth.
                if str(selected_mode or "").strip().upper() == "SWING" and (
                    not prev or str(prev.get("status") or "").strip().lower() != "open"
                ):
                    should_log, reason = True, "fresh_swing_after_no_open_trade"
                else:
                    should_log, reason = _should_log_new_paper_trade(
                        symbol, enriched, price, prev)

                sig = update_signal_tracking(enriched, symbol, selected_mode, price, now_str)

                if should_log:
                    if str(selected_mode or "").strip().upper() == "SWING":
                        blocked_rc15f, rc15f_reason, rc15f_diag = _swing_ict_smc_ema_guard(
                            analysis, enriched, symbol, strat_name, price, selected_mode
                        )
                        analysis.setdefault("_rc15f_swing_ict_smc_ema", []).append({
                            **rc15f_diag, "blocked": bool(blocked_rc15f), "reason": rc15f_reason,
                        })
                        if blocked_rc15f:
                            _rc15f_action = _rc15f_final_action_from_diag(rc15f_diag)
                            _rc15f_label = "WATCHLIST" if _rc15f_action.startswith("watchlist_") else "BLOCK"
                            print(f"[paper_all] RC15f {_rc15f_label} {symbol} {strat_name} | {rc15f_reason}")
                            try:
                                analysis.setdefault("_paper_skipped_rc15f_swing", []).append({
                                    "symbol": symbol, "strategy": strat_name, "trade_mode": selected_mode,
                                    "score": score, "reason": rc15f_reason,
                                    "ict_smc_score": enriched.get("ict_smc_score"),
                                    "ema_alignment_pass": enriched.get("ema_alignment_pass"),
                                })
                                _record_paper_insert_status(
                                    analysis, symbol, strat_name, score, selected=True,
                                    insert_attempted=False, inserted=False,
                                    block_reason=rc15f_reason, final_action=_rc15f_action,
                                    trade_mode=selected_mode,
                                )
                            except Exception:
                                pass
                            continue
                    # RC15i-1: make final duplicate check + INSERT atomic here too.
                    with _qualify_lock:
                        blocked_dup, dup_reason = _check_exact_duplicate_signature(trade_signature)
                        if blocked_dup:
                            print(f"[paper_all] ⏭ DUPLICATE_ATOMIC {symbol} {strat_name} | {dup_reason} | signature={trade_signature}")
                            try:
                                analysis.setdefault("_paper_skipped_duplicates", []).append({
                                    "symbol": symbol, "strategy": strat_name, "trade_mode": selected_mode,
                                    "expiry_date": enriched.get("expiry_date"), "score": score,
                                    "trade_signature": trade_signature, "reason": dup_reason,
                                    "stage": "atomic_pre_insert",
                                })
                            except Exception:
                                pass
                            continue

                        blocked_open_dup, open_dup_reason = _check_open_symbol_strategy_duplicate(symbol, strat_name, selected_mode)
                        if blocked_open_dup:
                            print(f"[paper_all] ⏭ DUPLICATE_OPEN_ATOMIC {symbol} {strat_name} | {open_dup_reason}")
                            try:
                                analysis.setdefault("_paper_skipped_duplicates", []).append({
                                    "symbol": symbol, "strategy": strat_name, "trade_mode": selected_mode,
                                    "expiry_date": enriched.get("expiry_date"), "score": score,
                                    "trade_signature": trade_signature, "reason": open_dup_reason,
                                    "stage": "atomic_pre_insert",
                                })
                                _record_paper_insert_status(analysis, symbol, strat_name, score, selected=True,
                                                            insert_attempted=False, inserted=False,
                                                            block_reason=open_dup_reason,
                                                            final_action="skip_duplicate_open_strategy_atomic",
                                                            trade_mode=selected_mode)
                            except Exception:
                                pass
                            continue

                        _paper_id = log_paper_trade(
                            strat         = {**enriched, **sig},
                            symbol        = symbol,
                            selected_mode = selected_mode,
                            source        = "qualified_signal" if market_open_for_scan else "study_only_market_closed",
                        )
                    if int(_paper_id or 0) > 0:
                        try:
                            analysis.setdefault("_paper_logged_trades", []).append({
                                "paper_id": _paper_id,
                                "symbol": symbol,
                                "strategy": strat_name,
                                "score": score,
                                "trade_mode": selected_mode,
                                "source": "qualified_signal" if market_open_for_scan else "study_only_market_closed",
                                "trade_signature": trade_signature,
                                "duplicate_note": "registered_distinct_signal",
                                "reason": reason,
                            })
                            _record_paper_insert_status(analysis, symbol, strat_name, score, selected=True,
                                                        insert_attempted=True, inserted=True, inserted_trade_id=_paper_id,
                                                        block_reason="", final_action="qualified_signal_logged",
                                                        trade_mode=selected_mode)
                        except Exception:
                            pass
                        last_trade[(symbol, strat_name)] = {
                            **enriched,
                            "status": "open",
                            "created_at": now_str, "timestamp": now_str,
                            "current_price": price,
                        }
                        print(f"[paper_all] ✅ NEW {symbol} {strat_name} score={score:.0f} | {reason}")
                    else:
                        block_reason = "BLOCKED_OPEN_SWING_EXISTS" if str(selected_mode or "").upper().strip() == "SWING" else "INSERT_NOT_COMPLETED"
                        _record_paper_insert_status(analysis, symbol, strat_name, score, selected=True,
                                                    insert_attempted=True, inserted=False, inserted_trade_id=None,
                                                    block_reason=block_reason,
                                                    final_action="qualified_but_not_opened",
                                                    trade_mode=selected_mode)
                        print(f"[paper_all] ⏭ {block_reason} {symbol} {strat_name} score={score:.0f}")
                else:
                    _record_paper_insert_status(analysis, symbol, strat_name, score, selected=True,
                                                insert_attempted=False, inserted=False, block_reason=reason,
                                                final_action="qualified_signal_not_logged", trade_mode=selected_mode)
                    print(f"[paper_all] ⏭ SKIP {symbol} {strat_name} score={score:.0f}"
                          f" | {reason}")

            except Exception as _le:
                print(f"[paper_all] {symbol} error: {_le}")
    except Exception as e:
        print(f"[paper_all] outer error: {e}")


# ── التسجيل التلقائي ──────────────────────────────────────────────────────────

def check_and_log_signal(analysis: Dict) -> Optional[Dict]:
    """
    يُستدعى بعد كل تحليل.
    يفحص SPX + SPY + QQQ على نظامين منفصلين:
      - Swing:  analyze_swing() ← chain أسبوعي + شروط مستقلة
      - 0DTE:   analysis[strategy] ← chain اليوم + pin/gex

    الأولوية: Swing مؤهل → 0DTE مؤهل → لا صفقة
    """
    # ── بداية الدورة ────────────────────────────────────────────────────────
    import time as _t
    _cycle_start = _t.time()
    log_cycle_start("SPX+SPY+QQQ+IWM+DIA+AAPL+NVDA+GLD")
    # UI hook: collected paper trades registered in this analysis cycle.
    # ui/app.py reads this list to show a clear homepage status.
    analysis["_paper_logged_trades"] = []
    analysis["_paper_insert_status"] = []

    # ── فحص وقت السوق أولاً ────────────────────────────────────────────────
    # أثناء إغلاق السوق: التحليل يبقى معروضاً للدراسة فقط، لكن لا نسجّل
    # أي Paper Trade تلقائياً حتى لا تعتمد النتائج على أسعار قديمة/مغلقة.
    market_is_open = _is_market_open()
    if not market_is_open:
        et = _et_now()
        log_market_closed(et.strftime('%H:%M'))
        msg = f"Market closed — analysis only, no Paper Trade registration. (ET {et.strftime('%H:%M')})"
        print(f"[signal] {msg}")
        analysis["_market_closed"] = True
        analysis["_market_closed_msg"] = msg
        analysis["_paper_logged_trades"] = []
        try:
            for _sym in ("SPX", "SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD"):
                _record_paper_insert_status(analysis, _sym, strategy="ALL", selected=False, insert_attempted=False, inserted=False, block_reason="market_closed_no_auto_paper_registration", final_action="skip_market_closed")
        except Exception:
            pass
        print(f"[FINAL_ACTION] {et.strftime('%H:%M:%S')} | ALL | auto_paper | final_action=skip_market_closed | reason=no_auto_paper_registration")
        return {"market_closed": True, "logged_trades": [], "reason": "market_closed_no_auto_paper_registration"}

    # ── Swing: من Cache (يُحدَّث كل 5 دق في background — لا تأخير) ───────────
    swing_candidates: List[Dict[str, Any]] = []
    cache_info = get_swing_cache()   # فوري — 0.9ms
    cache_data = cache_info["data"]

    # معلومات الحالة للـ UI
    swing_diag: Dict[str, Any] = {
        "_cache_age":    cache_info["age_seconds"],
        "_cache_last_ok": cache_info["last_ok"],
        "_cache_error":  cache_info["error"],
        "_cache_loading": cache_info["loading"],
        "_cache_stale":  cache_info["stale"],
    }

    if cache_info["loading"]:
        # أول تشغيل — Cache لم يُحمَّل بعد
        for sym in SWING_ETF_SYMBOLS:
            swing_diag[sym] = {"qualified": False, "rejected": ["Swing Status: Loading..."], "loading": True}
    else:
        for sym in SWING_ETF_SYMBOLS:
            sw = cache_data.get(sym, {})
            _sw_strategy = sw.get("strategy") or {}
            swing_diag[sym] = {
                "qualified": sw.get("qualified", False),
                "qualified_with_warning": sw.get("qualified_with_warning", False),
                "status": sw.get("status", ""),
                "trend_4h":  sw.get("trend_4h", "—"),
                "confirmed_trend": sw.get("confirmed_trend"),
                "iv_rank":   sw.get("iv_rank", 0),
                "iv_percentile": sw.get("iv_percentile", sw.get("iv_percentile_for_regime")),
                "iv_regime": sw.get("iv_regime", ""),
                "dte":       sw.get("dte", 0),
                "expiry_date": sw.get("expiry_date", ""),
                "price":     sw.get("price", 0),
                "rejected":  sw.get("rejected", []),
                "reasons":   sw.get("reasons", []),
                "score":     sw.get("score", 0),
                "strategy":  _sw_strategy,
                "all_scores": (_sw_strategy.get("all_scores") if isinstance(_sw_strategy, dict) else {}) or sw.get("all_scores", {}),
                "pipeline_stop_code": sw.get("pipeline_stop_code", ""),
                "smc_evaluation": sw.get("smc_evaluation", ""),
                "ds_evaluation": sw.get("ds_evaluation", ""),
                "ds_unavailable_reason": sw.get("ds_unavailable_reason", ""),
                "entry_protection": sw.get("entry_protection", ""),
                "diagnostic_code": sw.get("diagnostic_code", ""),
                "chain_type": (_sw_strategy.get("chain_type") if isinstance(_sw_strategy, dict) else "") or "",
                "swing_em": sw.get("swing_em"),
            }
            if sw.get("qualified") and sw.get("strategy"):
                strat = sw["strategy"]
                if not strat.get("no_trade") and strat.get("score", 0) >= MIN_SCORE_AUTO:
                    swing_candidates.append({
                        "symbol":    sym,
                        "strat":     strat,
                        "price":     sw.get("price", 0),
                        "name":      strat.get("strategy", ""),
                        "score":     float(strat.get("score", 0)),
                        "is_credit": strat.get("strategy", "") in (
                            "Iron Condor", "Bull Put Spread", "Bear Call Spread"),
                        "val":       strat.get("credit") or strat.get("debit") or 0,
                        "risk":      {"allowed": True, "max_loss_per_contract": 0,
                                      "contracts_allowed": 1},
                    })

    analysis["_swing_diag"] = swing_diag

    # ── 0DTE candidates من التحليل الحالي ────────────────────────────────────
    def _sym_data(key):
        d = analysis if key == "SPX" else (analysis.get(key.lower()) or {})
        return {
            "strat":        d.get("strategy"),
            "dq":           d.get("data_quality_report", {}),
            "price":        d.get("price", 0) or 0,
            "price_source": d.get("price_source", "unknown"),
        }

    candidates = [
        ("SPX", _sym_data("SPX")),
        ("SPY", _sym_data("SPY")),
        ("QQQ", _sym_data("QQQ")),
        ("IWM", _sym_data("IWM")),
    ]

    # log price summary before qualify
    for sym, sd in candidates:
        log_info(
            f"PRICE {sym} | raw={sd['price']:.2f} | "
            f"source={sd['price_source']} | "
            f"dq_mode={sd['dq'].get('mode','?')} | "
            f"has_chain={bool(sd['dq'].get('checks',{}).get('chain'))}"
        )

    # v3.33.5: سجّل أسعار SPX/SPY/QQQ/IWM لبناء ROC20/EMA diagnostics داخلياً.
    for sym, sd in candidates:
        try:
            _record_intraday_price(sym, sd.get("price", 0) or 0)
        except Exception as _ig_rec:
            print(f"[intraday_guard] record from analysis failed {sym}: {_ig_rec}")

    # rebuild flat list for backward compat
    candidates = [
        (sym, sd["strat"], sd["dq"], sd["price"], sd["price_source"])
        for sym, sd in candidates
    ]

    # ── تسجيل كل إشارة مؤهلة في Paper Trading (بصرف النظر عن التنفيذ) ────────
    _log_all_qualified_signals(candidates, analysis)

    qualified_trades: List[Dict[str, Any]] = []

    for symbol, strat, dq, price, price_source in candidates:
        if not strat:
            continue

        qualified, reason = _qualify(
            strat, dq or {}, symbol,
            _verbose=True,
            price=price,
            price_source=price_source,
        )
        if not qualified:
            log_rejection(symbol, strat.get("strategy","?"), strat.get("score",0), reason)
            _record_paper_insert_status(analysis, symbol, strat.get("strategy", "?"), strat.get("score", 0),
                                        selected=False, insert_attempted=False, inserted=False,
                                        block_reason=reason, final_action="qualify_failed",
                                        trade_mode=resolve_trade_mode(strat, analysis))
            try:
                from core.database import log_signal_rejection
                log_signal_rejection(
                    symbol,
                    strat.get("strategy", "?"),
                    strat.get("score", 0),
                    reason,
                )
            except Exception:
                pass
            print(f"[signal] {symbol} رُفض: {reason}")
            continue

        # فحص المخاطرة — أي risk.allowed=False يمنع التسجيل الورقي.
        # هذا يجعل Max Loss وبيانات المخاطرة الناقصة Hard Reject حقيقي قبل log_paper_trade.
        try:
            from core.risk_manager import check_risk
            risk = check_risk(strat, symbol)
        except Exception as e:
            risk = {"allowed": False, "reason": f"Risk check unavailable: {e}", "max_loss_per_contract": 0, "contracts_allowed": 0}

        if not risk.get("allowed", True):
            reason = risk.get("reason", "Risk blocked")
            _record_paper_insert_status(analysis, symbol, strat.get("strategy", "?"), strat.get("score", 0),
                                        selected=True, insert_attempted=False, inserted=False,
                                        block_reason=reason, final_action="rejected_risk",
                                        trade_mode=resolve_trade_mode(strat, analysis))
            try:
                from core.database import log_signal_rejection
                log_signal_rejection(symbol, strat.get("strategy", "?"), strat.get("score", 0), reason)
            except Exception:
                pass
            print(f"[risk_manager] {symbol} hard-blocked: {reason}")
            print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {strat.get('strategy','?')} | score={strat.get('score',0)} | final_action=rejected_risk | reason={reason}")
            continue

        name      = strat.get("strategy", "")
        score     = float(strat.get("score", 0) or 0)
        is_credit = name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
        raw_val   = strat.get("credit") if is_credit else strat.get("debit")
        try:
            val = float(raw_val or 0)
        except Exception:
            val = 0.0

        # إذا لم توجد قيمة credit/debit — حاول استخراجها من legs_detail
        if val <= 0:
            legs = strat.get("legs_detail", [])
            is_credit_strat = name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
            if legs:
                # Debit: long leg ask (أعلى سعر دفع) - short leg bid (أقل سعر استلام)
                longs  = [l for l in legs if l and l.get("side") in ("long",  "buy")]
                shorts = [l for l in legs if l and l.get("side") in ("short", "sell")]
                if not longs:
                    longs  = legs[:len(legs)//2] or legs[:1]
                    shorts = legs[len(legs)//2:] or legs[1:]
                long_ask  = sum(float(l.get("ask") or l.get("mid") or 0) for l in longs)
                short_bid = sum(float(s.get("bid") or s.get("mid") or 0) for s in shorts)
                val = round((short_bid - long_ask) if is_credit_strat else (long_ask - short_bid), 2)
                if val > 0:
                    print(f"[signal] {symbol} {name}: استُخرج val={val:.2f} من legs_detail")
                    if is_credit_strat:
                        strat = {**strat, "credit": val}
                    else:
                        strat = {**strat, "debit": val}

            if val <= 0:
                reason = "Credit/Debit غير صالح للتسجيل (bid/ask مفقود)"
                _record_paper_insert_status(analysis, symbol, name or "?", score, selected=True,
                                            insert_attempted=False, inserted=False, block_reason=reason,
                                            final_action="invalid_credit_debit", trade_mode=resolve_trade_mode(strat, analysis))
                try:
                    from core.database import log_signal_rejection
                    log_signal_rejection(symbol, name or "?", score, reason)
                except Exception:
                    pass
                print(f"[signal] {symbol} رُفض: {reason}")
                continue

        qualified_trades.append({
            "symbol": symbol,
            "strat": strat,
            "price": price,
            "risk": risk,
            "name": name,
            "score": score,
            "is_credit": is_credit,
            "val": val,
        })

    # ── تنفيذ المسارات المفعّلة من الإعدادات ─────────────────────────────────
    from core.database import get_enabled_trade_paths
    enabled_paths = get_enabled_trade_paths()

    # ── لوج المسارات المفعّلة مقابل qualified_trades ──────────────────────────
    enabled_syms_0dte = {p["symbol"] for p in enabled_paths if p["trade_mode"] == "0DTE"}
    qualified_syms    = {t["symbol"] for t in qualified_trades}
    _et_str = _et_now().strftime("%H:%M:%S")
    for sym, sd, _dq, _px, _psrc in candidates:
        # بعد flatten، sd هو قاموس الاستراتيجية نفسه وليس {strat: ...}
        _st = sd if isinstance(sd, dict) else None
        _s_name  = (_st or {}).get("strategy", "No Trade")
        _s_score = (_st or {}).get("score", 0)
        _s_nt    = (_st or {}).get("no_trade", True)
        _in_qual = sym in qualified_syms
        _in_path = sym in enabled_syms_0dte
        if not _in_qual:
            if _s_nt:
                _fa = "study_only" if _s_score > 0 else "rejected"
                _why = "no_trade=True من strategy engine"
            else:
                _fa = "rejected"
                _why = f"qualify failed (score={_s_score})"
        elif not _in_path:
            _fa  = "path_disabled"
            _why = f"path_{sym.lower()}_0dte_paper=0"
            _record_paper_insert_status(analysis, sym, _s_name, _s_score, selected=True,
                                        insert_attempted=False, inserted=False, block_reason=_why,
                                        final_action=_fa, trade_mode="0DTE")
        else:
            _fa  = "pending_execute"
            _why = "qualified — ينتظر execute"
        print(f"[FINAL_ACTION] {_et_str} | {sym} | {_s_name} | score={_s_score} | "
              f"final_action={_fa} | reason={_why}")

    results_all: List[Optional[Dict]] = []

    for path in enabled_paths:
        sym       = path["symbol"]
        t_mode    = path["trade_mode"]     # "0DTE" أو "Swing"
        exec_mode = path["execution_mode"] # Paper-only

        if t_mode == "Swing":
            sw = cache_data.get(sym, {})
            if not sw.get("qualified") or not sw.get("strategy"):
                continue
            strat = sw["strategy"]
            try:
                strat["quote_age_seconds"] = cache_info.get("age_seconds")
            except Exception:
                pass
            if strat.get("no_trade") or strat.get("score", 0) < MIN_SCORE_AUTO:
                continue
            try:
                from core.risk_manager import check_risk
                risk = check_risk(strat, sym)
            except Exception as e:
                risk = {"allowed": False, "reason": f"Risk check unavailable: {e}",
                        "max_loss_per_contract": 0, "contracts_allowed": 0}
            if not risk.get("allowed", True):
                reason = risk.get("reason", "Risk blocked")
                try:
                    from core.database import log_signal_rejection
                    log_signal_rejection(sym, strat.get("strategy", "?"), strat.get("score", 0), reason)
                except Exception:
                    pass
                print(f"[risk_manager] {sym} Swing hard-blocked: {reason}")
                print(f"[FINAL_ACTION] {_et_str} | {sym} | {strat.get('strategy','?')} | score={strat.get('score',0)} | final_action=rejected_risk | reason={reason}")
                continue
            best = {
                "symbol":    sym,
                "strat":     strat,
                "price":     sw.get("price", 0),
                "name":      strat.get("strategy", ""),
                "score":     float(strat.get("score", 0)),
                "is_credit": strat.get("strategy", "") in (
                    "Iron Condor", "Bull Put Spread", "Bear Call Spread"),
                "val":       strat.get("credit") or strat.get("debit") or 0,
                "risk":      risk,
                "_exec_mode": exec_mode,
            }
            print(f"[signal] Swing {sym} ✅ {best['name']} → {exec_mode}")
            r = _execute_trade(best, analysis, is_swing=True,
                               force_exec_mode=exec_mode)
            if r:
                results_all.append(r)

        else:
            # ── 0DTE: من qualified_trades ─────────────────────────────────
            sym_trades = [t for t in qualified_trades if t["symbol"] == sym]
            if not sym_trades:
                print(f"[FINAL_ACTION] {_et_str} | {sym} 0DTE {exec_mode} | "
                      f"final_action=path_disabled_or_not_qualified | "
                      f"reason=no qualified trade for this symbol+path")
                continue
            best = max(sym_trades, key=lambda x: (x["score"], x["val"]))
            best["_exec_mode"] = exec_mode
            print(f"[signal] 0DTE {sym} ✅ {best['name']} → {exec_mode}")
            r = _execute_trade(best, analysis, is_swing=False,
                               force_exec_mode=exec_mode)
            if r:
                results_all.append(r)

    wd_beat("SPX+SPY+QQQ+IWM+DIA+AAPL+NVDA+GLD")
    log_cycle_end("ALL", int((_t.time() - _cycle_start) * 1000))

    if not results_all:
        # إذا سُجّلت إشارات مؤهلة كـ Paper monitoring خارج مسارات التنفيذ،
        # أعد أول سجل حتى تعرضه الواجهة بدلاً من إظهار "لم يُسجّل".
        logged = analysis.get("_paper_logged_trades") or []
        if logged:
            return {"paper_logged": True, "logged_trades": logged}
        return None

    # إذا فُتحت أكثر من صفقة في نفس دورة التحليل، أعد ملخصاً كاملاً للواجهة
    # مع المحافظة على مفاتيح الصفقة الأولى للتوافق مع الكود القديم.
    first = dict(results_all[0])
    first["logged_trades"] = analysis.get("_paper_logged_trades") or results_all
    first["paper_logged"] = True
    return first




def _enrich_strategy_with_sigma_delta(strat: Dict, analysis: Dict, symbol: str, price: float) -> Dict:
    """
    يضيف حقول Delta/Sigma إلى الصفقة قبل حفظها في paper_trades.

    نفس منطق النسخة القديمة للاستراتيجية يبقى كما هو:
      - Credit 0DTE: short strike مبني على 1.5×EM تقريباً.
      - IC: short strikes أبعد قليلاً (~1.6×EM).
      - Debit: long leg مبني على Delta بين 0.36 و0.45، بدون fallback Sigma إذا لم تتوفر دلتا مناسبة.
      - Swing credit: strike مبني على EM×sigma_mult.

    هذه الدالة لا تغيّر اختيار السترايك؛ فقط تحفظ وتعرض التشخيص.
    """
    try:
        sym = (symbol or "").lower()
        data = analysis if sym == "spx" else (analysis.get(sym) or {})
        levels = data.get("levels") or analysis.get("levels") or {}
        em = (strat.get("expected_move") or data.get("expected_move") or
              data.get("swing_em") or levels.get("expected_move") or
              analysis.get("expected_move") or 0)
        try:
            em = float(em or 0)
        except Exception:
            em = 0.0
        px = float(price or strat.get("entry_price") or data.get("price") or 0)

        def _eq(a, b):
            try:
                return abs(float(a) - float(b)) < 1e-6
            except Exception:
                return False

        legs = strat.get("legs_detail") or []
        def _delta_for(strike):
            if strike is None:
                return None
            for leg in legs:
                if _eq(leg.get("strike"), strike):
                    d = leg.get("delta")
                    try:
                        return float(d) if d is not None else None
                    except Exception:
                        return None
            return None

        sp = strat.get("short_put");  lp = strat.get("long_put")
        sc = strat.get("short_call"); lc = strat.get("long_call")
        spd, lpd = _delta_for(sp), _delta_for(lp)
        scd, lcd = _delta_for(sc), _delta_for(lc)

        name = strat.get("strategy", "")
        # strike رئيسي لقياس sigma في الجدول: أقرب short للـ Credit، والـ long للـ Debit
        if name == "Bear Call Spread":
            short_strike, long_strike, side = sc, lc, "call"
            short_delta, long_delta = scd, lcd
        elif name == "Bull Put Spread":
            short_strike, long_strike, side = sp, lp, "put"
            short_delta, long_delta = spd, lpd
        elif name == "Put Debit Spread":
            short_strike, long_strike, side = sp, lp, "put_debit"
            short_delta, long_delta = spd, lpd
        elif name == "Call Debit Spread":
            short_strike, long_strike, side = sc, lc, "call_debit"
            short_delta, long_delta = scd, lcd
        elif name == "Iron Condor":
            # للـ IC نخزن أقرب short strike للسعر؛ ونحتفظ بدلتا الطرفين في حقول منفصلة
            candidates = [(sp, spd, "put"), (sc, scd, "call")]
            candidates = [(k, d, sd) for k, d, sd in candidates if k is not None]
            if candidates and px:
                short_strike, short_delta, side = min(candidates, key=lambda x: abs(float(x[0]) - px))
            else:
                short_strike, short_delta, side = None, None, "ic"
            long_strike, long_delta = None, None
        else:
            short_strike = strat.get("short_strike") or sc or sp
            long_strike  = strat.get("long_strike") or lc or lp
            side = "unknown"
            short_delta = _delta_for(short_strike)
            long_delta  = _delta_for(long_strike)

        distance_points = None
        distance_pct = None
        sigma_distance = None
        if short_strike is not None and px:
            distance_points = float(short_strike) - px
            distance_pct = (distance_points / px) * 100 if px else None
            sigma_distance = abs(distance_points) / em if em else None

        net_delta = None
        ds = [d for d in (spd, lpd, scd, lcd) if d is not None]
        if ds:
            # تقريب اتجاهي: بيع short يعكس إشارة الدلتا، شراء long يحتفظ بها
            try:
                net_delta = 0.0
                if spd is not None: net_delta += -spd
                if scd is not None: net_delta += -scd
                if lpd is not None: net_delta += lpd
                if lcd is not None: net_delta += lcd
            except Exception:
                net_delta = None

        # Credit/Width quality: 0DTE credit requires 25%, Swing credit requires 20%.
        is_credit_strategy = name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
        width = None
        if name == "Iron Condor":
            widths = []
            try:
                if sp is not None and lp is not None: widths.append(abs(float(sp) - float(lp)))
                if sc is not None and lc is not None: widths.append(abs(float(lc) - float(sc)))
            except Exception:
                pass
            width = max(widths) if widths else None
        elif name in ("Bull Put Spread", "Bear Call Spread", "Call Debit Spread", "Put Debit Spread"):
            try:
                if short_strike is not None and long_strike is not None:
                    width = abs(float(short_strike) - float(long_strike))
            except Exception:
                width = None
        credit_val = strat.get("credit") or (strat.get("credit_debit") if is_credit_strategy else None)
        trade_mode_label = (strat.get("trade_mode") or strat.get("selected_mode") or "0DTE").lower()
        min_cw = 0.20 if "swing" in trade_mode_label else 0.25
        cw_ratio = None
        cw_ok = True
        cw_note = None
        try:
            if is_credit_strategy and width and credit_val is not None and float(credit_val) > 0:
                cw_ratio = float(credit_val) / float(width)
                cw_ok = cw_ratio >= min_cw
                if not cw_ok:
                    cw_note = (f"Credit too small: credit/width={cw_ratio:.1%} < "
                               f"required {min_cw:.0%}; need ≥ {float(width)*min_cw:.2f}")
        except Exception:
            pass

        strat.update({
            "underlying_price": px or None,
            "expected_move": em or None,
            "em_upper": data.get("em_upper") or levels.get("em_upper") or (px + em if px and em else None),
            "em_lower": data.get("em_lower") or levels.get("em_lower") or (px - em if px and em else None),
            "sigma_distance": round(sigma_distance, 2) if sigma_distance is not None else None,
            "sigma_side": side,
            "short_delta": round(short_delta, 3) if short_delta is not None else None,
            "long_delta": round(long_delta, 3) if long_delta is not None else None,
            "short_put_delta": round(spd, 3) if spd is not None else None,
            "long_put_delta": round(lpd, 3) if lpd is not None else None,
            "short_call_delta": round(scd, 3) if scd is not None else None,
            "long_call_delta": round(lcd, 3) if lcd is not None else None,
            "net_delta": round(net_delta, 3) if net_delta is not None else None,
            "short_strike": short_strike,
            "long_strike": long_strike,
            "distance_points": round(distance_points, 2) if distance_points is not None else None,
            "distance_pct": round(distance_pct, 2) if distance_pct is not None else None,
            "target_delta": strat.get("target_delta"),
            "credit_width_ratio": round(cw_ratio, 3) if cw_ratio is not None else strat.get("credit_width_ratio"),
            "min_credit_width_ratio": min_cw if is_credit_strategy else strat.get("min_credit_width_ratio"),
            "credit_width_ok": cw_ok if is_credit_strategy else strat.get("credit_width_ok", True),
            "credit_width_note": cw_note or strat.get("credit_width_note"),
        })
    except Exception as e:
        print(f"[sigma_delta_enrich] {symbol}: {e}")
    return strat

def _execute_trade(best: Dict, analysis: Dict, is_swing: bool,
                   force_exec_mode: str = None) -> Optional[Dict]:
    """
    ينفّذ صفقة واحدة ويُعيد النتيجة.

    force_exec_mode: في هذه النسخة Paper فقط.
      Paper = تنفيذ ورقي عبر log_paper_trade
    """
    symbol    = best["symbol"]
    strat     = best["strat"]
    price     = best["price"]
    risk      = best["risk"]
    name      = best["name"]
    score     = best["score"]
    is_credit = best["is_credit"]
    val       = best["val"]

    # استخراج settlement info من الـ chain إذا كان متاحاً
    _chain_settle = {}
    if symbol.upper() == "SPX":
        try:
            from core.analyzer import classify_spx_expiry
            _expiry_check = strat.get("expiry_date") or ""
            _exp_type     = strat.get("expiration_type", "")
            _chain_settle = classify_spx_expiry(_expiry_check, _exp_type)
        except Exception:
            _chain_settle = {"settlement_type": "PM", "root_symbol": "SPXW",
                             "last_trade_time": "15:30 ET"}
    else:
        _chain_settle = {"settlement_type": "PM", "root_symbol": symbol,
                         "last_trade_time": "15:30 ET"}

    # تحديد Trade Mode و expiry — مصدر موحد
    trade_mode  = resolve_trade_mode(strat, analysis)
    expiry_date = resolve_expiry_date(strat, analysis)
    dte_entry   = strat.get("dte_at_entry") or strat.get("target_dte") or 0

    if trade_mode == "Swing":
        if not expiry_date:
            expiry_date = _swing_expiry(dte_entry)
        # حماية: Swing بتاريخ اليوم = خطأ منطقي → رفض
        if expiry_date == datetime.now().strftime("%Y-%m-%d"):
            print(f"[signal] ❌ رُفض: Swing بتاريخ اليوم ({expiry_date})")
            return None
    else:
        expiry_date = datetime.now().strftime("%Y-%m-%d")

    # ── فحص وقت السوق — Auto Paper Register لا يعمل خارج السوق
    market_open_for_trade = _is_market_open()
    if not market_open_for_trade:
        et = _et_now()
        print(f"[execute] ⏸ السوق مغلق (ET {et.strftime('%H:%M')}) — "
              f"skip auto Paper registration; analysis only.")
        try:
            analysis.setdefault("_paper_skipped_market_closed", []).append({
                "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                "score": score, "reason": "market_closed_no_auto_paper_registration"
            })
            _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                        insert_attempted=False, inserted=False,
                                        block_reason="market_closed_no_auto_paper_registration",
                                        final_action="skip_market_closed", trade_mode=trade_mode)
        except Exception:
            pass
        return None

    # ── التحكم في تكرار الصفقات الورقية ─────────────────────────────────────
    # في وضع الدراسة يمكن السماح بعدد غير محدود من نفس الاستراتيجية.
    # إذا allow_duplicate_open_strategies=1 فلا نمنع التكرار بسبب وجود صفقة مفتوحة.
    try:
        from core.database import get_setting, get_paper_open_count_for_strategy
        allow_dup = str(get_setting("allow_duplicate_open_strategies", "0")).strip() in (
            "1", "true", "True", "yes", "YES", "نعم"
        )
        if not allow_dup and get_paper_open_count_for_strategy(symbol, name, trade_mode) >= 1:
            print(f"[execute] {symbol} {name} {trade_mode} — مفتوحة بالفعل في Paper، رُفض حسب إعداد منع التكرار")
            return None
        if allow_dup:
            print(f"[execute] duplicate paper strategies allowed → will register if qualified: {symbol} {name} {trade_mode}")
    except Exception as _e:
        print(f"[execute] paper dup check error: {_e}")

    # هدف الربح حسب الـ Mode
    # RC13: Swing لا يستخدم TP/SL percentage؛ target=0 للتشخيص فقط حتى لا يظهر TP وهمي.
    if trade_mode == "Swing":
        target = 0.0
    else:
        target = round(val * (1 - PROFIT_TARGET_PCT) if is_credit else val * (1 + PROFIT_TARGET_PCT), 2)

    legs = _build_legs(strat)

    # RC15f - Swing ICT/SMC + EMA Confirmation Layer.
    # Hard block for Swing only; 0DTE and base score/GEX/IV/Delta logic are unchanged.
    try:
        if str(trade_mode or "") == "Swing":
            blocked_rc15f, rc15f_reason, rc15f_diag = _swing_ict_smc_ema_guard(
                analysis, strat, symbol, name, price, trade_mode
            )
            analysis.setdefault("_swing_ict_smc_ema_confirmation", []).append({
                **rc15f_diag, "blocked": bool(blocked_rc15f), "reason": rc15f_reason,
            })
            print(f"[rc15f_swing_ict_smc_ema] {symbol} {name} | blocked={blocked_rc15f} | {rc15f_reason}")
            if blocked_rc15f:
                _rc15f_action = _rc15f_final_action_from_diag(rc15f_diag)
                print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                      f"score={score:.0f} | final_action={_rc15f_action} | {rc15f_reason}")
                try:
                    from core.database import log_signal_rejection
                    log_signal_rejection(symbol, name, int(score or 0), rc15f_reason)
                except Exception:
                    pass
                try:
                    analysis.setdefault("_paper_skipped_swing_ict_smc_ema", []).append({
                        "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                        "score": score, "reason": rc15f_reason, **rc15f_diag,
                    })
                    _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                insert_attempted=False, inserted=False,
                                                block_reason=rc15f_reason,
                                                final_action=_rc15f_action,
                                                trade_mode=trade_mode)
                except Exception:
                    pass
                return None
    except Exception as _rc15f_e:
        reason = f"RC15f Swing blocked: ict_smc_ema_guard_error:{_rc15f_e}"
        print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
              f"score={score:.0f} | final_action=skip_swing_ict_smc_ema | {reason}")
        try:
            from core.database import log_signal_rejection
            log_signal_rejection(symbol, name, int(score or 0), reason)
        except Exception:
            pass
        return None

    # ── v3.33.5 — Intraday Reversal Guard قبل تسجيل الصفقة ────────────────
    # يمنع 0DTE Debit إذا الحركة اللحظية عكس اتجاه الصفقة.
    try:
        blocked_ig, ig_reason, ig_diag = _intraday_reversal_guard(
            analysis, symbol, name, price, trade_mode
        )
        analysis.setdefault("_intraday_guard", []).append({
            **ig_diag, "blocked": bool(blocked_ig), "reason": ig_reason,
        })
        print(f"[intraday_guard] {symbol} {name} | blocked={blocked_ig} | {ig_reason}")
        if blocked_ig:
            print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                  f"score={score:.0f} | final_action=skip_intraday_reversal_guard | {ig_reason}")
            try:
                analysis.setdefault("_paper_skipped_intraday_guard", []).append({
                    "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                    "score": score, "reason": ig_reason, **ig_diag,
                })
                _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                            insert_attempted=False, inserted=False,
                                            block_reason=ig_reason, final_action="skip_intraday_reversal_guard",
                                            trade_mode=trade_mode)
            except Exception:
                pass
            return None
    except Exception as _ig_e:
        print(f"[intraday_guard] error-pass {symbol} {name}: {_ig_e}")

    # ── v3.33.6a RC7 — SPY/QQQ/IWM 0DTE Intraday EMA Alignment + Exposure Cap ──
    # خاص بـ SPY/QQQ/IWM فقط: 15m و5m يجب أن يتفقا مع اتجاه الاستراتيجية.
    try:
        if str(trade_mode or "") == "0DTE" and str(symbol or "").upper() in ("SPY", "QQQ", "IWM"):
            blocked_ema, ema_reason, ema_diag = _spy_qqq_intraday_ema_alignment_guard(
                analysis, symbol, name, price, trade_mode
            )
            analysis.setdefault("_intraday_ema_alignment", []).append({
                **ema_diag, "blocked": bool(blocked_ema), "reason": ema_reason,
            })
            print(f"[intraday_ema_alignment] {symbol} {name} | blocked={blocked_ema} | {ema_reason}")
            if blocked_ema:
                print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                      f"score={score:.0f} | final_action=skip_intraday_ema_alignment | {ema_reason}")
                try:
                    from core.database import log_signal_rejection
                    log_signal_rejection(symbol, name, int(score or 0), ema_reason)
                except Exception:
                    pass
                try:
                    analysis.setdefault("_paper_skipped_intraday_ema_alignment", []).append({
                        "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                        "score": score, "reason": ema_reason, **ema_diag,
                    })
                    _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                insert_attempted=False, inserted=False,
                                                block_reason=ema_reason, final_action="skip_intraday_ema_alignment",
                                                trade_mode=trade_mode)
                except Exception:
                    pass
                return None

            direction = _strategy_direction(name)
            if direction in ("bearish", "bullish"):
                blocked_reentry, reentry_reason, reentry_diag = _spy_qqq_iwm_light_reentry_guard(
                    symbol, direction, ema_diag
                )
                analysis.setdefault("_rc11_reentry_control", []).append({
                    **reentry_diag, "blocked": bool(blocked_reentry), "reason": reentry_reason,
                })
                print(f"[rc11_reentry_control] {symbol} {name} | blocked={blocked_reentry} | {reentry_reason}")
                if blocked_reentry:
                    print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                          f"score={score:.0f} | final_action=skip_rc11_reentry_control | {reentry_reason}")
                    try:
                        from core.database import log_signal_rejection
                        log_signal_rejection(symbol, name, int(score or 0), reentry_reason)
                    except Exception:
                        pass
                    try:
                        analysis.setdefault("_paper_skipped_rc11_reentry_control", []).append({
                            "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                            "score": score, "direction": direction, "reason": reentry_reason, **reentry_diag,
                        })
                        _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                    insert_attempted=False, inserted=False,
                                                    block_reason=reentry_reason, final_action="skip_rc11_reentry_control",
                                                    trade_mode=trade_mode)
                    except Exception:
                        pass
                    return None

                open_dir_count = _count_open_same_symbol_direction_0dte(symbol, direction)
                if open_dir_count >= MAX_OPEN_SAME_SYMBOL_DIRECTION_0DTE:
                    reason = (f"same_symbol_direction_0dte_cap_reached: {symbol} {direction} "
                              f"open_count={open_dir_count}/{MAX_OPEN_SAME_SYMBOL_DIRECTION_0DTE}")
                    print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                          f"score={score:.0f} | final_action=skip_exposure_cap | {reason}")
                    try:
                        from core.database import log_signal_rejection
                        log_signal_rejection(symbol, name, int(score or 0), reason)
                    except Exception:
                        pass
                    try:
                        analysis.setdefault("_paper_skipped_exposure_cap", []).append({
                            "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                            "score": score, "direction": direction,
                            "open_count": open_dir_count,
                            "limit": MAX_OPEN_SAME_SYMBOL_DIRECTION_0DTE,
                            "reason": reason,
                        })
                        _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                    insert_attempted=False, inserted=False,
                                                    block_reason=reason, final_action="skip_exposure_cap",
                                                    trade_mode=trade_mode)
                    except Exception:
                        pass
                    return None
    except Exception as _ema_e:
        # فشل الحارس نفسه لا يُسمح له بفتح صفقة غير مؤكدة على SPY/QQQ/IWM 0DTE.
        reason = f"intraday_ema_alignment_error_reject:{_ema_e}"
        print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
              f"score={score:.0f} | final_action=skip_intraday_ema_alignment | {reason}")
        try:
            from core.database import log_signal_rejection
            log_signal_rejection(symbol, name, int(score or 0), reason)
        except Exception:
            pass
        return None

    # ── v3.33.4 — SPX 0DTE Debit Risk Control قبل تسجيل الصفقة ─────────────
    if symbol.upper() == "SPX" and trade_mode == "0DTE":
        minutes_to_close = _minutes_to_market_close()
        if minutes_to_close is not None and minutes_to_close <= SPX_0DTE_NO_NEW_ENTRY_MINUTES_TO_CLOSE:
            reason = (f"SPX_0DTE_NO_NEW_ENTRY_LAST_3H minutes_to_close={minutes_to_close} "
                      f"<= {SPX_0DTE_NO_NEW_ENTRY_MINUTES_TO_CLOSE}")
            print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                  f"score={score:.0f} | final_action=skip_risk_control | {reason}")
            try:
                analysis.setdefault("_paper_skipped_risk_control", []).append({
                    "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                    "score": score, "reason": reason,
                })
                _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                            insert_attempted=False, inserted=False,
                                            block_reason=reason, final_action="skip_risk_control",
                                            trade_mode=trade_mode)
            except Exception:
                pass
            return None

        if _is_0dte_debit_strategy(name):
            width = _spread_width_from_strategy(strat, legs)
            if width and width > 0 and val is not None:
                debit_width_ratio = float(val) / float(width)
                if debit_width_ratio > SPX_0DTE_DEBIT_MAX_DEBIT_WIDTH_RATIO:
                    reason = (f"SPX_0DTE_DEBIT_TOO_EXPENSIVE debit/width={debit_width_ratio:.1%} "
                              f"> {SPX_0DTE_DEBIT_MAX_DEBIT_WIDTH_RATIO:.0%} "
                              f"debit={float(val):.2f} width={float(width):.2f}")
                    print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                          f"score={score:.0f} | final_action=skip_risk_control | {reason}")
                    try:
                        analysis.setdefault("_paper_skipped_risk_control", []).append({
                            "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                            "score": score, "reason": reason,
                        })
                        _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                    insert_attempted=False, inserted=False,
                                                    block_reason=reason, final_action="skip_risk_control",
                                                    trade_mode=trade_mode)
                    except Exception:
                        pass
                    return None

            if name == "Put Debit Spread":
                daily_count = _count_daily_spx_put_debit_paper_trades()
                if daily_count >= MAX_DAILY_SPX_PUT_DEBIT_SPREADS:
                    reason = (f"SPX_PUT_DEBIT_DAILY_CAP reached {daily_count}/"
                              f"{MAX_DAILY_SPX_PUT_DEBIT_SPREADS}")
                    print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | "
                          f"score={score:.0f} | final_action=skip_risk_control | {reason}")
                    try:
                        analysis.setdefault("_paper_skipped_risk_control", []).append({
                            "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                            "score": score, "reason": reason,
                        })
                        _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                    insert_attempted=False, inserted=False,
                                                    block_reason=reason, final_action="skip_risk_control",
                                                    trade_mode=trade_mode)
                    except Exception:
                        pass
                    return None

    trade_signature = _build_trade_signature(symbol, trade_mode, name, expiry_date, legs)
    blocked_dup, dup_reason = _check_exact_duplicate_signature(trade_signature)
    if blocked_dup:
        print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | score={score:.0f} | final_action=skip_duplicate | {dup_reason} | signature={trade_signature}")
        try:
            analysis.setdefault("_paper_skipped_duplicates", []).append({
                "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                "expiry_date": expiry_date, "score": score,
                "trade_signature": trade_signature, "reason": dup_reason,
            })
            _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                        insert_attempted=False, inserted=False, block_reason=dup_reason,
                                        final_action="skip_duplicate", trade_mode=trade_mode)
        except Exception:
            pass
        return None

    try:
        from core.strategy_engine import get_threshold_mode
        mode_tag = get_threshold_mode()
    except Exception:
        mode_tag = "conservative"



    # ── تحديد مسار التنفيذ — Paper فقط ─────────────────────────────────────
    route_to_paper = True
    exec_tag = "Paper"
    _et_now_str = _et_now().strftime("%H:%M:%S")
    print(f"[signal] ✅ {symbol} {name} score={score:.0f} | "
          f"mode={trade_mode} | exec={exec_tag}")
    print(f"[FINAL_ACTION] {_et_now_str} | {symbol} | {name} | score={score:.0f} | "
          f"final_action=paper_opening | "
          f"exec_mode={exec_tag} | val={val}")

    result   = None
    trade_id = None

    # ── Paper Execution فقط (Swing أو 0DTE حسب trade_mode) ───────────────
    # فحص عمر Cache فقط لـ Swing — 0DTE لا يحتاجه
    if trade_mode == "Swing":
        cache_ok, cache_err = _swing_cache_age_ok()
        if not cache_ok:
            print(f"[signal] ❌ {cache_err}")
            return None

    # ── Paper Execution (Swing أو 0DTE حسب trade_mode) ───────────────────
    # نبني strat_paper بكل الحقول المطلوبة صراحةً
    strat_paper = {
        **strat,
        # الحقول الأساسية
        "strategy":        name,
        "trade_mode":      trade_mode,
        "selected_mode":   trade_mode,
        "expiry_date":     expiry_date,
        "dte_at_entry":    dte_entry,
        # السعر
        "entry_price":     price,
        "credit_debit":    val,
        "credit":          val if is_credit else None,
        "debit":           val if not is_credit else None,
        # الأرجل
        "short_put":       legs.get("short_put"),
        "long_put":        legs.get("long_put"),
        "short_call":      legs.get("short_call"),
        "long_call":       legs.get("long_call"),
        # Score / quality
        "score":           score,
        "setup_quality":   _paper_quality_tag(strat, market_open_for_trade),
        "trade_signature": trade_signature,
        "duplicate_note":  "registered_distinct_signal",
        "paper_only":      True,
        # RC15f Swing ICT/SMC + EMA confirmation diagnostics.
        "ict_smc_score":        strat.get("ict_smc_score"),
        "ict_smc_pass":         strat.get("ict_smc_pass"),
        "ict_smc_confidence":   strat.get("ict_smc_confidence"),
        "ema_alignment_pass":   strat.get("ema_alignment_pass"),
        "swing_block_reason":   strat.get("swing_block_reason"),
        "swing_watchlist_reason": strat.get("swing_watchlist_reason"),
        "ict_smc_details":      strat.get("ict_smc_details"),
        "ema_details":          strat.get("ema_details"),
        "rc15f_swing_confirmation": strat.get("rc15f_swing_confirmation"),
        "smc_0dte_mtf":         strat.get("smc_0dte_mtf"),
        "smc_0dte_mtf_full":    strat.get("smc_0dte_mtf_full"),
        # Settlement
        "settlement_type": _chain_settle.get("settlement_type", "PM"),
        "root_symbol":     _chain_settle.get("root_symbol", "SPXW"),
        "last_trade_time": _chain_settle.get("last_trade_time", "15:30 ET"),
    }
    strat_paper = _enrich_strategy_with_sigma_delta(strat_paper, analysis, symbol, price)
    try:
        from core.database import log_paper_trade as _log_paper
        # RC15i-1: final atomic duplicate guard. The last exact-signature
        # check, same symbol+strategy+mode check, and INSERT must be under
        # the same lock to prevent two 0DTE threads from both passing SELECT
        # before either INSERT is committed. This does not change strategy rules.
        with _qualify_lock:
            blocked_dup, dup_reason = _check_exact_duplicate_signature(trade_signature)
            if blocked_dup:
                print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | score={score:.0f} | final_action=skip_duplicate_atomic | {dup_reason} | signature={trade_signature}")
                try:
                    analysis.setdefault("_paper_skipped_duplicates", []).append({
                        "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                        "expiry_date": expiry_date, "score": score,
                        "trade_signature": trade_signature, "reason": dup_reason,
                        "stage": "atomic_pre_insert",
                    })
                    _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                insert_attempted=False, inserted=False,
                                                block_reason=dup_reason, final_action="skip_duplicate_atomic",
                                                trade_mode=trade_mode)
                except Exception:
                    pass
                return None

            blocked_open_dup, open_dup_reason = _check_open_symbol_strategy_duplicate(symbol, name, trade_mode)
            if blocked_open_dup:
                print(f"[FINAL_ACTION] {_et_now().strftime('%H:%M:%S')} | {symbol} | {name} | score={score:.0f} | final_action=skip_duplicate_open_strategy_atomic | {open_dup_reason}")
                try:
                    analysis.setdefault("_paper_skipped_duplicates", []).append({
                        "symbol": symbol, "strategy": name, "trade_mode": trade_mode,
                        "expiry_date": expiry_date, "score": score,
                        "trade_signature": trade_signature, "reason": open_dup_reason,
                        "stage": "atomic_pre_insert",
                    })
                    _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                                insert_attempted=False, inserted=False,
                                                block_reason=open_dup_reason,
                                                final_action="skip_duplicate_open_strategy_atomic",
                                                trade_mode=trade_mode)
                except Exception:
                    pass
                return None

            _paper_id = _log_paper(
                strat         = strat_paper,
                symbol        = symbol,
                selected_mode = trade_mode,
                source        = "paper_execution",
            )
        try:
            analysis.setdefault("_paper_logged_trades", []).append({
                "paper_id": _paper_id,
                "symbol": symbol,
                "strategy": name,
                "score": score,
                "trade_mode": trade_mode,
                "expiry_date": expiry_date,
                "credit_debit": val,
                "source": "paper_execution",
                "trade_signature": trade_signature,
                "duplicate_note": "registered_distinct_signal",
            })
            _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                        insert_attempted=True, inserted=True, inserted_trade_id=_paper_id,
                                        block_reason="", final_action="paper_opened", trade_mode=trade_mode)
        except Exception:
            pass
        log_trade_open(symbol, name, "Paper", trade_mode, val, score)
        print(f"[signal] ✅ routed to Paper ← {symbol} {name} "
              f"expiry={expiry_date} DTE={dte_entry} "
              f"{'credit' if is_credit else 'debit'}={val}")
        print(f"[FINAL_ACTION] {_et_now_str} | {symbol} | {name} | score={score:.0f} | "
              f"final_action=paper_opened | expiry={expiry_date} | val={val}")
        result = {
            "id":           f"paper_{symbol}_{name}",
            "symbol":       symbol,
            "strategy":     name,
            "score":        score,
            "price":        price,
            "legs":         legs,
            "credit_debit": val,
            "target":       target,
            "is_credit":    is_credit,
            "risk":         risk,
            "trade_mode":   trade_mode,
            "expiry_date":  expiry_date,
            "dte_at_entry": dte_entry,
            "paper_only":   True,
            # RC15b: Telegram source-of-truth fields.  The alert is allowed only
            # after log_paper_trade returned a real positive row ID.
            "inserted":     bool(int(_paper_id or 0) > 0),
            "trade_id":     int(_paper_id),
            "paper_id":     int(_paper_id),
            "trade_signature": trade_signature,
            "setup_quality": strat_paper.get("setup_quality"),
            "ict_smc_score": strat_paper.get("ict_smc_score"),
            "ict_smc_confidence": strat_paper.get("ict_smc_confidence"),
            "ema_alignment_pass": strat_paper.get("ema_alignment_pass"),
        }
    except Exception as _pe:
        import traceback
        _record_paper_insert_status(analysis, symbol, name, score, selected=True,
                                    insert_attempted=True, inserted=False,
                                    block_reason=f"database_insert_error:{type(_pe).__name__}: {str(_pe)[:160]}",
                                    final_action="paper_insert_error", trade_mode=trade_mode)
        print(f"[signal] ❌ فشل تسجيل Paper: {_pe}")
        traceback.print_exc()
        return None
    exec_label = "Paper"
    print(f"[signal] تم تسجيل {exec_label}: {symbol} {name} score={score}/100")
    _send_entry_alert(result, strat)
    return result


def _send_entry_alert(t: Dict, strat: Dict) -> None:
    """إرسال تنبيه Telegram فقط بعد إدراج Paper Trade فعلي وموثّق في DB."""
    # RC15b: do not let a qualified/selected candidate trigger Telegram.
    # The database row ID is the source of truth, not qualification or ranking.
    try:
        trade_id = int(t.get("trade_id") or t.get("paper_id") or 0)
    except Exception:
        trade_id = 0
    if not bool(t.get("inserted")) or trade_id <= 0:
        print(f"[telegram_entry] SKIP no confirmed insert | trade_id={trade_id} inserted={bool(t.get('inserted'))}")
        return
    try:
        from core.database import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT id, status FROM paper_trades WHERE id=? LIMIT 1",
                (trade_id,),
            ).fetchone()
        if not row:
            print(f"[telegram_entry] SKIP DB row missing | trade_id={trade_id}")
            return
    except Exception as exc:
        # Fail closed: if DB verification fails, no Telegram alert is sent.
        print(f"[telegram_entry] SKIP DB verification error | trade_id={trade_id} | {exc}")
        return

    trade_mode = t.get("trade_mode", "0DTE")
    symbol     = t.get("symbol", "—")
    is_credit  = t.get("is_credit", False)
    val_lbl    = "Credit" if is_credit else "Debit"
    mode_tag   = f"{trade_mode}"

    msg = (
        f"📋 <b>[PAPER {mode_tag}] صفقة ورقية</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 {symbol} — {t['strategy']}\n"
        f"🆔 Trade ID: {trade_id}\n"
        f"💰 {val_lbl}: {t.get('credit_debit', 0):.2f}\n"
        f"🎯 هدف: {t.get('target', 0):.2f}\n"
        f"📅 Expiry: {t.get('expiry_date','—')}  DTE: {t.get('dte_at_entry','—')}\n"
        f"📈 Score: {t.get('score', 0)}/100\n"
        f"🏷 Quality: {t.get('setup_quality', 'REALISTIC_PAPER')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ هذه صفقة ورقية — لا تنفيذ حقيقي\n"
        f"⏱ {datetime.now().strftime('%H:%M')} ET"
    )
    try:
        send_message(msg)
    except Exception:
        pass


# ── متابعة الصفقات المفتوحة ───────────────────────────────────────────────────

def monitor_open_trades(analysis: Dict) -> List[Dict]:
    """
    يُستدعى في كل دورة تشغيل تلقائي.
    يتحقق من شرط 50% أو إغلاق السوق لكل صفقة مفتوحة.
    يعيد قائمة الصفقات المُغلقة في هذه الدورة.
    """
    open_trades = get_open_trades()
    if not open_trades:
        return []

    closed = []
    near_close = _is_near_close()

    for t in open_trades:
        symbol   = t["symbol"]
        is_credit= bool(t["is_credit"])
        entry_val= t["credit_debit"]
        target   = t["target"]
        legs     = json.loads(t["legs_json"] or "{}")

        trade_mode  = t.get("trade_mode", "0DTE")

        # ── صفقات Swing: تُراقَب حصراً في refresh_open_trade_prices (chain أسبوعي)
        # هذه الدالة تستخدم chain اليوم (0DTE) فقط — لا تلمس Swing أبداً
        if trade_mode == "Swing":
            continue

        # ── 0DTE فقط من هنا ──────────────────────────────────────────────────
        current_val = _get_current_spread_value(symbol, legs, analysis)

        if near_close:
            # إغلاق إجباري عند 15:30 ET
            profit_pct = _calc_profit(is_credit, entry_val, current_val) if current_val else 0.0
            result     = _classify_result(profit_pct)
            _close_trade(t, current_val or 0, result, profit_pct, "إغلاق السوق EOD")
            closed.append({**t, "result": result, "profit_pct": profit_pct, "reason": "market_close"})
            continue

        if current_val is None:
            continue

        # هدف الربح 50%
        if _hit_target(is_credit, current_val, target):
            profit_pct = _calc_profit(is_credit, entry_val, current_val)
            _close_trade(t, current_val, "WIN", profit_pct, "50% profit target")
            closed.append({**t, "result": "WIN", "profit_pct": profit_pct, "reason": "profit_target"})

        # 0DTE: لا يوجد وقف خسارة تلقائي حسب القاعدة الحالية.

    return closed


def _classify_result(profit_pct: float) -> str:
    """
    v3.33.4: تصنيف النتيجة حسب P&L الفعلي فقط حتى لا تظهر أرباح جزئية كـ تعادل:
      P&L > 0  → WIN
      P&L = 0  → BREAKEVEN
      P&L < 0  → LOSS
    """
    try:
        p = float(profit_pct or 0)
    except Exception:
        p = 0.0
    if p > 0:
        return "WIN"
    if p < 0:
        return "LOSS"
    return "BREAKEVEN"


def _hit_target(is_credit: bool, current: float, target: float) -> bool:
    try:
        cur = float(current)
        tgt = float(target)
    except Exception:
        return False
    if is_credit:
        return cur <= (tgt + TP_HIT_TOLERANCE)   # credit: ننتظر انخفاض تكلفة الإغلاق
    else:
        return cur >= (tgt - TP_HIT_TOLERANCE)   # debit: ننتظر ارتفاع قيمة الإغلاق


def _calc_profit(is_credit: bool, entry: float, current: float) -> float:
    if entry <= 0:
        return 0.0
    if is_credit:
        return round((entry - current) / entry * 100, 1)
    else:
        return round((current - entry) / entry * 100, 1)


def _get_current_spread_value(symbol: str, legs: Dict, analysis: Dict) -> Optional[float]:
    """
    يحسب القيمة الحالية للـ spread من آخر option chain في التحليل.
    """
    if symbol == "SPX":
        chain = analysis.get("_chain")
    elif symbol == "SPY":
        chain = (analysis.get("spy") or {}).get("_chain")
    elif symbol == "QQQ":
        chain = (analysis.get("qqq") or {}).get("_chain")
    else:
        return None

    if not chain:
        return None

    calls = {o["strike"]: o for o in chain.get("calls", [])}
    puts  = {o["strike"]: o for o in chain.get("puts", [])}

    def mid(opt):
        if not opt:
            return None
        bid = opt.get("bid")
        ask = opt.get("ask")
        if bid is not None and ask is not None and bid >= 0 and ask >= 0:
            return round((bid + ask) / 2, 2)
        return opt.get("mid")

    total = 0.0
    found = 0

    for leg_key, strike in legs.items():
        if strike is None:
            continue
        if "call" in leg_key:
            opt = calls.get(strike)
        else:
            opt = puts.get(strike)
        m = mid(opt)
        if m is None:
            return None   # بيانات ناقصة → لا نحكم
        if leg_key.startswith("short"):
            total -= m    # الـ short نبيعه فنخسم قيمته
        else:
            total += m    # الـ long نشتريه فنضيف قيمته
        found += 1

    if found == 0:
        return None
    # للـ credit spread: القيمة الحالية للالتزام = -total
    # للـ debit spread: القيمة الحالية للأصل = total
    return round(abs(total), 2)


def _log_mode_perf(t: Dict, exit_price: float, result: str,
                   pnl_pct: float, pnl_dollar: float) -> None:
    """يسجل النتيجة في جدول mode_performance."""
    try:
        log_mode_performance({
            **t,
            "exit_price": exit_price,
            "result":     result,
            "profit_pct": pnl_pct,
            "pnl_dollar": pnl_dollar,
        })
    except Exception:
        pass


def _close_trade(t: Dict, exit_price: float, result: str,
                 profit_pct: float, reason: str) -> None:
    log_trade_close(t.get("symbol","?"), t.get("strategy","?"),
                    result, profit_pct, reason, t.get("id"))
    close_open_trade(t["id"], exit_price, result, profit_pct, reason)

    # حفظ في سجل الصفقات الرئيسي مع تصنيف دقيق
    now = datetime.now()
    result_ar = {"WIN": "ربح", "PARTIAL WIN": "ربح جزئي",
                 "BREAKEVEN": "تعادل", "LOSS": "خسارة"}.get(result, "خسارة")
    r_val    = round(profit_pct / 100, 2)
    r_signed = r_val if profit_pct >= 0 else -abs(r_val)
    entry_val = float(t.get("credit_debit") or 0)
    pnl_dollar = round((entry_val - exit_price if t.get("is_credit", 1)
                        else exit_price - entry_val) * 100, 2)
    try:
        save_trade(
            date      = now.strftime("%Y-%m-%d"),
            time_str  = now.strftime("%H:%M"),
            result    = result_ar,
            r_value   = r_signed,
            amount    = abs(pnl_dollar),
            notes     = f"AutoPaper | {t['symbol']} {t['strategy']} | {profit_pct:+.1f}% | {reason} | OpenTradeID={t.get('id')}",
            spx_price = t.get("entry_price", 0),
            strategy  = t["strategy"],
            trade_type= "AUTO_PAPER",
        )
    except Exception:
        pass

    # تحديث الرصيد عند إغلاق الصفقة
    try:
        from core.database import balance_close_trade, get_setting
        wing_width = float(get_setting(
            f"wing_width_{t['symbol'].lower()}"
            if t["symbol"] in ("SPY","QQQ") else "wing_width_spx", "5") or 5)
        if str(t.get("symbol", "")).upper() == "SPX" and wing_width <= 5:
            print("[RC15i][settings_warn] SPX wing_width_spx<=5 may cause excessive CW_REJECT; "
                  "kept unchanged by hotfix because strike-width rules were not modified.")
        balance_close_trade(t["id"], t["symbol"], t["strategy"],
                            wing_width, pnl_dollar)
    except Exception as _be:
        print(f"[balance] close error: {_be}")

    _send_exit_alert(t, exit_price, result, profit_pct, reason)


def _send_exit_alert(t: Dict, exit_price: float, result: str,
                     profit_pct: float, reason: str) -> None:
    icons = {
        "WIN":         "🏁",
        "PARTIAL WIN": "🟡",
        "BREAKEVEN":   "⚖️",
        "LOSS":        "❌",
    }
    icon      = icons.get(result, "📊")
    sign      = "+" if profit_pct >= 0 else ""
    close_lbl = "50% Profit Target ✅" if "profit" in reason else "إغلاق السوق ⏰"
    msg = (
        f"{icon} <b>تم إغلاق الصفقة</b>\n"
        f"📊 {t['symbol']} — {t['strategy']}\n"
        f"💰 Entry: {t['credit_debit']:.2f}  →  Exit: {exit_price:.2f}\n"
        f"📈 Result: <b>{result}</b>\n"
        f"💹 P&L: <b>{sign}{profit_pct:.1f}%</b>\n"
        f"📝 {close_lbl}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⏱ {datetime.now().strftime('%H:%M')} ET"
    )
    try:
        send_message(msg)
    except Exception:
        pass


# ── Live Price Refresh ────────────────────────────────────────────────────────

def _get_spread_from_chain(legs: Dict, chain: Dict) -> Optional[float]:
    """
    RC15c — يحسب قيمة إغلاق السبريد المفتوح من option chain الحالي.

    المخرجات لها نفس المعنى القديم حتى لا تتغير قواعد الخروج:
      - Debit spread: القيمة التي نستلمها عند الإغلاق.
      - Credit spread: تكلفة الإغلاق الحالية.

    الإصلاح هنا يعالج حالة ظهرت عملياً: Tastytrade يعرض SPY 750/745 Put Debit
    قريباً من TP @ 2.27 بينما شاشة Paper Trades كانت تعرض قيمة أقل بكثير.
    لذلك نحسب ثلاث طبقات ونأخذ القيمة المنطقية المحافظة:
      1) mid close value من bid/ask لكل رجل،
      2) natural/executable close value: long legs @ bid و short legs @ ask،
      3) intrinsic floor للـ 0DTE debit verticals إذا توفر سعر الأصل.

    إذا كانت chain/quotes ناقصة نرجع None ولا نغلق على بيانات جزئية.
    """
    if not chain:
        return None

    def _f(x, default=None):
        try:
            if x is None or x == "":
                return default
            return float(x)
        except Exception:
            return default

    calls = {_f(o.get("strike")): o for o in chain.get("calls", []) if _f(o.get("strike")) is not None}
    puts  = {_f(o.get("strike")): o for o in chain.get("puts",  []) if _f(o.get("strike")) is not None}

    def _quote(opt):
        if not opt:
            return None
        bid = _f(opt.get("bid"))
        ask = _f(opt.get("ask"))
        mid = _f(opt.get("mid"))
        if bid is not None and ask is not None and bid >= 0 and ask >= 0:
            mid = round((bid + ask) / 2.0, 2)
        if mid is None and bid is not None and ask is not None:
            mid = round((bid + ask) / 2.0, 2)
        if mid is None:
            return None
        return {"bid": bid, "ask": ask, "mid": mid}

    mid_signed = 0.0
    natural_signed = 0.0
    found = 0

    for leg_key, strike in legs.items():
        st = _f(strike)
        if st is None:
            continue
        opt = calls.get(st) if "call" in leg_key else puts.get(st)
        q = _quote(opt)
        if q is None:
            print(f"[spread_calc_rc15c] missing quote for {leg_key}={strike}")
            return None

        # Mid value: theoretical fair close value.
        # Natural value: long legs sell at bid; short legs buy at ask.
        if leg_key.startswith("long"):
            mid_signed += q["mid"]
            natural_signed += (q["bid"] if q["bid"] is not None else q["mid"])
        elif leg_key.startswith("short"):
            mid_signed -= q["mid"]
            natural_signed -= (q["ask"] if q["ask"] is not None else q["mid"])
        found += 1

    if found == 0:
        return None

    mid_value = abs(mid_signed)
    natural_value = abs(natural_signed)

    # Intrinsic sanity floor for vertical debit spreads at/near 0DTE.
    # This prevents a stale/incorrect option chain from valuing an ITM debit spread
    # below its obvious intrinsic spread value. It is only a floor, not a new entry rule.
    intrinsic_floor = 0.0
    spot = _f(chain.get("underlying_price") or chain.get("spot") or chain.get("current_price"))
    try:
        lp = _f(legs.get("long_put")); sp = _f(legs.get("short_put"))
        lc = _f(legs.get("long_call")); sc = _f(legs.get("short_call"))
        if spot is not None and lp is not None and sp is not None:
            intrinsic_floor = max(0.0, lp - spot) - max(0.0, sp - spot)
        elif spot is not None and lc is not None and sc is not None:
            intrinsic_floor = max(0.0, spot - lc) - max(0.0, spot - sc)
        intrinsic_floor = max(0.0, intrinsic_floor)
    except Exception:
        intrinsic_floor = 0.0

    value = max(mid_value, intrinsic_floor)

    # If natural close is materially above mid due to bad mid field, keep the safer higher value.
    # Normally natural <= mid for debit-close value, but malformed feeds sometimes invert fields.
    value = max(value, natural_value if natural_value > value + 0.05 else value)

    value = round(value, 2)
    if intrinsic_floor and value + 0.01 < intrinsic_floor:
        print(f"[spread_calc_rc15c] WARN value<{intrinsic_floor:.2f} intrinsic floor")
    return value


def refresh_open_trade_prices(tok: Optional[str] = None) -> List[Dict]:
    """
    يجلب أسعار الأرجل الحالية لكل الصفقات المفتوحة.
    يحدث P&L ويغلق عند الهدف.
    يُستدعى كل 10 ثوانٍ.
    RC15i.5: تُوقَف مؤقتاً أثناء Full Analysis لتجنب ضغط API المتزامن.

    مهم: Swing trades تستخدم Swing chain من المسار الموحد analyze_swing، وليس chain اليوم.
    """
    # RC15i.5: لا تتنافس مع Full Analysis على API calls
    # — لكن بعد 14:55 ET تعمل دائماً حتى لا تُفوَّت exit rules (15:00/15:20/15:30)
    if _analysis_running and not _et_at_or_after(14, 55):
        return []

    open_trades = get_open_trades()
    if not open_trades:
        return []

    # نجمع chains منفصلة: 0DTE وSwing لكل رمز
    chains_0dte:  Dict[str, Any] = {}  # symbol → 0DTE chain
    chains_swing: Dict[str, Any] = {}  # "symbol|expiry" → Swing chain

    from core.analyzer import (get_options_chain, get_swing_options_chain,
                                _get_access_token, enrich_exposures_with_spot,
                                _fetch_price_for_symbol)
    try:
        _tok = tok or _get_access_token()
    except Exception as _te:
        print(f"[RC15i] refresh_open_trade_prices skipped — token error: {_te}")
        return []

    symbols_needed = {t["symbol"] for t in open_trades}
    swing_expiries = {
        (t["symbol"], t.get("expiry_date", ""))
        for t in open_trades
        if (t.get("selected_mode") or t.get("trade_mode")) == "Swing" and t.get("expiry_date")
    }

    for sym in symbols_needed:
        try:
            chain = get_options_chain(_tok, sym)
            if chain:
                price = _fetch_price_for_symbol(_tok, sym)
                if price:
                    enrich_exposures_with_spot(chain, price)
                    _record_intraday_price(sym, price)
                chains_0dte[sym] = chain
        except Exception as exc:
            print(f"[refresh_prices] 0DTE {sym}: {exc}")

    for sym, expiry in swing_expiries:
        key = f"{sym}|{expiry}"
        try:
            swing_c = get_swing_options_chain(_tok, sym, target_expiry_date=expiry)
            if swing_c and swing_c.get("expiry") == expiry:
                chains_swing[key] = swing_c
                print(f"[refresh_prices] Swing chain {sym} expiry={expiry} ✅")
            else:
                got = swing_c.get("expiry") if swing_c else "None"
                print(f"[refresh_prices] Swing chain {sym}: expiry mismatch "
                      f"(want={expiry}, got={got}) → skip")
        except Exception as exc:
            print(f"[refresh_prices] Swing chain {sym}: {exc}")

    near_close = _is_near_close()
    closed_now: List[Dict] = []

    for t in open_trades:
        sym         = t["symbol"]
        is_credit   = bool(t["is_credit"])
        entry_val   = t.get("credit_debit") or 0
        target      = t.get("target") or 0
        legs        = json.loads(t.get("legs_json") or "{}")
        trade_mode  = resolve_trade_mode(t)
        expiry_date = t.get("expiry_date", "")

        # اختر الـ chain الصحيح حسب نوع الصفقة
        if trade_mode == "Swing":
            swing_key = f"{sym}|{expiry_date}"
            chain = chains_swing.get(swing_key)
            if not chain:
                print(f"[refresh_prices] skip Swing #{t['id']} {sym}: "
                      f"no matching Swing chain (expiry={expiry_date})")
                continue  # لا تُغلق ولا تحسب P&L بسعر خاطئ
        else:
            chain = chains_0dte.get(sym)

        current_val = _get_spread_from_chain(legs, chain) if chain else None

        # 0DTE EOD: أغلق بـ 0 إذا لا chain متاح
        if current_val is None:
            if near_close and trade_mode == "0DTE":
                current_val = 0.0
                print(f"[refresh_prices] #{t['id']} {sym}: EOD close at 0 (no chain)")
            else:
                continue

        pnl_pct    = _calc_profit(is_credit, entry_val, current_val)
        pnl_dollar = round(
            (entry_val - current_val if is_credit else current_val - entry_val) * 100, 2)
        update_open_trade_live(t["id"], current_val, pnl_dollar, pnl_pct)

        if trade_mode == "Swing":
            # قواعد خروج Swing
            dte_left = _dte_remaining(expiry_date) if expiry_date else 99

            # RC13 — Swing يُغلق فقط عند DTE<=2. لا TP/SL percentage.
            # المنطق: Swing مدته 12-17 يوم، والتذبذب اليومي طبيعي.
            # الخروج المبكر بـ 60% يُضيع الفرصة إذا عاد السعر لصالح الصفقة.
            if dte_left <= 2:
                result = _classify_result(pnl_pct)
                _close_trade(t, current_val, result, pnl_pct,
                             f"Swing إغلاق إجباري DTE≤2 (DTE={dte_left})")
                _log_mode_perf(t, current_val, result, pnl_pct, pnl_dollar)
                closed_now.append({**t, "current": current_val, "pnl_pct": pnl_pct})
        else:
            # قواعد 0DTE الأصلية
            if near_close:
                result = _classify_result(pnl_pct)
                _close_trade(t, current_val, result, pnl_pct, "إغلاق السوق EOD")
                _log_mode_perf(t, current_val, result, pnl_pct, pnl_dollar)
                closed_now.append({**t, "current": current_val, "pnl_pct": pnl_pct})
            elif _hit_target(is_credit, current_val, target):
                result = _classify_result(pnl_pct)
                _close_trade(t, current_val, result, pnl_pct, "50% profit target")
                _log_mode_perf(t, current_val, result, pnl_pct, pnl_dollar)
                closed_now.append({**t, "current": current_val, "pnl_pct": pnl_pct})

    return closed_now


# ── v3.33.6a: Unified 0DTE Exit Evaluator ────────────────────────────────────
# مصدر وحيد لقواعد خروج 0DTE Debit — تستخدمه refresh_paper_trade_prices
# و monitor_paper_trades كلتاهما، لضمان سلوك موحد دائماً.
#
# القواعد (بالترتيب — أعلى أولوية أولاً):
#   1. 50% Profit Target
#   2. -50% Stop Loss  (0DTE Debit فقط)
#   3. SPX Profit Lock (آخر 3 ساعات)
#   4. EOD 15:30 ET
#   5. Force Exit 15:20 ET  (0DTE Debit — SPX/SPY/QQQ/IWM)
#   6. Soft Exit 15:00 ET إذا P&L <= 0  (0DTE Debit)
#
# إذا current_val = None و _et_at_or_after(15,20) و is_debit_0dte:
#   → يُعاد EXIT_PENDING_NO_CHAIN بدلاً من الإغلاق على 0.0 لأن السعر غير متوفر.
#   → الدالة المُستدعِية تُسجّل pending وتُحاول عند التحديث التالي أو عند near_close.

EXIT_PENDING_NO_CHAIN = "__pending_exit_no_chain__"


def evaluate_paper_trade_exit(
    trade_id: int,
    symbol: str,
    strategy: str,
    is_credit: bool,
    entry_val: float,
    target: float,
    current_val,           # float أو None
    near_close: bool,
    caller: str = "?",     # للـ logging: "refresh" أو "monitor"
) -> dict:
    """
    v3.33.6a — يُقيّم قواعد خروج 0DTE ويُعيد:
      {
        "close_reason":  str | None,          # سبب الإغلاق أو None (لا إغلاق)
        "current_val":   float | None,        # قيمة السبريد الحالية
        "pnl_pct":       float | None,
        "pending_chain": bool,                # True = 15:20 pending لأن Chain غير متوفر
      }

    لا يُغلق الصفقة بنفسه — القرار للدالة المُستدعِية.
    """
    is_debit_0dte    = (not is_credit) and _is_0dte_debit_strategy(strategy)
    minutes_to_close = _minutes_to_market_close()
    close_reason     = None
    pending_chain    = False

    # ── معالجة current_val = None ──────────────────────────────────────────────
    if current_val is None:
        if near_close:
            # 15:30+ EOD — أغلق على 0.0 (انتهاء صلاحية بلا قيمة)
            current_val  = 0.0
            close_reason = "إغلاق السوق EOD"
            print(f"[exit_eval] #{trade_id} {symbol} {strategy}: "
                  f"EOD close at 0.0 — chain unavailable (caller={caller})")
        elif is_debit_0dte and _et_at_or_after(*EOD_FORCE_DEBIT_EXIT_ET):
            # 15:20-15:29 — نريد الخروج لكن لا سعر متوفر → pending
            pending_chain = True
            print(f"[exit_eval] #{trade_id} {symbol} {strategy}: "
                  f"exit_pending_no_chain_after_15:20 — will retry (caller={caller})")
            # v3.33.6a RC2: حدّث exit_trigger في DB ليظهر الوضع في الواجهة
            try:
                from core.database import update_paper_trade_exit_trigger_only
                update_paper_trade_exit_trigger_only(
                    trade_id, "exit_pending_due_to_missing_chain_after_1520"
                )
            except Exception as _e:
                print(f"[exit_eval] #{trade_id} warning: could not update exit_trigger: {_e}")
            return {"close_reason": None, "current_val": None,
                    "pnl_pct": None, "pending_chain": True}
        else:
            # لا سعر ولا وقت خروج محدد — انتظر
            return {"close_reason": None, "current_val": None,
                    "pnl_pct": None, "pending_chain": False}

    detected_val = current_val
    pnl_now = _calc_profit(is_credit, entry_val, current_val)
    threshold_close_val = None

    # ── ترتيب الأولويات ────────────────────────────────────────────────────────
    # RC15i.10: Paper exit accuracy. 0DTE can move from +45% to +200% between
    # polling cycles. When the stated exit reason is a rule threshold, record the
    # rule price itself (target/stop), not the late detected price, so paper stats
    # reflect the configured TP/SL rather than refresh delay overshoot.
    if close_reason is None and target and _hit_target(is_credit, current_val, target):
        close_reason = "50% profit target"
        threshold_close_val = round(float(target), 2)

    elif close_reason is None and is_debit_0dte and pnl_now <= DEBIT_STOP_PCT * 100:
        close_reason = f"0DTE Debit Stop Loss 50% ({pnl_now:.1f}%)"
        threshold_close_val = round(float(entry_val) * (1 + DEBIT_STOP_PCT), 2)

    elif (close_reason is None
          and symbol == "SPX"
          and minutes_to_close is not None
          and minutes_to_close <= SPX_0DTE_NO_NEW_ENTRY_MINUTES_TO_CLOSE
          and pnl_now > 0):
        close_reason = f"SPX last-3h profit lock ({pnl_now:.1f}%)"

    elif close_reason is None and near_close:
        close_reason = "إغلاق السوق EOD"

    elif close_reason is None and is_debit_0dte and _et_at_or_after(*EOD_FORCE_DEBIT_EXIT_ET):
        close_reason = "0DTE Debit Time Exit 15:20 ET (SPX/SPY/QQQ/IWM)"

    elif (close_reason is None
          and is_debit_0dte
          and _et_at_or_after(*EOD_SOFT_EXIT_ET)
          and pnl_now <= 0):
        close_reason = f"0DTE Debit Soft Time Exit 15:00 ET ({pnl_now:.1f}%)"

    if close_reason and threshold_close_val is not None:
        # Cap/normalize paper fill to the rule threshold. Keep detected_val in log
        # for diagnostics so we can still see refresh overshoot.
        current_val = threshold_close_val
        pnl_now = _calc_profit(is_credit, entry_val, current_val)

    # ── تفصيل Exit في الـ log دائماً عند الإغلاق (التعديل 5) ─────────────────
    if close_reason:
        pnl_dollar = round(
            (entry_val - current_val if is_credit else current_val - entry_val) * 100, 2)
        result_tag = _classify_result(pnl_now)
        _overshoot_txt = ""
        try:
            if detected_val is not None and threshold_close_val is not None:
                _overshoot_txt = f" | detected={float(detected_val):.2f} threshold_fill={float(current_val):.2f}"
        except Exception:
            pass
        print(
            f"[exit_eval] #{trade_id} | {symbol} | {strategy} | "
            f"entry={entry_val:.2f} current={current_val:.2f} | "
            f"pnl={pnl_now:+.1f}% (${pnl_dollar:+.0f}) | "
            f"result={result_tag} | reason={close_reason}{_overshoot_txt} | "
            f"exit_rule_source=evaluate_paper_trade_exit | caller={caller}"
        )

    return {
        "close_reason": close_reason,
        "current_val":  current_val,
        "detected_val": detected_val,
        "threshold_close_val": threshold_close_val,
        "pnl_pct":      pnl_now,
        "pending_chain": pending_chain,
    }


# ── Paper Trade Price Refresh (independent 10-second loop) ───────────────────
# Lock يمنع تداخل دورتين من refresh_paper_trade_prices في نفس الوقت
_refresh_paper_lock = threading.Lock()
# cache للـ Swing chains داخل الدورة: symbol → chain (أو False عند فشل سابق)
# يُعاد تعبئته في كل دورة — يمنع fetch متعدد لنفس الرمز خلال 60 ثانية
_swing_chain_cache: Dict[str, Any] = {}
_swing_chain_cache_ts: float = 0.0
_SWING_CACHE_TTL: float = 60.0   # ثانية

def refresh_paper_trade_prices(tok: Optional[str] = None, only_0dte: bool = False) -> List[Dict]:
    """
    يجلب أسعار paper trades المفتوحة مباشرة من Tastytrade ويغلقها عند TP/SL/EOD.
    يُستدعى كل 10 ثوانٍ مستقلاً عن دورات التحليل — بنفس منطق refresh_open_trade_prices.
    منطق الخروج موحَّد عبر evaluate_paper_trade_exit() — لا تكرار مع monitor_paper_trades.
    RC15i.5: تُوقَف مؤقتاً أثناء Full Analysis لتجنب ضغط API المتزامن.

    القواعد:
      - إذا لم تتوفر chain لرمز معين → skip بدون إغلاق عشوائي.
      0DTE:
        - TP  +50%  profit target
        - SL  -50%  stop loss للـ Debit Spreads  (v3.33.4 — DEBIT_STOP_PCT)
        - SPX profit lock: أغلق أي ربح إيجابي عند دخول آخر 3 ساعات
        - Soft exit 15:00 ET: أغلق إذا P&L <= 0
        - Force exit 15:20 ET: أغلق أي 0DTE Debit لم يصل للهدف (SPX/SPY/QQQ/IWM)
        - EOD 15:30 ET: إغلاق إجباري نهاية اليوم
      Swing:
        - لا TP/SL percentage
        - إغلاق إجباري عند DTE <= 2 فقط
    """
    global _swing_chain_cache, _swing_chain_cache_ts

    # RC15i.5: لا تتنافس مع Full Analysis على API calls
    # — لكن بعد 14:55 ET تعمل دائماً حتى لا تُفوَّت exit rules (15:00/15:20/15:30)
    if _analysis_running and not _et_at_or_after(14, 55):
        return []

    # منع تداخل دورتين — إذا دورة سابقة لم تنته بعد نتجاهل هذه الدورة
    if not _refresh_paper_lock.acquire(blocking=False):
        return []
    try:
        return _refresh_paper_trade_prices_inner(tok, only_0dte=only_0dte)
    finally:
        _refresh_paper_lock.release()


def _refresh_paper_trade_prices_inner(tok: Optional[str] = None, only_0dte: bool = False) -> List[Dict]:
    global _swing_chain_cache, _swing_chain_cache_ts

    from core.database import get_paper_trades, close_paper_trade, update_paper_trade_live

    open_trades = get_paper_trades(status="open")
    if only_0dte:
        # RC15i.10: high-priority fast path. When 0DTE positions are open, poll
        # only those trades every ~1s and leave Swing/legacy refresh to the slower
        # full cycle. This prevents Swing chains from delaying TP/SL checks.
        open_trades = [t for t in open_trades if resolve_trade_mode(t) == "0DTE"]
    if not open_trades:
        return []

    # تجديد Swing chain cache إذا انتهت مدته (60 ثانية)
    _now_ts = time.time()
    if _now_ts - _swing_chain_cache_ts > _SWING_CACHE_TTL:
        _swing_chain_cache = {}
        _swing_chain_cache_ts = _now_ts

    # جلب chains لكل رمز مطلوب
    chains: Dict[str, Any] = {}
    symbols_needed = {t.get("symbol", "SPX").upper() for t in open_trades}

    from core.analyzer import (get_options_chain, _get_access_token,
                                enrich_exposures_with_spot,
                                _fetch_price_for_symbol)
    try:
        _tok = tok or _get_access_token()
    except Exception as _te:
        print(f"[RC15i] refresh_paper_trade_prices skipped — token error: {_te}")
        return []

    for sym in symbols_needed:
        try:
            chain = get_options_chain(_tok, sym)
            if chain:
                p = _fetch_price_for_symbol(_tok, sym)
                if p:
                    # RC15c: keep the underlying price attached to the chain so
                    # open-trade exit checks can sanity-check debit spread value
                    # against intrinsic value when option quotes lag/stale.
                    try:
                        chain["underlying_price"] = float(p)
                    except Exception:
                        pass
                    enrich_exposures_with_spot(chain, p)
                    _record_intraday_price(sym, p)
                chains[sym] = chain
            else:
                print(f"[refresh_paper_prices] {sym}: 0DTE chain = None — skip")
        except Exception as exc:
            print(f"[refresh_paper_prices] {sym}: {exc}")

    near_close = _is_near_close()
    closed_now: List[Dict] = []

    for t in open_trades:
        try:
            entry_val = float(t.get("credit_debit") or 0)
            if entry_val <= 0:
                continue

            is_credit   = t.get("strategy", "") in (
                "Iron Condor", "Bull Put Spread", "Bear Call Spread")
            trade_mode  = resolve_trade_mode(t)
            expiry_date = t.get("expiry_date", "")
            symbol      = t.get("symbol", "SPX").upper()

            # target: محسوب من credit_debit (لا يُحفظ في paper_trades)
            # RC13: Swing لا يستخدم percentage target؛ الخروج فقط DTE<=2.
            if trade_mode == "Swing":
                target = 0.0
            else:
                profit_pct_target = PROFIT_TARGET_PCT
                target = round(
                    entry_val * (1 - profit_pct_target) if is_credit
                    else entry_val * (1 + profit_pct_target), 2
                )

            legs = {k: t.get(k) for k in
                    ("short_put", "long_put", "short_call", "long_call") if t.get(k)}
            if not legs:
                continue

            chain = chains.get(symbol)

            def _norm_exp(d):
                s = str(d or "").replace("-", "")
                return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 else s

            # Swing: يجب جلب chain خاص بـ expiry الصفقة — chain اليوم يعطي أسعار خاطئة
            # الـ cache يمنع fetch متعدد لنفس الرمز خلال 60 ثانية
            if trade_mode == "Swing" and expiry_date:
                _swing_key = f"{symbol}|{_norm_exp(expiry_date)}"
                _cached = _swing_chain_cache.get(_swing_key)
                if _cached is False:
                    # فشل سابق — لا نعيد المحاولة حتى تجديد الـ cache
                    continue
                elif _cached is not None:
                    chain = _cached
                else:
                    # لم يُجلب بعد في هذه الدورة
                    try:
                        from core.analyzer import get_swing_options_chain
                        swing_c = get_swing_options_chain(_tok, symbol, target_expiry_date=expiry_date)
                        if swing_c:
                            chain_exp = _norm_exp(swing_c.get("expiry"))
                            trade_exp = _norm_exp(expiry_date)
                            if chain_exp == trade_exp:
                                _swing_chain_cache[_swing_key] = swing_c
                                chain = swing_c
                            else:
                                print(f"[refresh_paper_prices] Swing #{t.get('id','?')} {symbol}: "
                                      f"expiry mismatch chain={chain_exp} trade={trade_exp} — skip")
                                _swing_chain_cache[_swing_key] = False
                                continue
                        else:
                            print(f"[refresh_paper_prices] Swing #{t.get('id','?')} {symbol}: "
                                  f"chain=None (expiry={expiry_date}) — skip (will retry in {int(_SWING_CACHE_TTL)}s)")
                            _swing_chain_cache[_swing_key] = False
                            continue
                    except Exception as _sw:
                        print(f"[refresh_paper_prices] Swing chain error {symbol}: {_sw}")
                        _swing_chain_cache[_swing_key] = False
                        continue

            current_val = _get_spread_from_chain(legs, chain) if chain else None

            # RC14 — P&L Diagnostic Logging for Swing trades
            if trade_mode == "Swing":
                _tid = t.get("id", "?")
                _stored_exp = expiry_date
                _norm_stored = _norm_exp(expiry_date) if expiry_date else "None"
                _matched_exp = (chain.get("expiry") if chain else "no-chain")
                # استخراج bid/ask لكل رجل من chain
                _calls_map = {o["strike"]: o for o in (chain.get("calls", []) if chain else [])}
                _puts_map  = {o["strike"]: o for o in (chain.get("puts",  []) if chain else [])}
                _leg_details = []
                for _lk, _strike in legs.items():
                    if _strike is None:
                        _leg_details.append(f"{_lk}=None")
                        continue
                    _opt = _calls_map.get(_strike) if "call" in _lk else _puts_map.get(_strike)
                    if _opt:
                        _leg_details.append(
                            f"{_lk}={_strike} bid={_opt.get('bid','?')} ask={_opt.get('ask','?')}"
                        )
                    else:
                        _leg_details.append(f"{_lk}={_strike} NOT_FOUND_IN_CHAIN")
                print(
                    f"[PnL-Diag] Swing #{_tid} {symbol} | "
                    f"stored_expiry={_stored_exp} normalized={_norm_stored} "
                    f"matched_contract={_matched_exp} | "
                    f"legs=[{', '.join(_leg_details)}] | "
                    f"calculated_value={current_val}"
                )

            close_reason = None

            if trade_mode == "Swing":
                if current_val is None:
                    print(f"[refresh_paper_prices] skip #{t.get('id','?')} {symbol}: no chain for Swing")
                    continue
                dte_left = _dte_remaining(expiry_date) if expiry_date else 99
                pnl_now = _calc_profit(is_credit, entry_val, current_val)
                # RC13 — Swing: إغلاق عند DTE<=2 فقط. لا TP/SL percentage.
                if dte_left <= 2:
                    close_reason = "Swing إغلاق إجباري DTE≤2"
            else:
                # 0DTE — v3.33.6a: استخدام evaluate_paper_trade_exit الموحدة
                ev = evaluate_paper_trade_exit(
                    trade_id    = t.get("id", 0),
                    symbol      = symbol,
                    strategy    = t.get("strategy", ""),
                    is_credit   = is_credit,
                    entry_val   = entry_val,
                    target      = target,
                    current_val = current_val,
                    near_close  = near_close,
                    caller      = "refresh",
                )
                if ev["pending_chain"]:
                    # 15:20 pending — لا سعر متوفر، سيُحاول في الدورة القادمة
                    continue
                if ev["current_val"] is None:
                    # لا سعر ولا وقت خروج — تخطَّ
                    print(f"[refresh_paper_prices] skip #{t.get('id','?')} {symbol}: no chain, not EOD")
                    continue
                current_val  = ev["current_val"]
                close_reason = ev["close_reason"]

            pnl_pct    = _calc_profit(is_credit, entry_val, current_val)
            pnl_dollar = round(
                (entry_val - current_val if is_credit else current_val - entry_val) * 100, 2)
            exit_trigger = close_reason or (
                "Swing waiting — DTE<=2 only" if trade_mode == "Swing" else
                (f"TP @ {target:.2f}" if target else "waiting")
            )
            try:
                update_paper_trade_live(t["id"], current_val, pnl_dollar, pnl_pct, exit_trigger)
            except Exception as _u:
                print(f"[paper_live_update] #{t.get('id','?')} {_u}")

            if close_reason:
                result = _classify_result(pnl_pct)
                close_paper_trade(
                    trade_id    = t["id"],
                    exit_price  = current_val,
                    result      = result,
                    profit_pct  = pnl_pct,
                    pnl_dollar  = pnl_dollar,
                    close_reason= close_reason,
                )
                closed_now.append({
                    "id": t["id"], "symbol": symbol,
                    "strategy": t.get("strategy", ""),
                    "result": result, "pnl_pct": pnl_pct,
                    "pnl_dollar": pnl_dollar, "close_reason": close_reason,
                })
                print(f"[refresh_paper_prices] #{t['id']} {symbol} {t.get('strategy','')} "
                      f"→ {result} {pnl_pct:+.1f}% | {close_reason}")
        except Exception as _e:
            print(f"[refresh_paper_prices] #{t.get('id','?')} error: {_e}")

    return closed_now


# ── Paper Trade Monitor ───────────────────────────────────────────────────────

def monitor_paper_trades(analysis: Dict) -> List[Dict]:
    """
    يراقب توصيات Paper Trading المفتوحة ويُغلقها تلقائياً عند:
      0DTE:
        - TP  +50%  profit target
        - SL  -50%  stop loss   (v3.33.4 — DEBIT_STOP_PCT)
        - SPX profit lock: أغلق أي ربح إيجابي عند دخول آخر 3 ساعات
        - Soft exit 15:00 ET: أغلق إذا P&L <= 0
        - Force exit 15:20 ET: أغلق أي 0DTE Debit Spread لم يصل للهدف (SPX/SPY/QQQ/IWM)
        - EOD 15:30 ET: إغلاق إجباري نهاية اليوم
      Swing:
        - لا TP/SL percentage
        - إغلاق إجباري عند DTE <= 2 فقط
    يُستدعى بعد كل تحليل من _on_analysis_done.
    منطق الخروج موحَّد عبر evaluate_paper_trade_exit() — لا تكرار مع refresh_paper_trade_prices.
    """
    from core.database import get_paper_trades, close_paper_trade, update_paper_trade_live

    open_trades = get_paper_trades(status="open")
    if not open_trades:
        return []

    near_close = _is_near_close()
    closed_now: List[Dict] = []

    # نبني chain lookup من التحليل
    chains: Dict[str, Any] = {}
    for sym in ("SPX", "SPY", "QQQ", "IWM"):
        src = analysis if sym == "SPX" else (analysis.get(sym.lower()) or {})
        c = src.get("_chain")
        if c:
            chains[sym] = c

    for t in open_trades:
        try:
            entry_val   = float(t.get("credit_debit") or 0)
            if entry_val <= 0:
                continue

            is_credit   = t.get("strategy", "") in (
                "Iron Condor", "Bull Put Spread", "Bear Call Spread")
            trade_mode  = resolve_trade_mode(t)
            expiry_date = t.get("expiry_date", "")
            symbol      = t.get("symbol", "SPX").upper()

            # target: احسبه من credit_debit مباشرة (لا يُحفظ في paper_trades)
            # RC13: Swing لا يستخدم TP/SL percentage.
            if trade_mode == "Swing":
                target = 0.0
            else:
                profit_pct_target = PROFIT_TARGET_PCT
                if is_credit:
                    target = round(entry_val * (1 - profit_pct_target), 2)
                else:
                    target = round(entry_val * (1 + profit_pct_target), 2)

            # بناء legs من الأعمدة المباشرة (paper_trades لها short_put/long_put/...)
            legs = {k: t.get(k) for k in
                    ("short_put","long_put","short_call","long_call") if t.get(k)}
            if not legs:
                continue

            # ── RC15i Swing DTE fallback: لا TP/SL للـ Swing هنا.
            # إذا وصل Swing إلى DTE<=2 نحاول إغلاقه بسعر Swing chain الصحيح فقط.
            # إذا لم يتوفر chain/quote صحيح، لا نغلق على 0 ولا نستخدم 0DTE chain.
            if trade_mode == "Swing":
                dte_left = _dte_remaining(expiry_date) if expiry_date else 99
                if dte_left > 2:
                    continue
                try:
                    from core.analyzer import _get_access_token, get_swing_options_chain
                    _tok = _get_access_token()
                    swing_chain = get_swing_options_chain(_tok, symbol, target_expiry_date=expiry_date)
                except Exception as _se:
                    print(f"[paper_monitor] Swing #{t.get('id','?')} {symbol}: DTE<=2 close pending — token/chain error: {_se}")
                    continue
                if not swing_chain:
                    print(f"[paper_monitor] Swing #{t.get('id','?')} {symbol}: DTE<=2 close pending — no Swing chain expiry={expiry_date}")
                    continue
                def _norm_exp(_d):
                    _s = str(_d or "").replace("-", "")
                    return f"{_s[:4]}-{_s[4:6]}-{_s[6:8]}" if len(_s) == 8 else str(_d or "")
                if _norm_exp(swing_chain.get("expiry")) != _norm_exp(expiry_date):
                    print(f"[paper_monitor] Swing #{t.get('id','?')} {symbol}: DTE<=2 close pending — expiry mismatch chain={swing_chain.get('expiry')} trade={expiry_date}")
                    continue
                current_val = _get_spread_from_chain(legs, swing_chain)
                if current_val is None:
                    print(f"[paper_monitor] Swing #{t.get('id','?')} {symbol}: DTE<=2 close pending — no valid spread quote")
                    continue
                pnl_pct = _calc_profit(is_credit, entry_val, current_val)
                pnl_dollar = round((entry_val - current_val if is_credit else current_val - entry_val) * 100, 2)
                close_reason = f"Swing إغلاق إجباري DTE≤2 (DTE={dte_left})"
                result = _classify_result(pnl_pct)
                try:
                    update_paper_trade_live(t["id"], current_val, pnl_dollar, pnl_pct, close_reason)
                except Exception as _u:
                    print(f"[paper_live_update] Swing #{t.get('id','?')} {_u}")
                close_paper_trade(
                    trade_id=t["id"], exit_price=current_val, result=result,
                    profit_pct=pnl_pct, pnl_dollar=pnl_dollar, close_reason=close_reason,
                )
                closed_now.append({
                    "id": t["id"], "symbol": symbol, "strategy": t.get("strategy", ""),
                    "result": result, "pnl_pct": pnl_pct, "pnl_dollar": pnl_dollar,
                    "close_reason": close_reason,
                })
                print(f"[paper_monitor] Swing #{t['id']} {symbol} {t.get('strategy','')} "
                      f"→ {result} {pnl_pct:+.1f}% | {close_reason}")
                continue

            chain = chains.get(symbol)

            current_val = _get_spread_from_chain(legs, chain) if chain else None

            # ── 0DTE فقط — v3.33.6a: evaluate_paper_trade_exit الموحدة
            ev = evaluate_paper_trade_exit(
                trade_id    = t.get("id", 0),
                symbol      = symbol,
                strategy    = t.get("strategy", ""),
                is_credit   = is_credit,
                entry_val   = entry_val,
                target      = target,
                current_val = current_val,
                near_close  = near_close,
                caller      = "monitor",
            )
            if ev["pending_chain"]:
                # 15:20 pending — لا سعر متوفر، سيُحاول refresh_paper_trade_prices
                continue
            if ev["current_val"] is None:
                continue
            current_val  = ev["current_val"]
            close_reason = ev["close_reason"]
            pnl_now      = ev["pnl_pct"]

            pnl_pct    = pnl_now if pnl_now is not None else _calc_profit(is_credit, entry_val, current_val)
            pnl_dollar = round(
                (entry_val - current_val if is_credit else current_val - entry_val) * 100, 2)
            exit_trigger = close_reason or (
                "Swing waiting — DTE<=2 only" if trade_mode == "Swing" else
                (f"TP @ {target:.2f}" if target else "waiting")
            )
            try:
                update_paper_trade_live(t["id"], current_val, pnl_dollar, pnl_pct, exit_trigger)
            except Exception as _u:
                print(f"[paper_live_update] #{t.get('id','?')} {_u}")

            if close_reason:
                result = _classify_result(pnl_pct)
                close_paper_trade(
                    trade_id    = t["id"],
                    exit_price  = current_val,
                    result      = result,
                    profit_pct  = pnl_pct,
                    pnl_dollar  = pnl_dollar,
                    close_reason= close_reason,
                )
                closed_now.append({
                    "id": t["id"], "symbol": symbol,
                    "strategy": t.get("strategy",""),
                    "result": result, "pnl_pct": pnl_pct,
                    "pnl_dollar": pnl_dollar, "close_reason": close_reason,
                })
                print(f"[paper_monitor] #{t['id']} {symbol} {t.get('strategy','')} "
                      f"→ {result} {pnl_pct:+.1f}% | {close_reason}")
        except Exception as _e:
            print(f"[paper_monitor] #{t.get('id','?')} error: {_e}")

    return closed_now
