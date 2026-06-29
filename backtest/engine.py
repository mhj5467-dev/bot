"""
backtest/engine.py
==================
محرك الاختبار التاريخي
يختبر الاستراتيجيات على بيانات تاريخية
المرحلة M4 - قيد التطوير
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

from config import settings


@dataclass
class BacktestResult:
    """نتيجة اختبار تاريخي"""
    strategy: str
    symbol: str
    start_date: str
    end_date: str
    total_trades: int = 0
    win_count: int = 0
    loss_count: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    average_win: float = 0.0
    average_loss: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    trades: List[Dict] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class BacktestEngine:
    """
    محرك الاختبار التاريخي - M4
    يُشغّل محاكاة على بيانات تاريخية لقياس أداء الاستراتيجيات
    """

    def __init__(self):
        if not settings.PHASE_M4_BACKTEST:
            logger.warning("المرحلة M4 (Backtest) غير مفعّلة في الإعدادات")
        logger.debug("تم تهيئة محرك الاختبار التاريخي")

    def run(
        self,
        strategy: str,
        symbol: str,
        start_date: str,
        end_date: str,
        candles: List[Dict],
        params: Optional[Dict] = None,
    ) -> BacktestResult:
        """
        تشغيل اختبار تاريخي

        Args:
            strategy: اسم الاستراتيجية
            symbol: رمز الأصل
            start_date: تاريخ البدء (YYYY-MM-DD)
            end_date: تاريخ الانتهاء (YYYY-MM-DD)
            candles: قائمة الشموع التاريخية
            params: معاملات مخصصة
        """
        result = BacktestResult(
            strategy=strategy,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )

        if not candles:
            result.notes.append("لا توجد بيانات تاريخية")
            return result

        logger.info(f"بدء اختبار {strategy} على {symbol} ({start_date} → {end_date})")

        params = params or {}
        iv_threshold_high = params.get("iv_high", settings.IV_HIGH_THRESHOLD)
        iv_threshold_low = params.get("iv_low", settings.IV_LOW_THRESHOLD)
        take_profit_pct = params.get("take_profit", settings.TAKE_PROFIT_PERCENT) / 100
        stop_loss_mult = params.get("stop_loss", settings.STOP_LOSS_CREDIT_MULTIPLIER)

        simulated_trades = self._simulate_trades(
            candles, strategy, symbol, iv_threshold_high, iv_threshold_low,
            take_profit_pct, stop_loss_mult, params
        )

        result.trades = simulated_trades
        result = self._calc_stats(result)
        logger.info(
            f"اكتمل الاختبار: {result.total_trades} صفقة | "
            f"الفوز: {result.win_rate:.1f}% | "
            f"P&L: ${result.total_pnl:+,.2f}"
        )
        return result

    # ──────────────────────────────────────────────────────
    # محاكاة الصفقات
    # ──────────────────────────────────────────────────────

    def _simulate_trades(
        self,
        candles: List[Dict],
        strategy: str,
        symbol: str,
        iv_high: float,
        iv_low: float,
        tp_pct: float,
        sl_mult: float,
        params: Dict,
    ) -> List[Dict]:
        """محاكاة الصفقات على البيانات التاريخية"""
        trades = []
        n = len(candles)
        if n < 20:
            return trades

        closes = [c["close"] for c in candles]
        highs = [c["high"] for c in candles]
        lows = [c["low"] for c in candles]
        timestamps = [c.get("timestamp", "") for c in candles]

        # محاكاة مبسطة بناءً على IV
        for i in range(20, n - 5):
            candle = candles[i]
            iv = candle.get("iv", 0.2) * 100  # تحويل لنسبة مئوية

            # تحديد الاستراتيجية المناسبة
            if iv >= iv_high and "spread" in strategy.lower():
                entry_eligible = True
            elif iv <= iv_low and "debit" in strategy.lower():
                entry_eligible = True
            else:
                entry_eligible = False

            if not entry_eligible:
                continue

            # محاكاة الدخول
            entry_price = closes[i]
            premium = entry_price * 0.01 * (iv / 100)  # تقدير الائتمان

            # محاكاة الخروج (حتى 5 شموع)
            exit_price = closes[min(i + 5, n - 1)]
            pnl = self._calc_trade_pnl(premium, entry_price, exit_price, strategy, tp_pct, sl_mult)

            trades.append({
                "entry_date": timestamps[i][:10],
                "exit_date": timestamps[min(i + 5, n - 1)][:10],
                "entry_price": entry_price,
                "exit_price": exit_price,
                "premium": premium,
                "pnl": pnl,
                "iv_at_entry": iv,
                "strategy": strategy,
                "symbol": symbol,
            })

        return trades

    def _calc_trade_pnl(
        self,
        premium: float,
        entry_price: float,
        exit_price: float,
        strategy: str,
        tp_pct: float,
        sl_mult: float,
    ) -> float:
        """حساب P&L للصفقة المحاكاة"""
        price_change_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0

        if "put" in strategy.lower() and "bull" in strategy.lower():
            # Bull Put Spread: يربح عند ثبات السعر أو ارتفاعه
            if price_change_pct >= 0:
                return premium * tp_pct
            else:
                loss = abs(price_change_pct) * 10 * premium
                return -min(loss, premium * sl_mult)

        elif "call" in strategy.lower() and "bear" in strategy.lower():
            # Bear Call Spread: يربح عند ثبات السعر أو هبوطه
            if price_change_pct <= 0:
                return premium * tp_pct
            else:
                loss = abs(price_change_pct) * 10 * premium
                return -min(loss, premium * sl_mult)

        elif "condor" in strategy.lower():
            # Iron Condor: يربح عند ثبات السعر
            if abs(price_change_pct) < 0.02:
                return premium * tp_pct
            else:
                return -premium * sl_mult * 0.5

        # افتراضي
        return premium * tp_pct if price_change_pct * (1 if "bull" in strategy.lower() else -1) > 0 else -premium * sl_mult

    # ──────────────────────────────────────────────────────
    # حساب الإحصائيات
    # ──────────────────────────────────────────────────────

    def _calc_stats(self, result: BacktestResult) -> BacktestResult:
        """حساب إحصائيات الأداء"""
        trades = result.trades
        if not trades:
            return result

        pnls = [t["pnl"] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        result.total_trades = len(pnls)
        result.win_count = len(wins)
        result.loss_count = len(losses)
        result.win_rate = round(len(wins) / len(pnls) * 100, 1) if pnls else 0
        result.total_pnl = round(sum(pnls), 2)
        result.average_win = round(sum(wins) / len(wins), 2) if wins else 0
        result.average_loss = round(abs(sum(losses) / len(losses)), 2) if losses else 0

        if losses and result.average_loss > 0:
            result.profit_factor = round(
                (result.average_win * len(wins)) / (result.average_loss * len(losses)), 2
            )

        # Max Drawdown
        cumulative = np.cumsum(pnls)
        peak = np.maximum.accumulate(cumulative)
        drawdown = peak - cumulative
        result.max_drawdown = round(float(np.max(drawdown)), 2) if len(drawdown) > 0 else 0

        # Sharpe Ratio (مبسط)
        if len(pnls) > 1:
            pnl_arr = np.array(pnls)
            if pnl_arr.std() > 0:
                result.sharpe_ratio = round(float(pnl_arr.mean() / pnl_arr.std() * (252 ** 0.5)), 2)

        return result

    def compare_strategies(
        self,
        symbol: str,
        candles: List[Dict],
        start_date: str,
        end_date: str,
    ) -> List[Dict]:
        """مقارنة أداء استراتيجيات متعددة"""
        strategies = [
            "Bull Put Spread",
            "Bear Call Spread",
            "Iron Condor",
            "Iron Butterfly",
        ]
        results = []
        for strat in strategies:
            try:
                r = self.run(strat, symbol, start_date, end_date, candles)
                results.append({
                    "strategy": strat,
                    "win_rate": r.win_rate,
                    "total_pnl": r.total_pnl,
                    "profit_factor": r.profit_factor,
                    "max_drawdown": r.max_drawdown,
                    "sharpe_ratio": r.sharpe_ratio,
                    "total_trades": r.total_trades,
                })
            except Exception as exc:
                logger.warning(f"خطأ في اختبار {strat}: {exc}")

        results.sort(key=lambda x: x["sharpe_ratio"], reverse=True)
        return results
