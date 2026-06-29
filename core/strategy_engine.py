"""
Strategy Engine v2 - Scoring System
محرك اختيار الاستراتيجية بنظام التسجيل
"""
from __future__ import annotations
from typing import Dict, Any, Optional, List
from enum import Enum


# ── Constants ─────────────────────────────────────────────────────────────────
MIN_CREDIT   = 0.10
WING_DEFAULT = 5

# ── Credit/Width Quality Filter — 0DTE ────────────────────────────────────────
# نسبة الكريدت إلى عرض الجناح — HARD REJECT إذا أقل من الحد
# المبرر: Score يقيس الاتجاه والاحتمال لكن لا يعالج ضعف العائد بعد العمولة والانزلاق
# مثال: wing=5, MIN=12% → يجب credit >= 0.60 كحد أدنى
# credit=0.24, wing=5 → $12 ربح مقابل $476 خسارة → لا تستحق
MIN_CREDIT_WIDTH_0DTE = 0.25   # 25% — 0DTE Bull Put / Bear Call
MIN_CREDIT_WIDTH_IC   = 0.25   # 25% — 0DTE Iron Condor total credit / wing width
MIN_CREDIT_WIDTH_SWING = 0.20  # 20% — Swing Credit Spread total credit / wing width

# ── Liquidity Thresholds ───────────────────────────────────────────────────────
# الحد الأقصى لـ Bid/Ask Spread % لقبول الضربة في الاختيار الأولي
LIQ_SPREAD_HARD_REJECT = 60   # % — رفض كامل في pre-filter (ضربة مرفوضة نهائياً)
LIQ_SPREAD_WARN        = 30   # % — تحذير وخصم في الـ score النهائي
LIQ_MIN_VOLUME         = 0    # عقود — 0 = لا فلتر حجم (يمكن رفعه لـ 5 أو 10)
LIQ_MIN_OI             = 0    # عقود — 0 = لا فلتر OI  (يمكن رفعه لـ 50 أو 100)

# ── Delta المستهدفة ────────────────────────────────────────────────────────────
TARGET_DELTA = {
    "ic_short":     0.12,   # IC short legs — بعيد عن ATM
    "credit_short": 0.15,   # Bull Put / Bear Call short — حدود EM
    "debit_long":   0.48,   # Debit long — قريب ATM (delta ~0.5)
}

# ملاحظة: CREDIT_MAX_DIST_PCT و MIN_DIST_PCT لم تعد مستخدمة
# اختيار الـ strike الآن يعتمد على 1.5σ مباشرة بدون حدود % ثابتة


def _get_wing(symbol: str = "") -> int:
    """يقرأ عرض الجناح من الإعدادات حسب الرمز."""
    try:
        from core.database import get_setting
        sym = symbol.upper().strip()
        if sym == "SPX":
            key = "wing_width_spx"
        elif sym == "SPY":
            key = "wing_width_spy"
        elif sym == "QQQ":
            key = "wing_width_qqq"
        else:
            key = "wing_width"
        v = get_setting(key, "") or get_setting("wing_width", str(WING_DEFAULT))
        return max(int(v), 1)
    except Exception:
        return WING_DEFAULT

# وضعان: conservative (افتراضي) و test (لجمع بيانات أسرع)
THRESHOLDS = {
    "conservative": {
        "Iron Condor":       65,
        "Bull Put Spread":   60,
        "Bear Call Spread":  60,
        "Call Debit Spread": 50,
        "Put Debit Spread":  50,
        "_default":          60,
    },
    "test": {
        "Iron Condor":       55,
        "Bull Put Spread":   50,
        "Bear Call Spread":  50,
        "Call Debit Spread": 40,
        "Put Debit Spread":  45,
        "_default":          50,
    },
}

_active_mode = "conservative"   # يُغيَّر من الإعدادات


def set_threshold_mode(mode: str) -> None:
    global _active_mode
    _active_mode = mode if mode in THRESHOLDS else "conservative"


def get_threshold_mode() -> str:
    return _active_mode


def get_strategy_min_score(strategy_name: str = "") -> int:
    t = THRESHOLDS.get(_active_mode, THRESHOLDS["conservative"])
    return t.get(strategy_name, t["_default"])


# للتوافق مع الكود القديم الذي يستورد STRATEGY_MIN_SCORE مباشرة
STRATEGY_MIN_SCORE = THRESHOLDS["conservative"]
MIN_SCORE_TO_TRADE = 60


class MarketRegime(str, Enum):
    MEAN_REVERSION = "Mean Reversion"
    TRENDING       = "Trending"
    MIXED          = "Mixed"
    NO_TRADE       = "No Trade"


# ── Strategy Engine ───────────────────────────────────────────────────────────

class StrategyEngine:

    def __init__(self, analysis: Dict[str, Any]):
        self.analysis = analysis
        self.price    = analysis.get("price", 0)
        self.levels   = analysis.get("levels", {})
        self.chain    = analysis.get("_chain")
        self.dq       = analysis.get("data_quality", {})
        self.symbol   = analysis.get("symbol", "SPX").upper()

        # Market inputs
        self.pin_score      = analysis.get("pin_score", 0)
        self.net_gex        = self.levels.get("net_gex") or 0
        # IV Rank + IV Percentile
        _iv_raw             = self.levels.get("iv_rank")
        self.iv_rank        = _iv_raw if (_iv_raw is not None and _iv_raw > 0) else 30
        self.iv_rank_is_estimated = (_iv_raw is None or _iv_raw == 0)
        self.iv_percentile  = self.levels.get("iv_percentile")   # None = لا بيانات
        self.iv_regime      = self.levels.get("iv_regime", "Unknown")
        self.iv_regime_data = self.levels.get("iv_regime_data") or {}
        self.vix            = analysis.get("vix") or self.levels.get("vix") or 0
        self.zero_gamma     = self.levels.get("zero_gamma")
        self.ema20          = self.levels.get("ema20")
        self.ema50          = self.levels.get("ema50")
        self.daily_trend    = self.levels.get("daily_trend") or self.levels.get("trend") or "neutral"
        self.ema20_15m      = self.levels.get("ema20_15m")
        self.ema50_15m      = self.levels.get("ema50_15m")
        self.intraday_trend = self.levels.get("intraday_trend") or "neutral"
        self.combined_trend = self.levels.get("combined_trend") or self.levels.get("trend") or "neutral"
        # EM: الانحراف المعياري اليومي الحقيقي — يُستخدم مباشرة للـ sigma
        # السقف 1.5% يمنع قيم غير واقعية فقط (مثلاً أيام الـ flash crash)
        _em_raw = self.levels.get("expected_move") or abs(self.price * 0.004)
        _em_cap = self.price * 0.015   # 1.5% سقف (بدل 0.8%) — sigma حقيقي
        self.em = min(_em_raw, _em_cap)
        self.market_open    = analysis.get("is_market_open", True)
        self.net_vanna      = self.levels.get("net_vanna") or 0
        self.net_charm      = self.levels.get("net_charm") or 0

        # Data quality flags
        self.has_delta   = self.dq.get("has_delta", False)
        self.has_gamma   = self.dq.get("has_gamma", False)
        self.has_prices  = self.dq.get("has_prices", False)
        self.gex_quality = self.dq.get("gex_quality", "gamma_proxy")  # real_oi / volume_proxy / gamma_proxy

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        """الدالة الرئيسية — تعيد أفضل استراتيجية أو No Trade"""

        # 1. فحص البيانات — فقط الأخطاء القاطعة تمنع التحليل
        hard_blocks = self._hard_blocks()
        if hard_blocks:
            return self._no_trade(hard_blocks)

        # 2. تحذيرات ناعمة تُضاف للنتيجة لكن لا توقف الحساب
        soft_warnings = self._soft_warnings()

        # 3. تصنيف السوق
        regime = self.classify_market()

        # 4. تسجيل كل الاستراتيجيات
        candidates = [
            self.scan_iron_condor(),
            self.scan_bull_put_spread(),
            self.scan_bear_call_spread(),
            self.scan_call_debit_spread(),
            self.scan_put_debit_spread(),
        ]

        # أضف التحذيرات الناعمة لكل مرشح
        if soft_warnings:
            for c in candidates:
                if c:
                    c.setdefault("warnings", []).extend(soft_warnings)

        # ── RC15j Phase 2C — Put Debit candidate diagnostics ─────────────────────
        # نستخرج نتيجة Put Debit بغض النظر عن أفضل مرشح، ونضيفها للنتيجة النهائية
        _pd_cand = next((c for c in candidates if c and c.get("strategy") == "Put Debit Spread"), {})
        _pd_diag = {
            "put_debit_score":            _pd_cand.get("score", 0),
            "put_debit_blocked_code":     _pd_cand.get("blocked_code", ""),
            "put_debit_pipeline_stop":    _pd_cand.get("pipeline_stop", ""),
            "put_debit_ds_location":      _pd_cand.get("ds_location_decision", ""),
            "put_debit_reject_reason":    ((_pd_cand.get("reasons") or [""])[0])[:120],
        }

        # 5. فلترة الاستراتيجيات بحد أدنى مختلف حسب النوع
        valid = [
            c for c in candidates
            if c and not c.get("no_trade")
            and c.get("score", 0) >= get_strategy_min_score(c.get("strategy", ""))
        ]

        if not valid:
            all_scores = [(c.get("strategy","?"), c.get("score",0)) for c in candidates if c]
            best = max(all_scores, key=lambda x: x[1], default=("?", 0))
            best_name = best[0]
            best_threshold = get_strategy_min_score(best_name)
            cw_rejects = [c for c in candidates if c and c.get("no_trade") and not c.get("credit_width_ok", True)]
            delta_rejects = [c for c in candidates if c and c.get("no_trade") and any("delta" in str(w).lower() for w in (c.get("warnings") or []) + (c.get("reasons") or []))]
            if cw_rejects:
                print(f"[CW_REJECT] no_valid_candidates | strategies={[c.get('strategy') for c in cw_rejects]}")
            if delta_rejects:
                print(f"[DELTA_REJECT] no_valid_candidates | strategies={[c.get('strategy') for c in delta_rejects]}")
            reasons = [
                f"لا توجد استراتيجية تتجاوز الحد الأدنى",
                f"أعلى درجة: {best_name} = {best[1]}/100  (حد: {best_threshold})",
            ] + soft_warnings
            if cw_rejects:
                reasons.append("Credit/Width rejects: " + ", ".join(c.get("strategy","?") for c in cw_rejects))
            if delta_rejects:
                reasons.append("Delta rejects: " + ", ".join(c.get("strategy","?") for c in delta_rejects))
            no_trade_result = self._no_trade(reasons, regime=regime,
                                  all_scores={c["strategy"]: c["score"] for c in candidates if c})
            no_trade_result.update(_pd_diag)
            return no_trade_result

        # 6. اختيار الأعلى درجة
        best = max(valid, key=lambda x: x.get("score", 0))
        best["regime"] = regime.value
        best["all_scores"] = {c["strategy"]: c["score"] for c in candidates if c}
        best.update(_pd_diag)
        return best

    def classify_market(self) -> MarketRegime:
        """المرحلة 1 — تحديد بيئة السوق"""
        is_ranging = (
            self.pin_score >= 65 and
            self.net_gex >= 0 and
            self._near_zero_gamma()
        )
        is_trending = (
            self.pin_score < 50 and
            self.net_gex < 0 and
            self._trend() in ("bullish", "bearish")
        )
        if is_ranging:
            return MarketRegime.MEAN_REVERSION
        if is_trending:
            return MarketRegime.TRENDING
        return MarketRegime.MIXED

    # ── Scanners ──────────────────────────────────────────────────────────────

    def scan_iron_condor(self) -> Dict[str, Any]:
        """Iron Condor — بيئة عكسية، IV مرتفع، Pin عالٍ"""
        score = 0
        reasons = []
        warnings = []

        pin_c  = min(self.pin_score / 100 * 35, 35)
        iv_c   = min(self.iv_rank  / 100 * 30, 30)
        gex_c  = 25 if self.net_gex > 50_000 else (15 if self.net_gex >= 0 else 0)
        score  = round(pin_c + iv_c + gex_c)
        breakdown = {"Pin": round(pin_c), "IV Rank": round(iv_c), "GEX": round(gex_c)}

        # VIX filter للـ Iron Condor
        if self.vix > 25:
            warnings.append(f"VIX مرتفع ({self.vix:.1f}) — توسيع الأجنحة أو تجنب Condor")
            score = max(score - 15, 0)
        elif self.vix > 20:
            warnings.append(f"VIX متوسط ({self.vix:.1f}) — كن حذراً")
            score = max(score - 5, 0)

        if self.pin_score >= 65:
            reasons.append(f"Pin Score {self.pin_score:.0f}/100 — السوق عكسي")
        elif self.pin_score >= 50:
            warnings.append(f"Pin Score متوسط ({self.pin_score:.0f}) — راقب الاختراق")
            score = max(score - 10, 0)
        else:
            warnings.append(f"Pin Score منخفض ({self.pin_score:.0f}) — خطر اختراق الجناح")
            score = max(score - 20, 0)  # Iron Condor يحتاج pin عالٍ

        if self.iv_rank_is_estimated:
            warnings.append("IV Rank مقدّر (30/100) — لا يوجد سجل تاريخي كافٍ بعد")
        elif self.iv_rank >= 40:
            reasons.append(f"IV Rank {self.iv_rank:.0f} — Premium مرتفع")
        elif self.iv_rank >= 25:
            reasons.append(f"IV Rank {self.iv_rank:.0f} — Premium متوسط")
        else:
            warnings.append(f"IV Rank منخفض ({self.iv_rank:.0f})")

        if self.net_gex > 0:
            reasons.append("GEX إيجابي — صانعو السوق يكبحون الحركة")

        # Credit: Short Strikes عند حدود EM (بعيد عن ATM)
        wing = _get_wing(self.symbol)
        sc = self._ic_short_strike("call")
        sp = self._ic_short_strike("put")

        # RC15i.3: Iron Condor short legs must obey the same Credit Delta guard
        # used by Bull Put / Bear Call. This is a risk-consistency fix only.
        if not self._check_credit_delta("call", sc, warnings) or not self._check_credit_delta("put", sp, warnings):
            return {
                "strategy": "Iron Condor", "emoji": "🦅", "score": 0,
                "no_trade": True,
                "decision": "❌ مرفوض — Short leg delta خارج النطاق المقبول",
                "reasons": ["Delta خارج النطاق المقبول للـ Credit short legs (0.05-0.20)"],
                "warnings": warnings,
                "short_call": sc,
                "short_put": sp,
                "credit_width_ok": False,
                "delta_guard_reject": True,
            }

        lc = (sc + wing) if sc else None
        lp = (sp - wing) if sp else None

        sc_d = self._opt("call", sc)
        sp_d = self._opt("put",  sp)
        lc_d = self._opt("call", lc)
        lp_d = self._opt("put",  lp)

        all_legs = [sc_d, sp_d, lc_d, lp_d]
        credit        = self._credit([sc_d, sp_d], [lc_d, lp_d])
        credit_worst  = self._credit_worst_case([sc_d, sp_d], [lc_d, lp_d])
        put_wing_used  = abs((sp or 0) - (lp or 0)) if sp and lp else _get_wing(self.symbol)
        call_wing_used = abs((lc or 0) - (sc or 0)) if sc and lc else _get_wing(self.symbol)
        wing_used      = max(float(put_wing_used or 0), float(call_wing_used or 0), float(_get_wing(self.symbol) or 0)) or 1
        max_loss      = round(wing_used - max(credit or 0, 0), 2) if credit else None
        credit_width_ratio = round((credit or 0) / wing_used, 3) if wing_used else 0

        credit_bonus = 0
        if credit and credit >= MIN_CREDIT:
            credit_bonus = 10
            score = min(score + credit_bonus, 100)
            reasons.append(f"Credit mid≈ {credit:.2f}  |  worst-case≈ {credit_worst:.2f}" if credit_worst else f"Credit مناسب ({credit:.2f})")
        elif credit and credit < MIN_CREDIT:
            warnings.append(f"Credit منخفض جداً ({credit:.2f})")
            credit_bonus = -20
            score = max(score + credit_bonus, 0)
        elif credit is None or credit <= 0:
            warnings.append("Credit = 0 أو سلبي — لا تنفذ")
            credit_bonus = -30
            score = max(score + credit_bonus, 0)

        spread_warns = self._spread_warnings(all_legs)
        score = self._apply_spread_penalty(score, spread_warns, breakdown)
        warnings.extend(self._clean_spread_warns(spread_warns))

        breakdown["Credit"] = credit_bonus

        # ── Credit/Width Hard Reject ──────────────────────────────────────────
        cw_rejected, cw_reason = self._credit_width_check(
            credit, wing_used, MIN_CREDIT_WIDTH_IC, "Iron Condor")
        if cw_rejected:
            return {
                "strategy": "Iron Condor", "emoji": "🦅", "score": 0,
                "no_trade": True,
                "decision": "❌ مرفوض — Poor credit/width after commission risk",
                "reasons":  [], "warnings": [cw_reason],
                "credit": credit, "max_loss": max_loss,
                "width": wing_used,
                "put_wing_width": put_wing_used,
                "call_wing_width": call_wing_used,
                "credit_width_ratio": credit_width_ratio,
                "min_credit_width_ratio": MIN_CREDIT_WIDTH_IC,
                "credit_width_ok": False,
            }

        pop = self._pop(sp_d.get("delta"), sc_d.get("delta"))

        rr = self._calc_rr(credit, max_loss, is_credit=True)
        return {
            "strategy":      "Iron Condor",
            "emoji":         "🦅",
            "score":         score,
            "score_breakdown": breakdown,
            "short_call":    sc,  "long_call": lc,
            "short_put":     sp,  "long_put":  lp,
            "credit":        credit,
            "credit_worst":  credit_worst,
            "max_loss":      max_loss,
            "width":         wing_used,
            "put_wing_width": put_wing_used,
            "call_wing_width": call_wing_used,
            "credit_width_ratio": credit_width_ratio,
            "min_credit_width_ratio": MIN_CREDIT_WIDTH_IC,
            "credit_width_ok": credit_width_ratio >= MIN_CREDIT_WIDTH_IC,
            "reward_risk":   rr,
            "setup_quality": self._quality_label(rr, is_credit=True),
            "pop":           pop,
            "legs_detail":   self._legs_detail(all_legs),
            "reasons":       reasons,
            "warnings":      warnings,
            "no_trade":      False,
            "decision":      self._decision(score, "Iron Condor"),
        }

    def scan_bull_put_spread(self) -> Dict[str, Any]:
        """Bull Put Spread — اتجاه صاعد، GEX+، IV متوسط/مرتفع"""
        score = 0
        reasons = []
        warnings = []
        trend = self._trend()

        bull = {"strong_bullish", "bullish", "slightly_bullish"}
        bear = {"strong_bearish", "bearish", "slightly_bearish", "bearish_bounce"}
        if trend in bull:
            trend_c = self._trend_strength() * (35/40)
        elif trend == "bullish_pullback":
            trend_c = 8 * (35/40)
        elif trend in bear:
            trend_c = -15  # عقوبة صريحة — اتجاه معاكس لـ Bull Put
        else:
            trend_c = 0

        gex_c   = self._gex_score("bullish")
        iv_c    = min(self.iv_rank / 100 * 25, 25)
        dist_c  = self._distance_score(direction="up")
        market_score = round(trend_c + gex_c + iv_c + dist_c)
        score        = max(market_score, 0)
        breakdown = {"Trend": round(trend_c), "GEX": round(gex_c), "IV Rank": round(iv_c), "Distance": round(dist_c)}
        if self.gex_quality == "gamma_proxy": breakdown["GEX_note"] = "proxy"

        if trend in ("bullish", "slightly_bullish", "strong_bullish"):
            reasons.append(f"Trend صاعد ({trend})")
        elif trend == "bullish_pullback":
            reasons.append(f"Daily صاعد — 15m يتراجع مؤقتاً")
            warnings.append("Daily صاعد لكن 15m هابط — دخول Bull Put يحتاج تأكيد")
        elif trend in bear:
            warnings.append(f"⛔ Trend هابط ({trend}) — Bull Put يعاكس الاتجاه (-15)")
        else:
            warnings.append(f"Trend محايد — Bull Put يحتاج تأكيد اتجاه")

        if self.net_gex > 0:
            reasons.append("GEX إيجابي — دعم صاعد")

        # Vanna/Charm signal
        vc = self._vanna_charm_signal()
        if vc["direction"] == "bullish":
            score = min(score + 3, 100)
            reasons.extend(vc["notes"])
        elif vc["direction"] == "bearish":
            warnings.append("Vanna/Charm ضغط هبوطي على Bull Put")

        if self.iv_rank >= 25:
            reasons.append(f"IV Rank {self.iv_rank:.0f} — Premium مناسب للبيع")

        # Pin Score ليس عاملاً أساسياً في Bull Put — فقط تحذير
        if self.pin_score < 40:
            warnings.append(f"Pin Score منخفض ({self.pin_score:.0f}) — السوق متحرك، ابتعد عن الـ Short Put")

        # Credit: Short Put عند 1.5σ تحت السعر + فلتر Delta
        wing = _get_wing(self.symbol)
        sp = self._credit_short_strike("put")
        if not self._check_credit_delta("put", sp, warnings):
            return {"no_trade": True, "strategy": "Bull Put Spread", "score": 0,
                    "warnings": warnings, "reasons": ["Delta خارج النطاق (0.05-0.20)"]}
        lp = (sp - wing) if sp else None
        sp_d = self._opt("put", sp)
        lp_d = self._opt("put", lp)
        legs = [sp_d, lp_d]
        credit       = self._credit([sp_d], [lp_d])
        credit_worst = self._credit_worst_case([sp_d], [lp_d])
        wing_used     = _get_wing(self.symbol)
        max_loss     = round(wing_used - max(credit or 0, 0), 2) if credit else None
        credit_width_ratio = round((credit or 0) / wing_used, 3) if wing_used else 0

        credit_bonus = 0
        if credit and credit >= MIN_CREDIT:
            credit_bonus = 10
            score = min(score + credit_bonus, 100)
            reasons.append(f"Credit mid≈ {credit:.2f}  |  worst-case≈ {credit_worst:.2f}" if credit_worst else f"Credit مقبول ({credit:.2f})")
        elif credit is None:
            # لا أسعار من DXLink — خصم أقل لأن الثقة بالاتجاه كافية للدراسة
            credit_bonus = -15
            score = max(score + credit_bonus, 0)
            warnings.append("Credit غير متاح من DXLink — تحقق من السعر الحي")
        elif credit <= 0:
            credit_bonus = -25
            score = max(score + credit_bonus, 0)
            warnings.append("Credit = 0 أو سلبي")

        spread_warns = self._spread_warnings(legs)
        score = self._apply_spread_penalty(score, spread_warns, breakdown)
        warnings.extend(self._clean_spread_warns(spread_warns))

        breakdown["Credit"] = credit_bonus

        # ── Credit/Width Hard Reject ──────────────────────────────────────────
        cw_rejected, cw_reason = self._credit_width_check(
            credit, _get_wing(self.symbol), MIN_CREDIT_WIDTH_0DTE, "Bull Put Spread")
        if cw_rejected:
            return {
                "strategy": "Bull Put Spread", "emoji": "🟢", "score": 0,
                "no_trade": True,
                "decision": "❌ مرفوض — Poor credit/width after commission risk",
                "reasons":  [], "warnings": [cw_reason],
                "credit": credit, "max_loss": max_loss,
                "credit_width_ratio": credit_width_ratio,
                "min_credit_width_ratio": MIN_CREDIT_WIDTH_0DTE,
                "credit_width_ok": False,
            }

        pop = self._pop(sp_d.get("delta"), None)

        rr = self._calc_rr(credit, max_loss, is_credit=True)
        return {
            "strategy":      "Bull Put Spread",
            "market_score":  market_score,
            "score_breakdown": breakdown,
            "emoji":        "🟢",
            "score":        score,
            "short_put":    sp, "long_put": lp,
            "credit":       credit, "credit_worst": credit_worst,
            "credit_width_ratio": credit_width_ratio,
            "min_credit_width_ratio": MIN_CREDIT_WIDTH_0DTE,
            "credit_width_ok": credit_width_ratio >= MIN_CREDIT_WIDTH_0DTE,
            "max_loss":     max_loss, "reward_risk": rr,
            "setup_quality": self._quality_label(rr, is_credit=True),
            "pop": pop,
            "legs_detail":  self._legs_detail(legs),
            "reasons":      reasons, "warnings": warnings,
            "no_trade":     False,
            "decision":     self._decision(score, "Bull Put Spread"),
        }

    def scan_bear_call_spread(self) -> Dict[str, Any]:
        """Bear Call Spread — اتجاه هابط، GEX+/محايد، IV متوسط/مرتفع"""
        score = 0
        reasons = []
        warnings = []
        trend = self._trend()

        bear = {"strong_bearish", "bearish", "slightly_bearish"}
        bull = {"strong_bullish", "bullish", "slightly_bullish", "bullish_pullback"}
        if trend in bear:
            trend_c = self._trend_strength() * (35/40)
        elif trend == "bearish_bounce":
            trend_c = 8 * (35/40)
        elif trend in bull:
            trend_c = -15  # عقوبة صريحة — اتجاه معاكس لـ Bear Call
        else:
            trend_c = 0

        gex_c   = self._gex_score("bearish")
        iv_c    = min(self.iv_rank / 100 * 25, 25)
        dist_c  = self._distance_score(direction="down")
        score   = max(round(trend_c + gex_c + iv_c + dist_c), 0)
        breakdown = {"Trend": round(trend_c), "GEX": round(gex_c), "IV Rank": round(iv_c), "Distance": round(dist_c)}
        if self.gex_quality == "gamma_proxy": breakdown["GEX_note"] = "proxy"

        if trend in ("bearish", "slightly_bearish", "strong_bearish"):
            reasons.append(f"Trend هابط ({trend})")
        elif trend == "bearish_bounce":
            reasons.append(f"Daily هابط — 15m يرتد مؤقتاً")
            warnings.append("Daily هابط لكن 15m صاعد — دخول Bear Call يحتاج تأكيد")
        elif trend in bull:
            warnings.append(f"⛔ Trend صاعد ({trend}) — Bear Call يعاكس الاتجاه (-15)")
        else:
            warnings.append(f"Trend محايد — Bear Call يحتاج تأكيد اتجاه")

        if self.net_gex >= 0:
            reasons.append("GEX محايد/إيجابي")
        if self.iv_rank >= 25:
            reasons.append(f"IV Rank {self.iv_rank:.0f} — Premium مناسب")
        if self.pin_score < 40:
            warnings.append(f"Pin Score منخفض ({self.pin_score:.0f}) — ابتعد عن الـ Short Call")

        # Vanna/Charm signal
        vc = self._vanna_charm_signal()
        if vc["direction"] == "bearish":
            score = min(score + 3, 100)
            reasons.extend(vc["notes"])
        elif vc["direction"] == "bullish":
            warnings.append("Vanna/Charm ضغط صعودي يعاكس Bear Call")

        # Credit: Short Call عند 1.5σ فوق السعر + فلتر Delta
        wing = _get_wing(self.symbol)
        sc = self._credit_short_strike("call")
        if not self._check_credit_delta("call", sc, warnings):
            return {"no_trade": True, "strategy": "Bear Call Spread", "score": 0,
                    "warnings": warnings, "reasons": ["Delta خارج النطاق (0.05-0.20)"]}
        lc = (sc + wing) if sc else None
        sc_d = self._opt("call", sc)
        lc_d = self._opt("call", lc)
        legs = [sc_d, lc_d]
        credit       = self._credit([sc_d], [lc_d])
        credit_worst = self._credit_worst_case([sc_d], [lc_d])
        wing_used     = _get_wing(self.symbol)
        max_loss     = round(wing_used - max(credit or 0, 0), 2) if credit else None
        credit_width_ratio = round((credit or 0) / wing_used, 3) if wing_used else 0

        credit_bonus = 0
        if credit and credit >= MIN_CREDIT:
            credit_bonus = 10
            score = min(score + credit_bonus, 100)
            reasons.append(f"Credit mid≈ {credit:.2f}  |  worst-case≈ {credit_worst:.2f}" if credit_worst else f"Credit مقبول ({credit:.2f})")
        elif credit is None:
            credit_bonus = -15
            score = max(score + credit_bonus, 0)
            warnings.append("Credit غير متاح من DXLink — تحقق من السعر الحي")
        elif credit <= 0:
            credit_bonus = -25
            score = max(score + credit_bonus, 0)
            warnings.append("Credit = 0 أو سلبي")

        spread_warns = self._spread_warnings(legs)
        score = self._apply_spread_penalty(score, spread_warns, breakdown)
        warnings.extend(self._clean_spread_warns(spread_warns))

        breakdown["Credit"] = credit_bonus

        # ── Credit/Width Hard Reject ──────────────────────────────────────────
        cw_rejected, cw_reason = self._credit_width_check(
            credit, _get_wing(self.symbol), MIN_CREDIT_WIDTH_0DTE, "Bear Call Spread")
        if cw_rejected:
            return {
                "strategy": "Bear Call Spread", "emoji": "🔴", "score": 0,
                "no_trade": True,
                "decision": "❌ مرفوض — Poor credit/width after commission risk",
                "reasons":  [], "warnings": [cw_reason],
                "credit": credit, "max_loss": max_loss,
                "credit_width_ratio": credit_width_ratio,
                "min_credit_width_ratio": MIN_CREDIT_WIDTH_0DTE,
                "credit_width_ok": False,
            }

        pop = self._pop(None, sc_d.get("delta"))

        rr = self._calc_rr(credit, max_loss, is_credit=True)
        return {
            "strategy":     "Bear Call Spread",
            "score_breakdown": breakdown,
            "emoji":        "🔴",
            "score":        score,
            "short_call":   sc, "long_call": lc,
            "credit":       credit, "credit_worst": credit_worst,
            "credit_width_ratio": credit_width_ratio,
            "min_credit_width_ratio": MIN_CREDIT_WIDTH_0DTE,
            "credit_width_ok": credit_width_ratio >= MIN_CREDIT_WIDTH_0DTE,
            "max_loss":     max_loss, "reward_risk": rr,
            "setup_quality": self._quality_label(rr, is_credit=True),
            "pop": pop,
            "legs_detail":  self._legs_detail(legs),
            "reasons":      reasons, "warnings": warnings,
            "no_trade":     False,
            "decision":     self._decision(score, "Bear Call Spread"),
        }

    def scan_call_debit_spread(self) -> Dict[str, Any]:
        """Call Debit Spread — صاعد + GEX سلبي + IV منخفض"""
        score = 0
        reasons = []
        warnings = []
        trend = self._trend()

        # ── RC15j Phase 2B — SPY Proxy D/S Hard Block for Call Debit ────────────
        # يحظر Call Debit إذا كان SPX قرب منطقة Supply (مستوحى من SPY proxy).
        _ds2b_ctx_c   = self.levels.get("_ds_proxy_context") or {}
        _ds2b_avail_c = _ds2b_ctx_c.get("available", False)
        if _ds2b_avail_c:
            _ds2b_ltf_c = _ds2b_ctx_c.get("ltf", {})
            _ds2b_htf_c = _ds2b_ctx_c.get("htf", {})
            _ds2b_ns_c  = (_ds2b_ltf_c.get("nearest_supply") or _ds2b_ltf_c.get("nearest_bearish_order_block") or
                           _ds2b_htf_c.get("nearest_supply") or _ds2b_htf_c.get("nearest_bearish_order_block") or {})
            _ds2b_thr_c = self.levels.get("_ds_near_threshold") or max(5.0, (self.price or 5000) * 0.0015)
            _ds2b_ns_lo = _ds2b_ns_c.get("low")
            _ds2b_ns_hi = _ds2b_ns_c.get("high")
            _spx_px_c   = self.price or 0
            # حظر إذا السعر داخل Supply أو أقل من near_threshold تحتها
            _in_supply  = (_ds2b_ns_lo and _ds2b_ns_hi and
                           _ds2b_ns_lo - _ds2b_thr_c <= _spx_px_c <= _ds2b_ns_hi)
            if _in_supply:
                _blk_c = "CALL_DEBIT_BLOCKED_NEAR_PROXY_SUPPLY"
                warnings.append(
                    f"⛔ SPX ({_spx_px_c}) قرب Supply proxy [{_ds2b_ns_lo}–{_ds2b_ns_hi}] — لا Call Debit"
                )
                return {
                    "strategy": "Call Debit Spread", "emoji": "📈", "score": 0,
                    "score_breakdown": {"Trend": 0, "GEX": 0, "IV": 0, "Momentum": 0},
                    "long_call": None, "short_call": None,
                    "debit": None, "debit_worst": None,
                    "max_gain": None, "max_loss": None,
                    "reward_risk": 0, "setup_quality": "Blocked",
                    "pop": None, "legs_detail": [],
                    "reasons": [f"SPX قرب منطقة Supply proxy (SPY-based) — انتظار الكسر"],
                    "warnings": warnings,
                    "no_trade": True,
                    "decision": f"❌ مرفوض — {_blk_c}",
                    "blocked_code": _blk_c,
                    "ds_location_decision": _blk_c,
                }

        # Confidence = Trend 40% + GEX Negative 25% + IV Low 20% + Momentum 15%
        bull = {"strong_bullish", "bullish", "slightly_bullish"}
        trend_c = (40 if trend == "strong_bullish" else 30 if trend == "bullish" else 20 if trend == "slightly_bullish" else 8 if trend == "bullish_pullback" else 0)
        gex_c   = 25 if self.net_gex < -50_000 else (15 if self.net_gex < 0 else 0)
        iv_c    = 20 if self.iv_rank < 30 else (10 if self.iv_rank < 45 else 0)
        mom_c   = self._momentum_score(direction="up")
        score   = round(trend_c + gex_c + iv_c + mom_c)
        breakdown = {"Trend": round(trend_c), "GEX": round(gex_c), "IV": round(iv_c), "Momentum": round(mom_c)}

        if trend in ("bullish", "slightly_bullish", "strong_bullish"):
            reasons.append(f"Trend صاعد ({trend})")
        elif trend == "bullish_pullback":
            reasons.append("Daily صاعد — 15m يتراجع")
            warnings.append("Daily صاعد لكن 15m هابط — انتظر عودة الزخم قبل Call Debit")
        else:
            warnings.append(f"Trend غير صاعد — خطر على Call Debit")

        if self.net_gex < 0:
            reasons.append("GEX سلبي — احتمال حركة قوية للأعلى")
        if self.iv_rank < 30:
            reasons.append(f"IV Rank منخفض ({self.iv_rank:.0f}) — شراء Premium رخيص")
        elif self.iv_rank >= 45:
            warnings.append(f"IV Rank مرتفع ({self.iv_rank:.0f}) — الشراء غالٍ")

        # Debit: Long Call قريب من ATM (delta ~0.48)
        wing = _get_wing(self.symbol)
        lc = self._debit_long_strike("call")
        sc = (lc + wing) if lc else None
        lc_d = self._opt("call", lc)
        sc_d = self._opt("call", sc)
        legs = [lc_d, sc_d]
        debit       = self._debit([lc_d], [sc_d])
        debit_worst = self._debit_worst_case([lc_d], [sc_d])
        max_gain    = round(_get_wing(self.symbol) - max(debit or 0, 0), 2) if debit else None

        debit_penalty = 0
        if debit and debit > 0:
            debit_penalty = 0
            if debit_worst:
                reasons.append(f"Debit mid≈ {debit:.2f}  |  worst-case≈ {debit_worst:.2f}")
        elif debit is None:
            debit_penalty = -10
            score = max(score + debit_penalty, 0)
            warnings.append("Debit غير متاح من DXLink — لا تُسجل صفقة بدون سعر دخول")
            return {
                "strategy": "Call Debit Spread", "emoji": "📈", "score": 0,
                "score_breakdown": {**breakdown, "Debit": debit_penalty},
                "long_call": lc, "short_call": sc,
                "debit": debit, "debit_worst": debit_worst,
                "max_gain": None, "max_loss": None,
                "reward_risk": 0,
                "setup_quality": "Invalid price",
                "pop": 45.0,
                "legs_detail": self._legs_detail(legs),
                "reasons": reasons,
                "warnings": warnings,
                "no_trade": True,
                "decision": "❌ مرفوض — Debit غير متاح",
                "invalid_debit_price": True,
            }
        else:
            debit_penalty = -20
            score = max(score + debit_penalty, 0)
            warnings.append("Debit = 0 — لا تُسجل صفقة بدون سعر دخول صالح")
            return {
                "strategy": "Call Debit Spread", "emoji": "📈", "score": 0,
                "score_breakdown": {**breakdown, "Debit": debit_penalty},
                "long_call": lc, "short_call": sc,
                "debit": debit, "debit_worst": debit_worst,
                "max_gain": None, "max_loss": None,
                "reward_risk": 0,
                "setup_quality": "Invalid price",
                "pop": 45.0,
                "legs_detail": self._legs_detail(legs),
                "reasons": reasons,
                "warnings": warnings,
                "no_trade": True,
                "decision": "❌ مرفوض — Debit غير صالح",
                "invalid_debit_price": True,
            }

        spread_warns = self._spread_warnings(legs)
        score = self._apply_spread_penalty(score, spread_warns, breakdown)
        warnings.extend(self._clean_spread_warns(spread_warns))

        # POP حقيقي: احتمال انتهاء الـ long call فوق الـ breakeven
        # = |delta_long_call| × 100  (تقريب: delta ≈ احتمال الانتهاء ITM)
        lc_delta = lc_d.get("delta")
        if lc_delta is not None:
            pop = round(abs(lc_delta) * 100, 1)
        else:
            pop = 45.0  # افتراضي محافظ

        pop_penalty = 0
        if pop < 40:
            pop_penalty = -15
            score = max(score + pop_penalty, 0)
            warnings.append(f"POP منخفض جداً ({pop:.0f}%) — احتمال الربح ضعيف")
        elif pop < 50:
            pop_penalty = -7
            score = max(score + pop_penalty, 0)
            warnings.append(f"POP أقل من 50% ({pop:.0f}%) — ارتفاع الـ Strike قد يُحسّنه")
        else:
            reasons.append(f"POP مقبول ({pop:.0f}%)")

        breakdown["Debit"] = debit_penalty
        if pop_penalty: breakdown["POP"] = pop_penalty

        rr = self._calc_rr(debit, max_gain, is_credit=False)
        return {
            "strategy":    "Call Debit Spread",
            "score_breakdown": breakdown,
            "emoji":       "📈",
            "score":       score,
            "long_call":   lc, "short_call": sc,
            "debit":       debit, "debit_worst": debit_worst,
            "max_gain":    max_gain, "max_loss": debit,
            "reward_risk": rr,
            "setup_quality": self._quality_label(rr, is_credit=False),
            "pop":         pop,
            "legs_detail": self._legs_detail(legs),
            "reasons":     reasons, "warnings": warnings,
            "no_trade":    False,
            "decision":    self._decision(score, "Call Debit Spread"),
        }

    def scan_put_debit_spread(self) -> Dict[str, Any]:
        """Put Debit Spread — هابط + GEX سلبي + IV منخفض

        RC15j Phase 1 safety rule:
        ``bearish_bounce`` is a bearish higher-timeframe context while the
        intraday/15m tape is bouncing upward. It must not be treated as
        active downside momentum for 0DTE Put Debit entries.
        """
        score = 0
        reasons = []
        warnings = []
        trend = self._trend()

        # ── RC15j Phase 2C — القاعدة 3 أولاً: Exposure Cap ──────────────────────
        # لا داعي لأي حساب إذا عندنا SPX Put Debit مفتوحة بالفعل
        if str(self.symbol).upper() == "SPX":
            try:
                from core.database import get_paper_open_count_for_strategy
                _open_pd = get_paper_open_count_for_strategy("SPX", "Put Debit Spread", "0DTE")
                if _open_pd >= 1:
                    return {
                        "strategy": "Put Debit Spread", "emoji": "📉", "score": 0,
                        "score_breakdown": {"Trend": 0, "GEX": 0, "IV": 0, "Momentum": 0},
                        "long_put": None, "short_put": None,
                        "debit": None, "debit_worst": None,
                        "max_gain": None, "max_loss": None,
                        "reward_risk": 0, "setup_quality": "Blocked",
                        "pop": None, "legs_detail": [],
                        "reasons": [f"يوجد {_open_pd} صفقة SPX Put Debit مفتوحة — الحد الأقصى 1"],
                        "warnings": ["⛔ SPX bearish exposure cap = 1 صفقة مفتوحة كحد أقصى"],
                        "no_trade": True,
                        "decision": "❌ مرفوض — SPX_0DTE_BEARISH_EXPOSURE_CAP",
                        "blocked_code": "SPX_0DTE_BEARISH_EXPOSURE_CAP",
                        "ds_location_decision": "SPX_0DTE_BEARISH_EXPOSURE_CAP",
                        "pipeline_stop": "exposure_cap",
                    }
            except Exception as _e:
                print(f"[phase2c_exposure_cap] {_e}")

        if trend == "bearish_bounce":
            warnings.append("⛔ bearish_bounce: Daily/HTF هابط لكن 15m يرتد صعودًا — لا Put Debit حتى يعود الزخم الهابط")
            return {
                "strategy": "Put Debit Spread",
                "emoji": "📉",
                "score": 0,
                "score_breakdown": {"Trend": 0, "GEX": 0, "IV": 0, "Momentum": 0},
                "long_put": None, "short_put": None,
                "debit": None, "debit_worst": None,
                "max_gain": None, "max_loss": None,
                "reward_risk": 0,
                "setup_quality": "Blocked",
                "pop": None,
                "legs_detail": [],
                "reasons": ["bearish_bounce = انتظار / ارتداد صاعد داخل سياق هابط، وليس دخول Put Debit"],
                "warnings": warnings,
                "no_trade": True,
                "decision": "❌ مرفوض — BEARISH_BOUNCE_BLOCK_PUT_DEBIT",
                "blocked_code": "BEARISH_BOUNCE_BLOCK_PUT_DEBIT",
            }

        # ── RC15j Phase 2B — SPY Proxy D/S Hard Block for Put Debit ─────────────
        # يحظر Put Debit إذا كان SPX قرب منطقة Demand (مستوحى من SPY proxy).
        # Fail-safe: إذا البيانات غير متوفرة → DS_LOCATION_NOT_EVALUATED_PASS (لا حظر).
        _ds2b_ctx   = self.levels.get("_ds_proxy_context") or {}
        _ds2b_avail = _ds2b_ctx.get("available", False)
        if _ds2b_avail:
            _ds2b_ltf   = _ds2b_ctx.get("ltf", {})
            _ds2b_htf   = _ds2b_ctx.get("htf", {})
            _ds2b_nd    = (_ds2b_ltf.get("nearest_demand") or _ds2b_ltf.get("nearest_bullish_order_block") or
                           _ds2b_htf.get("nearest_demand") or _ds2b_htf.get("nearest_bullish_order_block") or {})
            _ds2b_thr   = self.levels.get("_ds_near_threshold") or max(5.0, (self.price or 5000) * 0.0015)
            _ds2b_nd_lo = _ds2b_nd.get("low")
            _ds2b_nd_hi = _ds2b_nd.get("high")
            _spx_px     = self.price or 0
            # حظر إذا السعر داخل منطقة Demand أو أقل من near_threshold فوقها
            _in_demand  = (_ds2b_nd_lo and _ds2b_nd_hi and
                           _ds2b_nd_lo <= _spx_px <= _ds2b_nd_hi + _ds2b_thr)
            if _in_demand:
                _blk = "PUT_DEBIT_BLOCKED_NEAR_PROXY_DEMAND"
                warnings.append(
                    f"⛔ SPX ({_spx_px}) قرب Demand proxy [{_ds2b_nd_lo}–{_ds2b_nd_hi}] — لا Put Debit"
                )
                return {
                    "strategy": "Put Debit Spread", "emoji": "📉", "score": 0,
                    "score_breakdown": {"Trend": 0, "GEX": 0, "IV": 0, "Momentum": 0},
                    "long_put": None, "short_put": None,
                    "debit": None, "debit_worst": None,
                    "max_gain": None, "max_loss": None,
                    "reward_risk": 0, "setup_quality": "Blocked",
                    "pop": None, "legs_detail": [],
                    "reasons": [f"SPX قرب منطقة Demand proxy (SPY-based) — انتظار الكسر"],
                    "warnings": warnings,
                    "no_trade": True,
                    "decision": f"❌ مرفوض — {_blk}",
                    "blocked_code": _blk,
                    "ds_location_decision": _blk,
                }

        # ── RC15j Phase 2C — Strict 0DTE Debit Location Gate ────────────────────
        # يمنع Put Debit بدون Supply location واضحة، أو بدون قرب من Supply.
        # أيضاً يمنع أكثر من SPX Put Debit واحدة مفتوحة في نفس الوقت.
        _2c_ctx   = self.levels.get("_ds_proxy_context") or {}
        _2c_src   = str(_2c_ctx.get("source") or "").lower()
        _2c_avail = _2c_ctx.get("available", False)
        _2c_spx   = self.price or 0
        _2c_thr   = self.levels.get("_ds_near_threshold") or max(5.0, _2c_spx * 0.0015)

        # القاعدة 1: proxy متاح لكن لا توجد Supply zone → حظر
        if _2c_avail and ("proxy" in _2c_src or "dxlink" in _2c_src):
            _2c_ltf = _2c_ctx.get("ltf", {}); _2c_htf = _2c_ctx.get("htf", {})
            _2c_ns  = (_2c_ltf.get("nearest_supply") or _2c_ltf.get("nearest_bearish_order_block") or
                       _2c_htf.get("nearest_supply") or _2c_htf.get("nearest_bearish_order_block") or {})
            _2c_ns_lo = _2c_ns.get("low")
            _2c_ns_hi = _2c_ns.get("high")

            if not _2c_ns_lo or not _2c_ns_hi:
                _blk2c = "PUT_DEBIT_BLOCKED_NO_PROXY_SUPPLY_LOCATION"
                return {
                    "strategy": "Put Debit Spread", "emoji": "📉", "score": 0,
                    "score_breakdown": {"Trend": 0, "GEX": 0, "IV": 0, "Momentum": 0},
                    "long_put": None, "short_put": None,
                    "debit": None, "debit_worst": None,
                    "max_gain": None, "max_loss": None,
                    "reward_risk": 0, "setup_quality": "Blocked",
                    "pop": None, "legs_detail": [],
                    "reasons": ["لا توجد Supply zone في البيانات — Put Debit يحتاج رفض من Supply"],
                    "warnings": [f"⛔ ds_source={_2c_src} لكن nearest_supply فارغة"],
                    "no_trade": True,
                    "decision": f"❌ مرفوض — {_blk2c}",
                    "blocked_code": _blk2c,
                    "ds_location_decision": _blk2c,
                    "pipeline_stop": "ds_location",
                }

            # القاعدة 2: Supply موجودة لكن السعر بعيد عنها → حظر
            # "قريب من Supply" = السعر بين supply_low - near_threshold و supply_high + near_threshold
            _near_supply = (_2c_ns_lo - _2c_thr <= _2c_spx <= _2c_ns_hi + _2c_thr)
            if not _near_supply:
                _blk2c = "PUT_DEBIT_BLOCKED_NOT_AT_SUPPLY"
                _dist_to_supply = round(_2c_ns_lo - _2c_spx, 1) if _2c_ns_lo else None
                return {
                    "strategy": "Put Debit Spread", "emoji": "📉", "score": 0,
                    "score_breakdown": {"Trend": 0, "GEX": 0, "IV": 0, "Momentum": 0},
                    "long_put": None, "short_put": None,
                    "debit": None, "debit_worst": None,
                    "max_gain": None, "max_loss": None,
                    "reward_risk": 0, "setup_quality": "Blocked",
                    "pop": None, "legs_detail": [],
                    "reasons": [f"SPX ({_2c_spx}) بعيد عن Supply [{_2c_ns_lo}–{_2c_ns_hi}] — انتظر الرفض من Supply"],
                    "warnings": [f"⛔ distance={_dist_to_supply} نقطة تحت Supply — near_threshold={_2c_thr}"],
                    "no_trade": True,
                    "decision": f"❌ مرفوض — {_blk2c}",
                    "blocked_code": _blk2c,
                    "ds_location_decision": _blk2c,
                    "distance_to_supply_points": _dist_to_supply,
                    "near_threshold": _2c_thr,
                    "pipeline_stop": "ds_location",
                }

        trend_c = (40 if trend == "strong_bearish" else 30 if trend == "bearish" else 20 if trend == "slightly_bearish" else 0)
        gex_c   = 25 if self.net_gex < -50_000 else (15 if self.net_gex < 0 else 0)
        iv_c    = 20 if self.iv_rank < 30 else (10 if self.iv_rank < 45 else 0)
        mom_c   = self._momentum_score(direction="down")
        breakdown = {"Trend": round(trend_c), "GEX": round(gex_c), "IV": round(iv_c), "Momentum": round(mom_c)}
        score   = round(trend_c + gex_c + iv_c + mom_c)

        if trend in ("bearish", "slightly_bearish", "strong_bearish"):
            reasons.append(f"Trend هابط ({trend})")
        else:
            warnings.append("Trend غير هابط")

        if self.net_gex < 0:
            reasons.append("GEX سلبي — احتمال حركة قوية للأسفل")
        if self.iv_rank < 30:
            reasons.append(f"IV Rank منخفض ({self.iv_rank:.0f}) — شراء Premium رخيص")

        # Debit: Long Put قريب من ATM (delta ~0.48)
        wing = _get_wing(self.symbol)
        lp = self._debit_long_strike("put")
        sp = (lp - wing) if lp else None
        lp_d = self._opt("put", lp)
        sp_d = self._opt("put", sp)
        legs = [lp_d, sp_d]
        debit       = self._debit([lp_d], [sp_d])
        debit_worst = self._debit_worst_case([lp_d], [sp_d])
        max_gain    = round(_get_wing(self.symbol) - max(debit or 0, 0), 2) if debit else None

        debit_penalty = 0
        if debit and debit > 0:
            debit_penalty = 0
            if debit_worst:
                reasons.append(f"Debit mid≈ {debit:.2f}  |  worst-case≈ {debit_worst:.2f}")
        elif debit is None:
            debit_penalty = -10
            score = max(score + debit_penalty, 0)
            warnings.append("Debit غير متاح من DXLink — لا تُسجل صفقة بدون سعر دخول")
            return {
                "strategy": "Put Debit Spread", "emoji": "📉", "score": 0,
                "score_breakdown": {**breakdown, "Debit": debit_penalty},
                "long_put": lp, "short_put": sp,
                "debit": debit, "debit_worst": debit_worst,
                "max_gain": None, "max_loss": None,
                "reward_risk": 0,
                "setup_quality": "Invalid price",
                "pop": 45.0,
                "legs_detail": self._legs_detail(legs),
                "reasons": reasons,
                "warnings": warnings,
                "no_trade": True,
                "decision": "❌ مرفوض — Debit غير متاح",
                "invalid_debit_price": True,
            }
        else:
            debit_penalty = -20
            score = max(score + debit_penalty, 0)
            warnings.append("Debit = 0 — لا تُسجل صفقة بدون سعر دخول صالح")
            return {
                "strategy": "Put Debit Spread", "emoji": "📉", "score": 0,
                "score_breakdown": {**breakdown, "Debit": debit_penalty},
                "long_put": lp, "short_put": sp,
                "debit": debit, "debit_worst": debit_worst,
                "max_gain": None, "max_loss": None,
                "reward_risk": 0,
                "setup_quality": "Invalid price",
                "pop": 45.0,
                "legs_detail": self._legs_detail(legs),
                "reasons": reasons,
                "warnings": warnings,
                "no_trade": True,
                "decision": "❌ مرفوض — Debit غير صالح",
                "invalid_debit_price": True,
            }

        spread_warns = self._spread_warnings(legs)
        score = self._apply_spread_penalty(score, spread_warns, breakdown)
        warnings.extend(self._clean_spread_warns(spread_warns))

        # POP حقيقي للـ Put Debit: |delta_long_put| × 100
        lp_delta = lp_d.get("delta")
        if lp_delta is not None:
            pop = round(abs(lp_delta) * 100, 1)
        else:
            pop = 45.0

        pop_penalty = 0
        if pop < 40:
            pop_penalty = -15
            score = max(score + pop_penalty, 0)
            warnings.append(f"POP منخفض جداً ({pop:.0f}%) — احتمال الربح ضعيف")
        elif pop < 50:
            pop_penalty = -7
            score = max(score + pop_penalty, 0)
            warnings.append(f"POP أقل من 50% ({pop:.0f}%) — خفض الـ Strike قد يُحسّنه")
        else:
            reasons.append(f"POP مقبول ({pop:.0f}%)")

        breakdown["Debit"] = debit_penalty
        if pop_penalty: breakdown["POP"] = pop_penalty

        rr = self._calc_rr(debit, max_gain, is_credit=False)
        return {
            "strategy":    "Put Debit Spread",
            "score_breakdown": breakdown,
            "emoji":       "📉",
            "score":       score,
            "long_put":    lp, "short_put": sp,
            "debit":       debit, "debit_worst": debit_worst,
            "max_gain":    max_gain, "max_loss": debit,
            "reward_risk": rr,
            "setup_quality": self._quality_label(rr, is_credit=False),
            "pop":         pop,
            "legs_detail": self._legs_detail(legs),
            "reasons":     reasons, "warnings": warnings,
            "no_trade":    False,
            "decision":    self._decision(score, "Put Debit Spread"),
        }

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _vanna_charm_signal(self) -> Dict[str, Any]:
        """
        تحويل Vanna و Charm لإشارة نوعية تُستخدم كتحذير أو دعم.

        Vanna양 > 0: ضغط شرائي (dealers يشترون عند ارتفاع IV) → دعم صعودي
        Vanna < 0: ضغط بيعي → ضغط هبوطي
        Charm < 0: تسارع تآكل دلتا → ضغط على المراكز قرب الانتهاء
        """
        signal = {"direction": "neutral", "strength": 0, "notes": []}
        if self.net_vanna == 0 and self.net_charm == 0:
            return signal

        if self.net_vanna > 0:
            signal["direction"] = "bullish"
            signal["strength"] += 1
            signal["notes"].append("Vanna إيجابي — ضغط شرائي عند ارتفاع IV")
        elif self.net_vanna < 0:
            signal["direction"] = "bearish"
            signal["strength"] += 1
            signal["notes"].append("Vanna سلبي — ضغط بيعي")

        if self.net_charm < 0:
            signal["strength"] += 1
            signal["notes"].append("Charm سلبي — تسارع تآكل دلتا")

        return signal

    def _gex_score(self, direction: str, max_pts: int = 25) -> int:
        """
        نقاط GEX مع مراعاة جودة البيانات.
        - real_oi / volume_proxy: نقاط حقيقية بناءً على net_gex
        - gamma_proxy: نقاط محايدة (نصف الحد الأقصى) لأن net_gex غير موثوق
        """
        if self.gex_quality == "gamma_proxy":
            return max_pts // 2   # محايد: 12 من 25

        if direction == "bullish":   # Bull Put + Call Debit
            if self.net_gex > 10_000:   return max_pts           # GEX إيجابي قوي ✅
            if self.net_gex >= 0:       return int(max_pts * 0.6)
            if self.net_gex >= -50_000: return int(max_pts * 0.2)
            return int(max_pts * 0.1)                             # GEX سلبي كبير — نقاط جزئية بدلاً من صفر
        else:                        # Bear Call + Put Debit
            if self.net_gex < -10_000:  return max_pts           # GEX سلبي قوي ✅
            if self.net_gex < 0:        return int(max_pts * 0.6)
            if self.net_gex < 50_000:   return int(max_pts * 0.2)
            return int(max_pts * 0.1)                             # GEX إيجابي كبير — نقاط جزئية بدلاً من صفر

    def _hard_blocks(self) -> List[str]:
        """
        فقط الأخطاء القاطعة — VIX > 35 فقط.
        غياب الأسعار والـ Greeks لا يوقف التحليل بل يُحذر فقط.
        """
        issues = []
        if self.vix >= 35:
            issues.append(f"VIX مرتفع جداً ({self.vix:.1f}) — تجنب جميع الاستراتيجيات")
        return issues

    def _soft_warnings(self) -> List[str]:
        """تحذيرات تُعرض لكن لا توقف الحساب."""
        warns = []
        if not self.market_open:
            warns.append("السوق مغلق — هذا تحليل مرجعي")
        if not self.has_prices and not self.has_delta:
            warns.append("DXLink: لا توجد أسعار أو Greeks — التحليل بدون credit")
        if self.vix >= 25:
            warns.append(f"VIX مرتفع ({self.vix:.1f}) — توسيع الأجنحة موصى به")
        # تحذير قرب الإغلاق: بعد 3:30 PM ET السيولة تجف والـ spread يتسع
        try:
            from datetime import datetime, timezone, timedelta
            now_utc = datetime.now(timezone.utc)
            month = now_utc.month
            is_edt = 3 < month < 11 or (month == 3 and now_utc.day >= 8) or (month == 11 and now_utc.day < 8)
            now_et = now_utc + timedelta(hours=-4 if is_edt else -5)
            minutes_to_close = (16 * 60) - (now_et.hour * 60 + now_et.minute)
            if 0 < minutes_to_close <= 30:
                warns.append(f"⚠️ {minutes_to_close} دقيقة لإغلاق السوق — السيولة ضعيفة، spreads واسعة")
        except Exception:
            pass
        return warns

    def _check_data_quality(self) -> List[str]:
        """للتوافق مع الكود القديم — يستخدم _hard_blocks داخلياً"""
        return self._hard_blocks()

    def _trend(self) -> str:
        """Combined trend: Daily bias + 15m tactical filter."""
        if self.combined_trend and self.combined_trend != "neutral":
            return self.combined_trend

        # Fallback: daily EMA فقط إذا لم يتوفر combined
        p, e20, e50 = self.price, self.ema20, self.ema50
        if not e20:
            return "neutral"
        dist_pct = (p - e20) / e20 * 100 if e20 else 0
        if e50:
            if p > e20 and e20 > e50:
                return "strong_bullish" if dist_pct > 0.3 else "bullish"
            if p < e20 and e20 < e50:
                return "strong_bearish" if dist_pct < -0.3 else "bearish"
        if dist_pct > 0.15:   return "slightly_bullish"
        if dist_pct < -0.15:  return "slightly_bearish"
        return "neutral"

    def _is_bullish(self) -> bool:
        return self._trend() in ("strong_bullish", "bullish", "slightly_bullish", "bullish_pullback")

    def _is_bearish(self) -> bool:
        return self._trend() in ("strong_bearish", "bearish", "slightly_bearish", "bearish_bounce")

    def _trend_strength(self) -> int:
        """قوة الاتجاه 0-40، مع تخفيض عند اختلاف Daily و15m."""
        t = self._trend()
        if t in ("strong_bullish", "strong_bearish"):        return 40
        if t in ("bullish", "bearish"):                      return 30
        if t in ("slightly_bullish", "slightly_bearish"):    return 15
        if t in ("bullish_pullback", "bearish_bounce"):      return 8
        return 0

    def _near_zero_gamma(self) -> bool:
        if not self.zero_gamma:
            return False
        return abs(self.price - self.zero_gamma) < self.em * 0.4

    def _distance_score(self, direction: str) -> float:
        """
        درجة بُعد السعر عن أقرب مقاومة/دعم.
        يتحقق أن الجدار في الاتجاه الصحيح:
          direction="up"  → الجدار يجب أن يكون فوق السعر
          direction="down" → الجدار يجب أن يكون تحت السعر
        """
        em = max(self.em, 1)
        if direction == "up":
            # ابحث عن أقرب جدار فوق السعر فقط
            candidates = []
            for key in ("call_wall_oi", "call_wall_gex", "call_wall_vol", "em_upper"):
                w = self.levels.get(key)
                if w and w > self.price:
                    candidates.append(w)
            if candidates:
                wall = min(candidates)   # أقرب جدار فوق السعر
                dist = wall - self.price
                return min(dist / em * 20, 20)
        else:
            # ابحث عن أقرب جدار تحت السعر فقط
            candidates = []
            for key in ("put_wall_oi", "put_wall_gex", "put_wall_vol", "em_lower"):
                w = self.levels.get(key)
                if w and w < self.price:
                    candidates.append(w)
            if candidates:
                wall = max(candidates)   # أقرب جدار تحت السعر
                dist = self.price - wall
                return min(dist / em * 20, 20)
        return 10

    def _momentum_score(self, direction: str) -> float:
        """درجة الزخم للـ Debit Spreads"""
        trend = self._trend()
        if direction == "up" and trend in ("bullish", "slightly_bullish"):
            return 15
        if direction == "down" and trend in ("bearish", "slightly_bearish"):
            return 15
        return 5

    def _strike(self, option_type: str, above: bool, distance: float) -> Optional[float]:
        if not self.chain:
            return None
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        target  = self.price + distance if above else self.price - distance
        candidates = [o for o in options
                      if (o["strike"] >= target if above else o["strike"] <= target)]
        if not candidates:
            candidates = options
        if not candidates:
            return None
        return min(candidates, key=lambda o: abs(o["strike"] - target))["strike"]

    # 0DTE Credit sigma multiplier — متوافق مع Swing
    CREDIT_SIGMA_0DTE = 1.5   # Short leg = price ± 1.5 × EM_daily

    def _credit_short_strike(self, option_type: str,
                              sigma_mult: Optional[float] = None) -> Optional[float]:
        """
        اختيار Short Strike لـ Credit Spreads — يعتمد على 1.5σ (Sigma).

        Short Put  = price - 1.5 × EM_daily
        Short Call = price + 1.5 × EM_daily

        Delta يُستخدم للفحص فقط (ليس للتحديد).
        Fallback: نفس المسافة 1.5σ بدون delta.
        """
        above  = (option_type == "call")
        mult   = sigma_mult if sigma_mult is not None else self.CREDIT_SIGMA_0DTE
        target = self.price + self.em * mult if above else self.price - self.em * mult
        return self._nearest_strike_liquid(option_type, target)

    def _ic_short_strike(self, option_type: str) -> Optional[float]:
        """Short Strike لـ IC — نفس 1.5σ (IC يحتاج أبعد قليلاً → 1.6σ)."""
        above  = (option_type == "call")
        target = self.price + self.em * 1.6 if above else self.price - self.em * 1.6
        return self._nearest_strike_liquid(option_type, target)

    def _nearest_strike_liquid(self, option_type: str, target: float) -> Optional[float]:
        """يختار أقرب ضربة سائلة (bid > 0) للهدف."""
        if not self.chain:
            return None
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        liquid  = [o for o in options if (o.get("bid") or 0) > 0]
        pool    = liquid if liquid else options
        if not pool:
            return None
        return min(pool, key=lambda o: abs(o["strike"] - target))["strike"]

    def _check_credit_delta(self, option_type: str, strike: Optional[float],
                            warnings: list) -> bool:
        """
        فلتر Delta للـ Credit Short leg — فحص بعد اختيار الـ strike.

        قبول:  0.05 ≤ |delta| ≤ 0.20
        رفض إذا:
          delta > 0.20 → قريب جداً من ATM → خطر عالٍ
          delta < 0.05 → بعيد جداً → credit ضعيف جداً

        يعيد True إذا مقبول، False إذا يجب رفض الصفقة.
        """
        if strike is None or not self.chain:
            return True   # لا بيانات → لا رفض
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        opt = next((o for o in options if o.get("strike") == strike), None)
        if not opt:
            return True   # strike غير موجود في الـ chain → لا رفض
        delta = abs(opt.get("delta") or 0)
        if delta == 0:
            return True   # delta غير متاح → لا رفض

        if delta > 0.20:
            msg = f"Short {option_type} strike={strike} delta={delta:.3f} > 0.20 — قريب جداً من ATM، خطر Gamma"
            print(f"[DELTA_REJECT] {msg}")
            warnings.append(f"⚠️ {msg}")
            return False   # رفض

        if delta < 0.05:
            msg = f"Short {option_type} strike={strike} delta={delta:.3f} < 0.05 — Credit ضعيف جداً"
            print(f"[DELTA_REJECT] {msg}")
            warnings.append(f"⚠️ {msg}")
            return False   # رفض

        return True   # مقبول: 0.05 ≤ delta ≤ 0.20

    def _debit_long_strike(self, option_type: str) -> Optional[float]:
        """
        Long Strike لـ Debit Spreads — قريب ATM (delta ~0.48).
        حد أقصى للبُعد: 0.3% لضمان السيولة.
        """
        above      = (option_type == "call")
        max_dist   = self.price * 0.003   # 0.3% حد أقصى للبُعد عن ATM
        strike     = self._strike_by_delta(option_type, TARGET_DELTA["debit_long"],
                                           above, fallback_distance=0)
        if strike is None:
            return None
        # إذا كان البُعد أكبر من 0.3% → fallback للـ ATM مباشرة
        if abs(strike - self.price) > max_dist:
            strike = self._strike(option_type, above=above, distance=0)
        return strike

    @staticmethod
    def _minutes_to_close() -> float:
        """دقائق متبقية حتى إغلاق السوق (4:00 PM ET) مع مراعاة EDT/EST."""
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        month = now_utc.month
        is_edt = 3 < month < 11 or (month == 3 and now_utc.day >= 8) or (month == 11 and now_utc.day < 8)
        now_et = now_utc + timedelta(hours=-4 if is_edt else -5)
        return max(0.0, (16 * 60) - (now_et.hour * 60 + now_et.minute))

    def _near_close(self) -> bool:
        """True إذا تبقى أقل من ساعتين على الإغلاق — Delta تفقد معناها."""
        return self._minutes_to_close() <= 120

    # ── Liquidity Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _liquidity_score(opt: Dict) -> float:
        """
        نقاط سيولة 0.0–1.0 لعقد واحد — يُستخدم في ترتيب الاختيار.

        المكونات:
          Bid/Ask present  → 0.40
          Spread < 30%     → 0.30  (متناسب خطياً)
          Volume ≥ 1       → 0.20
          OI ≥ 1           → 0.10
        المجموع الأقصى = 1.0  (تعني سيولة ممتازة)
        """
        score = 0.0
        bid = opt.get("bid")
        ask = opt.get("ask")

        if bid is not None and ask is not None and bid > 0 and ask > 0:
            score += 0.40
            mid = (bid + ask) / 2
            if mid > 0:
                spread_pct = (ask - bid) / mid * 100
                # 0 spread → 0.30 نقطة، 30% spread → 0 نقطة، خطي
                spread_pts = max(0.0, 0.30 * (1 - spread_pct / 30))
                score += spread_pts

        volume = opt.get("volume") or 0
        oi     = opt.get("oi") or 0
        if volume >= 1:
            score += 0.20
        if oi >= 1:
            score += 0.10

        return round(min(score, 1.0), 4)

    @staticmethod
    def _liquidity_pass(opt: Dict) -> bool:
        """
        Pre-filter صارم: يرفض الضربة قبل الاختيار إذا كانت سيولتها مرفوضة.
        الرفض عند: لا bid/ask أبداً  أو  Spread > LIQ_SPREAD_HARD_REJECT%.
        """
        bid = opt.get("bid")
        ask = opt.get("ask")
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return False
        mid = (bid + ask) / 2
        if mid <= 0:
            return False
        spread_pct = (ask - bid) / mid * 100
        return spread_pct <= LIQ_SPREAD_HARD_REJECT

    def _strike_by_delta(self, option_type: str, target_delta: float,
                          above: bool, fallback_distance: float) -> Optional[float]:
        """
        Hybrid Model مع Liquidity Pre-filter:

        1. فلتر الاتجاه (calls فوق السعر / puts تحت السعر)
        2. Pre-filter السيولة: يُزيل الضربات ذات spread > 60% أو بلا bid/ask
           → إذا لم يتبقَّ شيء: fallback للكل بدون فلتر (مع تحذير)
        3. آخر ساعتين: Delta تنهار → EM مباشرة (من الضربات المفلترة)
        4. Delta متاح: أقرب delta + أعلى liquidity_score (وزن 30%)
        5. Delta غير متاح: fallback_distance

        target_delta: قيمة موجبة دائماً (0.15، 0.20...)
        """
        if not self.chain:
            return None

        options = self.chain.get("calls" if option_type == "call" else "puts", [])

        # 1. فلتر الاتجاه
        if above:
            side = [o for o in options if o.get("strike", 0) > self.price]
        else:
            side = [o for o in options if o.get("strike", 0) < self.price]
        if not side:
            side = options

        # 2. Pre-filter السيولة — نحتفظ بالضربات القابلة للتنفيذ
        liquid = [o for o in side if self._liquidity_pass(o)]
        # إذا لا شيء يمر الفلتر: نستخدم الكل مع وضع علامة
        pool = liquid if liquid else side
        _liq_fallback = len(liquid) == 0 and len(side) > 0

        # 3. آخر ساعتين: Delta تنهار → استخدم EM مباشرة
        if self._near_close():
            if _liq_fallback:
                return self._strike_from_pool(option_type, above, fallback_distance, pool)
            return self._strike_from_pool(option_type, above, fallback_distance, pool)

        # 4. تحقق هل Delta متاح
        has_delta = any(o.get("delta") is not None for o in pool)

        if has_delta:
            # ترتيب: delta_dist (70%) + liquidity_score (30%)
            # نُطبّع delta_dist على نطاق 0-1 قبل الجمع
            def _combined_score(o):
                d = abs(o.get("delta") or 0)
                delta_dist = abs(d - target_delta)         # 0 = مثالي
                liq = self._liquidity_score(o)             # 1.0 = مثالي
                # نحوّل: أقل delta_dist وأعلى liq = أفضل
                # نضيف penalty للسيولة بوزن 30% من مسافة delta نموذجية (0.15)
                liq_penalty = (1.0 - liq) * 0.15 * 0.30
                return delta_dist + liq_penalty

            best = min(pool, key=_combined_score)
            return best["strike"]
        else:
            # Fallback: distance-based من الضربات السائلة
            return self._strike_from_pool(option_type, above, fallback_distance, pool)

    def _strike_from_pool(self, option_type: str, above: bool,
                           distance: float, pool: List[Dict]) -> Optional[float]:
        """يختار أقرب ضربة لمسافة معينة من pool محدد."""
        target = self.price + distance if above else self.price - distance
        if above:
            candidates = [o for o in pool if o.get("strike", 0) >= target]
        else:
            candidates = [o for o in pool if o.get("strike", 0) <= target]
        if not candidates:
            candidates = pool
        if not candidates:
            return None
        return min(candidates, key=lambda o: abs(o["strike"] - target))["strike"]

    def _opt(self, option_type: str, strike: Optional[float]) -> Dict:
        if not self.chain or strike is None:
            return {}
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        for o in options:
            if o.get("strike") == strike:
                return o
        return {}

    @staticmethod
    def _mid(opt: Dict) -> float:
        mid = opt.get("mid")
        if mid:
            return float(mid)
        bid = opt.get("bid") or 0
        ask = opt.get("ask") or 0
        if bid and ask:
            return round((float(bid) + float(ask)) / 2, 2)
        return 0.0

    @staticmethod
    def _spread_pct(opt: Dict) -> Optional[float]:
        """نسبة Bid/Ask Spread من الـ Mid — كلما ارتفعت زادت تكلفة التنفيذ الخفية"""
        bid = opt.get("bid")
        ask = opt.get("ask")
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return None
        mid = (bid + ask) / 2
        if mid <= 0:
            return None
        return round((ask - bid) / mid * 100, 1)

    # عقوبات spread متدرجة — تُطبَّق على الـ score النهائي بعد الاختيار
    # الحالات الحرجة (>60%) تُزال مسبقاً بـ _liquidity_pass pre-filter
    _SPREAD_PENALTY_MISSING  = -15   # لا يوجد bid/ask (نادر بعد الفلتر — يعني الفلتر فشل في إيجاد بديل)
    _SPREAD_PENALTY_CRITICAL = -12   # spread > 50% (وصل هنا لأن pool كان فارغاً)
    _SPREAD_PENALTY_HIGH     = -6    # spread 30-50% — سيولة ضعيفة

    def _apply_spread_penalty(self, score: int, spread_warns: List[str],
                              breakdown: Dict) -> int:
        """يحسب العقوبة الفعلية من التحذيرات المضمّنة ويطبقها."""
        total_penalty = 0
        for w in spread_warns:
            if w.startswith("__PENALTY__"):
                parts = w.split("__", 3)
                try:
                    total_penalty += int(parts[2])
                except (IndexError, ValueError):
                    total_penalty += -5
        if total_penalty:
            score = max(score + total_penalty, 0)
            breakdown["Spread"] = total_penalty
        return score

    @staticmethod
    def _clean_spread_warns(warns: List[str]) -> List[str]:
        """يُزيل بادئة __PENALTY__X__ من نصوص التحذيرات للعرض."""
        cleaned = []
        for w in warns:
            if w.startswith("__PENALTY__"):
                cleaned.append(w.split("__", 3)[-1])
            else:
                cleaned.append(w)
        return cleaned

    def _spread_warnings(self, legs: List[Dict]) -> List[str]:
        """
        تحذيرات سيولة بعد الاختيار مع عقوبات متدرجة.
        ملاحظة: الحالات > 60% نادرة هنا لأن _liquidity_pass فلترها مسبقاً.
        """
        warns = []
        for opt in legs:
            if not opt:
                continue
            pct    = self._spread_pct(opt)
            strike = opt.get("strike")
            liq_score = round(self._liquidity_score(opt) * 100)
            vol = opt.get("volume") or 0
            oi  = opt.get("oi") or 0
            if pct is None:
                warns.append(f"__PENALTY__{self._SPREAD_PENALTY_MISSING}__Strike {strike:,.0f}: لا توجد bid/ask (Liq={liq_score}) — ⛔")
            elif pct > 50:
                warns.append(f"__PENALTY__{self._SPREAD_PENALTY_CRITICAL}__Strike {strike:,.0f}: Spread {pct:.0f}% (Liq={liq_score}) — سيولة ضعيفة ⛔")
            elif pct > LIQ_SPREAD_WARN:
                warns.append(f"__PENALTY__{self._SPREAD_PENALTY_HIGH}__Strike {strike:,.0f}: Spread {pct:.0f}% (Liq={liq_score}) — تحقق من التنفيذ ⚠️")
            elif vol == 0 and oi == 0 and pct is not None:
                # bid/ask موجود لكن لا حجم ولا OI — تحذير بدون عقوبة score
                warns.append(f"Strike {strike:,.0f}: Spread {pct:.0f}% — Volume=0, OI=0 ⚠️ تحقق من السيولة")
        return warns

    def _legs_detail(self, legs: List[Dict]) -> List[Dict]:
        """تفاصيل bid/ask/mid/spread/liquidity لكل leg للعرض في الواجهة"""
        result = []
        for opt in legs:
            if not opt:
                continue
            bid = opt.get("bid")
            ask = opt.get("ask")
            mid = self._mid(opt)
            spread_pct  = self._spread_pct(opt)
            liq_score   = round(self._liquidity_score(opt) * 100)
            liq_pass    = self._liquidity_pass(opt)
            volume      = opt.get("volume") or 0
            oi          = opt.get("oi") or 0
            result.append({
                "strike":      opt.get("strike"),
                "bid":         round(bid, 2) if bid else None,
                "ask":         round(ask, 2) if ask else None,
                "mid":         round(mid, 2) if mid else None,
                "delta":       opt.get("delta"),
                "gamma":       opt.get("gamma"),
                "iv":          opt.get("iv"),
                "symbol":      opt.get("symbol") or opt.get("streamer_symbol") or opt.get("eventSymbol"),
                "option_type": opt.get("option_type") or opt.get("type") or opt.get("put_call"),
                "spread_pct":  spread_pct,
                "liq_score":   liq_score,   # 0-100
                "liq_pass":    liq_pass,     # True/False
                "volume":      volume,
                "oi":          oi,
            })
        return result

    def _credit(self, sell_legs, buy_legs) -> Optional[float]:
        try:
            sell = sum(self._mid(o) for o in sell_legs if o)
            buy  = sum(self._mid(o) for o in buy_legs  if o)
            result = round(sell - buy, 2)
            return result if result > 0 else None
        except Exception:
            return None

    def _credit_worst_case(self, sell_legs, buy_legs) -> Optional[float]:
        """Credit بأسوأ حالة: بيع بـ bid، شراء بـ ask — أكثر واقعية للتنفيذ"""
        try:
            sell = sum(float(o.get("bid") or self._mid(o)) for o in sell_legs if o)
            buy  = sum(float(o.get("ask") or self._mid(o)) for o in buy_legs  if o)
            result = round(sell - buy, 2)
            return result if result > 0 else None
        except Exception:
            return None

    def _debit(self, buy_legs, sell_legs) -> Optional[float]:
        try:
            buy  = sum(self._mid(o) for o in buy_legs  if o)
            sell = sum(self._mid(o) for o in sell_legs if o)
            result = round(buy - sell, 2)
            if result > 0:
                return result
            # fallback: ask للشراء، bid للبيع — أكثر واقعية عند غياب mid
            buy_ask  = sum(float(o.get("ask") or o.get("mid") or 0) for o in buy_legs  if o)
            sell_bid = sum(float(o.get("bid") or o.get("mid") or 0) for o in sell_legs if o)
            worst = round(buy_ask - sell_bid, 2)
            return worst if worst > 0 else None
        except Exception:
            return None

    def _debit_worst_case(self, buy_legs, sell_legs) -> Optional[float]:
        """Debit بأسوأ حالة: شراء بـ ask، بيع بـ bid"""
        try:
            buy  = sum(float(o.get("ask") or self._mid(o)) for o in buy_legs  if o)
            sell = sum(float(o.get("bid") or self._mid(o)) for o in sell_legs if o)
            result = round(buy - sell, 2)
            return result if result > 0 else None
        except Exception:
            return None

    @staticmethod
    def _pop(put_delta, call_delta) -> Optional[float]:
        """
        POP = احتمال انتهاء الخيار OTM (خارج نطاق الخسارة).
        المعادلة: POP ≈ 1 - |delta_short_put| - |delta_short_call|
        هذا تقريب من Black-Scholes وليس احتمالاً رياضياً دقيقاً.
        Delta الحقيقي من DXLink يعطي نتيجة أدق من التخمين.
        """
        try:
            if put_delta is not None and call_delta is not None:
                pop = (1 - abs(put_delta) - abs(call_delta)) * 100
                return round(max(0, min(99, pop)), 1)
            elif put_delta is not None:
                pop = (1 - abs(put_delta)) * 100
                return round(max(0, min(99, pop)), 1)
            elif call_delta is not None:
                pop = (1 - abs(call_delta)) * 100
                return round(max(0, min(99, pop)), 1)
        except Exception:
            pass
        return None

    @staticmethod
    def _decision(score: int, strategy_name: str = "") -> str:
        """
        مقياس قرار موحد:
          < 50  → رفض
          50-64 → مؤهل للمراقبة/التسجيل الورقي إذا اجتاز فلاتر التنفيذ
          65-79 → مقبول للتسجيل الورقي
          ≥ 80  → قوي
        """
        if score >= 80: return "✅ قوي — يُسجَّل ورقياً إذا اجتاز الفلاتر"
        if score >= 65: return "✅ مقبول — يُسجَّل ورقياً إذا اجتاز الفلاتر"
        if score >= 50: return "⚠️ مؤهل للمراقبة — يُسجَّل ورقياً إذا اجتاز الفلاتر"
        return "❌ مرفوض"

    def _has_valid_prices(self, legs: List[Dict]) -> bool:
        """يتحقق أن جميع الـ legs لديها bid وask حقيقيان قبل اقتراح الصفقة."""
        for leg in legs:
            if not leg:
                continue
            bid = leg.get("bid")
            ask = leg.get("ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0:
                return False
        return True

    @staticmethod
    def _credit_width_check(
        credit: Optional[float],
        wing: float,
        min_ratio: float,
        strategy_name: str,
    ) -> Tuple[bool, Optional[str]]:
        """
        HARD REJECT إذا credit/wing < min_ratio.

        يُعيد (rejected: bool, reason: str | None).
          rejected=False → اجتاز — المتابعة طبيعية
          rejected=True  → رفض كامل — حتى لو Score مرتفع

        المبرر: Score يقيس جودة الاتجاه/الاحتمال.
        لكن credit ضعيف بعد العمولة والانزلاق يجعل الصفقة
        سالبة التوقع حتى مع إشارة ممتازة.
        """
        if not credit or credit <= 0 or wing <= 0:
            return False, None   # لا بيانات أسعار — لا رفض (معالجة أخرى ستتولى)

        actual_ratio = credit / wing
        if actual_ratio >= min_ratio:
            return False, None   # ✅ اجتاز

        min_credit_needed = round(wing * min_ratio, 2)
        reason = (
            f"Credit too small: credit/width = {actual_ratio:.1%} "
            f"< required {min_ratio:.0%}. "
            f"credit={credit:.2f}, wing={wing:.0f}, need ≥{min_credit_needed:.2f} ⛔"
        )
        print(f"[CW_REJECT] {strategy_name} | {reason}")
        return True, reason

    @staticmethod
    def _calc_rr(val: Optional[float], max_loss: Optional[float],
                 is_credit: bool = True) -> Optional[float]:
        """
        Reward/Risk للـ 0DTE:
          Credit: val / max_loss
          Debit:  (wing - val) / val  →  max_gain / val
        """
        try:
            if is_credit:
                if val and max_loss and max_loss > 0:
                    return round(val / max_loss, 3)
            else:
                # max_loss = debit، max_gain = max_loss من الـ strat
                if val and max_loss and val > 0:
                    max_gain = max_loss  # max_loss field في debit = wing - debit
                    return round(max_gain / val, 3) if max_gain else None
        except Exception:
            pass
        return None

    @staticmethod
    def _quality_label(rr: Optional[float], is_credit: bool = True) -> str:
        if rr is None:
            return "—"
        if is_credit:
            if rr >= 0.25: return "ممتاز"
            if rr >= 0.15: return "جيد"
            if rr >= 0.10: return "مقبول"
            return "ضعيف"
        else:
            if rr >= 2.0: return "ممتاز"
            if rr >= 1.5: return "جيد"
            if rr >= 1.0: return "مقبول"
            return "ضعيف"

    def _no_trade(self, reasons: List[str], regime: MarketRegime = MarketRegime.NO_TRADE, all_scores: dict = None) -> Dict:
        return {
            "strategy":   "No Trade",
            "emoji":      "🚫",
            "score":      0,
            "decision":   "لا تدخل — السوق لا يوفر أفضلية إحصائية",
            "reasons":    reasons,
            "warnings":   [],
            "no_trade":   True,
            "regime":     regime.value,
            "all_scores": all_scores or {},
        }


# ── Public API ────────────────────────────────────────────────────────────────

def _get_swing_wing(symbol: str = "", em: float = 0, price: float = 0) -> int:
    """
    Wing للـ Swing — نطاقات واقعية:
      SPX  : 10-20 نقطة  (افتراضي 10، مقرّب لأقرب 10)
      SPY  : 5-10 دولار  (افتراضي 5،  مقرّب لأقرب 1)
      QQQ  : 5-10 دولار  (افتراضي 5،  مقرّب لأقرب 1)
    يقرأ من الإعدادات (wing_width_swing_spx/spy/qqq) إذا وُجدت.
    """
    sym = (symbol or "").upper().strip()
    try:
        from core.database import get_setting
        key = f"wing_width_swing_{sym.lower()}" if sym in ("SPX", "SPY", "QQQ") else "wing_width_swing"
        v   = get_setting(key, "")
        if v:
            val = int(v)
            if sym == "SPX":
                return max(10, min(20, val))   # حد 10-20
            return max(5, min(10, val))         # حد 5-10
    except Exception:
        pass

    # قيم افتراضية واقعية بدون إعدادات
    if sym == "SPX":
        return 10   # 10 نقاط لـ SPX Swing
    return 5        # $5 لـ SPY/QQQ


# ── Swing Strategy Engine (unified analyze_swing path) ───────────────────────

class SwingStrategyEngine:
    """
    محرك استراتيجيات Swing عبر المسار الموحد analyze_swing().
    يعتمد على: Trend + IV Rank + Reward/Risk + EMA Structure
    لا يعتمد على: Pin Score / GEX / Gamma Walls (خاصة بـ 0DTE)
    """

    MIN_REWARD_RISK       = 0.25   # Credit: roughly credit/width >= 20% (credit/max_loss >= 25%)
    MIN_DEBIT_RATIO       = 0.05   # Debit:  debit/width >= 5%
    MAX_DEBIT_RATIO       = 0.60   # Debit:  debit/width <= 60%
    MIN_CREDIT_WIDTH_SWING = MIN_CREDIT_WIDTH_SWING  # 20% — module-level constant aliased here

    # ── قواعد اختيار الـ Strike ──────────────────────────────────────────────
    # Swing Credit rules:
    # - Iron Condor إن أضيف للـ Swing لاحقاً: short strikes عند ±1.5σ
    # - Single Credit Spreads الحالية: Bull Put / Bear Call عند ±1.0σ
    CREDIT_SIGMA_SINGLE = 1.0
    CREDIT_SIGMA_IC     = 1.5
    CREDIT_SIGMA_MULT   = CREDIT_SIGMA_SINGLE  # backward-compatible name used by current single-credit scanners

    # Swing Debit rules:
    # Long leg يجب أن تكون دلتاها بين 0.36 و 0.45، وليس هدف 0.35.
    DEBIT_DELTA_MIN     = 0.36
    DEBIT_DELTA_MAX     = 0.45
    DEBIT_LONG_DELTA    = DEBIT_DELTA_MIN  # نفضل أقل دلتا مقبولة داخل النطاق
    DEBIT_WING_POINTS   = 5     # عرض الجناح بالنقاط (ثابت)

    def __init__(self, analysis: Dict[str, Any]):
        self.analysis = analysis
        self.price    = analysis.get("price", 0)
        self.levels   = analysis.get("levels", {})
        self.chain    = analysis.get("_chain")
        self.dq       = analysis.get("data_quality", {})
        self.symbol   = analysis.get("symbol", "SPX").upper()

        _iv_raw             = self.levels.get("iv_rank")
        self.iv_rank        = _iv_raw if (_iv_raw is not None and _iv_raw > 0) else 30
        self.iv_percentile  = self.levels.get("iv_percentile")   # None = لا بيانات
        self.iv_regime      = self.levels.get("iv_regime", "Unknown")
        self.iv_regime_data = self.levels.get("iv_regime_data") or {}
        self.vix            = analysis.get("vix") or self.levels.get("vix") or 0
        self.ema20          = self.levels.get("ema20")
        self.ema50          = self.levels.get("ema50")
        self.daily_trend    = self.levels.get("daily_trend") or self.levels.get("trend") or "neutral"
        self.combined_trend = self.levels.get("combined_trend") or self.daily_trend
        self.target_dte     = self._infer_chain_dte()
        # EM من الـ chain مباشرة — لا ضرب في sqrt(DTE)
        # لأن calculate_expected_move يحسب EM الحقيقي للـ expiry المختار
        # إعادة تحجيم EM تضاعف المسافة بشكل مبالغ فيه
        _chain_em = self.levels.get("expected_move") or abs(self.price * 0.004)
        self.em   = round(_chain_em, 1)
        self.has_delta   = self.dq.get("has_delta", False)
        self.has_prices  = self.dq.get("has_prices", False)


    def _infer_chain_dte(self) -> int:
        """Infer DTE from the active Swing chain expiry.

        v3.18: no fixed legacy DTE path. The active chain is selected upstream
        by analyze_swing(): Credit uses its configured credit DTE range and
        Debit uses its configured debit DTE range.
        """
        try:
            exp = (self.chain or {}).get("expiry") or (self.chain or {}).get("expiration-date")
            if exp:
                from datetime import datetime, date
                return max(0, (datetime.strptime(exp, "%Y-%m-%d").date() - date.today()).days)
        except Exception:
            pass
        return 0

    # ── Public API ────────────────────────────────────────────────────────────

    def run(self) -> Dict[str, Any]:
        """
        يُعيد أفضل استراتيجية Swing أو No Trade.

        منطق الاختيار حسب Trend و IV:
          Bullish + IV ≥ 25 → Bull Put Credit أولاً، ثم Call Debit
          Bullish + IV < 25  → Call Debit أولاً (Credit ضعيف بـ IV منخفض)
          Bearish + IV ≥ 25 → Bear Call Credit أولاً، ثم Put Debit
          Bearish + IV < 25  → Put Debit أولاً (Credit ضعيف بـ IV منخفض)
        """
        # فحص الشروط الأساسية
        blocks = self._hard_blocks()
        if blocks:
            return self._no_trade(blocks)

        bullish = ("bullish", "strong_bullish", "bullish_pullback")
        bearish = ("bearish", "strong_bearish", "bearish_bounce")
        trend   = self.combined_trend or self.daily_trend

        # IV Percentile يحدد النوع المسموح:
        #   Low  (≤40%)  → Debit فقط
        #   High (≥50%)  → Credit فقط
        #   Unknown      → كلاهما مسموح (fallback للـ iv_rank)
        _regime    = self.iv_regime
        _iv_pct    = self.iv_percentile
        _regime_d  = self.iv_regime_data

        if _regime == "Unknown" or _iv_pct is None:
            # fallback: iv_rank >= 25 → credit مسموح
            iv_ok_for_credit = self.iv_rank >= 25
            iv_ok_for_debit  = True
        else:
            iv_ok_for_credit = _regime_d.get("credit_ok", False)
            iv_ok_for_debit  = _regime_d.get("debit_ok",  False)

        iv_pct_label = (f"IV Percentile={_iv_pct:.0f}% [{_regime}]"
                        if _iv_pct is not None
                        else f"IV Rank={self.iv_rank:.0f} [fallback]")

        # بناء قائمة المرشحين:
        # Credit-favored (>=60%) → Credit أولاً
        # Debit-favored  (<60%)  → Debit أولاً
        if trend in bullish:
            if iv_ok_for_credit:
                candidates = [self._scan_bull_put_spread()]
            else:
                candidates = [self._scan_call_debit_spread()]
        elif trend in bearish:
            if iv_ok_for_credit:
                candidates = [self._scan_bear_call_spread()]
            else:
                candidates = [self._scan_put_debit_spread()]
        else:
            return self._no_trade(["Trend محايد — لا استراتيجية Swing"])

        def _passes_quality(c: Dict) -> bool:
            if not c or c.get("no_trade"):
                return False
            if c.get("score", 0) < 50:
                return False
            # Credit Spread: يجب أن يكون الكريدت مجدياً بالنسبة لعرض الجناح
            if c.get("credit") is not None:
                return (c.get("credit_width_ratio") or 0) >= self.MIN_CREDIT_WIDTH_SWING
            # Debit Spread: debit/wing >= 5%
            if c.get("debit") is not None:
                wing = _get_swing_wing(self.symbol, self.em, self.price)
                debit = c.get("debit") or 0
                if wing > 0 and debit / wing < self.MIN_DEBIT_RATIO:
                    return False
            return True

        valid = [c for c in candidates if _passes_quality(c)]

        if not valid:
            all_scores = [(c.get("strategy","?"), c.get("score",0)) for c in candidates if c]
            best = max(all_scores, key=lambda x: x[1], default=("?", 0))
            cw_rejects = [c for c in candidates if c and c.get("credit") is not None and not c.get("credit_width_ok", True)]
            delta_rejects = [c for c in candidates if c and c.get("no_trade") and any("delta" in str(w).lower() for w in (c.get("warnings") or []) + (c.get("reasons") or []))]
            if cw_rejects:
                print(f"[CW_REJECT] swing_no_valid | strategies={[c.get('strategy') for c in cw_rejects]}")
            if delta_rejects:
                print(f"[DELTA_REJECT] swing_no_valid | strategies={[c.get('strategy') for c in delta_rejects]}")
            reasons = [
                f"لا توجد استراتيجية Swing تتجاوز الحد الأدنى",
                f"أعلى درجة: {best[0]} = {best[1]}/100",
            ]
            if cw_rejects:
                reasons.append("Credit/Width rejects: " + ", ".join(c.get("strategy","?") for c in cw_rejects))
            if delta_rejects:
                reasons.append("Delta rejects: " + ", ".join(c.get("strategy","?") for c in delta_rejects))
            return self._no_trade(reasons, all_scores={c["strategy"]: c["score"] for c in candidates if c})

        best = max(valid, key=lambda x: x.get("score", 0))
        best["trade_mode"] = "Swing"
        best["target_dte"] = self.target_dte
        best["all_scores"] = {c["strategy"]: c["score"] for c in candidates if c}
        return best

    # ── Hard Blocks ──────────────────────────────────────────────────────────

    def _hard_blocks(self) -> list:
        blocks = []
        if not self.price or self.price <= 0:
            blocks.append("لا يوجد سعر")
        # لا Swing بدون Trend واضح — الشرط الوحيد الصارم
        neutral = ("neutral", "mixed")
        if self.combined_trend in neutral and self.daily_trend in neutral:
            blocks.append("Trend محايد — Swing يحتاج اتجاه واضح")
        # ملاحظة: IV منخفض لا يرفض Swing بالكامل —
        # IV منخفض يضعّف Credit فقط ويفيد Debit (خيارات أرخص)
        # لا Swing في أول 30 دقيقة من فتح السوق — تذبذب عالٍ
        try:
            from datetime import datetime, timezone, timedelta
            now_utc = datetime.now(timezone.utc)
            m, d = now_utc.month, now_utc.day
            is_edt = 3 < m < 11 or (m == 3 and d >= 8) or (m == 11 and d < 8)
            now_et = now_utc + timedelta(hours=-4 if is_edt else -5)
            minutes_since_open = (now_et.hour - 9) * 60 + now_et.minute - 30
            if 0 <= minutes_since_open < 30:
                blocks.append(f"أول 30 دقيقة من الفتح — لا Swing (تذبذب عالٍ)")
        except Exception:
            pass
        return blocks

    # ── Bull Put Spread ───────────────────────────────────────────────────────

    def _scan_bull_put_spread(self) -> Dict[str, Any]:
        score    = 50
        reasons  = []
        warnings = []

        bullish = ("bullish", "strong_bullish", "bullish_pullback")
        if self.combined_trend not in bullish:
            return {"no_trade": True, "strategy": "Bull Put Spread", "score": 0,
                    "warnings": ["Trend غير صاعد — لا يناسب Bull Put Swing"]}

        # Trend Score
        if self.combined_trend == "strong_bullish":
            score += 25; reasons.append("Trend صاعد قوي")
        elif self.combined_trend == "bullish":
            score += 15; reasons.append("Trend صاعد")
        else:
            score += 8;  reasons.append("Trend صاعد مع تصحيح")

        # EMA Structure
        if self.ema20 and self.ema50 and self.price > self.ema20 > self.ema50:
            score += 15; reasons.append("السعر فوق EMA20 > EMA50 — هيكل صاعد قوي")
        elif self.ema20 and self.price > self.ema20:
            score += 8;  reasons.append("السعر فوق EMA20")

        # IV Rank
        if 30 <= self.iv_rank <= 70:
            score += 10; reasons.append(f"IV Rank مثالي ({self.iv_rank:.0f})")
        elif self.iv_rank > 70:
            score += 5;  reasons.append(f"IV Rank مرتفع ({self.iv_rank:.0f})")

        # Short Put: عند 1.0σ تحت السعر للـ Single Credit Swing
        sp = self._nearest_strike("put", self.price - self.em * self.CREDIT_SIGMA_MULT)
        # فلتر Delta — رفض إذا delta خارج نطاق 0.05–0.20
        if not self._check_credit_delta("put", sp, warnings):
            return {"no_trade": True, "strategy": "Bull Put Spread", "score": 0,
                    "warnings": warnings,
                    "reasons": [f"Delta خارج النطاق المقبول (0.05-0.20)"]}

        lp = self._wing_strike("put", sp, -1) if sp else None
        sp_d = self._opt("put", sp)
        lp_d = self._opt("put", lp)
        actual_wing = round(abs((sp or 0) - (lp or 0)), 2) or _get_swing_wing(self.symbol, self.em, self.price)

        credit   = round(self._mid(sp_d) - self._mid(lp_d), 2) if sp_d and lp_d else None
        if not credit or credit <= 0:
            return {"no_trade": True, "strategy": "Bull Put Spread", "score": 0,
                    "trade_mode": "Swing",
                    "warnings": ["Credit غير متاح أو صفر — لا أسعار حقيقية للـ Swing Credit"],
                    "reasons": ["missing_credit_price"]}
        max_loss = round(actual_wing - max(credit, 0), 2)
        rr       = round(credit / max_loss, 3) if max_loss > 0 else 0
        credit_width_ratio = round(credit / actual_wing, 3) if actual_wing else 0
        if credit_width_ratio < self.MIN_CREDIT_WIDTH_SWING:
            warnings.append(
                f"Credit too small: credit/width={credit_width_ratio:.1%} < "
                f"required {self.MIN_CREDIT_WIDTH_SWING:.0%} (need ≥ {actual_wing * self.MIN_CREDIT_WIDTH_SWING:.2f})"
            )
            score = 0

        score = self._check_credit_quality(credit, max_loss, rr, warnings, score)
        if rr >= self.MIN_REWARD_RISK:
            reasons.append(f"Reward/Risk = {rr:.2%}")
            score += 5

        score = max(0, min(100, score))
        return {
            "strategy":    "Bull Put Spread",
            "emoji":       "🐂",
            "score":       score,
            "trade_mode":  "Swing",
            "target_dte":  self.target_dte,
            "short_put":   sp,   "long_put":  lp,
            "credit":      credit,
            "credit_width_ratio": credit_width_ratio,
            "min_credit_width_ratio": self.MIN_CREDIT_WIDTH_SWING,
            "credit_width_ok": credit_width_ratio >= self.MIN_CREDIT_WIDTH_SWING,
            "max_gain":    credit,
            "max_loss":    max_loss,
            "reward_risk": rr,
            "setup_quality": self._setup_quality(rr, True),
            "reasons":     reasons,
            "warnings":    warnings,
            "legs_detail": [sp_d, lp_d],
            "decision":    "مقبول" if score >= 55 and credit_width_ratio >= self.MIN_CREDIT_WIDTH_SWING else "مرفوض",
        }

    # ── Bear Call Spread ──────────────────────────────────────────────────────

    def _scan_bear_call_spread(self) -> Dict[str, Any]:
        score    = 50
        reasons  = []
        warnings = []

        bearish = ("bearish", "strong_bearish", "bearish_bounce")
        if self.combined_trend not in bearish:
            return {"no_trade": True, "strategy": "Bear Call Spread", "score": 0,
                    "warnings": ["Trend غير هابط — لا يناسب Bear Call Swing"]}

        if self.combined_trend == "strong_bearish":
            score += 25; reasons.append("Trend هابط قوي")
        elif self.combined_trend == "bearish":
            score += 15; reasons.append("Trend هابط")
        else:
            score += 8;  reasons.append("Trend هابط مع ارتداد")

        if self.ema20 and self.ema50 and self.price < self.ema20 < self.ema50:
            score += 15; reasons.append("السعر تحت EMA20 < EMA50 — هيكل هابط قوي")
        elif self.ema20 and self.price < self.ema20:
            score += 8;  reasons.append("السعر تحت EMA20")

        if 30 <= self.iv_rank <= 70:
            score += 10; reasons.append(f"IV Rank مثالي ({self.iv_rank:.0f})")
        elif self.iv_rank > 70:
            score += 5;  reasons.append(f"IV Rank مرتفع ({self.iv_rank:.0f})")

        # Short Call: عند 1.0σ فوق السعر للـ Single Credit Swing
        sc = self._nearest_strike("call", self.price + self.em * self.CREDIT_SIGMA_MULT)
        # فلتر Delta — رفض إذا delta خارج نطاق 0.05–0.20
        if not self._check_credit_delta("call", sc, warnings):
            return {"no_trade": True, "strategy": "Bear Call Spread", "score": 0,
                    "warnings": warnings,
                    "reasons": [f"Delta خارج النطاق المقبول (0.05-0.20)"]}

        lc = self._wing_strike("call", sc, +1) if sc else None
        sc_d = self._opt("call", sc)
        lc_d = self._opt("call", lc)
        actual_wing = round(abs((lc or 0) - (sc or 0)), 2) or _get_swing_wing(self.symbol, self.em, self.price)

        credit   = round(self._mid(sc_d) - self._mid(lc_d), 2) if sc_d and lc_d else None
        if not credit or credit <= 0:
            return {"no_trade": True, "strategy": "Bear Call Spread", "score": 0,
                    "trade_mode": "Swing",
                    "warnings": ["Credit غير متاح أو صفر — لا أسعار حقيقية للـ Swing Credit"],
                    "reasons": ["missing_credit_price"]}
        max_loss = round(actual_wing - max(credit, 0), 2)
        rr       = round(credit / max_loss, 3) if max_loss > 0 else 0
        credit_width_ratio = round(credit / actual_wing, 3) if actual_wing else 0
        if credit_width_ratio < self.MIN_CREDIT_WIDTH_SWING:
            warnings.append(
                f"Credit too small: credit/width={credit_width_ratio:.1%} < "
                f"required {self.MIN_CREDIT_WIDTH_SWING:.0%} (need ≥ {actual_wing * self.MIN_CREDIT_WIDTH_SWING:.2f})"
            )
            score = 0

        score = self._check_credit_quality(credit, max_loss, rr, warnings, score)
        if rr >= self.MIN_REWARD_RISK:
            reasons.append(f"Reward/Risk = {rr:.2%}")
            score += 5

        score = max(0, min(100, score))
        return {
            "strategy":    "Bear Call Spread",
            "emoji":       "🐻",
            "score":       score,
            "trade_mode":  "Swing",
            "target_dte":  self.target_dte,
            "short_call":  sc,   "long_call": lc,
            "credit":      credit,
            "credit_width_ratio": credit_width_ratio,
            "min_credit_width_ratio": self.MIN_CREDIT_WIDTH_SWING,
            "credit_width_ok": credit_width_ratio >= self.MIN_CREDIT_WIDTH_SWING,
            "max_gain":    credit,
            "max_loss":    max_loss,
            "reward_risk": rr,
            "setup_quality": self._setup_quality(rr, True),
            "reasons":     reasons,
            "warnings":    warnings,
            "legs_detail": [sc_d, lc_d],
            "decision":    "مقبول" if score >= 55 and credit_width_ratio >= self.MIN_CREDIT_WIDTH_SWING else "مرفوض",
        }

    # ── Call Debit Spread ─────────────────────────────────────────────────────

    def _scan_call_debit_spread(self) -> Dict[str, Any]:
        score    = 45
        reasons  = []
        warnings = []

        bullish = ("bullish", "strong_bullish")
        if self.combined_trend not in bullish:
            return {"no_trade": True, "strategy": "Call Debit Spread", "score": 0,
                    "warnings": ["Trend غير صاعد"]}

        if self.combined_trend == "strong_bullish":
            score += 25; reasons.append("Trend صاعد قوي")
        else:
            score += 15; reasons.append("Trend صاعد")

        # IV Rank منخفض أفضل للشراء
        if self.iv_rank < 30:
            score += 20; reasons.append(f"IV Rank منخفض ({self.iv_rank:.0f}) — شراء رخيص")
        elif self.iv_rank < 50:
            score += 10; reasons.append(f"IV Rank معتدل ({self.iv_rank:.0f})")
        else:
            warnings.append(f"IV Rank مرتفع ({self.iv_rank:.0f}) — الشراء غالٍ")
            score -= 5

        if self.ema20 and self.ema50 and self.price > self.ema20 > self.ema50:
            score += 15; reasons.append("هيكل صاعد قوي")

        # Long Call: delta بين 0.36 و 0.45 | Short Call = Long + 5 نقاط
        lc = self._debit_strike_by_delta("call", target_delta=self.DEBIT_LONG_DELTA)
        if lc is None:
            warnings.append(f"لا يوجد Long Call بدلتا بين {self.DEBIT_DELTA_MIN:.2f} و {self.DEBIT_DELTA_MAX:.2f}")
        sc = self._wing_fixed("call", lc, +self.DEBIT_WING_POINTS) if lc else None
        lc_d = self._opt("call", lc)
        sc_d = self._opt("call", sc)

        wing     = round(abs((sc or 0) - (lc or 0)), 2) or _get_swing_wing(self.symbol, self.em, self.price)
        debit    = round(self._mid(lc_d) - self._mid(sc_d), 2) if lc_d and sc_d else None
        max_loss = round(debit, 2) if debit else None
        max_gain = round(wing - max(debit or 0, 0), 2) if debit else None
        rr       = round(max_gain / debit, 3) if (max_gain and debit and debit > 0) else 0

        # فلتر جودة السعر
        score, reject = self._check_debit_quality(debit, wing, warnings, score)
        if not reject and rr >= self.MIN_REWARD_RISK:
            reasons.append(f"Debit/Wing = {(debit/wing):.1%}  RR = {rr:.1%}")

        score = max(0, min(100, score))
        return {
            "strategy":    "Call Debit Spread",
            "emoji":       "📈",
            "score":       score,
            "trade_mode":  "Swing",
            "target_dte":  self.target_dte,
            "long_call":   lc,   "short_call": sc,
            "debit":       debit,
            "max_loss":    max_loss,
            "max_gain":    max_gain,
            "reward_risk": rr,
            "setup_quality": self._setup_quality(rr, False, round(debit/wing*100,1) if wing and debit else None),
            "reasons":     reasons,
            "warnings":    warnings,
            "legs_detail": [lc_d, sc_d],
            "decision":    "مقبول" if score >= 55 else "مرفوض",
        }

    # ── Put Debit Spread ──────────────────────────────────────────────────────

    def _scan_put_debit_spread(self) -> Dict[str, Any]:
        score    = 45
        reasons  = []
        warnings = []

        bearish = ("bearish", "strong_bearish")
        if self.combined_trend not in bearish:
            return {"no_trade": True, "strategy": "Put Debit Spread", "score": 0,
                    "warnings": ["Trend غير هابط"]}

        if self.combined_trend == "strong_bearish":
            score += 25; reasons.append("Trend هابط قوي")
        else:
            score += 15; reasons.append("Trend هابط")

        if self.iv_rank < 30:
            score += 20; reasons.append(f"IV Rank منخفض ({self.iv_rank:.0f}) — شراء رخيص")
        elif self.iv_rank < 50:
            score += 10; reasons.append(f"IV Rank معتدل ({self.iv_rank:.0f})")
        else:
            warnings.append(f"IV Rank مرتفع ({self.iv_rank:.0f}) — الشراء غالٍ")
            score -= 5

        if self.ema20 and self.ema50 and self.price < self.ema20 < self.ema50:
            score += 15; reasons.append("هيكل هابط قوي")

        # Long Put: |delta| بين 0.36 و 0.45 | Short Put = Long - 5 نقاط
        lp = self._debit_strike_by_delta("put", target_delta=self.DEBIT_LONG_DELTA)
        if lp is None:
            warnings.append(f"لا يوجد Long Put بدلتا بين {self.DEBIT_DELTA_MIN:.2f} و {self.DEBIT_DELTA_MAX:.2f}")
        sp = self._wing_fixed("put", lp, -self.DEBIT_WING_POINTS) if lp else None
        lp_d = self._opt("put", lp)
        sp_d = self._opt("put", sp)

        wing     = round(abs((lp or 0) - (sp or 0)), 2) or _get_swing_wing(self.symbol, self.em, self.price)
        debit    = round(self._mid(lp_d) - self._mid(sp_d), 2) if lp_d and sp_d else None
        max_loss = round(debit, 2) if debit else None
        max_gain = round(wing - max(debit or 0, 0), 2) if debit else None
        rr       = round(max_gain / debit, 3) if (max_gain and debit and debit > 0) else 0

        # فلتر جودة السعر
        score, reject = self._check_debit_quality(debit, wing, warnings, score)
        if not reject and rr >= self.MIN_REWARD_RISK:
            reasons.append(f"Debit/Wing = {(debit/wing):.1%}  RR = {rr:.1%}")

        score = max(0, min(100, score))
        return {
            "strategy":    "Put Debit Spread",
            "emoji":       "📉",
            "score":       score,
            "trade_mode":  "Swing",
            "target_dte":  self.target_dte,
            "long_put":    lp,   "short_put": sp,
            "debit":       debit,
            "max_loss":    max_loss,
            "max_gain":    max_gain,
            "reward_risk": rr,
            "setup_quality": self._setup_quality(rr, False, round(debit/wing*100,1) if wing and debit else None),
            "reasons":     reasons,
            "warnings":    warnings,
            "legs_detail": [lp_d, sp_d],
            "decision":    "مقبول" if score >= 55 else "مرفوض",
        }

    # ── Price Quality Filter ──────────────────────────────────────────────────

    def _check_credit_quality(self, credit: float, max_loss: float,
                               rr: float, warnings: list, score: int) -> int:
        """فلتر جودة Credit Spread: credit/max_loss >= MIN_REWARD_RISK"""
        if not credit or not max_loss or max_loss <= 0:
            warnings.append("Credit/MaxLoss غير محسوب — لا أسعار")
            return max(0, score - 20)
        if rr < self.MIN_REWARD_RISK:
            warnings.append(f"RR ضعيف {rr:.1%} < {self.MIN_REWARD_RISK:.0%} — Weak Setup")
            return max(0, score - 25)
        return score

    @staticmethod
    def _setup_quality(rr: float, is_credit: bool,
                       debit_ratio: float = None) -> str:
        """
        تقييم جودة الصفقة تداولياً:
        Credit Spreads:
          < 10%          → Weak (No Trade)
          10% - 15%      → Acceptable
          > 15%          → Good
        Debit Spreads:
          debit/wing < 5% → Weak
          RR > 1000%      → Unrealistic (احذر: max_profit نظري)
          debit/wing 5-30%→ Good
          debit/wing >30% → Expensive
        """
        if is_credit:
            if rr < 0.10:
                return "Weak"
            if rr < 0.15:
                return "Acceptable"
            return "Good"
        else:
            if debit_ratio is not None:
                if debit_ratio < 5:
                    return "Weak"
                if debit_ratio > 60:
                    return "Expensive"
            if rr > 10.0:
                return "Unrealistic RR"
            return "Good"

    def _check_debit_quality(self, debit: float, wing: int,
                              warnings: list, score: int) -> Tuple[int, bool]:
        """
        فلتر جودة Debit Spread:
          debit/wing >= MIN_DEBIT_RATIO  (5%)  → سعر منطقي
          debit/wing <= MAX_DEBIT_RATIO  (60%) → ليس غالياً جداً
        يُعيد (score_updated, reject: bool)
        """
        if not debit or wing <= 0:
            warnings.append("Debit/Wing غير محسوب")
            return max(0, score - 20), True
        ratio = debit / wing
        if ratio < self.MIN_DEBIT_RATIO:
            warnings.append(f"Debit/Wing = {ratio:.1%} < {self.MIN_DEBIT_RATIO:.0%} — سعر غير منطقي")
            return max(0, score - 30), True   # رفض
        if ratio > self.MAX_DEBIT_RATIO:
            warnings.append(f"Debit/Wing = {ratio:.1%} > {self.MAX_DEBIT_RATIO:.0%} — شراء غالٍ جداً")
            return max(0, score - 15), False  # تحذير فقط
        return score, False

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _nearest_strike(self, option_type: str, target: float) -> Optional[float]:
        """أقرب ضربة متاحة في الـ chain لأي هدف."""
        if not self.chain:
            return None
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        if not options:
            return None
        return min(options, key=lambda o: abs(o["strike"] - target))["strike"]

    def _wing_strike(self, option_type: str, base_strike: float, direction: int) -> Optional[float]:
        """
        يجد رجل الـ wing الأقرب للـ (base_strike + direction × wing).
        direction: +1 للـ long call، -1 للـ long put.
        يختار الضربة الأقرب المتاحة بدلاً من الضربة الحسابية.
        """
        wing    = _get_swing_wing(self.symbol, self.em, self.price)
        target  = base_strike + direction * wing
        return self._nearest_strike(option_type, target)

    def _check_credit_delta(self, option_type: str, strike: Optional[float],
                            warnings: list) -> bool:
        """فلتر Delta للـ Credit Short leg — 0.05 ≤ |delta| ≤ 0.20"""
        if not strike or not self.chain:
            return True
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        opt = next((o for o in options if o.get("strike") == strike), None)
        if not opt:
            return True
        delta = abs(opt.get("delta") or 0)
        if delta == 0:
            return True
        if delta > 0.20:
            msg = f"Short {option_type} strike={strike} delta={delta:.3f} > 0.20 — قريب من ATM"
            print(f"[DELTA_REJECT] {msg}")
            warnings.append(msg)
            return False
        if delta < 0.05:
            msg = f"Short {option_type} strike={strike} delta={delta:.3f} < 0.05 — Credit ضعيف"
            print(f"[DELTA_REJECT] {msg}")
            warnings.append(msg)
            return False
        return True

    def _nearest_strike_liquid(self, option_type: str, target: float) -> Optional[float]:
        """أقرب ضربة سائلة (bid > 0) للهدف."""
        if not self.chain:
            return None
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        liquid  = [o for o in options if (o.get("bid") or 0) > 0]
        pool    = liquid if liquid else options
        if not pool:
            return None
        return min(pool, key=lambda o: abs(o["strike"] - target))["strike"]

    def _debit_strike_by_delta(self, option_type: str,
                               target_delta: float = None) -> Optional[float]:
        """
        يختار Long Leg للـ Swing Debit Spread بناءً على نطاق دلتا صارم:
        0.36 ≤ |delta| ≤ 0.45.

        الاختيار يكون لأقرب دلتا إلى الحد الأدنى المقبول 0.36 لتقليل المخاطرة،
        مع تفضيل العقد الأفضل سيولة عند التساوي.

        لا يوجد fallback بالـ Sigma هنا؛ إذا لم تتوفر دلتا داخل النطاق لا تُختار صفقة Debit Swing.
        """
        if not self.chain:
            return None
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        if not options:
            return None

        # فلتر السيولة الأساسي
        liquid = [o for o in options if (o.get("bid") or 0) > 0 and (o.get("ask") or 0) > 0]
        pool   = liquid if liquid else options

        target = target_delta if target_delta is not None else self.DEBIT_DELTA_MIN
        candidates = []
        for o in pool:
            d_raw = o.get("delta")
            if d_raw is None:
                continue
            d = abs(d_raw)
            if self.DEBIT_DELTA_MIN <= d <= self.DEBIT_DELTA_MAX:
                candidates.append(o)

        if not candidates:
            return None

        def _liq(o):
            bid = o.get("bid") or 0
            ask = o.get("ask") or 0
            vol = o.get("volume") or 0
            oi  = o.get("open_interest") or o.get("openInterest") or 0
            spread = max(ask - bid, 0) if ask and bid else 999
            return (spread, -vol, -oi)

        return min(candidates, key=lambda o: (abs(abs(o.get("delta") or 0) - target), *_liq(o)))["strike"]

    def _wing_fixed(self, option_type: str, base_strike: Optional[float],
                    points: float) -> Optional[float]:
        """
        Short Leg = Long Leg + points (ثابت 5 نقاط للـ Debit Swing).
        يجد أقرب ضربة متاحة في الـ chain.
        """
        if base_strike is None:
            return None
        target = base_strike + points
        return self._nearest_strike(option_type, target)

    def _swing_strike(self, option_type: str, above: bool, sigma_mult: float) -> Optional[float]:
        """
        اختيار الضربة للـ Swing بناءً على σ (EM) — لا يعتمد على Delta.
        sigma_mult: مضاعف الانحراف المعياري (مثلاً 1.5σ للـ Credit)
        """
        if not self.chain:
            return None
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        # فلتر ±5σ لـ Swing (أوسع من 0DTE)
        nearby = [o for o in options if abs(o["strike"] - self.price) <= self.em * 5]
        if not nearby:
            return None
        distance = self.em * sigma_mult
        target   = self.price + distance if above else self.price - distance
        candidates = [o for o in nearby
                      if (o["strike"] >= target if above else o["strike"] <= target)]
        if not candidates:
            candidates = nearby
        return min(candidates, key=lambda o: abs(o["strike"] - target))["strike"]

    def _opt(self, option_type: str, strike: Optional[float]) -> Dict:
        if not self.chain or strike is None:
            return {}
        options = self.chain.get("calls" if option_type == "call" else "puts", [])
        for o in options:
            if o.get("strike") == strike:
                return o
        return {}

    @staticmethod
    def _mid(opt: Dict) -> float:
        if not opt:
            return 0.0
        mid = opt.get("mid")
        if mid:
            return float(mid)
        bid = opt.get("bid") or 0
        ask = opt.get("ask") or 0
        if bid and ask:
            return round((float(bid) + float(ask)) / 2, 2)
        return 0.0

    def _no_trade(self, reasons: list, all_scores: dict = None) -> Dict[str, Any]:
        return {
            "no_trade":   True,
            "strategy":   "No Trade",
            "emoji":      "⛔",
            "score":      0,
            "trade_mode": "Swing",
            "reasons":    reasons,
            "warnings":   [],
            "all_scores": all_scores or {},
        }


def run_swing_engine(
    price: float,
    iv_rank: float,
    expected_move: float,
    ema20: Optional[float],
    ema50: Optional[float],
    chain_data: Optional[Dict],
    levels: Dict[str, Any],
    data_quality: Optional[Dict] = None,
    vix: float = 0,
    symbol: str = "SPX",
    iv_percentile: Optional[float] = None,
    iv_regime: str = "Unknown",
    iv_regime_data: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Wrapper للاستدعاء من analyzer"""
    analysis = {
        "price":   price,
        "vix":     vix,
        "symbol":  symbol.upper(),
        "levels":  {
            **levels,
            "iv_rank":       iv_rank,
            "iv_percentile": iv_percentile,
            "iv_regime":     iv_regime,
            "iv_regime_data": iv_regime_data or {},
            "expected_move": expected_move,
            "ema20":         ema20,
            "ema50":         ema50,
            "vix":           vix,
        },
        "_chain":       chain_data,
        "data_quality": data_quality or {},
    }
    engine = SwingStrategyEngine(analysis)
    return engine.run()


def run_strategy_engine(
    price: float,
    pin_score: float,
    net_gex: float,
    iv_rank: float,
    expected_move: float,
    zero_gamma: Optional[float],
    ema20: Optional[float],
    ema50: Optional[float],
    chain_data: Optional[Dict],
    levels: Dict[str, Any],
    data_quality: Optional[Dict] = None,
    vix: float = 0,
    is_market_open: bool = True,
    symbol: str = "SPX",
) -> Dict[str, Any]:
    """Wrapper للاستدعاء من analyzer"""
    analysis = {
        "price":          price,
        "pin_score":      pin_score,
        "vix":            vix,
        "is_market_open": is_market_open,
        "symbol":         symbol.upper(),
        "levels":         {
            **levels,
            "net_gex":       net_gex,
            "iv_rank":       iv_rank,
            "expected_move": expected_move,
            "zero_gamma":    zero_gamma,
            "ema20":         ema20,
            "ema50":         ema50,
            "vix":           vix,
        },
        "_chain":       chain_data,
        "data_quality": data_quality or {},
    }
    engine = StrategyEngine(analysis)
    return engine.run()


def format_strategy_telegram(strategy: Dict[str, Any], price: float) -> str:
    """تنسيق الاستراتيجية لرسالة تيليغرام"""
    if not strategy:
        return ""

    emoji    = strategy.get("emoji", "🎯")
    name     = strategy.get("strategy", "")
    score    = strategy.get("score", 0)
    decision = strategy.get("decision", "")
    reasons  = strategy.get("reasons", [])
    warnings = strategy.get("warnings", [])
    regime   = strategy.get("regime", "")

    lines = [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"{emoji} <b>الاستراتيجية المختارة: {name}</b>",
        f"🧭 الثقة: <b>{score}/100</b>  |  {decision}",
    ]

    if regime:
        lines.append(f"🌍 بيئة السوق: {regime}")

    if reasons:
        lines.append("\n📊 <b>أسباب الاختيار:</b>")
        for r in reasons:
            lines.append(f"  • {r}")

    if not strategy.get("no_trade"):
        lines.append("\n📍 <b>التنفيذ المقترح:</b>")

        if name == "Iron Condor":
            sc, lc = strategy.get("short_call"), strategy.get("long_call")
            sp, lp = strategy.get("short_put"),  strategy.get("long_put")
            if sc and lc: lines.append(f"  📞 Call: Sell {sc:,.0f} / Buy {lc:,.0f}")
            if sp and lp: lines.append(f"  📟 Put:  Sell {sp:,.0f} / Buy {lp:,.0f}")

        elif name == "Bull Put Spread":
            sp, lp = strategy.get("short_put"), strategy.get("long_put")
            if sp and lp: lines.append(f"  📟 Sell {sp:,.0f} Put / Buy {lp:,.0f} Put")

        elif name == "Bear Call Spread":
            sc, lc = strategy.get("short_call"), strategy.get("long_call")
            if sc and lc: lines.append(f"  📞 Sell {sc:,.0f} Call / Buy {lc:,.0f} Call")

        elif name == "Call Debit Spread":
            lc, sc = strategy.get("long_call"), strategy.get("short_call")
            if lc and sc: lines.append(f"  📞 Buy {lc:,.0f} Call / Sell {sc:,.0f} Call")

        elif name == "Put Debit Spread":
            lp, sp = strategy.get("long_put"), strategy.get("short_put")
            if lp and sp: lines.append(f"  📟 Buy {lp:,.0f} Put / Sell {sp:,.0f} Put")

        credit   = strategy.get("credit")
        debit    = strategy.get("debit")
        max_loss = strategy.get("max_loss")
        max_gain = strategy.get("max_gain")
        pop      = strategy.get("pop")

        if credit:
            credit_worst = strategy.get("credit_worst")
            lines.append(f"  💵 Credit (mid): ~{credit:.2f}")
            if credit_worst:
                lines.append(f"  ⚡ Credit (worst-case bid/ask): ~{credit_worst:.2f}")
        if debit:
            debit_worst = strategy.get("debit_worst")
            lines.append(f"  💵 Debit (mid): ~{debit:.2f}")
            if debit_worst:
                lines.append(f"  ⚡ Debit (worst-case bid/ask): ~{debit_worst:.2f}")
        if max_loss: lines.append(f"  ⚖️ Max Loss: ~{max_loss:.2f}")
        if max_gain: lines.append(f"  🎯 Max Gain: ~{max_gain:.2f}")
        if pop:      lines.append(f"  📈 POP: ~{pop:.0f}%")

        # جدول Bid/Ask/Spread لكل leg
        legs = strategy.get("legs_detail", [])
        if legs:
            lines.append("\n📋 <b>تفاصيل الـ Legs:</b>")
            for leg in legs:
                if not leg:
                    continue
                strike = leg.get("strike")
                bid = leg.get("bid")
                ask = leg.get("ask")
                sp_pct = leg.get("spread_pct")
                sp_warn = " ⛔" if (sp_pct or 0) > 50 else (" ⚠️" if (sp_pct or 0) > 25 else "")
                lines.append(
                    f"  Strike {strike:,.0f}  bid={bid or '—'}  ask={ask or '—'}  spread={sp_pct:.0f}%{sp_warn}" if sp_pct else
                    f"  Strike {strike:,.0f}  bid={bid or '—'}  ask={ask or '—'}"
                )

    if warnings:
        lines.append("\n⚠️ <b>تحذيرات:</b>")
        for w in warnings:
            lines.append(f"  • {w}")

    # درجات باقي الاستراتيجيات
    all_scores = strategy.get("all_scores", {})
    if all_scores:
        lines.append("\n📊 <b>مقارنة الاستراتيجيات:</b>")
        for s_name, s_score in sorted(all_scores.items(), key=lambda x: x[1], reverse=True):
            marker = "◀" if s_name == name else " "
            lines.append(f"  {marker} {s_name}: {s_score}/100")

    # Market Hours Notice
    if strategy.get("no_trade") and "مغلق" in " ".join(strategy.get("reasons", [])):
        lines.append("\nℹ️ <i>هذا تحليل مرجعي — السوق مغلق حالياً</i>")

    return "\n".join(lines)
