"""
paper_trading/paper_engine.py
==============================
محرك التداول الورقي
يحاكي الصفقات بدون تنفيذ حقيقي
يتتبع: P&L، نسبة الفوز، الحد الأقصى للتراجع
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from config import settings


class PaperEngine:
    """
    محرك التداول الورقي
    يحاكي تنفيذ الصفقات ويتتبع الأداء
    """

    def __init__(self, db=None, notifier=None):
        # التحقق من الأمان الحاسم
        assert not settings.LIVE_TRADING_ENABLED, (
            "LIVE_TRADING_ENABLED يجب أن يكون false! لا تنفيذ حقيقي أبداً!"
        )
        self.db = db
        self.notifier = notifier
        self._open_trades: Dict[str, Dict] = {}  # تخزين مؤقت
        self._daily_pnl: float = 0.0
        self._peak_value: float = 0.0
        self._max_drawdown: float = 0.0
        logger.info("تم تهيئة محرك التداول الورقي")

    # ──────────────────────────────────────────────────────
    # فتح صفقة جديدة
    # ──────────────────────────────────────────────────────

    def open_trade(
        self,
        symbol: str,
        strategy: str,
        direction: str,
        entry_price: float,
        premium: float,
        max_profit: float,
        max_loss: float,
        expiry_date: Optional[str] = None,
        legs: Optional[List[Dict]] = None,
        notes: str = "",
    ) -> Optional[Dict[str, Any]]:
        """
        فتح صفقة ورقية جديدة

        يُطبّق:
        - التحقق من قاطع الدائرة
        - التحقق من حد المراكز المتزامنة
        - تسجيل في قاعدة البيانات
        """
        # تحقق أمان
        assert not settings.LIVE_TRADING_ENABLED
        assert settings.PAPER_TRADING_ENABLED, "PAPER_TRADING_ENABLED يجب أن يكون true"

        # التحقق من عدد المراكز
        open_count = len([t for t in self._open_trades.values() if t["status"] == "open"])
        if open_count >= settings.MAX_CONCURRENT_POSITIONS:
            logger.warning(f"تم الوصول للحد الأقصى للمراكز: {open_count}")
            return None

        trade_id = str(uuid.uuid4())[:8]
        trade = {
            "id": trade_id,
            "symbol": symbol,
            "strategy": strategy,
            "direction": direction,
            "entry_price": entry_price,
            "current_price": entry_price,
            "premium": premium,
            "max_profit": max_profit,
            "max_loss": abs(max_loss),
            "expiry_date": expiry_date,
            "legs": legs or [],
            "status": "open",
            "opened_at": datetime.now().isoformat(),
            "closed_at": None,
            "pnl": 0.0,
            "pnl_percent": 0.0,
            "unrealized_pnl": 0.0,
            "notes": notes,
            "paper_trade": True,   # علامة أمان
            "live_trade": False,   # أبداً
        }

        # حساب مستويات Take Profit و Stop Loss
        if premium > 0:  # استراتيجية ائتمانية
            trade["take_profit"] = round(
                premium * (1 - settings.TAKE_PROFIT_PERCENT / 100), 2
            )
            trade["stop_loss"] = round(
                premium * settings.STOP_LOSS_CREDIT_MULTIPLIER, 2
            )
        else:  # استراتيجية مدينة
            debit = abs(premium)
            trade["take_profit"] = round(debit * (1 + settings.TAKE_PROFIT_PERCENT / 100), 2)
            trade["stop_loss"] = round(debit * settings.STOP_LOSS_DEBIT_PERCENT / 100, 2)

        self._open_trades[trade_id] = trade

        # تخزين في قاعدة البيانات
        if self.db:
            try:
                db_id = self.db.insert_trade(trade)
                trade["db_id"] = db_id
            except Exception as exc:
                logger.warning(f"خطأ في تخزين الصفقة: {exc}")

        # إشعار
        if self.notifier:
            try:
                self.notifier.send_entry(trade)
            except Exception:
                pass

        logger.info(f"فُتحت صفقة ورقية: {symbol} {strategy} | ID: {trade_id}")
        return trade

    # ──────────────────────────────────────────────────────
    # تحديث الصفقات المفتوحة
    # ──────────────────────────────────────────────────────

    def update_trades(self, prices: Dict[str, float]) -> List[Dict]:
        """
        تحديث جميع الصفقات المفتوحة بالأسعار الجديدة
        يتحقق من حدود TP/SL ويُغلق الصفقات المؤهلة
        """
        closed = []
        for trade_id, trade in list(self._open_trades.items()):
            if trade["status"] != "open":
                continue

            symbol = trade["symbol"]
            current_price = prices.get(symbol)
            if current_price is None:
                continue

            trade["current_price"] = current_price
            pnl, pnl_pct = self._calc_pnl(trade, current_price)
            trade["unrealized_pnl"] = pnl
            trade["pnl_percent"] = pnl_pct

            # تحقق من TP/SL
            exit_reason = self._check_exit(trade, pnl, pnl_pct)
            if exit_reason:
                closed.append(self.close_trade(trade_id, exit_reason, current_price))

        return [t for t in closed if t is not None]

    # ──────────────────────────────────────────────────────
    # إغلاق صفقة
    # ──────────────────────────────────────────────────────

    def close_trade(
        self,
        trade_id: str,
        reason: str = "manual",
        exit_price: Optional[float] = None,
    ) -> Optional[Dict]:
        """إغلاق صفقة ورقية"""
        trade = self._open_trades.get(trade_id)
        if not trade or trade["status"] != "open":
            return None

        current_price = exit_price or trade.get("current_price", trade["entry_price"])
        pnl, pnl_pct = self._calc_pnl(trade, current_price)

        trade["status"] = "closed"
        trade["closed_at"] = datetime.now().isoformat()
        trade["exit_price"] = current_price
        trade["pnl"] = pnl
        trade["pnl_percent"] = pnl_pct
        trade["exit_reason"] = reason

        # تحديث إحصائيات اليوم
        self._daily_pnl += pnl
        self._update_drawdown(self._daily_pnl)

        # تحديث قاعدة البيانات
        if self.db and trade.get("db_id"):
            try:
                self.db.update_trade(trade["db_id"], {
                    "status": "closed",
                    "closed_at": trade["closed_at"],
                    "current_price": current_price,
                    "pnl": pnl,
                    "pnl_percent": pnl_pct,
                    "notes": f"{trade.get('notes', '')} | exit: {reason}",
                })
            except Exception as exc:
                logger.warning(f"خطأ في تحديث الصفقة: {exc}")

        # إشعار
        if self.notifier:
            try:
                self.notifier.send_exit(trade, pnl)
            except Exception:
                pass

        result_text = "✅ ربح" if pnl > 0 else "❌ خسارة"
        logger.info(f"أُغلقت صفقة {trade_id}: {result_text} ${pnl:+.2f} ({pnl_pct:+.1f}%) - {reason}")
        return trade

    # ──────────────────────────────────────────────────────
    # إحصائيات
    # ──────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """إحصائيات أداء التداول الورقي"""
        all_closed = [t for t in self._open_trades.values() if t["status"] == "closed"]
        open_trades = [t for t in self._open_trades.values() if t["status"] == "open"]

        wins = [t for t in all_closed if t.get("pnl", 0) > 0]
        losses = [t for t in all_closed if t.get("pnl", 0) <= 0]
        total = len(all_closed)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0

        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = abs(sum(t["pnl"] for t in losses) / len(losses)) if losses else 0
        profit_factor = (avg_win * len(wins)) / (avg_loss * len(losses)) if losses and avg_loss > 0 else 0

        unrealized = sum(t.get("unrealized_pnl", 0) for t in open_trades)

        return {
            "total_trades": total,
            "open_trades": len(open_trades),
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 1),
            "total_realized_pnl": round(self._daily_pnl, 2),
            "total_unrealized_pnl": round(unrealized, 2),
            "average_win": round(avg_win, 2),
            "average_loss": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "max_drawdown": round(self._max_drawdown, 2),
            "paper_trading": True,
            "live_trading": False,
        }

    def get_open_trades(self) -> List[Dict]:
        """قائمة الصفقات المفتوحة"""
        return [t for t in self._open_trades.values() if t["status"] == "open"]

    # ──────────────────────────────────────────────────────
    # حسابات مساعدة
    # ──────────────────────────────────────────────────────

    def _calc_pnl(
        self, trade: Dict, current_price: float
    ) -> Tuple[float, float]:
        """حساب الربح والخسارة"""
        premium = trade.get("premium", 0)
        max_profit = trade.get("max_profit", 0)

        if premium > 0:  # استراتيجية ائتمانية
            # الائتمان المتبقي في السوق (تقريبي)
            pnl = premium - max(0.0, premium * 0.5)
            pnl_pct = (pnl / premium * 100) if premium > 0 else 0
        elif premium < 0:  # استراتيجية مدينة
            debit = abs(premium)
            pnl = max_profit - debit
            pnl_pct = (pnl / debit * 100) if debit > 0 else 0
        else:
            pnl = 0.0
            pnl_pct = 0.0

        return round(pnl, 2), round(pnl_pct, 1)

    def _check_exit(
        self, trade: Dict, pnl: float, pnl_pct: float
    ) -> Optional[str]:
        """التحقق من شروط الخروج"""
        premium = trade.get("premium", 0)

        # Take Profit: 50% من الربح الأقصى
        max_profit = trade.get("max_profit", 0)
        if pnl >= max_profit * (settings.TAKE_PROFIT_PERCENT / 100):
            return "take_profit_50pct"

        # Stop Loss
        if premium > 0:  # ائتماني: 2× الائتمان
            if abs(pnl) >= premium * settings.STOP_LOSS_CREDIT_MULTIPLIER:
                return "stop_loss_2x_credit"
        elif premium < 0:  # مدين: 50% من المدفوع
            debit = abs(premium)
            if pnl < 0 and abs(pnl) >= debit * settings.STOP_LOSS_DEBIT_PERCENT / 100:
                return "stop_loss_50pct_debit"

        return None

    def _update_drawdown(self, current_pnl: float):
        """تحديث الحد الأقصى للتراجع"""
        if current_pnl > self._peak_value:
            self._peak_value = current_pnl
        drawdown = self._peak_value - current_pnl
        if drawdown > self._max_drawdown:
            self._max_drawdown = drawdown

    def reset_daily(self):
        """إعادة تعيين إحصائيات اليوم"""
        self._daily_pnl = 0.0
        self._peak_value = 0.0
        self._max_drawdown = 0.0
        # لا تحذف الصفقات المفتوحة!
        logger.info("تم إعادة تعيين إحصائيات اليوم")
