"""
core/ict_analyzer.py
====================
محرك تحليل ICT العميق - ICT Deep Analysis Engine
يحلل: Order Blocks (A+/A/B), Fair Value Gaps, Liquidity Sweeps,
       BOS/MSS, Displacement Candles
تحليل من أعلى إلى أسفل: Weekly → Daily → 4H → 1H → 15M → 5M
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from loguru import logger

from config import settings


# ============================================================
# هياكل البيانات
# ============================================================

@dataclass
class OrderBlock:
    """كتلة الأوامر - Order Block"""
    timeframe: str
    direction: str        # "bullish" أو "bearish"
    high: float
    low: float
    open: float
    close: float
    index: int
    timestamp: str = ""
    grade: str = "B"      # A+, A, B
    confluence_score: float = 0.0
    mitigated: bool = False
    fvg_present: bool = False
    displacement_confirmed: bool = False
    bos_after: bool = False

    def midpoint(self) -> float:
        return (self.high + self.low) / 2.0

    def is_valid(self, current_price: float) -> bool:
        """هل الكتلة لا تزال صالحة (غير مُخففة)"""
        if self.mitigated:
            return False
        if self.direction == "bullish":
            return current_price > self.low
        return current_price < self.high


@dataclass
class FairValueGap:
    """فجوة القيمة العادلة - Fair Value Gap"""
    timeframe: str
    direction: str       # "bullish" (FVG صعودي) أو "bearish"
    top: float
    bottom: float
    timestamp: str = ""
    index: int = 0
    filled: bool = False
    partial_fill: float = 0.0

    def size(self) -> float:
        return abs(self.top - self.bottom)


@dataclass
class LiquiditySweep:
    """كنس السيولة - Liquidity Sweep"""
    timeframe: str
    direction: str       # "buy_side" أو "sell_side"
    swept_level: float
    sweep_high: float
    sweep_low: float
    timestamp: str = ""
    index: int = 0
    reversed: bool = False  # هل عكست السعر بعد الكنس


@dataclass
class StructurePoint:
    """نقطة هيكل السوق"""
    type: str            # "HH", "HL", "LH", "LL", "BOS", "MSS", "CHoCH"
    price: float
    timestamp: str = ""
    index: int = 0
    timeframe: str = ""
    confirmed: bool = False


@dataclass
class ICTAnalysisResult:
    """نتيجة التحليل الكاملة"""
    symbol: str
    timeframe: str
    bias: str = "neutral"       # bullish, bearish, neutral
    order_blocks: List[OrderBlock] = field(default_factory=list)
    fvgs: List[FairValueGap] = field(default_factory=list)
    liquidity_sweeps: List[LiquiditySweep] = field(default_factory=list)
    structure_points: List[StructurePoint] = field(default_factory=list)
    top_ob: Optional[OrderBlock] = None   # أفضل OB للدخول
    confluence_score: float = 0.0
    grade: str = "B"
    setup_valid: bool = False
    notes: List[str] = field(default_factory=list)


# ============================================================
# المحرك الرئيسي
# ============================================================

class ICTAnalyzer:
    """
    محلل ICT العميق
    يطبق منهجية ICT الكاملة مع التحليل من الأعلى للأسفل
    """

    # الحد الأدنى لحجم الشمعة لتصنيفها displacement
    DISPLACEMENT_THRESHOLD = 1.5   # معامل ATR

    # الحد الأدنى لحجم FVG
    MIN_FVG_SIZE_ATR = 0.3

    def __init__(self):
        logger.debug("تم تهيئة محلل ICT")

    # ──────────────────────────────────────────────────────
    # نقطة الدخول الرئيسية
    # ──────────────────────────────────────────────────────

    def analyze(
        self,
        symbol: str,
        candles_by_tf: Dict[str, List[Dict]],
        current_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        """
        تحليل ICT كامل لرمز معين
        candles_by_tf: قاموس الإطارات الزمنية → قائمة الشموع
        كل شمعة: {"open": float, "high": float, "low": float, "close": float, "timestamp": str}
        """
        results: Dict[str, ICTAnalysisResult] = {}

        # التحليل من الأعلى للأسفل
        for tf in settings.TIMEFRAMES:
            candles = candles_by_tf.get(tf, [])
            if len(candles) < 20:
                continue
            try:
                result = self._analyze_timeframe(symbol, tf, candles, current_price)
                results[tf] = result
            except Exception as exc:
                logger.warning(f"خطأ في تحليل {symbol} على {tf}: {exc}")

        # دمج نتائج الإطارات الزمنية
        summary = self._build_summary(symbol, results, current_price)
        return summary

    # ──────────────────────────────────────────────────────
    # تحليل إطار زمني واحد
    # ──────────────────────────────────────────────────────

    def _analyze_timeframe(
        self,
        symbol: str,
        tf: str,
        candles: List[Dict],
        current_price: Optional[float] = None,
    ) -> ICTAnalysisResult:
        result = ICTAnalysisResult(symbol=symbol, timeframe=tf)

        highs = np.array([c["high"] for c in candles], dtype=float)
        lows = np.array([c["low"] for c in candles], dtype=float)
        opens = np.array([c["open"] for c in candles], dtype=float)
        closes = np.array([c["close"] for c in candles], dtype=float)
        timestamps = [c.get("timestamp", "") for c in candles]

        # حساب ATR
        atr = self._calc_atr(highs, lows, closes)

        # 1. هيكل السوق
        structure = self._detect_structure(highs, lows, closes, timestamps, tf)
        result.structure_points = structure

        # 2. BOS/MSS/CHoCH
        bias = self._determine_bias(structure)
        result.bias = bias

        # 3. كتل الأوامر
        obs = self._find_order_blocks(opens, highs, lows, closes, timestamps, tf, atr)
        result.order_blocks = obs

        # 4. فجوات القيمة العادلة
        fvgs = self._find_fvgs(highs, lows, timestamps, tf, atr)
        result.fvgs = fvgs

        # 5. كنس السيولة
        sweeps = self._find_liquidity_sweeps(highs, lows, closes, timestamps, tf)
        result.liquidity_sweeps = sweeps

        # 6. تحديد OB الأفضل وتقييم التوافق
        if current_price is not None:
            top_ob, score = self._grade_best_ob(obs, fvgs, sweeps, structure, current_price, atr)
            result.top_ob = top_ob
            result.confluence_score = score
            result.grade = self._score_to_grade(score)
            result.setup_valid = score >= settings.ICT_GRADE_B_THRESHOLD

        return result

    # ──────────────────────────────────────────────────────
    # كشف هيكل السوق
    # ──────────────────────────────────────────────────────

    def _detect_structure(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        timestamps: List[str],
        tf: str,
    ) -> List[StructurePoint]:
        points: List[StructurePoint] = []
        n = len(highs)
        if n < 5:
            return points

        # تحديد القمم والقيعان
        swing_highs: List[Tuple[int, float]] = []
        swing_lows: List[Tuple[int, float]] = []

        lookback = 3
        for i in range(lookback, n - lookback):
            # قمة
            if all(highs[i] >= highs[i - j] for j in range(1, lookback + 1)) and \
               all(highs[i] >= highs[i + j] for j in range(1, lookback + 1)):
                swing_highs.append((i, highs[i]))

            # قاع
            if all(lows[i] <= lows[i - j] for j in range(1, lookback + 1)) and \
               all(lows[i] <= lows[i + j] for j in range(1, lookback + 1)):
                swing_lows.append((i, lows[i]))

        # BOS - كسر الهيكل
        for idx, (i, level) in enumerate(swing_highs):
            # هل كُسر المستوى بعد تشكله
            for j in range(i + 1, min(i + 20, n)):
                if closes[j] > level:
                    sp = StructurePoint(
                        type="BOS",
                        price=level,
                        timestamp=timestamps[i] if i < len(timestamps) else "",
                        index=i,
                        timeframe=tf,
                        confirmed=True,
                    )
                    points.append(sp)
                    break

        # MSS - تحول هيكل السوق (عكس الاتجاه)
        for idx in range(1, len(swing_lows)):
            prev_idx, prev_low = swing_lows[idx - 1]
            curr_idx, curr_low = swing_lows[idx]
            if curr_idx > prev_idx:
                # هل كُسر القاع السابق
                for j in range(curr_idx + 1, min(curr_idx + 15, n)):
                    if closes[j] < prev_low:
                        sp = StructurePoint(
                            type="MSS",
                            price=prev_low,
                            timestamp=timestamps[curr_idx] if curr_idx < len(timestamps) else "",
                            index=curr_idx,
                            timeframe=tf,
                            confirmed=True,
                        )
                        points.append(sp)
                        break

        return points[-10:]  # آخر 10 نقاط هيكلية

    # ──────────────────────────────────────────────────────
    # تحديد اتجاه السوق
    # ──────────────────────────────────────────────────────

    def _determine_bias(self, structure: List[StructurePoint]) -> str:
        if not structure:
            return "neutral"
        bos_count = sum(1 for sp in structure if sp.type == "BOS")
        mss_count = sum(1 for sp in structure if sp.type == "MSS")
        recent = structure[-3:] if len(structure) >= 3 else structure
        recent_bos = sum(1 for sp in recent if sp.type == "BOS")
        recent_mss = sum(1 for sp in recent if sp.type == "MSS")

        if recent_bos > recent_mss:
            return "bullish"
        if recent_mss > recent_bos:
            return "bearish"
        if bos_count > mss_count:
            return "bullish"
        if mss_count > bos_count:
            return "bearish"
        return "neutral"

    # ──────────────────────────────────────────────────────
    # كشف كتل الأوامر
    # ──────────────────────────────────────────────────────

    def _find_order_blocks(
        self,
        opens: np.ndarray,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        timestamps: List[str],
        tf: str,
        atr: float,
    ) -> List[OrderBlock]:
        obs: List[OrderBlock] = []
        n = len(closes)
        if n < 5:
            return obs

        for i in range(1, n - 2):
            # شمعة displacement (حجمها كبير)
            body = abs(closes[i + 1] - opens[i + 1])
            if body < atr * self.DISPLACEMENT_THRESHOLD:
                continue

            # Order Block صعودي: شمعة هابطة قبل ارتفاع قوي
            if closes[i] < opens[i] and closes[i + 1] > opens[i + 1]:
                ob = OrderBlock(
                    timeframe=tf,
                    direction="bullish",
                    high=highs[i],
                    low=lows[i],
                    open=opens[i],
                    close=closes[i],
                    index=i,
                    timestamp=timestamps[i] if i < len(timestamps) else "",
                    displacement_confirmed=True,
                )
                # هل يوجد BOS بعد الكتلة
                ob.bos_after = any(closes[j] > highs[i] for j in range(i + 2, min(i + 10, n)))
                obs.append(ob)

            # Order Block هابط: شمعة صاعدة قبل هبوط قوي
            elif closes[i] > opens[i] and closes[i + 1] < opens[i + 1]:
                ob = OrderBlock(
                    timeframe=tf,
                    direction="bearish",
                    high=highs[i],
                    low=lows[i],
                    open=opens[i],
                    close=closes[i],
                    index=i,
                    timestamp=timestamps[i] if i < len(timestamps) else "",
                    displacement_confirmed=True,
                )
                ob.bos_after = any(closes[j] < lows[i] for j in range(i + 2, min(i + 10, n)))
                obs.append(ob)

        return obs[-20:]  # آخر 20 OB

    # ──────────────────────────────────────────────────────
    # كشف فجوات القيمة العادلة
    # ──────────────────────────────────────────────────────

    def _find_fvgs(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        timestamps: List[str],
        tf: str,
        atr: float,
    ) -> List[FairValueGap]:
        fvgs: List[FairValueGap] = []
        n = len(highs)
        if n < 3:
            return fvgs

        min_size = atr * self.MIN_FVG_SIZE_ATR

        for i in range(1, n - 1):
            # FVG صعودي: فجوة بين أعلى شمعة (i-1) وأدنى شمعة (i+1)
            if lows[i + 1] > highs[i - 1]:
                size = lows[i + 1] - highs[i - 1]
                if size >= min_size:
                    fvg = FairValueGap(
                        timeframe=tf,
                        direction="bullish",
                        top=lows[i + 1],
                        bottom=highs[i - 1],
                        timestamp=timestamps[i] if i < len(timestamps) else "",
                        index=i,
                    )
                    fvgs.append(fvg)

            # FVG هابط: فجوة بين أدنى شمعة (i-1) وأعلى شمعة (i+1)
            elif highs[i + 1] < lows[i - 1]:
                size = lows[i - 1] - highs[i + 1]
                if size >= min_size:
                    fvg = FairValueGap(
                        timeframe=tf,
                        direction="bearish",
                        top=lows[i - 1],
                        bottom=highs[i + 1],
                        timestamp=timestamps[i] if i < len(timestamps) else "",
                        index=i,
                    )
                    fvgs.append(fvg)

        return fvgs[-15:]

    # ──────────────────────────────────────────────────────
    # كشف كنس السيولة
    # ──────────────────────────────────────────────────────

    def _find_liquidity_sweeps(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        timestamps: List[str],
        tf: str,
    ) -> List[LiquiditySweep]:
        sweeps: List[LiquiditySweep] = []
        n = len(highs)
        if n < 10:
            return sweeps

        # إيجاد مستويات السيولة (قمم وقيعان)
        for i in range(5, n - 2):
            # مسح السيولة فوق القمم
            prev_high = max(highs[max(0, i - 5):i])
            if highs[i] > prev_high and closes[i] < prev_high:
                sweep = LiquiditySweep(
                    timeframe=tf,
                    direction="buy_side",
                    swept_level=prev_high,
                    sweep_high=highs[i],
                    sweep_low=lows[i],
                    timestamp=timestamps[i] if i < len(timestamps) else "",
                    index=i,
                    reversed=closes[i] < prev_high,
                )
                sweeps.append(sweep)

            # مسح السيولة تحت القيعان
            prev_low = min(lows[max(0, i - 5):i])
            if lows[i] < prev_low and closes[i] > prev_low:
                sweep = LiquiditySweep(
                    timeframe=tf,
                    direction="sell_side",
                    swept_level=prev_low,
                    sweep_high=highs[i],
                    sweep_low=lows[i],
                    timestamp=timestamps[i] if i < len(timestamps) else "",
                    index=i,
                    reversed=closes[i] > prev_low,
                )
                sweeps.append(sweep)

        return sweeps[-10:]

    # ──────────────────────────────────────────────────────
    # تقييم أفضل OB وحساب درجة التوافق
    # ──────────────────────────────────────────────────────

    def _grade_best_ob(
        self,
        obs: List[OrderBlock],
        fvgs: List[FairValueGap],
        sweeps: List[LiquiditySweep],
        structure: List[StructurePoint],
        current_price: float,
        atr: float,
    ) -> Tuple[Optional[OrderBlock], float]:
        best_ob = None
        best_score = 0.0

        # إيجاد OBs القريبة من السعر الحالي
        nearby_obs = [
            ob for ob in obs
            if ob.is_valid(current_price) and abs(ob.midpoint() - current_price) <= atr * 5
        ]

        for ob in nearby_obs:
            score = self._calc_confluence(ob, fvgs, sweeps, structure, current_price, atr)
            ob.confluence_score = score
            ob.grade = self._score_to_grade(score)
            if score > best_score:
                best_score = score
                best_ob = ob

        return best_ob, best_score

    def _calc_confluence(
        self,
        ob: OrderBlock,
        fvgs: List[FairValueGap],
        sweeps: List[LiquiditySweep],
        structure: List[StructurePoint],
        current_price: float,
        atr: float,
    ) -> float:
        """حساب درجة التوافق (0-100)"""
        score = 40.0  # نقطة البداية

        # +15: Displacement مؤكدة
        if ob.displacement_confirmed:
            score += 15

        # +15: BOS بعد الكتلة
        if ob.bos_after:
            score += 15

        # +10: FVG داخل نطاق الكتلة
        ob_mid = ob.midpoint()
        for fvg in fvgs:
            if fvg.direction == ob.direction:
                if ob.low <= fvg.bottom <= ob.high or ob.low <= fvg.top <= ob.high:
                    score += 10
                    ob.fvg_present = True
                    break

        # +10: كنس السيولة قريب (يشير لعكس قريب)
        for sweep in sweeps:
            if sweep.reversed and abs(sweep.swept_level - ob.midpoint()) <= atr * 3:
                score += 10
                break

        # +5: نقطة هيكل قريبة
        for sp in structure[-3:]:
            if abs(sp.price - ob.midpoint()) <= atr * 2:
                score += 5
                break

        # +5: الكتلة على مقربة من السعر (1-3 ATR)
        dist = abs(ob.midpoint() - current_price)
        if atr <= dist <= atr * 3:
            score += 5

        return min(score, 100.0)

    # ──────────────────────────────────────────────────────
    # تحويل الدرجة إلى تصنيف
    # ──────────────────────────────────────────────────────

    def _score_to_grade(self, score: float) -> str:
        if score >= settings.ICT_GRADE_A_PLUS_THRESHOLD:
            return "A+"
        if score >= settings.ICT_GRADE_A_THRESHOLD:
            return "A"
        if score >= settings.ICT_GRADE_B_THRESHOLD:
            return "B"
        return "C"

    # ──────────────────────────────────────────────────────
    # حساب ATR
    # ──────────────────────────────────────────────────────

    def _calc_atr(
        self,
        highs: np.ndarray,
        lows: np.ndarray,
        closes: np.ndarray,
        period: int = 14,
    ) -> float:
        n = len(highs)
        if n < 2:
            return float(highs[0] - lows[0]) if n == 1 else 1.0
        trs = []
        for i in range(1, n):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            trs.append(tr)
        trs_arr = np.array(trs)
        if len(trs_arr) >= period:
            return float(np.mean(trs_arr[-period:]))
        return float(np.mean(trs_arr)) if len(trs_arr) > 0 else 1.0

    # ──────────────────────────────────────────────────────
    # بناء الملخص النهائي
    # ──────────────────────────────────────────────────────

    def _build_summary(
        self,
        symbol: str,
        results: Dict[str, ICTAnalysisResult],
        current_price: Optional[float],
    ) -> Dict[str, Any]:
        if not results:
            return {
                "symbol": symbol,
                "bias": "neutral",
                "grade": "C",
                "confluence_score": 0.0,
                "setup_valid": False,
                "timeframes": {},
                "top_ob": None,
                "fvg_count": 0,
                "sweep_count": 0,
            }

        # تحديد الاتجاه العام من الإطارات الأعلى
        bias_votes = {"bullish": 0, "bearish": 0, "neutral": 0}
        for tf in ["1W", "1D", "4H"]:
            if tf in results:
                bias_votes[results[tf].bias] += 2  # وزن أعلى للإطارات الكبيرة
        for tf in ["1H", "15M", "5M"]:
            if tf in results:
                bias_votes[results[tf].bias] += 1

        overall_bias = max(bias_votes, key=bias_votes.get)

        # أعلى درجة OB عبر الإطارات
        best_score = 0.0
        best_ob_dict = None
        best_grade = "C"
        for tf, result in results.items():
            if result.confluence_score > best_score:
                best_score = result.confluence_score
                best_grade = result.grade
                if result.top_ob:
                    ob = result.top_ob
                    best_ob_dict = {
                        "timeframe": ob.timeframe,
                        "direction": ob.direction,
                        "high": ob.high,
                        "low": ob.low,
                        "grade": ob.grade,
                        "score": ob.confluence_score,
                        "fvg_present": ob.fvg_present,
                        "displacement": ob.displacement_confirmed,
                        "bos_after": ob.bos_after,
                    }

        total_fvgs = sum(len(r.fvgs) for r in results.values())
        total_sweeps = sum(len(r.liquidity_sweeps) for r in results.values())

        return {
            "symbol": symbol,
            "bias": overall_bias,
            "grade": best_grade,
            "confluence_score": round(best_score, 1),
            "setup_valid": best_score >= settings.ICT_GRADE_B_THRESHOLD,
            "timeframes": {
                tf: {
                    "bias": r.bias,
                    "grade": r.grade,
                    "score": round(r.confluence_score, 1),
                    "ob_count": len(r.order_blocks),
                    "fvg_count": len(r.fvgs),
                }
                for tf, r in results.items()
            },
            "top_ob": best_ob_dict,
            "fvg_count": total_fvgs,
            "sweep_count": total_sweeps,
            "current_price": current_price,
        }
