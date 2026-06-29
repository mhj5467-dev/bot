"""
notifications/telegram_notifier.py
===================================
نظام إشعارات تيليغرام
يرسل: الإشارات، الدخول، الخروج، التنبيهات، الأخطاء
يدعم: الرسائل العربية والإنجليزية
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

from config import settings


class TelegramNotifier:
    """
    مُرسل إشعارات تيليغرام
    يرسل رسائل منسقة بالعربية والإنجليزية
    """

    BASE_URL = "https://api.telegram.org/bot"
    MAX_RETRIES = 3
    RETRY_DELAY = 2.0   # ثانية

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.enabled = settings.TELEGRAM_ENABLED and bool(self.token) and bool(self.chat_id)
        self._last_send: float = 0.0
        self._min_interval = 1.0  # ثانية بين الرسائل
        if not self.enabled:
            logger.warning("إشعارات تيليغرام معطّلة (token أو chat_id مفقود)")

    # ──────────────────────────────────────────────────────
    # إرسال الرسائل الأساسية
    # ──────────────────────────────────────────────────────

    def send_message(
        self,
        text: str,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> Dict[str, Any]:
        """إرسال رسالة نصية"""
        if not self.enabled:
            logger.debug(f"[تيليغرام معطّل] {text[:80]}")
            return {"ok": False, "reason": "disabled"}

        # تحديد المعدل
        now = time.time()
        if now - self._last_send < self._min_interval:
            time.sleep(self._min_interval - (now - self._last_send))

        url = f"{self.BASE_URL}{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_notification": disable_notification,
        }

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                self._last_send = time.time()
                if resp.status_code == 200:
                    return {"ok": True, "message_id": resp.json().get("result", {}).get("message_id")}
                elif resp.status_code == 429:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                    logger.warning(f"تيليغرام: حد الإرسال - انتظار {retry_after}ث")
                    time.sleep(retry_after)
                else:
                    logger.warning(f"تيليغرام HTTP {resp.status_code}: {resp.text[:100]}")
                    break
            except Exception as exc:
                logger.warning(f"خطأ تيليغرام (محاولة {attempt + 1}): {exc}")
                if attempt < self.MAX_RETRIES - 1:
                    time.sleep(self.RETRY_DELAY)

        return {"ok": False, "reason": "max_retries_exceeded"}

    # ──────────────────────────────────────────────────────
    # رسائل متخصصة
    # ──────────────────────────────────────────────────────

    def send_signal(self, signal: Dict[str, Any]) -> Dict:
        """إرسال إشارة تداول"""
        symbol = signal.get("symbol", "")
        strategy = signal.get("strategy", "")
        direction = signal.get("direction", "")
        price = signal.get("price", 0)
        grade = signal.get("grade", "")
        score = signal.get("score", 0)
        iv_rank = signal.get("iv_rank")

        direction_emoji = "🟢" if "bull" in direction.lower() else "🔴" if "bear" in direction.lower() else "⚪"

        iv_text = f"{iv_rank:.0f}%" if iv_rank is not None else "N/A"

        text = (
            f"<b>📊 إشارة تداول - ICT Pro Bot</b>\n"
            f"{'━' * 30}\n"
            f"{direction_emoji} <b>الرمز:</b> {symbol}\n"
            f"<b>الاستراتيجية:</b> {strategy}\n"
            f"<b>السعر:</b> {price:,.2f}\n"
            f"<b>الدرجة:</b> {grade} ({score:.0f}/100)\n"
            f"<b>IV Rank:</b> {iv_text}\n"
            f"<b>الوقت:</b> {datetime.now().strftime('%H:%M:%S ET')}\n"
            f"{'━' * 30}\n"
            f"<i>⚠️ ورقي فقط - لا صفقات حقيقية</i>"
        )
        return self.send_message(text)

    def send_entry(self, trade: Dict[str, Any]) -> Dict:
        """إرسال إشعار دخول صفقة"""
        symbol = trade.get("symbol", "")
        strategy = trade.get("strategy", "")
        premium = trade.get("premium", 0)
        max_profit = trade.get("max_profit", 0)
        max_loss = trade.get("max_loss", 0)
        expiry = trade.get("expiry_date", "")

        text = (
            f"<b>✅ دخول صفقة جديدة (ورقي)</b>\n"
            f"{'━' * 30}\n"
            f"<b>الرمز:</b> {symbol}\n"
            f"<b>الاستراتيجية:</b> {strategy}\n"
            f"<b>الائتمان المستلم:</b> ${premium:,.2f}\n"
            f"<b>الربح الأقصى:</b> ${max_profit:,.2f}\n"
            f"<b>الخسارة القصوى:</b> ${max_loss:,.2f}\n"
            f"<b>الانتهاء:</b> {expiry}\n"
            f"<b>الوقت:</b> {datetime.now().strftime('%H:%M:%S ET')}\n"
            f"{'━' * 30}\n"
            f"<b>هدف الخروج:</b> 50% من الربح الأقصى\n"
            f"<b>وقف الخسارة:</b> 2× الائتمان\n"
            f"<i>⚠️ ورقي فقط</i>"
        )
        return self.send_message(text)

    def send_exit(self, trade: Dict[str, Any], pnl: float) -> Dict:
        """إرسال إشعار خروج صفقة"""
        symbol = trade.get("symbol", "")
        strategy = trade.get("strategy", "")
        reason = trade.get("exit_reason", "")
        pnl_pct = trade.get("pnl_percent", 0)

        emoji = "✅" if pnl > 0 else "❌"
        result_text = "ربح" if pnl > 0 else "خسارة"

        text = (
            f"<b>{emoji} {result_text} - إغلاق صفقة (ورقي)</b>\n"
            f"{'━' * 30}\n"
            f"<b>الرمز:</b> {symbol}\n"
            f"<b>الاستراتيجية:</b> {strategy}\n"
            f"<b>P&L:</b> ${pnl:+,.2f} ({pnl_pct:+.1f}%)\n"
            f"<b>السبب:</b> {reason}\n"
            f"<b>الوقت:</b> {datetime.now().strftime('%H:%M:%S ET')}\n"
            f"<i>⚠️ ورقي فقط</i>"
        )
        return self.send_message(text)

    def send_circuit_breaker(self, reason: str, daily_loss: float) -> Dict:
        """إرسال تنبيه قاطع الدائرة"""
        text = (
            f"<b>🚨 تنبيه: قاطع الدائرة نشط!</b>\n"
            f"{'━' * 30}\n"
            f"<b>السبب:</b> {reason}\n"
            f"<b>الخسارة اليومية:</b> ${daily_loss:,.2f}\n"
            f"<b>الحد الأقصى:</b> ${settings.MAX_DAILY_LOSS:,.2f}\n"
            f"<b>الوقت:</b> {datetime.now().strftime('%H:%M:%S ET')}\n"
            f"{'━' * 30}\n"
            f"<b>⛔ تم إيقاف التداول الورقي لبقية اليوم</b>"
        )
        return self.send_message(text)

    def send_daily_summary(self, stats: Dict[str, Any]) -> Dict:
        """إرسال ملخص اليوم"""
        date = stats.get("date", datetime.now().strftime("%Y-%m-%d"))
        total_pnl = stats.get("total_pnl", 0)
        win_count = stats.get("win_count", 0)
        loss_count = stats.get("loss_count", 0)
        total_trades = stats.get("total_trades", 0)
        win_rate = stats.get("win_rate", 0)

        emoji = "📈" if total_pnl > 0 else "📉"

        text = (
            f"<b>{emoji} ملخص اليوم - {date}</b>\n"
            f"{'━' * 30}\n"
            f"<b>إجمالي P&L:</b> ${total_pnl:+,.2f}\n"
            f"<b>الصفقات:</b> {total_trades} (✅{win_count} / ❌{loss_count})\n"
            f"<b>نسبة الفوز:</b> {win_rate:.1f}%\n"
            f"{'━' * 30}\n"
            f"<i>بوت أبو حسن - ICT Pro Bot</i>"
        )
        return self.send_message(text)

    def send_error(self, error: str, component: str = "") -> Dict:
        """إرسال تنبيه خطأ"""
        text = (
            f"<b>⚠️ خطأ في النظام</b>\n"
            f"{'━' * 30}\n"
            f"<b>المكوّن:</b> {component or 'غير محدد'}\n"
            f"<b>الخطأ:</b> {str(error)[:200]}\n"
            f"<b>الوقت:</b> {datetime.now().strftime('%H:%M:%S ET')}"
        )
        return self.send_message(text, disable_notification=True)

    def send_system_start(self) -> Dict:
        """إرسال إشعار بدء النظام"""
        text = (
            f"<b>🚀 ICT Pro Bot - Abu Hassan Bot</b>\n"
            f"{'━' * 30}\n"
            f"<b>الحالة:</b> ✅ يعمل\n"
            f"<b>الوضع:</b> تداول ورقي\n"
            f"<b>الوقت:</b> {datetime.now().strftime('%Y-%m-%d %H:%M:%S ET')}\n"
            f"{'━' * 30}\n"
            f"<b>الرموز:</b> SPX | SPY | QQQ | IWM | AAPL | NVDA | GLD\n"
            f"<i>⚠️ LIVE_TRADING_ENABLED=false - آمن تماماً</i>"
        )
        return self.send_message(text)

    def test_connection(self) -> bool:
        """اختبار الاتصال"""
        if not self.enabled:
            return False
        result = self.send_message(
            "🔗 اختبار اتصال ICT Pro Bot - تيليغرام يعمل!",
            disable_notification=True,
        )
        return result.get("ok", False)
