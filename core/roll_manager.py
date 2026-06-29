"""
core/roll_manager.py
====================
مدير تدوير المراكز - Roll Manager
يدعم: Roll Forward, Roll Up, Roll Down, Roll Out
للتعامل مع المراكز المفتوحة التي تحتاج إلى إدارة
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from loguru import logger

from config import settings


@dataclass
class RollDecision:
    """قرار تدوير مركز"""
    action: str           # "roll_forward", "roll_up", "roll_down", "roll_out", "no_action"
    reason: str
    original_strike: Optional[float] = None
    new_strike: Optional[float] = None
    original_expiry: Optional[str] = None
    new_expiry: Optional[str] = None
    estimated_credit: Optional[float] = None
    estimated_debit: Optional[float] = None
    net_cost: Optional[float] = None
    urgency: str = "low"  # "low", "medium", "high", "critical"
    notes: List[str] = None

    def __post_init__(self):
        if self.notes is None:
            self.notes = []

    def is_actionable(self) -> bool:
        return self.action != "no_action"


class RollManager:
    """
    مدير تدوير المراكز
    يقيم المراكز المفتوحة ويوصي بالتدوير المناسب
    """

    # حدود التدوير
    DTE_DANGER_ZONE = 7           # أيام للانتهاء - منطقة الخطر
    DTE_ROLL_FORWARD_TRIGGER = 14 # أيام للانتهاء - بدء التفكير في التدوير
    LOSS_ROLL_TRIGGER = 1.5       # 1.5× الائتمان - حد الخسارة للتدوير
    PROFIT_ROLL_TRIGGER = 0.21    # 21% من الربح الأقصى - خيار التدوير المبكر

    def __init__(self):
        logger.debug("تم تهيئة مدير التدوير")

    def evaluate_position(
        self,
        position: Dict[str, Any],
        current_price: float,
        option_data: Optional[Dict] = None,
    ) -> RollDecision:
        """
        تقييم مركز مفتوح وتحديد إجراء التدوير المناسب

        Args:
            position: بيانات المركز من قاعدة البيانات
            current_price: السعر الحالي للأصل الأساسي
            option_data: بيانات الخيارات الحالية (اختياري)
        """
        strategy = position.get("strategy", "")
        dte = self._calc_dte(position.get("expiry_date") or position.get("expiry"))
        pnl_pct = position.get("pnl_percent", 0) or 0
        max_profit = position.get("max_profit") or 0
        max_loss = position.get("max_loss") or 0
        premium = position.get("premium") or 0

        # تقييم الحالة الحالية
        unrealized_loss = abs(min(pnl_pct, 0))
        unrealized_profit = max(pnl_pct, 0)

        notes = []

        # -- الحالة 1: DTE حرج جداً
        if dte is not None and dte <= self.DTE_DANGER_ZONE:
            notes.append(f"DTE حرج: {dte} يوم متبقٍ")
            if unrealized_loss > 0:
                return RollDecision(
                    action="roll_forward",
                    reason=f"DTE حرج ({dte} أيام) مع خسارة - تدوير للأمام",
                    original_expiry=position.get("expiry_date"),
                    new_expiry=self._suggest_new_expiry(dte, strategy),
                    urgency="critical",
                    notes=notes,
                )
            elif unrealized_profit >= 0.5:
                # ربح كافٍ - لا تدوير
                return RollDecision(
                    action="no_action",
                    reason="DTE حرج لكن الربح > 50% - خذ الربح",
                    urgency="high",
                    notes=notes,
                )

        # -- الحالة 2: الخسارة تتجاوز الحد
        if premium > 0 and unrealized_loss > 0:
            loss_ratio = abs(pnl_pct) / 100.0 * (max_loss or premium * 2) / premium
            if loss_ratio >= self.LOSS_ROLL_TRIGGER:
                notes.append(f"الخسارة {loss_ratio:.1f}× الائتمان")
                return self._decide_directional_roll(
                    position, current_price, dte, notes, "loss"
                )

        # -- الحالة 3: نقطة التدوير الأمثل (DTE 14-21)
        if dte is not None and self.DTE_DANGER_ZONE < dte <= self.DTE_ROLL_FORWARD_TRIGGER:
            if unrealized_profit >= 0.5 * 100:  # 50% من الربح الأقصى
                return RollDecision(
                    action="roll_forward",
                    reason=f"الربح 50%+ مع DTE={dte} - تدوير للأمام لتجميع ائتمان إضافي",
                    original_expiry=position.get("expiry_date"),
                    new_expiry=self._suggest_new_expiry(dte, strategy),
                    urgency="medium",
                    notes=notes,
                )

        # -- الحالة 4: لا إجراء مطلوب
        return RollDecision(
            action="no_action",
            reason="المركز ضمن الحدود الطبيعية",
            urgency="low",
            notes=notes,
        )

    def _decide_directional_roll(
        self,
        position: Dict,
        current_price: float,
        dte: Optional[int],
        notes: List[str],
        trigger: str,
    ) -> RollDecision:
        """تحديد نوع التدوير الاتجاهي"""
        strategy = position.get("strategy", "")
        short_strike = position.get("short_strike") or position.get("entry_price")

        if not short_strike:
            return RollDecision(
                action="roll_forward",
                reason=f"تدوير للأمام ({trigger})",
                urgency="high",
                notes=notes,
            )

        # Bull Put Spread: إذا تحرك السعر للأسفل → Roll Down
        if "bull put" in strategy.lower() or "put spread" in strategy.lower():
            if current_price < short_strike:
                new_strike = round(current_price * 0.97, 0)  # 3% تحت السعر الحالي
                return RollDecision(
                    action="roll_down",
                    reason=f"Bull Put تحت سعر التنفيذ - تدوير للأسفل",
                    original_strike=short_strike,
                    new_strike=new_strike,
                    original_expiry=position.get("expiry_date"),
                    new_expiry=self._suggest_new_expiry(dte, strategy),
                    urgency="high",
                    notes=notes,
                )

        # Bear Call Spread: إذا تحرك السعر للأعلى → Roll Up
        elif "bear call" in strategy.lower() or "call spread" in strategy.lower():
            if current_price > short_strike:
                new_strike = round(current_price * 1.03, 0)  # 3% فوق السعر الحالي
                return RollDecision(
                    action="roll_up",
                    reason=f"Bear Call فوق سعر التنفيذ - تدوير للأعلى",
                    original_strike=short_strike,
                    new_strike=new_strike,
                    original_expiry=position.get("expiry_date"),
                    new_expiry=self._suggest_new_expiry(dte, strategy),
                    urgency="high",
                    notes=notes,
                )

        # Iron Condor أو استراتيجيات أخرى
        return RollDecision(
            action="roll_out",
            reason=f"تدوير للأمام مع تعديل ({trigger})",
            original_expiry=position.get("expiry_date"),
            new_expiry=self._suggest_new_expiry(dte, strategy),
            urgency="high",
            notes=notes,
        )

    def _calc_dte(self, expiry: Optional[str]) -> Optional[int]:
        """حساب أيام الانتهاء المتبقية"""
        if not expiry:
            return None
        try:
            exp_date = datetime.strptime(str(expiry)[:10], "%Y-%m-%d").date()
            today = datetime.now().date()
            return max(0, (exp_date - today).days)
        except Exception:
            return None

    def _suggest_new_expiry(
        self, current_dte: Optional[int], strategy: str
    ) -> str:
        """اقتراح تاريخ انتهاء جديد"""
        # للـ Swing: أضف 21-30 يوم
        target_dte = 30
        if "0dte" in strategy.lower() or current_dte == 0:
            target_dte = 7  # تدوير قصير المدى لـ 0DTE
        new_date = datetime.now() + timedelta(days=target_dte)
        # الجمعة الأقرب
        while new_date.weekday() != 4:  # 4 = الجمعة
            new_date += timedelta(days=1)
        return new_date.strftime("%Y-%m-%d")

    def evaluate_all_positions(
        self,
        positions: List[Dict],
        prices: Dict[str, float],
    ) -> List[Dict[str, Any]]:
        """تقييم جميع المراكز المفتوحة"""
        decisions = []
        for pos in positions:
            symbol = pos.get("symbol", "")
            price = prices.get(symbol, pos.get("current_price") or 0)
            if price <= 0:
                continue
            try:
                decision = self.evaluate_position(pos, price)
                if decision.is_actionable():
                    decisions.append({
                        "position_id": pos.get("id"),
                        "symbol": symbol,
                        "strategy": pos.get("strategy"),
                        "action": decision.action,
                        "reason": decision.reason,
                        "urgency": decision.urgency,
                        "new_strike": decision.new_strike,
                        "new_expiry": decision.new_expiry,
                        "notes": decision.notes,
                    })
            except Exception as exc:
                logger.warning(f"خطأ في تقييم مركز {pos.get('id')}: {exc}")

        # ترتيب حسب الأولوية
        urgency_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        decisions.sort(key=lambda d: urgency_order.get(d["urgency"], 9))
        return decisions
