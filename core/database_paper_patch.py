"""Patch: replace get_paper_report in database.py"""
import re

with open('core/database.py', 'r', encoding='utf-8') as f:
    content = f.read()

OLD_START = "def get_paper_report() -> dict:"
OLD_END   = "        return {}\n"   # last line of the function

# Find start/end positions
start = content.find(OLD_START)
# Find the end of the function (next top-level def or EOF)
end_marker = "\ndef "
end = content.find(end_marker, start + len(OLD_START))
if end == -1:
    end = len(content)

NEW_FUNC = '''def get_paper_report() -> dict:
    """
    تقرير Paper Trading الموسّع:
    Win Rate / Profit Factor / Expectancy / Max Drawdown
    منفصل حسب: Mode / Strategy / Symbol
    معايير الانتقال للتداول الحقيقي:
      - 50 صفقة مغلقة (مفضّل 100)
      - Profit Factor >= 1.3
      - Expectancy > 0
      - Max Drawdown مقبول (<= 20%)
    """
    # معايير الانتقال للتداول الحقيقي
    LIVE_MIN_TRADES  = 50
    LIVE_MIN_PF      = 1.3
    LIVE_MAX_DD      = 20.0   # % max drawdown مسموح

    try:
        trades = get_paper_trades(status="closed")
        if not trades:
            return {"total": 0, "message": "لا توجد توصيات مغلقة بعد"}

        def _max_drawdown(subset):
            """Max Drawdown كنسبة % من ذروة P&L المتراكم."""
            if not subset:
                return 0.0
            equity = 0.0
            peak   = 0.0
            max_dd = 0.0
            for t in sorted(subset, key=lambda x: x.get("exit_date") or x.get("closed_at") or x.get("created_at") or ""):
                pnl = t.get("pnl_dollar") or 0
                equity += pnl
                if equity > peak:
                    peak = equity
                if peak > 0:
                    dd = (peak - equity) / peak * 100
                    if dd > max_dd:
                        max_dd = dd
            return round(max_dd, 1)

        def _stats(subset):
            if not subset:
                return {}
            wins     = [t for t in subset if t.get("result") == "WIN"]
            losses   = [t for t in subset if t.get("result") == "LOSS"]
            partials = [t for t in subset if t.get("result") == "PARTIAL WIN"]
            total    = len(subset)
            win_rate = round(len(wins) / total * 100, 1)

            avg_win  = round(sum(t.get("profit_pct") or 0 for t in wins)  / len(wins),  1) if wins   else 0
            avg_loss = round(sum(abs(t.get("profit_pct") or 0) for t in losses) / len(losses), 1) if losses else 0

            gross_p  = sum(t.get("pnl_dollar") or 0 for t in wins + partials)
            gross_l  = abs(sum(t.get("pnl_dollar") or 0 for t in losses))
            pf       = round(gross_p / gross_l, 2) if gross_l > 0 else None

            expectancy = round(
                (win_rate/100 * avg_win) - ((1 - win_rate/100) * avg_loss), 2
            )
            avg_score = round(sum(t.get("score") or 0 for t in subset) / total, 1)
            max_dd    = _max_drawdown(subset)

            # هل تستحق الاستمرار؟ (للاستراتيجيات الفردية)
            disabled_suggestion = (
                total >= 10
                and (pf or 0) < 0.8
                and expectancy < 0
            )

            return {
                "total":              total,
                "wins":               len(wins),
                "losses":             len(losses),
                "win_rate":           win_rate,
                "avg_win_pct":        avg_win,
                "avg_loss_pct":       avg_loss,
                "profit_factor":      pf,
                "expectancy":         expectancy,
                "max_drawdown":       max_dd,
                "avg_score":          avg_score,
                "disable_suggestion": disabled_suggestion,
            }

        # الإجمالي
        overall = _stats(trades)
        overall["total_open"] = len(get_paper_trades(status="open"))

        # معايير الانتقال للتداول الحقيقي
        pf_ok  = (overall.get("profit_factor") or 0) >= LIVE_MIN_PF
        exp_ok = overall.get("expectancy", 0) > 0
        dd_ok  = overall.get("max_drawdown", 999) <= LIVE_MAX_DD
        n_ok   = overall.get("total", 0) >= LIVE_MIN_TRADES
        ready  = n_ok and pf_ok and exp_ok and dd_ok

        overall["live_criteria"] = {
            "trades_ok":  n_ok,
            "pf_ok":      pf_ok,
            "exp_ok":     exp_ok,
            "dd_ok":      dd_ok,
            "ready":      ready,
            "missing":    LIVE_MIN_TRADES - overall.get("total", 0) if not n_ok else 0,
        }

        # حسب Mode
        by_mode = {}
        for mode in sorted(set(t.get("selected_mode", "0DTE") for t in trades)):
            by_mode[mode] = _stats([t for t in trades if t.get("selected_mode") == mode])

        # حسب Strategy
        by_strat = {}
        for strat in sorted(set(t.get("strategy", "?") for t in trades)):
            st = _stats([t for t in trades if t.get("strategy") == strat])
            if st:
                by_strat[strat] = st

        # حسب Symbol
        by_symbol = {}
        for sym in sorted(set(t.get("symbol", "?") for t in trades)):
            sy = _stats([t for t in trades if t.get("symbol") == sym])
            if sy:
                by_symbol[sym] = sy

        # تصنيف الاستراتيجيات
        ranked = sorted(
            [(k, v) for k, v in by_strat.items() if v.get("total", 0) >= 5],
            key=lambda x: (x[1].get("profit_factor") or 0, x[1].get("expectancy", 0)),
            reverse=True
        )
        best_strat   = ranked[0][0]  if ranked else "—"
        worst_strat  = ranked[-1][0] if ranked else "—"
        disable_list = [k for k, v in by_strat.items()
                        if v.get("disable_suggestion") and v.get("total", 0) >= 10]

        return {
            "overall":          overall,
            "by_mode":          by_mode,
            "by_strategy":      by_strat,
            "by_symbol":        by_symbol,
            "best_strategy":    best_strat,
            "worst_strategy":   worst_strat,
            "disable_list":     disable_list,
            "ready_for_live":   ready,
            "live_criteria":    overall.get("live_criteria", {}),
        }

    except Exception as e:
        print(f"[get_paper_report error] {e}")
        return {}

'''

content = content[:start] + NEW_FUNC + content[end:]

with open('core/database.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("patch applied, new length:", len(content))
