"""
core/gex_analyzer.py
====================
محلل GEX (Gamma Exposure)
يحسب: Max Pain, Gamma Walls, Zero Gamma Level, Pin Score, GEX Regime
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

from config import settings


@dataclass
class GEXWall:
    """جدار غاما"""
    strike: float
    gex: float
    type: str       # "call_wall", "put_wall", "zero_gamma"
    strength: float = 0.0

    def is_support(self) -> bool:
        return self.type == "put_wall"

    def is_resistance(self) -> bool:
        return self.type == "call_wall"


@dataclass
class GEXResult:
    """نتيجة تحليل GEX"""
    symbol: str
    current_price: float
    max_pain: float = 0.0
    zero_gamma: float = 0.0
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None
    net_gex: float = 0.0
    gex_regime: str = "neutral"    # "positive", "negative", "neutral"
    pin_score: float = 0.0
    expected_move: float = 0.0
    walls: List[GEXWall] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class GEXAnalyzer:
    """
    محلل Gamma Exposure
    يستخدم بيانات سلسلة الخيارات لحساب مستويات الغاما
    """

    def __init__(self):
        logger.debug("تم تهيئة محلل GEX")

    def analyze(
        self,
        symbol: str,
        current_price: float,
        option_chain: Optional[List[Dict]] = None,
        iv: float = 0.2,
        dte: int = 1,
    ) -> Dict[str, Any]:
        """
        تحليل GEX الكامل
        option_chain: قائمة العقود مع: strike, call_oi, put_oi, call_gamma, put_gamma
        """
        result = GEXResult(symbol=symbol, current_price=current_price)

        if not option_chain:
            # تقدير مبدئي بدون بيانات سلسلة خيارات
            result = self._estimate_without_chain(result, iv, dte)
        else:
            result = self._analyze_with_chain(result, option_chain, iv, dte)

        return self._to_dict(result)

    # ──────────────────────────────────────────────────────
    # تحليل مع بيانات سلسلة الخيارات
    # ──────────────────────────────────────────────────────

    def _analyze_with_chain(
        self,
        result: GEXResult,
        chain: List[Dict],
        iv: float,
        dte: int,
    ) -> GEXResult:
        strikes = np.array([c["strike"] for c in chain], dtype=float)
        call_oi = np.array([c.get("call_oi", 0) for c in chain], dtype=float)
        put_oi = np.array([c.get("put_oi", 0) for c in chain], dtype=float)

        # استخدام غاما من البيانات أو تقديرها
        if "call_gamma" in chain[0]:
            call_gamma = np.array([c.get("call_gamma", 0) for c in chain], dtype=float)
            put_gamma = np.array([c.get("put_gamma", 0) for c in chain], dtype=float)
        else:
            call_gamma = self._estimate_gamma(strikes, result.current_price, iv, dte, "call")
            put_gamma = self._estimate_gamma(strikes, result.current_price, iv, dte, "put")

        # GEX لكل سعر تنفيذ (×100 لعامل الضرب القياسي)
        contract_size = 100
        gex_per_strike = (call_oi * call_gamma - put_oi * put_gamma) * contract_size * result.current_price

        # Max Pain: السعر الذي تصبح فيه قيمة الخيارات المنتهية صلاحيتها أقل ما يمكن
        result.max_pain = self._calc_max_pain(strikes, call_oi, put_oi, result.current_price)

        # Zero Gamma: نقطة توازن الغاما
        result.zero_gamma = self._calc_zero_gamma(strikes, gex_per_strike, result.current_price)

        # Net GEX
        result.net_gex = float(np.sum(gex_per_strike))

        # تحديد النظام
        result.gex_regime = "positive" if result.net_gex > 0 else "negative"

        # الجدران
        result.walls = self._find_walls(strikes, gex_per_strike, result.current_price)

        # Call/Put Wall
        call_walls = [w for w in result.walls if w.type == "call_wall"]
        put_walls = [w for w in result.walls if w.type == "put_wall"]
        if call_walls:
            result.call_wall = sorted(call_walls, key=lambda w: abs(w.gex), reverse=True)[0].strike
        if put_walls:
            result.put_wall = sorted(put_walls, key=lambda w: abs(w.gex), reverse=True)[0].strike

        # Pin Score: مدى قرب السعر من Max Pain
        if result.max_pain > 0:
            dist_pct = abs(result.current_price - result.max_pain) / result.current_price
            result.pin_score = max(0.0, 1.0 - dist_pct * 10)

        # الحركة المتوقعة
        result.expected_move = self._calc_expected_move(result.current_price, iv, dte)

        return result

    # ──────────────────────────────────────────────────────
    # تقدير بدون بيانات
    # ──────────────────────────────────────────────────────

    def _estimate_without_chain(
        self, result: GEXResult, iv: float, dte: int
    ) -> GEXResult:
        price = result.current_price
        em = self._calc_expected_move(price, iv, dte)
        result.expected_move = em
        result.max_pain = price  # تقدير مبدئي
        result.zero_gamma = price
        result.gex_regime = "neutral"
        result.pin_score = 0.5
        result.call_wall = round(price + em, 0)
        result.put_wall = round(price - em, 0)
        result.notes.append("estimated_without_option_chain")
        return result

    # ──────────────────────────────────────────────────────
    # حسابات مساعدة
    # ──────────────────────────────────────────────────────

    def _calc_max_pain(
        self,
        strikes: np.ndarray,
        call_oi: np.ndarray,
        put_oi: np.ndarray,
        current_price: float,
    ) -> float:
        """حساب Max Pain بطريقة المجموع التراكمي"""
        total_losses = []
        for test_price in strikes:
            call_loss = float(np.sum(np.maximum(test_price - strikes, 0) * call_oi))
            put_loss = float(np.sum(np.maximum(strikes - test_price, 0) * put_oi))
            total_losses.append((test_price, call_loss + put_loss))

        if not total_losses:
            return current_price

        min_loss = min(total_losses, key=lambda x: x[1])
        return float(min_loss[0])

    def _calc_zero_gamma(
        self,
        strikes: np.ndarray,
        gex_per_strike: np.ndarray,
        current_price: float,
    ) -> float:
        """حساب مستوى Zero Gamma"""
        if len(strikes) < 2:
            return current_price

        # إيجاد التقاطع مع الصفر
        above = current_price
        below = current_price
        for i in range(len(strikes) - 1):
            if gex_per_strike[i] * gex_per_strike[i + 1] < 0:
                # تقاطع خطي
                t = gex_per_strike[i] / (gex_per_strike[i] - gex_per_strike[i + 1])
                zero_level = strikes[i] + t * (strikes[i + 1] - strikes[i])
                if abs(zero_level - current_price) < abs(above - current_price):
                    above = zero_level
                if abs(zero_level - current_price) < abs(below - current_price):
                    below = zero_level

        return float(above)

    def _find_walls(
        self,
        strikes: np.ndarray,
        gex_per_strike: np.ndarray,
        current_price: float,
    ) -> List[GEXWall]:
        walls: List[GEXWall] = []
        if len(strikes) == 0:
            return walls

        max_abs = float(np.max(np.abs(gex_per_strike))) if len(gex_per_strike) > 0 else 1.0

        # أعلى 5 جدران موجبة (Call Walls) فوق السعر
        above_mask = strikes > current_price
        above_strikes = strikes[above_mask]
        above_gex = gex_per_strike[above_mask]
        if len(above_gex) > 0:
            top_idx = np.argsort(above_gex)[-3:]
            for idx in top_idx:
                if above_gex[idx] > 0:
                    strength = abs(above_gex[idx]) / max_abs if max_abs > 0 else 0
                    if strength >= settings.GEX_WALL_STRENGTH_MIN:
                        walls.append(GEXWall(
                            strike=float(above_strikes[idx]),
                            gex=float(above_gex[idx]),
                            type="call_wall",
                            strength=round(strength, 3),
                        ))

        # أعلى 5 جدران سالبة (Put Walls) تحت السعر
        below_mask = strikes < current_price
        below_strikes = strikes[below_mask]
        below_gex = gex_per_strike[below_mask]
        if len(below_gex) > 0:
            bot_idx = np.argsort(below_gex)[:3]
            for idx in bot_idx:
                if below_gex[idx] < 0:
                    strength = abs(below_gex[idx]) / max_abs if max_abs > 0 else 0
                    if strength >= settings.GEX_WALL_STRENGTH_MIN:
                        walls.append(GEXWall(
                            strike=float(below_strikes[idx]),
                            gex=float(below_gex[idx]),
                            type="put_wall",
                            strength=round(strength, 3),
                        ))

        return walls

    def _estimate_gamma(
        self,
        strikes: np.ndarray,
        spot: float,
        iv: float,
        dte: int,
        option_type: str,
    ) -> np.ndarray:
        """تقدير الغاما باستخدام Black-Scholes المبسط"""
        import math
        T = max(dte / 365.0, 1 / 365.0)
        sigma = max(iv, 0.01)
        results = []
        for K in strikes:
            try:
                if spot <= 0 or K <= 0:
                    results.append(0.0)
                    continue
                d1 = (math.log(spot / K) + 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
                # دالة الكثافة الاحتمالية
                phi = math.exp(-0.5 * d1 ** 2) / math.sqrt(2 * math.pi)
                gamma = phi / (spot * sigma * math.sqrt(T))
                results.append(gamma)
            except Exception:
                results.append(0.0)
        return np.array(results, dtype=float)

    def _calc_expected_move(
        self, price: float, iv: float, dte: int
    ) -> float:
        """حساب الحركة المتوقعة ± بناءً على IV"""
        T = max(dte / 365.0, 1 / 365.0)
        return round(price * iv * (T ** 0.5), 2)

    def _to_dict(self, result: GEXResult) -> Dict[str, Any]:
        return {
            "symbol": result.symbol,
            "current_price": result.current_price,
            "max_pain": round(result.max_pain, 2),
            "zero_gamma": round(result.zero_gamma, 2),
            "call_wall": round(result.call_wall, 2) if result.call_wall else None,
            "put_wall": round(result.put_wall, 2) if result.put_wall else None,
            "net_gex": round(result.net_gex, 0),
            "gex_regime": result.gex_regime,
            "pin_score": round(result.pin_score, 3),
            "expected_move": result.expected_move,
            "walls": [
                {
                    "strike": w.strike,
                    "gex": round(w.gex, 0),
                    "type": w.type,
                    "strength": w.strength,
                }
                for w in result.walls
            ],
            "notes": result.notes,
        }
