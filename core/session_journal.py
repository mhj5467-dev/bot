"""Daily Session Journal / Decision Memory.

RC15i.9 diagnostics-only addition.
Records one row per symbol per analysis cycle to data/session_journal_YYYY-MM-DD.csv
and data/session_journal_YYYY-MM-DD.jsonl so the user can reconstruct what happened
from the start of the trading day to the end.

This module is intentionally best-effort: journal failures must never affect trading,
analysis, paper registration, exits, or UI refresh.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - old Python fallback
    ZoneInfo = None  # type: ignore

try:
    from core.app_paths import get_app_root, get_data_dir
    ROOT_DIR = get_app_root()
    DATA_DIR = get_data_dir()
except Exception:
    ROOT_DIR = Path(__file__).resolve().parents[1]
    DATA_DIR = ROOT_DIR / "data"

SYMBOLS: List[str] = ["SPX", "SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD"]

# RC15i.9d: strict journal scope.
# 0DTE journal rows are SPX-only. ETF/index diagnostics such as SPY/QQQ/IWM
# must not be written as 0DTE rows, because they caused misleading IWM:0DTE entries.
# All non-SPX tracked symbols are recorded only under the Swing track.
BASE_TRACKS = [("SPX", "0DTE")] + [(s, "Swing") for s in ("SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD")]
OPTIONAL_0DTE_SYMBOLS: tuple = ()

CSV_FIELDS: List[str] = [
    "cycle_id",
    "timestamp_et",
    "date_et",
    "time_et",
    "symbol",
    "mode",
    "track",
    "strategy",
    "best_candidate",
    "candidate_scores",
    "price",
    "score",
    "required_score",
    "final_decision",
    "pipeline_stop",
    "pipeline_code",
    "registered",
    "paper_trade_id",
    "insert_attempted",
    "final_action",
    "reject_reason",
    "reject_detail",
    "pin_score",
    "net_gex",
    "expected_move",
    "iv_rank",
    "iv_percentile",
    "iv_regime",
    "dte",
    "expiry_date",
    "chain_type",
    "trend_4h",
    "trend_confirmed",
    "data_quality_mode",
    "liquidity_status",
    "credit",
    "debit",
    "credit_width_ratio",
    "rc15f_decision",
    "rc15f_reason",
    "smc_status",
    "demand_supply_status",
    "entry_protection",
    "diagnostic_code",
    "notes",
    # ── RC15j Phase 2A — D/S DXLink Diagnostic Audit ─────────────────────────
    "ds_source",
    "ds_symbol_requested",
    "ds_symbol_resolved",
    "ds_1h_available",
    "ds_15m_available",
    "ds_1h_candles_count",
    "ds_15m_candles_count",
    "atr_15m",
    "nearest_demand_low",
    "nearest_demand_high",
    "distance_to_demand_points",
    "nearest_supply_low",
    "nearest_supply_high",
    "distance_to_supply_points",
    "near_threshold",
    "ds_not_evaluated_reason",
    "ds_location_decision",
    # RC15j Phase 2B — SPY proxy fields
    "ds_proxy_source_symbol",
    "ds_proxy_target_symbol",
    "ds_proxy_ratio",
    "ds_proxy_reason",
    "spy_nearest_demand_low",
    "spy_nearest_demand_high",
    "spy_nearest_supply_low",
    "spy_nearest_supply_high",
    # RC15j Phase 2C — Put Debit candidate diagnostics
    "put_debit_score",
    "put_debit_blocked_code",
    "put_debit_pipeline_stop",
    "put_debit_ds_location",
    "put_debit_reject_reason",
    # M1 — Tastytrade shadow provider diagnostics (no trade impact)
    "tasty_shadow_enabled",
    "tasty_shadow_ok",
    "tasty_shadow_source",
    "tasty_shadow_reason",
    "tasty_shadow_underlying_requested_symbol",
    "tasty_shadow_underlying_event_symbol",
    "tasty_shadow_underlying_bid",
    "tasty_shadow_underlying_ask",
    "tasty_shadow_underlying_mid",
    "tasty_shadow_underlying_last",
    "tasty_shadow_price_diff_points",
    "tasty_shadow_price_diff_pct",
    "tasty_shadow_price_mismatch",
    "tasty_shadow_warning",
    "tasty_shadow_mismatch_threshold_pts",
    "tasty_shadow_option_quote_count",
    "tasty_shadow_leg_symbols",
    "tasty_shadow_note",
    # RC15j Phase 2B.1 — Structural zone shadow (SPY 1H + 15m, no trade impact)
    "struct_shadow_available",
    "struct_shadow_converted",
    "struct_shadow_reason",
    "struct_shadow_zone_method",
    "struct_shadow_spx_price",    # SPX live price used for ratio
    "struct_shadow_spy_price",    # SPY live price used for ratio
    "struct_shadow_ratio",        # = spx_price / spy_price (live, not fixed)
    "struct_put_debit_shadow_allowed",
    "struct_put_debit_shadow_block_code",
    "struct_put_debit_shadow_block_reason",
    "struct_put_debit_shadow_primary_tf",
    "struct_put_debit_shadow_context_tf",
    # 1H structural zones — SPX-scaled (primary) + raw SPY (audit)
    "struct_1h_candle_count",
    "struct_1h_demand_found",
    "struct_1h_demand_low",       # SPX-scaled
    "struct_1h_demand_high",      # SPX-scaled
    "struct_1h_demand_bos_lvl",   # SPX-scaled
    "struct_1h_demand_spy_low",   # raw SPY
    "struct_1h_demand_spy_high",  # raw SPY
    "struct_1h_demand_spy_bos_lvl",
    "struct_1h_demand_base_t",
    "struct_1h_demand_base_idx",
    "struct_1h_demand_bos_t",
    "struct_1h_demand_bos_idx",
    "struct_1h_demand_sh_cnt",
    "struct_1h_demand_sl_cnt",
    "struct_1h_demand_struct",
    "struct_1h_demand_bos_candle_close",
    "struct_1h_demand_sh_local_cnt",
    "struct_1h_demand_sl_local_cnt",
    "struct_1h_demand_local_lookback",
    "struct_1h_demand_last2_sh_t",
    "struct_1h_demand_last2_sl_t",
    # 1H Demand — Phase 2B.2 BOS locality
    "struct_1h_demand_bos_level_idx",
    "struct_1h_demand_bos_level_time",
    "struct_1h_demand_bos_level_age_bars",
    "struct_1h_demand_max_bos_lookback_bars",
    "struct_1h_demand_bos_local_valid",
    "struct_1h_demand_zone_status",
    "struct_1h_demand_zone_reject_reason",
    "struct_1h_demand_mitigation_status",
    "struct_1h_demand_tested",
    "struct_1h_demand_mitigated",
    "struct_1h_demand_invalidated",
    "struct_1h_demand_touches",
    "struct_1h_demand_last_touch_idx",
    "struct_1h_demand_last_touch_time",
    "struct_1h_demand_invalidated_idx",
    "struct_1h_demand_invalidated_time",
    "struct_1h_demand_structure_event",
    "struct_1h_demand_structure_direction",
    "struct_1h_demand_structure_bias_before",
    "struct_1h_supply_found",
    "struct_1h_supply_low",
    "struct_1h_supply_high",
    "struct_1h_supply_bos_lvl",
    "struct_1h_supply_spy_low",
    "struct_1h_supply_spy_high",
    "struct_1h_supply_spy_bos_lvl",
    "struct_1h_supply_base_t",
    "struct_1h_supply_base_idx",
    "struct_1h_supply_bos_t",
    "struct_1h_supply_bos_idx",
    "struct_1h_supply_sh_cnt",
    "struct_1h_supply_sl_cnt",
    "struct_1h_supply_struct",
    "struct_1h_supply_bos_candle_close",
    "struct_1h_supply_sh_local_cnt",
    "struct_1h_supply_sl_local_cnt",
    "struct_1h_supply_local_lookback",
    "struct_1h_supply_last2_sh_t",
    "struct_1h_supply_last2_sl_t",
    # 1H Supply — Phase 2B.2 BOS locality
    "struct_1h_supply_bos_level_idx",
    "struct_1h_supply_bos_level_time",
    "struct_1h_supply_bos_level_age_bars",
    "struct_1h_supply_max_bos_lookback_bars",
    "struct_1h_supply_bos_local_valid",
    "struct_1h_supply_zone_status",
    "struct_1h_supply_zone_reject_reason",
    "struct_1h_supply_mitigation_status",
    "struct_1h_supply_tested",
    "struct_1h_supply_mitigated",
    "struct_1h_supply_invalidated",
    "struct_1h_supply_touches",
    "struct_1h_supply_last_touch_idx",
    "struct_1h_supply_last_touch_time",
    "struct_1h_supply_invalidated_idx",
    "struct_1h_supply_invalidated_time",
    "struct_1h_supply_structure_event",
    "struct_1h_supply_structure_direction",
    "struct_1h_supply_structure_bias_before",
    # 1H conflict diagnostics (4 filters)
    "struct_1h_demand_supply_overlap",
    "struct_1h_overlap_low",
    "struct_1h_overlap_high",
    "struct_1h_price_in_demand",
    "struct_1h_price_in_supply",
    "struct_1h_clean_gap",
    "struct_1h_min_clean_gap_pts",
    "struct_1h_demand_bos_close_spx",
    "struct_1h_supply_bos_close_spx",
    "struct_1h_demand_bos_stale",
    "struct_1h_supply_bos_stale",
    "struct_1h_max_bos_dist_pts",
    "struct_1h_entry_distance_threshold_pts",
    "struct_1h_distance_to_demand_zone",
    "struct_1h_distance_to_supply_zone",
    "struct_1h_price_at_demand_valid",
    "struct_1h_price_at_supply_valid",
    "struct_1h_price_in_active_demand",
    "struct_1h_price_in_active_supply",
    "struct_1h_demand_active_valid",
    "struct_1h_supply_active_valid",
    "struct_1h_zone_state_reason",
    "struct_1h_price_at_active_demand_valid",
    "struct_1h_price_at_active_supply_valid",
    "struct_1h_put_debit_shadow_allowed",
    "struct_1h_put_debit_shadow_block_code",
    "struct_1h_put_debit_shadow_block_reason",
    "struct_1h_structural_conflict",
    "struct_1h_structural_conflict_reason",
    # 15m structural zones
    "struct_15m_candle_count",
    "struct_15m_demand_found",
    "struct_15m_demand_low",
    "struct_15m_demand_high",
    "struct_15m_demand_bos_lvl",
    "struct_15m_demand_spy_low",
    "struct_15m_demand_spy_high",
    "struct_15m_demand_spy_bos_lvl",
    "struct_15m_demand_base_t",
    "struct_15m_demand_base_idx",
    "struct_15m_demand_bos_t",
    "struct_15m_demand_bos_idx",
    "struct_15m_demand_sh_cnt",
    "struct_15m_demand_sl_cnt",
    "struct_15m_demand_struct",
    "struct_15m_demand_bos_candle_close",
    "struct_15m_demand_sh_local_cnt",
    "struct_15m_demand_sl_local_cnt",
    "struct_15m_demand_local_lookback",
    "struct_15m_demand_last2_sh_t",
    "struct_15m_demand_last2_sl_t",
    # 15m Demand — Phase 2B.2 BOS locality
    "struct_15m_demand_bos_level_idx",
    "struct_15m_demand_bos_level_time",
    "struct_15m_demand_bos_level_age_bars",
    "struct_15m_demand_max_bos_lookback_bars",
    "struct_15m_demand_bos_local_valid",
    "struct_15m_demand_zone_status",
    "struct_15m_demand_zone_reject_reason",
    "struct_15m_demand_mitigation_status",
    "struct_15m_demand_tested",
    "struct_15m_demand_mitigated",
    "struct_15m_demand_invalidated",
    "struct_15m_demand_touches",
    "struct_15m_demand_last_touch_idx",
    "struct_15m_demand_last_touch_time",
    "struct_15m_demand_invalidated_idx",
    "struct_15m_demand_invalidated_time",
    "struct_15m_demand_structure_event",
    "struct_15m_demand_structure_direction",
    "struct_15m_demand_structure_bias_before",
    "struct_15m_supply_found",
    "struct_15m_supply_low",
    "struct_15m_supply_high",
    "struct_15m_supply_bos_lvl",
    "struct_15m_supply_spy_low",
    "struct_15m_supply_spy_high",
    "struct_15m_supply_spy_bos_lvl",
    "struct_15m_supply_base_t",
    "struct_15m_supply_base_idx",
    "struct_15m_supply_bos_t",
    "struct_15m_supply_bos_idx",
    "struct_15m_supply_sh_cnt",
    "struct_15m_supply_sl_cnt",
    "struct_15m_supply_struct",
    "struct_15m_supply_bos_candle_close",
    "struct_15m_supply_sh_local_cnt",
    "struct_15m_supply_sl_local_cnt",
    "struct_15m_supply_local_lookback",
    "struct_15m_supply_last2_sh_t",
    "struct_15m_supply_last2_sl_t",
    # 15m Supply — Phase 2B.2 BOS locality
    "struct_15m_supply_bos_level_idx",
    "struct_15m_supply_bos_level_time",
    "struct_15m_supply_bos_level_age_bars",
    "struct_15m_supply_max_bos_lookback_bars",
    "struct_15m_supply_bos_local_valid",
    "struct_15m_supply_zone_status",
    "struct_15m_supply_zone_reject_reason",
    "struct_15m_supply_mitigation_status",
    "struct_15m_supply_tested",
    "struct_15m_supply_mitigated",
    "struct_15m_supply_invalidated",
    "struct_15m_supply_touches",
    "struct_15m_supply_last_touch_idx",
    "struct_15m_supply_last_touch_time",
    "struct_15m_supply_invalidated_idx",
    "struct_15m_supply_invalidated_time",
    "struct_15m_supply_structure_event",
    "struct_15m_supply_structure_direction",
    "struct_15m_supply_structure_bias_before",
    # 15m conflict diagnostics (4 filters)
    "struct_15m_demand_supply_overlap",
    "struct_15m_overlap_low",
    "struct_15m_overlap_high",
    "struct_15m_price_in_demand",
    "struct_15m_price_in_supply",
    "struct_15m_clean_gap",
    "struct_15m_min_clean_gap_pts",
    "struct_15m_demand_bos_close_spx",
    "struct_15m_supply_bos_close_spx",
    "struct_15m_demand_bos_stale",
    "struct_15m_supply_bos_stale",
    "struct_15m_max_bos_dist_pts",
    "struct_15m_entry_distance_threshold_pts",
    "struct_15m_distance_to_demand_zone",
    "struct_15m_distance_to_supply_zone",
    "struct_15m_price_at_demand_valid",
    "struct_15m_price_at_supply_valid",
    "struct_15m_price_in_active_demand",
    "struct_15m_price_in_active_supply",
    "struct_15m_demand_active_valid",
    "struct_15m_supply_active_valid",
    "struct_15m_zone_state_reason",
    "struct_15m_price_at_active_demand_valid",
    "struct_15m_price_at_active_supply_valid",
    "struct_15m_put_debit_shadow_allowed",
    "struct_15m_put_debit_shadow_block_code",
    "struct_15m_put_debit_shadow_block_reason",
    "struct_15m_structural_conflict",
    "struct_15m_structural_conflict_reason",
]


def _now_et() -> datetime:
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("America/New_York"))
        except Exception:
            pass
    return datetime.now()


def _to_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _fmt(value: Any) -> str:
    """CSV-safe compact text."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, (list, tuple)):
        return "; ".join(_fmt(v) for v in value if v is not None)[:500]
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, default=str)[:500]
        except Exception:
            return str(value)[:500]
    return str(value).replace("\r", " ").replace("\n", " ")[:500]


def _is_placeholder_text(txt: str) -> bool:
    t = str(txt or "").strip().lower()
    return t in {"?", "-", "n/a", "na", "none", "null"}


def _first_text(*values: Any) -> str:
    """Return first meaningful compact text, ignoring placeholder markers like '?'."""
    for value in values:
        txt = _fmt(value).strip()
        if txt and not _is_placeholder_text(txt):
            return txt
    return ""


def _list_first(value: Any) -> str:
    if isinstance(value, (list, tuple)) and value:
        return _fmt(value[0])
    return _fmt(value)


def _safe_get(d: Any, key: str, default: Any = None) -> Any:
    return d.get(key, default) if isinstance(d, dict) else default


def _track_key(symbol: str, mode: str) -> str:
    return f"{str(symbol or '').upper()}:{str(mode or '').upper()}"


def _normalize_mode(value: Any) -> str:
    txt = str(value or "").upper().replace("-", "").replace(" ", "").strip()
    if txt in {"0DTE", "ZERO_DTE", "ZERODTE", "INTRADAY"}:
        return "0DTE"
    if txt in {"SWING", "SWINGTRADE", "SWING_TRADING"}:
        return "SWING"
    return txt


def _all_statuses(analysis: Dict[str, Any], symbol: str, mode: Optional[str] = None) -> List[Dict[str, Any]]:
    statuses = _safe_get(analysis, "_paper_insert_status", []) or []
    sym = str(symbol or "").upper()
    mode_u = _normalize_mode(mode) if mode else ""
    out: List[Dict[str, Any]] = []
    for s in statuses:
        if str(_safe_get(s, "symbol", "")).upper() != sym:
            continue
        if mode_u:
            row_mode = _normalize_mode(_first_text(_safe_get(s, "trade_mode"), _safe_get(s, "mode")))
            if mode_u == "0DTE" and sym != "SPX":
                # RC15i.9c: optional ETF 0DTE journal rows require explicit 0DTE metadata.
                # Older/missing mode rows are treated as Swing/diagnostic to avoid false 0DTE rows.
                if row_mode != "0DTE":
                    continue
            elif row_mode and row_mode != mode_u:
                continue
        out.append(s)
    return out


def _latest_status(analysis: Dict[str, Any], symbol: str, mode: Optional[str] = None) -> Dict[str, Any]:
    matches = _all_statuses(analysis, symbol, mode)
    if not matches:
        return {}
    # Prefer inserted/selected records, otherwise use the last diagnostic record.
    inserted = [s for s in matches if _safe_get(s, "inserted")]
    if inserted:
        return inserted[-1]
    selected = [s for s in matches if _safe_get(s, "selected")]
    if selected:
        return selected[-1]
    return matches[-1]


def _symbol_data(analysis: Dict[str, Any], symbol: str, mode: str = "0DTE") -> Dict[str, Any]:
    if str(mode or "").upper() == "SWING":
        sw = _swing_diag_for_symbol(analysis, symbol)
        return sw if isinstance(sw, dict) else {}
    if symbol == "SPX":
        return analysis
    return _safe_get(analysis, symbol.lower(), {}) or {}


def _swing_diag_for_symbol(analysis: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    diag = _safe_get(analysis, "_swing_diag", {}) or {}
    if isinstance(diag, dict):
        return _safe_get(diag, str(symbol or "").upper(), {}) or {}
    return {}


def _strategy_for_track(analysis: Dict[str, Any], symbol: str, mode: str) -> Dict[str, Any]:
    if str(mode or "").upper() == "SWING":
        sw = _swing_diag_for_symbol(analysis, symbol)
        st = _safe_get(sw, "strategy", {}) or {}
        if isinstance(st, dict) and st:
            return st
        # For rejected Swing candidates analyzer may not keep strategy_result.
        # Build a compact diagnostic pseudo-strategy from swing_diag/rejected/all_scores.
        return {
            "strategy": _best_candidate_name(sw) or "No Trade",
            "score": _safe_get(sw, "score", 0),
            "no_trade": True,
            "reasons": _safe_get(sw, "rejected", []) or [],
            "all_scores": _safe_get(sw, "all_scores", {}) or {},
            "trade_mode": "Swing",
            "dte": _safe_get(sw, "dte"),
        }
    data = _symbol_data(analysis, symbol, mode)
    return _safe_get(data, "strategy", {}) or {}


def _rc15f_for_symbol(analysis: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    rows = _safe_get(analysis, "_rc15f_swing_ict_smc_ema", []) or []
    for row in reversed(rows):
        if str(_safe_get(row, "symbol", "")).upper() == symbol:
            return row
    return {}


def _best_candidate_name(source: Any) -> str:
    scores = _safe_get(source, "all_scores", {})
    if isinstance(scores, dict) and scores:
        try:
            name, score = max(scores.items(), key=lambda kv: float(kv[1] or 0))
            return f"{name} ({float(score or 0):.0f}/100)"
        except Exception:
            pass
    # Parse Arabic/English diagnostic reason like: أعلى درجة: Bear Call Spread = 0/100
    txt = " ".join(_fmt(x) for x in (_safe_get(source, "rejected", []) or []))
    import re
    m = re.search(r"أعلى درجة:\s*([^=|]+)=\s*([\d.]+)\s*/\s*100", txt)
    if not m:
        m = re.search(r"best(?: candidate| score)?\s*:?\s*([^=|]+)=\s*([\d.]+)\s*/\s*100", txt, re.I)
    if m:
        return f"{m.group(1).strip()} ({m.group(2).strip()}/100)"
    return ""


def _candidate_scores(source: Any, strategy: Dict[str, Any]) -> str:
    scores = _safe_get(strategy, "all_scores", {}) or _safe_get(source, "all_scores", {}) or {}
    if isinstance(scores, dict) and scores:
        try:
            return "; ".join(f"{k}={float(v or 0):.0f}" for k, v in sorted(scores.items()))
        except Exception:
            return _fmt(scores)
    # Fall back to reason text with highest candidate info.
    best = _best_candidate_name(source)
    return best


def _required_score(strategy_name: str, mode: str) -> int:
    # Diagnostics only. Keep broad defaults to avoid importing strategy code in journal.
    # The actual trading thresholds remain in strategy_engine/trade_monitor.
    if str(mode or "").upper() == "SWING":
        return 50
    return 50


def _classify_pipeline_stop(final_action: str, reason: str, strategy: Dict[str, Any], swing_diag: Dict[str, Any]) -> str:
    explicit = _first_text(_safe_get(swing_diag, "pipeline_stop_code"), _safe_get(strategy, "pipeline_stop_code"))
    if explicit:
        up = explicit.upper()
        if "DELTA" in up:
            return "delta_filter"
        if "CREDIT_WIDTH" in up or "CREDIT" in up and "WIDTH" in up:
            return "credit_width"
        if "LIQUID" in up:
            return "liquidity_quotes"
        if "SCORE" in up:
            return "score_threshold"
        if "RC15F" in up or "SMC" in up or "DS" in up:
            return "rc15f_smc_ema"
        if "NO_STRATEGY" in up:
            # If analyzer rejected before strategy because trend was neutral/weak, show the real stop.
            if "neutral trend" in str(reason).lower() or "4h" in str(reason).lower() or "trend" in str(reason).lower():
                return "trend_4h"
            return "no_trade"
    txt = f"{final_action} {reason} {_list_first(_safe_get(strategy, 'warnings', []))} {_list_first(_safe_get(strategy, 'reasons', []))}".lower()
    if "market_closed" in txt:
        return "market_closed"
    if "score_below" in txt or "rejected_score" in txt or "<50" in txt or "<60" in txt:
        return "score_threshold"
    if "delta" in txt:
        return "delta_filter"
    if "liquidity" in txt or "no_quotes" in txt or "bid/ask" in txt:
        return "liquidity_quotes"
    if "credit/width" in txt or "credit_width" in txt or "c/w" in txt or "cw" in txt:
        return "credit_width"
    if "credit" in txt and ("none" in txt or "unavailable" in txt or "missing" in txt):
        return "missing_credit_price"
    if "risk" in txt:
        return "risk_manager"
    if "neutral trend" in txt or "4h" in txt or "trend" in txt or _safe_get(swing_diag, "trend_4h") in ("neutral", "weak"):
        return "trend_4h"
    if "rc15f" in txt or "ict" in txt or "smc" in txt or "ema" in txt:
        return "rc15f_smc_ema"
    if _safe_get(strategy, "no_trade"):
        return "no_trade"
    return "not_evaluated"


def _final_decision(status: Dict[str, Any], strategy: Dict[str, Any], pipeline_stop: str, swing_diag: Dict[str, Any]) -> str:
    if _safe_get(status, "inserted"):
        return "REGISTERED"
    action = str(_safe_get(status, "final_action", "")).lower()
    if action.startswith("watchlist"):
        return "WATCHLIST"
    if action:
        return "REJECTED"
    if bool(_safe_get(swing_diag, "qualified", False)) and not _safe_get(strategy, "no_trade"):
        return "QUALIFIED_NOT_REGISTERED"
    if _safe_get(strategy, "no_trade"):
        return "NO_TRADE"
    if pipeline_stop != "not_evaluated":
        return "REJECTED"
    return "NOT_EVALUATED"


def _date_text_is_today(value: Any) -> bool:
    txt = _fmt(value).strip()
    if not txt:
        return False
    return txt[:10] == _now_et().strftime("%Y-%m-%d")


def _looks_like_explicit_0dte(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    mode_txt = _first_text(
        _safe_get(obj, "trade_mode"),
        _safe_get(obj, "mode"),
        _safe_get(obj, "chain_type"),
        _safe_get(obj, "expiry_mode"),
        _safe_get(obj, "selected_mode"),
    )
    if _normalize_mode(mode_txt) == "0DTE" or "0DTE" in mode_txt.upper().replace("-", "").replace(" ", ""):
        return True
    dte = _to_float(_safe_get(obj, "dte", _safe_get(obj, "dte_at_entry")), None)
    expiry = _first_text(_safe_get(obj, "expiry_date"), _safe_get(obj, "expiration"), _safe_get(obj, "expiry"))
    # DTE=0 alone can be a default/placeholder in Swing diagnostics; require a same-day expiry too.
    if dte == 0 and _date_text_is_today(expiry):
        return True
    return False


def _has_0dte_data(analysis: Dict[str, Any], symbol: str) -> bool:
    if symbol == "SPX":
        return True
    sym = str(symbol or "").upper()
    # RC15i.9c: SPY/QQQ/IWM optional 0DTE rows are written only with explicit 0DTE evidence.
    for s in (_safe_get(analysis, "_paper_insert_status", []) or []):
        if str(_safe_get(s, "symbol", "")).upper() == sym and _looks_like_explicit_0dte(s):
            return True
    data = _safe_get(analysis, symbol.lower(), {}) or {}
    if _looks_like_explicit_0dte(data):
        return True
    st = _safe_get(data, "strategy", {}) or {}
    return _looks_like_explicit_0dte(st)


def _tracks_for_analysis(analysis: Dict[str, Any]) -> List[tuple]:
    # RC15i.9d: strict SPX-only 0DTE journal filter.
    # Even if analyzer includes ETF/index 0DTE diagnostic dictionaries, the daily
    # journal should not write IWM:0DTE / SPY:0DTE / QQQ:0DTE rows.
    tracks = list(BASE_TRACKS)
    order = {"0DTE": 0, "SWING": 1}
    return sorted(tracks, key=lambda x: (order.get(x[1].upper(), 9), x[0]))


def _build_row(analysis: Dict[str, Any], symbol: str, mode: str, cycle_id: str, ts: datetime) -> Dict[str, Any]:
    mode = "Swing" if str(mode or "").upper() == "SWING" else "0DTE"
    data = _symbol_data(analysis, symbol, mode)
    strategy = _strategy_for_track(analysis, symbol, mode)
    status = _latest_status(analysis, symbol, mode)
    swing_diag = _swing_diag_for_symbol(analysis, symbol) if mode == "Swing" else {}
    rc15f = _rc15f_for_symbol(analysis, symbol)
    levels = _safe_get(analysis, "levels", {}) or {}
    dq = _safe_get(data, "data_quality_report", {}) or (_safe_get(analysis, "data_quality_report", {}) if symbol == "SPX" else {}) or {}
    # M1: كل رمز يأخذ بيانات tasty_shadow الخاصة به.
    # SPX: البيانات في المستوى الأعلى للـ analysis.
    # غير SPX: البيانات داخل analysis[symbol.lower()] أو داخل strategy.
    # الخطأ القديم: كان يقرأ المستوى الأعلى أولاً فيعطي كل الرموز بيانات SPX.
    _symbol_analysis_for_shadow = _safe_get(analysis, str(symbol or "").lower(), {}) or {}
    if symbol == "SPX":
        tasty_shadow = _safe_get(analysis, "tastytrade_shadow", {}) or {}
    else:
        tasty_shadow = (
            _safe_get(_symbol_analysis_for_shadow, "tastytrade_shadow", {})
            or _safe_get(strategy, "tastytrade_shadow", {})
            or {}
        )

    final_action = _first_text(_safe_get(status, "final_action"), _safe_get(strategy, "final_action"))
    reject_reason = _first_text(
        _safe_get(status, "block_reason"),
        _safe_get(strategy, "reject_reason"),
        _list_first(_safe_get(strategy, "warnings", [])),
        _list_first(_safe_get(strategy, "reasons", [])),
        _list_first(_safe_get(swing_diag, "rejected", [])),
        _safe_get(data, "error"),
    )
    reject_detail = _first_text(
        _safe_get(swing_diag, "pipeline_stop_code"),
        _safe_get(swing_diag, "diagnostic_code"),
        _safe_get(strategy, "decision"),
        reject_reason,
    )
    pipeline_stop = _classify_pipeline_stop(final_action, reject_reason, strategy, swing_diag)

    strategy_name = _first_text(
        _safe_get(status, "strategy"),
        _safe_get(strategy, "strategy"),
        _best_candidate_name(swing_diag if mode == "Swing" else strategy),
        "No Trade",
    )
    score = _to_float(_safe_get(strategy, "score"), None)
    if score is None:
        score = _to_float(_safe_get(status, "score"), _to_float(_safe_get(swing_diag, "score"), 0.0))

    # IV fields: for Swing, prefer swing diagnostics; for SPX/0DTE, prefer analysis/levels.
    if mode == "Swing":
        iv_rank = _to_float(_safe_get(swing_diag, "iv_rank"), _to_float(_safe_get(data, "iv_rank"), None))
        iv_percentile = _to_float(_safe_get(swing_diag, "iv_percentile"), _to_float(_safe_get(data, "iv_percentile"), None))
    else:
        iv_rank = _to_float(_safe_get(data, "iv_rank"), _to_float(_safe_get(levels, "iv_rank"), None))
        iv_percentile = _to_float(_safe_get(data, "iv_percentile"), _to_float(_safe_get(levels, "iv_percentile"), None))

    price = _to_float(
        _safe_get(data, "price"),
        _to_float(_safe_get(swing_diag, "price"), _to_float(_safe_get(analysis, "price") if symbol == "SPX" else None, None)),
    )

    dte_value = _to_float(_safe_get(strategy, "dte"), _to_float(_safe_get(strategy, "dte_at_entry"), _to_float(_safe_get(swing_diag, "dte"), None)))
    expiry_value = _first_text(_safe_get(strategy, "expiry_date"), _safe_get(swing_diag, "expiry_date"))
    # RC15i.9c: in Swing, dte=0 without an expiry is usually a missing-value placeholder, not real 0DTE.
    if mode == "Swing" and dte_value == 0 and not expiry_value:
        dte_value = None

    # RC15j structural cleanup: when a selected SPX Put Debit is stopped by
    # trade_monitor risk/time gates after scan_put_debit_spread(), the candidate
    # blocked_code can be empty. Derive a journal-only blocked code from the
    # paper insert status so audit rows are not blank. This does not change risk.
    put_debit_blocked_code = _first_text(
        _safe_get(strategy, "put_debit_blocked_code"),
        _safe_get(strategy, "blocked_code"),
    )
    if not put_debit_blocked_code and symbol == "SPX" and mode == "0DTE":
        _rr_up = str(reject_reason or "").upper()
        for _code in (
            "SPX_0DTE_NO_NEW_ENTRY_LAST_3H",
            "SPX_0DTE_DEBIT_TOO_EXPENSIVE",
            "SPX_PUT_DEBIT_DAILY_CAP",
        ):
            if _code in _rr_up:
                put_debit_blocked_code = _code
                break

    row = {
        "cycle_id": cycle_id,
        "timestamp_et": ts.strftime("%Y-%m-%d %H:%M:%S"),
        "date_et": ts.strftime("%Y-%m-%d"),
        "time_et": ts.strftime("%H:%M:%S"),
        "symbol": symbol,
        "mode": mode,
        "track": _track_key(symbol, mode),
        "strategy": strategy_name,
        "best_candidate": _best_candidate_name(strategy) or _best_candidate_name(swing_diag),
        "candidate_scores": _candidate_scores(swing_diag if mode == "Swing" else data, strategy),
        "price": price,
        "score": score,
        "required_score": _required_score(strategy_name, mode),
        "final_decision": _final_decision(status, strategy, pipeline_stop, swing_diag),
        "pipeline_stop": pipeline_stop,
        "pipeline_code": _first_text(_safe_get(swing_diag, "pipeline_stop_code"), _safe_get(strategy, "pipeline_stop_code")),
        "registered": bool(_safe_get(status, "inserted", False)),
        "paper_trade_id": _safe_get(status, "inserted_trade_id", ""),
        "insert_attempted": bool(_safe_get(status, "insert_attempted", False)),
        "final_action": final_action,
        "reject_reason": reject_reason,
        "reject_detail": reject_detail,
        "pin_score": _to_float(_safe_get(analysis, "pin_score") if symbol == "SPX" else _safe_get(data, "pin_score"), None),
        "net_gex": _to_float(_safe_get(levels, "net_gex") if symbol == "SPX" else _safe_get(data, "net_gex"), None),
        "expected_move": _to_float(_safe_get(analysis, "expected_move") if symbol == "SPX" else _safe_get(data, "expected_move"), _to_float(_safe_get(swing_diag, "swing_em"), None)),
        "iv_rank": iv_rank,
        "iv_percentile": iv_percentile,
        "iv_regime": _first_text(_safe_get(swing_diag, "iv_regime"), _safe_get(data, "iv_regime"), _safe_get(strategy, "iv_regime")),
        "dte": dte_value,
        "expiry_date": expiry_value,
        "chain_type": _first_text(_safe_get(strategy, "chain_type"), _safe_get(swing_diag, "chain_type")),
        "trend_4h": _first_text(_safe_get(swing_diag, "trend_4h"), _safe_get(data, "trend_4h"), _safe_get(strategy, "trend_4h"), _safe_get(analysis, "trend") if symbol == "SPX" else ""),
        "trend_confirmed": _first_text(_safe_get(swing_diag, "confirmed_trend"), _safe_get(data, "confirmed_trend"), _safe_get(swing_diag, "qualified")),
        "data_quality_mode": _first_text(_safe_get(dq, "mode"), _safe_get(analysis, "mode") if symbol == "SPX" else ""),
        "liquidity_status": _first_text(_safe_get(strategy, "liquidity_status"), _safe_get(strategy, "liquidity_reason")),
        "credit": _to_float(_safe_get(strategy, "credit"), None),
        "debit": _to_float(_safe_get(strategy, "debit"), None),
        "credit_width_ratio": _to_float(_safe_get(strategy, "credit_width_ratio"), None),
        "rc15f_decision": _first_text(_safe_get(rc15f, "decision"), _safe_get(rc15f, "final_action"), _safe_get(strategy, "rc15f_decision"), _safe_get(strategy, "decision")),
        "rc15f_reason": _first_text(_safe_get(rc15f, "reason"), _safe_get(strategy, "rc15f_reason"), _safe_get(strategy, "swing_block_reason")),
        "smc_status": _first_text(_safe_get(swing_diag, "smc_evaluation"), _safe_get(rc15f, "smc_evaluation"), _safe_get(strategy, "smc_status"), _safe_get(strategy, "smc_reason")),
        "demand_supply_status": _first_text(_safe_get(swing_diag, "ds_evaluation"), _safe_get(rc15f, "ds_evaluation"), _safe_get(strategy, "demand_supply_status"), _safe_get(strategy, "demand_supply_reason")),
        "entry_protection": _first_text(_safe_get(swing_diag, "entry_protection"), _safe_get(strategy, "entry_protection")),
        "diagnostic_code": _first_text(_safe_get(swing_diag, "diagnostic_code"), _safe_get(strategy, "diagnostic_code")),
        "notes": "",
        # ── RC15j Phase 2A — D/S DXLink Diagnostic Audit ─────────────────────
        "ds_source":              _first_text(_safe_get(strategy, "ds_source"), _safe_get(strategy, "demand_supply_source")),
        "ds_symbol_requested":    _first_text(_safe_get(strategy, "ds_symbol_requested")),
        "ds_symbol_resolved":     _first_text(_safe_get(strategy, "ds_symbol_resolved")),
        "ds_1h_available":        _first_text(_safe_get(strategy, "ds_1h_available")),
        "ds_15m_available":       _first_text(_safe_get(strategy, "ds_15m_available")),
        "ds_1h_candles_count":    _safe_get(strategy, "ds_1h_candles_count"),
        "ds_15m_candles_count":   _safe_get(strategy, "ds_15m_candles_count"),
        "atr_15m":                _to_float(_safe_get(strategy, "atr_15m"), None),
        "nearest_demand_low":     _to_float(_safe_get(strategy, "nearest_demand_low"), None),
        "nearest_demand_high":    _to_float(_safe_get(strategy, "nearest_demand_high"), None),
        "distance_to_demand_points": _to_float(_safe_get(strategy, "distance_to_demand_points"), None),
        "nearest_supply_low":     _to_float(_safe_get(strategy, "nearest_supply_low"), None),
        "nearest_supply_high":    _to_float(_safe_get(strategy, "nearest_supply_high"), None),
        "distance_to_supply_points": _to_float(_safe_get(strategy, "distance_to_supply_points"), None),
        "near_threshold":         _to_float(_safe_get(strategy, "near_threshold"), None),
        "ds_not_evaluated_reason": _first_text(_safe_get(strategy, "ds_not_evaluated_reason")),
        "ds_location_decision":   _first_text(_safe_get(strategy, "ds_location_decision")),
        # RC15j Phase 2B — SPY proxy fields
        "ds_proxy_source_symbol": _first_text(_safe_get(strategy, "ds_proxy_source_symbol")),
        "ds_proxy_target_symbol": _first_text(_safe_get(strategy, "ds_proxy_target_symbol")),
        "ds_proxy_ratio":         _to_float(_safe_get(strategy, "ds_proxy_ratio"), None),
        "ds_proxy_reason":        _first_text(_safe_get(strategy, "ds_proxy_reason")),
        "spy_nearest_demand_low":  _to_float(_safe_get(strategy, "spy_nearest_demand_low"), None),
        "spy_nearest_demand_high": _to_float(_safe_get(strategy, "spy_nearest_demand_high"), None),
        "spy_nearest_supply_low":  _to_float(_safe_get(strategy, "spy_nearest_supply_low"), None),
        "spy_nearest_supply_high": _to_float(_safe_get(strategy, "spy_nearest_supply_high"), None),
        # RC15j Phase 2C — Put Debit candidate diagnostics
        "put_debit_score":        _safe_get(strategy, "put_debit_score"),
        "put_debit_blocked_code": put_debit_blocked_code,
        "put_debit_pipeline_stop":_first_text(_safe_get(strategy, "put_debit_pipeline_stop"), pipeline_stop),
        "put_debit_ds_location":  _first_text(_safe_get(strategy, "put_debit_ds_location")),
        "put_debit_reject_reason":_first_text(_safe_get(strategy, "put_debit_reject_reason")),
        # M1 — Tastytrade shadow provider diagnostics (no trade impact)
        "tasty_shadow_enabled": _safe_get(strategy, "tasty_shadow_enabled", _safe_get(tasty_shadow, "tasty_shadow_enabled")),
        "tasty_shadow_ok": _safe_get(strategy, "tasty_shadow_ok", _safe_get(tasty_shadow, "tasty_shadow_ok")),
        "tasty_shadow_source": _first_text(_safe_get(strategy, "tasty_shadow_source"), _safe_get(tasty_shadow, "tasty_shadow_source")),
        "tasty_shadow_reason": _first_text(_safe_get(strategy, "tasty_shadow_reason"), _safe_get(tasty_shadow, "tasty_shadow_reason")),
        "tasty_shadow_underlying_requested_symbol": _first_text(_safe_get(strategy, "tasty_shadow_underlying_requested_symbol"), _safe_get(tasty_shadow, "tasty_shadow_underlying_requested_symbol")),
        "tasty_shadow_underlying_event_symbol": _first_text(_safe_get(strategy, "tasty_shadow_underlying_event_symbol"), _safe_get(tasty_shadow, "tasty_shadow_underlying_event_symbol")),
        "tasty_shadow_underlying_bid": _to_float(_safe_get(strategy, "tasty_shadow_underlying_bid", _safe_get(tasty_shadow, "tasty_shadow_underlying_bid")), None),
        "tasty_shadow_underlying_ask": _to_float(_safe_get(strategy, "tasty_shadow_underlying_ask", _safe_get(tasty_shadow, "tasty_shadow_underlying_ask")), None),
        "tasty_shadow_underlying_mid": _to_float(_safe_get(strategy, "tasty_shadow_underlying_mid", _safe_get(tasty_shadow, "tasty_shadow_underlying_mid")), None),
        "tasty_shadow_underlying_last": _to_float(_safe_get(strategy, "tasty_shadow_underlying_last", _safe_get(tasty_shadow, "tasty_shadow_underlying_last")), None),
        "tasty_shadow_price_diff_points": _to_float(_safe_get(strategy, "tasty_shadow_price_diff_points", _safe_get(tasty_shadow, "tasty_shadow_price_diff_points")), None),
        "tasty_shadow_price_diff_pct": _to_float(_safe_get(strategy, "tasty_shadow_price_diff_pct", _safe_get(tasty_shadow, "tasty_shadow_price_diff_pct")), None),
        "tasty_shadow_price_mismatch": _safe_get(strategy, "tasty_shadow_price_mismatch", _safe_get(tasty_shadow, "tasty_shadow_price_mismatch")),
        "tasty_shadow_warning": _first_text(_safe_get(strategy, "tasty_shadow_warning"), _safe_get(tasty_shadow, "tasty_shadow_warning")),
        "tasty_shadow_mismatch_threshold_pts": _to_float(_safe_get(strategy, "tasty_shadow_mismatch_threshold_pts", _safe_get(tasty_shadow, "tasty_shadow_mismatch_threshold_pts")), None),
        "tasty_shadow_option_quote_count": _safe_get(strategy, "tasty_shadow_option_quote_count", _safe_get(tasty_shadow, "tasty_shadow_option_quote_count")),
        "tasty_shadow_leg_symbols": _first_text(_safe_get(strategy, "tasty_shadow_leg_symbols"), _safe_get(tasty_shadow, "tasty_shadow_leg_symbols")),
        "tasty_shadow_note": _first_text(_safe_get(strategy, "tasty_shadow_note"), _safe_get(tasty_shadow, "tasty_shadow_note")),
        # RC15j Phase 2B.1 — Structural zone shadow
        "struct_shadow_available":   _safe_get(strategy, "struct_shadow_available"),
        "struct_shadow_converted":   _safe_get(strategy, "struct_shadow_converted"),
        "struct_shadow_reason":      _first_text(_safe_get(strategy, "struct_shadow_reason")),
        "struct_shadow_zone_method": _first_text(_safe_get(strategy, "struct_shadow_zone_method")),
        "struct_shadow_spx_price":   _to_float(_safe_get(strategy, "struct_shadow_spx_price"), None),
        "struct_shadow_spy_price":   _to_float(_safe_get(strategy, "struct_shadow_spy_price"), None),
        "struct_shadow_ratio":       _to_float(_safe_get(strategy, "struct_shadow_ratio"), None),
        "struct_put_debit_shadow_allowed": _safe_get(strategy, "struct_put_debit_shadow_allowed"),
        "struct_put_debit_shadow_block_code": _first_text(_safe_get(strategy, "struct_put_debit_shadow_block_code")),
        "struct_put_debit_shadow_block_reason": _first_text(_safe_get(strategy, "struct_put_debit_shadow_block_reason")),
        "struct_put_debit_shadow_primary_tf": _first_text(_safe_get(strategy, "struct_put_debit_shadow_primary_tf")),
        "struct_put_debit_shadow_context_tf": _first_text(_safe_get(strategy, "struct_put_debit_shadow_context_tf")),
        # 1H — SPX-scaled + raw SPY audit
        "struct_1h_candle_count":       _safe_get(strategy, "struct_1h_candle_count"),
        "struct_1h_demand_found":       _safe_get(strategy, "struct_1h_demand_found"),
        "struct_1h_demand_low":         _to_float(_safe_get(strategy, "struct_1h_demand_low"), None),
        "struct_1h_demand_high":        _to_float(_safe_get(strategy, "struct_1h_demand_high"), None),
        "struct_1h_demand_bos_lvl":     _to_float(_safe_get(strategy, "struct_1h_demand_bos_lvl"), None),
        "struct_1h_demand_spy_low":     _to_float(_safe_get(strategy, "struct_1h_demand_spy_low"), None),
        "struct_1h_demand_spy_high":    _to_float(_safe_get(strategy, "struct_1h_demand_spy_high"), None),
        "struct_1h_demand_spy_bos_lvl": _to_float(_safe_get(strategy, "struct_1h_demand_spy_bos_lvl"), None),
        "struct_1h_demand_base_t":      _first_text(_safe_get(strategy, "struct_1h_demand_base_t")),
        "struct_1h_demand_base_idx":    _safe_get(strategy, "struct_1h_demand_base_idx"),
        "struct_1h_demand_bos_t":       _first_text(_safe_get(strategy, "struct_1h_demand_bos_t")),
        "struct_1h_demand_bos_idx":     _safe_get(strategy, "struct_1h_demand_bos_idx"),
        "struct_1h_demand_sh_cnt":      _safe_get(strategy, "struct_1h_demand_sh_cnt"),
        "struct_1h_demand_sl_cnt":      _safe_get(strategy, "struct_1h_demand_sl_cnt"),
        "struct_1h_demand_struct":      _safe_get(strategy, "struct_1h_demand_struct"),
        "struct_1h_demand_bos_candle_close": _to_float(_safe_get(strategy, "struct_1h_demand_bos_candle_close"), None),
        "struct_1h_demand_sh_local_cnt": _safe_get(strategy, "struct_1h_demand_sh_local_cnt"),
        "struct_1h_demand_sl_local_cnt": _safe_get(strategy, "struct_1h_demand_sl_local_cnt"),
        "struct_1h_demand_local_lookback": _safe_get(strategy, "struct_1h_demand_local_lookback"),
        "struct_1h_demand_last2_sh_t":  _first_text(_safe_get(strategy, "struct_1h_demand_last2_sh_t")),
        "struct_1h_demand_last2_sl_t":  _first_text(_safe_get(strategy, "struct_1h_demand_last2_sl_t")),
        # 1H Demand — Phase 2B.2 BOS locality
        "struct_1h_demand_bos_level_idx":         _safe_get(strategy, "struct_1h_demand_bos_level_idx"),
        "struct_1h_demand_bos_level_time":        _first_text(_safe_get(strategy, "struct_1h_demand_bos_level_time")),
        "struct_1h_demand_bos_level_age_bars":    _safe_get(strategy, "struct_1h_demand_bos_level_age_bars"),
        "struct_1h_demand_max_bos_lookback_bars": _safe_get(strategy, "struct_1h_demand_max_bos_lookback_bars"),
        "struct_1h_demand_bos_local_valid":       _safe_get(strategy, "struct_1h_demand_bos_local_valid"),
        "struct_1h_demand_zone_status":           _first_text(_safe_get(strategy, "struct_1h_demand_zone_status")),
        "struct_1h_demand_zone_reject_reason":    _first_text(_safe_get(strategy, "struct_1h_demand_zone_reject_reason")),
        "struct_1h_demand_mitigation_status": _first_text(_safe_get(strategy, "struct_1h_demand_mitigation_status")),
        "struct_1h_demand_tested": _safe_get(strategy, "struct_1h_demand_tested"),
        "struct_1h_demand_mitigated": _safe_get(strategy, "struct_1h_demand_mitigated"),
        "struct_1h_demand_invalidated": _safe_get(strategy, "struct_1h_demand_invalidated"),
        "struct_1h_demand_touches": _safe_get(strategy, "struct_1h_demand_touches"),
        "struct_1h_demand_last_touch_idx": _safe_get(strategy, "struct_1h_demand_last_touch_idx"),
        "struct_1h_demand_last_touch_time": _first_text(_safe_get(strategy, "struct_1h_demand_last_touch_time")),
        "struct_1h_demand_invalidated_idx": _safe_get(strategy, "struct_1h_demand_invalidated_idx"),
        "struct_1h_demand_invalidated_time": _first_text(_safe_get(strategy, "struct_1h_demand_invalidated_time")),
        "struct_1h_demand_structure_event": _first_text(_safe_get(strategy, "struct_1h_demand_structure_event")),
        "struct_1h_demand_structure_direction": _first_text(_safe_get(strategy, "struct_1h_demand_structure_direction")),
        "struct_1h_demand_structure_bias_before": _first_text(_safe_get(strategy, "struct_1h_demand_structure_bias_before")),
        "struct_1h_supply_found":       _safe_get(strategy, "struct_1h_supply_found"),
        "struct_1h_supply_low":         _to_float(_safe_get(strategy, "struct_1h_supply_low"), None),
        "struct_1h_supply_high":        _to_float(_safe_get(strategy, "struct_1h_supply_high"), None),
        "struct_1h_supply_bos_lvl":     _to_float(_safe_get(strategy, "struct_1h_supply_bos_lvl"), None),
        "struct_1h_supply_spy_low":     _to_float(_safe_get(strategy, "struct_1h_supply_spy_low"), None),
        "struct_1h_supply_spy_high":    _to_float(_safe_get(strategy, "struct_1h_supply_spy_high"), None),
        "struct_1h_supply_spy_bos_lvl": _to_float(_safe_get(strategy, "struct_1h_supply_spy_bos_lvl"), None),
        "struct_1h_supply_base_t":      _first_text(_safe_get(strategy, "struct_1h_supply_base_t")),
        "struct_1h_supply_base_idx":    _safe_get(strategy, "struct_1h_supply_base_idx"),
        "struct_1h_supply_bos_t":       _first_text(_safe_get(strategy, "struct_1h_supply_bos_t")),
        "struct_1h_supply_bos_idx":     _safe_get(strategy, "struct_1h_supply_bos_idx"),
        "struct_1h_supply_sh_cnt":      _safe_get(strategy, "struct_1h_supply_sh_cnt"),
        "struct_1h_supply_sl_cnt":      _safe_get(strategy, "struct_1h_supply_sl_cnt"),
        "struct_1h_supply_struct":      _safe_get(strategy, "struct_1h_supply_struct"),
        "struct_1h_supply_bos_candle_close": _to_float(_safe_get(strategy, "struct_1h_supply_bos_candle_close"), None),
        "struct_1h_supply_sh_local_cnt": _safe_get(strategy, "struct_1h_supply_sh_local_cnt"),
        "struct_1h_supply_sl_local_cnt": _safe_get(strategy, "struct_1h_supply_sl_local_cnt"),
        "struct_1h_supply_local_lookback": _safe_get(strategy, "struct_1h_supply_local_lookback"),
        "struct_1h_supply_last2_sh_t":  _first_text(_safe_get(strategy, "struct_1h_supply_last2_sh_t")),
        "struct_1h_supply_last2_sl_t":  _first_text(_safe_get(strategy, "struct_1h_supply_last2_sl_t")),
        # 1H Supply — Phase 2B.2 BOS locality
        "struct_1h_supply_bos_level_idx":         _safe_get(strategy, "struct_1h_supply_bos_level_idx"),
        "struct_1h_supply_bos_level_time":        _first_text(_safe_get(strategy, "struct_1h_supply_bos_level_time")),
        "struct_1h_supply_bos_level_age_bars":    _safe_get(strategy, "struct_1h_supply_bos_level_age_bars"),
        "struct_1h_supply_max_bos_lookback_bars": _safe_get(strategy, "struct_1h_supply_max_bos_lookback_bars"),
        "struct_1h_supply_bos_local_valid":       _safe_get(strategy, "struct_1h_supply_bos_local_valid"),
        "struct_1h_supply_zone_status":           _first_text(_safe_get(strategy, "struct_1h_supply_zone_status")),
        "struct_1h_supply_zone_reject_reason":    _first_text(_safe_get(strategy, "struct_1h_supply_zone_reject_reason")),
        "struct_1h_supply_mitigation_status": _first_text(_safe_get(strategy, "struct_1h_supply_mitigation_status")),
        "struct_1h_supply_tested": _safe_get(strategy, "struct_1h_supply_tested"),
        "struct_1h_supply_mitigated": _safe_get(strategy, "struct_1h_supply_mitigated"),
        "struct_1h_supply_invalidated": _safe_get(strategy, "struct_1h_supply_invalidated"),
        "struct_1h_supply_touches": _safe_get(strategy, "struct_1h_supply_touches"),
        "struct_1h_supply_last_touch_idx": _safe_get(strategy, "struct_1h_supply_last_touch_idx"),
        "struct_1h_supply_last_touch_time": _first_text(_safe_get(strategy, "struct_1h_supply_last_touch_time")),
        "struct_1h_supply_invalidated_idx": _safe_get(strategy, "struct_1h_supply_invalidated_idx"),
        "struct_1h_supply_invalidated_time": _first_text(_safe_get(strategy, "struct_1h_supply_invalidated_time")),
        "struct_1h_supply_structure_event": _first_text(_safe_get(strategy, "struct_1h_supply_structure_event")),
        "struct_1h_supply_structure_direction": _first_text(_safe_get(strategy, "struct_1h_supply_structure_direction")),
        "struct_1h_supply_structure_bias_before": _first_text(_safe_get(strategy, "struct_1h_supply_structure_bias_before")),
        # 1H conflict diagnostics (4 filters)
        "struct_1h_demand_supply_overlap": _safe_get(strategy, "struct_1h_demand_supply_overlap"),
        "struct_1h_overlap_low":        _to_float(_safe_get(strategy, "struct_1h_overlap_low"), None),
        "struct_1h_overlap_high":       _to_float(_safe_get(strategy, "struct_1h_overlap_high"), None),
        "struct_1h_price_in_demand":    _safe_get(strategy, "struct_1h_price_in_demand"),
        "struct_1h_price_in_supply":    _safe_get(strategy, "struct_1h_price_in_supply"),
        "struct_1h_clean_gap":          _to_float(_safe_get(strategy, "struct_1h_clean_gap"), None),
        "struct_1h_min_clean_gap_pts":  _safe_get(strategy, "struct_1h_min_clean_gap_pts"),
        "struct_1h_demand_bos_close_spx": _to_float(_safe_get(strategy, "struct_1h_demand_bos_close_spx"), None),
        "struct_1h_supply_bos_close_spx": _to_float(_safe_get(strategy, "struct_1h_supply_bos_close_spx"), None),
        "struct_1h_demand_bos_stale":   _safe_get(strategy, "struct_1h_demand_bos_stale"),
        "struct_1h_supply_bos_stale":   _safe_get(strategy, "struct_1h_supply_bos_stale"),
        "struct_1h_max_bos_dist_pts":   _safe_get(strategy, "struct_1h_max_bos_dist_pts"),
        "struct_1h_entry_distance_threshold_pts": _to_float(_safe_get(strategy, "struct_1h_entry_distance_threshold_pts"), None),
        "struct_1h_distance_to_demand_zone": _to_float(_safe_get(strategy, "struct_1h_distance_to_demand_zone"), None),
        "struct_1h_distance_to_supply_zone": _to_float(_safe_get(strategy, "struct_1h_distance_to_supply_zone"), None),
        "struct_1h_price_at_demand_valid": _safe_get(strategy, "struct_1h_price_at_demand_valid"),
        "struct_1h_price_at_supply_valid": _safe_get(strategy, "struct_1h_price_at_supply_valid"),
        "struct_1h_price_in_active_demand": _safe_get(strategy, "struct_1h_price_in_active_demand"),
        "struct_1h_price_in_active_supply": _safe_get(strategy, "struct_1h_price_in_active_supply"),
        "struct_1h_demand_active_valid": _safe_get(strategy, "struct_1h_demand_active_valid"),
        "struct_1h_supply_active_valid": _safe_get(strategy, "struct_1h_supply_active_valid"),
        "struct_1h_zone_state_reason": _first_text(_safe_get(strategy, "struct_1h_zone_state_reason")),
        "struct_1h_price_at_active_demand_valid": _safe_get(strategy, "struct_1h_price_at_active_demand_valid"),
        "struct_1h_price_at_active_supply_valid": _safe_get(strategy, "struct_1h_price_at_active_supply_valid"),
        "struct_1h_put_debit_shadow_allowed": _safe_get(strategy, "struct_1h_put_debit_shadow_allowed"),
        "struct_1h_put_debit_shadow_block_code": _first_text(_safe_get(strategy, "struct_1h_put_debit_shadow_block_code")),
        "struct_1h_put_debit_shadow_block_reason": _first_text(_safe_get(strategy, "struct_1h_put_debit_shadow_block_reason")),
        "struct_1h_structural_conflict": _safe_get(strategy, "struct_1h_structural_conflict"),
        "struct_1h_structural_conflict_reason": _first_text(_safe_get(strategy, "struct_1h_structural_conflict_reason")),
        # 15m
        "struct_15m_candle_count":       _safe_get(strategy, "struct_15m_candle_count"),
        "struct_15m_demand_found":       _safe_get(strategy, "struct_15m_demand_found"),
        "struct_15m_demand_low":         _to_float(_safe_get(strategy, "struct_15m_demand_low"), None),
        "struct_15m_demand_high":        _to_float(_safe_get(strategy, "struct_15m_demand_high"), None),
        "struct_15m_demand_bos_lvl":     _to_float(_safe_get(strategy, "struct_15m_demand_bos_lvl"), None),
        "struct_15m_demand_spy_low":     _to_float(_safe_get(strategy, "struct_15m_demand_spy_low"), None),
        "struct_15m_demand_spy_high":    _to_float(_safe_get(strategy, "struct_15m_demand_spy_high"), None),
        "struct_15m_demand_spy_bos_lvl": _to_float(_safe_get(strategy, "struct_15m_demand_spy_bos_lvl"), None),
        "struct_15m_demand_base_t":      _first_text(_safe_get(strategy, "struct_15m_demand_base_t")),
        "struct_15m_demand_base_idx":    _safe_get(strategy, "struct_15m_demand_base_idx"),
        "struct_15m_demand_bos_t":       _first_text(_safe_get(strategy, "struct_15m_demand_bos_t")),
        "struct_15m_demand_bos_idx":     _safe_get(strategy, "struct_15m_demand_bos_idx"),
        "struct_15m_demand_sh_cnt":      _safe_get(strategy, "struct_15m_demand_sh_cnt"),
        "struct_15m_demand_sl_cnt":      _safe_get(strategy, "struct_15m_demand_sl_cnt"),
        "struct_15m_demand_struct":      _safe_get(strategy, "struct_15m_demand_struct"),
        "struct_15m_demand_bos_candle_close": _to_float(_safe_get(strategy, "struct_15m_demand_bos_candle_close"), None),
        "struct_15m_demand_sh_local_cnt": _safe_get(strategy, "struct_15m_demand_sh_local_cnt"),
        "struct_15m_demand_sl_local_cnt": _safe_get(strategy, "struct_15m_demand_sl_local_cnt"),
        "struct_15m_demand_local_lookback": _safe_get(strategy, "struct_15m_demand_local_lookback"),
        "struct_15m_demand_last2_sh_t":  _first_text(_safe_get(strategy, "struct_15m_demand_last2_sh_t")),
        "struct_15m_demand_last2_sl_t":  _first_text(_safe_get(strategy, "struct_15m_demand_last2_sl_t")),
        # 15m Demand — Phase 2B.2 BOS locality
        "struct_15m_demand_bos_level_idx":         _safe_get(strategy, "struct_15m_demand_bos_level_idx"),
        "struct_15m_demand_bos_level_time":        _first_text(_safe_get(strategy, "struct_15m_demand_bos_level_time")),
        "struct_15m_demand_bos_level_age_bars":    _safe_get(strategy, "struct_15m_demand_bos_level_age_bars"),
        "struct_15m_demand_max_bos_lookback_bars": _safe_get(strategy, "struct_15m_demand_max_bos_lookback_bars"),
        "struct_15m_demand_bos_local_valid":       _safe_get(strategy, "struct_15m_demand_bos_local_valid"),
        "struct_15m_demand_zone_status":           _first_text(_safe_get(strategy, "struct_15m_demand_zone_status")),
        "struct_15m_demand_zone_reject_reason":    _first_text(_safe_get(strategy, "struct_15m_demand_zone_reject_reason")),
        "struct_15m_demand_mitigation_status": _first_text(_safe_get(strategy, "struct_15m_demand_mitigation_status")),
        "struct_15m_demand_tested": _safe_get(strategy, "struct_15m_demand_tested"),
        "struct_15m_demand_mitigated": _safe_get(strategy, "struct_15m_demand_mitigated"),
        "struct_15m_demand_invalidated": _safe_get(strategy, "struct_15m_demand_invalidated"),
        "struct_15m_demand_touches": _safe_get(strategy, "struct_15m_demand_touches"),
        "struct_15m_demand_last_touch_idx": _safe_get(strategy, "struct_15m_demand_last_touch_idx"),
        "struct_15m_demand_last_touch_time": _first_text(_safe_get(strategy, "struct_15m_demand_last_touch_time")),
        "struct_15m_demand_invalidated_idx": _safe_get(strategy, "struct_15m_demand_invalidated_idx"),
        "struct_15m_demand_invalidated_time": _first_text(_safe_get(strategy, "struct_15m_demand_invalidated_time")),
        "struct_15m_demand_structure_event": _first_text(_safe_get(strategy, "struct_15m_demand_structure_event")),
        "struct_15m_demand_structure_direction": _first_text(_safe_get(strategy, "struct_15m_demand_structure_direction")),
        "struct_15m_demand_structure_bias_before": _first_text(_safe_get(strategy, "struct_15m_demand_structure_bias_before")),
        "struct_15m_supply_found":       _safe_get(strategy, "struct_15m_supply_found"),
        "struct_15m_supply_low":         _to_float(_safe_get(strategy, "struct_15m_supply_low"), None),
        "struct_15m_supply_high":        _to_float(_safe_get(strategy, "struct_15m_supply_high"), None),
        "struct_15m_supply_bos_lvl":     _to_float(_safe_get(strategy, "struct_15m_supply_bos_lvl"), None),
        "struct_15m_supply_spy_low":     _to_float(_safe_get(strategy, "struct_15m_supply_spy_low"), None),
        "struct_15m_supply_spy_high":    _to_float(_safe_get(strategy, "struct_15m_supply_spy_high"), None),
        "struct_15m_supply_spy_bos_lvl": _to_float(_safe_get(strategy, "struct_15m_supply_spy_bos_lvl"), None),
        "struct_15m_supply_base_t":      _first_text(_safe_get(strategy, "struct_15m_supply_base_t")),
        "struct_15m_supply_base_idx":    _safe_get(strategy, "struct_15m_supply_base_idx"),
        "struct_15m_supply_bos_t":       _first_text(_safe_get(strategy, "struct_15m_supply_bos_t")),
        "struct_15m_supply_bos_idx":     _safe_get(strategy, "struct_15m_supply_bos_idx"),
        "struct_15m_supply_sh_cnt":      _safe_get(strategy, "struct_15m_supply_sh_cnt"),
        "struct_15m_supply_sl_cnt":      _safe_get(strategy, "struct_15m_supply_sl_cnt"),
        "struct_15m_supply_struct":      _safe_get(strategy, "struct_15m_supply_struct"),
        "struct_15m_supply_bos_candle_close": _to_float(_safe_get(strategy, "struct_15m_supply_bos_candle_close"), None),
        "struct_15m_supply_sh_local_cnt": _safe_get(strategy, "struct_15m_supply_sh_local_cnt"),
        "struct_15m_supply_sl_local_cnt": _safe_get(strategy, "struct_15m_supply_sl_local_cnt"),
        "struct_15m_supply_local_lookback": _safe_get(strategy, "struct_15m_supply_local_lookback"),
        "struct_15m_supply_last2_sh_t":  _first_text(_safe_get(strategy, "struct_15m_supply_last2_sh_t")),
        "struct_15m_supply_last2_sl_t":  _first_text(_safe_get(strategy, "struct_15m_supply_last2_sl_t")),
        # 15m Supply — Phase 2B.2 BOS locality
        "struct_15m_supply_bos_level_idx":         _safe_get(strategy, "struct_15m_supply_bos_level_idx"),
        "struct_15m_supply_bos_level_time":        _first_text(_safe_get(strategy, "struct_15m_supply_bos_level_time")),
        "struct_15m_supply_bos_level_age_bars":    _safe_get(strategy, "struct_15m_supply_bos_level_age_bars"),
        "struct_15m_supply_max_bos_lookback_bars": _safe_get(strategy, "struct_15m_supply_max_bos_lookback_bars"),
        "struct_15m_supply_bos_local_valid":       _safe_get(strategy, "struct_15m_supply_bos_local_valid"),
        "struct_15m_supply_zone_status":           _first_text(_safe_get(strategy, "struct_15m_supply_zone_status")),
        "struct_15m_supply_zone_reject_reason":    _first_text(_safe_get(strategy, "struct_15m_supply_zone_reject_reason")),
        "struct_15m_supply_mitigation_status": _first_text(_safe_get(strategy, "struct_15m_supply_mitigation_status")),
        "struct_15m_supply_tested": _safe_get(strategy, "struct_15m_supply_tested"),
        "struct_15m_supply_mitigated": _safe_get(strategy, "struct_15m_supply_mitigated"),
        "struct_15m_supply_invalidated": _safe_get(strategy, "struct_15m_supply_invalidated"),
        "struct_15m_supply_touches": _safe_get(strategy, "struct_15m_supply_touches"),
        "struct_15m_supply_last_touch_idx": _safe_get(strategy, "struct_15m_supply_last_touch_idx"),
        "struct_15m_supply_last_touch_time": _first_text(_safe_get(strategy, "struct_15m_supply_last_touch_time")),
        "struct_15m_supply_invalidated_idx": _safe_get(strategy, "struct_15m_supply_invalidated_idx"),
        "struct_15m_supply_invalidated_time": _first_text(_safe_get(strategy, "struct_15m_supply_invalidated_time")),
        "struct_15m_supply_structure_event": _first_text(_safe_get(strategy, "struct_15m_supply_structure_event")),
        "struct_15m_supply_structure_direction": _first_text(_safe_get(strategy, "struct_15m_supply_structure_direction")),
        "struct_15m_supply_structure_bias_before": _first_text(_safe_get(strategy, "struct_15m_supply_structure_bias_before")),
        # 15m conflict diagnostics (4 filters)
        "struct_15m_demand_supply_overlap": _safe_get(strategy, "struct_15m_demand_supply_overlap"),
        "struct_15m_overlap_low":        _to_float(_safe_get(strategy, "struct_15m_overlap_low"), None),
        "struct_15m_overlap_high":       _to_float(_safe_get(strategy, "struct_15m_overlap_high"), None),
        "struct_15m_price_in_demand":    _safe_get(strategy, "struct_15m_price_in_demand"),
        "struct_15m_price_in_supply":    _safe_get(strategy, "struct_15m_price_in_supply"),
        "struct_15m_clean_gap":          _to_float(_safe_get(strategy, "struct_15m_clean_gap"), None),
        "struct_15m_min_clean_gap_pts":  _safe_get(strategy, "struct_15m_min_clean_gap_pts"),
        "struct_15m_demand_bos_close_spx": _to_float(_safe_get(strategy, "struct_15m_demand_bos_close_spx"), None),
        "struct_15m_supply_bos_close_spx": _to_float(_safe_get(strategy, "struct_15m_supply_bos_close_spx"), None),
        "struct_15m_demand_bos_stale":   _safe_get(strategy, "struct_15m_demand_bos_stale"),
        "struct_15m_supply_bos_stale":   _safe_get(strategy, "struct_15m_supply_bos_stale"),
        "struct_15m_max_bos_dist_pts":   _safe_get(strategy, "struct_15m_max_bos_dist_pts"),
        "struct_15m_entry_distance_threshold_pts": _to_float(_safe_get(strategy, "struct_15m_entry_distance_threshold_pts"), None),
        "struct_15m_distance_to_demand_zone": _to_float(_safe_get(strategy, "struct_15m_distance_to_demand_zone"), None),
        "struct_15m_distance_to_supply_zone": _to_float(_safe_get(strategy, "struct_15m_distance_to_supply_zone"), None),
        "struct_15m_price_at_demand_valid": _safe_get(strategy, "struct_15m_price_at_demand_valid"),
        "struct_15m_price_at_supply_valid": _safe_get(strategy, "struct_15m_price_at_supply_valid"),
        "struct_15m_price_in_active_demand": _safe_get(strategy, "struct_15m_price_in_active_demand"),
        "struct_15m_price_in_active_supply": _safe_get(strategy, "struct_15m_price_in_active_supply"),
        "struct_15m_demand_active_valid": _safe_get(strategy, "struct_15m_demand_active_valid"),
        "struct_15m_supply_active_valid": _safe_get(strategy, "struct_15m_supply_active_valid"),
        "struct_15m_zone_state_reason": _first_text(_safe_get(strategy, "struct_15m_zone_state_reason")),
        "struct_15m_price_at_active_demand_valid": _safe_get(strategy, "struct_15m_price_at_active_demand_valid"),
        "struct_15m_price_at_active_supply_valid": _safe_get(strategy, "struct_15m_price_at_active_supply_valid"),
        "struct_15m_put_debit_shadow_allowed": _safe_get(strategy, "struct_15m_put_debit_shadow_allowed"),
        "struct_15m_put_debit_shadow_block_code": _first_text(_safe_get(strategy, "struct_15m_put_debit_shadow_block_code")),
        "struct_15m_put_debit_shadow_block_reason": _first_text(_safe_get(strategy, "struct_15m_put_debit_shadow_block_reason")),
        "struct_15m_structural_conflict": _safe_get(strategy, "struct_15m_structural_conflict"),
        "struct_15m_structural_conflict_reason": _first_text(_safe_get(strategy, "struct_15m_structural_conflict_reason")),
    }
    return row


def _append_csv(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    """Append rows and migrate old RC15i.9/9a headers safely if needed."""
    exists = path.exists() and path.stat().st_size > 0
    if exists:
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                old_fields = reader.fieldnames or []
                if old_fields != CSV_FIELDS:
                    old_rows = list(reader)
                    backup = path.with_suffix(path.suffix + ".pre_rc15i9d.bak")
                    try:
                        if not backup.exists():
                            path.replace(backup)
                        else:
                            path.unlink()
                    except Exception:
                        # If backup fails, still rewrite the file safely below.
                        pass
                    with path.open("w", newline="", encoding="utf-8-sig") as out:
                        writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
                        writer.writeheader()
                        for old in old_rows:
                            writer.writerow({key: _fmt(old.get(key, "")) for key in CSV_FIELDS})
                    exists = True
        except Exception:
            # Do not break trading/journal; append with current fields if migration fails.
            pass

    with path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow({key: _fmt(row.get(key, "")) for key in CSV_FIELDS})


def _append_jsonl(path: Path, rows: Iterable[Dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def write_decision_journal(analysis: Dict[str, Any], source: str = "analysis_done") -> Dict[str, Any]:
    """Write one decision-memory row per tracked symbol.

    Returns a small status dictionary for UI/logging. Never raises.
    """
    try:
        if not isinstance(analysis, dict) or analysis.get("error"):
            return {"ok": False, "reason": "analysis_error_or_invalid"}
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = _now_et()
        cycle_id = f"{ts.strftime('%Y%m%d_%H%M%S')}_{id(analysis) % 100000:05d}"
        tracks = _tracks_for_analysis(analysis)
        rows = [_build_row(analysis, sym, mode, cycle_id, ts) for sym, mode in tracks]
        for row in rows:
            row["notes"] = source

        date_str = ts.strftime("%Y-%m-%d")
        csv_path = DATA_DIR / f"session_journal_{date_str}.csv"
        jsonl_path = DATA_DIR / f"session_journal_{date_str}.jsonl"
        _append_csv(csv_path, rows)
        _append_jsonl(jsonl_path, rows)

        summary = {
            "ok": True,
            "cycle_id": cycle_id,
            "rows": len(rows),
            "csv_path": str(csv_path),
            "jsonl_path": str(jsonl_path),
        }
        analysis["_session_journal"] = summary
        return summary
    except Exception as exc:
        try:
            analysis["_session_journal"] = {"ok": False, "error": str(exc)}
        except Exception:
            pass
        return {"ok": False, "error": str(exc)}



def rebuild_csv_from_jsonl(csv_path: Optional[str] = None, jsonl_path: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild CSV from JSONL for the UI button.

    RC15i.9d: the JSONL file is the source of truth if the CSV was created empty
    by the UI button or had an old header. This keeps "ذاكرة اليوم" synchronized
    before opening it in Excel. Best-effort only; never affects trading.
    """
    try:
        paths = get_today_journal_paths()
        cpath = Path(csv_path or paths["csv_path"])
        jpath = Path(jsonl_path or paths["jsonl_path"])
        cpath.parent.mkdir(parents=True, exist_ok=True)

        rows: List[Dict[str, Any]] = []
        if jpath.exists() and jpath.stat().st_size > 0:
            with jpath.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    # Strict journal cleanup when rebuilding old files:
                    # remove misleading non-SPX 0DTE rows from pre-9d JSONL.
                    sym = str(obj.get("symbol", "")).upper()
                    mode = _normalize_mode(obj.get("mode", ""))
                    track = str(obj.get("track", "")).upper()
                    if mode == "0DTE" and sym != "SPX":
                        continue
                    if track.endswith(":0DTE") and not track.startswith("SPX:"):
                        continue
                    rows.append(obj)

        with cpath.open("w", newline="", encoding="utf-8-sig") as out:
            writer = csv.DictWriter(out, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: _fmt(row.get(key, "")) for key in CSV_FIELDS})
        return {"ok": True, "rows": len(rows), "csv_path": str(cpath), "jsonl_path": str(jpath)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

def get_today_journal_paths() -> Dict[str, str]:
    ts = _now_et()
    date_str = ts.strftime("%Y-%m-%d")
    return {
        "csv_path": str(DATA_DIR / f"session_journal_{date_str}.csv"),
        "jsonl_path": str(DATA_DIR / f"session_journal_{date_str}.jsonl"),
    }
