"""
Excel Exporter - Export trades to Excel file
"""
import os
from datetime import datetime
from core.database import get_all_trades, get_stats


def export_trades_to_excel(output_path=None):
    """Export all trades to Excel file"""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False, "يرجى تثبيت openpyxl: pip install openpyxl"
    
    trades = get_all_trades()
    stats = get_stats()
    
    if not output_path:
        # سطح المكتب العربي (OneDrive) أولاً، ثم الإنجليزي كـ fallback
        arabic_desktop  = os.path.expanduser("~/OneDrive/سطح المكتب")
        english_desktop = os.path.expanduser("~/Desktop")
        desktop = arabic_desktop if os.path.isdir(arabic_desktop) else english_desktop
        os.makedirs(desktop, exist_ok=True)
        filename = f"SPX_Trades_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        output_path = os.path.join(desktop, filename)
    
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "تقرير الصفقات"
    ws.sheet_view.rightToLeft = True
    
    # Colors
    dark_blue = "1a1a2e"
    green = "00b894"
    red = "d63031"
    gold = "fdcb6e"
    white = "FFFFFF"
    light_gray = "f8f9fa"
    header_blue = "2d3561"
    
    # Title row
    ws.merge_cells("A1:H1")
    title_cell = ws["A1"]
    title_cell.value = "📊 تقرير الصفقات — SPX Options Trading Bot"
    title_cell.font = Font(bold=True, size=16, color=white)
    title_cell.fill = PatternFill("solid", fgColor=dark_blue)
    title_cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 35
    
    # Stats row
    ws.merge_cells("A2:B2")
    ws["A2"].value = f"إجمالي الصفقات: {stats['total']}"
    ws.merge_cells("C2:D2")
    ws["C2"].value = f"نسبة النجاح: {stats['win_rate']}%"
    ws.merge_cells("E2:F2")
    ws["E2"].value = f"صافي R: {stats['net_r']:+.2f}R"
    ws.merge_cells("G2:H2")
    ws["G2"].value = f"رابحة: {stats['wins']} | خاسرة: {stats['losses']}"
    
    for col in ["A", "C", "E", "G"]:
        cell = ws[f"{col}2"]
        cell.font = Font(bold=True, size=12, color=white)
        cell.fill = PatternFill("solid", fgColor=header_blue)
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[2].height = 28
    
    # Headers
    headers = ["#", "التاريخ", "الوقت", "الاستراتيجية", "النوع", "النتيجة", "R المبلغ", "المبلغ $", "ملاحظات"]
    # Add extra column
    ws.merge_cells("A3:A3")
    for i, h in enumerate(headers, 1):
        cell = ws.cell(row=3, column=i, value=h)
        cell.font = Font(bold=True, size=11, color=white)
        cell.fill = PatternFill("solid", fgColor="34495e")
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[3].height = 24
    
    def _fmt_r(v):
        try:
            return f"{float(v):+.2f}R"
        except Exception:
            return ""

    def _fmt_money(v):
        try:
            return f"${float(v):,.2f}"
        except Exception:
            return ""

    # Data rows
    for row_idx, trade in enumerate(trades, 4):
        is_win = trade["result"] == "ربح"
        bg = "e8f8f5" if is_win else "ffeaa7"

        values = [
            trade["id"],
            trade["date"],
            trade["time"],
            trade.get("strategy", "Iron Condor"),
            trade.get("type", "MANUAL"),
            trade["result"],
            _fmt_r(trade.get("r_value")),
            _fmt_money(trade.get("amount")),
            trade.get("notes", ""),
        ]
        
        for col_idx, val in enumerate(values, 1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.fill = PatternFill("solid", fgColor=bg)
            cell.alignment = Alignment(horizontal="center", vertical="center")
            if col_idx == 6:  # Result column
                cell.font = Font(bold=True, color=("006400" if is_win else "8B0000"))
            if col_idx == 7:  # R value
                try:
                    r_color = "006400" if float(trade.get("r_value") or 0) >= 0 else "8B0000"
                except Exception:
                    r_color = "000000"
                cell.font = Font(bold=True, color=r_color)
        ws.row_dimensions[row_idx].height = 20
    
    # Column widths
    col_widths = [5, 14, 10, 18, 12, 10, 12, 14, 30]
    for i, w in enumerate(col_widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    
    try:
        wb.save(output_path)
        return True, output_path
    except Exception as e:
        return False, f"خطأ في الحفظ: {str(e)}"


def export_paper_study_to_excel(output_path=None):
    """Export detailed paper_trades rows for v3.30 time-of-day/repetition study."""
    try:
        import openpyxl
        from openpyxl.styles import PatternFill, Font, Alignment
        from openpyxl.utils import get_column_letter
    except ImportError:
        return False, "يرجى تثبيت openpyxl: pip install openpyxl"

    from core.database import get_paper_trades, get_paper_report
    trades = get_paper_trades(limit=10000)
    report = get_paper_report()

    if not output_path:
        arabic_desktop  = os.path.expanduser("~/OneDrive/سطح المكتب")
        english_desktop = os.path.expanduser("~/Desktop")
        desktop = arabic_desktop if os.path.isdir(arabic_desktop) else english_desktop
        os.makedirs(desktop, exist_ok=True)
        filename = f"AbuHassan_Paper_Study_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        output_path = os.path.join(desktop, filename)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Paper Trades Study"
    ws.freeze_panes = "A2"

    headers = [
        "id", "timestamp", "entry_time_ny", "minutes_since_market_open", "time_bucket",
        "symbol", "selected_mode", "strategy", "setup_direction", "expiry_date", "dte_at_entry",
        "score", "setup_quality", "credit_debit", "exit_price", "status", "result",
        "profit_pct", "pnl_dollar", "close_reason",
        "current_value", "current_pnl_dollar", "current_pnl_pct",
        "best_value_seen", "best_pnl_dollar_seen", "best_pnl_pct_seen", "best_seen_at", "monitor_checks",
        "same_setup_key", "is_repeated_setup",
        "same_setup_open_count_before_entry", "same_setup_closed_count_today",
        "minutes_since_last_same_setup", "repetition_note", "source", "trade_signature",
        "short_put", "long_put", "short_call", "long_call", "short_delta", "long_delta",
        "sigma_distance", "credit_width_ratio", "created_at",
    ]
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="34495e")
        cell.alignment = Alignment(horizontal="center")

    for r, t in enumerate(trades, 2):
        for c, h in enumerate(headers, 1):
            ws.cell(row=r, column=c, value=t.get(h))

    for c, h in enumerate(headers, 1):
        ws.column_dimensions[get_column_letter(c)].width = min(max(len(h) + 2, 12), 34)

    # Summary sheets
    def _write_stats_sheet(name, data):
        sh = wb.create_sheet(name[:31])
        cols = ["group", "total", "wins", "losses", "win_rate", "avg_win_pct", "avg_loss_pct", "profit_factor", "expectancy", "max_drawdown", "avg_score"]
        for c, h in enumerate(cols, 1):
            cell = sh.cell(row=1, column=c, value=h)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="2d3561")
        row = 2
        for group, st in (data or {}).items():
            if not st:
                continue
            sh.cell(row=row, column=1, value=group)
            for c, h in enumerate(cols[1:], 2):
                sh.cell(row=row, column=c, value=st.get(h))
            row += 1
        for c, h in enumerate(cols, 1):
            sh.column_dimensions[get_column_letter(c)].width = max(len(h) + 2, 14)

    _write_stats_sheet("By Time Bucket", report.get("by_time_bucket"))
    _write_stats_sheet("First vs Repeated", report.get("by_repetition"))

    rep_sh = wb.create_sheet("Repeated Setups")
    rep_headers = ["label", "total", "win_rate", "profit_factor", "expectancy", "max_drawdown", "avg_score"]
    for c, h in enumerate(rep_headers, 1):
        cell = rep_sh.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2d3561")
    for r, st in enumerate(report.get("repeated_setups") or [], 2):
        for c, h in enumerate(rep_headers, 1):
            rep_sh.cell(row=r, column=c, value=st.get(h))
    for c, h in enumerate(rep_headers, 1):
        rep_sh.column_dimensions[get_column_letter(c)].width = max(len(h) + 2, 18)

    try:
        wb.save(output_path)
        return True, output_path
    except Exception as e:
        return False, f"خطأ في الحفظ: {str(e)}"
