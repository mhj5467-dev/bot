"""
Swing Pipeline Diagnostic Codes — RC14

قاموس موحد لجميع رموز التشخيص في مسار analyze_swing().
يُستخدم في:
  - analyzer.py        (pipeline_stop_code, diagnostic_code, entry_protection)
  - database.py        (paper_trades schema)
  - UI (Swing Diag)    (عرض السبب بدل Not evaluated)

# pipeline_stop_code
# NOT_EVALUATED يعني المحرك لم يُطلب أصلاً (توقف Pipeline قبله)
# UNAVAILABLE   يعني المحرك طُلب لكن فشلت البيانات

# ds_evaluation / smc_evaluation
# NOT_EVALUATED → Pipeline توقف قبل الوصول لهذه المرحلة
# UNAVAILABLE   → المرحلة بدأت لكن البيانات لم تكتمل
# الفرق مهم للإحصاءات: UNAVAILABLE يدخل في denominator الـ Coverage
"""

# ── pipeline_stop_code ────────────────────────────────────────────────────────
PIPELINE_STOP_CODES = {
    "STOPPED_AT_4H_TREND":       "Stopped at 4H trend filter",
    "STOPPED_AT_MANUAL_BLOCK":   "Stopped at manual block (earnings/news)",
    "STOPPED_AT_SPX_GUARD":      "Stopped: SPX is reserved for 0DTE only",
    "PRICE_UNAVAILABLE":         "Live price fetch failed — cannot proceed",
    "CHAIN_UNAVAILABLE":         "Option chain fetch failed or returned empty",
    "NO_VALID_EXPIRY":           "Chain fetched but no expiry found in DTE range",
    "NO_STRATEGY_CANDIDATE":     "Strategy engine produced no candidate",
    "REJECTED_BY_DELTA":         "Candidate rejected by Delta filter",
    "REJECTED_BY_LIQUIDITY":     "Candidate rejected by Liquidity filter",
    "REJECTED_BY_CREDIT_WIDTH":  "Candidate rejected by Credit/Width ratio",
    "REJECTED_BY_DEBIT_WIDTH":   "Candidate rejected by Debit/Width ratio",
    "REJECTED_BY_SCORE":         "Candidate score below minimum threshold",
    "REJECTED_BY_EVENT":         "Rejected by high-risk event filter",
    "REJECTED_BY_SMC":           "Rejected by SMC conflict filter",
    "REJECTED_BY_DS":            "Rejected by Demand/Supply hard reject",
    "SMC_EVALUATED":             "Pipeline completed — SMC/DS evaluated",
}

# ── smc_evaluation ────────────────────────────────────────────────────────────
SMC_EVALUATION = {
    "PASSED":          "SMC evaluation completed — aligned with trade direction",
    "FAILED":          "SMC evaluation completed — conflict with trade direction",
    "NOT_EVALUATED":   "SMC not evaluated — pipeline stopped before this stage",
    "UNAVAILABLE":     "SMC engine ran but data fetch failed",
    "LEGACY_UNKNOWN":  "Trade created before SMC evaluation tracking was added",
}

# ── ds_evaluation ─────────────────────────────────────────────────────────────
DS_EVALUATION = {
    "PASSED":          "D/S evaluation completed — no conflict found",
    "NO_ACTIVE_ZONE":  "D/S engine ran successfully — no active zone near price",
    "WARNING_ZONE":    "D/S engine ran — low-confidence zone near price (warning only)",
    "HARD_REJECTED":   "D/S engine ran — strong conflicting zone triggered hard reject",
    "UNAVAILABLE":     "D/S engine ran but data fetch/compute failed",
    "NOT_EVALUATED":   "D/S not evaluated — pipeline stopped before this stage",
    "LEGACY_UNKNOWN":  "Trade created before D/S evaluation tracking was added",
}

# ds_unavailable_reason — السبب التقني عند ds_evaluation = UNAVAILABLE
DS_UNAVAILABLE_REASON = {
    "INSUFFICIENT_CANDLES": "Not enough candles to compute zones",
    "DATA_FETCH_FAILED":     "DXLink/Yahoo candle fetch returned empty or errored",
    "ENGINE_ERROR":          "Exception raised inside demand_supply engine",
    "CACHE_UNAVAILABLE":     "Candle cache expired and live fetch failed",
    "UNKNOWN":               "Unknown error in D/S engine",
}

# ── entry_protection ──────────────────────────────────────────────────────────
ENTRY_PROTECTION = {
    "PASSED":               "SMC and D/S both passed",
    "PASSED_WITH_WARNING":  "SMC passed but D/S unavailable or low-confidence zone",
    "HARD_REJECTED":        "Hard rejected by SMC conflict or D/S zone",
    "NOT_EVALUATED":        "Entry protection not evaluated — pipeline stopped earlier",
    "LEGACY_UNKNOWN":       "Trade created before entry protection tracking was added",
}

# ── diagnostic_code ───────────────────────────────────────────────────────────
DIAGNOSTIC_CODES = {
    "DS_PASSED":            "D/S evaluation passed — no conflict",
    "DS_NOT_EVALUATED":     "D/S was not evaluated (pipeline stopped before it)",
    "DS_UNAVAILABLE":       "D/S engine ran but failed to complete",
    "DS_NO_ACTIVE_ZONE":    "D/S ran — no active zone near current price",
    "DS_WARNING_ZONE":      "D/S ran — low-confidence zone present (warning only)",
    "DS_CONFLICT":          "D/S ran — strong conflicting zone triggered reject",
    "SMC_CONFLICT":         "SMC hard reject — 1H and 15m both against trade",
    "SMC_NOT_EVALUATED":    "SMC not evaluated — pipeline stopped earlier",
}

# ── helpers ───────────────────────────────────────────────────────────────────

def ds_eval_from_context(ds_ctx: dict) -> tuple:
    """
    يُحدد ds_evaluation و ds_unavailable_reason من نتيجة detect_swing_demand_supply_zones().
    يُعيد (ds_evaluation, ds_unavailable_reason).

    الفرق الجوهري:
      available=False          → UNAVAILABLE (المحرك حاول لكن فشل)
      available=True, no zone  → NO_ACTIVE_ZONE (المحرك نجح، لا منطقة فعالة)
      available=True, zone     → PASSED أو WARNING_ZONE أو HARD_REJECTED
    """
    if not ds_ctx:
        return "NOT_EVALUATED", None

    available = bool(ds_ctx.get("available"))

    if not available:
        err = str(ds_ctx.get("error") or "")
        if "candle" in err.lower() or "ohlc" in err.lower() or "fetch" in err.lower():
            reason = "DATA_FETCH_FAILED"
        elif "insufficient" in err.lower() or "not enough" in err.lower():
            reason = "INSUFFICIENT_CANDLES"
        elif err:
            reason = "ENGINE_ERROR"
        else:
            reason = "UNKNOWN"
        return "UNAVAILABLE", reason

    # المحرك نجح — هل وجد منطقة؟
    nd = ds_ctx.get("nearest_demand") or {}
    ns = ds_ctx.get("nearest_supply") or {}
    has_zone = bool(nd or ns)

    if not has_zone:
        return "NO_ACTIVE_ZONE", None

    # منطقة موجودة — فحص مستوى الثقة
    d_conf = int(nd.get("confidence") or 0)
    s_conf = int(ns.get("confidence") or 0)
    max_conf = max(d_conf, s_conf)

    # حدود الثقة (مقصودة ومثبّتة):
    #   >= 70 → PASSED       (منطقة قوية — تأثير واضح على السعر)
    #   40-69 → WARNING_ZONE (منطقة متوسطة — تحذير فقط، لا رفض)
    #   < 40  → NO_ACTIVE_ZONE (منطقة ضعيفة — تُعامَل كغياب المنطقة)
    if max_conf >= 70:
        return "PASSED", None
    elif max_conf >= 40:
        return "WARNING_ZONE", None
    else:
        return "NO_ACTIVE_ZONE", None
