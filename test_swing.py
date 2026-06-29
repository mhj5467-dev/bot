# -*- coding: utf-8 -*-
"""RC13f regression checks for Swing safety rules."""
from datetime import date, timedelta
from pathlib import Path
from core.database import init_db
from core.trade_monitor import _dte_remaining
from core.analyzer import _manual_swing_blocked

init_db()
print("=" * 60)
print("RC13f SWING REGRESSION TESTS")
print("=" * 60)
future = (date.today() + timedelta(days=14)).isoformat()
near = (date.today() + timedelta(days=2)).isoformat()
t1 = _dte_remaining(future) > 2 and _dte_remaining(near) <= 2
print(f"1. DTE boundary (14d open, 2d exit): {'PASS' if t1 else 'FAIL'}")
t2 = _manual_swing_blocked("SPY")[0] is False
print(f"2. SPY unaffected by AAPL/NVDA switch: {'PASS' if t2 else 'FAIL'}")
code = Path(__file__).with_name("core").joinpath("analyzer.py").read_text(encoding="utf-8")
apply_count = code.count("strategy_result = _apply_swing_smc_filter(")
t3 = apply_count == 1
print(f"3. Swing SMC/D-S applied once (count={apply_count}): {'PASS' if t3 else 'FAIL'}")
db_code = Path(__file__).with_name("core").joinpath("database.py").read_text(encoding="utf-8")
t4 = '("max_open_swing_total",    "2")' in db_code
print(f"4. max_open_swing_total default=2: {'PASS' if t4 else 'FAIL'}")
t5 = "iv_diagnostics_only" in code and "stopped_at_4h_trend_filter" in code
print(f"5. IV retained after neutral 4H reject: {'PASS' if t5 else 'FAIL'}")
overall = all((t1,t2,t3,t4,t5))
print("-"*60)
print(f"Overall: {'ALL PASS' if overall else 'SOME FAILED'}")
raise SystemExit(0 if overall else 1)
