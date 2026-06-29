"""
Risk Manager — حدود المخاطرة والحجم لكل صفقة
يُستدعى قبل تسجيل أي صفقة تلقائية.
"""
from __future__ import annotations
import math
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from core.database import get_setting, get_connection

# Multiplier ثابت لجميع الرموز
CONTRACT_MULTIPLIER = 100


# ── إعدادات المخاطرة ──────────────────────────────────────────────────────────

def get_risk_settings() -> Dict[str, float]:
    return {
        "account_size":          float(get_setting("account_size",          "1500")),
        "max_risk_per_trade_pct": float(get_setting("max_risk_per_trade_pct", "2.0")),
        # 0 = unlimited; useful for paper-study mode
        "max_trades_per_day":    int(get_setting("max_trades_per_day",     "0") or 0),
        "max_daily_loss_pct":    float(get_setting("max_daily_loss_pct",    "4.0")),
    }


def max_risk_dollar(settings: Dict) -> float:
    """الحد الأقصى للمخاطرة بالدولار في صفقة واحدة."""
    return round(settings["account_size"] * settings["max_risk_per_trade_pct"] / 100, 2)


def max_daily_loss_dollar(settings: Dict) -> float:
    """الحد الأقصى للخسارة اليومية بالدولار."""
    return round(settings["account_size"] * settings["max_daily_loss_pct"] / 100, 2)


# ── حساب Max Loss لكل استراتيجية ─────────────────────────────────────────────

def calculate_max_loss_per_contract(strat: Dict, symbol: str) -> Optional[float]:
    """
    Max Loss بالدولار لعقد واحد.

    Credit spreads (Bull Put, Bear Call, Iron Condor):
        Max Loss = (Wing Width - Credit Received) × 100

    Debit spreads (Call/Put Debit):
        Max Loss = Debit Paid × 100  (لا يمكن خسارة أكثر من ما دفعناه)
    """
    name = strat.get("strategy", "")

    if name in ("Bull Put Spread", "Bear Call Spread"):
        credit = strat.get("credit") or 0
        if name == "Bull Put Spread":
            sp = strat.get("short_put") or 0
            lp = strat.get("long_put")  or 0
            wing = round(abs(sp - lp), 2) if (sp and lp and sp != lp) else 5
        else:
            sc = strat.get("short_call") or 0
            lc = strat.get("long_call")  or 0
            wing = round(abs(lc - sc), 2) if (sc and lc and sc != lc) else 5
        max_loss = (wing - max(credit, 0)) * CONTRACT_MULTIPLIER
        return round(max(max_loss, 0), 2)

    if name == "Iron Condor":
        credit = strat.get("credit") or 0
        sc = strat.get("short_call") or 0
        lc = strat.get("long_call")  or 0
        sp = strat.get("short_put")  or 0
        lp = strat.get("long_put")   or 0
        call_wing = round(abs(lc - sc), 2) if (sc and lc and sc != lc) else 5
        put_wing  = round(abs(sp - lp), 2) if (sp and lp and sp != lp) else 5
        wing = max(call_wing, put_wing)
        max_loss = (wing - max(credit, 0)) * CONTRACT_MULTIPLIER
        return round(max(max_loss, 0), 2)

    if name in ("Call Debit Spread", "Put Debit Spread"):
        debit = strat.get("debit") or 0
        return round(debit * CONTRACT_MULTIPLIER, 2)

    return None


def contracts_allowed(max_risk: float, max_loss_per_contract: float) -> int:
    """عدد العقود المسموحة = floor(Max Risk / Max Loss per Contract)"""
    if max_loss_per_contract <= 0:
        return 0
    return math.floor(max_risk / max_loss_per_contract)


# ── إحصاءات اليوم ────────────────────────────────────────────────────────────

def get_today_stats() -> Dict[str, Any]:
    """عدد الصفقات المفتوحة اليوم وإجمالي الخسائر المحققة."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        with get_connection() as conn:
            # صفقات فُتحت اليوم (بغض النظر عن حالتها)
            trades_today = conn.execute(
                "SELECT COUNT(*) as cnt FROM open_trades WHERE entry_date=?",
                (today,)
            ).fetchone()["cnt"]

            # خسائر اليوم من pnl_dollar الفعلي (سالب = خسارة)
            # credit_debit × 100 كان خاطئاً — credit_debit هو قيمة الكريدت المستلم لا الخسارة
            losses = conn.execute(
                """SELECT COALESCE(
                       SUM(CASE WHEN pnl_dollar < 0 THEN ABS(pnl_dollar) ELSE 0 END),
                       0) as total
                   FROM open_trades
                   WHERE entry_date=? AND status='closed'""",
                (today,)
            ).fetchone()["total"]

        return {"trades_today": trades_today, "daily_loss": float(losses), "stats_error": False}
    except Exception as e:
        # RC15i.3: risk checks must fail closed. Returning zero loss/trades on a
        # database error can silently bypass daily limits.
        return {
            "trades_today": 999999,
            "daily_loss": float("inf"),
            "stats_error": True,
            "stats_error_message": str(e),
        }


# ── الفحص الشامل ─────────────────────────────────────────────────────────────

def check_risk(strat: Dict, symbol: str) -> Dict[str, Any]:
    """
    يفحص كل شروط المخاطرة قبل تسجيل الصفقة.

    يعيد dict:
      allowed            : bool
      reason             : str
      max_loss_per_contract : float
      contracts_allowed  : int
      max_risk_allowed   : float
      daily_trades       : int
      daily_loss         : float
    """
    settings    = get_risk_settings()
    max_risk    = max_risk_dollar(settings)
    max_daily   = max_daily_loss_dollar(settings)
    today_stats = get_today_stats()

    result: Dict[str, Any] = {
        "allowed":                True,
        "reason":                 "OK",
        "max_loss_per_contract":  None,
        "contracts_allowed":      1,
        "max_risk_allowed":       max_risk,
        "account_size":           settings["account_size"],
        "daily_trades":           today_stats["trades_today"],
        "daily_loss":             today_stats["daily_loss"],
        "max_trades_per_day":     settings["max_trades_per_day"],
        "max_daily_loss":         max_daily,
    }

    def _reject(reason: str) -> Dict[str, Any]:
        result["allowed"] = False
        result["reason"] = reason
        result["contracts_allowed"] = 0
        return result

    # RC15i.3: إذا فشل جلب إحصاءات اليوم، ارفض الصفقة بدلاً من فتح المخاطرة.
    if today_stats.get("stats_error"):
        return _reject(f"تعذّر قراءة إحصاءات المخاطرة اليومية — fail closed: {today_stats.get('stats_error_message', '')}")

    # 1. حد الصفقات اليومي
    if settings["max_trades_per_day"] > 0 and today_stats["trades_today"] >= settings["max_trades_per_day"]:
        return _reject(f"تجاوز حد الصفقات اليومي ({today_stats['trades_today']}/{int(settings['max_trades_per_day'])})")

    # 2. حد الخسارة اليومية
    if today_stats["daily_loss"] >= max_daily:
        return _reject(f"تجاوز حد الخسارة اليومية (${today_stats['daily_loss']:.0f} / ${max_daily:.0f})")

    # 3. حساب Max Loss للاستراتيجية
    ml = calculate_max_loss_per_contract(strat, symbol)
    result["max_loss_per_contract"] = ml

    if ml is None:
        return _reject("تعذّر حساب Max Loss — بيانات غير كاملة")

    if ml <= 0:
        return _reject("Max Loss = 0 — بيانات غير صالحة")

    # 4. مقارنة Max Loss مع حد المخاطرة
    if ml > max_risk:
        return _reject(f"Max Loss ${ml:,.0f} > حد المخاطرة ${max_risk:.0f} "
                       f"({settings['max_risk_per_trade_pct']:.1f}% من ${settings['account_size']:,.0f})")

    # 5. حساب عدد العقود
    n = contracts_allowed(max_risk, ml)
    result["contracts_allowed"] = n
    if n < 1:
        result["allowed"] = False
        result["reason"]  = (f"Max Loss ${ml:,.0f} > حد المخاطرة ${max_risk:.0f} — "
                             f"لا يمكن تنفيذ عقد واحد")
        return result

    result["reason"] = (f"OK — Max Loss ${ml:,.0f} × {n} عقد | "
                        f"إجمالي خطر ${ml*n:,.0f} / ${max_risk:.0f}")
    return result


def format_risk_summary(risk: Dict) -> str:
    """نص ملخص للعرض في الـ UI والتيليغرام."""
    if risk["allowed"]:
        ml  = risk.get("max_loss_per_contract", 0) or 0
        n   = risk.get("contracts_allowed", 1)
        return (f"✅ Risk OK — MaxLoss ${ml:,.0f}/عقد × {n}  |  "
                f"صفقات اليوم: {risk['daily_trades']}/"
                f"{('∞' if int(risk['max_trades_per_day']) == 0 else int(risk['max_trades_per_day']))}")
    return f"❌ رُفض بالمخاطرة: {risk['reason']}"
