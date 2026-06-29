"""
Database Manager - SQLite for trade tracking
"""
import sqlite3
import os
from datetime import datetime


# v3.27: قاعدة بيانات ثابتة خارج مجلد النسخة حتى لا تضيع Paper Trades عند تحديث/إعادة تشغيل البوت.
try:
    from core.app_paths import get_app_root, get_data_dir
    _BOT_DIR = str(get_app_root())
    _BOT_DATA_DIR = str(get_data_dir())
except Exception:
    _BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _BOT_DATA_DIR = os.path.join(_BOT_DIR, "data")
    os.makedirs(_BOT_DATA_DIR, exist_ok=True)

# يمكن تجاوز المسار بمتغير بيئة، وإلا نستخدم مجلد ثابت داخل Home.
_USER_DIR = os.environ.get("ABU_HASSAN_BOT_DATA_DIR") or os.path.join(os.path.expanduser("~"), "AbuHassanBot")
os.makedirs(_USER_DIR, exist_ok=True)
DB_PATH = os.path.join(_USER_DIR, "trades.db")

# ترحيل تلقائي من قواعد البيانات القديمة إن كانت موجودة.
_CANDIDATE_DBS = [
    os.path.join(_BOT_DATA_DIR, "trades.db"),
    os.path.join(os.path.expanduser("~"), "AbuHassanBot", "trades.db"),
]
if not os.path.exists(DB_PATH):
    import shutil
    for _old_db in _CANDIDATE_DBS:
        try:
            if _old_db != DB_PATH and os.path.exists(_old_db):
                shutil.copy2(_old_db, DB_PATH)
                print(f"[database] migrated DB: {_old_db} → {DB_PATH}")
                break
        except Exception as _e:
            print(f"[database] migration failed from {_old_db}: {_e}")


def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL mode: قراءة وكتابة متزامنة بدون تعارض بين الـ threads
    conn.execute("PRAGMA journal_mode=WAL")
    # 5 ثوانٍ انتظار قبل رفع "database is locked"
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """SQLite lightweight migration: add missing column if an older DB already exists."""
    try:
        cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
            print(f"[database] migrated {table}: added column {column}")
    except Exception as e:
        print(f"[database migration error] {table}.{column}: {e}")


def init_db():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                strategy TEXT DEFAULT 'Iron Condor',
                type TEXT DEFAULT 'MANUAL',
                result TEXT NOT NULL,
                r_value REAL NOT NULL,
                amount REAL NOT NULL,
                notes TEXT DEFAULT '',
                spx_price REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # جدول IV التاريخي — يُحدَّث يومياً لحساب IV Rank/Percentile حقيقي
        # symbol: SPX / SPY / QQQ — كل رمز له سجل مستقل
        conn.execute("""
            CREATE TABLE IF NOT EXISTS iv_history (
                date   TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT 'SPX',
                atm_iv REAL,
                vix    REAL,
                spx_price REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (date, symbol)
            )
        """)
        # v3.33.3: ترقية قواعد البيانات القديمة التي أُنشئت قبل إضافة symbol.
        _ensure_column(conn, "iv_history", "symbol", "symbol TEXT NOT NULL DEFAULT 'SPX'")
        # جدول OI cache — يحفظ آخر OI ناجح مع طابع زمني
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oi_cache (
                strike REAL,
                option_type TEXT,
                expiry TEXT,
                oi INTEGER,
                volume INTEGER,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (strike, option_type, expiry)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS open_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT    NOT NULL,
                strategy        TEXT    NOT NULL,
                entry_date      TEXT    NOT NULL,
                entry_time      TEXT    NOT NULL,
                entry_price     REAL,
                legs_json       TEXT,
                credit_debit    REAL,
                target          REAL,
                is_credit       INTEGER DEFAULT 1,
                score           INTEGER DEFAULT 0,
                status          TEXT    DEFAULT 'open',
                exit_price      REAL,
                exit_date       TEXT,
                exit_time       TEXT,
                result          TEXT,
                profit_pct      REAL,
                close_reason    TEXT,
                current_value   REAL,
                pnl_dollar      REAL,
                pnl_pct         REAL,
                last_updated    TEXT,
                trade_mode      TEXT    DEFAULT '0DTE',
                dte_at_entry    INTEGER DEFAULT 0,
                expiry_date     TEXT,
                execution_mode  TEXT    DEFAULT 'Auto',
                settlement_type TEXT    DEFAULT 'PM',
                root_symbol     TEXT    DEFAULT 'SPXW',
                last_trade_time TEXT    DEFAULT '15:55 ET',
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # جدول أداء كل Mode منفصل
        conn.execute("""
            CREATE TABLE IF NOT EXISTS mode_performance (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_mode      TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                strategy        TEXT    NOT NULL,
                entry_date      TEXT    NOT NULL,
                dte_at_entry    INTEGER DEFAULT 0,
                credit_debit    REAL,
                exit_price      REAL,
                result          TEXT,
                profit_pct      REAL,
                pnl_dollar      REAL,
                close_reason    TEXT,
                current_value   REAL,
                current_pnl_dollar REAL,
                current_pnl_pct REAL,
                exit_trigger    TEXT,
                last_updated    TEXT,
                why_details     TEXT,
                entry_smc_snapshot_json TEXT,
                exposure_note   TEXT,
                entry_time_ny   TEXT,
                minutes_since_market_open INTEGER,
                time_bucket     TEXT,
                setup_direction TEXT,
                same_setup_key  TEXT,
                is_repeated_setup INTEGER DEFAULT 0,
                same_setup_open_count_before_entry INTEGER DEFAULT 0,
                same_setup_closed_count_today INTEGER DEFAULT 0,
                minutes_since_last_same_setup REAL,
                repetition_note TEXT,
                best_value_seen REAL,
                best_pnl_dollar_seen REAL,
                best_pnl_pct_seen REAL,
                best_seen_at TEXT,
                monitor_checks INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # جدول Paper Trading — يسجل كل توصية بدون تنفيذ حقيقي
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                selected_mode   TEXT    NOT NULL,
                strategy        TEXT    NOT NULL,
                expiry_date     TEXT,
                dte_at_entry    INTEGER DEFAULT 0,
                short_put       REAL,
                long_put        REAL,
                short_call      REAL,
                long_call       REAL,
                credit_debit    REAL,
                max_loss        REAL,
                max_profit      REAL,
                reward_risk     REAL,
                score           INTEGER DEFAULT 0,
                entry_reasons   TEXT,
                mode_reasons    TEXT,
                bid_ask_ok      INTEGER DEFAULT 1,
                debit_ratio     REAL,
                setup_quality   TEXT,
                source          TEXT    DEFAULT "auto",
                signal_key      TEXT,
                trade_signature TEXT,
                duplicate_note TEXT,
                first_detected_time TEXT,
                current_signal_time TEXT,
                signal_age      TEXT,
                initial_score   REAL,
                current_score   REAL,
                peak_score      REAL,
                current_price   REAL,
                price_first_detected REAL,
                underlying_price REAL,
                expected_move   REAL,
                em_upper        REAL,
                em_lower        REAL,
                sigma_distance  REAL,
                sigma_side      TEXT,
                short_delta     REAL,
                long_delta      REAL,
                short_put_delta REAL,
                long_put_delta  REAL,
                short_call_delta REAL,
                long_call_delta REAL,
                net_delta       REAL,
                short_strike    REAL,
                long_strike     REAL,
                distance_points REAL,
                distance_pct    REAL,
                target_delta    REAL,
                credit_width_ratio REAL,
                min_credit_width_ratio REAL,
                credit_width_ok INTEGER DEFAULT 1,
                credit_width_note TEXT,
                status          TEXT    DEFAULT 'open',
                exit_price      REAL,
                exit_date       TEXT,
                result          TEXT,
                profit_pct      REAL,
                pnl_dollar      REAL,
                close_reason    TEXT,
                current_value   REAL,
                current_pnl_dollar REAL,
                current_pnl_pct REAL,
                exit_trigger    TEXT,
                last_updated    TEXT,
                why_details     TEXT,
                entry_smc_snapshot_json TEXT,
                exposure_note   TEXT,
                entry_time_ny   TEXT,
                minutes_since_market_open INTEGER,
                time_bucket     TEXT,
                setup_direction TEXT,
                same_setup_key  TEXT,
                is_repeated_setup INTEGER DEFAULT 0,
                same_setup_open_count_before_entry INTEGER DEFAULT 0,
                same_setup_closed_count_today INTEGER DEFAULT 0,
                minutes_since_last_same_setup REAL,
                repetition_note TEXT,
                best_value_seen REAL,
                best_pnl_dollar_seen REAL,
                best_pnl_pct_seen REAL,
                best_seen_at TEXT,
                monitor_checks INTEGER DEFAULT 0,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS signal_tracking (
                signal_key      TEXT PRIMARY KEY,
                symbol          TEXT,
                selected_mode   TEXT,
                strategy        TEXT,
                first_detected_time TEXT,
                last_seen_time  TEXT,
                initial_score   REAL,
                current_score   REAL,
                peak_score      REAL,
                price_first_detected REAL,
                current_price   REAL,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # جدول حالة الاستراتيجيات — تعطيل/تحذير/تخفيض وزن حسب (mode+strategy+symbol)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS strategy_status (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_mode      TEXT    NOT NULL,
                strategy        TEXT    NOT NULL,
                symbol          TEXT    NOT NULL,
                status          TEXT    NOT NULL DEFAULT 'active',
                weight          REAL    NOT NULL DEFAULT 1.0,
                stage           INTEGER NOT NULL DEFAULT 0,
                pf_at_stage     REAL,
                exp_at_stage    REAL,
                trades_at_stage INTEGER,
                probation_count INTEGER DEFAULT 0,
                probation_started_at TEXT,
                reason          TEXT,
                disabled_at     TEXT,
                re_enabled_at   TEXT,
                last_updated    TEXT,
                created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(trade_mode, strategy, symbol)
            )
        """)
        # أضف أعمدة strategy_status الجديدة إذا لم تكن موجودة
        for col in ["probation_count INTEGER DEFAULT 0",
                    "probation_started_at TEXT",
                    "last_updated TEXT"]:
            try:
                conn.execute(f"ALTER TABLE strategy_status ADD COLUMN {col}")
            except Exception:
                pass

        # أضف الأعمدة الجديدة للجداول القديمة إذا لم تكن موجودة
        for col in [
            "current_value REAL", "pnl_dollar REAL", "pnl_pct REAL",
            "last_updated TEXT", "trade_mode TEXT DEFAULT '0DTE'",
            "dte_at_entry INTEGER DEFAULT 0", "expiry_date TEXT",
            "execution_mode TEXT DEFAULT 'Auto'",
            "settlement_type TEXT DEFAULT 'PM'",
            "root_symbol TEXT DEFAULT 'SPXW'",
            "last_trade_time TEXT DEFAULT '15:55 ET'",
            "underlying_price REAL",
            "expected_move REAL",
            "em_upper REAL",
            "em_lower REAL",
            "sigma_distance REAL",
            "sigma_side TEXT",
            "short_delta REAL",
            "long_delta REAL",
            "short_put_delta REAL",
            "long_put_delta REAL",
            "short_call_delta REAL",
            "long_call_delta REAL",
            "net_delta REAL",
            "short_strike REAL",
            "long_strike REAL",
            "distance_points REAL",
            "distance_pct REAL",
            "target_delta REAL",
            "credit_width_ratio REAL",
            "min_credit_width_ratio REAL",
            "credit_width_ok INTEGER DEFAULT 1",
            "credit_width_note TEXT",
            "trade_signature TEXT",
            "duplicate_note TEXT",
            "current_value REAL",
            "current_pnl_dollar REAL",
            "current_pnl_pct REAL",
            "exit_trigger TEXT",
            "last_updated TEXT",
            "why_details TEXT",
            "entry_smc_snapshot_json TEXT",
            "exposure_note TEXT",
            "entry_time_ny TEXT",
            "minutes_since_market_open INTEGER",
            "time_bucket TEXT",
            "setup_direction TEXT",
            "same_setup_key TEXT",
            "is_repeated_setup INTEGER DEFAULT 0",
            "same_setup_open_count_before_entry INTEGER DEFAULT 0",
            "same_setup_closed_count_today INTEGER DEFAULT 0",
            "minutes_since_last_same_setup REAL",
            "repetition_note TEXT",
            "best_value_seen REAL",
            "best_pnl_dollar_seen REAL",
            "best_pnl_pct_seen REAL",
            "best_seen_at TEXT",
            "monitor_checks INTEGER DEFAULT 0",
        ]:
            try:
                conn.execute(f"ALTER TABLE open_trades ADD COLUMN {col}")
            except Exception:
                pass
        # أضف أعمدة paper_trades الجديدة إذا لم تكن موجودة
        for col in [
            "setup_quality TEXT", "source TEXT DEFAULT 'auto'",
            "signal_key TEXT", "first_detected_time TEXT",
            "current_signal_time TEXT", "signal_age TEXT",
            "initial_score REAL", "current_score REAL", "peak_score REAL",
            "current_price REAL", "price_first_detected REAL",
            "settlement_type TEXT DEFAULT 'PM'",
            "root_symbol TEXT DEFAULT 'SPXW'",
            "last_trade_time TEXT DEFAULT '15:55 ET'",
            "underlying_price REAL",
            "expected_move REAL",
            "em_upper REAL",
            "em_lower REAL",
            "sigma_distance REAL",
            "sigma_side TEXT",
            "short_delta REAL",
            "long_delta REAL",
            "short_put_delta REAL",
            "long_put_delta REAL",
            "short_call_delta REAL",
            "long_call_delta REAL",
            "net_delta REAL",
            "short_strike REAL",
            "long_strike REAL",
            "distance_points REAL",
            "distance_pct REAL",
            "target_delta REAL",
            "credit_width_ratio REAL",
            "min_credit_width_ratio REAL",
            "credit_width_ok INTEGER DEFAULT 1",
            "credit_width_note TEXT",
            "trade_signature TEXT",
            "duplicate_note TEXT",
            "current_value REAL",
            "current_pnl_dollar REAL",
            "current_pnl_pct REAL",
            "exit_trigger TEXT",
            "last_updated TEXT",
            "why_details TEXT",
            "entry_smc_snapshot_json TEXT",
            "exposure_note TEXT",
            "entry_time_ny TEXT",
            "minutes_since_market_open INTEGER",
            "time_bucket TEXT",
            "setup_direction TEXT",
            "same_setup_key TEXT",
            "is_repeated_setup INTEGER DEFAULT 0",
            "same_setup_open_count_before_entry INTEGER DEFAULT 0",
            "same_setup_closed_count_today INTEGER DEFAULT 0",
            "minutes_since_last_same_setup REAL",
            "repetition_note TEXT",
            "best_value_seen REAL",
            "best_pnl_dollar_seen REAL",
            "best_pnl_pct_seen REAL",
            "best_seen_at TEXT",
            "monitor_checks INTEGER DEFAULT 0",
            # RC14 — pipeline diagnostic codes
            # NULL في الصفقات القديمة = LEGACY_UNKNOWN (تُستبعد من إحصاءات Coverage)
            "pipeline_stop_code TEXT",
            "smc_evaluation TEXT",
            "ds_evaluation TEXT",
            "ds_unavailable_reason TEXT",
            "entry_protection TEXT",
            "diagnostic_code TEXT",
        ]:
            try:
                conn.execute(f"ALTER TABLE paper_trades ADD COLUMN {col}")
            except Exception:
                pass
        # جدول سجل الرصيد
        conn.execute("""
            CREATE TABLE IF NOT EXISTS balance_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT    NOT NULL,
                event_type   TEXT    NOT NULL,
                trade_id     INTEGER,
                symbol       TEXT,
                strategy     TEXT,
                amount       REAL    NOT NULL,
                balance      REAL    NOT NULL,
                reserved     REAL    NOT NULL DEFAULT 0,
                note         TEXT    DEFAULT ''
            )
        """)
        defaults = [
            ("telegram_token", ""),
            ("telegram_chat_id", ""),
            ("tasty_client_secret", ""),
            ("tasty_refresh_token", ""),
            ("risk_per_trade", "100"),
            ("threshold_mode",          "conservative"),
            ("wing_width",              "5"),
            ("wing_width_spx",          "5"),
            ("wing_width_spy",          "5"),
            ("wing_width_qqq",          "5"),
            ("account_size",            "1500"),
            ("max_risk_per_trade_pct",  "2.0"),
            # 0 = unlimited for paper-study experimentation
            ("max_trades_per_day",      "0"),
            ("max_open_trades",         "0"),
            ("max_per_symbol",          "0"),
            ("max_open_swing_total",    "2"),
            ("enable_auto_sync_on_exit", "0"),
            ("allow_duplicate_open_strategies", "0"),  # RC15i: default no duplicate open symbol+strategy
            # Universal exact-signal de-duplication. Keeps unlimited distinct trades,
            # but blocks the exact same legs/signature from being logged every scan.
            ("prevent_exact_duplicate_open_trade", "3"),
            ("duplicate_signal_cooldown_minutes", "0"),
            ("prevent_same_trade_same_day", "0"),
            ("max_daily_loss_pct",      "4.0"),
            ("paper_balance",           "10000"),   # الرصيد الابتدائي للـ Paper Trading
            # ── مسارات التداول — Paper فقط ─────────────────────────────
            # القيم: "1" = مفعّل  |  "0" = معطّل
            ("path_spx_0dte_paper",  "1"),
            ("path_spx_0dte_auto",   "0"),  # disabled: paper-only build
            ("path_spy_0dte_paper",  "1"),
            ("path_spy_0dte_auto",   "0"),  # disabled: paper-only build
            ("path_qqq_0dte_paper",  "1"),
            ("path_qqq_0dte_auto",   "0"),  # disabled: paper-only build
            ("path_iwm_0dte_paper",  "1"),  # v3.33.6a RC10: IWM 0DTE paper-only
            ("path_iwm_0dte_auto",   "0"),  # disabled: paper-only build
            ("path_spy_swing_paper", "1"),
            ("path_spy_swing_auto",  "0"),  # disabled: paper-only build
            ("path_qqq_swing_paper", "1"),
            ("path_qqq_swing_auto",  "0"),  # disabled: paper-only build
            ("path_iwm_swing_paper", "1"),  # v3.33.6a RC9: ETF Swing-only experimental
            ("path_iwm_swing_auto",  "0"),  # disabled: paper-only build
            ("path_dia_swing_paper", "1"),  # v3.33.6a RC9: ETF Swing-only experimental
            ("path_dia_swing_auto",  "0"),  # disabled: paper-only build
            ("path_aapl_swing_paper", "1"), # AAPL is Swing-only in paper mode
            ("path_aapl_swing_auto",  "0"), # disabled: paper-only build
            ("path_nvda_swing_paper", "1"), # NVDA is Swing-only in paper mode
            ("path_gld_swing_paper", "1"),  # GLD follows the standard Swing paper path
            ("path_nvda_swing_auto",  "0"), # disabled: paper-only build
        ]
        for k, v in defaults:
            conn.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))

        # v3.33.1 Study mode: disable exact-signal cooldown by default.
        # Existing databases may still contain 60 from older versions; reset it to 0
        # so repeated valid opportunities are recorded for study instead of blocked.
        conn.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?", ("0", "duplicate_signal_cooldown_minutes", "60"))
        # v3.33.2 Study mode: allow up to 3 identical open paper trades for analysis.
        # Existing databases that still have boolean "1" are upgraded to "3".
        conn.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?", ("3", "prevent_exact_duplicate_open_trade", "1"))

        # ── Indexes — تسريع الاستعلامات الأكثر شيوعاً ───────────────────────
        indexes = [
            # paper_trades: الأكثر استخداماً
            "CREATE INDEX IF NOT EXISTS idx_pt_status      ON paper_trades(status)",
            "CREATE INDEX IF NOT EXISTS idx_pt_symbol      ON paper_trades(symbol, status)",
            "CREATE INDEX IF NOT EXISTS idx_pt_created     ON paper_trades(created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_pt_source      ON paper_trades(source)",
            "CREATE INDEX IF NOT EXISTS idx_pt_signature   ON paper_trades(trade_signature, status, created_at)",
            # open_trades
            "CREATE INDEX IF NOT EXISTS idx_ot_status      ON open_trades(status)",
            "CREATE INDEX IF NOT EXISTS idx_ot_symbol      ON open_trades(symbol, status)",
            # signal_tracking
            "CREATE INDEX IF NOT EXISTS idx_st_key         ON signal_tracking(signal_key)",
            # balance_log
            "CREATE INDEX IF NOT EXISTS idx_bl_ts          ON balance_log(timestamp DESC)",
            # trades
            "CREATE INDEX IF NOT EXISTS idx_tr_date        ON trades(date DESC)",
        ]
        for idx_sql in indexes:
            try:
                conn.execute(idx_sql)
            except Exception:
                pass

        # RC14 — تنظيف duplicates قبل إنشاء partial UNIQUE INDEX
        # يبقي أقدم صفقة Swing مفتوحة لكل رمز ويغلق الباقي
        try:
            conn.execute("""
                UPDATE paper_trades
                SET status='duplicate_closed', close_reason='DUPLICATE_CLEANUP'
                WHERE UPPER(TRIM(selected_mode))='SWING' AND LOWER(TRIM(status))='open'
                  AND id NOT IN (
                      SELECT MIN(id) FROM paper_trades
                      WHERE UPPER(TRIM(selected_mode))='SWING' AND LOWER(TRIM(status))='open'
                      GROUP BY symbol
                  )
            """)
        except Exception as _e:
            print(f"[init_db] duplicate Swing cleanup failed: {_e}")

        # partial UNIQUE INDEX — خط الدفاع النهائي ضد duplicates متزامنة
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_swing_symbol "
                "ON paper_trades(UPPER(TRIM(symbol))) "
                "WHERE UPPER(TRIM(selected_mode))='SWING' AND LOWER(TRIM(status))='open'"
            )
        except Exception as _e:
            print(f"[init_db] uq_open_swing_symbol index failed: {_e}")

        conn.commit()


def save_trade(date, time_str, result, r_value, amount, notes="", spx_price=None, strategy="Iron Condor", trade_type="MANUAL"):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO trades (date, time, strategy, type, result, r_value, amount, notes, spx_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (date, time_str, strategy, trade_type, result, r_value, amount, notes, spx_price))
        conn.commit()


def _paper_trade_to_general_log(conn, trade_id: int, exit_price: float, result: str,
                                profit_pct: float, pnl_dollar: float, close_reason: str) -> None:
    """v3.27: نسخ الصفقة الورقية المغلقة إلى صفحة الصفقات العامة مرة واحدة."""
    try:
        t = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        if not t:
            return
        d = dict(t)
        tag = f"PaperTradeID={trade_id}"
        exists = conn.execute("SELECT id FROM trades WHERE notes LIKE ? LIMIT 1", (f"%{tag}%",)).fetchone()
        if exists:
            return
        dt = datetime.now()
        date_s = dt.strftime("%Y-%m-%d")
        time_s = dt.strftime("%H:%M")
        res_ar = "ربح" if result == "WIN" else ("خسارة" if result == "LOSS" else "تعادل")
        max_loss_pts = float(d.get("max_loss") or d.get("credit_debit") or 1)
        risk_dollar = max(max_loss_pts * 100.0, 1.0)
        r_value = round(float(pnl_dollar or 0) / risk_dollar, 2)
        amount = abs(float(pnl_dollar or 0))
        notes = (f"AutoPaper | {d.get('symbol','?')} {d.get('selected_mode','')} {d.get('strategy','')} "
                 f"| entry={float(d.get('credit_debit') or 0):.2f} exit={float(exit_price or 0):.2f} "
                 f"| {profit_pct:+.1f}% | {close_reason} | {tag}")
        conn.execute("""
            INSERT INTO trades (date, time, strategy, type, result, r_value, amount, notes, spx_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (date_s, time_s, d.get("strategy") or "Paper Trade", "AUTO_PAPER", res_ar, r_value, amount, notes, d.get("underlying_price")))
    except Exception as e:
        print(f"[paper_to_trades_log] {e}")


def get_all_trades():
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM trades ORDER BY date DESC, time DESC").fetchall()
        return [dict(r) for r in rows]


def get_stats():
    trades = get_all_trades()
    if not trades:
        return {"total": 0, "wins": 0, "losses": 0, "win_rate": 0, "net_r": 0.0}
    wins   = [t for t in trades if t["result"] in ("ربح", "ربح جزئي")]
    losses = [t for t in trades if t["result"] == "خسارة"]
    net_r  = sum(t["r_value"] for t in trades)
    win_rate = (len(wins) / len(trades) * 100) if trades else 0
    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "net_r": round(net_r, 2),
    }


def delete_trade(trade_id):
    with get_connection() as conn:
        conn.execute("DELETE FROM trades WHERE id = ?", (trade_id,))
        conn.commit()


def get_setting(key, default=""):
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def save_setting(key, value):
    with get_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
        conn.commit()


def save_iv_history(date: str, atm_iv: float, vix: float, spx_price: float,
                    symbol: str = "SPX") -> None:
    """
    حفظ IV اليومي لحساب IV Rank/Percentile الحقيقي لاحقاً.
    symbol: SPX / SPY / QQQ — كل رمز له سجل مستقل.
    """
    try:
        # رفض قيم ATM IV غير منطقية (< 1% أو > 100%)
        if atm_iv is not None and not (1.0 <= atm_iv <= 100.0):
            print(f"[save_iv_history] رُفض ATM IV={atm_iv:.2f} لـ {symbol} — خارج النطاق المنطقي")
            return
        with get_connection() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO iv_history
                   (date, symbol, atm_iv, vix, spx_price)
                   VALUES (?, ?, ?, ?, ?)""",
                (date, symbol.upper(), atm_iv, vix, spx_price)
            )
            conn.commit()
    except Exception as e:
        print(f"[save_iv_history error] {e}")


def get_iv_history(days: int = 252, symbol: str = "SPX") -> list:
    """
    جلب سجل IV لرمز محدد لحساب IV Rank/Percentile.
    يُعيد قائمة من atm_iv مرتبة تنازلياً بالتاريخ (الأحدث أولاً).
    """
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT date, atm_iv, vix FROM iv_history
                   WHERE symbol=?
                   ORDER BY date DESC LIMIT ?""",
                (symbol.upper(), days)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def save_oi_cache(options: list, expiry: str) -> None:
    """حفظ OI من آخر تحليل ناجح"""
    if not options:
        return
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with get_connection() as conn:
            for opt in options:
                strike = opt.get("strike")
                otype = opt.get("type")
                oi = opt.get("oi", 0)
                volume = opt.get("volume", 0)
                if strike and otype and oi > 0:
                    conn.execute(
                        "INSERT OR REPLACE INTO oi_cache (strike, option_type, expiry, oi, volume, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (strike, otype, expiry, oi, volume, now)
                    )
            conn.commit()
    except Exception as e:
        print(f"[save_oi_cache error] {e}")


def get_oi_cache(expiry: str) -> tuple:
    """جلب OI المحفوظ — يُعيد (قائمة_الخيارات، عمر_البيانات_بالدقائق)"""
    try:
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM oi_cache WHERE expiry = ? ORDER BY strike",
                (expiry,)
            ).fetchall()
            if not rows:
                return [], None
            data = [dict(r) for r in rows]
            updated_at = data[0].get("updated_at", "")
            try:
                age_minutes = int((datetime.now() - datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 60)
            except Exception:
                age_minutes = None
            return data, age_minutes
    except Exception:
        return [], None


# ── Open Trades ───────────────────────────────────────────────────────────────

def open_trade_exists_today(symbol: str) -> bool:
    """للتوافق مع الكود القديم — يستخدم get_open_count_for_strategy داخلياً."""
    return False  # الفحص الآن في _qualify عبر get_open_count_for_strategy


def get_open_count_for_strategy(symbol: str, strategy: str) -> int:
    """عدد الصفقات المفتوحة لنفس الرمز والاستراتيجية (open_trades فقط = 0DTE Real)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_trades WHERE symbol=? AND strategy=? AND status='open'",
            (symbol, strategy)
        ).fetchone()
    return row["cnt"] if row else 0


def get_enabled_trade_paths() -> list:
    """
    يعيد قائمة مسارات Paper Trading فقط.
    كل مسار: {symbol, trade_mode, execution_mode="Paper"}

    ملاحظة: تم تعطيل/تجاهل كل مفاتيح Auto/Real في هذه النسخة حتى لا تُسجّل
    أي صفقة في open_trades ولا يظهر مسار Auto في التنفيذ.
    """
    all_paths = [
        ("SPX", "0DTE",  "path_spx_0dte_paper"),
        ("SPY", "0DTE",  "path_spy_0dte_paper"),
        ("QQQ", "0DTE",  "path_qqq_0dte_paper"),
        ("IWM", "0DTE",  "path_iwm_0dte_paper"),
        ("SPY", "Swing", "path_spy_swing_paper"),
        ("QQQ", "Swing", "path_qqq_swing_paper"),
        ("IWM", "Swing", "path_iwm_swing_paper"),
        ("DIA", "Swing", "path_dia_swing_paper"),
        ("AAPL", "Swing", "path_aapl_swing_paper"),
        ("NVDA", "Swing", "path_nvda_swing_paper"),
        ("GLD", "Swing", "path_gld_swing_paper"),
    ]
    enabled = []
    for sym, mode, paper_key in all_paths:
        if get_setting(paper_key, "0") == "1":
            enabled.append({"symbol": sym, "trade_mode": mode, "execution_mode": "Paper"})
    return enabled


def get_swing_paper_open_count(symbol: str) -> int:
    """
    عدد صفقات Swing المفتوحة في paper_trades لنفس الرمز.
    يُستخدم لمنع فتح أكثر من Swing واحدة في اليوم.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM paper_trades
               WHERE UPPER(TRIM(symbol))=UPPER(TRIM(?))
               AND UPPER(TRIM(selected_mode))='SWING'
               AND LOWER(TRIM(status))='open'
               AND DATE(timestamp) = ?""",
            (symbol, today)
        ).fetchone()
    return row["cnt"] if row else 0


def get_swing_paper_open_count_any() -> int:
    """إجمالي Swing المفتوحة في paper_trades (كل الرموز)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM paper_trades WHERE UPPER(TRIM(selected_mode))='SWING' AND LOWER(TRIM(status))='open'"
        ).fetchone()
    return row["cnt"] if row else 0


def get_paper_open_count_for_strategy(symbol: str, strategy: str, selected_mode: str = "0DTE") -> int:
    """عدد صفقات Paper المفتوحة لنفس الرمز والاستراتيجية والمسار."""
    with get_connection() as conn:
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM paper_trades
               WHERE symbol=? AND strategy=? AND selected_mode=? AND status='open'""",
            (symbol, strategy, selected_mode)
        ).fetchone()
    return row["cnt"] if row else 0


def get_total_risk_report() -> dict:
    """
    تقرير إجمالي المخاطرة:
      open_risk_0dte  : إجمالي margin محجوز للـ 0DTE Real
      open_risk_swing : تقدير مخاطرة Swing Paper (max_loss × عدد الصفقات)
      total_open_risk : المجموع
      trades_0dte     : عدد صفقات 0DTE المفتوحة
      trades_swing    : عدد صفقات Swing المفتوحة
    """
    # 0DTE Real — من balance_log
    bal       = get_paper_balance()
    reserved  = bal.get("reserved", 0.0)

    # Swing Paper — نحسب من paper_trades المفتوحة
    swing_risk = 0.0
    swing_count = 0
    try:
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT credit_debit, max_loss, strategy FROM paper_trades
                   WHERE UPPER(TRIM(selected_mode))='SWING' AND LOWER(TRIM(status))='open'"""
            ).fetchall()
        swing_count = len(rows)
        for r in rows:
            ml = r["max_loss"]
            if ml:
                swing_risk += abs(float(ml)) * 100   # per contract
            else:
                # تقدير: debit كامل هو الـ max loss
                cd = r["credit_debit"] or 0
                swing_risk += abs(float(cd)) * 100
    except Exception:
        pass

    return {
        "open_risk_0dte":  round(reserved, 2),
        "open_risk_swing": round(swing_risk, 2),
        "total_open_risk": round(reserved + swing_risk, 2),
        "trades_0dte":     get_open_trades_count(),
        "trades_swing":    swing_count,
    }


def get_open_count_for_symbol(symbol: str) -> int:
    """عدد الصفقات المفتوحة لنفس الرمز (جميع الاستراتيجيات)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_trades WHERE symbol=? AND status='open'",
            (symbol,)
        ).fetchone()
    return row["cnt"] if row else 0


def get_open_trades_count() -> int:
    """عدد الصفقات المفتوحة حالياً (كل الرموز)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM open_trades WHERE status='open'"
        ).fetchone()
    return row["cnt"] if row else 0


def log_open_trade(symbol, strategy, entry_price, legs_json, credit_debit,
                   target, is_credit, score,
                   trade_mode: str = "0DTE",
                   dte_at_entry: int = 0,
                   expiry_date: str = "",
                   execution_mode: str = "Auto",
                   settlement_type: str = "PM",
                   root_symbol: str = "SPXW",
                   last_trade_time: str = "15:55 ET") -> int:
    now = datetime.now()
    with get_connection() as conn:
        cur = conn.execute("""
            INSERT INTO open_trades
            (symbol, strategy, entry_date, entry_time, entry_price,
             legs_json, credit_debit, target, is_credit, score,
             trade_mode, dte_at_entry, expiry_date, execution_mode,
             settlement_type, root_symbol, last_trade_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, strategy, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
              entry_price, legs_json, credit_debit, target, int(is_credit), score,
              trade_mode, dte_at_entry, expiry_date, execution_mode,
              settlement_type, root_symbol, last_trade_time))
        conn.commit()
        return cur.lastrowid


def log_signal_rejection(symbol: str, strategy: str, score: int, reason: str) -> None:
    """يحفظ سبب رفض الإشارة للعرض في Auto Trades."""
    try:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    symbol TEXT,
                    strategy TEXT,
                    score INTEGER,
                    reason TEXT
                )
            """)
            conn.execute(
                "INSERT INTO signal_rejections (timestamp, symbol, strategy, score, reason) VALUES (?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, strategy, score, reason)
            )
            # احتفظ بآخر 100 رفض فقط
            conn.execute(
                "DELETE FROM signal_rejections WHERE id NOT IN (SELECT id FROM signal_rejections ORDER BY id DESC LIMIT 100)"
            )
            conn.commit()
    except Exception:
        pass


def get_recent_rejections(limit: int = 20) -> list:
    try:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS signal_rejections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT, symbol TEXT, strategy TEXT, score INTEGER, reason TEXT
                )
            """)
            rows = conn.execute(
                "SELECT * FROM signal_rejections ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def get_open_trades() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM open_trades WHERE status='open' ORDER BY created_at DESC"
        ).fetchall()
    statuses = [dict(r) for r in rows]
    trades = get_paper_trades(status="closed", limit=1000)

    def _combo_stats(mode: str, strategy: str, symbol: str) -> dict:
        subset = [
            t for t in trades
            if t.get("selected_mode") == mode
            and t.get("strategy") == strategy
            and t.get("symbol") == symbol
        ]
        total = len(subset)
        if not total:
            return {"sample_size": 0, "pf_current": None, "exp_current": None}
        wins = [t for t in subset if t.get("result") == "WIN"]
        losses = [t for t in subset if t.get("result") == "LOSS"]
        gross_p = sum(t.get("pnl_dollar") or 0 for t in wins)
        gross_l = abs(sum(t.get("pnl_dollar") or 0 for t in losses))
        pf = round(gross_p / gross_l, 2) if gross_l > 0 else 99.0
        wr = len(wins) / total
        avg_w = sum(t.get("profit_pct") or 0 for t in wins) / len(wins) if wins else 0
        avg_l = sum(abs(t.get("profit_pct") or 0) for t in losses) / len(losses) if losses else 0
        exp = round((wr * avg_w) - ((1 - wr) * avg_l), 2)
        return {"sample_size": total, "pf_current": pf, "exp_current": exp}

    for s in statuses:
        s.update(_combo_stats(
            s.get("trade_mode", "0DTE"),
            s.get("strategy", ""),
            s.get("symbol", ""),
        ))
    return statuses


def get_all_open_trades() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM open_trades ORDER BY created_at DESC"
        ).fetchall()
    statuses = [dict(r) for r in rows]
    trades = get_paper_trades(status="closed", limit=1000)

    def _combo_stats(mode: str, strategy: str, symbol: str) -> dict:
        subset = [
            t for t in trades
            if t.get("selected_mode") == mode
            and t.get("strategy") == strategy
            and t.get("symbol") == symbol
        ]
        total = len(subset)
        if not total:
            return {"sample_size": 0, "pf_current": None, "exp_current": None}
        wins = [t for t in subset if t.get("result") == "WIN"]
        losses = [t for t in subset if t.get("result") == "LOSS"]
        gross_p = sum(t.get("pnl_dollar") or 0 for t in wins)
        gross_l = abs(sum(t.get("pnl_dollar") or 0 for t in losses))
        pf = round(gross_p / gross_l, 2) if gross_l > 0 else 99.0
        wr = len(wins) / total
        avg_w = sum(t.get("profit_pct") or 0 for t in wins) / len(wins) if wins else 0
        avg_l = sum(abs(t.get("profit_pct") or 0) for t in losses) / len(losses) if losses else 0
        exp = round((wr * avg_w) - ((1 - wr) * avg_l), 2)
        return {"sample_size": total, "pf_current": pf, "exp_current": exp}

    for s in statuses:
        s.update(_combo_stats(
            s.get("trade_mode", "0DTE"),
            s.get("strategy", ""),
            s.get("symbol", ""),
        ))
    return statuses


def update_open_trade_live(trade_id: int, current_value: float,
                           pnl_dollar: float, pnl_pct: float) -> None:
    """تحديث السعر الحالي والـ P&L للصفقة المفتوحة."""
    now = datetime.now().strftime("%H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
            UPDATE open_trades
            SET current_value=?, pnl_dollar=?, pnl_pct=?, last_updated=?
            WHERE id=?
        """, (current_value, pnl_dollar, pnl_pct, now, trade_id))
        conn.commit()


def get_trade_by_id(trade_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM open_trades WHERE id=?", (trade_id,)
        ).fetchone()
    return dict(row) if row else {}


def close_open_trade(trade_id: int, exit_price: float, result: str,
                     profit_pct: float, close_reason: str) -> None:
    now = datetime.now()
    with get_connection() as conn:
        conn.execute("""
            UPDATE open_trades
            SET status=?, exit_price=?, exit_date=?, exit_time=?,
                result=?, profit_pct=?, close_reason=?
            WHERE id=? AND LOWER(TRIM(status))='open'
        """, ("closed", exit_price, now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
              result, profit_pct, close_reason, trade_id))
        conn.commit()


def save_analysis_log(analysis: dict) -> None:
    """حفظ سجل التحليل في قاعدة البيانات"""
    try:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analysis_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    spx_price REAL,
                    pin_score REAL,
                    strategy TEXT,
                    strategy_score INTEGER,
                    em REAL,
                    zero_gamma REAL,
                    net_gex REAL,
                    price_source TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                INSERT INTO analysis_logs
                (timestamp, spx_price, pin_score, strategy, strategy_score,
                 em, zero_gamma, net_gex, price_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                analysis.get("timestamp", ""),
                analysis.get("price", 0),
                analysis.get("pin_score", 0),
                analysis.get("strategy", {}).get("strategy", "") if analysis.get("strategy") else "",
                analysis.get("strategy", {}).get("score", 0) if analysis.get("strategy") else 0,
                analysis.get("expected_move", 0),
                analysis.get("zero_gamma", 0),
                analysis.get("net_gex", 0),
                analysis.get("price_source", ""),
            ))
            conn.commit()
    except Exception as e:
        print(f"[save_analysis_log error] {e}")


def get_backtest_data() -> list:
    """ربط سجلات التحليل بنتائج الصفقات لعرض أداء الاستراتيجيات"""
    try:
        with get_connection() as conn:
            logs = conn.execute(
                "SELECT * FROM analysis_logs ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
            trades = conn.execute(
                "SELECT * FROM trades ORDER BY date DESC, time DESC"
            ).fetchall()
            return {
                "logs": [dict(r) for r in logs],
                "trades": [dict(r) for r in trades],
            }
    except Exception as e:
        print(f"[get_backtest_data error] {e}")
        return {"logs": [], "trades": []}


def log_mode_performance(trade: dict) -> None:
    """يُسجّل نتيجة الصفقة في جدول أداء الـ Mode عند الإغلاق."""
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO mode_performance
                (trade_mode, symbol, strategy, entry_date, dte_at_entry,
                 credit_debit, exit_price, result, profit_pct, pnl_dollar, close_reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                trade.get("trade_mode", "0DTE"),
                trade.get("symbol", ""),
                trade.get("strategy", ""),
                trade.get("entry_date", ""),
                trade.get("dte_at_entry", 0),
                trade.get("credit_debit"),
                trade.get("exit_price"),
                trade.get("result"),
                trade.get("profit_pct"),
                trade.get("pnl_dollar"),
                trade.get("close_reason", ""),
            ))
            conn.commit()
    except Exception as e:
        print(f"[log_mode_performance error] {e}")


def get_mode_performance(mode: str = None) -> dict:
    """
    إحصاءات الأداء لكل Mode أو لـ Mode محدد.
    يُعيد: win_rate, avg_return, profit_factor, expectancy, total_trades
    """
    try:
        with get_connection() as conn:
            query = "SELECT * FROM mode_performance"
            params = ()
            if mode:
                query += " WHERE trade_mode = ?"
                params = (mode,)
            rows = [dict(r) for r in conn.execute(query, params).fetchall()]

        if not rows:
            return {}

        def _stats(trades):
            if not trades:
                return {}
            wins   = [t for t in trades if t.get("result") == "WIN"]
            losses = [t for t in trades if t.get("result") == "LOSS"]
            total  = len(trades)
            win_rate = round(len(wins) / total * 100, 1) if total else 0

            avg_win  = round(sum(t.get("profit_pct") or 0 for t in wins)  / len(wins),  1) if wins   else 0
            avg_loss = round(sum(abs(t.get("profit_pct") or 0) for t in losses) / len(losses), 1) if losses else 0

            gross_profit = sum(t.get("pnl_dollar") or 0 for t in wins)
            gross_loss   = abs(sum(t.get("pnl_dollar") or 0 for t in losses))
            profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else None

            expectancy = round(
                (win_rate/100 * avg_win) - ((1 - win_rate/100) * avg_loss), 2
            )
            return {
                "total_trades":   total,
                "win_rate":       win_rate,
                "avg_win_pct":    avg_win,
                "avg_loss_pct":   avg_loss,
                "profit_factor":  profit_factor,
                "expectancy":     expectancy,
            }

        if mode:
            return _stats(rows)

        # منفصل لكل Mode
        modes = {}
        for m in set(r.get("trade_mode", "0DTE") for r in rows):
            modes[m] = _stats([r for r in rows if r.get("trade_mode") == m])
        return modes

    except Exception as e:
        print(f"[get_mode_performance error] {e}")
        return {}


def get_recent_analysis_logs(limit: int = 20) -> list:
    """جلب آخر سجلات التحليل"""
    try:
        with get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS analysis_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    spx_price REAL,
                    pin_score REAL,
                    strategy TEXT,
                    strategy_score INTEGER,
                    em REAL,
                    zero_gamma REAL,
                    net_gex REAL,
                    price_source TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            rows = conn.execute(
                "SELECT * FROM analysis_logs ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        print(f"[get_recent_analysis_logs error] {e}")
        return []


# ── Paper Trading ─────────────────────────────────────────────────────────────


def clear_paper_data() -> None:
    """Clear Paper Trading data and strategy status state."""
    with get_connection() as conn:
        conn.execute("DELETE FROM paper_trades")
        conn.execute("DELETE FROM strategy_status")
        conn.execute("DELETE FROM signal_tracking")
        conn.commit()


def delete_paper_trades(ids: list) -> int:
    """حذف توصيات Paper Trading بحسب قائمة IDs. يُعيد عدد الصفوف المحذوفة."""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_connection() as conn:
        cur = conn.execute(
            f"DELETE FROM paper_trades WHERE id IN ({placeholders})", ids)
        conn.commit()
        return cur.rowcount


def _format_signal_age(first_time: str, current_time: str) -> str:
    try:
        start = datetime.strptime(first_time, "%Y-%m-%d %H:%M:%S")
        now = datetime.strptime(current_time, "%Y-%m-%d %H:%M:%S")
        mins = max(int((now - start).total_seconds() // 60), 0)
        h, m = divmod(mins, 60)
        return f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        return "0m"


def signal_key_for(strat: dict, symbol: str, selected_mode: str) -> str:
    legs = [
        strat.get("short_put"), strat.get("long_put"),
        strat.get("short_call"), strat.get("long_call"),
        strat.get("expiry_date") or strat.get("target_dte") or "",
    ]
    legs_txt = "|".join(str(x) for x in legs if x not in (None, ""))
    return f"{symbol}|{selected_mode}|{strat.get('strategy','')}|{legs_txt}"


def update_signal_tracking(strat: dict, symbol: str, selected_mode: str,
                           current_price: float, current_time: str = None) -> dict:
    now_str = current_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    key = signal_key_for(strat, symbol, selected_mode)
    score = float(strat.get("score") or 0)
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM signal_tracking WHERE signal_key=?", (key,)).fetchone()
        if row:
            first_time = row["first_detected_time"]
            initial_score = row["initial_score"] or score
            peak_score = max(float(row["peak_score"] or score), score)
            price_then = row["price_first_detected"]
            conn.execute("""
                UPDATE signal_tracking
                SET last_seen_time=?, current_score=?, peak_score=?, current_price=?
                WHERE signal_key=?
            """, (now_str, score, peak_score, current_price, key))
        else:
            first_time = now_str
            initial_score = score
            peak_score = score
            price_then = current_price
            conn.execute("""
                INSERT INTO signal_tracking
                (signal_key, symbol, selected_mode, strategy,
                 first_detected_time, last_seen_time,
                 initial_score, current_score, peak_score,
                 price_first_detected, current_price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (key, symbol, selected_mode, strat.get("strategy", ""),
                  now_str, now_str, score, score, score,
                  current_price, current_price))
        conn.commit()
    return {
        "signal_key": key,
        "first_detected_time": first_time,
        "current_signal_time": now_str,
        "signal_age": _format_signal_age(first_time, now_str),
        "initial_score": initial_score,
        "current_score": score,
        "peak_score": peak_score,
        "price_first_detected": price_then,
        "current_price": current_price,
    }



def _ny_now_for_study():
    """Return current New York time for time-of-day paper-trade study."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        # Fallback: keep bot running even if zoneinfo is unavailable.
        return datetime.now()


def _market_time_bucket(ny_dt) -> tuple:
    """Return (minutes_since_open, bucket label) for US regular session."""
    mins = ny_dt.hour * 60 + ny_dt.minute
    open_m = 9 * 60 + 30
    mso = mins - open_m
    if mso < 0:
        return mso, "Pre-market"
    if mso < 15:
        return mso, "09:30-09:45 Opening 15m"
    if mso < 60:
        return mso, "09:45-10:30 Early Session"
    if mso < 150:
        return mso, "10:30-12:00 Morning Trend"
    if mso < 270:
        return mso, "12:00-14:00 Midday"
    if mso < 330:
        return mso, "14:00-15:00 Afternoon"
    if mso < 360:
        return mso, "15:00-15:30 Late Session"
    if mso < 390:
        return mso, "15:30-16:00 Close Risk"
    return mso, "After-hours"


def _infer_setup_direction(strategy: str, strat: dict = None) -> str:
    """Classify setup direction for repetition-study grouping only; it does not block trades."""
    st = (strategy or "").lower()
    if "put debit" in st or "bear call" in st:
        return "bearish"
    if "call debit" in st or "bull put" in st:
        return "bullish"
    if "iron condor" in st or "condor" in st:
        return "neutral"
    # Optional explicit direction if future strategy objects provide it.
    if strat:
        d = (strat.get("direction") or strat.get("bias") or "").lower()
        if d in ("bullish", "bearish", "neutral"):
            return d
    return "unknown"


def _parse_db_datetime(value: str):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except Exception:
            pass
    return None


def _same_setup_metrics(conn, same_setup_key: str, now_local: datetime) -> dict:
    """Collect repetition metrics without blocking the trade."""
    out = {
        "open_count": 0,
        "closed_today": 0,
        "minutes_since_last": None,
        "is_repeated": 0,
        "note": "First observed setup in current DB window",
    }
    if not same_setup_key:
        return out
    try:
        open_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM paper_trades WHERE same_setup_key=? AND status='open'",
            (same_setup_key,)
        ).fetchone()["cnt"]
        closed_today = conn.execute(
            "SELECT COUNT(*) AS cnt FROM paper_trades WHERE same_setup_key=? AND status='closed' AND date(timestamp)=date(?)",
            (same_setup_key, now_local.strftime("%Y-%m-%d %H:%M:%S"))
        ).fetchone()["cnt"]
        last = conn.execute(
            """SELECT timestamp, created_at FROM paper_trades
               WHERE same_setup_key=?
               ORDER BY COALESCE(created_at, timestamp) DESC, id DESC LIMIT 1""",
            (same_setup_key,)
        ).fetchone()
        minutes_since = None
        if last:
            last_dt = _parse_db_datetime(last["timestamp"] or last["created_at"])
            if last_dt:
                minutes_since = round((now_local - last_dt).total_seconds() / 60.0, 1)
        is_rep = 1 if (open_count or 0) > 0 or (closed_today or 0) > 0 else 0
        if is_rep:
            note = f"Repeated setup tracked only: open_before={open_count}, closed_today={closed_today}"
            if minutes_since is not None:
                note += f", last_seen={minutes_since:.1f}m ago"
        else:
            note = "First setup entry — no blocking applied"
        out.update({
            "open_count": int(open_count or 0),
            "closed_today": int(closed_today or 0),
            "minutes_since_last": minutes_since,
            "is_repeated": is_rep,
            "note": note,
        })
    except Exception as e:
        out["note"] = f"Repetition metrics unavailable: {e}"
    return out



def _build_entry_smc_snapshot(strat: dict, analysis: dict, symbol: str, selected_mode: str) -> str:
    """RC12b: persist the SMC 0DTE MTF context exactly as seen at paper-trade entry.

    Diagnostics only. This does not change scoring, filtering, exits, or strategy rules.
    Stored as JSON so open/closed Paper Trade details can be audited later.
    """
    try:
        import json
        sym = (symbol or "").upper()
        mode = selected_mode or ""
        if sym not in ("SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD"):
            return None
        smc = None
        if isinstance(strat, dict):
            smc = strat.get("smc_0dte_mtf_full") or strat.get("smc_0dte_mtf")
        if not smc and isinstance(analysis, dict):
            smc = analysis.get("smc_0dte_mtf_full") or analysis.get("smc_0dte_mtf")
        if not isinstance(smc, dict) or not smc:
            smc = {"available": False, "reason": "smc_snapshot_missing_at_entry", "scope": "Swing/0DTE diagnostics"}
        snap = {
            "snapshot_type": "entry_smc_0dte_mtf",
            "diagnostics_only": True,
            "captured_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": sym,
            "selected_mode": mode,
            "strategy": (strat or {}).get("strategy", ""),
            "underlying_price": (strat or {}).get("underlying_price") or (strat or {}).get("entry_price") or (strat or {}).get("current_price"),
            "ict_smc_score": (strat or {}).get("ict_smc_score") or smc.get("ict_smc_score"),
            "ict_smc_pass": (strat or {}).get("ict_smc_pass") if (strat or {}).get("ict_smc_pass") is not None else smc.get("ict_smc_pass"),
            "ict_smc_confidence": (strat or {}).get("ict_smc_confidence") or smc.get("ict_smc_confidence"),
            "ema_alignment_pass": (strat or {}).get("ema_alignment_pass") if (strat or {}).get("ema_alignment_pass") is not None else smc.get("ema_alignment_pass"),
            "swing_block_reason": (strat or {}).get("swing_block_reason"),
            "swing_watchlist_reason": (strat or {}).get("swing_watchlist_reason"),
            "ict_smc_details": (strat or {}).get("ict_smc_details") or smc.get("ict_smc_details"),
            "ema_details": (strat or {}).get("ema_details") or smc.get("ema_details"),
            "smc_0dte_mtf": smc,
        }
        return json.dumps(snap, ensure_ascii=False, default=str)
    except Exception as exc:
        try:
            return json.dumps({
                "snapshot_type": "entry_smc_0dte_mtf",
                "diagnostics_only": True,
                "available": False,
                "error": f"snapshot_build_error:{type(exc).__name__}:{str(exc)[:160]}",
                "scope": "Swing/0DTE diagnostics",
            }, ensure_ascii=False)
        except Exception:
            return None

def log_paper_trade(strat: dict, symbol: str, selected_mode: str,
                    analysis: dict = None,
                    source: str = None) -> int:
    """
    يسجل توصية Paper Trading بكل التفاصيل المطلوبة.

    source:
      "paper_execution"  — صفقة ورقية فعلية مفعّلة من إعدادات المسارات
      "qualified_signal" — إشارة مؤهلة للمراقبة فقط (بدون Paper path مفعّل)
      "swing_auto_paper" — Swing موجَّه لـ Paper
      "signal_scan"      — تسجيل تلقائي من دورة التحليل (legacy)
    """
    import json
    now = datetime.now()
    a   = analysis or {}
    # source: معامل صريح يسبق قيمة analysis["source"]
    _source = source or a.get("source", "auto")

    # v3.30: Time-of-Day & Repetition Study — تسجيل فقط، بدون منع أي صفقة.
    ny_now = _ny_now_for_study()
    entry_time_ny = ny_now.strftime("%Y-%m-%d %H:%M:%S")
    minutes_since_open, time_bucket = _market_time_bucket(ny_now)

    # حساب debit_ratio
    val  = strat.get("credit") or strat.get("debit") or 0
    ml   = strat.get("max_loss") or 0
    mp   = strat.get("max_profit") or strat.get("max_gain") or strat.get("credit") or 0
    rr   = strat.get("reward_risk") or 0
    wing = abs((strat.get("short_put") or strat.get("short_call") or 0) -
               (strat.get("long_put")  or strat.get("long_call")  or 0))
    debit_ratio = round(val / wing * 100, 1) if (wing > 0 and val) else None

    # bid/ask check
    legs_ok = all(
        (leg.get("bid") or 0) > 0 and (leg.get("ask") or 0) > 0
        for leg in (strat.get("legs_detail") or []) if leg
    )

    entry_reasons = json.dumps(strat.get("reasons", []), ensure_ascii=False)
    mode_reasons  = json.dumps(strat.get("mode_reasons", []), ensure_ascii=False)
    setup_direction = _infer_setup_direction(strat.get("strategy", ""), strat)
    same_setup_key = "|".join([
        (symbol or "").upper(),
        selected_mode or "",
        setup_direction or "unknown",
        strat.get("strategy", "") or "",
        strat.get("expiry_date", "") or "",
    ])
    sig = {k: strat.get(k) for k in (
        "signal_key", "trade_signature", "duplicate_note", "first_detected_time", "current_signal_time",
        "signal_age", "initial_score", "current_score", "peak_score",
        "current_price", "price_first_detected"
    )}
    entry_smc_snapshot_json = _build_entry_smc_snapshot(strat, a, symbol, selected_mode)

    # RC14 — pipeline diagnostic codes
    # analysis (نتيجة analyze_swing) هو المصدر الأول لأن القيم النهائية تُحسم فيه
    # strat هو fallback فقط في حالة استدعاء مباشر دون analysis
    def _dc(field, default=None):
        v = a.get(field)
        if v is None:
            v = strat.get(field)
        return v if v is not None else default

    pipeline_stop_code    = _dc("pipeline_stop_code",    "SMC_EVALUATED")
    smc_evaluation        = _dc("smc_evaluation",        "NOT_EVALUATED")
    ds_evaluation         = _dc("ds_evaluation",         "NOT_EVALUATED")
    ds_unavailable_reason = _dc("ds_unavailable_reason")
    entry_protection      = _dc("entry_protection",      "NOT_EVALUATED")
    diagnostic_code       = _dc("diagnostic_code",       "SMC_NOT_EVALUATED")

    with get_connection() as conn:
        # RC14 — حارس ذري: BEGIN IMMEDIATE يحجز قفل الكتابة فور بدء الفحص
        # يمنع سيناريو Thread-A-SELECT / Thread-B-SELECT / both-INSERT
        # الطبقة الثانية: uq_open_swing_symbol UNIQUE INDEX يرفع IntegrityError كخط دفاع نهائي
        if str(selected_mode or "").upper().strip() == "SWING":
            # RC15f final registration guard. Scope is directional Swing Debit only.
            # Credit/range strategies must be handled by a future RC15g Range/IV/Sigma layer
            # and are not blocked for missing ICT/SMC displacement data here.
            _strategy_for_rc15f = str((strat or {}).get("strategy") or "").strip()
            if _strategy_for_rc15f in ("Call Debit Spread", "Put Debit Spread"):
                try:
                    ict_score = int((strat or {}).get("ict_smc_score") or 0)
                except Exception:
                    ict_score = 0
                ict_pass = bool((strat or {}).get("ict_smc_pass")) and ict_score >= 4
                ema_pass = bool((strat or {}).get("ema_alignment_pass"))
                rc15f_diag = (strat or {}).get("rc15f_swing_confirmation") or {}
                smc = (
                    (strat or {}).get("smc_0dte_mtf_full")
                    or (strat or {}).get("smc_0dte_mtf")
                    or (a or {}).get("smc_0dte_mtf_full")
                    or (a or {}).get("smc_0dte_mtf")
                    or {}
                )
                h1 = smc.get("h1") if isinstance(smc, dict) and isinstance(smc.get("h1"), dict) else {}
                m15 = smc.get("m15") if isinstance(smc, dict) and isinstance(smc.get("m15"), dict) else {}
                insufficient_rc15f_data = (
                    not isinstance(smc, dict)
                    or smc.get("available") is not True
                    or h1.get("available") is not True
                    or m15.get("available") is not True
                    or (strat or {}).get("swing_block_reason") == "insufficient_ict_smc_data"
                    or rc15f_diag.get("swing_block_reason") == "insufficient_ict_smc_data"
                )
                if insufficient_rc15f_data or not (ict_pass and ema_pass):
                    reason = (
                        "insufficient_ict_smc_data" if insufficient_rc15f_data
                        else ((strat or {}).get("swing_block_reason")
                              or (strat or {}).get("swing_watchlist_reason")
                              or f"RC15f Swing blocked before INSERT: ict_smc_score={ict_score}, ema_alignment_pass={ema_pass}")
                    )
                    print(f"[log_paper_trade] RC15f_BLOCKED_SWING_INSERT: {symbol} - {reason}")
                    return 0
            else:
                try:
                    (strat or {}).setdefault("rc15f_swing_confirmation", {
                        "applied": False,
                        "not_applicable": True,
                        "decision": "NOT_APPLICABLE_DIRECTIONAL_DEBIT_ONLY",
                        "reason": "RC15f applies only to Swing Call Debit Spread / Put Debit Spread",
                    })
                except Exception:
                    pass
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                print(f"[log_paper_trade] BEGIN IMMEDIATE warning: {e} — proceeding without exclusive lock")
            except Exception as e:
                print(f"[log_paper_trade] BEGIN IMMEDIATE unexpected error: {e}")
            _dup = conn.execute(
                "SELECT id FROM paper_trades "
                "WHERE UPPER(TRIM(symbol))=UPPER(TRIM(?)) "
                "AND UPPER(TRIM(selected_mode))='SWING' "
                "AND LOWER(TRIM(status))='open' LIMIT 1",
                (symbol,)
            ).fetchone()
            if _dup:
                print(f"[log_paper_trade] DUPLICATE_OPEN_SWING_SYMBOL: {symbol} — open Swing #{_dup['id']} exists, skipping INSERT")
                return 0   # 0 = لم يُدرج (caller يتحقق > 0)

        rep = _same_setup_metrics(conn, same_setup_key, now)
        cur = conn.execute("""
            INSERT INTO paper_trades
            (timestamp, symbol, selected_mode, strategy, expiry_date, dte_at_entry,
             short_put, long_put, short_call, long_call,
             credit_debit, max_loss, max_profit, reward_risk, score,
             entry_reasons, mode_reasons, bid_ask_ok, debit_ratio, setup_quality,
             source, settlement_type, root_symbol, last_trade_time,
             signal_key, trade_signature, duplicate_note, first_detected_time, current_signal_time, signal_age,
             initial_score, current_score, peak_score, current_price, price_first_detected,
             underlying_price, expected_move, em_upper, em_lower, sigma_distance, sigma_side,
             short_delta, long_delta, short_put_delta, long_put_delta, short_call_delta, long_call_delta, net_delta, short_strike, long_strike,
             distance_points, distance_pct, target_delta,
             credit_width_ratio, min_credit_width_ratio, credit_width_ok, credit_width_note,
             entry_smc_snapshot_json,
             entry_time_ny, minutes_since_market_open, time_bucket, setup_direction, same_setup_key,
             is_repeated_setup, same_setup_open_count_before_entry, same_setup_closed_count_today,
             minutes_since_last_same_setup, repetition_note,
             pipeline_stop_code, smc_evaluation, ds_evaluation,
             ds_unavailable_reason, entry_protection, diagnostic_code)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            now.strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            selected_mode,
            strat.get("strategy", ""),
            strat.get("expiry_date", ""),
            strat.get("dte_at_entry", 0),
            strat.get("short_put"),
            strat.get("long_put"),
            strat.get("short_call"),
            strat.get("long_call"),
            val,
            ml or None,
            mp or None,
            rr or None,
            strat.get("score", 0),
            entry_reasons,
            mode_reasons,
            int(legs_ok),
            debit_ratio,
            strat.get("setup_quality", ""),
            _source,
            strat.get("settlement_type", "PM"),
            strat.get("root_symbol", "SPXW"),
            strat.get("last_trade_time", "15:55 ET"),
            sig.get("signal_key"),
            sig.get("trade_signature"),
            sig.get("duplicate_note"),
            sig.get("first_detected_time"),
            sig.get("current_signal_time"),
            sig.get("signal_age"),
            sig.get("initial_score"),
            sig.get("current_score"),
            sig.get("peak_score"),
            sig.get("current_price"),
            sig.get("price_first_detected"),
            strat.get("underlying_price") or strat.get("entry_price") or strat.get("current_price"),
            strat.get("expected_move"),
            strat.get("em_upper"),
            strat.get("em_lower"),
            strat.get("sigma_distance"),
            strat.get("sigma_side"),
            strat.get("short_delta"),
            strat.get("long_delta"),
            strat.get("short_put_delta"),
            strat.get("long_put_delta"),
            strat.get("short_call_delta"),
            strat.get("long_call_delta"),
            strat.get("net_delta"),
            strat.get("short_strike"),
            strat.get("long_strike"),
            strat.get("distance_points"),
            strat.get("distance_pct"),
            strat.get("target_delta"),
            strat.get("credit_width_ratio"),
            strat.get("min_credit_width_ratio"),
            1 if strat.get("credit_width_ok", True) else 0,
            strat.get("credit_width_note"),
            entry_smc_snapshot_json,
            entry_time_ny,
            minutes_since_open,
            time_bucket,
            setup_direction,
            same_setup_key,
            rep.get("is_repeated", 0),
            rep.get("open_count", 0),
            rep.get("closed_today", 0),
            rep.get("minutes_since_last"),
            rep.get("note"),
            pipeline_stop_code,
            smc_evaluation,
            ds_evaluation,
            ds_unavailable_reason,
            entry_protection,
            diagnostic_code,
        ))
        try:
            conn.commit()
        except sqlite3.IntegrityError as e:
            print(f"[log_paper_trade] DUPLICATE blocked by UNIQUE index: {e}")
            return 0
        return cur.lastrowid


def update_paper_trade_live(trade_id: int, current_value: float,
                            pnl_dollar: float, pnl_pct: float,
                            exit_trigger: str = "") -> None:
    """
    v3.33: تحديث القيمة الحالية والربح/الخسارة للصفقات الورقية المفتوحة،
    مع حفظ أفضل قيمة وأعلى P&L وصلت لها الصفقة أثناء المراقبة.
    """
    now = datetime.now().strftime("%H:%M:%S")
    with get_connection() as conn:
        row = conn.execute(
            "SELECT best_pnl_pct_seen FROM paper_trades WHERE id=?",
            (trade_id,)
        ).fetchone()
        best_old = None if row is None else row["best_pnl_pct_seen"]
        is_new_best = best_old is None or float(pnl_pct or 0) > float(best_old or 0)
        if is_new_best:
            conn.execute("""
                UPDATE paper_trades
                SET current_value=?, current_pnl_dollar=?, current_pnl_pct=?, exit_trigger=?, last_updated=?,
                    best_value_seen=?, best_pnl_dollar_seen=?, best_pnl_pct_seen=?, best_seen_at=?,
                    monitor_checks=COALESCE(monitor_checks,0)+1
                WHERE id=? AND status='open'
            """, (current_value, pnl_dollar, pnl_pct, exit_trigger, now,
                  current_value, pnl_dollar, pnl_pct, now, trade_id))
        else:
            conn.execute("""
                UPDATE paper_trades
                SET current_value=?, current_pnl_dollar=?, current_pnl_pct=?, exit_trigger=?, last_updated=?,
                    monitor_checks=COALESCE(monitor_checks,0)+1
                WHERE id=? AND status='open'
            """, (current_value, pnl_dollar, pnl_pct, exit_trigger, now, trade_id))
        conn.commit()


def update_paper_trade_exit_trigger_only(trade_id: int, exit_trigger: str) -> None:
    """
    v3.33.6a RC2: تحديث حقل exit_trigger فقط — بدون لمس P&L أو current_value.
    تُستخدم حصراً عند pending_chain=True (انتظار chain بعد 15:20)
    لإظهار الحالة في الواجهة: "exit_pending_due_to_missing_chain_after_1520".
    """
    now = datetime.now().strftime("%H:%M:%S")
    with get_connection() as conn:
        conn.execute(
            "UPDATE paper_trades SET exit_trigger=?, last_updated=? WHERE id=? AND status='open'",
            (exit_trigger, now, trade_id)
        )
        conn.commit()


def close_paper_trade(trade_id: int, exit_price: float, result: str,
                      profit_pct: float, pnl_dollar: float,
                      close_reason: str) -> None:
    """يُغلق توصية Paper Trading بالنتيجة + يربطها بسجل الصفقات ويرسل Telegram."""
    now = datetime.now()
    with get_connection() as conn:
        # تأكد أن قيمة الإغلاق نفسها تُحتسب ضمن أفضل P&L إذا كانت أفضل من السابق.
        row = conn.execute("SELECT best_pnl_pct_seen FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
        best_old = None if row is None else row["best_pnl_pct_seen"]
        best_sql = ""
        params_extra = []
        if best_old is None or float(profit_pct or 0) > float(best_old or 0):
            best_sql = ", best_value_seen=?, best_pnl_dollar_seen=?, best_pnl_pct_seen=?, best_seen_at=?"
            params_extra = [exit_price, pnl_dollar, profit_pct, now.strftime("%H:%M:%S")]
        cur_close = conn.execute(f"""
            UPDATE paper_trades
            SET status=?, exit_price=?, exit_date=?, result=?,
                profit_pct=?, pnl_dollar=?, close_reason=?,
                current_value=?, current_pnl_dollar=?, current_pnl_pct=?, exit_trigger=?, last_updated=?{best_sql}
            WHERE id=? AND LOWER(TRIM(status))='open'
        """, ("closed", exit_price, now.strftime("%Y-%m-%d"),
              result, profit_pct, pnl_dollar, close_reason,
              exit_price, pnl_dollar, profit_pct, close_reason, now.strftime("%H:%M:%S"),
              *params_extra, trade_id))
        if cur_close.rowcount != 1:
            print(f"[paper_close_skip] trade_id={trade_id} already closed or missing; duplicate close ignored")
            return False
        _paper_trade_to_general_log(conn, trade_id, exit_price, result, profit_pct, pnl_dollar, close_reason)
        conn.commit()

        closed_row = conn.execute(
            "SELECT symbol, selected_mode, status FROM paper_trades WHERE id=?", (trade_id,)
        ).fetchone()
        if closed_row:
            open_after = conn.execute(
                "SELECT COUNT(*) AS cnt FROM paper_trades "
                "WHERE UPPER(TRIM(symbol))=UPPER(TRIM(?)) "
                "AND UPPER(TRIM(selected_mode))='SWING' "
                "AND LOWER(TRIM(status))='open'",
                (closed_row["symbol"],)
            ).fetchone()["cnt"]
            print(
                f"[manual_close_sync] trade_id={trade_id} symbol={closed_row['symbol']} "
                f"mode={closed_row['selected_mode']} status={closed_row['status']} "
                f"open_swing_count_after_close={open_after} source_of_truth=paper_trades"
            )

    # Telegram close alert خارج اتصال DB حتى لا يعطل الإغلاق إذا فشل الإرسال.
    try:
        with get_connection() as conn:
            row = conn.execute("SELECT * FROM paper_trades WHERE id=?", (trade_id,)).fetchone()
            trade = dict(row) if row else {"id": trade_id}
        from core.telegram_bot import send_paper_close_notification
        ok, msg = send_paper_close_notification(trade)
        print(f"[telegram_close] #{trade_id} ok={ok} msg={msg}")
    except Exception as e:
        print(f"[telegram_close error] #{trade_id}: {e}")




def find_paper_trade_by_signature(trade_signature: str,
                                  open_only: bool = False,
                                  cooldown_minutes: int = 0,
                                  same_day: bool = False) -> dict:
    """
    Returns the most recent paper trade matching an exact trade signature.

    Used by v3.15 universal de-duplication: unlimited distinct paper trades
    are allowed, but the exact same legs/expiry/strategy should not be
    logged repeatedly while the signal is unchanged.
    """
    if not trade_signature:
        return {}
    try:
        clauses = ["trade_signature=?"]
        params = [trade_signature]
        if open_only:
            clauses.append("status='open'")
        if same_day:
            clauses.append("date(created_at)=date('now','localtime')")
        if cooldown_minutes and int(cooldown_minutes) > 0:
            clauses.append("datetime(created_at) >= datetime('now','localtime', ?)")
            params.append(f"-{int(cooldown_minutes)} minutes")
        sql = "SELECT * FROM paper_trades WHERE " + " AND ".join(clauses) + " ORDER BY created_at DESC LIMIT 1"
        with get_connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else {}
    except Exception:
        return {}


def count_paper_trades_by_signature(trade_signature: str, open_only: bool = False) -> int:
    """Count paper trades matching an exact signature.

    In study mode, prevent_exact_duplicate_open_trade may be an integer >1.
    Example: value=3 means allow up to 3 identical open paper trades, then block.
    """
    if not trade_signature:
        return 0
    try:
        clauses = ["trade_signature=?"]
        params = [trade_signature]
        if open_only:
            clauses.append("status='open'")
        sql = "SELECT COUNT(*) AS n FROM paper_trades WHERE " + " AND ".join(clauses)
        with get_connection() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"] if row and "n" in row.keys() else 0)
    except Exception:
        return 0

def get_paper_trades(status: str = None, limit: int = 200) -> list:
    """جلب توصيات Paper Trading."""
    with get_connection() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM paper_trades WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM paper_trades ORDER BY created_at DESC LIMIT ?",
                (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


def _quality_calibration(quality: str, win_rate: float,
                         profit_factor) -> str:
    """
    يقيّم هل حدود Quality مناسبة بناءً على النتائج الفعلية.
    يُعيد ملاحظة معايرة للمستخدم.
    """
    pf = profit_factor or 0
    if quality == "Good":
        if win_rate >= 55 and pf >= 1.3:
            return "الحد مناسب — Good يتفوق فعلاً"
        if win_rate < 45 or pf < 1.0:
            return "⚠️ Good لا يتفوق — فكّر برفع الحد أو إعادة المعايرة"
        return "مقبول — تحتاج مزيداً من البيانات"
    if quality == "Acceptable":
        if win_rate >= 50 and pf >= 1.1:
            return "Acceptable أفضل من المتوقع — هل تخفّض الحد؟"
        if win_rate < 40 or pf < 0.9:
            return "⚠️ Acceptable ضعيف — ارفع الحد أو اجعله No Trade"
        return "مقبول — راقب المزيد"
    if quality == "Weak":
        if win_rate >= 50:
            return "⚠️ Weak يربح! — راجع منطق التصنيف"
        return "Weak خاسر كما هو متوقع — الحد صحيح"
    if quality == "Unrealistic RR":
        if win_rate >= 60:
            return "RR مرتفع لكن يربح — راجع منطق الحد"
        return "RR غير واقعي وخاسر — تجنّب هذه الصفقات"
    return ""


def get_paper_report() -> dict:
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
        all_closed = get_paper_trades(status="closed")
        if not all_closed:
            return {"total": 0, "message": "لا توجد توصيات مغلقة بعد"}

        # الأداء الرسمي يُحسب من paper_execution فقط
        # qualified_signal = مراقبة فقط — لا تدخل في الإحصاء الرسمي
        _EXEC_SOURCES = {"paper_execution", "swing_auto_paper"}
        trades = [t for t in all_closed
                  if (t.get("source") or "auto") in _EXEC_SOURCES
                  or (t.get("source") or "auto") == "auto"]  # legacy قبل الفصل
        signal_only_count = len([t for t in all_closed
                                  if (t.get("source") or "") == "qualified_signal"])

        def _max_drawdown(subset):
            """Max Drawdown كنسبة % من ذروة P&L المتراكم."""
            if not subset:
                return 0.0
            equity = 0.0
            peak   = 0.0
            max_dd = 0.0
            for t in sorted(subset, key=lambda x: x.get("exit_date") or x.get("closed_at") or x.get("updated_at") or x.get("created_at") or ""):
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

        # v3.30: حسب وقت الدخول خلال جلسة السوق — دراسة فقط، بدون منع.
        BUCKET_ORDER = [
            "Pre-market",
            "09:30-09:45 Opening 15m",
            "09:45-10:30 Early Session",
            "10:30-12:00 Morning Trend",
            "12:00-14:00 Midday",
            "14:00-15:00 Afternoon",
            "15:00-15:30 Late Session",
            "15:30-16:00 Close Risk",
            "After-hours",
        ]
        by_time_bucket = {}
        for bucket in BUCKET_ORDER:
            subset = [t for t in trades if (t.get("time_bucket") or "Unclassified") == bucket]
            if subset:
                by_time_bucket[bucket] = _stats(subset)
        for bucket in sorted(set((t.get("time_bucket") or "Unclassified") for t in trades)):
            if bucket not in by_time_bucket:
                subset = [t for t in trades if (t.get("time_bucket") or "Unclassified") == bucket]
                if subset:
                    by_time_bucket[bucket] = _stats(subset)

        # v3.30: أول دخول مقابل الدخول المتكرر لنفس setup — تسجيل وتحليل فقط.
        first_entries = [t for t in trades if not (t.get("is_repeated_setup") or 0)]
        repeated_entries = [t for t in trades if (t.get("is_repeated_setup") or 0)]
        by_repetition = {
            "First setup entries": _stats(first_entries) if first_entries else {},
            "Repeated setup entries": _stats(repeated_entries) if repeated_entries else {},
        }

        # v3.30: أكثر setups تكراراً للمراجعة بعد أسبوع.
        repeated_setups = []
        for key in sorted(set((t.get("same_setup_key") or "") for t in trades if t.get("same_setup_key"))):
            subset = [t for t in trades if (t.get("same_setup_key") or "") == key]
            if len(subset) >= 2:
                st = _stats(subset)
                st["same_setup_key"] = key
                st["label"] = key.replace("|", " | ")
                repeated_setups.append(st)
        repeated_setups = sorted(repeated_setups, key=lambda x: x.get("total", 0), reverse=True)[:12]

        # حسب Setup Quality — هل التصنيف يميز الصفقات الجيدة فعلاً؟
        QUALITY_ORDER = ["Good", "Acceptable", "Weak", "Unrealistic RR"]
        by_quality = {}
        for q in QUALITY_ORDER:
            subset = [t for t in trades
                      if (t.get("setup_quality") or "").strip().lower() == q.lower()]
            if subset:
                st = _stats(subset)
                st["calibration_note"] = _quality_calibration(
                    q, st.get("win_rate", 0), st.get("profit_factor"))
                by_quality[q] = st
        # أي فئات أخرى غير متوقعة
        known = {q.lower() for q in QUALITY_ORDER}
        for q in sorted(set((t.get("setup_quality") or "").strip() for t in trades)):
            if q and q.lower() not in known:
                subset = [t for t in trades if (t.get("setup_quality") or "").strip() == q]
                if subset:
                    by_quality[q] = _stats(subset)

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

        # تشغيل تقييم المراحل تلقائياً مع كل تقرير
        stage_changes = evaluate_strategy_stages()

        return {
            "overall":          overall,
            "by_mode":          by_mode,
            "by_strategy":      by_strat,
            "by_symbol":        by_symbol,
            "by_time_bucket":   by_time_bucket,
            "by_repetition":    by_repetition,
            "repeated_setups":  repeated_setups,
            "by_quality":       by_quality,
            "best_strategy":    best_strat,
            "worst_strategy":   worst_strat,
            "disable_list":     disable_list,
            "stage_changes":    stage_changes,
            "strategy_statuses":get_all_strategy_statuses(),
            "ready_for_live":       ready,
            "live_criteria":        overall.get("live_criteria", {}),
            "signal_only_count":    signal_only_count,
        }

    except Exception as e:
        print(f"[get_paper_report error] {e}")
        return {}



# ── Strategy Status Management ────────────────────────────────────────────────

# مراحل التعطيل
STAGE_THRESHOLDS = {
    1: {"min_trades": 10, "max_pf": 0.8, "action": "warning",  "weight": 1.0},
    2: {"min_trades": 20, "max_pf": 0.8, "action": "reduced",  "weight": 0.7},
    3: {"min_trades": 30, "max_pf": 0.8, "action": "disabled", "weight": 0.0},
}


def get_strategy_status(trade_mode: str, strategy: str, symbol: str) -> dict:
    """يجلب حالة استراتيجية معينة (mode+strategy+symbol)."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM strategy_status WHERE trade_mode=? AND strategy=? AND symbol=?",
            (trade_mode, strategy, symbol)
        ).fetchone()
    return dict(row) if row else {
        "status": "active", "weight": 1.0, "stage": 0
    }


def get_all_strategy_statuses() -> list:
    """Return strategy status rows enriched with current paper-trading stats."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM strategy_status ORDER BY trade_mode, strategy, symbol"
        ).fetchall()
    statuses = [dict(r) for r in rows]
    trades = get_paper_trades(status="closed", limit=1000)

    def _combo_stats(mode: str, strategy: str, symbol: str) -> dict:
        subset = [
            t for t in trades
            if t.get("selected_mode") == mode
            and t.get("strategy") == strategy
            and t.get("symbol") == symbol
        ]
        total = len(subset)
        if not total:
            return {"sample_size": 0, "pf_current": None, "exp_current": None}
        wins = [t for t in subset if t.get("result") == "WIN"]
        losses = [t for t in subset if t.get("result") == "LOSS"]
        gross_p = sum(t.get("pnl_dollar") or 0 for t in wins)
        gross_l = abs(sum(t.get("pnl_dollar") or 0 for t in losses))
        pf = round(gross_p / gross_l, 2) if gross_l > 0 else 99.0
        wr = len(wins) / total
        avg_w = sum(t.get("profit_pct") or 0 for t in wins) / len(wins) if wins else 0
        avg_l = sum(abs(t.get("profit_pct") or 0) for t in losses) / len(losses) if losses else 0
        exp = round((wr * avg_w) - ((1 - wr) * avg_l), 2)
        return {"sample_size": total, "pf_current": pf, "exp_current": exp}

    for s in statuses:
        s.update(_combo_stats(
            s.get("trade_mode", "0DTE"),
            s.get("strategy", ""),
            s.get("symbol", ""),
        ))
    return statuses

def evaluate_strategy_stages() -> list:
    """
    يُقيّم كل (mode+strategy+symbol) من paper_trades ويُحدّث المراحل.
    يُعيد قائمة بالتغييرات التي حدثت.
    """
    changes = []
    try:
        trades = get_paper_trades(status="closed")
        if not trades:
            return []

        # جمّع الصفقات حسب (mode, strategy, symbol)
        combos = {}
        for t in trades:
            key = (
                t.get("selected_mode", "0DTE"),
                t.get("strategy", "?"),
                t.get("symbol", "?"),
            )
            combos.setdefault(key, []).append(t)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for (mode, strat, sym), subset in combos.items():
            total = len(subset)
            wins  = [t for t in subset if t.get("result") == "WIN"]
            losses= [t for t in subset if t.get("result") == "LOSS"]

            gross_p = sum(t.get("pnl_dollar") or 0 for t in wins)
            gross_l = abs(sum(t.get("pnl_dollar") or 0 for t in losses))
            pf  = round(gross_p / gross_l, 2) if gross_l > 0 else 99.0
            wr  = round(len(wins)/total*100, 1)
            avg_w = sum(t.get("profit_pct") or 0 for t in wins)  / len(wins)  if wins   else 0
            avg_l = sum(abs(t.get("profit_pct") or 0) for t in losses) / len(losses) if losses else 0
            exp = round((wr/100*avg_w) - ((1-wr/100)*avg_l), 2)

            bad = (pf < 0.8 and exp < 0)
            current = get_strategy_status(mode, strat, sym)
            current_stage = current.get("stage", 0)
            current_status = current.get("status", "active")

            new_stage   = current_stage
            new_status  = current_status
            new_weight  = current.get("weight", 1.0)
            new_reason  = None

            if current_status not in ("disabled", "probation"):
                for stage_num in [3, 2, 1]:
                    th = STAGE_THRESHOLDS[stage_num]
                    if total >= th["min_trades"] and bad and stage_num > current_stage:
                        new_stage  = stage_num
                        new_action = th["action"]
                        new_weight = th["weight"]
                        if new_action == "warning":
                            new_status = "warning"
                            new_reason = (f"Stage 1 — تحذير: PF={pf} EXP={exp}% "
                                          f"بعد {total} صفقة")
                        elif new_action == "reduced":
                            new_status = "reduced"
                            new_reason = (f"Stage 2 — وزن -30%: PF={pf} EXP={exp}% "
                                          f"بعد {total} صفقة")
                        elif new_action == "disabled":
                            new_status = "disabled"
                            new_reason = (f"Stage 3 — معطّل مؤقتاً: PF={pf} EXP={exp}% "
                                          f"بعد {total} صفقة")
                        break

            # Stage 3 (disabled): لا تعافٍ تلقائي — يدوي فقط أو عبر Probation
            # Stage 1-2: تعافٍ تلقائي إذا تحسّن الأداء
            if current_status not in ("disabled", "probation"):
                if not bad and current_stage > 0 and pf >= 1.0 and exp > 0:
                    new_stage  = 0
                    new_status = "active"
                    new_weight = 1.0
                    new_reason = (f"تعافى تلقائياً: PF={pf} EXP={exp}% "
                                  f"بعد {total} صفقة")

            # Probation: evaluate only closed paper trades created after probation started.
            if current_status == "probation":
                prob_count = current.get("probation_count", 0)
                probation_started = current.get("probation_started_at") or ""
                probation_subset = [
                    t for t in subset
                    if (t.get("timestamp") or t.get("created_at") or "") > probation_started
                ]
                if prob_count >= 10 and len(probation_subset) >= 10:
                    p_wins = [t for t in probation_subset if t.get("result") == "WIN"]
                    p_losses = [t for t in probation_subset if t.get("result") == "LOSS"]
                    p_gross_p = sum(t.get("pnl_dollar") or 0 for t in p_wins)
                    p_gross_l = abs(sum(t.get("pnl_dollar") or 0 for t in p_losses))
                    p_pf = round(p_gross_p / p_gross_l, 2) if p_gross_l > 0 else 99.0
                    p_wr = len(p_wins) / len(probation_subset)
                    p_avg_w = sum(t.get("profit_pct") or 0 for t in p_wins) / len(p_wins) if p_wins else 0
                    p_avg_l = sum(abs(t.get("profit_pct") or 0) for t in p_losses) / len(p_losses) if p_losses else 0
                    p_exp = round((p_wr * p_avg_w) - ((1 - p_wr) * p_avg_l), 2)
                    pf, exp, total = p_pf, p_exp, len(probation_subset)

                    if p_pf >= 1.0 and p_exp > 0:
                        new_stage = 0
                        new_status = "active"
                        new_weight = 1.0
                        new_reason = f"Probation passed: PF={p_pf} EXP={p_exp}% after {total} closed signals"
                    else:
                        new_stage = current_stage
                        new_status = "disabled"
                        new_weight = 0.0
                        new_reason = f"Probation failed: PF={p_pf} EXP={p_exp}% after {total} closed signals"
            if new_stage != current_stage or new_status != current_status:
                _upsert_strategy_status(
                    mode, strat, sym, new_status, new_weight,
                    new_stage, pf, exp, total, new_reason or "", now_str
                )
                changes.append({
                    "mode": mode, "strategy": strat, "symbol": sym,
                    "old_stage": current_stage, "new_stage": new_stage,
                    "status": new_status, "weight": new_weight,
                    "reason": new_reason,
                })

    except Exception as e:
        print(f"[evaluate_strategy_stages error] {e}")
    return changes


def _upsert_strategy_status(mode, strat, sym, status, weight,
                             stage, pf, exp, trades, reason, now_str,
                             probation_count: int = None):
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO strategy_status
            (trade_mode, strategy, symbol, status, weight, stage,
             pf_at_stage, exp_at_stage, trades_at_stage, reason,
             disabled_at, last_updated)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(trade_mode, strategy, symbol) DO UPDATE SET
                status=excluded.status,
                weight=excluded.weight,
                stage=excluded.stage,
                pf_at_stage=excluded.pf_at_stage,
                exp_at_stage=excluded.exp_at_stage,
                trades_at_stage=excluded.trades_at_stage,
                reason=excluded.reason,
                last_updated=excluded.last_updated,
                probation_count=CASE WHEN excluded.status='probation'
                                THEN COALESCE(probation_count,0)
                                ELSE 0 END,
                disabled_at=CASE WHEN excluded.status IN ('disabled','probation')
                            THEN excluded.disabled_at ELSE disabled_at END
        """, (mode, strat, sym, status, weight, stage,
              pf, exp, trades, reason,
              now_str if status in ("disabled", "probation") else None,
              now_str))
        conn.commit()


def re_enable_strategy(trade_mode: str, strategy: str, symbol: str,
                        reason: str = "إعادة تفعيل يدوي",
                        probation: bool = False) -> None:
    """
    إعادة تفعيل استراتيجية يدوياً.
    probation=True: يضعها في Probation Mode (paper only) بدل Active مباشرة.
    probation=False: تعود Active مباشرة.
    """
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_status = "probation" if probation else "active"
    with get_connection() as conn:
        conn.execute("""
            UPDATE strategy_status
            SET status=?, weight=CASE WHEN ?='active' THEN 1.0 ELSE 0.0 END,
                stage=0, probation_count=0,
                probation_started_at=CASE WHEN ?='probation' THEN ? ELSE NULL END,
                reason=?, re_enabled_at=?, last_updated=?
            WHERE trade_mode=? AND strategy=? AND symbol=?
        """, (new_status, new_status, new_status, now_str, reason, now_str, now_str,
              trade_mode, strategy, symbol))
        conn.commit()


def increment_probation_count(trade_mode: str, strategy: str, symbol: str) -> None:
    """يزيد عداد إشارات Probation بواحد."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
            UPDATE strategy_status
            SET probation_count = COALESCE(probation_count, 0) + 1,
                last_updated = ?
            WHERE trade_mode=? AND strategy=? AND symbol=? AND status='probation'
        """, (now_str, trade_mode, strategy, symbol))
        conn.commit()


def is_strategy_allowed(trade_mode: str, strategy: str, symbol: str,
                         paper_only: bool = False) -> tuple:
    """
    يتحقق هل الاستراتيجية مسموحة ويُعيد (allowed: bool, weight: float, reason: str).
    paper_only=True: يسمح للـ probation بالمرور (لتسجيل Paper فقط).
    """
    s = get_strategy_status(trade_mode, strategy, symbol)
    status = s.get("status", "active")
    weight = s.get("weight", 1.0)
    pc     = s.get("probation_count", 0)

    if status == "disabled":
        return False, 0.0, s.get("reason", "معطّلة — فعّلها يدوياً أو ضعها في Probation")
    if status == "probation":
        if paper_only:
            return True, 0.0, f"Probation ({pc}/10 إشارات) — paper only"
        return False, 0.0, f"Probation Mode — paper only ({pc}/10)"
    if status == "warning":
        return True, 1.0, f"⚠️ Stage 1 تحذير: {s.get('reason','')}"
    if status == "reduced":
        return True, weight, f"⚠️ Stage 2 وزن {weight:.0%}: {s.get('reason','')}"
    return True, 1.0, ""


# ── Paper Balance Manager ─────────────────────────────────────────────────────

def get_paper_balance() -> dict:
    """
    يعيد الرصيد الحالي والمحجوز للـ Paper Trading.
    الرصيد المتاح = إجمالي الرصيد - المحجوز في صفقات مفتوحة.
    """
    initial = float(get_setting("paper_balance", "10000") or 10000)
    with get_connection() as conn:
        # آخر رصيد مسجّل
        last = conn.execute(
            "SELECT balance, reserved FROM balance_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if last:
            balance  = float(last["balance"])
            reserved = float(last["reserved"])
        else:
            balance  = initial
            reserved = 0.0
    return {
        "initial":   initial,
        "balance":   round(balance,  2),
        "reserved":  round(reserved, 2),
        "available": round(balance - reserved, 2),
    }


def _calc_margin(strategy: str, wing: float, contracts: int = 1) -> float:
    """
    حساب الـ Margin المطلوب بناءً على نوع الاستراتيجية وعرض الجناح.
    - Credit Spreads / Iron Condor: wing × 100 × contracts
    - Debit Spreads: Debit المدفوع (يُمرَّر كـ wing)
    """
    return round(wing * 100 * contracts, 2)


def balance_open_trade(trade_id: int, symbol: str, strategy: str,
                       wing: float, contracts: int = 1) -> float:
    """
    يحجز Margin عند فتح صفقة.
    يعيد قيمة الـ Margin المحجوز.
    """
    margin   = _calc_margin(strategy, wing, contracts)
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bal_data = get_paper_balance()
    new_reserved = bal_data["reserved"] + margin
    # الرصيد لا يتغير عند الفتح — فقط الـ reserved يزيد
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO balance_log
            (timestamp, event_type, trade_id, symbol, strategy, amount, balance, reserved, note)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (now, "OPEN", trade_id, symbol, strategy,
              -margin, bal_data["balance"], new_reserved,
              f"حجز margin: {strategy} wing={wing}"))
        conn.commit()
    return margin


def balance_close_trade(trade_id: int, symbol: str, strategy: str,
                        wing: float, pnl_dollar: float, contracts: int = 1) -> None:
    """
    يُحرَّر الـ Margin ويُضاف/يُخصم الربح/الخسارة عند إغلاق صفقة.
    pnl_dollar: موجب = ربح، سالب = خسارة
    """
    margin   = _calc_margin(strategy, wing, contracts)
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    bal_data = get_paper_balance()
    new_balance  = bal_data["balance"]  + pnl_dollar
    new_reserved = max(bal_data["reserved"] - margin, 0.0)
    event = "WIN" if pnl_dollar >= 0 else "LOSS"
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO balance_log
            (timestamp, event_type, trade_id, symbol, strategy, amount, balance, reserved, note)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (now, event, trade_id, symbol, strategy,
              pnl_dollar, new_balance, new_reserved,
              f"P&L: ${pnl_dollar:+.2f} | margin released: ${margin:.0f}"))
        conn.commit()


def reset_paper_balance() -> None:
    """إعادة تعيين الرصيد للقيمة الابتدائية."""
    initial = float(get_setting("paper_balance", "10000") or 10000)
    now     = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        conn.execute("""
            INSERT INTO balance_log
            (timestamp, event_type, trade_id, symbol, strategy, amount, balance, reserved, note)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (now, "RESET", None, "", "", 0, initial, 0, "إعادة تعيين الرصيد"))
        conn.commit()


def get_balance_history(limit: int = 50) -> list:
    """سجل آخر تغييرات الرصيد."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM balance_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
