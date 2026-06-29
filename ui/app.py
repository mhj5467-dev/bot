"""
Main Desktop UI - SPX Trading Bot
Dark theme, RTL Arabic interface
"""
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import threading
from datetime import datetime
import sys, os, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.database import init_db, get_stats, save_trade, get_all_trades, delete_trade, get_setting, save_setting, save_analysis_log, get_recent_analysis_logs, get_backtest_data, get_open_trades, get_all_open_trades, get_open_trades_count, get_recent_rejections, get_trade_by_id, log_paper_trade, get_paper_trades, get_paper_report, close_paper_trade, get_all_strategy_statuses, re_enable_strategy, evaluate_strategy_stages, increment_probation_count, clear_paper_data, get_paper_balance, reset_paper_balance
# RC15i.4: heavy modules loaded lazily (inside worker functions) to avoid
# blocking the UI thread during startup. Do not restore top-level imports.

# ── Colors ──────────────────────────────────────────────────
BG        = "#0f0f1a"
BG2       = "#1a1a2e"
BG3       = "#16213e"
CARD      = "#1e2140"
ACCENT    = "#00e676"
GOLD      = "#ffd700"
RED       = "#ff4757"
BLUE      = "#4fc3f7"
TEXT      = "#e0e0e0"
TEXT_DIM  = "#888"
WHITE     = "#ffffff"
BORDER    = "#2a2a4a"


def hex_lighten(color, amount=30):
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    r = min(255, r + amount)
    g = min(255, g + amount)
    b = min(255, b + amount)
    return f"#{r:02x}{g:02x}{b:02x}"


class SPXBotApp:
    def __init__(self, root):
        self.root = root
        # RC15i.2: shutdown guard for scheduled tkinter after() callbacks.
        self._closing = False
        self.root.title("Abu Hassan Bot — Paper Only v3.33.7 RC15")
        self.root.geometry("1100x700")
        self.root.configure(bg=BG)
        self.root.minsize(900, 600)

        init_db()
        # RC14 — Session Counters (in-memory, resets on restart)
        # OrderedDict preserves insertion order for FIFO eviction
        from collections import OrderedDict
        self._session_ds_results: OrderedDict = OrderedDict()
        self._session_lock = threading.Lock()   # حماية القراءة/الكتابة بين worker thread و main thread
        self._analysis_in_progress = False
        self._analysis_guard_lock = threading.Lock()
        # RC15i.10 — fast 0DTE exit monitor state. Full refresh stays throttled,
        # but open 0DTE paper trades get a high-priority 1s exit-check loop.
        self._last_full_price_refresh_ts = 0.0
        self._setup_styles()
        # تحميل وضع الحدود المحفوظ
        try:
            from core.strategy_engine import set_threshold_mode
            from core.database import get_setting as _gs
            set_threshold_mode(_gs("threshold_mode", "conservative"))
        except Exception:
            pass
        self._build_ui()
        self._refresh_stats()
        self.root.after(5_000,  self._start_ui_refresh)    # UI كل 5 ث
        # RC15i.4: short delay before first price refresh lets the UI settle without
        # risking a missed exit signal (20 s was too long near EOD force-exit windows).
        self.root.after(5_000, self._start_auto_refresh)
        # RC15i.4: delay Swing cache warmup by 30 s and only when market is open.
        self.root.after(30_000, self._maybe_start_swing_warmup)

    # ── RC15i.4: Deferred Swing cache warmup ────────────────
    def _maybe_start_swing_warmup(self):
        """RC15i.4: Start Swing cache only after 30 s and only during market hours."""
        if getattr(self, "_closing", False):
            return
        try:
            from core.analyzer import is_us_market_open, get_et_time
            et = get_et_time()
            market_ready = (
                is_us_market_open()
                and (et.hour > 9 or (et.hour == 9 and et.minute >= 35))
            )
            if not market_ready:
                # RC15i.4: retry in 60 s — handles starting before 9:35 ET or pre-market.
                self.root.after(60_000, self._maybe_start_swing_warmup)
                return
            from core.trade_monitor import _update_swing_cache_bg
            threading.Thread(target=_update_swing_cache_bg, daemon=True).start()
        except Exception as _e:
            # RC15i.4: log and retry — a transient failure must not permanently
            # block Swing entries by leaving _swing_cache_loading=True for the session.
            print(f"[swing_warmup] ⚠️ warmup error (retry in 60 s): {_e}")
            self.root.after(60_000, self._maybe_start_swing_warmup)

    # ── Styles ──────────────────────────────────────────────
    def _setup_styles(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Dark.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Title.TLabel", background=BG, foreground=WHITE, font=("Segoe UI", 14, "bold"))
        style.configure("Stat.TLabel", background=CARD, foreground=WHITE, font=("Segoe UI", 22, "bold"))
        style.configure("StatSub.TLabel", background=CARD, foreground=TEXT_DIM, font=("Segoe UI", 9))
        style.configure("Green.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI", 22, "bold"))
        style.configure("Red.TLabel", background=CARD, foreground=RED, font=("Segoe UI", 22, "bold"))
        style.configure("Gold.TLabel", background=CARD, foreground=GOLD, font=("Segoe UI", 22, "bold"))
        style.configure("Treeview", background=BG3, foreground=TEXT,
                        fieldbackground=BG3, rowheight=26, font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=BG2, foreground=BLUE,
                        font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", "#2a3560")])

    # ── Build UI ─────────────────────────────────────────────
    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=BG2, height=55)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="🤖  Abu Hassan Bot", bg=BG2, fg=WHITE,
                 font=("Segoe UI", 15, "bold")).pack(side="left", padx=18, pady=12)
        tk.Label(header, text="SPX Options Trading Assistant — Paper 0DTE + Swing",
                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=4)

        now_lbl = tk.Label(header, text="", bg=BG2, fg=BLUE, font=("Segoe UI", 9))
        now_lbl.pack(side="right", padx=18)
        self._update_clock(now_lbl)

        # Status dot
        self.status_dot = tk.Label(header, text="● جاهر", bg=BG2, fg=ACCENT,
                                   font=("Segoe UI", 9, "bold"))
        self.status_dot.pack(side="right", padx=8)

        # Main notebook (tabs)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=0, pady=0)

        self._build_dashboard_tab()
        # Strategy comparison tab hidden: main dashboard already shows strategy details.
        self._build_trades_tab()
        # Paper-only UI: removed old Auto/Paper Monitor tab to avoid duplicate Paper screens
        self._build_backtest_tab()
        self._build_paper_tab()
        self._build_swing_diag_tab()
        self._build_settings_tab()
        self._build_debug_tab()
        self._remove_legacy_paper_monitor_tab()
        self._install_global_mousewheel_scroll()

    def _install_global_mousewheel_scroll(self):
        """Enable mouse-wheel scrolling across all tabs and scrollable widgets.

        The app contains multiple Canvas-based pages and Treeview tables.  Some older
        sections only scrolled when the scrollbar had focus.  This global handler
        scrolls the scrollable widget under the mouse pointer, so the wheel works
        naturally on Dashboard, Trades, Backtest, Paper Trades, Swing Diag, Settings,
        Debug, and popup windows.
        """
        def _find_scroll_target(widget):
            cur = widget
            while cur is not None:
                try:
                    # Canvas and Treeview both expose yview/yview_scroll.
                    if hasattr(cur, "yview") and hasattr(cur, "yview_scroll"):
                        return cur
                    parent_name = cur.winfo_parent()
                    if not parent_name:
                        break
                    cur = cur._nametowidget(parent_name)
                except Exception:
                    break
            return None

        def _on_mousewheel(event):
            try:
                widget = self.root.winfo_containing(event.x_root, event.y_root)
                target = _find_scroll_target(widget)
                if target is None:
                    return None
                delta = event.delta
                if delta == 0:
                    return None
                units = int(-1 * (delta / 120))
                if units == 0:
                    units = -1 if delta > 0 else 1
                target.yview_scroll(units, "units")
                return "break"
            except Exception:
                return None

        def _on_linux_wheel_up(event):
            try:
                widget = self.root.winfo_containing(event.x_root, event.y_root)
                target = _find_scroll_target(widget)
                if target is not None:
                    target.yview_scroll(-1, "units")
                    return "break"
            except Exception:
                return None

        def _on_linux_wheel_down(event):
            try:
                widget = self.root.winfo_containing(event.x_root, event.y_root)
                target = _find_scroll_target(widget)
                if target is not None:
                    target.yview_scroll(1, "units")
                    return "break"
            except Exception:
                return None

        try:
            # Replace older bind_all handlers so page-specific handlers do not steal
            # the wheel from other tabs.
            self.root.bind_all("<MouseWheel>", _on_mousewheel)
            self.root.bind_all("<Button-4>", _on_linux_wheel_up)
            self.root.bind_all("<Button-5>", _on_linux_wheel_down)
        except Exception:
            pass

    def _update_clock(self, label):
        if getattr(self, "_closing", False):
            return
        try:
            from core.analyzer import get_et_time, is_us_market_open
            et = get_et_time()
            market_str = "🟢 السوق مفتوح" if is_us_market_open() else "🔴 السوق مغلق"
            label.config(text=f"{datetime.now().strftime('%H:%M:%S')}  |  NY {et.strftime('%H:%M')}  {market_str}")
        except Exception:
            if getattr(self, "_closing", False):
                return
            try:
                label.config(text=datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
            except Exception:
                return
        if not getattr(self, "_closing", False):
            try:
                self.root.after(1000, lambda: self._update_clock(label))
            except Exception:
                return

    # ── Scrollable Body Helper ───────────────────────────────
    def _scrollable_body(self, parent: tk.Frame, bg: str = None) -> tk.Frame:
        """
        يُنشئ Canvas + Scrollbar عمودي داخل parent ويُعيد الـ inner frame
        الذي يجب أن تُضاف إليه كل الـ widgets.
        الـ inner frame يتمدد تلقائياً ليملأ عرض الـ Canvas الكامل.
        عجلة الماوس تعمل تلقائياً عند المرور فوق المنطقة.
        """
        _bg = bg or BG
        canvas = tk.Canvas(parent, bg=_bg, highlightthickness=0)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=_bg)

        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        # تحديث scrollregion عند تغيّر حجم المحتوى
        inner.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        # تمديد inner ليملأ عرض الـ Canvas دائماً
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(win_id, width=e.width)
        )

        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        def _on_enter(e):
            canvas.bind("<MouseWheel>",
                lambda ev: canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units"))
        def _on_leave(e):
            canvas.unbind("<MouseWheel>")
        canvas.bind("<Enter>", _on_enter)
        canvas.bind("<Leave>", _on_leave)

        return inner


    def _remove_legacy_paper_monitor_tab(self):
        """Remove any leftover legacy Paper Monitor tab if an older build added it.
        Safe no-op when the tab does not exist.
        """
        try:
            for tab_id in list(self.notebook.tabs()):
                title = self.notebook.tab(tab_id, "text") or ""
                if "Paper Monitor" in title:
                    self.notebook.forget(tab_id)
        except Exception:
            pass

    # ── Dashboard Tab ────────────────────────────────────────
    def _build_dashboard_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  🏠 لوحة التحكم  ")

        # Left panel: controls (scrollable so nothing is cut off on small screens)
        left_outer = tk.Frame(frame, bg=BG2, width=310)
        left_outer.pack(side="left", fill="y")
        left_outer.pack_propagate(False)

        left_canvas = tk.Canvas(left_outer, bg=BG2, highlightthickness=0, width=310)
        left_sb = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        left = tk.Frame(left_canvas, bg=BG2)
        left.bind(
            "<Configure>",
            lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all"))
        )
        left_canvas.create_window((0, 0), window=left, anchor="nw", width=310)
        left_canvas.configure(yscrollcommand=left_sb.set)
        left_sb.pack(side="right", fill="y")
        left_canvas.pack(side="left", fill="both", expand=True)

        def _bind_mw(e):
            left_canvas.bind("<MouseWheel>",
                lambda ev: left_canvas.yview_scroll(int(-1*(ev.delta/120)), "units"))
        def _unbind_mw(e):
            left_canvas.unbind("<MouseWheel>")
        left_canvas.bind("<Enter>", _bind_mw)
        left_canvas.bind("<Leave>", _unbind_mw)

        tk.Label(left, text="التحكم", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(anchor="e", padx=15, pady=(14, 4))

        # Buttons
        btn_configs = [
            ("▶  تشغيل تحليل كامل", ACCENT, BG, self._run_analysis),
            ("📊  تحليل آخر بيانات", CARD, TEXT, self._run_quick_analysis),
            ("✈  اختبار التيليغرام", CARD, TEXT, self._test_telegram),
            ("🧪  فحص النظام", CARD, BLUE, lambda: self.notebook.select(7)),
            ("⚙  الإعدادات", CARD, TEXT, lambda: self.notebook.select(6)),
        ]

        for text, bg, fg, cmd in btn_configs:
            btn = tk.Button(left, text=text, bg=bg, fg=fg,
                            activebackground=hex_lighten(bg), activeforeground=fg,
                            font=("Segoe UI", 10, "bold"), relief="flat",
                            cursor="hand2", command=cmd, pady=10)
            btn.pack(fill="x", padx=14, pady=4)

        # Auto-scheduler button
        self._auto_running = False
        self._auto_job_id  = None
        self.auto_btn = tk.Button(
            left, text="⏱  Paper Auto-Scan (كل 5 دقائق)",
            bg="#1a3a5a", fg=BLUE,
            activebackground="#1e4570", activeforeground=BLUE,
            font=("Segoe UI", 10, "bold"), relief="flat",
            cursor="hand2", command=self._toggle_auto,
            pady=10,
        )
        self.auto_btn.pack(fill="x", padx=14, pady=4)

        self.auto_status = tk.Label(
            left, text="", bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 8),
            wraplength=270, justify="left"
        )
        self.auto_status.pack(anchor="w", padx=18)

        # زر وضع الحدود
        from core.strategy_engine import get_threshold_mode
        self._mode_var = tk.StringVar(value=get_threshold_mode())
        mode_frame = tk.Frame(left, bg=BG2)
        mode_frame.pack(fill="x", padx=14, pady=(6, 2))
        tk.Label(mode_frame, text="وضع الحدود:", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="right")
        self.mode_btn = tk.Button(
            left, text=self._mode_label(),
            bg="#1a1a3a", fg=BLUE,
            activebackground="#22224a", activeforeground=BLUE,
            font=("Segoe UI", 9, "bold"), relief="flat",
            cursor="hand2", command=self._toggle_mode, pady=6,
        )
        self.mode_btn.pack(fill="x", padx=14, pady=(0, 6))

        # Gold button
        tk.Button(left, text="＋  تسجيل صفقة منفذة", bg=GOLD, fg="#000",
                  activebackground="#e6c200", font=("Segoe UI", 10, "bold"),
                  relief="flat", cursor="hand2", command=self._open_trade_dialog,
                  pady=10).pack(fill="x", padx=14, pady=4)

        tk.Button(left, text="📋  تقرير الصفقات", bg=CARD, fg=TEXT,
                  activebackground=hex_lighten(CARD), font=("Segoe UI", 10, "bold"),
                  relief="flat", cursor="hand2", command=lambda: self.notebook.select(2),
                  pady=10).pack(fill="x", padx=14, pady=4)

        # Stats cards
        stats_frame = tk.Frame(left, bg=BG2)
        stats_frame.pack(fill="x", padx=14, pady=(20, 0))

        self.stat_cards = {}
        cards = [
            ("net_r",    "+0.00R", "صافي R",       "Green"),
            ("win_rate", "0%",     "نسبة النجاح",  "Gold"),
            ("total",    "0",      "الصفقات",       "Stat"),
            ("balance",  "$0",     "الرصيد المتاح", "Blue"),
            ("reserved", "$0",     "محجوز Margin",  "Gold"),
        ]
        for key, val, label, style in cards:
            card = tk.Frame(stats_frame, bg=CARD, relief="flat", bd=0)
            card.pack(fill="x", pady=4)
            tk.Label(card, text=label, bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(pady=(8, 0))
            color = (ACCENT if style == "Green" else
                     GOLD   if style == "Gold"  else
                     BLUE   if style == "Blue"  else WHITE)
            lbl = tk.Label(card, text=val, bg=CARD, fg=color,
                           font=("Segoe UI", 16, "bold"))
            lbl.pack(pady=(0, 8))
            self.stat_cards[key] = lbl

        # Right panel: analysis output
        right = tk.Frame(frame, bg=BG)
        right.pack(side="left", fill="both", expand=True)

        tk.Label(right, text="اللوح المباشر", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(anchor="e", padx=15, pady=(14, 4))

        # Analysis display area (v3.31: scrollable so dashboard cards are not cut off on smaller screens)
        analysis_outer = tk.Frame(right, bg=BG)
        analysis_outer.pack(fill="both", expand=True, padx=12, pady=4)

        self.analysis_canvas = tk.Canvas(analysis_outer, bg=BG, highlightthickness=0)
        analysis_sb = ttk.Scrollbar(analysis_outer, orient="vertical", command=self.analysis_canvas.yview)
        self.analysis_frame = tk.Frame(self.analysis_canvas, bg=BG)
        self.analysis_frame.bind(
            "<Configure>",
            lambda e: self.analysis_canvas.configure(scrollregion=self.analysis_canvas.bbox("all"))
        )
        self._analysis_window_id = self.analysis_canvas.create_window((0, 0), window=self.analysis_frame, anchor="nw")
        self.analysis_canvas.configure(yscrollcommand=analysis_sb.set)
        self.analysis_canvas.bind("<Configure>",
            lambda e: self.analysis_canvas.itemconfig(self._analysis_window_id, width=e.width))
        analysis_sb.pack(side="right", fill="y")
        self.analysis_canvas.pack(side="left", fill="both", expand=True)

        def _bind_analysis_mw(e):
            self.analysis_canvas.bind("<MouseWheel>",
                lambda ev: self.analysis_canvas.yview_scroll(int(-1*(ev.delta/120)), "units"))
        def _unbind_analysis_mw(e):
            self.analysis_canvas.unbind("<MouseWheel>")
        self.analysis_canvas.bind("<Enter>", _bind_analysis_mw)
        self.analysis_canvas.bind("<Leave>", _unbind_analysis_mw)

        self._show_welcome()

    @staticmethod
    def _render_legs_box(parent, strat: dict, bg: str) -> None:
        """يرسم صندوق الأرجل + الأرقام المالية لأي استراتيجية."""
        name = strat.get("strategy", "")
        if not name or strat.get("no_trade"):
            return

        box = tk.Frame(parent, bg=bg, pady=2)
        box.pack(fill="x", padx=8, pady=(2, 4))

        # ── الأرجل ──
        legs_lines = []
        if name == "Iron Condor":
            sp, lp = strat.get("short_put"),  strat.get("long_put")
            sc, lc = strat.get("short_call"), strat.get("long_call")
            if sp and lp: legs_lines.append(f"Sell Put {sp:,.0f}  /  Buy Put {lp:,.0f}")
            if sc and lc: legs_lines.append(f"Sell Call {sc:,.0f}  /  Buy Call {lc:,.0f}")
        elif name == "Bull Put Spread":
            sp, lp = strat.get("short_put"), strat.get("long_put")
            if sp and lp: legs_lines.append(f"Sell Put {sp:,.0f}  /  Buy Put {lp:,.0f}")
        elif name == "Bear Call Spread":
            sc, lc = strat.get("short_call"), strat.get("long_call")
            if sc and lc: legs_lines.append(f"Sell Call {sc:,.0f}  /  Buy Call {lc:,.0f}")
        elif name == "Call Debit Spread":
            lc, sc = strat.get("long_call"), strat.get("short_call")
            if lc and sc: legs_lines.append(f"Buy Call {lc:,.0f}  /  Sell Call {sc:,.0f}")
        elif name == "Put Debit Spread":
            lp, sp = strat.get("long_put"), strat.get("short_put")
            if lp and sp: legs_lines.append(f"Buy Put {lp:,.0f}  /  Sell Put {sp:,.0f}")

        for line in legs_lines:
            tk.Label(box, text=line, bg=bg, fg="#c8d8ff",
                     font=("Consolas", 8, "bold")).pack(anchor="w", padx=4)

        # ── الأرقام ──
        is_credit = name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
        credit  = strat.get("credit")
        debit   = strat.get("debit")
        ml      = strat.get("max_loss")
        mg      = strat.get("max_gain")
        pop     = strat.get("pop")

        # Width
        width = None
        if name in ("Bull Put Spread", "Bear Call Spread"):
            a = strat.get("short_put") or strat.get("short_call") or 0
            b = strat.get("long_put")  or strat.get("long_call")  or 0
            if a and b: width = abs(a - b)
        elif name == "Iron Condor":
            sp, lp = strat.get("short_put",0) or 0, strat.get("long_put",0) or 0
            sc, lc = strat.get("short_call",0) or 0, strat.get("long_call",0) or 0
            if sp and lp: width = abs(sp - lp)
        elif name in ("Call Debit Spread", "Put Debit Spread"):
            a = strat.get("long_call")  or strat.get("long_put")  or 0
            b = strat.get("short_call") or strat.get("short_put") or 0
            if a and b: width = abs(a - b)

        nums = []
        if width:               nums.append(f"Width={width:.0f}")
        if credit:              nums.append(f"Credit={credit:.2f}")
        if debit:               nums.append(f"Debit={debit:.2f}")
        if ml:                  nums.append(f"MaxLoss={ml:.2f}")
        if mg:                  nums.append(f"MaxProfit={mg:.2f}")
        if pop is not None:     nums.append(f"POP={pop:.0f}%")
        if credit and width:
            cw = credit / width
            nums.append(f"C/W={cw:.0%}")
        if credit and ml and ml > 0:
            rr = round(credit / ml, 2)
            nums.append(f"R/R={rr}")

        if nums:
            tk.Label(box, text="  |  ".join(nums), bg=bg, fg="#aaa",
                     font=("Consolas", 7), wraplength=900, justify="left").pack(anchor="w", padx=4)

    @staticmethod
    def _render_sigma_delta_box(parent, strat: dict, data: dict, bg: str) -> None:
        """يعرض Delta و Sigma بناءً على منطق النسخة القديمة: EM = 1σ، والسترايك يقاس كمسافة من السعر/EM."""
        try:
            if not strat or strat.get("no_trade"):
                return
            name = strat.get("strategy", "")
            price = data.get("price") or data.get("underlying_price") or data.get("current_price")
            levels = data.get("levels") or {}
            em = (strat.get("expected_move") or data.get("expected_move") or
                  data.get("swing_em") or levels.get("expected_move"))
            try:
                price = float(price or 0)
                em = float(em or 0)
            except Exception:
                price, em = 0.0, 0.0

            legs = strat.get("legs_detail") or []
            def _eq(a, b):
                try: return abs(float(a) - float(b)) < 1e-6
                except Exception: return False
            def _delta_for(strike):
                if strike is None: return None
                for leg in legs:
                    if _eq(leg.get("strike"), strike):
                        d = leg.get("delta")
                        try: return float(d) if d is not None else None
                        except Exception: return None
                return None
            def _fmt_delta(d):
                return "—" if d is None else f"{d:+.2f}"

            sp, lp = strat.get("short_put"), strat.get("long_put")
            sc, lc = strat.get("short_call"), strat.get("long_call")
            spd, lpd = _delta_for(sp), _delta_for(lp)
            scd, lcd = _delta_for(sc), _delta_for(lc)

            rows = []
            if em:
                rows.append(f"EM/1σ=±{em:.2f}")
            if name == "Iron Condor":
                if sp and price and em:
                    rows.append(f"Put σ={abs(float(sp)-price)/em:.2f}")
                if sc and price and em:
                    rows.append(f"Call σ={abs(float(sc)-price)/em:.2f}")
                rows.append(f"ΔSP={_fmt_delta(spd)} ΔSC={_fmt_delta(scd)}")
            elif name == "Bull Put Spread":
                if sp and price and em:
                    rows.append(f"Short σ={abs(float(sp)-price)/em:.2f}")
                rows.append(f"Δ Short={_fmt_delta(spd)} / Long={_fmt_delta(lpd)}")
            elif name == "Bear Call Spread":
                if sc and price and em:
                    rows.append(f"Short σ={abs(float(sc)-price)/em:.2f}")
                rows.append(f"Δ Short={_fmt_delta(scd)} / Long={_fmt_delta(lcd)}")
            elif name == "Put Debit Spread":
                if lp and price and em:
                    rows.append(f"Long σ={abs(float(lp)-price)/em:.2f}")
                rows.append(f"Δ Long={_fmt_delta(lpd)} / Short={_fmt_delta(spd)}")
            elif name == "Call Debit Spread":
                if lc and price and em:
                    rows.append(f"Long σ={abs(float(lc)-price)/em:.2f}")
                rows.append(f"Δ Long={_fmt_delta(lcd)} / Short={_fmt_delta(scd)}")

            if not rows:
                return
            tk.Label(parent, text="σ/Delta: " + "  |  ".join(rows),
                     bg=bg, fg=BLUE, font=("Consolas", 7, "bold"),
                     wraplength=900, justify="left").pack(anchor="w", padx=10, pady=(0, 3))
        except Exception:
            return

    @staticmethod
    def _render_smc_box(parent, smc: dict, strat: dict | None = None, bg: str = None) -> None:
        """يعرض SMC Lite MTF: HTF direction + LTF confirmation + P/D + Sweep."""
        try:
            if not smc:
                return
            bg = bg or CARD
            available = smc.get("available", False)
            if not available and not smc.get("error") and not smc.get("dxlink_status") and not smc.get("yahoo_status"):
                return
            bias = smc.get("bias", "neutral")
            zone = smc.get("zone", "—")
            sweep = smc.get("recent_sweep", "None")
            adj = smc.get("smc_score", 0)
            htf_tf = smc.get("htf_timeframe") or "HTF"
            ltf_tf = smc.get("ltf_timeframe") or "LTF"
            htf_bias = smc.get("htf_bias", "neutral")
            ltf_bias = smc.get("ltf_bias", "neutral")
            htf_struct = smc.get("htf_last_structure", "—")
            ltf_struct = smc.get("ltf_last_structure", "—")
            color = ACCENT if bias == "bullish" else (RED if bias == "bearish" else (GOLD if bias == "mixed" else TEXT_DIM))
            adj_txt = f"{adj:+}" if isinstance(adj, (int, float)) else str(adj)
            dx_ok = smc.get("dxlink_candles_ok")
            dx_txt = f"DXLink={'OK' if dx_ok else 'UNAVAILABLE'}" if dx_ok is not None else "DXLink=UNKNOWN"
            dx_txt += " | yahoo_used=False"
            txt = (
                f"SMC MTF: {bias.upper()} | HTF({htf_tf})={htf_bias}/{htf_struct} "
                f"| LTF({ltf_tf})={ltf_bias}/{ltf_struct} | Zone={zone} | Sweep={sweep} | Adj={adj_txt} | {dx_txt}"
            )
            if not available and smc.get("error"):
                txt = f"SMC: unavailable — {smc.get('error')} | {dx_txt}"
                color = GOLD
            elif not available and (smc.get("yahoo_status") or smc.get("dxlink_status")):
                txt = f"SMC: neutral fallback — {smc.get('reason','OHLC unavailable')} | {dx_txt}"
                color = GOLD
            tk.Label(parent, text=txt, bg=bg, fg=color,
                     font=("Consolas", 7, "bold"), wraplength=620,
                     justify="left").pack(anchor="w", padx=8, pady=(1, 3))
        except Exception:
            return

    @staticmethod
    def _render_demand_supply_box(parent, ds: dict, strat: dict | None = None, bg: str = None) -> None:
        """يعرض Order Block / Demand-Supply MTF كطبقة Score فقط."""
        try:
            if not ds:
                return
            bg = bg or CARD
            available = ds.get("available", False)
            if not available and not ds.get("error") and not ds.get("dxlink_status") and not ds.get("yahoo_status"):
                return
            adj = ds.get("score", 0)
            direction = ds.get("direction", "neutral")
            htf = ds.get("htf", {}) or {}
            ltf = ds.get("ltf", {}) or {}
            htf_tf = htf.get("timeframe", "HTF")
            ltf_tf = ltf.get("timeframe", "LTF")
            hd = htf.get("nearest_demand") or {}
            hs = htf.get("nearest_supply") or {}
            ld = ltf.get("nearest_demand") or {}
            ls = ltf.get("nearest_supply") or {}

            def fmt_zone(z):
                if not z:
                    return "—"
                d = z.get("distance_pct")
                d_txt = f", {d:.2f}%" if isinstance(d, (int, float)) else ""
                return f"{z.get('low')}–{z.get('high')}{d_txt}"

            color = ACCENT if adj > 0 else (RED if adj < 0 else TEXT_DIM)
            adj_txt = f"{adj:+}" if isinstance(adj, (int, float)) else str(adj)
            dx_ok = ds.get("dxlink_candles_ok")
            dx_txt = f"DXLink={'OK' if dx_ok else 'UNAVAILABLE'}" if dx_ok is not None else "DXLink=UNKNOWN"
            dx_txt += " | yahoo_used=False"
            txt = (
                f"OrderBlock: dir={direction} | HTF({htf_tf}) Bull={fmt_zone(hd)} Bear={fmt_zone(hs)} "
                f"| LTF({ltf_tf}) Bull={fmt_zone(ld)} Bear={fmt_zone(ls)} | Adj={adj_txt} | {dx_txt}"
            )
            if not available and ds.get("error"):
                txt = f"OrderBlock: unavailable — {ds.get('error')} | {dx_txt}"
                color = GOLD
            elif not available and (ds.get("yahoo_status") or ds.get("dxlink_status")):
                txt = f"OrderBlock: neutral fallback — {ds.get('reason','OHLC unavailable')} | {dx_txt}"
                color = GOLD
            tk.Label(parent, text=txt, bg=bg, fg=color,
                     font=("Consolas", 7, "bold"), wraplength=620,
                     justify="left").pack(anchor="w", padx=8, pady=(1, 3))
        except Exception:
            return

    @staticmethod
    def _render_smc_0dte_mtf_box(parent, smc: dict, strat: dict | None = None, bg: str = None) -> None:
        """RC12: 0DTE-only SMC MTF diagnostics: 1H bias, 15m zones, 5m trigger."""
        try:
            if not smc:
                return
            bg = bg or CARD
            if not smc.get("enabled", True) and not smc.get("available"):
                return
            scope = smc.get("scope", "SPY/QQQ/IWM 0DTE only")
            verdict = smc.get("verdict") or smc.get("reason") or "not_available"
            direction = smc.get("direction", "neutral")
            h1 = smc.get("h1") or {}
            m15 = smc.get("m15") or {}
            m5 = smc.get("m5") or {}

            def fmt_zone(z):
                if not z:
                    return "—"
                d = z.get("distance_pct")
                d_txt = f", {d:.2f}%" if isinstance(d, (int, float)) else ""
                status = z.get("status") or ""
                status_txt = f" {status}" if status else ""
                return f"{z.get('low')}–{z.get('high')}{status_txt}{d_txt}"

            demand = smc.get("nearest_15m_demand") or m15.get("nearest_demand") or {}
            supply = smc.get("nearest_15m_supply") or m15.get("nearest_supply") or {}
            sweep = smc.get("liquidity_sweep_5m") or m5.get("recent_sweep") or "None"
            fresh_reason = smc.get("fresh_break_reason_5m") or m5.get("fresh_break_reason") or "—"
            supports = smc.get("supports") or []
            warnings = smc.get("warnings") or []
            color = ACCENT if verdict == "supports_trade" else (RED if verdict == "warns_trade" else (GOLD if verdict == "mixed_context" else TEXT_DIM))
            txt = (
                f"SMC 0DTE MTF: {verdict} | dir={direction} | "
                f"1H bias={h1.get('bias','—')} | 15m structure={m15.get('last_structure','—')} | "
                f"Demand={fmt_zone(demand)} | Supply={fmt_zone(supply)} | "
                f"5m trigger={m5.get('last_structure','—')} | sweep={sweep} | "
                f"fresh={fresh_reason} | diagnostics only | {scope}"
            )
            if supports:
                txt += "\nSupports: " + "; ".join(str(x) for x in supports[:3])
            if warnings:
                txt += "\nWarnings: " + "; ".join(str(x) for x in warnings[:3])
            if smc.get("error"):
                txt = f"SMC 0DTE MTF: unavailable — {smc.get('error')} | diagnostics only | {scope}"
                color = GOLD
            elif not smc.get("available") and smc.get("reason"):
                txt = f"SMC 0DTE MTF: {smc.get('reason')} | diagnostics only | {scope}"
                color = TEXT_DIM
            tk.Label(parent, text=txt, bg=bg, fg=color,
                     font=("Consolas", 7, "bold"), wraplength=720,
                     justify="left").pack(anchor="w", padx=8, pady=(1, 3))
        except Exception:
            return


    @staticmethod
    def _render_entry_smc_snapshot(parent, snapshot_json: str, bg: str = None, compact: bool = False) -> None:
        """RC12b: render persisted SMC snapshot saved at paper-trade entry."""
        try:
            if not snapshot_json:
                return
            import json
            bg = bg or BG
            snap = json.loads(snapshot_json or "{}")
            smc = snap.get("smc_0dte_mtf") if isinstance(snap, dict) else {}
            if not isinstance(smc, dict):
                return

            def _val(v, default="—"):
                return default if v in (None, "", []) else v

            def _fmt_zone(z):
                if not isinstance(z, dict) or not z:
                    return "—"
                d = z.get("distance_pct")
                d_txt = f", {d:.2f}%" if isinstance(d, (int, float)) else ""
                status = z.get("status") or ""
                status_txt = f" {status}" if status else ""
                return f"{_val(z.get('low'))}–{_val(z.get('high'))}{status_txt}{d_txt}"

            h1 = smc.get("h1") or {}
            m15 = smc.get("m15") or {}
            m5 = smc.get("m5") or {}
            demand = smc.get("nearest_15m_demand") or m15.get("nearest_demand") or {}
            supply = smc.get("nearest_15m_supply") or m15.get("nearest_supply") or {}
            supports = smc.get("supports") or []
            warnings = smc.get("warnings") or []
            verdict = smc.get("verdict") or smc.get("reason") or ("unavailable" if not smc.get("available") else "—")
            direction = smc.get("direction") or "neutral"
            sweep = smc.get("liquidity_sweep_5m") or m5.get("recent_sweep") or "None"
            fresh = smc.get("fresh_break_reason_5m") or m5.get("fresh_break_reason") or "—"
            color = ACCENT if verdict == "supports_trade" else (RED if verdict == "warns_trade" else (GOLD if verdict == "mixed_context" else TEXT_DIM))

            tk.Label(parent, text="SMC Snapshot at Entry", bg=bg, fg=BLUE,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(8, 2))

            rows = [
                ("Captured", snap.get("captured_at")),
                ("Verdict", verdict),
                ("Direction", direction),
                ("1H Bias", h1.get("bias")),
                ("15m Structure", m15.get("last_structure")),
                ("15m Demand", _fmt_zone(demand)),
                ("15m Supply", _fmt_zone(supply)),
                ("5m Trigger", m5.get("last_structure")),
                ("5m Fresh", fresh),
                ("Liquidity Sweep", sweep),
            ]
            for label, val in rows:
                f = tk.Frame(parent, bg=BG2); f.pack(fill="x", padx=16, pady=1)
                tk.Label(f, text=label, bg=BG2, fg=TEXT_DIM,
                         font=("Segoe UI", 8), width=18, anchor="e").pack(side="left")
                tk.Label(f, text=str(_val(val)), bg=BG2,
                         fg=color if label == "Verdict" else TEXT,
                         font=("Segoe UI", 8, "bold"), anchor="w", wraplength=360,
                         justify="left").pack(side="left", padx=8, fill="x", expand=True)
            if supports:
                tk.Label(parent, text="Supports: " + "; ".join(str(x) for x in supports[:4]),
                         bg=bg, fg=ACCENT, font=("Segoe UI", 8), wraplength=500,
                         justify="left").pack(anchor="w", padx=20, pady=(2, 0), fill="x")
            if warnings:
                tk.Label(parent, text="Warnings: " + "; ".join(str(x) for x in warnings[:4]),
                         bg=bg, fg=GOLD, font=("Segoe UI", 8), wraplength=500,
                         justify="left").pack(anchor="w", padx=20, pady=(2, 0), fill="x")
        except Exception:
            return

    @staticmethod
    def _liquidity_status(strat: dict) -> tuple:
        """
        يفحص legs_detail ويعيد (status, color, icon).

        6 حالات واضحة:
          1. Not checked  — الاستراتيجية رُفضت قبل بناء الأرجل
          2. Chain unavailable — الـ chain لم يُجلب
          3. Expiry not found  — لا expiry في النطاق
          4. Strike not found  — لا strike مناسب
          5. FAIL              — الأرجل موجودة لكن bid/ask سيء
          6. PASS              — كل شيء جيد
        """
        legs     = strat.get("legs_detail") or []
        warnings = strat.get("warnings")    or []
        reasons  = strat.get("reasons")     or []
        no_trade = strat.get("no_trade", False)

        # نص مجمّع للبحث فيه (lowercase)
        all_txt = " ".join(warnings + reasons).lower()

        if not legs:
            # ── لا أرجل — نحدد السبب ─────────────────────────────────────
            if any(k in all_txt for k in ("chain unavailable", "chain غير متاح",
                                          "chain = none", "chain_unavailable")):
                return "Chain unavailable", "#ff8c00", "⛔"

            if any(k in all_txt for k in ("expir", "no expiry", "expiry not found",
                                          "expiry مفقود", "لا expiry")):
                return "Expiry not found", "#ff8c00", "⛔"

            if any(k in all_txt for k in ("strike", "no valid strike",
                                          "strike not found", "لا strike")):
                return "Strike not found", "#ff8c00", "⛔"

            if no_trade:
                return "Not checked — rejected before liquidity", "#888", "—"

            return "N/A — no valid option legs", "#888", "—"

        # ── الأرجل موجودة — فحص bid/ask ──────────────────────────────────
        has_missing  = any("لا توجد bid/ask" in w or "bid/ask" in w.lower()
                           for w in warnings)
        has_critical = any("سيولة ضعيفة" in w or "spread > 50" in w.lower()
                           for w in warnings)
        has_wide     = any("تحقق من التنفيذ" in w or "spread > 25" in w.lower()
                           for w in warnings)

        if has_missing or has_critical:
            return "FAIL — bid/ask unavailable or spread too wide", "#ff4757", "❌"
        if has_wide:
            return "WARN — spread wide", "#ffd700", "⚠️"

        all_ok = all(leg.get("bid") and leg.get("ask") for leg in legs if leg)
        if all_ok:
            return "PASS", "#00e676", "✅"

        # أرجل موجودة لكن بعضها فاقدة bid/ask
        return "FAIL — bid/ask missing on some legs", "#ff4757", "❌"

    @staticmethod
    def _compact_ui_text(value, max_len: int = 90) -> str:
        """اختصار نصوص التشخيص الطويلة ومنع عرض dict/list خام في الواجهة."""
        try:
            txt = str(value or "").replace("\n", " ").replace("\r", " ")
        except Exception:
            return "—"
        # لا تعرض قواميس/قوائم خام طويلة في UI الرئيسي
        if ("{" in txt and "}" in txt) or ("[" in txt and "]" in txt):
            low = txt.lower()
            if "credit_width" in low:
                txt = "Credit/Width context available — details hidden"
            elif "delta" in low:
                txt = "Delta/Greeks context available — details hidden"
            elif "vanna" in low or "charm" in low or "gex" in low:
                txt = "Greeks/GEX context available — details hidden"
            else:
                txt = "Detailed diagnostics hidden — use Why?/Diagnostics"
        # اختصارات شائعة
        replacements = {
            "score_below_auto_threshold": "score below threshold",
            "no valid option legs": "no valid option legs",
            "Credit/Width rejects": "Credit/Width reject",
            "Delta rejects": "Delta reject",
            "intraday_ema_alignment_failed": "EMA alignment failed",
            "same_symbol_direction_0dte_cap_reached": "exposure cap reached",
            "stale_bearish_signal_no_fresh_breakdown": "stale bearish signal — no fresh breakdown",
            "stale_bullish_signal_no_fresh_breakout": "stale bullish signal — no fresh breakout",
            "reentry_requires_fresh_breakdown": "re-entry requires fresh breakdown",
            "reentry_requires_fresh_breakout": "re-entry requires fresh breakout",
            "rc11_reentry_guard_error_reject": "RC11 re-entry guard error",
        }
        for k, v in replacements.items():
            txt = txt.replace(k, v)
        txt = " ".join(txt.split())
        return txt[:max_len] + ("…" if len(txt) > max_len else "")

    @staticmethod
    def _compact_strategy_summary(sym: str, strat: dict, reason: str = "") -> str:
        try:
            name = (strat or {}).get("strategy") or "No Trade"
            score = (strat or {}).get("score", 0)
            if (strat or {}).get("no_trade"):
                score_txt = "N/A" if not score else str(score)
                r = SPXBotApp._compact_ui_text(reason or (strat or {}).get("decision") or (strat or {}).get("reject_reason") or "No valid candidate", 44)
                return f"{sym}: — {r} ({score_txt})"
            return f"{sym}: ✓ {name} {float(score or 0):.0f}"
        except Exception:
            return f"{sym}: —"

    # ── خريطة أسباب الرفض المبسّطة ──────────────────────────────────────────
    _SCORE_KW = ("مقبول", "قوي", "ادرس", "تحقق", "دراسة", "✅", "⚠️",
                 "مناسب", "ضعيف", "دراسة فقط")
    _REASON_MAP = [
        # Trend
        ("neutral",             "Trend محايد — لا اتجاه واضح"),
        ("Trend",               "Trend لا يدعم الاستراتيجية"),
        # Chain / Data
        ("chain unavailable",   "Chain unavailable — لا بيانات للـ DTE المطلوب"),
        ("chain = none",        "Chain unavailable"),
        ("no chain",            "Chain unavailable"),
        # Expiry / Strike
        ("no expiry",           "Expiry not found — لا expiry في النطاق"),
        ("expiry not found",    "Expiry not found"),
        ("strike not found",    "Strike not found — لا strike مناسب"),
        ("no valid strike",     "Strike not found"),
        # Liquidity
        ("poor liquidity",      "Liquidity: FAIL — spread too wide"),
        ("bid/ask",             "Liquidity: FAIL — bid/ask unavailable"),
        # DTE
        ("DTE",                 "DTE mismatch — لا expiry في النطاق"),
        # Strategy status
        ("probation",           "الاستراتيجية في Probation Mode"),
        ("disabled",            "الاستراتيجية معطّلة"),
        # Credit/Delta
        ("credit/width",        "نسبة Credit/Width ضعيفة"),
        ("Delta",               "Delta خارج النطاق المسموح"),
        # Other
        ("IV Neutral",          "IV Neutral — لا أفضلية"),
        ("Gamma",               "Gamma مرتفع — خطر EOD"),
        ("لا توجد",             "لا توجد استراتيجية تتجاوز الحد"),
        # تجاهل
        ("Score",               None),
        ("مرفوض",               None),
    ]

    @staticmethod
    def _map_reason(raw: str) -> str:
        """يحوّل السبب الخام إلى رسالة مفهومة."""
        if not raw:
            return ""
        for key, msg in SPXBotApp._REASON_MAP:
            if key in raw:
                return msg or ""
        return raw[:60]

    @staticmethod
    def _real_rejection(strat: dict, sym: str = "",
                        analysis_data: dict = None) -> str:
        """
        يستخرج سبب الرفض الفعلي بالأولوية:
          1. Liquidity FAIL/N/A
          2. reasons من _no_trade()
          3. _swing_diag rejected list
          4. warnings فيلترة
          5. signal_rejections table
        """
        SKW = SPXBotApp._SCORE_KW

        # 1. Liquidity — الحالات الست
        liq_s, _, _ = SPXBotApp._liquidity_status(strat)
        if liq_s.startswith("FAIL"):
            return f"Liquidity {liq_s}"
        if liq_s in ("Chain unavailable", "Expiry not found", "Strike not found"):
            return f"Liquidity: {liq_s}"

        # 2. reasons من _no_trade()
        for r in (strat.get("reasons") or []):
            if not r or any(kw in r for kw in SKW):
                continue
            mapped = SPXBotApp._map_reason(r)
            if mapped:
                return mapped

        # 3. _swing_diag للرمز
        if analysis_data and sym:
            sd = (analysis_data.get("_swing_diag") or {}).get(sym.upper(), {})
            for r in (sd.get("rejected") or []):
                if not r or any(kw in r for kw in SKW):
                    continue
                mapped = SPXBotApp._map_reason(r)
                if mapped:
                    return mapped

        # 4. warnings فيلترة
        for w in (strat.get("warnings") or []):
            if not w or any(kw in w for kw in SKW):
                continue
            mapped = SPXBotApp._map_reason(w)
            if mapped:
                return mapped

        # 5. آخر رفض من trade_monitor
        try:
            from core.database import get_recent_rejections
            strat_name = strat.get("strategy", "")
            for r in get_recent_rejections(20):
                if r.get("symbol") == sym.upper() and \
                        (not strat_name or r.get("strategy") == strat_name):
                    reason = r.get("reason", "")
                    if reason and not any(kw in reason for kw in SKW):
                        return SPXBotApp._map_reason(reason) or reason[:60]
        except Exception:
            pass

        return ""

    @staticmethod
    def _path_enabled(sym: str) -> bool:
        """هل يوجد أي مسار (Paper أو Auto) مفعّل لهذا الرمز؟"""
        try:
            from core.database import get_enabled_trade_paths
            return any(p["symbol"] == sym for p in get_enabled_trade_paths())
        except Exception:
            return True

    def _show_why_winner(self, symbol: str, strat_name: str, analysis: dict):
        """نافذة تشخيص شاملة: مقارنة كل الرموز جنباً لجنب مع تفصيل الدرجة."""

        # جمع بيانات كل الرموز
        symbol_data_map = {
            "SPX": analysis,
            "SPY": analysis.get("spy") or {},
            "QQQ": analysis.get("qqq") or {},
            "IWM": analysis.get("iwm") or {},
        }
        symbol_map = {
            "SPX": analysis.get("strategy") or {},
            "SPY": (analysis.get("spy") or {}).get("strategy") or {},
            "QQQ": (analysis.get("qqq") or {}).get("strategy") or {},
            "IWM": (analysis.get("iwm") or {}).get("strategy") or {},
        }
        # RC12i: attach/synchronize per-symbol 0DTE SMC/DXLink diagnostics to each Why? card.
        # Some No-Trade cards keep SMC diagnostics at the symbol analysis level rather than inside strategy;
        # in other refresh paths the symbol-level snapshot may not be attached at all.  In that case, display
        # only already-attached cached SMC snapshots; no on-demand candle fetch inside Why? to prevent freezes.
        def _resolve_smc0_for_why(_sym: str, _container: dict, _strat: dict) -> dict:
            """
            RC12l: Return the full SMC snapshot for Why? card display.
            Priority:
              1. smc_0dte_mtf_full  — explicit full snapshot written at analysis time
              2. smc_0dte_mtf       — same object, alternate key
              3. container-level copies of the above (symbol analysis dict)
            A snapshot is considered valid if it contains h1 OR m15 sub-dicts
            (even when available=False — e.g. insufficient candles).
            A snapshot with only top-level summary keys (bias_1h etc.) but no
            h1/m15 is NOT sufficient for the Order Block panel.
            """
            def _has_full_data(src) -> bool:
                """True if src carries h1 or m15 sub-dicts (full snapshot, not summary-only)."""
                if not isinstance(src, dict):
                    return False
                return bool(src.get("h1") or src.get("m15") or src.get("m5"))

            try:
                # Pass 1: prefer sources that carry full h1/m15 data
                for _src in (
                    _strat.get("smc_0dte_mtf_full"),
                    _strat.get("smc_0dte_mtf"),
                    _container.get("smc_0dte_mtf_full") if isinstance(_container, dict) else None,
                    _container.get("smc_0dte_mtf") if isinstance(_container, dict) else None,
                    _container.get("smc_mtf") if isinstance(_container, dict) else None,
                ):
                    if _has_full_data(_src):
                        return _src

                # Pass 2: accept any DXLink-flagged snapshot even without h1/m15
                # (error/exception path — shows reason instead of "not attached")
                for _src in (
                    _strat.get("smc_0dte_mtf_full"),
                    _strat.get("smc_0dte_mtf"),
                    _container.get("smc_0dte_mtf_full") if isinstance(_container, dict) else None,
                    _container.get("smc_0dte_mtf") if isinstance(_container, dict) else None,
                ):
                    if isinstance(_src, dict) and (
                        _src.get("enabled") or
                        _src.get("diagnostics_only") or
                        "dxlink" in str(_src.get("source", "")).lower()
                    ) and not _src.get("why_attach_mode"):  # exclude our own fallback dict
                        return _src

            except Exception:
                pass

            # RC12j safety: no on-demand fetch in Why? thread — show informative placeholder
            if _sym in ("SPY", "QQQ", "IWM"):
                return {
                    "available": False,
                    "source": "dxlink_smc_0dte_mtf",
                    "reason": "full snapshot not attached — run full analysis to refresh",
                    "symbol": _sym,
                    "why_attach_mode": "display_only_no_on_demand",
                }
            return {}

        for _sym, _strat in list(symbol_map.items()):
            if not isinstance(_strat, dict):
                continue
            _container = symbol_data_map.get(_sym) or {}
            if _sym in ("SPY", "QQQ", "IWM"):
                _smc0 = _resolve_smc0_for_why(_sym, _container if isinstance(_container, dict) else {}, _strat)
                if isinstance(_smc0, dict) and _smc0:
                    _strat["smc_0dte_mtf"] = _smc0
                    # Mirror DXLink SMC zones into the legacy demand_supply slot used by the Why? OB panel.
                    _m15 = _smc0.get("m15") or {}
                    _h1 = _smc0.get("h1") or {}
                    # RC12k: mirror even when available=False (e.g. insufficient candles).
                    # Checking only available=True caused "not attached" message whenever candles < 20.
                    # We now mirror if h1/m15 timeframe dicts exist at all (source is DXLink, data is partial).
                    if _h1 or _m15 or _smc0.get("enabled") or _smc0.get("h1") is not None:
                        _strat["demand_supply"] = {
                            "available": bool(_m15.get("available") or _smc0.get("available")),
                            "source": "dxlink_smc_0dte_mtf",
                            "reason": "attached to Why? card from SMC DXLink snapshot",
                            "score": 0,
                            "htf": {
                                "available": bool(_h1.get("available")),
                                "source": "dxlink_smc_0dte_mtf",
                                "timeframe": "1h",
                                "candles": _h1.get("candles", 0),
                                "bias": _h1.get("bias"),
                                "demand": _h1.get("demand") or [],
                                "supply": _h1.get("supply") or [],
                                "active_bullish_order_blocks": _h1.get("demand") or [],
                                "active_bearish_order_blocks": _h1.get("supply") or [],
                                "nearest_demand": _h1.get("nearest_demand"),
                                "nearest_supply": _h1.get("nearest_supply"),
                                "reason": _h1.get("reason", "available" if _h1.get("available") else "no 1H SMC zones"),
                            },
                            "ltf": {
                                "available": bool(_m15.get("available")),
                                "source": "dxlink_smc_0dte_mtf",
                                "timeframe": "15m",
                                "candles": _m15.get("candles", 0),
                                "bias": _m15.get("bias"),
                                "demand": _m15.get("demand") or [],
                                "supply": _m15.get("supply") or [],
                                "active_bullish_order_blocks": _m15.get("demand") or [],
                                "active_bearish_order_blocks": _m15.get("supply") or [],
                                "nearest_demand": _m15.get("nearest_demand"),
                                "nearest_supply": _m15.get("nearest_supply"),
                                "reason": _m15.get("reason", "available" if _m15.get("available") else "no 15m SMC zones"),
                            },
                        }
                if not _strat.get("demand_supply") and isinstance(_container, dict):
                    _ds0 = _container.get("demand_supply") or _container.get("order_block")
                    if isinstance(_ds0, dict) and not str(_ds0.get("source", "")).lower().startswith("yahoo"):
                        _strat["demand_supply"] = _ds0
        # market context لكل رمز (Pin / GEX / EM)
        ctx_map = {
            "SPX": {"pin": analysis.get("pin_score", 0),
                    "gex": analysis.get("net_gex", 0),
                    "em":  analysis.get("expected_move", 0),
                    "iv":  analysis.get("iv_rank", 0)},
            "SPY": {"pin": (analysis.get("spy") or {}).get("pin_score", 0),
                    "gex": (analysis.get("spy") or {}).get("net_gex", 0),
                    "em":  (analysis.get("spy") or {}).get("expected_move", 0),
                    "iv":  (analysis.get("spy") or {}).get("iv_rank", 0)},
            "QQQ": {"pin": (analysis.get("qqq") or {}).get("pin_score", 0),
                    "gex": (analysis.get("qqq") or {}).get("net_gex", 0),
                    "em":  (analysis.get("qqq") or {}).get("expected_move", 0),
                    "iv":  (analysis.get("qqq") or {}).get("iv_rank", 0)},
        }

        win = tk.Toplevel(self.root)
        win.title("Why Registered / Why Rejected — تشخيص شامل")
        win.geometry("900x580")
        win.configure(bg=BG)
        win.resizable(True, True)

        tk.Label(win, text="🔍  Why Registered / Why Rejected — تشخيص القرار لكل رمز",
                 bg=BG, fg=WHITE, font=("Segoe UI", 12, "bold")).pack(pady=(14, 4), padx=16, anchor="w")
        tk.Label(win,
                 text="يعرض Final Decision والـ threshold وتفاصيل Trend/GEX/IV وSMC وOrder Block والمخاطر",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w", padx=16)

        # RC15i.5: Canvas + Scrollbar لتمكين scroll بعجلة الفأرة
        _scroll_outer = tk.Frame(win, bg=BG)
        _scroll_outer.pack(fill="both", expand=True, padx=12, pady=8)

        _vbar = tk.Scrollbar(_scroll_outer, orient="vertical")
        _vbar.pack(side="right", fill="y")

        _canvas = tk.Canvas(_scroll_outer, bg=BG, highlightthickness=0,
                             yscrollcommand=_vbar.set)
        _canvas.pack(side="left", fill="both", expand=True)
        _vbar.config(command=_canvas.yview)

        cols_frame = tk.Frame(_canvas, bg=BG)
        _canvas_win = _canvas.create_window((0, 0), window=cols_frame, anchor="nw")

        def _on_cols_configure(event):
            _canvas.configure(scrollregion=_canvas.bbox("all"))
            _canvas.itemconfig(_canvas_win, width=_canvas.winfo_width())
        cols_frame.bind("<Configure>", _on_cols_configure)
        _canvas.bind("<Configure>",
                     lambda e: _canvas.itemconfig(_canvas_win, width=e.width))

        def _on_mousewheel(event):
            _canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        def _bind_scroll(widget):
            widget.bind("<MouseWheel>", _on_mousewheel)
            for child in widget.winfo_children():
                _bind_scroll(child)
        win.bind("<MouseWheel>", _on_mousewheel)

        for sym, strat in symbol_map.items():
            is_winner = sym == symbol  # ranking winner / best candidate only

            # RC15a — Resolve the actual paper-registration state before styling the card.
            # A ranking winner is NOT the same thing as a registered paper trade.
            pis_list = analysis.get("_paper_insert_status") or []
            pis = None
            s_no_trade_pre = strat.get("no_trade", True)
            for _ev in reversed(pis_list):
                if str(_ev.get("symbol", "")).upper() != sym:
                    continue
                if s_no_trade_pre and bool(_ev.get("inserted")):
                    continue
                pis = _ev
                break
            inserted_flag = bool(pis and pis.get("inserted"))
            attempted_flag = bool(pis and pis.get("insert_attempted"))
            selected_flag = bool(pis and pis.get("selected"))
            inserted_trade_id = pis.get("inserted_trade_id") if pis else None

            # Green is reserved for an actually inserted trade.  The best candidate that
            # was not inserted gets a neutral/amber card instead of a false success state.
            col_bg = "#0d2b0d" if inserted_flag else ("#2b260d" if is_winner else CARD)
            col = tk.Frame(cols_frame, bg=col_bg)
            col.pack(side="left", fill="both", expand=True, padx=4)

            # Header
            s_no_trade = strat.get("no_trade", True)
            s_name     = strat.get("strategy", "No Trade")
            s_score    = strat.get("score", 0)

            # إذا كان No Trade، اعرض أفضل raw score من all_scores
            all_scores = strat.get("all_scores", {})
            best_raw   = max(all_scores.values(), default=0) if all_scores else 0
            best_raw_name = max(all_scores, key=all_scores.get, default="") if all_scores else ""
            try:
                from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                best_threshold = STRATEGY_MIN_SCORE.get(best_raw_name, MIN_SCORE_TO_TRADE)
            except Exception:
                best_threshold = 60

            display_score = best_raw if s_no_trade else s_score
            s_color = ACCENT if display_score >= 75 else (GOLD if display_score >= 60 else (TEXT if display_score >= 40 else RED))
            is_valid_candidate = is_winner and not s_no_trade
            if inserted_flag:
                winner_mark = f"  ✅ REGISTERED" + (f"  ID {inserted_trade_id}" if inserted_trade_id else "")
            elif selected_flag and attempted_flag:
                winner_mark = "  ⚠ SELECTED — NOT INSERTED"
            elif is_valid_candidate:
                winner_mark = "  ► BEST CANDIDATE — NOT REGISTERED"
            elif is_winner:
                winner_mark = "  ► أفضل مرشح"
            else:
                winner_mark = ""

            tk.Label(col, text=f"{sym}{winner_mark}",
                     bg=col_bg, fg=ACCENT if inserted_flag else (GOLD if is_winner else WHITE),
                     font=("Segoe UI", 11, "bold")).pack(pady=(10, 2), padx=10, anchor="w")
            tk.Label(col, text=f"{strat.get('emoji','🚫')} {s_name if not s_no_trade else best_raw_name or 'No Trade'}",
                     bg=col_bg, fg=s_color,
                     font=("Segoe UI", 9, "bold")).pack(padx=10, anchor="w")

            try:
                from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                required_score = STRATEGY_MIN_SCORE.get(s_name, MIN_SCORE_TO_TRADE) if s_name else best_threshold
            except Exception:
                required_score = best_threshold
            _smc_swing_block  = bool((strat or {}).get("smc_swing_block"))
            _smc_swing_filter = (strat or {}).get("smc_swing_filter") or {}
            _smc_swing_adj    = (strat or {}).get("smc_swing_adj")
            _swing_fa         = _smc_swing_filter.get("final_action", "")
            _swing_reject_label = (
                _swing_fa if _swing_fa and "REJECTED" in _swing_fa
                else "REJECTED_SWING_SMC_CONFLICT"
            )
            _explicit_decision = (strat or {}).get("decision") or ""
            _pis_final = (pis or {}).get("final_action") or ""
            _pis_reason = (pis or {}).get("block_reason") or ""

            if inserted_flag:
                final_decision = "REGISTERED" + (f"  |  Trade ID: {inserted_trade_id}" if inserted_trade_id else "")
            elif selected_flag and attempted_flag:
                final_decision = f"SELECTED — INSERT FAILED: {_pis_reason or _pis_final or 'unknown reason'}"
            elif is_valid_candidate:
                # It won the ranking but did not become a paper trade.
                final_decision = f"BEST CANDIDATE — NOT REGISTERED: {_pis_reason or _pis_final or 'registration not attempted'}"
            else:
                final_decision = (
                    _swing_reject_label if _smc_swing_block else (
                    _explicit_decision if _explicit_decision.startswith("REJECTED_SWING_") else (
                    f"REJECTED_SCORE {display_score} < {required_score}" if display_score < required_score else
                    ("FILTERS PASSED / NOT SELECTED" if not s_no_trade else "NO_TRADE")))
                )
            if s_no_trade and best_raw > 0:
                tk.Label(col, text=f"Raw: {best_raw}/100  |  Required: {best_threshold}",
                         bg=col_bg, fg=GOLD, font=("Segoe UI", 8)).pack(padx=10, anchor="w")
                tk.Label(col, text=f"Final Decision: {final_decision}",
                         bg=col_bg, fg=RED, font=("Segoe UI", 8)).pack(padx=10, anchor="w")
            else:
                if s_no_trade and (not best_raw):
                    tk.Label(col, text=f"Score: N/A — no valid candidate  |  Required: {required_score}",
                             bg=col_bg, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(padx=10, anchor="w")
                else:
                    tk.Label(col, text=f"Score: {s_score}/100  |  Required: {required_score}",
                             bg=col_bg, fg=s_color, font=("Segoe UI", 9)).pack(padx=10, anchor="w")
                tk.Label(col, text=f"Final Decision: {final_decision}",
                         bg=col_bg, fg=ACCENT if inserted_flag else (GOLD if is_valid_candidate else TEXT_DIM), font=("Segoe UI", 8)).pack(padx=10, anchor="w")
            # RC15f — Swing ICT/SMC + EMA confirmation display (must appear even when RC13 SMC filter did not apply)
            if not _smc_swing_filter.get("applied"):
                _rc15f       = (strat or {}).get("rc15f_swing_confirmation") or {}
                _ict_score   = (strat or {}).get("ict_smc_score")
                _ict_conf    = (strat or {}).get("ict_smc_confidence")
                _ict_pass    = (strat or {}).get("ict_smc_pass")
                _ema_pass    = (strat or {}).get("ema_alignment_pass")
                _ict_details = (strat or {}).get("ict_smc_details") or {}
                _ema_details = (strat or {}).get("ema_details") or {}
                if _rc15f or _ict_score is not None or _ema_pass is not None:
                    tk.Label(col, text="── RC15f Swing ICT/SMC + EMA ──", bg=col_bg, fg=TEXT_DIM,
                             font=("Consolas", 7)).pack(padx=10, anchor="w")
                    _rc15f_na = bool(_rc15f.get("not_applicable") or (strat or {}).get("rc15f_not_applicable"))
                    if _rc15f_na:
                        tk.Label(col, text="  RC15f: NOT_APPLICABLE — directional Swing Debit only",
                                 bg=col_bg, fg=TEXT_DIM, font=("Consolas", 7),
                                 wraplength=260, justify="left").pack(padx=10, anchor="w")
                        tk.Label(col, text=f"  Reason: {_rc15f.get('reason') or 'RC15f applies only to Swing Call Debit Spread / Put Debit Spread'}",
                                 bg=col_bg, fg=TEXT_DIM, font=("Consolas", 7),
                                 wraplength=260, justify="left").pack(padx=10, anchor="w")
                        _ict_col = TEXT_DIM
                        _ema_col = TEXT_DIM
                    else:
                        _ict_col = ACCENT if _ict_pass and (_ict_score or 0) >= 4 else (GOLD if _ict_score == 3 else RED)
                        _ema_col = ACCENT if _ema_pass else RED
                        tk.Label(col, text=f"  ICT/SMC: {_ict_score}/5  {_ict_conf or ''}",
                                 bg=col_bg, fg=_ict_col, font=("Consolas", 7),
                                 wraplength=260, justify="left").pack(padx=10, anchor="w")
                    try:
                        _checks = (_ict_details.get("checks") or {})
                        for _lbl, _key in [
                            ("Premium/Discount", "premium_discount"),
                            ("Liquidity Sweep", "liquidity_sweep"),
                            ("MSS after Sweep", "mss"),
                            ("Displacement/FVG", "displacement_fvg"),
                            ("FVG/OB Retest", "fvg_ob_retest"),
                        ]:
                            _ok = bool(_checks.get(_key))
                            tk.Label(col, text=f"    {_lbl}: {'PASS' if _ok else 'FAIL'}",
                                     bg=col_bg, fg=ACCENT if _ok else RED,
                                     font=("Consolas", 7)).pack(padx=10, anchor="w")
                    except Exception:
                        pass
                    _ema_reason = (_ema_details.get("reason") if isinstance(_ema_details, dict) else "") or ""
                    tk.Label(col, text=f"  EMA Alignment: {'PASS' if _ema_pass else 'FAIL'}  {_ema_reason}",
                             bg=col_bg, fg=_ema_col, font=("Consolas", 7),
                             wraplength=260, justify="left").pack(padx=10, anchor="w")
                    _rc_reason = (strat or {}).get("swing_block_reason") or (strat or {}).get("swing_watchlist_reason") or _rc15f.get("decision")
                    if _rc_reason:
                        tk.Label(col, text=f"  RC15f Decision: {_rc_reason}",
                                 bg=col_bg, fg=RED if (strat or {}).get("swing_block_reason") else TEXT_DIM,
                                 font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                else:
                    tk.Label(col, text="── RC15f Swing ICT/SMC + EMA ──", bg=col_bg, fg=TEXT_DIM,
                             font=("Consolas", 7)).pack(padx=10, anchor="w")
                    tk.Label(col, text="  RC15f: NOT_EVALUATED - rejected earlier by Delta/4H trend.",
                             bg=col_bg, fg=TEXT_DIM, font=("Consolas", 7),
                             wraplength=260, justify="left").pack(padx=10, anchor="w")

            # RC13 — SMC Swing Filter full decision tree display
            if _smc_swing_filter.get("applied"):
                _sf_adj      = _smc_swing_filter.get("adj", 0)
                _sf_action   = _smc_swing_filter.get("action", "")
                _sf_h1       = _smc_swing_filter.get("h1_bias", "?")
                _sf_m15      = _smc_swing_filter.get("m15_bias", "?")
                _sf_m5       = _smc_swing_filter.get("m5_bias", "?")
                _sf_ema15    = _smc_swing_filter.get("ema15_status", "unavailable")
                _sf_ema5     = _smc_swing_filter.get("ema5_status", "unavailable")
                _sf_fa       = _smc_swing_filter.get("final_action", "NO_CHANGE")
                _sf_base     = _smc_swing_filter.get("base_score", "?")
                _sf_ds_warn  = _smc_swing_filter.get("ds_warning", False)
                _sf_ind      = _smc_swing_filter.get("inside_demand_1h") or _smc_swing_filter.get("inside_supply_1h")
                _sf_near_d   = _smc_swing_filter.get("near_demand_1h")
                _sf_near_s   = _smc_swing_filter.get("near_supply_1h")
                _sf_nd       = _smc_swing_filter.get("nd_1h") or {}
                _sf_ns       = _smc_swing_filter.get("ns_1h") or {}
                _rc15f       = (strat or {}).get("rc15f_swing_confirmation") or {}
                _ict_score   = (strat or {}).get("ict_smc_score")
                _ict_conf    = (strat or {}).get("ict_smc_confidence")
                _ict_pass    = (strat or {}).get("ict_smc_pass")
                _ema_pass    = (strat or {}).get("ema_alignment_pass")
                _ict_details = (strat or {}).get("ict_smc_details") or {}
                _ema_details = (strat or {}).get("ema_details") or {}
                # Header line
                tk.Label(col, text="── Swing Entry Filters ──", bg=col_bg, fg=TEXT_DIM,
                         font=("Consolas", 7)).pack(padx=10, anchor="w")
                if _rc15f or _ict_score is not None or _ema_pass is not None:
                    _rc15f_na = bool(_rc15f.get("not_applicable") or (strat or {}).get("rc15f_not_applicable"))
                    if _rc15f_na:
                        _ict_col = TEXT_DIM
                        _ema_col = TEXT_DIM
                        tk.Label(col, text="  RC15f: NOT_APPLICABLE — directional Swing Debit only",
                                 bg=col_bg, fg=TEXT_DIM, font=("Consolas", 7),
                                 wraplength=260, justify="left").pack(padx=10, anchor="w")
                        tk.Label(col, text=f"  Reason: {_rc15f.get('reason') or 'RC15f applies only to Swing Call Debit Spread / Put Debit Spread'}",
                                 bg=col_bg, fg=TEXT_DIM, font=("Consolas", 7),
                                 wraplength=260, justify="left").pack(padx=10, anchor="w")
                    else:
                        _ict_col = ACCENT if _ict_pass and (_ict_score or 0) >= 4 else (GOLD if _ict_score == 3 else RED)
                        _ema_col = ACCENT if _ema_pass else RED
                        tk.Label(col, text=f"  RC15f ICT/SMC: {_ict_score}/5  {_ict_conf or ''}",
                                 bg=col_bg, fg=_ict_col, font=("Consolas", 7),
                                 wraplength=260, justify="left").pack(padx=10, anchor="w")
                    try:
                        _checks = (_ict_details.get("checks") or {})
                        for _lbl, _key in [
                            ("Premium/Discount", "premium_discount"),
                            ("Liquidity Sweep", "liquidity_sweep"),
                            ("MSS", "mss"),
                            ("Displacement/FVG", "displacement_fvg"),
                            ("FVG/OB Retest", "fvg_ob_retest"),
                        ]:
                            _ok = bool(_checks.get(_key))
                            tk.Label(col, text=f"    {_lbl}: {'PASS' if _ok else 'FAIL'}",
                                     bg=col_bg, fg=ACCENT if _ok else RED,
                                     font=("Consolas", 7)).pack(padx=10, anchor="w")
                    except Exception:
                        pass
                    _ema_reason = (_ema_details.get("reason") if isinstance(_ema_details, dict) else "") or ""
                    tk.Label(col, text=f"  EMA Alignment: {'PASS' if _ema_pass else 'FAIL'}  {_ema_reason}",
                             bg=col_bg, fg=_ema_col, font=("Consolas", 7),
                             wraplength=260, justify="left").pack(padx=10, anchor="w")
                    _rc_reason = (strat or {}).get("swing_block_reason") or (strat or {}).get("swing_watchlist_reason") or _rc15f.get("decision")
                    if _rc_reason:
                        tk.Label(col, text=f"  RC15f Decision: {_rc_reason}",
                                 bg=col_bg, fg=RED if (strat or {}).get("swing_block_reason") else TEXT_DIM,
                                 font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                else:
                    tk.Label(col, text="  RC15f: NOT_EVALUATED - rejected earlier by Delta/4H trend.",
                             bg=col_bg, fg=TEXT_DIM, font=("Consolas", 7),
                             wraplength=260, justify="left").pack(padx=10, anchor="w")
                # Base score
                tk.Label(col, text=f"  Base Score: {_sf_base}", bg=col_bg, fg=TEXT_DIM,
                         font=("Consolas", 7)).pack(padx=10, anchor="w")
                # SMC biases
                _h1_col  = (ACCENT if _sf_h1 in ("bearish","strong_bearish") else
                            RED    if _sf_h1 in ("bullish","strong_bullish") else TEXT_DIM)
                _m15_col = (ACCENT if _sf_m15 in ("bearish","strong_bearish") else
                            RED    if _sf_m15 in ("bullish","strong_bullish") else TEXT_DIM)
                tk.Label(col, text=f"  SMC 1H:  {_sf_h1}", bg=col_bg, fg=_h1_col,
                         font=("Consolas", 7)).pack(padx=10, anchor="w")
                tk.Label(col, text=f"  SMC 15m: {_sf_m15}", bg=col_bg, fg=_m15_col,
                         font=("Consolas", 7)).pack(padx=10, anchor="w")
                tk.Label(col, text=f"  SMC 5m:  {_sf_m5}", bg=col_bg, fg=TEXT_DIM,
                         font=("Consolas", 7)).pack(padx=10, anchor="w")
                tk.Label(col, text=f"  15m EMA20/EMA50: {_sf_ema15}", bg=col_bg, fg=TEXT_DIM,
                         font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                tk.Label(col, text=f"  5m EMA20/EMA50:  {_sf_ema5}", bg=col_bg, fg=TEXT_DIM,
                         font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                # D/S zones
                try:
                    if _sf_nd:
                        _d_lbl = ("INSIDE" if _sf_nd.get("inside") else
                                  "NEAR"   if _sf_near_d else "far")
                        _d_col = RED if _d_lbl in ("INSIDE","NEAR") else TEXT_DIM
                        _d_txt = (f"  Demand 1H: {_d_lbl}"
                                  f"  {_sf_nd.get('low','?')}-{_sf_nd.get('high','?')}"
                                  f"  dist={_sf_nd.get('distance_pct','?')}%"
                                  f"  conf={_sf_nd.get('confidence','?')}"
                                  f"  age={_sf_nd.get('age_bars','?')}/{_sf_nd.get('max_age_bars','?')}"
                                  f"  status={_sf_nd.get('status','?')}")
                        tk.Label(col, text=_d_txt, bg=col_bg, fg=_d_col,
                                 font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                    if _sf_ns:
                        _s_lbl = ("INSIDE" if _sf_ns.get("inside") else
                                  "NEAR"   if _sf_near_s else "far")
                        _s_col = ACCENT if _s_lbl in ("INSIDE","NEAR") else TEXT_DIM
                        _s_txt = (f"  Supply 1H: {_s_lbl}"
                                  f"  {_sf_ns.get('low','?')}-{_sf_ns.get('high','?')}"
                                  f"  dist={_sf_ns.get('distance_pct','?')}%"
                                  f"  conf={_sf_ns.get('confidence','?')}"
                                  f"  age={_sf_ns.get('age_bars','?')}/{_sf_ns.get('max_age_bars','?')}"
                                  f"  status={_sf_ns.get('status','?')}")
                        tk.Label(col, text=_s_txt, bg=col_bg, fg=_s_col,
                                 font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                except Exception:
                    pass
                # D/S warning
                if _sf_ds_warn:
                    tk.Label(col, text=f"  D/S: ⚠ zone near/inside but not strong enough for hard reject",
                             bg=col_bg, fg=GOLD, font=("Consolas", 7),
                             wraplength=260, justify="left").pack(padx=10, anchor="w")
                # Score adj / reject result
                if _sf_action == "hard_reject":
                    _rej_map = {
                        "REJECTED_SWING_DEMAND_BOUNCE": "D/S: Demand + bullish 15m → BOUNCE RISK",
                        "REJECTED_SWING_SUPPLY_REJECTION": "D/S: Supply + bearish confirm → REJECTION RISK",
                        "REJECTED_SWING_SUPPLY_BOUNCE": "D/S: Supply + bearish 15m → REJECTION RISK",
                        "REJECTED_SWING_SMC_CONFLICT":  "SMC: 1H+15m conflict vs Swing trade",
                    }
                    _rej_txt = _rej_map.get(_sf_fa, f"Rejected: {_sf_fa}")
                    tk.Label(col, text=f"  Filter: {_rej_txt}", bg=col_bg, fg=RED,
                             font=("Consolas", 7), wraplength=260, justify="left").pack(padx=10, anchor="w")
                elif _sf_adj > 0:
                    tk.Label(col, text=f"  Filter: boost +{_sf_adj} applied",
                             bg=col_bg, fg=ACCENT, font=("Consolas", 7)).pack(padx=10, anchor="w")
                elif _sf_adj < 0:
                    tk.Label(col, text=f"  Filter: penalty {_sf_adj} applied",
                             bg=col_bg, fg=GOLD, font=("Consolas", 7)).pack(padx=10, anchor="w")
                elif _sf_fa == "BOOST_SKIPPED_LOW_BASE":
                    tk.Label(col, text=f"  Filter: boost skipped (base score {_sf_base} < 45)",
                             bg=col_bg, fg=GOLD, font=("Consolas", 7)).pack(padx=10, anchor="w")
                # Final action tag
                _fa_col = (RED    if "REJECTED" in _sf_fa else
                           ACCENT if "BOOST"    in _sf_fa else
                           GOLD   if "PENALTY"  in _sf_fa or "WARNING" in _sf_fa or "SKIPPED" in _sf_fa
                           else TEXT_DIM)
                tk.Label(col, text=f"  Final Swing Action: {_sf_fa}",
                         bg=col_bg, fg=_fa_col, font=("Consolas", 7),
                         wraplength=260, justify="left").pack(padx=10, anchor="w")

            # RC12d — Paper insert status: selected vs actually inserted into paper_trades.
            try:
                if pis:
                    inserted = bool(pis.get("inserted"))
                    attempted = bool(pis.get("insert_attempted"))
                    selected_flag = bool(pis.get("selected"))
                    final_a = pis.get("final_action") or "—"
                    reason = pis.get("block_reason") or ("inserted" if inserted else "—")
                    tid = pis.get("inserted_trade_id")
                    tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=3)
                    tk.Label(col, text="Paper Insert Status:", bg=col_bg, fg=BLUE,
                             font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=10)
                    line1 = f"selected={str(selected_flag).lower()} | attempted={str(attempted).lower()} | inserted={str(inserted).lower()}"
                    if tid:
                        line1 += f" | id={tid}"
                    tk.Label(col, text="  " + line1, bg=col_bg,
                             fg=ACCENT if inserted else GOLD, font=("Consolas", 7),
                             wraplength=250, justify="left").pack(anchor="w", padx=10)
                    tk.Label(col, text=f"  final_action={final_a}", bg=col_bg, fg=TEXT_DIM,
                             font=("Consolas", 7), wraplength=250, justify="left").pack(anchor="w", padx=10)
                    tk.Label(col, text=f"  reason={reason}", bg=col_bg,
                             fg=ACCENT if inserted else RED, font=("Consolas", 7),
                             wraplength=250, justify="left").pack(anchor="w", padx=10)
                else:
                    tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=3)
                    tk.Label(col, text="Paper Insert Status:", bg=col_bg, fg=BLUE,
                             font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=10)
                    _msg = "no insert-status event captured in this analysis cycle"
                    if s_no_trade:
                        _msg = "not applicable — no valid candidate for this symbol"
                    tk.Label(col, text="  " + _msg, bg=col_bg, fg=TEXT_DIM,
                             font=("Consolas", 7), wraplength=250, justify="left").pack(anchor="w", padx=10)
            except Exception:
                pass

            # Explain why a candidate passed filters but was not selected.
            try:
                if (not inserted_flag) and (not is_winner) and (not s_no_trade) and display_score >= required_score:
                    selected_name = (symbol_map.get(symbol) or {}).get("strategy", "?")
                    selected_score = (symbol_map.get(symbol) or {}).get("score", 0)
                    why_not = (
                        f"Not selected: {symbol} selected "
                        f"({selected_name}, score={selected_score})."
                    )
                    if display_score == selected_score:
                        why_not += " Tie resolved by ranking/priority and current selected candidate."
                    tk.Label(col, text=why_not, bg=col_bg, fg=GOLD,
                             font=("Segoe UI", 7), wraplength=250, justify="left").pack(padx=10, anchor="w")
                elif s_no_trade:
                    if _smc_swing_block:
                        _rj = (strat or {}).get("reject_reason") or f"1H={_smc_swing_filter.get('h1_bias','?')} & 15m={_smc_swing_filter.get('m15_bias','?')} conflict"
                        reject_hint = f"{_swing_reject_label}: {_rj}"
                    else:
                        reject_hint = strat.get("decision") or strat.get("reject_reason") or strat.get("final_action_reason") or "No candidate passed final filters"
                    tk.Label(col, text=f"Reject reason: {reject_hint}", bg=col_bg, fg=RED,
                             font=("Segoe UI", 7), wraplength=250, justify="left").pack(padx=10, anchor="w")
            except Exception:
                pass

            # Context (Pin / GEX / EM / IV)
            ctx = ctx_map.get(sym, {})
            gex_v = ctx.get("gex") or 0
            gex_s = "+" if gex_v >= 0 else ""
            tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=4)
            for label, val in [
                ("Pin",     f"{ctx.get('pin', 0)}/100"),
                ("Net GEX", f"{gex_s}{gex_v:,.0f}"),
                ("EM",      f"±{ctx.get('em', 0)}"),
                ("IV Rank", f"{ctx.get('iv', 0):.0f}" if ctx.get('iv') is not None else "—"),
            ]:
                r = tk.Frame(col, bg=col_bg)
                r.pack(fill="x", padx=10, pady=1)
                tk.Label(r, text=f"{label}:", bg=col_bg, fg=TEXT_DIM,
                         font=("Segoe UI", 8)).pack(side="left")
                tk.Label(r, text=val, bg=col_bg, fg=TEXT,
                         font=("Segoe UI", 8, "bold")).pack(side="right")

            # Score Breakdown
            breakdown = strat.get("score_breakdown", {})
            if breakdown:
                tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=4)
                tk.Label(col, text="تفصيل الدرجة:", bg=col_bg, fg=TEXT_DIM,
                         font=("Segoe UI", 8)).pack(anchor="w", padx=10)
                max_abs = max((abs(v) for v in breakdown.values() if isinstance(v, (int, float))), default=1)
                for k, v in breakdown.items():
                    if not isinstance(v, (int, float)):
                        continue
                    sign  = "+" if v >= 0 else ""
                    color = ACCENT if v > 0 else (RED if v < 0 else TEXT_DIM)
                    bar_w = max(int(abs(v) / max(max_abs, 1) * 80), 1)
                    row   = tk.Frame(col, bg=col_bg)
                    row.pack(fill="x", padx=10, pady=1)
                    tk.Label(row, text=f"{k[:10]:10}", bg=col_bg, fg=TEXT_DIM,
                             font=("Consolas", 7)).pack(side="left")
                    tk.Frame(row, bg=color, width=bar_w, height=8).pack(side="left", padx=2)
                    tk.Label(row, text=f"{sign}{v:>4}", bg=col_bg, fg=color,
                             font=("Consolas", 8, "bold")).pack(side="right")

                tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=3)
                tk.Label(col, text=f"Total: {s_score}/100",
                         bg=col_bg, fg=GOLD, font=("Consolas", 9, "bold")).pack(anchor="w", padx=10)

            # SMC / Order Block diagnostics
            try:
                tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=3)
                tk.Label(col, text="SMC MTF:", bg=col_bg, fg=BLUE, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=10)
                smc_obj = strat.get("smc") or {}
                smc_adj = breakdown.get('SMC MTF', strat.get('smc_score_adjustment', strat.get('smc_score', 0)))
                smc_htf_bias = strat.get('smc_htf_bias', '—')
                smc_ltf_bias = strat.get('smc_ltf_bias', '—')
                smc_zone = strat.get('smc_zone', '—')
                smc_sweep = strat.get('smc_recent_sweep', '—')
                smc_reason = "active"
                if not smc_obj:
                    smc_reason = "unavailable / not returned by SMC engine"
                elif smc_adj == 0:
                    if str(smc_htf_bias).lower() in ('neutral', 'none', '—', '-') and str(smc_ltf_bias).lower() in ('neutral', 'none', '—', '-'):
                        smc_reason = "neutral HTF/LTF bias; no score adjustment"
                    elif smc_zone in ('—', '-', None, '') and smc_sweep in ('—', '-', None, '', 'None'):
                        smc_reason = "no confirmed zone/sweep support; no score adjustment"
                    else:
                        smc_reason = "SMC available but net adjustment is 0"
                smc_lines = [
                    f"Source: {'available' if smc_obj else 'unavailable'}",
                    f"HTF: {strat.get('smc_htf_tf','1H')} bias={smc_htf_bias}",
                    f"LTF: {strat.get('smc_ltf_tf','15m')} bias={smc_ltf_bias}",
                    f"Zone: {smc_zone}",
                    f"Sweep: {smc_sweep}",
                    f"Adj: {smc_adj}",
                    f"Reason: {smc_reason}",
                ]
                for line in smc_lines:
                    fg = TEXT if line.startswith('Reason:') else TEXT_DIM
                    tk.Label(col, text="  " + line, bg=col_bg, fg=fg, font=("Consolas", 7)).pack(anchor="w", padx=10)

                tk.Label(col, text="Order Block / Demand-Supply:", bg=col_bg, fg=BLUE, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=10, pady=(2,0))
                ds = strat.get('demand_supply') or {}
                ob_adj = breakdown.get('Order Block / Demand-Supply', breakdown.get('Demand/Supply', strat.get('order_block_score_adjustment', 0)))

                # RC12l: for SPY/QQQ/IWM, use the full DXLink SMC snapshot directly.
                # smc_0dte_mtf on strat was set by _resolve_smc0_for_why (pass 1078).
                # We prefer smc_0dte_mtf_full (explicit key added by RC12l in analyzer).
                _sym_container = symbol_data_map.get(sym) or {}
                smc0 = (
                    strat.get('smc_0dte_mtf_full') or
                    strat.get('smc_0dte_mtf') or
                    (isinstance(_sym_container, dict) and (
                        _sym_container.get('smc_0dte_mtf_full') or
                        _sym_container.get('smc_0dte_mtf') or
                        _sym_container.get('smc_mtf')
                    )) or {}
                )
                if not isinstance(smc0, dict):
                    smc0 = {}
                _m15_probe = smc0.get('m15') or {}
                _h1_probe  = smc0.get('h1')  or {}
                _m5_probe  = smc0.get('m5')  or {}
                # RC12l: use DXLink path for SPY/QQQ/IWM whenever snapshot was attempted
                # (even available=False). Require h1 OR m15 to be present in snapshot.
                _has_full_snapshot = bool(_h1_probe or _m15_probe or _m5_probe)
                _has_dxlink_smc    = bool(smc0.get('available') or _m15_probe.get('available') or _h1_probe.get('available') or _m5_probe.get('available'))
                use_dxlink_smc_ob  = (sym in ('SPY', 'QQQ', 'IWM') and (
                    _has_full_snapshot or
                    _has_dxlink_smc or
                    (isinstance(ds, dict) and ds.get("source") == "dxlink_smc_0dte_mtf")
                ))
                suppress_legacy_yahoo_ob = (sym in ('SPY', 'QQQ', 'IWM'))

                def _fmt_zone_short(z):
                    if not isinstance(z, dict) or not z:
                        return 'none'
                    lo = z.get('low', '—'); hi = z.get('high', '—')
                    dist = z.get('distance_pct')
                    status = z.get('status') or z.get('structure_event') or ''
                    dist_txt = f", dist={dist}%" if dist not in (None, '', '—') else ''
                    st_txt = f", {status}" if status else ''
                    return f"{lo}-{hi}{st_txt}{dist_txt}"

                if use_dxlink_smc_ob:
                    # RC12l: use full h1/m15 sub-dicts from the snapshot.
                    # Fallback to ds.htf/ltf only if smc0 has no sub-dicts (error path).
                    m15 = smc0.get('m15') or (ds.get('ltf') if isinstance(ds, dict) else None) or {}
                    h1  = smc0.get('h1')  or (ds.get('htf') if isinstance(ds, dict) else None) or {}
                    m5  = smc0.get('m5')  or {}
                    nd = smc0.get('nearest_15m_demand') or m15.get('nearest_demand') or {}
                    ns = smc0.get('nearest_15m_supply') or m15.get('nearest_supply') or {}
                    demand_count  = len(m15.get('demand') or [])
                    supply_count  = len(m15.get('supply') or [])
                    h1_candles    = h1.get('candles', '?')
                    m15_candles   = m15.get('candles', '?')
                    m5_candles    = m5.get('candles', '?')
                    h1_bias       = h1.get('bias') or smc0.get('bias_1h') or '—'
                    m15_structure = m15.get('last_structure') or smc0.get('structure_15m') or '—'
                    m5_trigger    = m5.get('last_structure') or smc0.get('trigger_5m') or '—'
                    sweep         = smc0.get('liquidity_sweep_5m') or m5.get('recent_sweep') or 'None'
                    _dxlink_avail = bool(smc0.get('available') or m15.get('available') or h1.get('available'))
                    # Collect reason from all levels: top, h1, m15 (insufficient candles lives inside h1/m15)
                    _smc_error = (smc0.get('error') or smc0.get('reason') or
                                  h1.get('reason') or m15.get('reason') or '')
                    if _dxlink_avail:
                        _dxlink_reason = "DXLink SMC zones available"
                    elif _smc_error and "insufficient" in str(_smc_error):
                        _dxlink_reason = f"insufficient candles for zone detection — 1H:{h1_candles}, 15m:{m15_candles}, 5m:{m5_candles} (need ≥20)"
                    elif _smc_error:
                        _dxlink_reason = f"DXLink error: {str(_smc_error)[:80]}"
                    else:
                        _dxlink_reason = "DXLink snapshot attached; no active zones detected"
                    ob_lines = [
                        f"Available: {_dxlink_avail}",
                        "Source: dxlink_smc_0dte_mtf",
                        f"HTF: 1H bias={h1_bias}, candles={h1_candles}",
                        f"LTF: 15m structure={m15_structure}, demand={demand_count}, supply={supply_count}, candles={m15_candles}",
                        f"5m: trigger={m5_trigger}, sweep={sweep}, candles={m5_candles}",
                        f"Nearest Demand: {_fmt_zone_short(nd)}",
                        f"Nearest Supply: {_fmt_zone_short(ns)}",
                        f"Reason: {_dxlink_reason}",
                        f"Adj: {ob_adj}",
                    ]
                else:
                    htf_ds = ds.get('htf', {}) if isinstance(ds, dict) else {}
                    ltf_ds = ds.get('ltf', {}) if isinstance(ds, dict) else {}
                    def _tf_state(d):
                        if not d:
                            return 'missing'
                        if not d.get('available'):
                            return f"unavailable:{d.get('reason','unknown')}"
                        bull = len(d.get('active_bullish_order_blocks') or d.get('demand') or [])
                        bear = len(d.get('active_bearish_order_blocks') or d.get('supply') or [])
                        return f"active demand={bull}, supply={bear}, candles={d.get('candles','?')}"
                    if suppress_legacy_yahoo_ob and (not ds or str(ds.get('source','')).lower().startswith('yahoo') or 'YAHOO' in str(ds.get('reason','')).upper()):
                        ob_lines = [
                            "Available: False",
                            "Source: dxlink_smc_0dte_mtf",
                            "HTF: DXLink SMC snapshot not attached to this Why? card",
                            "LTF: DXLink SMC snapshot not attached to this Why? card",
                            "Reason: legacy Yahoo OB suppressed for SPY/QQQ/IWM 0DTE; run full analysis; on-demand SMC is disabled to prevent UI freeze",
                            f"Adj: {ob_adj}",
                        ]
                    else:
                        if not ds:
                            ob_reason = "not returned by Order Block engine"
                        elif not ds.get('available'):
                            ob_reason = ds.get('reason') or htf_ds.get('reason') or ltf_ds.get('reason') or ds.get('source') or 'unavailable'
                        elif ob_adj == 0:
                            ob_reason = "OB data available but no valid fresh/near block affected this setup"
                        else:
                            ob_reason = "OB adjustment applied"
                        ob_lines = [
                            f"Available: {bool(ds and ds.get('available'))}",
                            f"Source: {ds.get('source','—') if isinstance(ds, dict) else '—'}",
                            f"HTF: {_tf_state(htf_ds)}",
                            f"LTF: {_tf_state(ltf_ds)}",
                            f"Reason: {ob_reason}",
                            f"Adj: {ob_adj}",
                        ]
                for line in ob_lines:
                    fg = TEXT if line.startswith('Reason:') else TEXT_DIM
                    tk.Label(col, text="  " + line, bg=col_bg, fg=fg, font=("Consolas", 7), wraplength=250, justify="left").pack(anchor="w", padx=10)
            except Exception:
                pass

            # Liquidity
            liq_s, liq_c, liq_i = SPXBotApp._liquidity_status(strat)
            tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=3)
            tk.Label(col, text=f"Liquidity: {liq_i} {liq_s}",
                     bg=col_bg, fg=liq_c, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=10, pady=2)

            # Warnings split into hard blockers vs warning-only diagnostics.
            warnings = strat.get("warnings", [])
            if warnings:
                tk.Frame(col, bg=BORDER, height=1).pack(fill="x", padx=8, pady=4)
                tk.Label(col, text="⚠ Diagnostics:", bg=col_bg, fg=GOLD,
                         font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=10)
                hard_keys = ("⛔", "لا تنفذ", "reject", "rejected", "blocked", "missing bid", "no quotes", "Credit = 0", "Debit = 0", "لا أسعار")
                hard = []
                soft = []
                for w in warnings:
                    txt = str(w)
                    if any(k.lower() in txt.lower() for k in hard_keys):
                        hard.append(txt)
                    else:
                        soft.append(txt)
                if hard:
                    tk.Label(col, text="  Hard / Blocking:", bg=col_bg, fg=RED,
                             font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=10)
                    for w in hard[:3]:
                        tk.Label(col, text=f"    • {w[:60]}", bg=col_bg, fg=RED,
                                 font=("Segoe UI", 7), wraplength=250, justify="left").pack(anchor="w", padx=10)
                if soft:
                    tk.Label(col, text="  Warning only:", bg=col_bg, fg=GOLD,
                             font=("Segoe UI", 7, "bold")).pack(anchor="w", padx=10)
                    for w in soft[:4]:
                        tk.Label(col, text=f"    • {w[:60]}", bg=col_bg, fg=GOLD,
                                 font=("Segoe UI", 7), wraplength=250, justify="left").pack(anchor="w", padx=10)

        # RC15i.5: ربط scroll بكل العناصر الداخلية بعد اكتمال البناء
        win.after(100, lambda: _bind_scroll(cols_frame))

        tk.Button(win, text="إغلاق", bg=CARD, fg=TEXT, relief="flat",
                  cursor="hand2", font=("Segoe UI", 9),
                  command=win.destroy).pack(pady=12)

    def _show_symbol_cards(self, analysis: dict):
        """عرض بطاقات SPY / QQQ / IWM بجانب بعض كتحليل كامل."""
        symbols = [
            ("spy", "SPY", "S&P 500 ETF"),
            ("qqq", "QQQ", "Nasdaq ETF"),
            ("iwm", "IWM", "Russell 2000 ETF"),
        ]
        row = tk.Frame(self.analysis_frame, bg=BG)
        row.pack(fill="x", pady=(4, 2))

        for key, ticker, name in symbols:
            data = analysis.get(key, {})
            if not data or "error" in data:
                err = data.get("error", "لا بيانات") if data else "لا بيانات"
                cell = tk.Frame(row, bg=CARD)
                cell.pack(side="left", fill="both", expand=True, padx=4, pady=2)
                tk.Label(cell, text=f"{ticker} — {err}", bg=CARD, fg=RED,
                         font=("Segoe UI", 9)).pack(padx=10, pady=8)
                continue

            price_v   = data.get("price")
            pin       = data.get("pin_score", 0)
            pin_color = ACCENT if pin >= 70 else (GOLD if pin >= 50 else RED)
            trend_v   = data.get("trend_label", "")
            net_gex   = data.get("net_gex") or 0
            gex_color = ACCENT if net_gex > 0 else RED
            gex_sign  = "+" if net_gex > 0 else ""
            em        = data.get("expected_move")
            iv_rank   = data.get("iv_rank")
            strategy  = data.get("strategy") or {}
            s_name    = strategy.get("strategy", "—")
            if isinstance(s_name, (dict, list)) or len(str(s_name)) > 60 or "{" in str(s_name):
                s_name = strategy.get("selected_strategy") or strategy.get("trade_strategy") or "Candidate"
            s_score   = strategy.get("score", 0)
            s_emoji   = strategy.get("emoji", "")
            s_decision= strategy.get("decision", "")
            s_color   = ACCENT if s_score >= 75 else (GOLD if s_score >= 60 else RED)
            dq        = data.get("data_quality_report", {})
            mode      = dq.get("mode", "?")
            mode_color= ACCENT if mode == "LIVE" else (GOLD if mode == "PARTIAL" else RED)

            cell = tk.Frame(row, bg=CARD)
            cell.pack(side="left", fill="both", expand=True, padx=4, pady=2)

            # Header
            h = tk.Frame(cell, bg=CARD)
            h.pack(fill="x", padx=8, pady=(6, 2))
            tk.Label(h, text=f"{ticker}  ${price_v:,.2f}" if price_v else ticker,
                     bg=CARD, fg=WHITE, font=("Segoe UI", 13, "bold")).pack(side="left")
            tk.Label(h, text=f"● {mode}", bg=CARD, fg=mode_color,
                     font=("Segoe UI", 8, "bold")).pack(side="right")

            # Pin + Trend
            tk.Label(cell, text=f"Pin: {pin}/100  |  {trend_v}",
                     bg=CARD, fg=pin_color, font=("Segoe UI", 9)).pack(anchor="w", padx=8)

            # GEX + EM — مع توضيح حالة البيانات
            gex_txt = f"Net GEX: {gex_sign}{net_gex:,.0f}"
            if net_gex == 0:
                gex_quality = data.get("gex_quality", "unavailable")
                gex_note    = " (لا بيانات)" if gex_quality == "unavailable" else " (توازن)"
                gex_txt    += gex_note
                gex_color   = TEXT_DIM
            em_txt  = f"  |  EM: ±{em}" if em else ""
            iv_rank_txt = f"  |  IV Rank: {iv_rank:.0f}" if iv_rank is not None else ""
            tk.Label(cell, text=gex_txt + em_txt + iv_rank_txt,
                     bg=CARD, fg=gex_color, font=("Segoe UI", 8)).pack(anchor="w", padx=8)

            # IV Percentile + IV Regime
            iv_pct   = data.get("iv_percentile")
            iv_reg   = data.get("iv_regime", "")
            if iv_pct is not None:
                reg_color = RED if iv_reg == "Credit-favored" else (BLUE if iv_reg == "Debit-favored" else TEXT_DIM)
                tk.Label(cell,
                         text=f"IV Pct: {iv_pct:.0f}%  |  Regime: {iv_reg}",
                         bg=CARD, fg=reg_color, font=("Segoe UI", 8)).pack(anchor="w", padx=8)

            # SMC Lite v1 — BOS/CHoCH + Premium/Discount + Sweep
            try:
                self._render_smc_box(cell, data.get("smc") or strategy.get("smc") or {}, strategy, CARD)
                self._render_demand_supply_box(cell, data.get("demand_supply") or strategy.get("demand_supply") or {}, strategy, CARD)
                self._render_smc_0dte_mtf_box(cell, data.get("smc_0dte_mtf") or strategy.get("smc_0dte_mtf") or {}, strategy, CARD)
            except Exception:
                pass

            # Data Quality warning إذا كانت Greeks غير متاحة
            dq_checks = dq.get("checks", {})
            missing_greeks = not dq_checks.get("delta") and not dq_checks.get("gamma")
            if missing_greeks:
                tk.Label(cell, text="⚠ Greeks unavailable — GEX/Score قد يكون مقدّراً",
                         bg=CARD, fg=GOLD, font=("Segoe UI", 7)).pack(anchor="w", padx=8)

            # Strategy — عرض واضح مع Raw Score والسبب الحقيقي للرفض
            no_trade_sym  = strategy.get("no_trade", True)
            all_sc        = strategy.get("all_scores", {})
            raw           = max(all_sc.values(), default=0) if all_sc and no_trade_sym else s_score
            best_n        = max(all_sc, key=all_sc.get, default=s_name) if all_sc and no_trade_sym else s_name
            try:
                from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                thresh = STRATEGY_MIN_SCORE.get(best_n, MIN_SCORE_TO_TRADE)
            except Exception:
                thresh = 60

            # Liquidity + Risk
            liq_status, liq_color, liq_icon = self._liquidity_status(strategy)
            tk.Label(cell, text=f"Liquidity: {liq_icon} {liq_status}",
                     bg=CARD, fg=liq_color, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8)
            try:
                from core.risk_manager import check_risk, calculate_max_loss_per_contract
                ml = calculate_max_loss_per_contract(strategy, ticker)
                if ml is not None:
                    from core.risk_manager import get_risk_settings, max_risk_dollar
                    rs = get_risk_settings()
                    mr = max_risk_dollar(rs)
                    risk_ok = ml <= mr
                    risk_icon = "✅" if risk_ok else "❌"
                    risk_color = ACCENT if risk_ok else RED
                    tk.Label(cell, text=f"Risk: {risk_icon} MaxLoss ${ml:,.0f}  (حد: ${mr:.0f})",
                             bg=CARD, fg=risk_color, font=("Segoe UI", 8)).pack(anchor="w", padx=8)
            except Exception:
                pass

            # ── تحديد الحالة الفعلية: 4 حالات واضحة ────────────────────────
            # (دوال _real_rejection و _path_enabled معرّفة كـ @staticmethod في الـ class)

            if not no_trade_sym:
                # ── ① Qualified — المحرك قبلها ───────────────────────────────
                status_bg = "#0a2a0a"
                tk.Frame(cell, bg=status_bg, height=1).pack(fill="x", padx=8, pady=(2, 0))
                q_row = tk.Frame(cell, bg=status_bg)
                q_row.pack(fill="x", padx=8, pady=2)
                tk.Label(q_row, text="✅ Qualified",
                         bg=status_bg, fg=ACCENT,
                         font=("Segoe UI", 8, "bold")).pack(side="left")
                tk.Label(q_row, text=f"{s_emoji} {s_name}  {s_score}/100",
                         bg=status_bg, fg=s_color,
                         font=("Segoe UI", 8, "bold")).pack(side="right")
                if s_decision and not any(k in s_decision for k in _SCORE_KEYWORDS):
                    tk.Label(cell, text=self._compact_ui_text(s_decision, 80), bg=CARD, fg=TEXT_DIM,
                             font=("Segoe UI", 7)).pack(anchor="w", padx=8, pady=(0, 4))
                else:
                    tk.Label(cell, text="", bg=CARD).pack(pady=2)

            elif raw >= thresh:
                # ── ② Score جيد لكن المحرك رفض — سبب حقيقي ─────────────────
                reject_reason = self._real_rejection(strategy, ticker, analysis)
                path_ok = self._path_enabled(ticker)

                tk.Label(cell, text=f"🔶 {best_n}  {raw}/100",
                         bg=CARD, fg=GOLD, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8, pady=(2, 0))

                if not path_ok:
                    # ── ③ Signal Only — Path disabled ────────────────────────
                    tk.Label(cell,
                             text="📡 Signal Only — path disabled",
                             bg=CARD, fg=BLUE, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 3))
                elif reject_reason:
                    tk.Label(cell,
                             text=f"Rejected: {reject_reason}",
                             bg=CARD, fg=RED, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 3))
                else:
                    # Score جيد لكن لا سبب رفض واضح → اعرضه كمرشح
                    tk.Label(cell,
                             text=f"👁 Candidate — Score={raw}/100 (حد={thresh})",
                             bg=CARD, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 3))

            elif raw > 0:
                # ── ④ Score منخفض ─────────────────────────────────────────────
                tk.Label(cell, text=f"🚫 {best_n}",
                         bg=CARD, fg=TEXT_DIM, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8)
                tk.Label(cell,
                         text=f"Score: {raw}/100  <  الحد: {thresh}",
                         bg=CARD, fg=RED, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 2))
            else:
                tk.Label(cell, text="🚫 No Trade",
                         bg=CARD, fg=RED, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(0, 1))
                reject_reason = self._real_rejection(strategy, ticker, analysis)
                detail = self._compact_ui_text(reject_reason or "Score: N/A — no valid candidate / rejected before scoring", 80)
                tk.Label(cell, text=detail,
                         bg=CARD, fg=TEXT_DIM, font=("Segoe UI", 7), wraplength=280, justify="left").pack(anchor="w", padx=8, pady=(0, 4))

            # Legs detail
            if not no_trade_sym:
                self._render_legs_box(cell, strategy, CARD)
                self._render_sigma_delta_box(cell, strategy, data, CARD)

            # Targets
            ups   = [f"${t['price']:,.0f}" for t in (data.get("targets_up") or [])[:2]]
            downs = [f"${t['price']:,.0f}" for t in (data.get("targets_down") or [])[:2]]
            if ups or downs:
                tk.Label(cell,
                         text=("⬆ " + " | ".join(ups) if ups else "") +
                              ("   ⬇ " + " | ".join(downs) if downs else ""),
                         bg=CARD, fg=BLUE, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 6))


    def _show_symbol_cards(self, analysis: dict):
        """عرض آمن لبطاقات SPY / QQQ؛ لا يوقف بقية الصفحة إذا رمز واحد تعطل."""
        def _fmt_price(v):
            try:
                return f"${float(v):,.2f}"
            except Exception:
                return "—"

        def _candidate_for(ticker):
            best = analysis.get("best_opportunity", {}) or {}
            for c in (best.get("all_candidates") or []):
                if str(c.get("symbol", "")).upper() == ticker:
                    return c
            return {}

        row = tk.Frame(self.analysis_frame, bg=BG)
        row.pack(fill="x", pady=(4, 2))

        for key, ticker in [("spy", "SPY"), ("qqq", "QQQ"), ("iwm", "IWM")]:
            cell = tk.Frame(row, bg=CARD)
            cell.pack(side="left", fill="both", expand=True, padx=4, pady=2)
            try:
                data = analysis.get(key) or {}
                cand = _candidate_for(ticker)
                if not data:
                    tk.Label(cell, text=f"{ticker} — لا توجد بيانات تفصيلية", bg=CARD, fg=GOLD,
                             font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
                    if cand:
                        tk.Label(cell, text=f"{cand.get('best_strat', cand.get('strategy','—'))}  {cand.get('score', cand.get('raw_score',0))}/100",
                                 bg=CARD, fg=GOLD, font=("Consolas", 8, "bold")).pack(anchor="w", padx=10, pady=(0, 8))
                    continue

                strategy = data.get("strategy") or {}
                s_name = strategy.get("strategy") or cand.get("best_strat") or cand.get("strategy") or "—"
                if isinstance(s_name, (dict, list)) or len(str(s_name)) > 60 or "{" in str(s_name):
                    s_name = strategy.get("selected_strategy") or strategy.get("trade_strategy") or cand.get("best_strat") or "Candidate"
                s_score = strategy.get("score", cand.get("score", cand.get("raw_score", 0))) or 0
                no_trade = strategy.get("no_trade", False) if strategy else cand.get("no_trade", False)
                score_color = ACCENT if s_score >= 75 else (GOLD if s_score >= 60 else RED)

                h = tk.Frame(cell, bg=CARD)
                h.pack(fill="x", padx=8, pady=(6, 2))
                tk.Label(h, text=f"{ticker}  {_fmt_price(data.get('price'))}", bg=CARD, fg=WHITE,
                         font=("Segoe UI", 13, "bold")).pack(side="left")
                mode = ((data.get("data_quality_report") or {}).get("mode") or "?")
                mode_color = ACCENT if mode == "LIVE" else (GOLD if mode in ("PARTIAL", "LIMITED") else RED)
                tk.Label(h, text=f"● {mode}", bg=CARD, fg=mode_color,
                         font=("Segoe UI", 8, "bold")).pack(side="right")

                pin = data.get("pin_score", 0) or 0
                trend_v = data.get("trend_label", "")
                pin_color = ACCENT if pin >= 70 else (GOLD if pin >= 50 else RED)
                tk.Label(cell, text=f"Pin: {pin}/100  |  {trend_v}", bg=CARD, fg=pin_color,
                         font=("Segoe UI", 9)).pack(anchor="w", padx=8)

                net_gex = data.get("net_gex") or 0
                em = data.get("expected_move")
                iv_rank = data.get("iv_rank")
                parts = [f"Net GEX: {net_gex:,.0f}"]
                if em: parts.append(f"EM: ±{em}")
                if iv_rank is not None:
                    try: parts.append(f"IV Rank: {float(iv_rank):.0f}")
                    except Exception: parts.append(f"IV Rank: {iv_rank}")
                tk.Label(cell, text="  |  ".join(parts), bg=CARD, fg=TEXT_DIM,
                         font=("Segoe UI", 8)).pack(anchor="w", padx=8)

                if strategy:
                    liq_s, liq_c, liq_i = self._liquidity_status(strategy)
                    tk.Label(cell, text=f"Liquidity: {liq_i} {liq_s}", bg=CARD, fg=liq_c,
                             font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8)
                    try:
                        from core.risk_manager import calculate_max_loss_per_contract, get_risk_settings, max_risk_dollar
                        ml = calculate_max_loss_per_contract(strategy, ticker)
                        if ml is not None:
                            mr = max_risk_dollar(get_risk_settings())
                            ok = ml <= mr
                            tk.Label(cell, text=f"Risk: {'✅' if ok else '❌'} MaxLoss ${ml:,.0f}  (حد: ${mr:.0f})",
                                     bg=CARD, fg=(ACCENT if ok else RED), font=("Segoe UI", 8)).pack(anchor="w", padx=8)
                    except Exception:
                        pass

                status_bg = "#0a2a0a" if not no_trade and s_score > 0 else "#2b0d0d"
                q_row = tk.Frame(cell, bg=status_bg)
                q_row.pack(fill="x", padx=8, pady=3)
                status = "✅ Qualified" if not no_trade and s_score > 0 else "🚫 Rejected / Signal"
                tk.Label(q_row, text=status, bg=status_bg, fg=(ACCENT if not no_trade else RED),
                         font=("Segoe UI", 8, "bold")).pack(side="left")
                tk.Label(q_row, text=f"{s_name}  {s_score}/100", bg=status_bg, fg=score_color,
                         font=("Segoe UI", 8, "bold")).pack(side="right")

                if strategy and not no_trade:
                    try:
                        self._render_legs_box(cell, strategy, CARD)
                        self._render_sigma_delta_box(cell, strategy, data, CARD)
                    except Exception:
                        pass

                ups = []
                downs = []
                for t in (data.get("targets_up") or [])[:2]:
                    try: ups.append(f"${float(t.get('price')):,.0f}")
                    except Exception: pass
                for t in (data.get("targets_down") or [])[:2]:
                    try: downs.append(f"${float(t.get('price')):,.0f}")
                    except Exception: pass
                if ups or downs:
                    tk.Label(cell, text=("⬆ " + " | ".join(ups) if ups else "") + ("   ⬇ " + " | ".join(downs) if downs else ""),
                             bg=CARD, fg=BLUE, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0, 6))
            except Exception as e:
                tk.Label(cell, text=f"{ticker} — خطأ عرض البطاقة: {e}", bg=CARD, fg=RED,
                         font=("Segoe UI", 8)).pack(anchor="w", padx=10, pady=8)

    def _show_welcome(self):
        for w in self.analysis_frame.winfo_children():
            w.destroy()

        tk.Label(self.analysis_frame, text="لم يتم تحليل بعد",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 13)).pack(expand=True)
        tk.Label(self.analysis_frame,
                 text="اضغط 'تشغيل تحليل كامل' للبدء",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 10)).pack()

    def _show_analysis_result(self, analysis):
        for w in self.analysis_frame.winfo_children():
            w.destroy()
        try:
            self.analysis_canvas.yview_moveto(0)
        except Exception:
            pass

        if "error" in analysis:
            tk.Label(self.analysis_frame, text=f"❌ {analysis['error']}",
                     bg=BG, fg=RED, font=("Segoe UI", 12)).pack(expand=True)
            return

        # ── Best Opportunity Banner ───────────────────────────────────────────
        best      = analysis.get("best_opportunity", {})
        winner    = best.get("winner")          # None إذا لا يوجد فائز حقيقي
        best_cand = best.get("best_candidate") or {}
        all_cands = best.get("all_candidates", [])
        has_opp   = best.get("has_opportunity", False)

        banner_bg = "#0d2b0d" if has_opp else "#2b0d0d"
        banner = tk.Frame(self.analysis_frame, bg=banner_bg)
        banner.pack(fill="x", pady=(0, 6))

        left_b = tk.Frame(banner, bg=banner_bg)
        left_b.pack(side="left", padx=12, pady=8)

        if has_opp and winner:
            tk.Label(left_b, text="✅ أفضل فرصة حالياً",
                     bg=banner_bg, fg=ACCENT, font=("Segoe UI", 8, "bold")).pack(anchor="w")
            w_txt = f"{winner.get('emoji','')}  {winner.get('symbol','')}  —  {winner.get('best_strat', winner.get('strategy',''))}"
            tk.Label(left_b, text=w_txt,
                     bg=banner_bg, fg=ACCENT, font=("Segoe UI", 13, "bold")).pack(anchor="w")
            # v3.27: نص قرار أوضح + Mode/Expiry/DTE
            mode_txt = winner.get("trade_mode") or winner.get("selected_mode") or winner.get("mode") or "0DTE"
            expiry_txt = winner.get("expiry_date") or winner.get("expiry") or "—"
            dte_txt = winner.get("dte_at_entry", winner.get("dte", "—"))
            decision_txt = winner.get("decision", "")
            if analysis.get("_market_closed"):
                decision_txt = "تحليل فقط — السوق مغلق، لم يتم تسجيل صفقة ورقية"
            tk.Label(left_b, text=decision_txt,
                     bg=banner_bg, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")
            tk.Label(left_b, text=f"Mode: {mode_txt}  |  Expiry: {expiry_txt}  |  DTE: {dte_txt}",
                     bg=banner_bg, fg=BLUE, font=("Segoe UI", 8)).pack(anchor="w")
        else:
            tk.Label(left_b, text="🚫 لا توجد فرصة — جميع الرموز مرفوضة",
                     bg=banner_bg, fg=RED, font=("Segoe UI", 10, "bold")).pack(anchor="w")
            if best_cand:
                raw   = best_cand.get("raw_score", best_cand.get("score", 0))
                b_sym = best_cand.get("symbol", "")
                b_str = best_cand.get("best_strat", best_cand.get("strategy", ""))
                tk.Label(left_b, text=f"أفضل رمز حالياً: {b_sym}  —  {b_str}  ({raw}/100)",
                         bg=banner_bg, fg=GOLD, font=("Segoe UI", 9)).pack(anchor="w")
                try:
                    from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                    req = STRATEGY_MIN_SCORE.get(b_str, MIN_SCORE_TO_TRADE)
                except Exception:
                    req = 50
                tk.Label(left_b, text=f"Final Decision: REJECTED_SCORE {raw} < required {req}",
                         bg=banner_bg, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="w")

        right_b = tk.Frame(banner, bg=banner_bg)
        right_b.pack(side="right", padx=16, pady=8)

        # عرض الـ score: الفائز الحقيقي أو أفضل مرشح
        display_score = (winner.get("score", 0) if winner
                         else best_cand.get("raw_score", best_cand.get("score", 0)))
        score_color = ACCENT if (has_opp and winner) else (GOLD if display_score >= 50 else RED)
        tk.Label(right_b, text=f"{display_score}/100",
                 bg=banner_bg, fg=score_color,
                 font=("Segoe UI", 22, "bold")).pack()

        # زر Why Winner?
        ref_sym   = (winner or best_cand).get("symbol", "")
        ref_strat = (winner or best_cand).get("strategy", "")
        _why_analysis = analysis

        def _show_why(sym=ref_sym, strat=ref_strat, a=_why_analysis):
            self._show_why_winner(sym, strat, a)

        tk.Button(right_b, text="🔍 Why?",
                  bg="#1a2a3a", fg=BLUE,
                  activebackground="#1e3448", activeforeground=BLUE,
                  font=("Segoe UI", 8), relief="flat", cursor="hand2",
                  command=_show_why).pack(pady=(4, 0))

        # جدول مقارنة مصغّر
        if all_cands:
            comp_frame = tk.Frame(banner, bg=banner_bg)
            comp_frame.pack(side="right", padx=20, pady=6)
            winner_sym = winner.get("symbol") if winner else None
            best_sym   = best_cand.get("symbol") if best_cand else None
            for c in all_cands:
                is_real = (not c["no_trade"]) and c["symbol"] == winner_sym
                is_best = c["symbol"] == best_sym and not is_real
                if is_real:
                    marker  = "✅ "
                    c_color = ACCENT
                elif is_best:
                    marker  = "►  "
                    c_color = GOLD
                else:
                    marker  = "   "
                    c_color = TEXT_DIM
                strat_lbl = c.get("best_strat", c["strategy"])[:16]
                score_lbl = c.get("raw_score", c["score"])
                nt_mark   = " ✗" if c["no_trade"] else "  "
                tk.Label(comp_frame,
                         text=f"{marker}{c['symbol']:4}  {strat_lbl:16}  {score_lbl:3}/100{nt_mark}",
                         bg=banner_bg, fg=c_color,
                         font=("Consolas", 8)).pack(anchor="e")

        # Main info card
        top = tk.Frame(self.analysis_frame, bg=CARD)
        top.pack(fill="x", pady=(0, 8))

        pin = analysis["pin_score"]
        pin_color = ACCENT if pin >= 70 else (GOLD if pin >= 50 else RED)

        # مؤشر LIVE/FALLBACK
        # v3.31: split the SPX market header into two rows so long GEX/IV text is not clipped.
        top_row1 = tk.Frame(top, bg=CARD)
        top_row1.pack(fill="x", padx=6, pady=(8, 2))
        top_row2 = tk.Frame(top, bg=CARD)
        top_row2.pack(fill="x", padx=6, pady=(0, 8))

        dq_report = analysis.get("data_quality_report", {})
        mode = dq_report.get("mode", "UNKNOWN")
        quality_pct = dq_report.get("quality_pct", 0)
        mode_color = ACCENT if mode == "LIVE" else (GOLD if mode == "PARTIAL" else RED)
        if mode == "FALLBACK":
            checks = dq_report.get("checks", {}) or {}
            miss = [k for k, v in checks.items() if not v]
            mode_text = f"● FALLBACK {quality_pct}% — missing: {','.join(miss[:3]) or 'unknown'}"
        else:
            mode_text  = f"● {mode}  {quality_pct}%"
        tk.Label(top_row1, text=mode_text, bg=CARD, fg=mode_color,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=6)

        tk.Label(top_row1, text=f"SPX  ${analysis['price']:,.2f}",
                 bg=CARD, fg=WHITE, font=("Segoe UI", 22, "bold"), anchor="w").pack(side="left", padx=18)
        tk.Label(top_row1, text=f"Pin Score: {pin}/100",
                 bg=CARD, fg=pin_color, font=("Segoe UI", 14, "bold"), anchor="w").pack(side="left", padx=10)

        # ── Trade Mode Badge ─────────────────────────────────────────────────
        mode_eval     = analysis.get("trade_mode_eval", {})
        sel_mode      = mode_eval.get("selected_mode", "0DTE")
        score_0dte    = mode_eval.get("score_0dte", 0)
        score_swing   = mode_eval.get("score_swing", 0)
        mode_bg       = BLUE if sel_mode == "0DTE" else ACCENT
        mode_badge_txt = f"  {sel_mode}  {score_0dte}↔{score_swing}  "
        tk.Label(top_row1, text=mode_badge_txt,
                 bg=mode_bg, fg="#000000",
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=8)

        tk.Label(top_row1, text=f"🧲 ${analysis['magnetic']:,}",
                 bg=CARD, fg=BLUE, font=("Segoe UI", 12), anchor="e").pack(side="right", padx=10)
        tk.Label(top_row1, text=analysis["pin_label"],
                 bg=CARD, fg=pin_color, font=("Segoe UI", 10), anchor="e").pack(side="right", padx=8)

        # Net GEX بارز — مع توضيح إذا لا بيانات
        net_gex = analysis.get("net_gex") or 0
        gex_quality = analysis.get("gex_quality", "unavailable")
        gex_color  = ACCENT if net_gex > 0 else (RED if net_gex < 0 else TEXT_DIM)
        gex_sign   = "+" if net_gex > 0 else ""
        quality_icon = "✅" if gex_quality == "real_oi" else ("📊" if gex_quality == "volume_proxy" else ("⚡" if gex_quality == "gamma_proxy" else "❓"))
        gex_reason = analysis.get("gex_near_zero_reason", "")
        reason_suffix = " (توازن)" if gex_reason == "balanced" else (" (لا بيانات)" if gex_reason == "no_data" else "")
        if gex_quality == "unavailable" and net_gex == 0:
            reason_suffix = " (بيانات غير متاحة)"
        gross = analysis.get("gross_gex") or 0
        gross_txt = f" | gross {gross:,.0f}" if gross > 0 else ""
        gex_lbl = tk.Label(top_row2, text=f"Net GEX: {gex_sign}{net_gex:,.0f}{reason_suffix}{gross_txt} {quality_icon}",
                           bg=CARD, fg=gex_color, font=("Segoe UI", 9, "bold"), anchor="w", justify="left")
        gex_lbl.pack(side="left", padx=6)

        # IV Percentile + Regime
        _iv_pct = analysis.get("iv_percentile")
        _iv_reg = analysis.get("iv_regime", "")
        if _iv_pct is not None:
            _reg_color = RED if _iv_reg == "Credit-favored" else (BLUE if _iv_reg == "Debit-favored" else TEXT_DIM)
            tk.Label(top_row2,
                     text=f"IV Pct: {_iv_pct:.0f}% [{_iv_reg}]",
                     bg=CARD, fg=_reg_color,
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(side="left", padx=10)

        # Trend Label
        trend_lbl = analysis.get("trend_label", "")
        if trend_lbl:
            tk.Label(top_row2, text=f"📈 {trend_lbl}",
                     bg=CARD, fg=BLUE, font=("Segoe UI", 9), anchor="w").pack(side="left", padx=8)

        # ── SPY / QQQ بطاقات كاملة تحت SPX ──────────────────────────────────
        self._show_symbol_cards(analysis)

        # Levels grid
        # RC8b: hide dashboard target levels (Call/Put Wall, EM levels) from main dashboard.
        # Details remain available in Why?/diagnostics; keep frame un-packed so legacy code below is harmless.
        grid = tk.Frame(self.analysis_frame, bg=BG)
        # grid.pack(fill="x", pady=(0, 4))

        # Targets UP
        up_frame = tk.Frame(grid, bg=CARD)
        up_frame.pack(side="left", fill="both", expand=True, padx=(0, 2), pady=4)
        tk.Label(up_frame, text="⬆  صعود", bg=CARD, fg=ACCENT,
                 font=("Segoe UI", 11, "bold")).pack(pady=(10, 5), padx=10, anchor="e")
        for t in analysis.get("targets_up", []):
            f = tk.Frame(up_frame, bg=BG3)
            f.pack(fill="x", padx=10, pady=3)
            tk.Label(f, text=(f"${t.get('price',0):,.0f}" if isinstance(t, dict) else str(t)), bg=BG3, fg=WHITE,
                     font=("Segoe UI", 12, "bold")).pack(side="right", padx=10, pady=6)
            tk.Label(f, text=(f"{t.get('pct',0):+.2f}%  |  {t.get('strength','')}" if isinstance(t, dict) else ''),
                     bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=10)
            tk.Label(f, text=" · ".join((t.get('labels') or []) if isinstance(t, dict) else []),
                     bg=BG3, fg=BLUE, font=("Segoe UI", 8)).pack(side="left")
        if not analysis.get("targets_up"):
            tk.Label(up_frame, text="لا توجد أهداف", bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 9)).pack(pady=10)

        # Targets DOWN
        dn_frame = tk.Frame(grid, bg=CARD)
        dn_frame.pack(side="left", fill="both", expand=True, padx=(4, 0), pady=4)
        tk.Label(dn_frame, text="⬇  هبوط", bg=CARD, fg=RED,
                 font=("Segoe UI", 11, "bold")).pack(pady=(10, 5), padx=10, anchor="e")
        for t in analysis.get("targets_down", []):
            f = tk.Frame(dn_frame, bg=BG3)
            f.pack(fill="x", padx=10, pady=3)
            tk.Label(f, text=(f"${t.get('price',0):,.0f}" if isinstance(t, dict) else str(t)), bg=BG3, fg=WHITE,
                     font=("Segoe UI", 12, "bold")).pack(side="right", padx=10, pady=6)
            tk.Label(f, text=(f"{t.get('pct',0):+.2f}%  |  {t.get('strength','')}" if isinstance(t, dict) else ''),
                     bg=BG3, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=10)
            tk.Label(f, text=" · ".join((t.get('labels') or []) if isinstance(t, dict) else []),
                     bg=BG3, fg=BLUE, font=("Segoe UI", 8)).pack(side="left")
        if not analysis.get("targets_down"):
            tk.Label(dn_frame, text="لا توجد أهداف", bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 9)).pack(pady=10)

        # ── Strategy Engine Display ──────────────────────────────────────
        # إذا SPX no_trade → اعرض بيانات الفائز الحقيقي (QQQ أو SPY)
        strategy   = analysis.get("strategy")
        display_sym = "SPX"
        best        = analysis.get("best_opportunity", {})
        winner      = best.get("winner")
        if winner and winner.get("symbol") != "SPX":
            _winner_sym   = winner.get("symbol", "SPX").lower()
            _winner_data  = analysis.get(_winner_sym) or {}
            _winner_strat = _winner_data.get("strategy")
            if _winner_strat and not _winner_strat.get("no_trade"):
                strategy    = _winner_strat
                display_sym = winner.get("symbol", "SPX")
        if strategy:
            s_name    = strategy.get("strategy", "")
            if isinstance(s_name, (dict, list)) or len(str(s_name)) > 60 or "{" in str(s_name):
                s_name = strategy.get("selected_strategy") or strategy.get("trade_strategy") or "Candidate"
            s_score   = strategy.get("score", 0)
            s_emoji   = strategy.get("emoji", "🎯")
            s_decision= strategy.get("decision", "")
            s_reasons = strategy.get("reasons", [])
            s_warnings= strategy.get("warnings", [])
            s_no_trade= strategy.get("no_trade", False)
            all_scores= strategy.get("all_scores", {})

            # Score color
            if s_no_trade:
                s_color = RED
            elif s_score >= 75:
                s_color = ACCENT
            elif s_score >= 65:
                s_color = GOLD
            else:
                s_color = RED

            ic = tk.Frame(self.analysis_frame, bg=CARD)
            ic.pack(fill="x", pady=(6, 4))

            # Header
            header_row = tk.Frame(ic, bg=CARD)
            header_row.pack(fill="x", padx=12, pady=(8, 2))
            tk.Label(header_row, text=f"{s_emoji}  {display_sym}: {s_name}",
                     bg=CARD, fg=WHITE, font=("Segoe UI", 11, "bold")).pack(side="right")
            market_sc = strategy.get("market_score")
            score_txt = f"🧭 Trade: {s_score}/100"
            if market_sc is not None and market_sc != s_score:
                score_txt += f"  |  Market: {market_sc}/100"
            score_txt += f"  |  {s_decision}"
            tk.Label(header_row, text=score_txt,
                     bg=CARD, fg=s_color, font=("Segoe UI", 9, "bold")).pack(side="left")

            # Liquidity + Risk
            liq_s, liq_c, liq_i = self._liquidity_status(strategy)
            info_row = tk.Frame(ic, bg=CARD)
            info_row.pack(fill="x", padx=12, pady=(0, 2))
            tk.Label(info_row, text=f"Liquidity: {liq_i} {liq_s}",
                     bg=CARD, fg=liq_c, font=("Segoe UI", 8, "bold")).pack(side="right", padx=8)
            try:
                from core.risk_manager import calculate_max_loss_per_contract, get_risk_settings, max_risk_dollar
                ml = calculate_max_loss_per_contract(strategy, "SPX")
                if ml is not None:
                    mr = max_risk_dollar(get_risk_settings())
                    risk_ok = ml <= mr
                    tk.Label(info_row,
                             text=f"Risk: {'✅' if risk_ok else '❌'} MaxLoss ${ml:,.0f} (حد: ${mr:.0f})",
                             bg=CARD, fg=ACCENT if risk_ok else RED,
                             font=("Segoe UI", 8)).pack(side="left", padx=8)
            except Exception:
                pass

            # Regime
            regime = strategy.get("regime", "")
            if regime:
                net_gex_lbl = analysis.get("net_gex_label", "")
                trend_lbl   = analysis.get("trend_label", "")
                info_parts  = []
                if regime:     info_parts.append(f"بيئة: {regime}")
                if trend_lbl:  info_parts.append(f"اتجاه: {trend_lbl}")
                if net_gex_lbl: info_parts.append(f"Net GEX: {net_gex_lbl}")
                tk.Label(ic, text="  |  ".join(info_parts),
                         bg=CARD, fg=BLUE, font=("Segoe UI", 8)).pack(anchor="e", padx=12, pady=1)

            # Execution details — legs box
            if not s_no_trade:
                self._render_legs_box(ic, strategy, CARD)
                self._render_sigma_delta_box(ic, strategy, analysis, CARD)
                self._render_smc_box(ic, analysis.get("smc") or strategy.get("smc") or {}, strategy, CARD)
                self._render_demand_supply_box(ic, analysis.get("demand_supply") or strategy.get("demand_supply") or {}, strategy, CARD)
                self._render_smc_0dte_mtf_box(ic, analysis.get("smc_0dte_mtf") or strategy.get("smc_0dte_mtf") or {}, strategy, CARD)

                # جدول تفاصيل الأرجل أُخفي من اللوحة الرئيسية لتقليل الحشو.
                # التفاصيل الكاملة موجودة في Paper Trade detail / Why? / diagnostics.

                # worst-case credit/debit
                cw = strategy.get("credit_worst") or strategy.get("debit_worst")
                if cw:
                    label_txt = f"⚡ Worst-case (bid/ask فعلي): {'Credit' if strategy.get('credit_worst') else 'Debit'} ≈ {cw:.2f}"
                    tk.Label(ic, text=label_txt, bg=CARD, fg=GOLD,
                             font=("Segoe UI", 8, "bold")).pack(anchor="e", padx=12, pady=1)

            # ── حالة SPX: 4 حالات واضحة ─────────────────────────────────────
            if s_no_trade and all_scores:
                best_raw_name  = max(all_scores, key=all_scores.get, default="")
                best_raw_score = all_scores.get(best_raw_name, 0)
                try:
                    from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                    _thresh = STRATEGY_MIN_SCORE.get(best_raw_name, MIN_SCORE_TO_TRADE)
                except Exception:
                    _thresh = 60

                reject_bg  = "#1a0a0a"
                reject_row = tk.Frame(ic, bg=reject_bg)
                reject_row.pack(fill="x", padx=12, pady=(2, 4))

                if best_raw_score >= _thresh:
                    real_reason = self._real_rejection(strategy, "SPX", analysis)
                    tk.Label(reject_row,
                             text=f"Score: {best_raw_score}/100  ✓ (حد: {_thresh})",
                             bg=reject_bg, fg=GOLD, font=("Segoe UI", 8)).pack(anchor="e", padx=6, pady=(3,1))
                    if real_reason:
                        tk.Label(reject_row,
                                 text=f"Rejected: {real_reason}",
                                 bg=reject_bg, fg=RED, font=("Segoe UI", 8, "bold")).pack(anchor="e", padx=6, pady=(1,3))
                    else:
                        tk.Label(reject_row,
                                 text="👁 Candidate — Score جيد، راجع سجل الرفض",
                                 bg=reject_bg, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="e", padx=6, pady=(1,3))
                else:
                    tk.Label(reject_row,
                             text=f"Score: {best_raw_score}/100  <  الحد: {_thresh}  →  Score منخفض",
                             bg=reject_bg, fg=RED, font=("Segoe UI", 8)).pack(anchor="e", padx=6, pady=3)

            # Reasons
            if s_reasons:
                tk.Label(ic, text="📊 " + "  |  ".join(s_reasons[:3]),
                         bg=CARD, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="e", padx=12, pady=1)

            # Warnings
            if s_warnings:
                tk.Label(ic, text="⚠️ " + "  |  ".join(s_warnings[:3]),
                         bg=CARD, fg=GOLD, font=("Segoe UI", 8)).pack(anchor="e", padx=12, pady=1)

            # Score comparison أُخفي من اللوحة الرئيسية لتقليل الحشو. استخدم Why?/Diagnostics للتفاصيل.

        elif analysis.get("iron_condor"):
            # Fallback: Iron Condor القديم
            condor = analysis.get("iron_condor")
            ic = tk.Frame(self.analysis_frame, bg=CARD)
            ic.pack(fill="x", pady=(6, 4))
            score = condor.get("score", 0)
            score_color = ACCENT if score >= 75 else (GOLD if score >= 50 else RED)
            tk.Label(ic, text="🧩 Iron Condor مقترح — يُسجَّل ورقياً إذا انطبقت الشروط", bg=CARD, fg=WHITE,
                     font=("Segoe UI", 11, "bold")).pack(anchor="e", padx=12, pady=(8, 2))
            details = (
                f"Put: Sell {condor['short_put']:,.0f} / Buy {condor['long_put']:,.0f}    |    "
                f"Call: Sell {condor['short_call']:,.0f} / Buy {condor['long_call']:,.0f}    |    "
                f"Credit≈ {condor.get('credit', 0):.2f}    MaxLoss≈ {condor.get('max_loss', 0):.2f}"
            )
            tk.Label(ic, text=details, bg=CARD, fg=TEXT,
                     font=("Segoe UI", 9)).pack(anchor="e", padx=12, pady=2)
            tk.Label(ic, text=f"Score: {score}/100 — {condor.get('decision', '')}",
                     bg=CARD, fg=score_color, font=("Segoe UI", 9, "bold")).pack(anchor="e", padx=12, pady=2)
            warnings = condor.get("warnings") or []
            if warnings:
                tk.Label(ic, text="⚠️ " + " | ".join(warnings[:4]),
                         bg=CARD, fg=GOLD, font=("Segoe UI", 8)).pack(anchor="e", padx=12, pady=(0, 8))

        # Top exposure walls
        def _fmt_top(title, rows):
            if not rows:
                return None
            shown = []
            for r in rows[:3]:
                try:
                    shown.append(f"{r.get('strike'):,.0f}")
                except Exception:
                    pass
            return f"{title}: " + ", ".join(shown) if shown else None

        wall_bits = []
        for title, key in [("Top Call GEX", "top_call_gex"), ("Top Put GEX", "top_put_gex"),
                           ("Top Call OI", "top_call_oi"), ("Top Put OI", "top_put_oi")]:
            val = _fmt_top(title, analysis.get(key) or [])
            if val:
                wall_bits.append(val)
        # RC8b: hide Top GEX/OI wall summary from the main dashboard.
        # Keep the data for diagnostics/Why? only.
        if False and wall_bits:
            wall_frame = tk.Frame(self.analysis_frame, bg=BG)
            wall_frame.pack(fill="x", pady=(2, 2))
            tk.Label(wall_frame, text="  |  ".join(wall_bits), bg=BG, fg=BLUE,
                     font=("Segoe UI", 8, "bold")).pack(anchor="e", padx=10)

        # Expected move / data quality
        em = analysis.get("expected_move")
        dq = analysis.get("data_quality") or {}
        meta = []
        if em:
            em_source = analysis.get("expected_move_source", "")
            em_straddle = analysis.get("levels", {}).get("straddle_mid") or analysis.get("straddle_mid")
            em_str = f"EM≈ ±{em}"
            if em_straddle:
                em_str += f" (straddle={em_straddle:.2f})"
            if em_source:
                em_str += f" [{em_source}]"
            meta.append(em_str)
        if analysis.get("iv_rank") is not None:
            meta.append(f"IV Rank≈ {analysis.get('iv_rank'):.1f}")
        if analysis.get("vix") is not None:
            meta.append(f"VIX≈ {analysis.get('vix'):.2f}")
        if analysis.get("daily_trend"):
            meta.append(f"Daily: {analysis.get('daily_trend')}")
        if analysis.get("intraday_trend"):
            meta.append(f"15m: {analysis.get('intraday_trend')}")
        if analysis.get("trend"):
            meta.append(f"Combined: {analysis.get('trend')}")
        if analysis.get("ema20") is not None:
            meta.append(f"EMA20≈ {analysis.get('ema20'):,.2f}")
        if analysis.get("ema50") is not None:
            meta.append(f"EMA50≈ {analysis.get('ema50'):,.2f}")
        if analysis.get("ema20_15m") is not None:
            meta.append(f"EMA20(15m)≈ {analysis.get('ema20_15m'):,.2f}")
        meta.append(f"Delta: {'متاح' if dq.get('has_delta') else 'غير متاح'}")
        meta.append(f"Gamma: {'متاح' if dq.get('has_gamma') else 'غير متاح'}")
        meta.append(f"Vanna: {'متاح/مقدر' if dq.get('has_vanna') else 'غير متاح'}")
        meta.append(f"Charm: {'متاح/مقدر' if dq.get('has_charm') else 'غير متاح'}")
        if analysis.get("zero_gamma") is not None:
            meta.append(f"Zero Gamma≈ {analysis.get('zero_gamma'):,.0f}")
        if analysis.get("net_gex") is not None:
            meta.append(f"Net GEX≈ {analysis.get('net_gex'):,.0f}")
        if analysis.get("net_dex") is not None:
            meta.append(f"Net DEX≈ {analysis.get('net_dex'):,.0f}")
        if analysis.get("net_vanna") is not None:
            meta.append(f"VannaExp≈ {analysis.get('net_vanna'):,.0f}")
        if analysis.get("net_charm") is not None:
            meta.append(f"CharmExp≈ {analysis.get('net_charm'):,.0f}")
        tk.Label(self.analysis_frame, text="  |  ".join(meta),
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="e", padx=10, pady=2)

        # Timestamp
        tk.Label(self.analysis_frame, text=f"آخر تحديث: {analysis['timestamp']}",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(anchor="e", padx=10, pady=4)

        # ملء المساحة السفلية بتشخيص مفيد بدلاً من تركها فارغة
        self._render_dashboard_recent_activity()

    def _render_dashboard_recent_activity(self):
        """يعرض آخر الصفقات الورقية وآخر الإشارات المرفوضة في الصفحة الرئيسية."""
        wrap = tk.Frame(self.analysis_frame, bg=BG)
        wrap.pack(fill="x", padx=2, pady=(6, 4))

        left = tk.Frame(wrap, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=(0, 4))
        right = tk.Frame(wrap, bg=CARD)
        right.pack(side="left", fill="both", expand=True, padx=(4, 0))

        tk.Label(left, text="📝 آخر Paper Trades", bg=CARD, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="e", padx=10, pady=(8, 3))
        try:
            rows = get_paper_trades(limit=5)
        except Exception:
            rows = []
        if not rows:
            tk.Label(left, text="لا توجد صفقات ورقية حديثة", bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(anchor="e", padx=10, pady=(2, 10))
        else:
            for r in rows[:5]:
                result = r.get("result") or r.get("status") or "open"
                color = ACCENT if str(result).upper() == "WIN" else (RED if str(result).upper() == "LOSS" else GOLD)
                q = r.get("setup_quality") or "—"
                src = r.get("source") or "—"
                txt = (f"#{r.get('id')}  {r.get('symbol')}  {r.get('strategy')}  "
                       f"score={r.get('score',0)}  {result}  | {q} | {src}")
                tk.Label(left, text=txt, bg=CARD, fg=color,
                         font=("Consolas", 8)).pack(anchor="w", padx=10, pady=1)

        tk.Label(right, text="⛔ آخر الإشارات المرفوضة", bg=CARD, fg=RED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="e", padx=10, pady=(8, 3))
        try:
            rej = get_recent_rejections(5)
        except Exception:
            rej = []
        if not rej:
            tk.Label(right, text="لا توجد إشارات مرفوضة حديثة", bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(anchor="e", padx=10, pady=(2, 10))
        else:
            for r in rej[:5]:
                reason = (r.get("reason") or "")[:70]
                raw_score = r.get('score', 0)
                try:
                    score_txt = "N/A" if float(raw_score or 0) == 0 and str(r.get('strategy') or '').lower() in ('none', 'no trade', '') else str(raw_score)
                except Exception:
                    score_txt = str(raw_score)
                txt = f"{r.get('symbol')}  {r.get('strategy')}  score={score_txt}  | {reason}"
                tk.Label(right, text=txt, bg=CARD, fg=TEXT_DIM,
                         font=("Consolas", 8)).pack(anchor="w", padx=10, pady=1)

    # ── Trades Tab ───────────────────────────────────────────
    def _build_strategy_tab(self):
        """تبويب مقارنة الاستراتيجيات"""
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  🎯 الاستراتيجيات  ")

        # Header
        header = tk.Frame(frame, bg=BG2, height=44)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="🔎 SPX — مقارنة الاستراتيجيات",
                 bg=BG2, fg=WHITE, font=("Segoe UI", 10, "bold")).pack(side="left", padx=14, pady=12)
        tk.Label(header, text="هذا التبويب يعرض استراتيجيات SPX فقط  |  للمقارنة مع SPY/QQQ: اضغط Why Winner?",
                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 8)).pack(side="left", padx=4)
        tk.Button(header, text="🔄 تحديث", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._refresh_strategy_tab, padx=12).pack(side="right", padx=10, pady=8)

        # Scrollable content
        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        self.strategy_scroll_frame = tk.Frame(canvas, bg=BG)
        self.strategy_scroll_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        strat_win_id = canvas.create_window((0, 0), window=self.strategy_scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        # تمديد الـ inner frame ليملأ العرض الكامل
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(strat_win_id, width=e.width))
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self.strategy_canvas = canvas
        self._last_strategy_result = None

        # Show placeholder
        tk.Label(self.strategy_scroll_frame,
                 text="شغّل تحليلاً كاملاً لعرض مقارنة الاستراتيجيات",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 12)).pack(expand=True, pady=60)

    def _refresh_strategy_tab(self):
        """تحديث تبويب الاستراتيجيات من آخر تحليل"""
        if self._last_strategy_result:
            self._show_strategy_comparison(self._last_strategy_result)

    def _show_strategy_comparison(self, analysis: dict):
        """عرض مقارنة الاستراتيجيات التفصيلية"""
        self._last_strategy_result = analysis
        if not hasattr(self, "strategy_scroll_frame"):
            return
        frame = self.strategy_scroll_frame

        # Clear
        for w in frame.winfo_children():
            w.destroy()

        strategy = analysis.get("strategy")
        if not strategy:
            tk.Label(frame, text="لا توجد بيانات استراتيجية",
                     bg=BG, fg=TEXT_DIM, font=("Segoe UI", 11)).pack(pady=40)
            return

        price     = analysis.get("price", 0)
        all_scores= strategy.get("all_scores", {})
        winner    = strategy.get("strategy", "")
        regime    = strategy.get("regime", "")

        # ── Summary bar ──────────────────────────────────────────────
        summary = tk.Frame(frame, bg=CARD)
        summary.pack(fill="x", padx=10, pady=(10, 6))

        # Mode / DTE / Expiry
        trade_mode   = strategy.get("trade_mode", "0DTE")
        dte_entry    = strategy.get("dte_at_entry", 0)
        expiry_date  = strategy.get("expiry_date", "")
        score_0dte   = strategy.get("mode_score_0dte", 0)
        score_swing  = strategy.get("mode_score_swing", 0)
        mode_color   = BLUE if trade_mode == "Swing" else TEXT_DIM
        mode_txt     = (f"Mode: {trade_mode}  |  DTE: {dte_entry}  |  Expiry: {expiry_date}"
                        f"  |  0DTE={score_0dte}  Swing={score_swing}")

        tk.Label(summary, text=f"SPX ${price:,.2f}  |  بيئة السوق: {regime}",
                 bg=CARD, fg=BLUE, font=("Segoe UI", 10, "bold")).pack(side="right", padx=14, pady=(10,2))
        tk.Label(summary, text=mode_txt,
                 bg=CARD, fg=mode_color, font=("Segoe UI", 8)).pack(side="right", padx=14, pady=(0,8))
        tk.Label(summary, text=f"الفائز: {strategy.get('emoji','')} {winner}  —  {strategy.get('score',0)}/100",
                 bg=CARD, fg=ACCENT if not strategy.get("no_trade") else RED,
                 font=("Segoe UI", 11, "bold")).pack(side="left", padx=14, pady=10)

        # ── Strategy cards ────────────────────────────────────────────
        strategies_info = [
            ("Iron Condor",      "🦅", "بيع محايد — بيئة عكسية"),
            ("Bull Put Spread",  "🟢", "صاعد — بيع Put"),
            ("Bear Call Spread", "🔴", "هابط — بيع Call"),
            ("Call Debit Spread","📈", "صاعد — شراء Call"),
            ("Put Debit Spread", "📉", "هابط — شراء Put"),
            ("No Trade",         "🚫", "لا تدخل"),
        ]

        # Run fresh scan for detailed info
        try:
            from core.strategy_engine import StrategyEngine
            engine_analysis = {
                "price":          price,
                "pin_score":      analysis.get("pin_score", 0),
                "vix":            analysis.get("levels", {}).get("vix", 0),
                "is_market_open": analysis.get("is_market_open", True),
                "levels":         analysis.get("levels", {}),
                "_chain":         analysis.get("_chain"),
                "data_quality":   analysis.get("data_quality", {}),
            }
            engine = StrategyEngine(engine_analysis)
            detailed = {
                "Iron Condor":       engine.scan_iron_condor(),
                "Bull Put Spread":   engine.scan_bull_put_spread(),
                "Bear Call Spread":  engine.scan_bear_call_spread(),
                "Call Debit Spread": engine.scan_call_debit_spread(),
                "Put Debit Spread":  engine.scan_put_debit_spread(),
            }
        except Exception as e:
            detailed = {}

        for s_name, s_emoji, s_desc in strategies_info:
            if s_name == "No Trade":
                continue

            s_score   = all_scores.get(s_name, 0)
            is_winner = s_name == winner
            detail    = detailed.get(s_name, {})

            # Card background
            card_bg = "#1a2a1a" if is_winner and not strategy.get("no_trade") else CARD
            card = tk.Frame(frame, bg=card_bg, relief="flat")
            card.pack(fill="x", padx=10, pady=4)

            # Score bar
            bar_frame = tk.Frame(card, bg=card_bg)
            bar_frame.pack(fill="x", padx=12, pady=(8, 2))

            from core.strategy_engine import STRATEGY_MIN_SCORE
            s_threshold = STRATEGY_MIN_SCORE.get(s_name, 65)
            score_color = ACCENT if s_score >= 75 else (GOLD if s_score >= s_threshold else (TEXT_DIM if s_score >= 40 else RED))

            tk.Label(bar_frame, text=f"{s_emoji}  {s_name}",
                     bg=card_bg, fg=WHITE if is_winner else TEXT,
                     font=("Segoe UI", 10, "bold" if is_winner else "normal")).pack(side="right")

            # Score visual bar
            bar_container = tk.Frame(bar_frame, bg=BG3, height=8, width=200)
            bar_container.pack(side="left", padx=(0, 8))
            bar_container.pack_propagate(False)
            bar_fill = tk.Frame(bar_container, bg=score_color,
                                height=8, width=max(1, int(s_score * 2)))
            bar_fill.pack(side="right")

            threshold_txt = f"/{s_threshold}" if s_threshold != 65 else ""
            tk.Label(bar_frame, text=f"{s_score}/100{threshold_txt}",
                     bg=card_bg, fg=score_color,
                     font=("Segoe UI", 10, "bold")).pack(side="left", padx=6)

            tk.Label(card, text=s_desc, bg=card_bg, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack(anchor="e", padx=12)

            # Winner badge
            if is_winner and not strategy.get("no_trade"):
                tk.Label(card, text="◀ الاستراتيجية المختارة",
                         bg=card_bg, fg=ACCENT,
                         font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12)

            # Execution details
            if detail and not detail.get("no_trade") and s_score >= 40:
                exec_parts = []
                if s_name == "Iron Condor":
                    sc, lc = detail.get("short_call"), detail.get("long_call")
                    sp, lp = detail.get("short_put"),  detail.get("long_put")
                    if sp and lp: exec_parts.append(f"Put: Sell {sp:,.0f}/Buy {lp:,.0f}")
                    if sc and lc: exec_parts.append(f"Call: Sell {sc:,.0f}/Buy {lc:,.0f}")
                elif s_name == "Bull Put Spread":
                    sp, lp = detail.get("short_put"), detail.get("long_put")
                    if sp and lp: exec_parts.append(f"Sell {sp:,.0f}/Buy {lp:,.0f} Put")
                elif s_name == "Bear Call Spread":
                    sc, lc = detail.get("short_call"), detail.get("long_call")
                    if sc and lc: exec_parts.append(f"Sell {sc:,.0f}/Buy {lc:,.0f} Call")
                elif s_name in ("Call Debit Spread", "Put Debit Spread"):
                    lc = detail.get("long_call") or detail.get("long_put")
                    sc = detail.get("short_call") or detail.get("short_put")
                    if lc and sc: exec_parts.append(f"Buy {lc:,.0f}/Sell {sc:,.0f}")

                credit = detail.get("credit")
                debit  = detail.get("debit")
                ml     = detail.get("max_loss")
                pop    = detail.get("pop")
                if credit: exec_parts.append(f"Credit: {credit:.2f}")
                if debit:  exec_parts.append(f"Debit: {debit:.2f}")
                if ml:     exec_parts.append(f"MaxLoss: {ml:.2f}")
                if pop:    exec_parts.append(f"POP: {pop:.0f}%")

                if exec_parts:
                    tk.Label(card, text="  |  ".join(exec_parts),
                             bg=card_bg, fg=BLUE, font=("Segoe UI", 8)).pack(anchor="e", padx=12, pady=1)

            # Score Breakdown
            breakdown = detail.get("score_breakdown", {})
            if breakdown:
                parts = []
                for k, v in breakdown.items():
                    if k.endswith("_note"):  # تجاهل الملاحظات التشخيصية
                        continue
                    try:
                        num = float(v)
                        if num != 0:
                            sign = "+" if num > 0 else ""
                            parts.append(f"{sign}{int(num)} {k}")
                    except (TypeError, ValueError):
                        pass  # تجاهل القيم غير الرقمية
                if parts:
                    tk.Label(card, text="  ".join(parts),
                             bg=card_bg, fg=BLUE, font=("Consolas", 7)).pack(anchor="e", padx=12, pady=1)

            # Reasons & Warnings
            reasons  = detail.get("reasons", [])
            warnings = detail.get("warnings", [])
            if reasons:
                tk.Label(card, text="📊 " + "  |  ".join(reasons[:2]),
                         bg=card_bg, fg=TEXT_DIM, font=("Segoe UI", 7)).pack(anchor="e", padx=12, pady=1)
            if warnings:
                tk.Label(card, text="⚠️ " + "  |  ".join(warnings[:2]),
                         bg=card_bg, fg=GOLD, font=("Segoe UI", 7)).pack(anchor="e", padx=12, pady=(0,6))
            else:
                tk.Frame(card, bg=card_bg, height=4).pack()

        # No Trade card if winner
        if strategy.get("no_trade"):
            nt = tk.Frame(frame, bg="#2a1a1a")
            nt.pack(fill="x", padx=10, pady=4)
            tk.Label(nt, text="🚫  SPX: لا تدخل — السوق لا يوفر أفضلية إحصائية",
                     bg="#2a1a1a", fg=RED, font=("Segoe UI", 11, "bold")).pack(anchor="e", padx=12, pady=(10,4))
            all_sc = strategy.get("all_scores", {})
            if all_sc:
                best = max(all_sc, key=all_sc.get)
                try:
                    from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                    thresh = STRATEGY_MIN_SCORE.get(best, MIN_SCORE_TO_TRADE)
                except Exception:
                    thresh = 60
                tk.Label(nt, text=f"  أعلى درجة SPX: {best} = {all_sc[best]}/100  (الحد: {thresh})",
                         bg="#2a1a1a", fg=GOLD, font=("Segoe UI", 9)).pack(anchor="e", padx=20, pady=2)
            for r in strategy.get("reasons", [])[:3]:
                tk.Label(nt, text=f"  • {r}", bg="#2a1a1a", fg=TEXT_DIM,
                         font=("Segoe UI", 9)).pack(anchor="e", padx=20, pady=1)
            tk.Frame(nt, bg="#2a1a1a", height=4).pack()

        # ── مقارنة سريعة SPY/QQQ أسفل الصفحة ──────────────────────────────
        spy_strat = (analysis.get("spy") or {}).get("strategy") or {}
        qqq_strat = (analysis.get("qqq") or {}).get("strategy") or {}
        iwm_strat = (analysis.get("iwm") or {}).get("strategy") or {}
        comp_items = [("SPY", spy_strat, analysis.get("spy") or {}),
                      ("QQQ", qqq_strat, analysis.get("qqq") or {}),
                      ("IWM", iwm_strat, analysis.get("iwm") or {})]
        has_comp = any(s for _, s, _ in comp_items)
        if has_comp:
            sep = tk.Frame(frame, bg=BORDER, height=1)
            sep.pack(fill="x", padx=10, pady=(8, 4))
            tk.Label(frame, text="📡 مقارنة سريعة — SPY / QQQ / IWM  (Why Winner? للتفصيل)",
                     bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8, "bold")).pack(anchor="e", padx=14, pady=(0, 4))
            row_comp = tk.Frame(frame, bg=BG)
            row_comp.pack(fill="x", padx=10, pady=(0, 8))
            for sym, s, d in comp_items:
                if not s:
                    continue
                nt_sym  = s.get("no_trade", True)
                sc_sym  = s.get("score", 0)
                all_sym = s.get("all_scores", {})
                raw_sym = max(all_sym.values(), default=0) if nt_sym and all_sym else sc_sym
                nm_sym  = max(all_sym, key=all_sym.get, default="") if nt_sym and all_sym else s.get("strategy","")
                try:
                    from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                    thr_sym = STRATEGY_MIN_SCORE.get(nm_sym, MIN_SCORE_TO_TRADE)
                except Exception:
                    thr_sym = 60
                cc = tk.Frame(row_comp, bg=CARD)
                cc.pack(side="left", padx=4, ipadx=10, ipady=6, fill="x", expand=True)
                tk.Label(cc, text=f"{sym}  ${d.get('price',0):,.2f}" if d.get("price") else sym,
                         bg=CARD, fg=WHITE, font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=8, pady=(4,0))
                if nt_sym:
                    tk.Label(cc, text=f"🚫 {nm_sym}  {raw_sym}/100  (الحد: {thr_sym})",
                             bg=CARD, fg=RED, font=("Segoe UI", 8)).pack(anchor="w", padx=8, pady=(0,4))
                else:
                    sc_c = ACCENT if sc_sym >= 75 else (GOLD if sc_sym >= 60 else RED)
                    tk.Label(cc, text=f"✅ {s.get('strategy','')}  {sc_sym}/100",
                             bg=CARD, fg=sc_c, font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=8, pady=(0,4))

    def _build_trades_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  📋 الصفقات  ")

        # Toolbar
        toolbar = tk.Frame(frame, bg=BG2, height=48)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        tk.Button(toolbar, text="＋ تسجيل صفقة", bg=GOLD, fg="#000",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  command=self._open_trade_dialog, padx=12).pack(side="right", padx=10, pady=10)
        tk.Button(toolbar, text="📥 تصدير Excel", bg=ACCENT, fg="#000",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  command=self._export_excel, padx=12).pack(side="right", padx=4, pady=10)
        tk.Button(toolbar, text="🔄 تحديث", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._refresh_trades, padx=12).pack(side="right", padx=4, pady=10)
        tk.Button(toolbar, text="🗑 حذف محدد", bg=RED, fg=WHITE,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._delete_selected_trade, padx=12).pack(side="right", padx=4, pady=10)

        tk.Label(toolbar, text="سجل الصفقات", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=14)

        # Tree
        cols = ("#", "التاريخ", "الوقت", "الاستراتيجية", "النوع", "النتيجة", "R المبلغ", "المبلغ $", "ملاحظات")
        tree_frame = tk.Frame(frame, bg=BG)
        tree_frame.pack(fill="both", expand=True, padx=10, pady=8)

        scroll_y = ttk.Scrollbar(tree_frame, orient="vertical")
        scroll_y.pack(side="right", fill="y")

        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings",
                                  yscrollcommand=scroll_y.set)
        scroll_y.config(command=self.tree.yview)

        widths = [40, 100, 70, 130, 90, 80, 90, 100, 200]
        for col, w in zip(cols, widths):
            self.tree.heading(col, text=col, anchor="center")
            self.tree.column(col, width=w, anchor="center")

        self.tree.tag_configure("win",     background="#1a3a2a", foreground=ACCENT)
        self.tree.tag_configure("partial", background="#1a2a1a", foreground=GOLD)
        self.tree.tag_configure("loss",    background="#3a1a1a", foreground=RED)
        self.tree.pack(fill="both", expand=True)

        self._refresh_trades()

    def _refresh_trades(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        trades = get_all_trades()
        for t in trades:
            result = t["result"]
            if result == "ربح":
                tag = "win"
            elif result in ("ربح جزئي", "تعادل"):
                tag = "partial"
            else:
                tag = "loss"
            r_str = f"{t['r_value']:+.2f}R"
            amt_str = f"${t['amount']:,.2f}"
            self.tree.insert("", "end", iid=t["id"], tags=(tag,),
                             values=(t["id"], t["date"], t["time"],
                                     t.get("strategy","Iron Condor"), t.get("type","MANUAL"),
                                     t["result"], r_str, amt_str, t.get("notes","")))

    def _delete_selected_trade(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "يرجى تحديد صفقة للحذف")
            return
        if messagebox.askyesno("تأكيد الحذف", f"هل تريد حذف {len(selected)} صفقة؟"):
            for item in selected:
                delete_trade(int(item))
            self._refresh_trades()
            self._refresh_stats()

    def _export_excel(self):
        from core.exporter import export_trades_to_excel  # RC15i.4: lazy
        ok, result = export_trades_to_excel()
        if ok:
            messagebox.showinfo("تم التصدير", f"✅ تم حفظ الملف:\n{result}")
        else:
            messagebox.showerror("خطأ", result)

    # ── Auto Trades Tab ──────────────────────────────────────
    def _on_trade_click(self, event):
        tree = event.widget
        sel  = tree.selection()
        if not sel:
            return
        values = tree.item(sel[0], "values")
        if not values:
            return
        try:
            # استخدم النافذة الموسّعة الجديدة
            trade_id = int(values[0])
            from core.database import get_all_open_trades
            import json as _json
            all_t = get_all_open_trades()
            t = next((x for x in all_t if x["id"] == trade_id), None)
            if not t:
                t = get_trade_by_id(trade_id)
            if t:
                legs = {}
                try:
                    legs = _json.loads(t.get("legs_json") or "{}")
                except Exception:
                    pass
                # استدعِ نافذة التفاصيل الموسّعة مباشرة
                self._show_full_trade_detail(t, legs)
        except (ValueError, IndexError):
            pass

    def _show_trade_detail(self, trade_id: int):
        import json
        t = get_trade_by_id(trade_id)
        if not t:
            return

        win = tk.Toplevel(self.root)
        win.title(f"تفاصيل الصفقة #{trade_id}")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.grab_set()

        def row(parent, label, value, val_color=TEXT):
            f = tk.Frame(parent, bg=BG2)
            f.pack(fill="x", padx=16, pady=2)
            tk.Label(f, text=label, bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 9), width=18, anchor="e").pack(side="left")
            tk.Label(f, text=str(value) if value not in (None, "") else "—",
                     bg=BG2, fg=val_color,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=8)

        is_win  = t.get("result") == "WIN"
        is_loss = t.get("result") == "LOSS"
        hdr_color = ACCENT if is_win else (RED if is_loss else GOLD)
        tk.Label(win, text=f"  #{trade_id}  {t.get('symbol','')}  —  {t.get('strategy','')}  ",
                 bg=hdr_color, fg="#000000" if is_win else TEXT,
                 font=("Segoe UI", 11, "bold")).pack(fill="x", pady=(0, 8))

        # الأرجل
        tk.Label(win, text="الأرجل", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(4, 0))
        try:
            legs = json.loads(t.get("legs_json") or "[]")
            for leg in legs:
                name   = leg.get("name") or leg.get("type", "")
                strike = leg.get("strike", "")
                bid    = leg.get("bid", "—")
                ask    = leg.get("ask", "—")
                tk.Label(win, text=f"  {name}  Strike: {strike}   Bid: {bid}  Ask: {ask}",
                         bg=BG2, fg=TEXT, font=("Segoe UI", 9)).pack(fill="x", padx=16, pady=1)
        except Exception:
            tk.Label(win, text=f"  {t.get('legs_json','—')}",
                     bg=BG2, fg=TEXT, font=("Segoe UI", 9)).pack(fill="x", padx=16, pady=1)

        tk.Frame(win, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)

        # الدخول
        tk.Label(win, text="الدخول", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(0, 2))
        row(win, "تاريخ الدخول",   f"{t.get('entry_date','')} {t.get('entry_time','')[:5]}")
        row(win, "سعر الدخول",     f"${t['entry_price']:,.2f}" if t.get("entry_price") else "—")
        row(win, "Credit / Debit", f"{t['credit_debit']:.2f}" if t.get("credit_debit") is not None else "—")
        row(win, "Target (50%)",   f"{t['target']:.2f}" if t.get("target") is not None else "—")
        row(win, "Score",          f"{t.get('score', 0)}/100")

        tk.Frame(win, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)

        # الخروج
        tk.Label(win, text="الخروج", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(0, 2))
        row(win, "تاريخ الخروج",
            f"{t.get('exit_date','')} {(t.get('exit_time') or '')[:5]}" if t.get("exit_date") else "—")
        row(win, "سعر الخروج",
            f"{t['exit_price']:.2f}" if t.get("exit_price") is not None else "—")

        tk.Frame(win, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)

        # الربح / الخسارة
        tk.Label(win, text="الربح / الخسارة", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(0, 2))

        pnl_pct = t.get("profit_pct")
        if pnl_pct is not None:
            row(win, "P&L %", f"{pnl_pct:+.1f}%",
                val_color=ACCENT if pnl_pct >= 0 else RED)

        try:
            credit = float(t.get("credit_debit") or 0)
            exit_p = float(t.get("exit_price") or 0)
            if t.get("is_credit", 1) and credit and exit_p:
                pnl_dollar = round((credit - exit_p) * 100, 2)
                row(win, "P&L $", f"${pnl_dollar:+,.2f}",
                    val_color=ACCENT if pnl_dollar >= 0 else RED)
        except Exception:
            pass

        row(win, "النتيجة", t.get("result", "—"),
            val_color=ACCENT if is_win else (RED if is_loss else GOLD))
        row(win, "سبب الإغلاق", t.get("close_reason") or "—")

        btn_frame = tk.Frame(win, bg=BG)
        btn_frame.pack(pady=12)

        # زر الإغلاق اليدوي — يظهر فقط للصفقات المفتوحة
        if t.get("status", "open") == "open":
            def _do_manual_close():
                import tkinter.simpledialog as sd
                current_lbl = t.get("current_value", "")
                exit_str = sd.askstring(
                    "إغلاق يدوي",
                    f"أدخل سعر الخروج\n(اتركه فارغاً للسعر الحالي: {current_lbl}):",
                    parent=win,
                )
                if exit_str is None:
                    return
                try:
                    exit_p = float(exit_str.strip()) if exit_str.strip() else float(str(current_lbl) or 0)
                except ValueError:
                    return
                entry_val = float(t.get("credit_debit") or 0)
                is_cr     = bool(t.get("is_credit", 1))
                profit_pct = round(
                    (entry_val - exit_p) / entry_val * 100 if is_cr
                    else (exit_p - entry_val) / entry_val * 100, 1
                ) if entry_val else 0.0
                result_lbl = ("WIN" if profit_pct >= 50
                              else "PARTIAL WIN" if profit_pct > 0
                              else "BREAKEVEN" if profit_pct >= -5
                              else "LOSS")
                from core.database import close_open_trade, balance_close_trade, get_setting
                close_open_trade(trade_id, exit_p, result_lbl, profit_pct, "إغلاق يدوي")
                try:
                    pnl_dollar = round(
                        (entry_val - exit_p if is_cr else exit_p - entry_val) * 100, 2)
                    sym_ = t.get("symbol", "")
                    strat_ = t.get("strategy", "")
                    wing = float(get_setting(
                        "wing_width_spx" if sym_ == "SPX"
                        else f"wing_width_{sym_.lower()}", "5") or 5)
                    balance_close_trade(trade_id, sym_, strat_, wing, pnl_dollar)
                except Exception as _be:
                    print(f"[balance] manual close error: {_be}")
                win.destroy()
                self._refresh_auto_trades()
                self._refresh_stats()

            tk.Button(btn_frame, text="🔴 إغلاق يدوي", bg="#3a1a1a", fg=RED,
                      font=("Segoe UI", 10, "bold"), relief="flat", cursor="hand2",
                      padx=20, command=_do_manual_close).pack(side="left", padx=8)

        tk.Button(btn_frame, text="إغلاق النافذة", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  padx=20, command=win.destroy).pack(side="left", padx=8)

    def _build_auto_trades_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  📋 Legacy Monitor  ")

        toolbar = tk.Frame(frame, bg=BG2, height=48)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(toolbar, text="مراقبة الصفقات الورقية — تسجيل وإغلاق تجريبي",
                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=14)
        tk.Button(toolbar, text="🔄 تحديث", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._refresh_auto_trades, padx=12).pack(side="right", padx=10, pady=10)

        # ── Watchdog Bar ──────────────────────────────────────────────────────
        wd_bar = tk.Frame(frame, bg="#0a0a1a", height=28)
        wd_bar.pack(fill="x")
        wd_bar.pack_propagate(False)
        self._wd_status_lbl = tk.Label(
            wd_bar, text="⏱ Watchdog: جاري التهيئة...",
            bg="#0a0a1a", fg=TEXT_DIM, font=("Segoe UI", 8))
        self._wd_status_lbl.pack(side="left", padx=10, pady=4)
        self._wd_err_lbl = tk.Label(
            wd_bar, text="",
            bg="#0a0a1a", fg=RED, font=("Segoe UI", 8))
        self._wd_err_lbl.pack(side="right", padx=10, pady=4)

        frame = self._scrollable_body(frame)

        # ── شريط الرصيد ───────────────────────────────────────────────────────
        balance_bar = tk.Frame(frame, bg=CARD)
        balance_bar.pack(fill="x", padx=10, pady=(10, 4))

        for col_key, col_label, col_color in [
            ("bal_initial",  "الرصيد الابتدائي", TEXT_DIM),
            ("bal_available","الرصيد المتاح",    ACCENT),
            ("bal_reserved", "محجوز Margin",      GOLD),
            ("bal_pnl",      "P&L الكلي",         ACCENT),
        ]:
            col = tk.Frame(balance_bar, bg=CARD)
            col.pack(side="right", padx=16, pady=8, expand=True)
            tk.Label(col, text=col_label, bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 8)).pack()
            lbl = tk.Label(col, text="$—", bg=CARD, fg=col_color,
                           font=("Segoe UI", 13, "bold"))
            lbl.pack()
            if not hasattr(self, "balance_labels"):
                self.balance_labels = {}
            self.balance_labels[col_key] = lbl

        # ── شريط إجمالي المخاطرة ─────────────────────────────────────────────
        risk_bar = tk.Frame(frame, bg=BG2)
        risk_bar.pack(fill="x", padx=10, pady=(0, 6))
        self.risk_labels = {}
        for rk, rl, rc in [
            ("risk_0dte",   "Risk 0DTE",    RED),
            ("risk_swing",  "Risk Swing",   GOLD),
            ("risk_total",  "Total Risk",   "#ff8c00"),
            ("trades_0dte", "0DTE Open",    ACCENT),
            ("trades_swing","Swing Open",   ACCENT),
        ]:
            c = tk.Frame(risk_bar, bg=BG2)
            c.pack(side="right", padx=12, pady=6, expand=True)
            tk.Label(c, text=rl, bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 7)).pack()
            lbl = tk.Label(c, text="—", bg=BG2, fg=rc,
                           font=("Segoe UI", 10, "bold"))
            lbl.pack()
            self.risk_labels[rk] = lbl

        # Open trades
        # عداد الصفقات المفتوحة
        self.open_counter = tk.Label(frame, text="", bg=BG, fg=ACCENT,
                                     font=("Segoe UI", 10, "bold"))
        self.open_counter.pack(anchor="w", padx=14, pady=(6, 2))
        open_hdr = tk.Frame(frame, bg=BG)
        open_hdr.pack(fill="x", padx=10, pady=(0, 2))
        tk.Label(open_hdr, text="📂 صفقات مفتوحة", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 10, "bold")).pack(side="right")
        tk.Button(open_hdr, text="🔴 إغلاق يدوي", bg="#3a1a1a", fg=RED,
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  command=self._manual_close_trade).pack(side="left", padx=4)

        # RC15j — جدول مبسّط: الأعمدة الأساسية فقط. التفاصيل بـ Double-click
        open_cols = ("ID", "الوقت", "الرمز", "Mode", "الاستراتيجية",
                     "DTE", "Entry", "Current", "P&L%", "P&L$", "Exit Trigger", "LastCh", "Score")
        open_frame = tk.Frame(frame, bg=BG)
        open_frame.pack(fill="x", padx=10)
        self.open_trades_tree = ttk.Treeview(open_frame, columns=open_cols,
                                              show="headings", height=6)
        for col, w in zip(open_cols, [38, 68, 52, 50, 130, 35, 58, 58, 55, 58, 90, 60, 45]):
            self.open_trades_tree.heading(col, text=col, anchor="center")
            self.open_trades_tree.column(col, width=w, anchor="center")
        self.open_trades_tree.tag_configure("profit",    foreground=ACCENT)
        self.open_trades_tree.tag_configure("loss_open", foreground=RED)
        self.open_trades_tree.tag_configure("neutral",   foreground=TEXT)
        self.open_trades_tree.pack(fill="x")
        self.open_trades_tree.bind("<Double-1>", self._on_open_trade_double_click)

        # Closed trades
        tk.Label(frame, text="📋 سجل الصفقات المغلقة", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=14, pady=(12, 2))
        closed_cols = ("ID", "الرمز", "الاستراتيجية", "Entry", "Exit",
                       "النتيجة", "P&L%", "السبب", "التاريخ")
        closed_frame = tk.Frame(frame, bg=BG)
        closed_frame.pack(fill="x", padx=10)
        scroll = ttk.Scrollbar(closed_frame, orient="vertical")
        scroll.pack(side="right", fill="y")
        self.closed_trades_tree = ttk.Treeview(closed_frame, columns=closed_cols,
                                                show="headings", height=10,
                                                yscrollcommand=scroll.set)
        scroll.config(command=self.closed_trades_tree.yview)
        for col, w in zip(closed_cols, [40, 60, 160, 70, 70, 70, 70, 180, 120]):
            self.closed_trades_tree.heading(col, text=col, anchor="center")
            self.closed_trades_tree.column(col, width=w, anchor="center")
        self.closed_trades_tree.tag_configure("win",     background="#1a3a2a", foreground=ACCENT)
        self.closed_trades_tree.tag_configure("partial", background="#1a2a1a", foreground=GOLD)
        self.closed_trades_tree.tag_configure("even",    background="#1a1a2a", foreground=BLUE)
        self.closed_trades_tree.tag_configure("loss",    background="#3a1a1a", foreground=RED)
        self.closed_trades_tree.pack(fill="x")
        self.closed_trades_tree.bind("<Double-1>", self._on_open_trade_double_click)

        # جدول أسباب الرفض
        tk.Label(frame, text="🚫 آخر الإشارات المرفوضة", bg=BG, fg=RED,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        rej_cols = ("الوقت", "الرمز", "الاستراتيجية", "Score", "✓/✗ حد", "سبب الرفض")
        rej_frame = tk.Frame(frame, bg=BG)
        rej_frame.pack(fill="x", padx=10, pady=(0, 8))
        self.rejection_tree = ttk.Treeview(rej_frame, columns=rej_cols,
                                            show="headings", height=5)
        for col, w in zip(rej_cols, [120, 55, 140, 55, 70, 320]):
            self.rejection_tree.heading(col, text=col, anchor="center")
            self.rejection_tree.column(col, width=w, anchor="center")
        self.rejection_tree.tag_configure("above_thresh", foreground=GOLD)
        self.rejection_tree.tag_configure("below_thresh", foreground=RED)
        self.rejection_tree.pack(fill="x")

        # ── لوحة أداء الـ Mode ────────────────────────────────────────────────
        tk.Label(frame, text="📊 أداء كل Mode", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(12, 2))

        perf_frame = tk.Frame(frame, bg=BG)
        perf_frame.pack(fill="x", padx=10, pady=(0, 10))

        perf_cols = ("Mode", "الصفقات", "Win Rate", "Avg Win%", "Avg Loss%",
                     "Profit Factor", "Expectancy")
        self.perf_tree = ttk.Treeview(perf_frame, columns=perf_cols,
                                       show="headings", height=3)
        for col, w in zip(perf_cols, [70, 70, 80, 80, 80, 100, 90]):
            self.perf_tree.heading(col, text=col, anchor="center")
            self.perf_tree.column(col, width=w, anchor="center")
        self.perf_tree.tag_configure("0dte",  background="#1a2a3a", foreground=BLUE)
        self.perf_tree.tag_configure("swing", background="#1a3a2a", foreground=ACCENT)
        self.perf_tree.pack(fill="x")

        self._refresh_auto_trades()

    @staticmethod
    def _legs_compact(legs_json: str) -> str:
        """يحول legs_json إلى نص مختصر للجدول."""
        try:
            import json
            legs = json.loads(legs_json or "{}")
            parts = []
            if legs.get("short_put"):  parts.append(f"SP{legs['short_put']:,.0f}")
            if legs.get("long_put"):   parts.append(f"LP{legs['long_put']:,.0f}")
            if legs.get("short_call"): parts.append(f"SC{legs['short_call']:,.0f}")
            if legs.get("long_call"):  parts.append(f"LC{legs['long_call']:,.0f}")
            if legs.get("long_call") and not legs.get("short_put"):
                parts = []
                if legs.get("long_call"):  parts.append(f"BuyC{legs['long_call']:,.0f}")
                if legs.get("short_call"): parts.append(f"SellC{legs['short_call']:,.0f}")
            if legs.get("long_put") and not legs.get("short_call"):
                parts = []
                if legs.get("long_put"):   parts.append(f"BuyP{legs['long_put']:,.0f}")
                if legs.get("short_put"):  parts.append(f"SellP{legs['short_put']:,.0f}")
            return " / ".join(parts) if parts else "—"
        except Exception:
            return "—"

    def _manual_close_trade(self):
        """إغلاق يدوي للصفقة المحددة في جدول المفتوحة."""
        import tkinter.simpledialog as sd
        import tkinter.messagebox as mb
        sel = self.open_trades_tree.selection()
        if not sel:
            mb.showwarning("تنبيه", "حدد صفقة من الجدول أولاً ثم اضغط إغلاق يدوي")
            return
        try:
            vals     = self.open_trades_tree.item(sel[0])["values"]
            trade_id = int(vals[0])
            # RC15j: أعمدة جديدة → ID=0 الوقت=1 الرمز=2 Mode=3 الاستراتيجية=4 DTE=5 Entry=6 Current=7
            symbol   = str(vals[2])
            strategy = str(vals[4])
            current  = vals[7]
        except Exception:
            mb.showwarning("خطأ", "تعذّر قراءة بيانات الصفقة")
            return

        exit_price_str = sd.askstring(
            "إغلاق يدوي",
            f"إغلاق {symbol} — {strategy}\n\n"
            f"السعر الحالي: {current}\n\n"
            f"أدخل سعر الخروج (اتركه فارغاً لاستخدام السعر الحالي):",
        )
        if exit_price_str is None:
            return

        try:
            exit_price = (float(exit_price_str.strip())
                          if exit_price_str.strip()
                          else float(str(current).replace(",", "") or 0))
        except ValueError:
            mb.showwarning("خطأ", "سعر الخروج غير صالح")
            return

        try:
            from core.database import get_trade_by_id, close_open_trade, balance_close_trade, save_trade
            t = get_trade_by_id(trade_id)
            if not t:
                mb.showwarning("خطأ", f"لم يُعثر على الصفقة #{trade_id}")
                return
            entry_val = float(t.get("credit_debit") or 0)
            is_credit = bool(t.get("is_credit", 1))
            if entry_val > 0:
                profit_pct = round(
                    (entry_val - exit_price) / entry_val * 100 if is_credit
                    else (exit_price - entry_val) / entry_val * 100, 1)
            else:
                profit_pct = 0.0
            result = ("WIN" if profit_pct >= 50
                      else "PARTIAL WIN" if profit_pct > 0
                      else "BREAKEVEN" if profit_pct >= -5
                      else "LOSS")
            close_open_trade(trade_id, exit_price, result, profit_pct, "إغلاق يدوي")
            # نسخ الإغلاق اليدوي إلى سجل الصفقات العام/Backtest كـ AUTO_PAPER.
            # هذا خاص بمسار Legacy Monitor، أما Paper Trades الرسمي فيستخدم close_paper_trade().
            try:
                from datetime import datetime as _dt
                res_ar = {"WIN": "ربح", "PARTIAL WIN": "ربح جزئي",
                          "BREAKEVEN": "تعادل", "LOSS": "خسارة"}.get(result, "خسارة")
                pnl_dollar_for_log = round(
                    (entry_val - exit_price if is_credit else exit_price - entry_val) * 100, 2)
                risk_base = max(float(t.get("target") or t.get("credit_debit") or 1) * 100.0, 1.0)
                r_signed = round(pnl_dollar_for_log / risk_base, 2)
                _now = _dt.now()
                save_trade(
                    date=_now.strftime("%Y-%m-%d"),
                    time_str=_now.strftime("%H:%M"),
                    result=res_ar,
                    r_value=r_signed,
                    amount=abs(pnl_dollar_for_log),
                    notes=(f"ManualPaperClose | {symbol} {strategy} "
                           f"| entry={entry_val:.2f} exit={exit_price:.2f} "
                           f"| {profit_pct:+.1f}% | إغلاق يدوي | OpenTradeID={trade_id}"),
                    spx_price=t.get("entry_price", 0),
                    strategy=strategy,
                    trade_type="AUTO_PAPER",
                )
            except Exception as _log_e:
                print(f"[manual_close_to_trades_log] {_log_e}")
            # تحرير الـ Margin المحجوز
            try:
                from core.database import get_setting as _gs
                pnl_dollar = round(
                    (entry_val - exit_price if is_credit else exit_price - entry_val) * 100, 2)
                wing = float(_gs(
                    "wing_width_spx" if symbol == "SPX"
                    else f"wing_width_{symbol.lower()}", "5") or 5)
                balance_close_trade(trade_id, symbol, strategy, wing, pnl_dollar)
            except Exception as _be:
                print(f"[balance] manual close error: {_be}")
            mb.showinfo("تم", f"صفقة #{trade_id} أُغلقت\n{result} | {profit_pct:+.1f}%")
            self._refresh_auto_trades()
            if hasattr(self, "_refresh_trades"):
                self._refresh_trades()
            if hasattr(self, "_refresh_backtest"):
                self._refresh_backtest()
            self._refresh_stats()
        except Exception as e:
            mb.showerror("خطأ", f"فشل الإغلاق: {e}")

    def _on_open_trade_double_click(self, event):
        """نافذة تفاصيل الصفقة عند الضغط — يعمل مع المفتوحة والمغلقة."""
        tree = event.widget
        sel  = tree.selection()
        if not sel:
            return
        try:
            trade_id = int(tree.item(sel[0])["values"][0])
        except (ValueError, IndexError):
            return
        try:
            from core.database import get_all_open_trades
            import json
            all_t = get_all_open_trades()
            t = next((x for x in all_t if x["id"] == trade_id), None)
            if not t:
                return
            legs = {}
            try:
                legs = json.loads(t.get("legs_json") or "{}")
            except Exception:
                pass
            self._show_full_trade_detail(t, legs)
        except Exception:
            pass

    def _show_full_trade_detail(self, t: dict, legs: dict):
        """النافذة الموسّعة لتفاصيل الصفقة — تُستدعى من أي مكان."""
        import json
        trade_id = t.get("id", "?")
        win = tk.Toplevel(self.root)
        win.title(f"تفاصيل الصفقة #{trade_id}")
        win.geometry("440x620")
        win.configure(bg=BG)
        win.resizable(True, True)

        # ── Scrollable container ──────────────────────────────────────────────
        canvas  = tk.Canvas(win, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=BG)
        scroll_frame.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        # scroll بالماوس
        win.bind("<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        def section(title: str, color=BLUE):
            tk.Frame(scroll_frame, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(8, 0))
            tk.Label(scroll_frame, text=title, bg=BG, fg=color,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(4, 2))

        def row(label: str, value, val_color=TEXT):
            f = tk.Frame(scroll_frame, bg=CARD)
            f.pack(fill="x", padx=16, pady=1)
            tk.Label(f, text=label, bg=CARD, fg=TEXT_DIM,
                     font=("Segoe UI", 9), width=18, anchor="w").pack(side="left", padx=(8,0))
            tk.Label(f, text=str(value) if value not in (None, "", "None") else "—",
                     bg=CARD, fg=val_color,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left", padx=6)

        is_closed = t.get("status") == "closed"
        result    = t.get("result", "")
        res_color = ACCENT if result == "WIN" else (RED if result == "LOSS" else GOLD)
        is_credit = bool(t.get("is_credit", 1))

        # ── Header ────────────────────────────────────────────────────────────
        hdr_bg = ACCENT if result == "WIN" else (RED if result == "LOSS" else BG2)
        hdr_fg = "#000" if result == "WIN" else WHITE
        tk.Label(scroll_frame, text=f"  {t['symbol']}  —  {t['strategy']}  ",
                 bg=hdr_bg, fg=hdr_fg,
                 font=("Segoe UI", 12, "bold")).pack(fill="x", pady=(0, 2))
        tk.Label(scroll_frame,
                 text=f"{t.get('entry_date','')}  {(t.get('entry_time') or '')[:5]}",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8)).pack()

        # ── الأرجل ────────────────────────────────────────────────────────────
        section("📍 الأرجل")
        leg_labels = {
            "short_put":  ("Sell Put",  RED),
            "long_put":   ("Buy Put",   ACCENT),
            "short_call": ("Sell Call", RED),
            "long_call":  ("Buy Call",  ACCENT),
        }
        for key, (lbl, color) in leg_labels.items():
            val = legs.get(key)
            if val:
                row(lbl, f"{val:,.0f}", color)

        # ── معلومات الصفقة ────────────────────────────────────────────────────
        section("📋 معلومات الصفقة")
        trade_mode  = t.get("trade_mode", "0DTE")
        expiry      = t.get("expiry_date", "—")
        dte         = t.get("dte_at_entry", 0)
        val_lbl     = "Credit" if is_credit else "Debit"
        credit_val  = t.get("credit_debit")

        # حساب Max Loss و Reward/Risk
        ml = None
        rr_pct = None
        try:
            from core.risk_manager import calculate_max_loss_per_contract
            strat_mock = {
                "strategy":   t.get("strategy", ""),
                "short_put":  legs.get("short_put"),
                "long_put":   legs.get("long_put"),
                "short_call": legs.get("short_call"),
                "long_call":  legs.get("long_call"),
                "credit":     credit_val if is_credit else None,
                "debit":      credit_val if not is_credit else None,
            }
            ml = calculate_max_loss_per_contract(strat_mock, t.get("symbol", ""))
            if ml and credit_val and ml > 0:
                rr_pct = round(float(credit_val) / ml * 100, 1) if is_credit else None
        except Exception:
            pass

        settle      = t.get("settlement_type", "PM")
        root_sym    = t.get("root_symbol", "SPXW")
        ltt         = t.get("last_trade_time", "15:55 ET")
        settle_color = RED if settle == "AM" else ACCENT

        row("Mode",            trade_mode,  GOLD)
        row("Settlement",      f"{settle} — {root_sym}",  settle_color)
        row("Last Trade Time", ltt,         settle_color)
        row("Expiry Date",     expiry,      TEXT)
        row("DTE دخول",       f"{dte} أيام" if dte else "0DTE", TEXT)
        row(f"Entry {val_lbl}", f"{credit_val:.2f}" if credit_val is not None else "—", WHITE)
        row("Target 50%",  f"{t['target']:.2f}" if t.get('target') else "—", BLUE)
        row("Max Loss/عقد", f"${ml:,.0f}" if ml else "—",
            RED if ml else TEXT_DIM)
        row("Reward/Risk",  f"{rr_pct:.1f}%" if rr_pct else "—",
            ACCENT if (rr_pct or 0) >= 20 else (GOLD if (rr_pct or 0) >= 10 else TEXT_DIM))
        row("Score",        f"{t.get('score', 0)}/100", TEXT)

        # ── الحالة الحالية ────────────────────────────────────────────────────
        section("📊 الحالة الحالية")
        cur   = t.get("current_value")
        pnl_p = t.get("pnl_pct") or t.get("profit_pct")
        pnl_d = t.get("pnl_dollar")
        if pnl_d is None and credit_val and t.get("exit_price"):
            try:
                pnl_d = round((float(credit_val) - float(t["exit_price"])) * 100, 2)
            except Exception:
                pass
        pnl_color = ACCENT if (pnl_p or 0) >= 0 else RED

        row("Current Value",
            f"{cur:.2f}" if cur is not None else ("—" if is_closed else "انتظار سعر"),
            GOLD if cur is not None else TEXT_DIM)
        row("P&L $",  f"${pnl_d:+,.2f}" if pnl_d is not None else "—", pnl_color)
        row("P&L %",  f"{pnl_p:+.1f}%"  if pnl_p is not None else "—", pnl_color)

        if is_closed:
            section("✅ الإغلاق", res_color)
            row("تاريخ الخروج",
                f"{t.get('exit_date','')} {(t.get('exit_time') or '')[:5]}",
                TEXT)
            row("سعر الخروج",
                f"{t['exit_price']:.2f}" if t.get("exit_price") is not None else "—",
                TEXT)
            row("النتيجة",     result or "—", res_color)
            row("سبب الإغلاق", (t.get("close_reason") or "—")[:45], TEXT_DIM)

        # ── السيولة عند الدخول (من legs_json إذا فيها bid/ask) ───────────────
        try:
            legs_full = json.loads(t.get("legs_json") or "{}")
            # legs_json الجديد يحتوي فقط strikes، لكن إذا وُجد bid/ask نعرضها
            has_liq = any(isinstance(v, dict) and v.get("bid") for v in legs_full.values())
            if has_liq:
                section("💧 السيولة عند الدخول")
                for key, ldata in legs_full.items():
                    if not isinstance(ldata, dict):
                        continue
                    bid = ldata.get("bid")
                    ask = ldata.get("ask")
                    sp  = ldata.get("spread_pct")
                    liq = ldata.get("liq_score")
                    if bid or ask:
                        sp_txt  = f"  spread={sp:.0f}%" if sp is not None else ""
                        liq_txt = f"  Liq={liq}" if liq is not None else ""
                        sp_color = RED if (sp or 0) > 30 else (GOLD if (sp or 0) > 15 else ACCENT)
                        row(key.replace("_", " ").title(),
                            f"bid={bid or '—'}  ask={ask or '—'}{sp_txt}{liq_txt}",
                            sp_color)
        except Exception:
            pass

        # ── أسباب الاختيار ────────────────────────────────────────────────────
        try:
            entry_reasons = json.loads(t.get("entry_reasons") or "[]")
            if entry_reasons:
                section("✅ أسباب الاختيار")
                for r_txt in entry_reasons[:6]:
                    tk.Label(scroll_frame, text=f"  • {r_txt}",
                             bg=BG, fg=ACCENT, font=("Segoe UI", 8),
                             anchor="w", wraplength=390).pack(fill="x", padx=20)
        except Exception:
            pass

        try:
            mode_reasons = json.loads(t.get("mode_reasons") or "[]")
            if mode_reasons:
                section("⚠️ تحذيرات", GOLD)
                for w_txt in mode_reasons[:4]:
                    tk.Label(scroll_frame, text=f"  ⚠ {w_txt}",
                             bg=BG, fg=GOLD, font=("Segoe UI", 8),
                             anchor="w", wraplength=390).pack(fill="x", padx=20)
        except Exception:
            pass

        if t.get("entry_smc_snapshot_json"):
            section("SMC Snapshot at Entry", BLUE)
            self._render_entry_smc_snapshot(scroll_frame, t.get("entry_smc_snapshot_json"), BG)

        # ── D/S Diagnostics (RC15j Phase 2B) ─────────────────────────────────
        ds_src = str(t.get("ds_source") or t.get("ds_location_decision") or "")
        if ds_src:
            section("🗺 Demand / Supply Diagnostics", BLUE)
            row("DS Source",      t.get("ds_source") or "—", BLUE)
            row("DS Decision",    t.get("ds_location_decision") or "—",
                ACCENT if "ACTIVE" in str(t.get("ds_location_decision","")) else TEXT_DIM)
            row("Proxy Ratio",    t.get("ds_proxy_ratio") or "—", GOLD)
            row("Proxy Reason",   t.get("ds_proxy_reason") or "—", TEXT_DIM)
            nd_lo = t.get("nearest_demand_low");  nd_hi = t.get("nearest_demand_high")
            ns_lo = t.get("nearest_supply_low");  ns_hi = t.get("nearest_supply_high")
            row("Demand (SPX)",
                f"{nd_lo}–{nd_hi}" if nd_lo and nd_hi else "—", ACCENT)
            row("Supply (SPX)",
                f"{ns_lo}–{ns_hi}" if ns_lo and ns_hi else "—", RED)
            row("ATR 15m",        t.get("atr_15m") or "—", TEXT_DIM)
            row("Near Threshold", t.get("near_threshold") or "—", TEXT_DIM)

        tk.Frame(scroll_frame, bg=BG, height=6).pack()
        tk.Button(scroll_frame, text="  إغلاق  ", bg=CARD, fg=TEXT, relief="flat",
                  cursor="hand2", font=("Segoe UI", 9),
                  command=win.destroy).pack(pady=10)

    def _refresh_auto_trades(self):
        if not hasattr(self, "open_trades_tree"):
            return

        # ── تحديث Watchdog Bar ────────────────────────────────────────────────
        if hasattr(self, "_wd_status_lbl"):
            try:
                from core.bot_logger import wd_status
                wd = wd_status(max_gap_seconds=420)   # 7 دقائق
                if wd["ok"] or wd["age_sec"] is None:
                    color  = ACCENT if wd["ok"] else TEXT_DIM
                    cycles = wd["cycles"]
                    last   = wd["last_run"] or "—"
                    txt    = f"⏱ Watchdog: {wd['status']}  |  آخر دورة: {last}  |  دورات: {cycles}"
                else:
                    color = RED
                    txt   = f"⚠️ Watchdog: {wd['status']}  |  آخر دورة: {wd['last_run'] or '—'}"
                self._wd_status_lbl.config(text=txt, fg=color)
                err = wd.get("last_err", "")
                self._wd_err_lbl.config(
                    text=f"آخر خطأ: {err}" if err else "",
                    fg=RED)
            except Exception:
                pass

        # ── تحديث شريط الرصيد ────────────────────────────────────────────────
        if hasattr(self, "balance_labels"):
            try:
                bal = get_paper_balance()
                initial   = bal["initial"]
                available = bal["available"]
                reserved  = bal["reserved"]
                balance   = bal["balance"]
                pnl       = balance - initial   # P&L حقيقي بدون حسم الـ Margin المحجوز
                self.balance_labels["bal_initial"].config(
                    text=f"${initial:,.0f}")
                self.balance_labels["bal_available"].config(
                    text=f"${available:,.0f}",
                    fg=ACCENT if available >= initial else RED)
                self.balance_labels["bal_reserved"].config(
                    text=f"${reserved:,.0f}",
                    fg=GOLD if reserved > 0 else TEXT_DIM)
                self.balance_labels["bal_pnl"].config(
                    text=f"${pnl:+,.0f}",
                    fg=ACCENT if pnl >= 0 else RED)
            except Exception:
                pass

        # عداد الصفقات المفتوحة
        try:
            max_open = int(get_setting("max_open_trades", "0") or 0)
            current  = get_open_trades_count()
            color    = ACCENT if current < max_open else RED
            self.open_counter.config(
                text=f"📂 صفقات مفتوحة: {current} / {max_open}",
                fg=color
            )
        except Exception:
            pass

        for row in self.open_trades_tree.get_children():
            self.open_trades_tree.delete(row)
        for row in self.closed_trades_tree.get_children():
            self.closed_trades_tree.delete(row)

        self._open_trade_details = {}   # iid → dict كامل للتفاصيل
        all_trades = get_all_open_trades()
        for t in all_trades:
            if t["status"] == "open":
                cur   = t.get("current_value")
                pnl_d = t.get("pnl_dollar")
                pnl_p = t.get("pnl_pct")
                upd   = t.get("last_updated") or "—"
                tgt   = t.get("target")
                entry_dt = str(t.get("entry_date") or "")
                entry_tm = str(t.get("entry_time") or "")
                time_str = entry_tm[:5] if entry_tm else entry_dt[-5:] if entry_dt else "—"
                dte_val  = t.get("dte", "—")
                score    = t.get("score")
                # Exit trigger
                if tgt:
                    tgt_str = f"TP@{tgt:.2f}"
                else:
                    tgt_str = "—"
                # LastCh = وقت آخر تحديث (HH:MM:SS) مثل "11:05:12"
                lastch = upd.split(" ")[-1][:8] if " " in upd else upd[:8]

                legs_txt       = self._legs_compact(t.get("legs_json", "{}"))
                trade_mode_lbl = t.get("trade_mode", "0DTE")
                pnl_tag = "profit" if (pnl_p or 0) > 0 else ("loss_open" if (pnl_p or 0) < 0 else "neutral")
                iid = self.open_trades_tree.insert("", "end", tags=(pnl_tag,), values=(
                    t["id"],
                    time_str,
                    t["symbol"],
                    trade_mode_lbl,
                    t["strategy"],
                    dte_val if dte_val != "—" else "—",
                    f"{t['credit_debit']:.2f}" if t.get("credit_debit") else "—",
                    f"{cur:.2f}" if cur is not None else "—",
                    f"{pnl_p:+.1f}%" if pnl_p is not None else "—",
                    f"{pnl_d:+.2f}$" if pnl_d is not None else "—",
                    tgt_str,
                    lastch,
                    str(score) if score is not None else "—",
                ))
                # حفظ التفاصيل الكاملة لنافذة الـ Double-click
                self._open_trade_details[iid] = {
                    "ID": t["id"], "الرمز": t["symbol"],
                    "Mode": trade_mode_lbl, "الاستراتيجية": t["strategy"],
                    "Entry": f"{t['credit_debit']:.2f}" if t.get("credit_debit") else "—",
                    "Current": f"{cur:.2f}" if cur is not None else "—",
                    "P&L$": f"{pnl_d:+.2f}$" if pnl_d is not None else "—",
                    "P&L%": f"{pnl_p:+.1f}%" if pnl_p is not None else "—",
                    "Score": str(score) if score is not None else "—",
                    "DTE": str(dte_val), "Expiry": str(t.get("expiry_date") or "—"),
                    "Exec": t.get("execution_mode", "Auto"),
                    "Repeat": str(t.get("repeat", "—")),
                    "MaxLoss": f"{t['max_loss']:.2f}" if t.get("max_loss") else "—",
                    "RR%": f"{t['rr_pct']:.0f}%" if t.get("rr_pct") else "—",
                    "C/W": f"{t['credit_width_ratio']:.2f}" if t.get("credit_width_ratio") else "—",
                    "σ": str(t.get("sigma", "—")), "Δ": str(t.get("delta", "—")),
                    "Bucket": str(t.get("bucket") or "—"),
                    "Quality": str(t.get("quality") or "—"),
                    "Legs": legs_txt,
                    "Target": f"{tgt:.2f}" if tgt else "—",
                    "LastUpdated": upd,
                    "EP": str(t.get("entry_protection") or "—"),
                    "D/S": str(t.get("ds_location_decision") or "—"),
                    "ds_ratio": str(t.get("ds_proxy_ratio") or "—"),
                    "nearest_demand": f"{t.get('nearest_demand_low','—')}–{t.get('nearest_demand_high','—')}",
                    "nearest_supply": f"{t.get('nearest_supply_low','—')}–{t.get('nearest_supply_high','—')}",
                }
            else:
                r    = t.get("result", "")
                tag  = "win" if r == "WIN" else ("partial" if r == "PARTIAL WIN" else ("even" if r == "BREAKEVEN" else "loss"))
                pnl  = f"{t['profit_pct']:+.1f}%" if t.get("profit_pct") is not None else "—"
                self.closed_trades_tree.insert("", "end", tags=(tag,), values=(
                    t["id"], t["symbol"], t["strategy"],
                    f"{t['credit_debit']:.2f}" if t.get("credit_debit") else "—",
                    f"{t['exit_price']:.2f}" if t.get("exit_price") else "—",
                    t.get("result", "—"),
                    pnl,
                    (t.get("close_reason") or "")[:30],
                    f"{t['exit_date']} {(t['exit_time'] or '')[:5]}" if t.get("exit_date") else "—",
                ))

        # جدول الرفض
        if hasattr(self, "rejection_tree"):
            for row in self.rejection_tree.get_children():
                self.rejection_tree.delete(row)
            try:
                from core.strategy_engine import STRATEGY_MIN_SCORE, MIN_SCORE_TO_TRADE
                for r in get_recent_rejections(20):
                    score  = r.get("score", 0)
                    strat  = r.get("strategy", "")
                    thresh = STRATEGY_MIN_SCORE.get(strat, MIN_SCORE_TO_TRADE)
                    above  = score >= thresh
                    thresh_lbl = f"✓ ({thresh})" if above else f"✗ ({thresh})"
                    tag = "above_thresh" if above else "below_thresh"
                    reason = r.get("reason", "")
                    self.rejection_tree.insert("", "end", tags=(tag,), values=(
                        r.get("timestamp", "")[:16],
                        r.get("symbol", ""),
                        strat,
                        score,
                        thresh_lbl,
                        reason,
                    ))
            except Exception:
                pass

        # لوحة أداء الـ Mode
        if hasattr(self, "perf_tree"):
            for row in self.perf_tree.get_children():
                self.perf_tree.delete(row)
            try:
                from core.database import get_mode_performance
                perf = get_mode_performance()
                for mode_name, stats in perf.items():
                    if not stats:
                        continue
                    tag = "0dte" if mode_name == "0DTE" else "swing"
                    pf  = stats.get("profit_factor")
                    self.perf_tree.insert("", "end", tags=(tag,), values=(
                        mode_name,
                        stats.get("total_trades", 0),
                        f"{stats.get('win_rate', 0):.1f}%",
                        f"{stats.get('avg_win_pct', 0):+.1f}%",
                        f"-{stats.get('avg_loss_pct', 0):.1f}%",
                        f"{pf:.2f}" if pf else "—",
                        f"{stats.get('expectancy', 0):+.2f}%",
                    ))
                if not perf:
                    self.perf_tree.insert("", "end", values=(
                        "لا توجد بيانات بعد", "—", "—", "—", "—", "—", "—"
                    ))
            except Exception:
                pass

    # ── Paper Trading Tab ─────────────────────────────────────
    def _build_paper_tab(self):
        import json
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  📝 Paper Trades  ")

        # Toolbar
        tb = tk.Frame(frame, bg=BG2, height=48)
        tb.pack(fill="x"); tb.pack_propagate(False)
        tk.Label(tb, text="Paper Trades — الصفقات التجريبية فقط",
                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=14)
        self._paper_refresh_btn = tk.Button(tb, text="🔄 تحديث", bg=CARD, fg=TEXT, font=("Segoe UI", 9),
                  relief="flat", cursor="hand2",
                  command=self._refresh_paper_with_prices)
        self._paper_refresh_btn.pack(side="right", padx=10, pady=10)
        tk.Button(tb, text="📊 تقرير", bg=BLUE, fg=TEXT, font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2",
                  command=self._show_paper_report).pack(side="right", padx=6, pady=10)
        tk.Button(tb, text="📥 Study Excel", bg=ACCENT, fg="#000", font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2",
                  command=self._export_paper_study_excel).pack(side="right", padx=6, pady=10)
        # RC15i.9a: quick access to the daily decision-memory journal.
        tk.Button(tb, text="📘 ذاكرة اليوم", bg=BG3, fg=TEXT, font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2",
                  command=self._open_today_journal_csv).pack(side="right", padx=6, pady=10)
        tk.Button(tb, text="📂 مجلد الذاكرة", bg=CARD, fg=TEXT_DIM, font=("Segoe UI", 9),
                  relief="flat", cursor="hand2",
                  command=self._open_journal_folder).pack(side="right", padx=6, pady=10)
        tk.Button(tb, text="\U0001f5d1 \u0645\u0633\u062d \u0627\u0644\u0628\u064a\u0627\u0646\u0627\u062a", bg=RED, fg=TEXT, font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2",
                  command=self._clear_paper_data).pack(side="right", padx=6, pady=10)

        # Scrollable body (everything below the toolbar)
        frame = self._scrollable_body(frame)

        # Counter
        self.paper_counter = tk.Label(frame, text="", bg=BG, fg=ACCENT,
                                      font=("Segoe UI", 10, "bold"))
        self.paper_counter.pack(anchor="w", padx=14, pady=(8, 2))

        # ── شريط أدوات الجداول ────────────────────────────────────────────────
        tbl_tb = tk.Frame(frame, bg=BG)
        tbl_tb.pack(fill="x", padx=10, pady=(4, 0))
        tk.Label(tbl_tb, text="📂 توصيات مفتوحة", bg=BG, fg=ACCENT,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)
        tk.Button(tbl_tb, text="🔴 إغلاق يدوي", bg="#3a1a1a", fg=RED,
                  font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                  command=self._manual_close_paper_trade).pack(side="right", padx=4, pady=2)
        tk.Button(tbl_tb, text="🗑 حذف المحدد", bg="#3a1a1a", fg=RED,
                  font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                  command=self._delete_selected_paper).pack(side="right", padx=4, pady=2)
        tk.Button(tbl_tb, text="☑ تحديد الكل", bg=CARD, fg=TEXT_DIM,
                  font=("Segoe UI", 8), relief="flat", cursor="hand2",
                  command=self._select_all_paper).pack(side="right", padx=4, pady=2)

        # RC15j — جدول مبسّط. التفاصيل بـ Double-click
        open_cols = ("ID", "الوقت", "الرمز", "Mode", "الاستراتيجية",
                     "DTE", "Entry", "Current", "P&L$", "P&L%", "Best%", "LastChk", "Exit Trigger", "Score")
        op_frame = tk.Frame(frame, bg=BG)
        op_frame.pack(fill="x", padx=10)
        op_scroll = ttk.Scrollbar(op_frame, orient="vertical")
        op_scroll.pack(side="right", fill="y")
        self.paper_open_tree = ttk.Treeview(op_frame, columns=open_cols,
                                             show="headings", height=10,
                                             selectmode="extended",
                                             yscrollcommand=op_scroll.set)
        op_scroll.config(command=self.paper_open_tree.yview)
        for col, w in zip(open_cols, [38, 100, 55, 50, 140, 35, 58, 62, 65, 60, 55, 68, 115, 45]):
            self.paper_open_tree.heading(col, text=col, anchor="center")
            self.paper_open_tree.column(col, width=w, anchor="center")
        self.paper_open_tree.tag_configure("good",       foreground="#00ff88")
        self.paper_open_tree.tag_configure("acceptable",  foreground="#ffd700")
        self.paper_open_tree.tag_configure("weak",        foreground="#ff4444")
        self.paper_open_tree.pack(fill="x")
        self.paper_open_tree.bind("<Double-1>", self._on_paper_click)

        # ── شريط أدوات الجدول المغلق ─────────────────────────────────────────
        cl_tb = tk.Frame(frame, bg=BG)
        cl_tb.pack(fill="x", padx=10, pady=(10, 0))
        tk.Label(cl_tb, text="📋 توصيات مغلقة", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9, "bold")).pack(side="left", padx=4)
        tk.Button(cl_tb, text="🗑 حذف المحدد", bg="#3a1a1a", fg=RED,
                  font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                  command=self._delete_selected_paper_closed).pack(side="right", padx=4, pady=2)
        tk.Button(cl_tb, text="☑ تحديد الكل", bg=CARD, fg=TEXT_DIM,
                  font=("Segoe UI", 8), relief="flat", cursor="hand2",
                  command=self._select_all_paper_closed).pack(side="right", padx=4, pady=2)

        # Closed Recommendations — (تسمية مدمجة في الشريط أعلاه)
        if False:  # placeholder للمحاذاة
            tk.Label(frame, text="📋 توصيات مغلقة", bg=BG, fg=TEXT_DIM,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        closed_cols = ("ID", "الوقت", "الرمز", "Mode", "الاستراتيجية",
                       "Val", "Bucket", "Repeat", "σ", "Δ", "Exit", "النتيجة", "P&L%", "Best%", "P&L$", "EP", "السبب")
        cl_frame = tk.Frame(frame, bg=BG)
        cl_frame.pack(fill="x", padx=10)
        cl_scroll = ttk.Scrollbar(cl_frame, orient="vertical")
        cl_scroll.pack(side="right", fill="y")
        self.paper_closed_tree = ttk.Treeview(cl_frame, columns=closed_cols,
                                               show="headings", height=8,
                                               selectmode="extended",
                                               yscrollcommand=cl_scroll.set)
        cl_scroll.config(command=self.paper_closed_tree.yview)
        for col, w in zip(closed_cols, [35, 100, 55, 55, 140, 55, 115, 65, 45, 45, 55, 60, 55, 55, 60, 115, 120]):
            self.paper_closed_tree.heading(col, text=col, anchor="center")
            self.paper_closed_tree.column(col, width=w, anchor="center")
        self.paper_closed_tree.tag_configure("win",  background="#1a3a2a", foreground=ACCENT)
        self.paper_closed_tree.tag_configure("loss", background="#3a1a1a", foreground=RED)
        self.paper_closed_tree.tag_configure("part", background="#1a2a1a", foreground=GOLD)
        self.paper_closed_tree.pack(fill="both", expand=True)
        self.paper_closed_tree.bind("<Double-1>", self._on_paper_click)

        # ── إدارة الاستراتيجيات ───────────────────────────────────────────────
        tk.Label(frame, text="⚙️ حالة الاستراتيجيات", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=14, pady=(10, 2))
        ss_tb = tk.Frame(frame, bg=BG)
        ss_tb.pack(fill="x", padx=10)
        tk.Button(ss_tb, text="🔄 تقييم المراحل", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 8), relief="flat", cursor="hand2",
                  command=self._run_stage_evaluation).pack(side="left", padx=4, pady=4)
        tk.Button(ss_tb, text="✅ إعادة تفعيل", bg=ACCENT, fg="#000",
                  font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                  command=self._reenable_selected_strategy).pack(side="left", padx=4, pady=4)
        tk.Button(ss_tb, text="🔬 Probation", bg=GOLD, fg="#000",
                  font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2",
                  command=self._probation_selected_strategy).pack(side="left", padx=4, pady=4)

        ss_cols = ("Mode", "Strategy", "Symbol", "Status",
                   "Stage", "Weight", "PF", "EXP%", "n", "Prob", "Updated", "Reason")
        ss_f = tk.Frame(frame, bg=BG)
        ss_f.pack(fill="x", padx=10, pady=(0, 8))
        self.ss_tree = ttk.Treeview(ss_f, columns=ss_cols,
                                     show="headings", height=5)
        for col, w in zip(ss_cols, [55, 140, 50, 70, 55, 50, 50, 55, 40, 45, 95, 200]):
            self.ss_tree.heading(col, text=col, anchor="center")
            self.ss_tree.column(col, width=w, anchor="center")
        self.ss_tree.tag_configure("active",   foreground=ACCENT)
        self.ss_tree.tag_configure("warning",  foreground=GOLD)
        self.ss_tree.tag_configure("reduced",  foreground="#ff8800")
        self.ss_tree.tag_configure("disabled", foreground=RED)
        self.ss_tree.tag_configure("probation", foreground=BLUE)
        self.ss_tree.pack(fill="x")

        self._refresh_paper()

    # ── Paper Delete Helpers ──────────────────────────────────────────────────

    def _get_selected_ids(self, tree) -> list:
        """يجلب IDs الصفوف المحددة من أي Treeview."""
        ids = []
        for item in tree.selection():
            try:
                ids.append(int(tree.item(item, "values")[0]))
            except (ValueError, IndexError):
                pass
        return ids

    def _select_all_paper(self):
        """تحديد كل صفوف جدول المفتوحة."""
        self.paper_open_tree.selection_set(self.paper_open_tree.get_children())

    def _select_all_paper_closed(self):
        """تحديد كل صفوف جدول المغلقة."""
        self.paper_closed_tree.selection_set(self.paper_closed_tree.get_children())

    def _delete_selected_paper(self):
        """حذف التوصيات المحددة من جدول المفتوحة."""
        import tkinter.messagebox as mb
        from core.database import delete_paper_trades
        ids = self._get_selected_ids(self.paper_open_tree)
        if not ids:
            mb.showwarning("تنبيه", "لم تحدد أي صف.\nاضغط على الصف أولاً ثم حذف.")
            return
        ok = mb.askyesno("تأكيد الحذف",
                         f"هل تريد حذف {len(ids)} توصية مفتوحة؟\n"
                         f"IDs: {ids}")
        if not ok:
            return
        deleted = delete_paper_trades(ids)
        mb.showinfo("تم", f"تم حذف {deleted} توصية.")
        self._refresh_paper()

    def _delete_selected_paper_closed(self):
        """حذف التوصيات المحددة من جدول المغلقة."""
        import tkinter.messagebox as mb
        from core.database import delete_paper_trades
        ids = self._get_selected_ids(self.paper_closed_tree)
        if not ids:
            mb.showwarning("تنبيه", "لم تحدد أي صف.\nاضغط على الصف أولاً ثم حذف.")
            return
        ok = mb.askyesno("تأكيد الحذف",
                         f"هل تريد حذف {len(ids)} توصية مغلقة؟\n"
                         f"IDs: {ids}")
        if not ok:
            return
        deleted = delete_paper_trades(ids)
        mb.showinfo("تم", f"تم حذف {deleted} توصية.")
        self._refresh_paper()

    def _clear_paper_data(self):
        import tkinter.messagebox as mb
        import shutil, os
        from core.database import DB_PATH, get_paper_trades

        # أظهر عدد السجلات قبل الحذف
        try:
            n_open   = len(get_paper_trades(status="open"))
            n_closed = len(get_paper_trades(status="closed"))
            count_msg = f"\n\nسيتم حذف: {n_open} توصية مفتوحة + {n_closed} مغلقة."
        except Exception:
            count_msg = ""

        ok = mb.askyesno(
            "⚠️ تأكيد مسح البيانات",
            f"هل تريد مسح paper_trades و strategy_status و signal_tracking والبدء من الصفر؟"
            f"{count_msg}\n\nسيتم عمل نسخة احتياطية تلقائياً قبل المسح."
        )
        if not ok:
            return

        # نسخة احتياطية تلقائية
        try:
            from datetime import datetime
            backup_path = DB_PATH.replace(".db", f"_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db")
            shutil.copy2(DB_PATH, backup_path)
            mb.showinfo("نسخة احتياطية",
                        f"تم حفظ نسخة احتياطية:\n{os.path.basename(backup_path)}")
        except Exception as be:
            go = mb.askyesno("تحذير", f"فشل عمل نسخة احتياطية:\n{be}\n\nهل تريد المتابعة بدون نسخة احتياطية؟")
            if not go:
                return

        try:
            clear_paper_data()
            self._refresh_paper()
            mb.showinfo("تم", "تم مسح بيانات Paper Trading.")
        except Exception as e:
            mb.showerror("خطأ", str(e))

    def _run_stage_evaluation(self):
        changes = evaluate_strategy_stages()
        self._refresh_paper()
        import tkinter.messagebox as mb
        if changes:
            msg = "\n".join(
                f"{c['symbol']} {c['strategy']} [{c['mode']}]: "
                f"Stage {c['old_stage']}→{c['new_stage']} ({c['status']})"
                for c in changes
            )
            mb.showinfo("تقييم المراحل", f"تغييرات:\n{msg}")
        else:
            mb.showinfo("تقييم المراحل", "لا تغييرات")

    def _reenable_selected_strategy(self):
        if not hasattr(self, "ss_tree"):
            return
        sel = self.ss_tree.selection()
        if not sel:
            return
        vals = self.ss_tree.item(sel[0], "values")
        try:
            mode, strat, sym = str(vals[0]), str(vals[1]), str(vals[2])
            re_enable_strategy(mode, strat, sym,
                               "إعادة تفعيل يدوي من الواجهة",
                               probation=False)
            self._refresh_paper()
        except Exception as e:
            print(f"[reenable] {e}")

    def _probation_selected_strategy(self):
        if not hasattr(self, "ss_tree"):
            return
        sel = self.ss_tree.selection()
        if not sel:
            return
        vals = self.ss_tree.item(sel[0], "values")
        try:
            mode, strat, sym = str(vals[0]), str(vals[1]), str(vals[2])
            re_enable_strategy(mode, strat, sym,
                               "Probation — paper only x10",
                               probation=True)
            self._refresh_paper()
        except Exception as e:
            print(f"[probation] {e}")

    @staticmethod
    def _ep_badge(t: dict) -> str:
        """RC14: badge مختصر لحالة Entry Protection في جدول Paper Trades."""
        ep   = t.get("entry_protection") or ""
        ds   = t.get("ds_evaluation")    or ""
        smc  = t.get("smc_evaluation")   or ""
        pipe = t.get("pipeline_stop_code") or ""
        # صفقة قديمة — حقول RC14 فارغة أو LEGACY_UNKNOWN صريحة
        if (not ep and not ds and not smc) or ep == "LEGACY_UNKNOWN" or ds == "LEGACY_UNKNOWN":
            return "LEGACY" if t.get("selected_mode") == "Swing" else "—"
        # hard reject
        if ep == "HARD_REJECTED" or ds == "HARD_REJECTED":
            return "REJECT"
        # passed
        if ep == "PASSED" and ds in ("PASSED", "NO_ACTIVE_ZONE", ""):
            return "SMC OK"
        # warning
        if "WARNING" in ep or ds == "UNAVAILABLE":
            ds_short = {"UNAVAILABLE": "N/A", "NO_ACTIVE_ZONE": "NO ZONE",
                        "WARNING_ZONE": "WARN", "PASSED": "OK"}.get(ds, ds[:6])
            return f"SMC OK|DS {ds_short}"
        # pipeline stopped before SMC
        if smc in ("NOT_EVALUATED", "") and pipe:
            return "NOT EVAL"
        return ep[:10] if ep else "—"

    def _refresh_paper_with_prices(self):
        """زر التحديث اليدوي: جلب أسعار جديدة من API ثم تحديث الواجهة."""
        import threading
        btn = getattr(self, "_paper_refresh_btn", None)
        if btn:
            try:
                if btn.cget("state") == "disabled":
                    return  # منع الضغط المزدوج
                btn.config(state="disabled", text="⏳ جاري...")
            except Exception:
                pass

        def worker():
            try:
                from core.trade_monitor import refresh_paper_trade_prices
                refresh_paper_trade_prices()
            except Exception as e:
                print(f"[manual_refresh] {e}")
            def done():
                self._refresh_paper()
                if btn:
                    try:
                        btn.config(state="normal", text="🔄 تحديث")
                    except Exception:
                        pass
            self.root.after(0, done)
        threading.Thread(target=worker, daemon=True).start()

    def _refresh_paper(self):
        if not hasattr(self, "paper_open_tree"):
            return
        try:
            for row in self.paper_open_tree.get_children():
                self.paper_open_tree.delete(row)
            for row in self.paper_closed_tree.get_children():
                self.paper_closed_tree.delete(row)

            open_t   = get_paper_trades(status="open")
            closed_t = get_paper_trades(status="closed")

            if hasattr(self, "paper_counter"):
                exposure = {}
                for _t in open_t:
                    st = (_t.get("strategy") or "")
                    sym = (_t.get("symbol") or "?").upper()
                    direction = "bearish" if ("Put Debit" in st or "Bear Call" in st) else ("bullish" if ("Call Debit" in st or "Bull Put" in st) else "neutral")
                    key = f"{sym} {direction}"
                    exposure[key] = exposure.get(key, 0) + 1
                exp_txt = " | Exposure: " + ", ".join(f"{k}={v}" for k, v in sorted(exposure.items())) if exposure else ""
                self.paper_counter.config(
                    text=f"📝 توصيات: {len(open_t)} مفتوحة / {len(closed_t)} مغلقة"
                         f"  |  إجمالي: {len(open_t)+len(closed_t)}" + exp_txt
                )

            for t in open_t:
                rr  = t.get("reward_risk") or 0
                val = t.get("credit_debit") or 0
                cur = t.get("current_value")
                pnl_d = t.get("current_pnl_dollar")
                pnl_p = t.get("current_pnl_pct")
                trig = t.get("exit_trigger") or "waiting"
                q = (t.get("setup_quality") or "").lower()
                qtag = "good" if q=="good" else ("acceptable" if q=="acceptable" else ("weak" if q in ("weak","unrealistic rr") else ""))
                _last_ch = (t.get("last_updated") or "")
                _last_ch = _last_ch.split(" ")[-1][:8] if " " in _last_ch else _last_ch[:8]
                _iid = self.paper_open_tree.insert("", "end", tags=(qtag,) if qtag else (), values=(
                    t["id"],
                    (t.get("timestamp") or "")[:16],
                    t.get("symbol", ""),
                    t.get("selected_mode", ""),
                    t.get("strategy", ""),
                    t.get("dte_at_entry", 0),
                    f"{val:.2f}" if val else "—",
                    f"{cur:.2f}" if cur is not None else "—",
                    f"${pnl_d:+.2f}" if pnl_d is not None else "—",
                    f"{pnl_p:+.1f}%" if pnl_p is not None else "—",
                    f"{t.get('best_pnl_pct_seen'):+.1f}%" if t.get('best_pnl_pct_seen') is not None else "—",
                    _last_ch or "—",
                    trig[:24],
                    t.get("score", 0),
                ))
                if not hasattr(self, "_paper_open_details"):
                    self._paper_open_details = {}
                self._paper_open_details[_iid] = t

            for t in closed_t:
                r   = t.get("result", "")
                tag = "win" if r == "WIN" else ("loss" if r == "LOSS" else "part")
                pnl_p = t.get("profit_pct")
                pnl_d = t.get("pnl_dollar")
                val   = t.get("credit_debit") or 0
                exit_ = t.get("exit_price")
                self.paper_closed_tree.insert("", "end", tags=(tag,), values=(
                    t["id"],
                    (t.get("timestamp") or "")[:16],
                    t.get("symbol", ""),
                    t.get("selected_mode", ""),
                    t.get("strategy", ""),
                    f"{val:.2f}" if val else "—",
                    (t.get("time_bucket") or "—").replace("09:30-09:45 ", "").replace("09:45-10:30 ", "").replace("10:30-12:00 ", "").replace("12:00-14:00 ", "").replace("14:00-15:00 ", "").replace("15:00-15:30 ", "").replace("15:30-16:00 ", "")[:18],
                    "YES" if t.get("is_repeated_setup") else "NO",
                    f"{t.get('sigma_distance'):.2f}" if t.get('sigma_distance') is not None else "—",
                    f"{t.get('short_delta'):+.2f}" if t.get('short_delta') is not None else "—",
                    f"{exit_:.2f}" if exit_ else "—",
                    r or "—",
                    f"{pnl_p:+.1f}%" if pnl_p is not None else "—",
                    f"{t.get('best_pnl_pct_seen'):+.1f}%" if t.get('best_pnl_pct_seen') is not None else "—",
                    f"${pnl_d:+.2f}" if pnl_d is not None else "—",
                    self._ep_badge(t),
                    (t.get("close_reason") or "")[:20],
                ))
        except Exception as e:
            print(f"[refresh_paper] {e}")

        # تحديث جدول حالة الاستراتيجيات
        if hasattr(self, "ss_tree"):
            for row in self.ss_tree.get_children():
                self.ss_tree.delete(row)
            try:
                statuses = get_all_strategy_statuses()
                for s in statuses:
                    st  = s.get("status", "active")
                    pf  = s.get("pf_current")
                    exp = s.get("exp_current")
                    self.ss_tree.insert("", "end", tags=(st,), values=(
                        s.get("trade_mode", ""),
                        s.get("strategy", ""),
                        s.get("symbol", ""),
                        st,
                        s.get("stage", 0),
                        f"{s.get('weight',1.0):.0%}",
                        f"{pf:.2f}" if pf else "—",
                        f"{exp:+.1f}%" if exp is not None else "—",
                        s.get("sample_size", s.get("trades_at_stage", 0)),
                        s.get("probation_count", 0),
                        (s.get("last_updated") or "")[:16],
                        (s.get("reason") or "")[:30],
                    ))
            except Exception as e:
                print(f"[refresh_ss] {e}")

    def _manual_close_paper_trade(self):
        """إغلاق يدوي للصفقة المحددة في جدول Paper Trades المفتوحة.

        يستخدم مسار paper_trades الرسمي: close_paper_trade()
        حتى تنتقل الصفقة إلى توصيات مغلقة، وتُنسخ إلى سجل الصفقات/Backtest كـ AUTO_PAPER،
        ويُستدعى Telegram close alert بنفس مسار الإغلاق التلقائي.
        """
        import tkinter.simpledialog as sd
        import tkinter.messagebox as mb

        if not hasattr(self, "paper_open_tree"):
            return
        sel = self.paper_open_tree.selection()
        if not sel:
            mb.showwarning("تنبيه", "حدد صفقة مفتوحة من جدول Paper Trades أولاً.")
            return

        if len(sel) > 1:
            mb.showwarning("تنبيه", "الإغلاق اليدوي يتم لصفقة واحدة فقط. حدد صفقة واحدة.")
            return

        try:
            vals = self.paper_open_tree.item(sel[0], "values")
            trade_id = int(vals[0])
        except Exception:
            mb.showwarning("خطأ", "تعذّر قراءة رقم الصفقة المحددة.")
            return

        try:
            trades = get_paper_trades(status="open", limit=10000)
            t = next((x for x in trades if int(x.get("id", -1)) == trade_id), None)
            if not t:
                mb.showwarning("خطأ", f"لم يُعثر على Paper Trade مفتوحة برقم #{trade_id}.")
                return

            symbol   = str(t.get("symbol") or "")
            strategy = str(t.get("strategy") or "")
            entry    = float(t.get("credit_debit") or 0)
            current  = t.get("current_value")
            default_exit = current if current is not None else entry

            exit_price_str = sd.askstring(
                "إغلاق Paper Trade يدوي",
                f"إغلاق #{trade_id} — {symbol} — {strategy}\n\n"
                f"Entry: {entry:.2f}\n"
                f"Current: {default_exit:.2f}\n\n"
                f"أدخل سعر الخروج، أو اتركه فارغاً لاستخدام Current:",
            )
            if exit_price_str is None:
                return
            exit_price = (float(exit_price_str.strip())
                          if exit_price_str.strip()
                          else float(default_exit or 0))

            is_credit = any(x in strategy for x in (
                "Iron Condor", "Bull Put Spread", "Bear Call Spread"
            ))
            if entry > 0:
                profit_pct = round((entry - exit_price) / entry * 100, 1) if is_credit \
                             else round((exit_price - entry) / entry * 100, 1)
            else:
                profit_pct = 0.0
            pnl_dollar = round((entry - exit_price if is_credit else exit_price - entry) * 100, 2)

            if profit_pct >= 50:
                result = "WIN"
            elif profit_pct > 0:
                result = "PARTIAL WIN"
            elif profit_pct >= -5:
                result = "BREAKEVEN"
            else:
                result = "LOSS"

            close_paper_trade(
                trade_id=trade_id,
                exit_price=exit_price,
                result=result,
                profit_pct=profit_pct,
                pnl_dollar=pnl_dollar,
                close_reason="إغلاق يدوي",
            )

            mb.showinfo(
                "تم",
                f"تم إغلاق Paper Trade #{trade_id}\n"
                f"{result} | {profit_pct:+.1f}% | ${pnl_dollar:+.2f}"
            )
            self._refresh_paper()
            # RC14 hotfix: force a fresh Swing analysis after manual close so a
            # newly-qualified setup can be considered on the next analysis cycle.
            try:
                import threading
                from core.trade_monitor import _update_swing_cache_bg
                threading.Thread(target=_update_swing_cache_bg, daemon=True).start()
                print(f"[manual_close_sync] #{trade_id} UI refreshed; Swing cache refresh requested")
            except Exception as _sync_e:
                print(f"[manual_close_sync] Swing cache refresh failed: {_sync_e}")
            if hasattr(self, "_refresh_trades"):
                self._refresh_trades()
            if hasattr(self, "_refresh_backtest"):
                self._refresh_backtest()
            if hasattr(self, "_refresh_stats"):
                self._refresh_stats()
        except ValueError:
            mb.showwarning("خطأ", "سعر الخروج غير صالح.")
        except Exception as e:
            mb.showerror("خطأ", f"فشل إغلاق Paper Trade: {e}")

    def _on_paper_click(self, event):
        tree = event.widget
        sel  = tree.selection()
        if not sel:
            return
        try:
            tid = int(tree.item(sel[0], "values")[0])
            self._show_paper_detail(tid)
        except Exception:
            pass

    def _show_paper_detail(self, trade_id: int):
        import json
        trades = get_paper_trades()
        t = next((x for x in trades if x["id"] == trade_id), None)
        if not t:
            return

        win = tk.Toplevel(self.root)
        win.title(f"Paper Trade #{trade_id}")
        win.configure(bg=BG)
        win.geometry("560x780")
        win.minsize(480, 520)
        win.resizable(True, True)
        win.grab_set()

        is_win  = t.get("result") == "WIN"
        is_loss = t.get("result") == "LOSS"
        hdr_bg  = ACCENT if is_win else (RED if is_loss else CARD)
        tk.Label(win,
                 text=f"  #{trade_id}  {t.get('symbol','')}  [{t.get('selected_mode','')}]  —  {t.get('strategy','')}  ",
                 bg=hdr_bg, fg="#000" if is_win else TEXT,
                 font=("Segoe UI", 11, "bold")).pack(fill="x")

        # v3.33.6a RC3: make the detail window scrollable/resizable.
        # The previous fixed-height popup could hide lower diagnostics on small screens.
        outer = tk.Frame(win, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = tk.Frame(canvas, bg=BG)
        body_window = canvas.create_window((0, 0), window=body, anchor="nw")

        def _sync_scroll_region(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _sync_body_width(event):
            canvas.itemconfigure(body_window, width=event.width)

        body.bind("<Configure>", _sync_scroll_region)
        canvas.bind("<Configure>", _sync_body_width)

        def _on_mousewheel(event):
            # Windows/macOS wheel support while the detail popup is focused/hovered.
            delta = -1 * int(event.delta / 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        def _on_linux_scroll_up(_event):
            canvas.yview_scroll(-1, "units")

        def _on_linux_scroll_down(_event):
            canvas.yview_scroll(1, "units")

        # Bind only while the pointer is inside the popup to avoid hijacking main dashboard scroll.
        def _bind_scroll(_event=None):
            canvas.bind_all("<MouseWheel>", _on_mousewheel)
            canvas.bind_all("<Button-4>", _on_linux_scroll_up)
            canvas.bind_all("<Button-5>", _on_linux_scroll_down)

        def _unbind_scroll(_event=None):
            canvas.unbind_all("<MouseWheel>")
            canvas.unbind_all("<Button-4>")
            canvas.unbind_all("<Button-5>")

        body.bind("<Enter>", _bind_scroll)
        body.bind("<Leave>", _unbind_scroll)
        win.protocol("WM_DELETE_WINDOW", lambda: (_unbind_scroll(), win.destroy()))

        def row(label, val, color=TEXT):
            f = tk.Frame(body, bg=BG2); f.pack(fill="x", padx=16, pady=2)
            tk.Label(f, text=label, bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 9), width=18, anchor="e").pack(side="left")
            tk.Label(f, text=str(val) if val not in (None,"") else "—",
                     bg=BG2, fg=color,
                     font=("Segoe UI", 9, "bold"), anchor="w", wraplength=340,
                     justify="left").pack(side="left", padx=8, fill="x", expand=True)

        tk.Label(body, text="الأرجل", bg=BG, fg=BLUE,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(8,0))
        for k, v in [("Short Put", t.get("short_put")), ("Long Put",  t.get("long_put")),
                     ("Short Call",t.get("short_call")), ("Long Call", t.get("long_call"))]:
            if v:
                tk.Label(body, text=f"  {k}: {v:,.0f}",
                         bg=BG2, fg=TEXT, font=("Segoe UI",9)).pack(fill="x", padx=16, pady=1)

        tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
        row("Timestamp",    (t.get("timestamp","") or "")[:16])
        row("Expiry / DTE", f"{t.get('expiry_date','')}  (DTE={t.get('dte_at_entry',0)})")
        row("Credit/Debit", f"{t['credit_debit']:.2f}" if t.get("credit_debit") else "—")
        row("Max Profit",   f"{t['max_profit']:.2f}" if t.get("max_profit") else "—", ACCENT)
        row("Max Loss",     f"{t['max_loss']:.2f}"   if t.get("max_loss")   else "—", RED)
        rr = t.get("reward_risk") or 0
        row("Reward/Risk",  f"{rr:.1%}" if rr else "—")
        row("Score",        f"{t.get('score',0)}/100")
        row("Current Val",  f"{t.get('current_value'):.2f}" if t.get("current_value") is not None else "—")
        row("P&L$",         f"${t.get('current_pnl_dollar'):+.2f}" if t.get("current_pnl_dollar") is not None else "—", ACCENT if (t.get("current_pnl_dollar") or 0) >= 0 else RED)
        row("P&L%",         f"{t.get('current_pnl_pct'):+.1f}%" if t.get("current_pnl_pct") is not None else "—", ACCENT if (t.get("current_pnl_pct") or 0) >= 0 else RED)
        row("Exit Trigger", t.get("exit_trigger") or "—")
        row("Debit Ratio",  f"{t.get('debit_ratio','')}%" if t.get("debit_ratio") else "—")
        row("Bid/Ask OK",   "نعم" if t.get("bid_ask_ok") else "لا",
            ACCENT if t.get("bid_ask_ok") else RED)

        tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
        row("First Detected", (t.get("first_detected_time") or "")[-8:-3] or "?")
        row("Current Signal", (t.get("current_signal_time") or "")[-8:-3] or "?")
        row("Signal Age",     t.get("signal_age") or "?")
        row("Initial Score",  t.get("initial_score") if t.get("initial_score") is not None else "?")
        row("Current Score",  t.get("current_score") if t.get("current_score") is not None else "?")
        row("Peak Score",     t.get("peak_score") if t.get("peak_score") is not None else "?")
        row("Price Then",     f"{t.get('price_first_detected'):.2f}" if t.get("price_first_detected") else "?")
        row("Price Now",      f"{t.get('current_price'):.2f}" if t.get("current_price") else "?")

        if t.get("entry_smc_snapshot_json"):
            tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
            self._render_entry_smc_snapshot(body, t.get("entry_smc_snapshot_json"), BG)

        # ── RC14: Entry Protection Summary ───────────────────────────────────
        _t_pipe  = t.get("pipeline_stop_code")
        _t_smc   = t.get("smc_evaluation")
        _t_ds    = t.get("ds_evaluation")
        _t_dsr   = t.get("ds_unavailable_reason")
        _t_ep    = t.get("entry_protection")
        _t_diag  = t.get("diagnostic_code")
        if any((_t_smc, _t_ds, _t_ep, _t_diag)):
            tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
            tk.Label(body, text="Entry Protection", bg=BG, fg=BLUE,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=16, pady=(2, 0))
            def _ep_color(val):
                if not val:            return TEXT_DIM
                if val == "PASSED":    return "#4caf50"
                if "WARNING" in val:   return GOLD
                if "REJECTED" in val:  return RED
                if "NOT_EVAL" in val:  return TEXT_DIM
                return TEXT_DIM
            if _t_smc:
                row("  SMC", _t_smc or "—", _ep_color(_t_smc))
            if _t_ds:
                row("  D/S", _t_ds or "—", _ep_color(_t_ds))
            if _t_dsr:
                row("  D/S Reason", _t_dsr, GOLD)
            if _t_ep:
                row("  Result", _t_ep or "—", _ep_color(_t_ep))
            if _t_diag:
                row("  Code", _t_diag or "—", TEXT_DIM)
            if _t_pipe:
                row("  Pipeline", _t_pipe or "—", TEXT_DIM)
        elif t.get("selected_mode") == "Swing":
            tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
            row("Entry Protection", "LEGACY / NOT RECORDED", TEXT_DIM)

        tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
        row("Underlying",     f"{t.get('underlying_price'):.2f}" if t.get("underlying_price") else "—")
        row("Expected Move",  f"±{t.get('expected_move'):.2f}" if t.get("expected_move") else "—")
        row("Sigma Dist",     f"{t.get('sigma_distance'):.2f}σ" if t.get("sigma_distance") is not None else "—")
        row("Short Delta",    f"{t.get('short_delta'):+.3f}" if t.get("short_delta") is not None else "—")
        row("Long Delta",     f"{t.get('long_delta'):+.3f}" if t.get("long_delta") is not None else "—")
        row("Put Δ S/L",      (f"{t.get('short_put_delta'):+.3f}" if t.get("short_put_delta") is not None else "—") + " / " + (f"{t.get('long_put_delta'):+.3f}" if t.get("long_put_delta") is not None else "—"))
        row("Call Δ S/L",     (f"{t.get('short_call_delta'):+.3f}" if t.get("short_call_delta") is not None else "—") + " / " + (f"{t.get('long_call_delta'):+.3f}" if t.get("long_call_delta") is not None else "—"))
        row("Distance",       f"{t.get('distance_points'):+.2f} pts ({t.get('distance_pct'):+.2f}%)" if t.get("distance_points") is not None else "—")
        row("Credit/Width",   f"{t.get('credit_width_ratio'):.1%} / min {t.get('min_credit_width_ratio'):.0%}" if t.get("credit_width_ratio") is not None and t.get("min_credit_width_ratio") is not None else "—")
        if t.get("credit_width_note"):
            row("C/W Note", t.get("credit_width_note"), RED)

        tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
        try:
            reasons = json.loads(t.get("entry_reasons") or "[]")
            if reasons:
                tk.Label(body, text="أسباب الدخول:", bg=BG, fg=BLUE,
                         font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16)
                for r in reasons[:8]:
                    tk.Label(body, text=f"  • {r}", bg=BG, fg=TEXT_DIM,
                             font=("Segoe UI",8), wraplength=500, justify="left").pack(anchor="w", padx=20, fill="x")
        except Exception:
            pass

        if t.get("status") == "closed":
            tk.Frame(body, bg=BG3, height=1).pack(fill="x", padx=16, pady=6)
            pnl_p = t.get("profit_pct")
            pnl_d = t.get("pnl_dollar")
            row("النتيجة",    t.get("result","—"),
                ACCENT if is_win else (RED if is_loss else GOLD))
            row("P&L %",      f"{pnl_p:+.1f}%" if pnl_p is not None else "—",
                ACCENT if (pnl_p or 0) >= 0 else RED)
            row("P&L $",      f"${pnl_d:+,.2f}" if pnl_d is not None else "—",
                ACCENT if (pnl_d or 0) >= 0 else RED)
            row("سبب الإغلاق", t.get("close_reason","—"))

        tk.Button(body, text="إغلاق", bg=CARD, fg=TEXT, font=("Segoe UI",9),
                  relief="flat", cursor="hand2", padx=20,
                  command=lambda: (_unbind_scroll(), win.destroy())).pack(pady=12)

    def _export_paper_study_excel(self):
        from core.exporter import export_paper_study_to_excel  # RC15i.4: lazy
        ok, result = export_paper_study_to_excel()
        if ok:
            messagebox.showinfo("تم التصدير", f"✅ تم حفظ ملف دراسة Paper Trades:\n{result}")
        else:
            messagebox.showerror("خطأ", result)

    # ── RC15i.9a: Daily Session Journal UI buttons ─────────────
    def _open_external_path(self, path):
        """Open a file/folder with the operating system default app. Never affects trading."""
        try:
            import subprocess
            path = os.path.abspath(str(path))
            if sys.platform.startswith("win"):
                os.startfile(path)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
            return True
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذر فتح المسار:\n{path}\n\n{exc}")
            return False

    def _open_today_journal_csv(self):
        """Create/sync/open today's Daily Session Journal CSV for quick review in Excel."""
        try:
            import csv
            from pathlib import Path
            from core.session_journal import get_today_journal_paths, CSV_FIELDS, rebuild_csv_from_jsonl

            paths = get_today_journal_paths()
            csv_path = Path(paths["csv_path"])
            jsonl_path = Path(paths.get("jsonl_path", ""))
            csv_path.parent.mkdir(parents=True, exist_ok=True)

            # RC15i.9d: JSONL is the source of truth. Rebuild CSV before opening
            # so the button does not show an empty header-only file while JSONL has data.
            sync = rebuild_csv_from_jsonl(str(csv_path), str(jsonl_path))
            if sync.get("ok"):
                if int(sync.get("rows") or 0) == 0:
                    messagebox.showinfo(
                        "ذاكرة اليوم",
                        "تم فتح ملف ذاكرة اليوم، لكنه لا يحتوي سجلات بعد.\n"
                        "بعد تشغيل تحليل كامل سيبدأ البوت بإضافة القرارات تلقائيًا.\n\n"
                        f"المسار:\n{csv_path}",
                    )
            else:
                # Fallback: create header only if sync failed and CSV does not exist.
                if not csv_path.exists():
                    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
                        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
                        writer.writeheader()
                print(f"[session_journal csv sync warning] {sync}")

            self._open_external_path(csv_path)
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذر فتح ذاكرة اليوم:\n{exc}")
    def _open_journal_folder(self):
        """Open the folder that contains session_journal_YYYY-MM-DD files."""
        try:
            from pathlib import Path
            from core.session_journal import get_today_journal_paths

            paths = get_today_journal_paths()
            folder = Path(paths["csv_path"]).parent
            folder.mkdir(parents=True, exist_ok=True)
            self._open_external_path(folder)
        except Exception as exc:
            messagebox.showerror("خطأ", f"تعذر فتح مجلد الذاكرة:\n{exc}")

    def _show_paper_report(self):
        report = get_paper_report()
        if not report or not report.get("overall"):
            import tkinter.messagebox as mb
            mb.showinfo("Paper Report", "لا توجد توصيات مغلقة بعد.")
            return

        win = tk.Toplevel(self.root)
        win.title("Paper Trading Report")
        win.configure(bg=BG); win.resizable(True, True); win.grab_set()
        win.geometry("640x520")

        tk.Label(win, text="  📊 Paper Trading Report  ",
                 bg=BLUE, fg=TEXT, font=("Segoe UI",12,"bold")).pack(fill="x")

        canvas = tk.Canvas(win, bg=BG, highlightthickness=0)
        scroll = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0,0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        def section(title):
            tk.Label(inner, text=f"  {title}", bg=BG2, fg=BLUE,
                     font=("Segoe UI",9,"bold")).pack(fill="x", padx=8, pady=(10,2))

        def stat_row(label, val, color=TEXT):
            f = tk.Frame(inner, bg=BG); f.pack(fill="x", padx=20, pady=1)
            tk.Label(f, text=label, bg=BG, fg=TEXT_DIM,
                     font=("Segoe UI",9), width=22, anchor="w").pack(side="left")
            tk.Label(f, text=str(val), bg=BG, fg=color,
                     font=("Segoe UI",9,"bold")).pack(side="left")

        ov = report.get("overall", {})
        lc = report.get("live_criteria", {})

        section("الإجمالي")
        stat_row("إجمالي التوصيات",  ov.get("total", 0))
        stat_row("مفتوحة",           ov.get("total_open", 0))
        stat_row("Win Rate",         f"{ov.get('win_rate',0):.1f}%",
                 ACCENT if ov.get("win_rate",0) >= 50 else RED)
        stat_row("Avg Win %",        f"+{ov.get('avg_win_pct',0):.1f}%", ACCENT)
        stat_row("Avg Loss %",       f"-{ov.get('avg_loss_pct',0):.1f}%", RED)
        pf = ov.get("profit_factor")
        stat_row("Profit Factor",    f"{pf:.2f}" if pf else "—",
                 ACCENT if (pf or 0) >= 1.3 else (GOLD if (pf or 0) >= 1.0 else RED))
        exp = ov.get("expectancy", 0)
        stat_row("Expectancy",       f"{exp:+.2f}%",
                 ACCENT if exp > 0 else RED)
        dd = ov.get("max_drawdown", 0)
        stat_row("Max Drawdown",     f"{dd:.1f}%",
                 ACCENT if dd <= 10 else (GOLD if dd <= 20 else RED))
        stat_row("Avg Score",        f"{ov.get('avg_score',0):.1f}/100")

        # معايير الانتقال للتداول الحقيقي
        section("معايير التداول الحقيقي")
        criteria = [
            ("50 صفقة مغلقة",    lc.get("trades_ok", False),
             f"{ov.get('total',0)}/50" + (f"  (تحتاج {lc.get('missing',0)} أخرى)" if not lc.get("trades_ok") else "")),
            ("Profit Factor >= 1.3", lc.get("pf_ok", False),
             f"{pf:.2f}" if pf else "—"),
            ("Expectancy > 0",    lc.get("exp_ok", False),
             f"{exp:+.2f}%"),
            ("Max Drawdown <= 20%", lc.get("dd_ok", False),
             f"{dd:.1f}%"),
        ]
        for label, ok, val_txt in criteria:
            stat_row(f"{'✅' if ok else '❌'}  {label}", val_txt,
                     ACCENT if ok else RED)

        ready = lc.get("ready", False)
        tk.Label(inner,
                 text=f"  {'✅ جاهز للتداول الحقيقي — ابدأ بحجم صغير جداً' if ready else '⏳ لم تكتمل المعايير بعد'}  ",
                 bg=ACCENT if ready else GOLD, fg="#000",
                 font=("Segoe UI", 10, "bold")).pack(fill="x", padx=8, pady=8)

        # حسب Mode
        for mode, ms in sorted((report.get("by_mode") or {}).items()):
            if not ms: continue
            section(f"Mode: {mode}")
            stat_row("الصفقات",    ms.get("total", 0))
            stat_row("Win Rate",   f"{ms.get('win_rate',0):.1f}%",
                     ACCENT if ms.get("win_rate",0) >= 50 else RED)
            mpf = ms.get("profit_factor")
            stat_row("P.Factor",   f"{mpf:.2f}" if mpf else "—",
                     ACCENT if (mpf or 0) >= 1.3 else RED)
            stat_row("Expectancy", f"{ms.get('expectancy',0):+.2f}%",
                     ACCENT if ms.get("expectancy",0) > 0 else RED)
            stat_row("MaxDrawdown",f"{ms.get('max_drawdown',0):.1f}%")

        # v3.30 Time-of-Day Study
        by_tb = report.get("by_time_bucket") or {}
        if by_tb:
            section("v3.30 — أداء حسب وقت الدخول")
            for bucket, bs in by_tb.items():
                if not bs:
                    continue
                bpf = bs.get("profit_factor")
                color = ACCENT if (bpf or 0) >= 1.3 and bs.get("expectancy", 0) > 0 else (GOLD if bs.get("expectancy", 0) >= 0 else RED)
                tk.Label(inner, text=f"  {bucket}", bg=BG, fg=color,
                         font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(4,0))
                stat_row(f"  n={bs.get('total',0)}  WR={bs.get('win_rate',0):.0f}%  DD={bs.get('max_drawdown',0):.0f}%",
                         f"  EXP={bs.get('expectancy',0):+.1f}%  PF={bpf:.2f}" if bpf else f"  EXP={bs.get('expectancy',0):+.1f}%",
                         color)

        by_rep = report.get("by_repetition") or {}
        if by_rep:
            section("v3.30 — أول دخول مقابل التكرار")
            for label, rs in by_rep.items():
                if not rs:
                    continue
                rpf = rs.get("profit_factor")
                color = ACCENT if (rpf or 0) >= 1.3 and rs.get("expectancy", 0) > 0 else (GOLD if rs.get("expectancy", 0) >= 0 else RED)
                stat_row(f"{label}  n={rs.get('total',0)}  WR={rs.get('win_rate',0):.0f}%",
                         f"EXP={rs.get('expectancy',0):+.1f}%  PF={rpf:.2f}" if rpf else f"EXP={rs.get('expectancy',0):+.1f}%",
                         color)

        reps = report.get("repeated_setups") or []
        if reps:
            section("v3.30 — أكثر Setups تكراراً")
            for item in reps[:8]:
                ipf = item.get("profit_factor")
                label = item.get("label") or item.get("same_setup_key") or "—"
                tk.Label(inner, text=f"  {label}", bg=BG, fg=TEXT_DIM,
                         font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(4,0))
                stat_row(f"  n={item.get('total',0)}  WR={item.get('win_rate',0):.0f}%",
                         f"EXP={item.get('expectancy',0):+.1f}%  PF={ipf:.2f}" if ipf else f"EXP={item.get('expectancy',0):+.1f}%")

        # حسب Symbol
        section("حسب الرمز")
        for sym, sy in sorted((report.get("by_symbol") or {}).items()):
            if not sy: continue
            spf = sy.get("profit_factor")
            tk.Label(inner, text=f"  {sym}", bg=BG, fg=BLUE,
                     font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(4,0))
            stat_row(f"  n={sy.get('total',0)}  WR={sy.get('win_rate',0):.0f}%",
                     f"  EXP={sy.get('expectancy',0):+.1f}%  PF={spf:.2f}" if spf else "  —")

        # حسب الاستراتيجية
        section("حسب الاستراتيجية")
        disable_list = report.get("disable_list", [])
        for strat, ss in sorted((report.get("by_strategy") or {}).items(),
                                  key=lambda x: (x[1].get("profit_factor") or 0), reverse=True):
            if not ss: continue
            is_bad = strat in disable_list
            spf = ss.get("profit_factor")
            color = RED if is_bad else (ACCENT if (spf or 0) >= 1.3 else GOLD)
            tk.Label(inner,
                     text=f"  {'⚠️ يُنصح بتعطيله: ' if is_bad else ''}{strat}",
                     bg=BG, fg=color,
                     font=("Segoe UI",8,"bold")).pack(anchor="w", padx=16, pady=(4,0))
            stat_row(f"  n={ss.get('total',0)}  WR={ss.get('win_rate',0):.0f}%  DD={ss.get('max_drawdown',0):.0f}%",
                     f"  EXP={ss.get('expectancy',0):+.1f}%  PF={spf:.2f}" if spf else "  —",
                     color)

        if disable_list:
            section("استراتيجيات يُنصح بتعطيلها")
            for d in disable_list:
                stat_row(d, "PF < 0.8 + EXP < 0 بعد 10+ صفقات", RED)

        # حسب Setup Quality
        by_q = report.get("by_quality", {})
        if by_q:
            section("أداء حسب Setup Quality")
            q_colors = {"Good": ACCENT, "Acceptable": GOLD,
                        "Weak": RED, "Unrealistic RR": "#ff8800"}
            for q, qs in by_q.items():
                if not qs:
                    continue
                qc  = q_colors.get(q, TEXT)
                qpf = qs.get("profit_factor")
                tk.Label(inner, text=f"  [{q}]",
                         bg=BG, fg=qc,
                         font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=16, pady=(6,0))
                stat_row(f"  n={qs.get('total',0)}  WR={qs.get('win_rate',0):.0f}%",
                         f"  PF={qpf:.2f}  EXP={qs.get('expectancy',0):+.1f}%" if qpf
                         else f"  EXP={qs.get('expectancy',0):+.1f}%", qc)
                note = qs.get("calibration_note", "")
                if note:
                    tk.Label(inner, text=f"    → {note}",
                             bg=BG, fg=TEXT_DIM,
                             font=("Segoe UI", 8)).pack(anchor="w", padx=24, pady=(0,2))

        section("خلاصة")
        stat_row("أفضل استراتيجية",  report.get("best_strategy","—"),  ACCENT)
        stat_row("أسوأ استراتيجية",  report.get("worst_strategy","—"), RED)

        tk.Button(inner, text="إغلاق", bg=CARD, fg=TEXT, font=("Segoe UI",9),
                  relief="flat", cursor="hand2", padx=20,
                  command=win.destroy).pack(pady=14)

    # ── Backtest Tab ─────────────────────────────────────────
    def _build_backtest_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  📈 Backtest  ")

        toolbar = tk.Frame(frame, bg=BG2, height=48)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)
        tk.Label(toolbar, text="أداء الاستراتيجيات من سجل التحليلات والصفقات",
                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=14)
        tk.Button(toolbar, text="🔄 تحديث", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._refresh_backtest, padx=12).pack(side="right", padx=10, pady=10)

        body = self._scrollable_body(frame)

        # بطاقات الملخص
        self.bt_cards_frame = tk.Frame(body, bg=BG)
        self.bt_cards_frame.pack(fill="x", pady=(0, 8))

        # جدول الأداء حسب الاستراتيجية
        tk.Label(body, text="أداء كل استراتيجية", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 2))

        cols = ("الاستراتيجية", "عدد الصفقات", "الربح", "الخسارة", "نسبة النجاح %", "صافي R")
        self.bt_tree = ttk.Treeview(body, columns=cols, show="headings", height=8)
        for col, width in zip(cols, [160, 110, 80, 80, 130, 100]):
            self.bt_tree.heading(col, text=col, anchor="center")
            self.bt_tree.column(col, width=width, anchor="center")
        self.bt_tree.tag_configure("positive", foreground=ACCENT)
        self.bt_tree.tag_configure("negative", foreground=RED)
        self.bt_tree.pack(fill="x", pady=(0, 12))

        # رسم بياني نصي لـ IV Rank المخزّن
        tk.Label(body, text="سجل IV Rank التاريخي (آخر 30 يوم)", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 2))
        self.iv_history_text = tk.Text(body, bg=BG3, fg=BLUE, relief="flat",
                                       font=("Consolas", 9), height=6, wrap="word")
        self.iv_history_text.pack(fill="x")
        self.iv_history_text.config(state="disabled")

        self._refresh_backtest()

    def _refresh_backtest(self):
        data = get_backtest_data()
        trades = data.get("trades", [])

        # احذف البطاقات القديمة
        for w in self.bt_cards_frame.winfo_children():
            w.destroy()

        # ملخص عام
        total   = len(trades)
        wins    = sum(1 for t in trades if t.get("result") in ("ربح", "ربح جزئي"))
        losses  = sum(1 for t in trades if t.get("result") == "خسارة")
        net_r   = sum(t.get("r_value", 0) for t in trades)
        win_rate = round(wins / total * 100, 1) if total else 0

        for label, value, color in [
            ("إجمالي الصفقات", str(total),   WHITE),
            ("ربح",            str(wins),    ACCENT),
            ("خسارة",          str(losses),  RED),
            ("نسبة النجاح",    f"{win_rate}%", GOLD),
            ("صافي R",         f"{net_r:+.2f}R", ACCENT if net_r >= 0 else RED),
        ]:
            card = tk.Frame(self.bt_cards_frame, bg=CARD, relief="flat")
            card.pack(side="left", padx=4, pady=2, ipadx=14, ipady=6)
            tk.Label(card, text=label, bg=CARD, fg=TEXT_DIM, font=("Segoe UI", 8)).pack()
            tk.Label(card, text=value, bg=CARD, fg=color, font=("Segoe UI", 14, "bold")).pack()

        # أداء حسب الاستراتيجية
        for row in self.bt_tree.get_children():
            self.bt_tree.delete(row)

        strategy_stats: dict = {}
        for t in trades:
            s = t.get("strategy", "Unknown")
            if s not in strategy_stats:
                strategy_stats[s] = {"wins": 0, "losses": 0, "net_r": 0.0}
            r = t.get("result", "")
            if r in ("ربح", "ربح جزئي"):
                strategy_stats[s]["wins"] += 1
            else:
                strategy_stats[s]["losses"] += 1
            strategy_stats[s]["net_r"] += t.get("r_value", 0)

        for s_name, st in sorted(strategy_stats.items(), key=lambda x: x[1]["net_r"], reverse=True):
            total_s = st["wins"] + st["losses"]
            wr = round(st["wins"] / total_s * 100, 1) if total_s else 0
            net = st["net_r"]
            tag = "positive" if net >= 0 else "negative"
            self.bt_tree.insert("", "end", tags=(tag,), values=(
                s_name, total_s, st["wins"], st["losses"],
                f"{wr}%", f"{net:+.2f}R"
            ))

        # سجل IV Rank
        self.iv_history_text.config(state="normal")
        self.iv_history_text.delete("1.0", "end")
        try:
            from core.database import get_iv_history
            iv_records = get_iv_history(30)
            if iv_records:
                lines = []
                for r in reversed(iv_records[:30]):
                    bar_len = int((r.get("atm_iv") or 0) / 2)
                    bar = "█" * min(bar_len, 50)
                    lines.append(f"{r['date']}  IV={r.get('atm_iv', 0):5.1f}%  VIX={r.get('vix', 0):5.2f}  {bar}")
                self.iv_history_text.insert("1.0", "\n".join(lines))
            else:
                self.iv_history_text.insert("1.0", "لا يوجد سجل IV بعد — شغّل تحليلاً كاملاً لبدء تخزين البيانات.")
        except Exception as e:
            self.iv_history_text.insert("1.0", f"خطأ: {e}")
        self.iv_history_text.config(state="disabled")

    # ── Swing Diagnostics Tab ────────────────────────────────
    def _build_swing_diag_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  📊 Swing Diag  ")

        # Toolbar
        tb = tk.Frame(frame, bg=BG2, height=48)
        tb.pack(fill="x"); tb.pack_propagate(False)
        tk.Label(tb, text="Swing Diagnostics — تشخيص قرارات Swing Paper",
                 bg=BG2, fg=TEXT_DIM, font=("Segoe UI", 9)).pack(side="left", padx=14)
        tk.Button(tb, text="🔄 تحديث", bg=CARD, fg=TEXT, font=("Segoe UI", 9),
                  relief="flat", cursor="hand2",
                  command=self._refresh_swing_diag).pack(side="right", padx=10, pady=10)

        body = self._scrollable_body(frame)

        # Last update label
        self._swing_diag_time = tk.Label(body, text="لم يتم التحديث بعد",
                                          bg=BG, fg=TEXT_DIM, font=("Segoe UI", 8))
        self._swing_diag_time.pack(anchor="e", padx=14, pady=(4, 0))

        # Cards container
        self._swing_diag_cards = tk.Frame(body, bg=BG)
        self._swing_diag_cards.pack(fill="x", padx=10, pady=8)

        # Initial placeholder
        tk.Label(self._swing_diag_cards,
                 text="اضغط 🔄 تحديث لرؤية تشخيص Swing",
                 bg=BG, fg=TEXT_DIM, font=("Segoe UI", 11)).pack(pady=40)

        self._swing_diag_frame = body

    def _refresh_swing_diag(self):
        """يجلب analyze_swing لـ SPY/QQQ/IWM/DIA ويعرض النتيجة."""
        from datetime import datetime

        # RC14 — معرّف دورة فريد يشمل microseconds — يضمن الفرادة حتى عند الضغط السريع
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")

        def worker():
            try:
                from core.trade_monitor import (
                    _update_swing_cache_bg, get_swing_cache,
                    _swing_cache_loading
                )
                _update_swing_cache_bg()
                cache = get_swing_cache()
                data  = cache["data"]

                results = {
                    "_cache_age":     cache["age_seconds"],
                    "_cache_last_ok": cache["last_ok"],
                    "_cache_error":   cache["error"],
                    "_cache_loading": cache["loading"],
                    "_cache_stale":   cache["stale"],
                    "_run_id":        run_id,
                }

                swing_symbols = ("SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD")
                # RC14 — سجّل نتيجة D/S لكل رمز في هذه الدورة (replace on update)
                run_record: dict = {}
                for sym in swing_symbols:
                    sym_data = data.get(sym, {
                        "qualified": False, "score": 0,
                        "rejected": ["لم تُجلب بيانات"],
                        "reasons": [], "trend_4h": "—",
                        "iv_rank": 0, "dte": 0,
                    })
                    results[sym] = sym_data
                    run_record[sym] = {
                        "ds_evaluation":         sym_data.get("ds_evaluation")         or "NOT_EVALUATED",
                        "ds_unavailable_reason": sym_data.get("ds_unavailable_reason") or None,
                        "pipeline_stop_code":    sym_data.get("pipeline_stop_code")    or "",
                    }

                # SPX دائماً NOT_EVALUATED بسبب SPX Guard
                results["SPX"] = {
                    "qualified": False, "score": 0,
                    "rejected": ["Swing disabled — SPX = 0DTE فقط"],
                    "reasons": [], "trend_4h": "—",
                    "iv_rank": 0, "dte": 0,
                }
                run_record["SPX"] = {
                    "ds_evaluation": "NOT_EVALUATED",
                    "ds_unavailable_reason": None,
                    "pipeline_stop_code": "STOPPED_AT_SPX_GUARD",
                }

                # حفظ سجل الدورة تحت Lock — ثم انسخ للحساب خارجه
                with self._session_lock:
                    self._session_ds_results[run_id] = run_record
                    # FIFO eviction — احتفظ بآخر 100 دورة فقط
                    while len(self._session_ds_results) > 100:
                        self._session_ds_results.popitem(last=False)
                    # نسخة فورية لمنع تجميد main thread أثناء الحساب
                    run_record_copy = dict(self._session_ds_results.get(run_id, {}))

                # الحساب بعد تحرير الـ Lock
                results["_session_summary"] = self._compute_session_summary(run_id, run_record_copy)

                self.root.after(0, lambda: self._render_swing_diag(results))
            except Exception as e:
                self.root.after(0, lambda: self._swing_diag_time.config(
                    text=f"خطأ: {e}", fg=RED))

        threading.Thread(target=worker, daemon=True).start()
        self._swing_diag_time.config(text="جاري التحديث...", fg=GOLD)

    def _compute_session_summary(self, run_id: str, record: dict) -> dict:
        """
        يحسب إحصاءات D/S Coverage من نسخة run_record الممررة مباشرة.
        يُستدعى بعد تحرير _session_lock — لا يمس _session_ds_results مباشرة.
        يستبعد SPX (STOPPED_AT_SPX_GUARD) و NOT_EVALUATED من denominator الـ Coverage.
        """

        counts = {
            "PASSED":        0,
            "NO_ACTIVE_ZONE":0,
            "WARNING_ZONE":  0,
            "HARD_REJECTED": 0,
            "UNAVAILABLE":   0,
            "NOT_EVALUATED": 0,
        }
        unavail_reasons: dict = {}
        total_symbols = 0

        for sym, r in record.items():
            ds = r.get("ds_evaluation", "NOT_EVALUATED")
            reason = r.get("ds_unavailable_reason")
            pipe   = r.get("pipeline_stop_code", "")

            # استبعد SPX من الإحصاءات
            if pipe == "STOPPED_AT_SPX_GUARD":
                continue

            total_symbols += 1
            bucket = ds if ds in counts else "NOT_EVALUATED"
            counts[bucket] += 1

            if ds == "UNAVAILABLE" and reason:
                unavail_reasons[reason] = unavail_reasons.get(reason, 0) + 1

        successful_evals = (counts["PASSED"] + counts["NO_ACTIVE_ZONE"] +
                            counts["WARNING_ZONE"] + counts["HARD_REJECTED"])
        denominator = successful_evals + counts["UNAVAILABLE"]

        coverage_pct = round(successful_evals / denominator * 100, 1) if denominator > 0 else None
        strong_rate_pct = round(
            (counts["PASSED"] + counts["NO_ACTIVE_ZONE"] + counts["HARD_REJECTED"]) /
            successful_evals * 100, 1
        ) if successful_evals > 0 else None

        return {
            "run_id":           run_id,
            "total_symbols":    total_symbols,
            "counts":           counts,
            "successful_evals": successful_evals,
            "unavail_reasons":  unavail_reasons,
            "coverage_pct":     coverage_pct,
            "strong_rate_pct":  strong_rate_pct,
        }

    def _render_swing_diag(self, results: dict):
        """يرسم بطاقة تشخيص لكل رمز مع معلومات Cache + لوحة Swing SMC على اليمين."""
        # امسح القديم
        for w in self._swing_diag_cards.winfo_children():
            w.destroy()

        # ── شريط حالة الـ Cache ───────────────────────────────────────────────
        cache_age   = results.get("_cache_age", 0)
        cache_ok_t  = results.get("_cache_last_ok", "—")
        cache_err   = results.get("_cache_error", "")
        cache_load  = results.get("_cache_loading", False)
        cache_stale = results.get("_cache_stale", False)

        if cache_load:
            cache_txt = "Cache: جاري التحميل..."
            cache_fg  = GOLD
        elif cache_stale:
            cache_txt = f"Cache: قديمة ({round(cache_age/60,1)} دق) — قد لا تفتح صفقات"
            cache_fg  = RED
        elif cache_err:
            cache_txt = f"Cache: تحديث جزئي | آخر نجاح: {cache_ok_t}"
            cache_fg  = GOLD
        else:
            cache_txt = f"Cache: {round(cache_age/60,1)} دق | آخر تحديث: {cache_ok_t}"
            cache_fg  = TEXT_DIM

        self._swing_diag_time.config(text=cache_txt, fg=cache_fg)

        if cache_err:
            err_lbl = tk.Label(
                self._swing_diag_cards,
                text=f"⚠️ {cache_err}",
                bg=BG,
                fg=GOLD,
                font=("Segoe UI", 8),
                wraplength=900,
                anchor="w",
                justify="left",
            )
            err_lbl.pack(anchor="w", padx=14, pady=(0, 4))

        symbol_order = ["SPX", "SPY", "QQQ", "IWM", "DIA", "AAPL", "NVDA", "GLD"]
        for sym in symbol_order:
            data = results.get(sym, {})
            loading      = data.get("loading", False)
            qualified    = data.get("qualified", False)
            with_warning = data.get("qualified_with_warning", False)
            score        = data.get("score", 0)
            trend        = data.get("trend_4h", "—")
            iv_rank      = data.get("iv_rank", 0)
            dte          = data.get("dte", 0)
            rejected     = list(data.get("rejected", []))
            event_info   = data.get("event_info", {})
            reasons      = data.get("reasons", [])
            strategy     = (data.get("strategy") or {})
            strat_name   = strategy.get("strategy", "—")

            # RC14 — سلسلة القرار النهائي (من الأعلى للأدنى أولوية):
            # 1) analyze_swing() final status — المصدر الأكثر موثوقية
            _final_status = str(data.get("status") or "")
            if qualified and _final_status == "Rejected":
                qualified    = False
                with_warning = False
                _st_rej = strategy.get("reject_reason") or data.get("reject_reason") or "Rejected by analysis pipeline"
                if _st_rej not in rejected:
                    rejected.append(_st_rej)
            # 2) strategy flags — تغطية صريحة لـ no_trade / smc_swing_block
            if qualified and (strategy.get("no_trade") or strategy.get("smc_swing_block")):
                qualified    = False
                with_warning = False
                _fl_rej = strategy.get("reject_reason") or "Swing rejected by SMC/D-S filter"
                if _fl_rej not in rejected:
                    rejected.append(_fl_rej)
            smc_filter   = strategy.get("smc_swing_filter") or {}
            smc_context  = strategy.get("smc_0dte_mtf") or {}
            ds_1h_ctx    = strategy.get("swing_ds_1h") or {}
            ds_15m_ctx   = strategy.get("swing_ds_15m") or {}

            # RC15f final Swing eligibility — distinguish base qualification from final Swing registration eligibility.
            rc15f_diag = strategy.get("rc15f_swing_confirmation") or {}
            ict_score = strategy.get("ict_smc_score")
            if ict_score is None and isinstance(smc_context, dict):
                ict_score = smc_context.get("ict_smc_score")
            try:
                ict_score_int = int(ict_score) if ict_score is not None else None
            except Exception:
                ict_score_int = None
            ict_conf = strategy.get("ict_smc_confidence") or (smc_context.get("ict_smc_confidence") if isinstance(smc_context, dict) else None)
            ema_pass_val = strategy.get("ema_alignment_pass")
            if ema_pass_val is None and isinstance(smc_context, dict):
                ema_pass_val = smc_context.get("ema_alignment_pass")
            rc15f_not_applicable = bool(rc15f_diag.get("not_applicable") or strategy.get("rc15f_not_applicable"))
            rc15f_has_data = bool(rc15f_diag) or ict_score_int is not None or ema_pass_val is not None
            rc15f_block_reason = strategy.get("swing_block_reason") or rc15f_diag.get("swing_block_reason")
            rc15f_watch_reason = strategy.get("swing_watchlist_reason") or rc15f_diag.get("swing_watchlist_reason")
            rc15f_allowed = bool((not rc15f_not_applicable) and rc15f_has_data and ict_score_int is not None and ict_score_int >= 4 and bool(ema_pass_val) and not rc15f_block_reason and not rc15f_watch_reason)

            # لون البطاقة
            if sym == "SPX":
                card_color = BG2
                status_txt = "⛔ Swing Disabled — 0DTE فقط"
                status_fg  = TEXT_DIM
            elif loading:
                card_color = BG2
                status_txt = "⏳ Swing Status: Loading..."
                status_fg  = GOLD
            elif qualified and rc15f_not_applicable:
                card_color = "#2a2a0a"
                status_txt = f"ℹ️ Base Qualified — RC15f NOT APPLICABLE — {strat_name}"
                status_fg  = GOLD
            elif qualified and rc15f_allowed and with_warning:
                card_color = "#2a2a0a"
                _conf_txt = f"RC15f {ict_score_int}/5 {ict_conf or ''}".strip()
                status_txt = f"⚠️ Swing Allowed — {_conf_txt} — {strat_name}"
                status_fg  = GOLD
            elif qualified and rc15f_allowed:
                card_color = "#1a2e1a"
                _conf_txt = f"RC15f {ict_score_int}/5 {ict_conf or ''}".strip()
                status_txt = f"✅ Swing Allowed — {_conf_txt} — {strat_name}"
                status_fg  = "#4caf50"
            elif strategy and rc15f_watch_reason:
                card_color = "#2a2a0a"
                status_txt = f"👁 Base Qualified — RC15f WATCHLIST — {strat_name}"
                status_fg  = GOLD
            elif strategy and rc15f_block_reason:
                card_color = "#2e1a1a"
                status_txt = f"⛔ Base Qualified — RC15f BLOCKED — {strat_name}"
                status_fg  = RED
            elif qualified and not rc15f_has_data:
                card_color = "#2a2a0a"
                status_txt = f"⚠️ Base Qualified — RC15f NOT SHOWN — {strat_name}"
                status_fg  = GOLD
            elif qualified and with_warning:
                card_color = "#2a2a0a"
                status_txt = f"⚠️ Qualified with Warning — {strat_name}"
                status_fg  = GOLD
            elif qualified:
                card_color = "#1a2e1a"
                status_txt = f"✅ Qualified — {strat_name}"
                status_fg  = "#4caf50"
            else:
                card_color = "#2e1a1a"
                status_txt = "❌ Rejected"
                status_fg  = RED

            card = tk.Frame(self._swing_diag_cards, bg=card_color, relief="flat", bd=0)
            card.pack(fill="x", padx=4, pady=6)

            hdr = tk.Frame(card, bg=card_color)
            hdr.pack(fill="x", padx=12, pady=(10, 4))
            tk.Label(hdr, text=f"  {sym}", bg=card_color, fg=TEXT,
                     font=("Segoe UI", 13, "bold")).pack(side="left")
            tk.Label(hdr, text=status_txt, bg=card_color, fg=status_fg,
                     font=("Segoe UI", 10, "bold")).pack(side="right")

            tk.Frame(card, bg=CARD, height=1).pack(fill="x", padx=12)

            content = tk.Frame(card, bg=card_color)
            content.pack(fill="x", padx=16, pady=8)
            left = tk.Frame(content, bg=card_color)
            left.pack(side="left", fill="both", expand=True, padx=(0, 10), anchor="n")
            right = tk.Frame(content, bg=card_color)
            right.pack(side="left", fill="both", expand=True, padx=(10, 0), anchor="n")

            def row(parent, label, val, fg=TEXT_DIM, value_font=("Segoe UI", 9, "bold"), wrap=460):
                r = tk.Frame(parent, bg=card_color)
                r.pack(fill="x", pady=1, anchor="n")
                tk.Label(r, text=label, bg=card_color, fg=TEXT_DIM,
                         font=("Segoe UI", 8), width=18, anchor="w").pack(side="left")
                tk.Label(r, text=str(val), bg=card_color, fg=fg,
                         font=value_font, anchor="w", justify="left",
                         wraplength=wrap).pack(side="left", fill="x", expand=True)

            def section(parent, title, fg=TEXT_DIM):
                tk.Frame(parent, bg=CARD, height=1).pack(fill="x", pady=(6, 2))
                tk.Label(parent, text=title, bg=card_color, fg=fg,
                         font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(0, 2))

            def bullets(parent, title, items, title_fg, text_fg):
                if not items:
                    return
                tk.Label(parent, text=title, bg=card_color, fg=title_fg,
                         font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(6, 1))
                for item in items:
                    tk.Label(parent, text=f"  • {item}", bg=card_color, fg=text_fg,
                             font=("Segoe UI", 8), wraplength=560,
                             justify="left", anchor="w").pack(anchor="w")

            def fmt_zone(zone: dict | None) -> str:
                if not isinstance(zone, dict) or not zone:
                    return "—"
                low = zone.get("low", "?")
                high = zone.get("high", "?")
                dist = zone.get("distance_pct")
                dist_txt = f"{dist:.2f}%" if isinstance(dist, (int, float)) else "?"
                conf = zone.get("confidence", "?")
                age = zone.get("age_bars", "?")
                max_age = zone.get("max_age_bars", "?")
                status = zone.get("status", "?")
                inside = "inside" if zone.get("inside") else "outside"
                return f"{low}-{high} | dist={dist_txt} | conf={conf} | age={age}/{max_age} | {status} | {inside}"

            def bool_txt(v) -> str:
                return "YES" if v else "NO"

            # ── RC14: pipeline codes من نتيجة analyze_swing ──────────────────
            _pipe_stop   = data.get("pipeline_stop_code") or ""
            _smc_eval    = data.get("smc_evaluation")     or ""
            _ds_eval     = data.get("ds_evaluation")      or ""
            _ds_reason   = data.get("ds_unavailable_reason") or ""
            _ep          = data.get("entry_protection")   or ""
            _diag_code   = data.get("diagnostic_code")    or ""

            # ألوان الحالات
            def _eval_color(val: str) -> str:
                if val in ("PASSED", "SMC_EVALUATED", "NO_ACTIVE_ZONE"):
                    return ACCENT
                if val in ("PASSED_WITH_WARNING", "WARNING_ZONE", "DS_WARNING_ZONE", "DS_NO_ACTIVE_ZONE"):
                    return GOLD
                if val in ("FAILED", "HARD_REJECTED", "SMC_CONFLICT", "DS_CONFLICT",
                           "REJECTED_BY_SMC", "REJECTED_BY_DS"):
                    return RED
                if val in ("NOT_EVALUATED", "SMC_NOT_EVALUATED"):
                    return TEXT_DIM
                if val in ("UNAVAILABLE", "DS_UNAVAILABLE", "DS_NOT_EVALUATED"):
                    return GOLD
                return TEXT_DIM

            # سبب Not evaluated الدقيق من pipeline_stop_code
            _not_eval_reason_map = {
                "STOPPED_AT_4H_TREND":      "Stopped at 4H trend filter",
                "STOPPED_AT_MANUAL_BLOCK":  "Stopped: manual block active",
                "STOPPED_AT_SPX_GUARD":     "SPX = 0DTE only",
                "PRICE_UNAVAILABLE":        "Stopped: price fetch failed",
                "CHAIN_UNAVAILABLE":        "Stopped: option chain fetch failed",
                "NO_VALID_EXPIRY":          "Stopped: no valid expiry in DTE range",
                "NO_STRATEGY_CANDIDATE":    "Stopped: strategy engine no candidate",
                "REJECTED_BY_DELTA":        "SMC not evaluated — candidate rejected by Delta filter",
                "REJECTED_BY_LIQUIDITY":    "SMC not evaluated — rejected by Liquidity filter",
                "REJECTED_BY_SCORE":        "SMC not evaluated — score below threshold",
                "REJECTED_BY_EVENT":        "SMC not evaluated — rejected by Event filter",
            }

            def smc_eval_summary() -> tuple[str, str]:
                if sym == "SPX":
                    return ("غير مطبق", TEXT_DIM)
                if not smc_filter.get("applied"):
                    return ("غير مقيم بعد", TEXT_DIM)
                final_action = str(smc_filter.get("final_action") or "")
                if final_action.startswith("REJECTED_SWING_") or strategy.get("smc_swing_block"):
                    return ("NO — rejected by SMC/Demand-Supply", RED)
                if smc_filter.get("ds_warning"):
                    return ("WARNING — passed with Demand/Supply warning", GOLD)
                if qualified:
                    return ("YES — passed SMC Swing filter", ACCENT)
                return ("PASSED FILTER — not selected by other rules", TEXT_DIM)

            # ── العمود الأيسر: القرار الأساسي وتفاصيل الصفقة ───────────────────
            if sym != "SPX":
                trend_color = ("#4caf50" if "bullish" in trend else RED if "bearish" in trend else TEXT_DIM)
                row(left, "Trend 4H", trend, trend_color)
                trend_src = data.get("trend_source") or "—"
                trend_price_src = data.get("trend_price_source") or "—"
                trend_ema_src = data.get("trend_ema_source") or "—"
                trend_updated_at = data.get("trend_updated_at") or "—"
                trend_live_err = data.get("trend_live_price_error") or ""
                trend_yahoo_proxy = data.get("trend_yahoo_proxy_price")
                trend_ticker = data.get("trend_ticker") or sym
                trend_price = data.get("trend_price") or data.get("price") or "—"
                ema20v = data.get("ema20_4h") or "—"
                ema50v = data.get("ema50_4h") or "—"
                raw_bars = data.get("trend_raw_bars")
                bars_4h = data.get("trend_4h_bars")
                pvsema = data.get("price_vs_ema20_pct")
                pvsema50 = data.get("price_vs_ema50_pct")
                emaspread = data.get("ema20_vs_ema50_pct")
                ema_order = data.get("ema_order")
                pressure_label = data.get("trend_pressure_label")
                confirmed_trend = data.get("confirmed_trend")
                strength_reason = data.get("trend_strength_reason")
                neutral_reason = data.get("trend_neutral_reason") or data.get("trend_error") or ""

                row(left, "Trend Source", f"{trend_src} | {trend_ticker}")
                row(left, "Price Source", trend_price_src)
                row(left, "EMA Source", trend_ema_src)
                row(left, "Updated", trend_updated_at)
                row(left, "Trend Price", trend_price)
                if trend_yahoo_proxy is not None and str(trend_price_src).lower().startswith("dxlink"):
                    row(left, "Yahoo 4H Proxy", trend_yahoo_proxy, TEXT_DIM)
                if trend_live_err and str(trend_price_src).startswith("Yahoo"):
                    row(left, "Live Price Note", trend_live_err, GOLD)
                row(left, "EMA20 / EMA50", f"{ema20v} / {ema50v}")
                if ema_order:
                    row(left, "EMA Order", ema_order)
                if confirmed_trend is not None:
                    row(left, "Confirmed Trend", bool_txt(confirmed_trend), ACCENT if confirmed_trend else GOLD)
                if pressure_label:
                    row(left, "Pressure Label", pressure_label, GOLD if pressure_label else TEXT_DIM)
                if pvsema is not None:
                    row(left, "Price vs EMA20", f"{pvsema:+.3f}%", ACCENT if abs(float(pvsema)) >= 0.30 else TEXT_DIM)
                if pvsema50 is not None:
                    row(left, "Price vs EMA50", f"{pvsema50:+.3f}%", ACCENT if abs(float(pvsema50)) >= 0.30 else TEXT_DIM)
                if emaspread is not None:
                    row(left, "EMA20 vs EMA50", f"{emaspread:+.3f}%", ACCENT if abs(float(emaspread)) > 0.2 else TEXT_DIM)
                if raw_bars is not None or bars_4h is not None:
                    row(left, "Bars 1H / 4H", f"{raw_bars or 0} / {bars_4h or 0}")
                if strength_reason:
                    row(left, "Trend Reason", strength_reason, ACCENT if confirmed_trend else GOLD, wrap=520)
                elif neutral_reason:
                    row(left, "Neutral Reason", neutral_reason, GOLD, wrap=520)

                row(left, "Score", f"{score}/100", ACCENT if score >= 40 else TEXT_DIM)
                row(left, "IV Rank", f"{iv_rank:.1f}" if iv_rank else "—")
                _iv_pct = data.get("iv_percentile")
                _iv_pct_reg = data.get("iv_percentile_for_regime")
                _iv_proxy = data.get("iv_regime_proxy_used", False)
                _iv_regime = data.get("iv_regime", "Unknown")
                _iv_src = data.get("iv_rank_source") or "—"
                _regime_note = (data.get("iv_regime_data") or {}).get("regime_note", "")
                _iv_pct_txt = f"{_iv_pct:.1f}%" if _iv_pct is not None else "unavailable"
                if _iv_proxy and _iv_pct_reg is not None:
                    _iv_pct_txt += f"  (proxy: Rank={_iv_pct_reg:.1f})"
                row(left, "IV Percentile", _iv_pct_txt,
                    TEXT_DIM if _iv_pct is None else (ACCENT if _iv_pct >= 60 else "#4caf50"), wrap=520)
                _regime_color = ACCENT if "Credit" in _iv_regime else ("#4caf50" if "Debit" in _iv_regime else TEXT_DIM)
                row(left, "IV Regime", _iv_regime + (f"  [{_regime_note}]" if _regime_note else ""), _regime_color, wrap=520)
                row(left, "IV Source", _iv_src[:100], TEXT_DIM, wrap=520)
                row(left, "DTE", f"{dte} أيام" if dte else "—")
                row(left, "Strategy", strat_name if strat_name != "—" else "—", ACCENT if strat_name != "—" else TEXT_DIM)

                if qualified and strategy:
                    section(left, "تفاصيل الصفقة:", ACCENT)
                    em_val = strategy.get("expected_move") or data.get("swing_em", 0)
                    sigma = 1.5
                    if em_val:
                        row(left, "Expected Move", f"{em_val:.1f}")
                        row(left, "Sigma (1.5x)", f"{em_val * sigma:.1f}")

                    lc = strategy.get("long_call"); sc2 = strategy.get("short_call")
                    lp = strategy.get("long_put");  sp2 = strategy.get("short_put")
                    if lc and sc2:
                        row(left, "Long Call", f"{lc:,.0f}", "#4caf50")
                        row(left, "Short Call", f"{sc2:,.0f}", RED)
                    if lp and sp2:
                        row(left, "Long Put", f"{lp:,.0f}", "#4caf50")
                        row(left, "Short Put", f"{sp2:,.0f}", RED)

                    legs_d = strategy.get("legs_detail") or []
                    long_leg = next((o for o in legs_d if o and o.get("delta") and abs(o.get("delta", 0)) > 0.1), None)
                    if long_leg:
                        delta_v = abs(long_leg.get("delta", 0))
                        row(left, "Delta (Long)", f"{delta_v:.2f}", ACCENT)
                        row(left, "POP ~", f"{round(delta_v * 100):.0f}%", ACCENT)

                    debit = strategy.get("debit")
                    credit = strategy.get("credit")
                    ml = strategy.get("max_loss")
                    mg = strategy.get("max_gain")
                    rr = strategy.get("reward_risk")
                    if debit:
                        row(left, "Max Loss", f"${debit * 100:.0f}", RED)
                        if mg:
                            row(left, "Max Profit", f"${mg * 100:.0f}", "#4caf50")
                    elif credit:
                        row(left, "Max Profit", f"${credit * 100:.0f}", "#4caf50")
                        if ml:
                            row(left, "Max Loss", f"${ml * 100:.0f}", RED)
                    if rr:
                        row(left, "Risk/Reward", f"{rr:.2f}", ACCENT)

                    if event_info:
                        section(left, "حدث اقتصادي قادم:", RED if (event_info.get("days", 0) <= 3) else GOLD)
                        ev_days = event_info.get("days", 0)
                        ev_name = event_info.get("name", "")
                        ev_date = event_info.get("date", "")
                        ev_pen = event_info.get("penalty", 0)
                        ev_color = RED if ev_days <= 3 else GOLD if ev_days <= 7 else TEXT_DIM
                        row(left, "الحدث", ev_name, ev_color)
                        row(left, "التاريخ", ev_date)
                        row(left, "متبقي", f"{ev_days} أيام", ev_color)
                        row(left, "تأثير Score", f"{ev_pen:+d} نقطة", RED if ev_pen < 0 else TEXT_DIM)
                        row(left, "الحالة", "Qualified with Warning" if with_warning else status_txt.replace('⚠️ ','').replace('✅ ','').replace('❌ ',''), GOLD if with_warning else ("#4caf50" if qualified else RED), wrap=520)

                    breakdown = strategy.get("score_breakdown") or {}
                    if breakdown:
                        section(left, "Score Breakdown:", TEXT_DIM)
                        for k, v in breakdown.items():
                            color = "#4caf50" if v > 0 else RED if v < 0 else TEXT_DIM
                            row(left, f"  {k}", f"{v:+d}", color)
            else:
                bullets(left, "⚠️ أسباب الرفض:", ["Swing disabled — SPX = 0DTE فقط"], RED, "#ff6b6b")

            bullets(left, "✅ أسباب القبول:", reasons[:6], "#4caf50", TEXT_DIM)
            bullets(left, "⚠️ أسباب الرفض:", rejected, RED, "#ff6b6b")

            # ── العمود الأيمن: Swing SMC + Demand/Supply Diagnostics ───────────
            panel = tk.Frame(right, bg=card_color)
            panel.pack(fill="both", expand=True, anchor="n")
            section(panel, "Swing SMC / Demand-Supply Diagnostics", BLUE)

            # ── RC15f: Final Swing registration eligibility ──────────────────
            section(panel, "RC15f Swing ICT/SMC + EMA", BLUE)
            if sym == "SPX":
                row(panel, "RC15f Decision", "NOT_APPLICABLE — SPX is 0DTE only", TEXT_DIM, wrap=520)
            elif rc15f_has_data:
                _rc_decision = rc15f_diag.get("decision") or ("ALLOWED" if rc15f_allowed else "BLOCKED")
                if rc15f_not_applicable:
                    _rc_color = TEXT_DIM
                    row(panel, "Final Eligibility", _rc_decision, _rc_color, wrap=520)
                    row(panel, "RC15f Scope", "NOT_APPLICABLE — directional Swing Debit only", _rc_color, wrap=520)
                    row(panel, "RC15f Reason", rc15f_diag.get("reason") or "RC15f applies only to Swing Call Debit Spread / Put Debit Spread", _rc_color, wrap=520)
                else:
                    _rc_color = ACCENT if rc15f_allowed else (GOLD if rc15f_watch_reason else RED)
                    row(panel, "Final Eligibility", _rc_decision, _rc_color, wrap=520)
                    row(panel, "ICT/SMC Score", f"{ict_score_int if ict_score_int is not None else 'N/A'}/5", _rc_color, wrap=520)
                    row(panel, "ICT Confidence", ict_conf or "—", _rc_color, wrap=520)
                    row(panel, "EMA Alignment", bool_txt(bool(ema_pass_val)), ACCENT if ema_pass_val else RED, wrap=520)
                    _rc_reason = rc15f_block_reason or rc15f_watch_reason or "Swing allowed by RC15f"
                    row(panel, "RC15f Reason", _rc_reason, TEXT_DIM if rc15f_allowed else _rc_color, wrap=520)

                _ict_d = strategy.get("ict_smc_details") or (smc_context.get("ict_smc_details") if isinstance(smc_context, dict) else {}) or {}
                _ema_d = strategy.get("ema_details") or (smc_context.get("ema_details") if isinstance(smc_context, dict) else {}) or {}
                _checks = _ict_d.get("checks") or _ict_d.get("details") or {}
                if isinstance(_checks, dict) and _checks:
                    row(panel, "Premium/Discount", bool_txt(bool(_checks.get("premium_discount") or _checks.get("premium_discount_pass"))), TEXT_DIM, wrap=520)
                    row(panel, "Liquidity Sweep", bool_txt(bool(_checks.get("liquidity_sweep") or _checks.get("liquidity_sweep_pass"))), TEXT_DIM, wrap=520)
                    row(panel, "MSS after Sweep", bool_txt(bool(_checks.get("mss") or _checks.get("mss_pass"))), TEXT_DIM, wrap=520)
                    row(panel, "Displacement/FVG", bool_txt(bool(_checks.get("displacement_fvg") or _checks.get("displacement_fvg_pass"))), TEXT_DIM, wrap=520)
                    row(panel, "FVG/OB Retest", bool_txt(bool(_checks.get("fvg_ob_retest") or _checks.get("retest") or _checks.get("retest_pass"))), TEXT_DIM, wrap=520)
                elif _ict_d:
                    for _k in ("premium_discount", "liquidity_sweep", "mss", "displacement_fvg", "fvg_ob_retest"):
                        _v = _ict_d.get(_k)
                        if isinstance(_v, dict):
                            row(panel, _k.replace("_", " ").title(), bool_txt(bool(_v.get("pass"))), ACCENT if _v.get("pass") else RED, wrap=520)
                        elif _v is not None:
                            row(panel, _k.replace("_", " ").title(), bool_txt(bool(_v)), ACCENT if _v else RED, wrap=520)
                if _ema_d:
                    row(panel, "EMA Detail", _ema_d.get("reason") or _ema_d.get("ema_status") or _ema_d, TEXT_DIM, wrap=520)
            else:
                _pre_reason = _not_eval_reason_map.get(_pipe_stop, "") if '_pipe_stop' in locals() else ""
                if not _pre_reason:
                    _pre_reason = "RC15f not evaluated — no final Swing candidate reached the RC15f guard"
                row(panel, "RC15f Decision", "NOT_EVALUATED", TEXT_DIM, wrap=520)
                row(panel, "Reason", _pre_reason, TEXT_DIM, wrap=520)

            # ── RC14: Pipeline Evaluation Summary ────────────────────────────
            if _pipe_stop:
                _pipe_color = RED if _pipe_stop.startswith("REJECTED") or _pipe_stop.startswith("STOPPED") else (GOLD if _pipe_stop == "SMC_EVALUATED" else TEXT_DIM)
                row(panel, "Pipeline Stop", _pipe_stop, _pipe_color, wrap=520)

            # SMC Evaluation
            if _smc_eval:
                row(panel, "SMC Evaluation", _smc_eval, _eval_color(_smc_eval), wrap=520)
            else:
                smc_match_txt, smc_match_fg = smc_eval_summary()
                row(panel, "SMC Match", smc_match_txt, smc_match_fg, wrap=520)

            # D/S Evaluation
            if _ds_eval:
                row(panel, "D/S Evaluation", _ds_eval, _eval_color(_ds_eval), wrap=520)
                if _ds_reason:
                    row(panel, "D/S Reason", _ds_reason, GOLD, wrap=520)
            else:
                ds1_available = bool(ds_1h_ctx.get("available"))
                row(panel, "DS 1H Available", bool_txt(ds1_available), ACCENT if ds1_available else GOLD)

            # Entry Protection
            if _ep:
                _ep_color = ACCENT if _ep == "PASSED" else (GOLD if "WARNING" in _ep else (RED if "REJECTED" in _ep else TEXT_DIM))
                row(panel, "Entry Protection", _ep, _ep_color, wrap=520)

            # Diagnostic Code
            if _diag_code:
                row(panel, "Diagnostic Code", _diag_code, _eval_color(_diag_code), wrap=520)

            # سبب Not evaluated الدقيق
            if _smc_eval in ("NOT_EVALUATED", "") or not _smc_eval:
                _reason_txt = _not_eval_reason_map.get(_pipe_stop, "")
                if _reason_txt:
                    row(panel, "Reason", _reason_txt, TEXT_DIM, wrap=520)
                elif not strategy:
                    row(panel, "Reason", "لم تصل العملية لمرحلة SMC لأن شروط Trend/IV/DTE/Strategy الأساسية لم تنتج مرشحاً", TEXT_DIM, wrap=520)

            # ── تفاصيل SMC Filter عند التطبيق ──────────────────────────────
            if sym == "SPX":
                row(panel, "Swing SMC Filter", "غير مطبق على SPX", TEXT_DIM, wrap=520)
            elif smc_filter.get("applied"):
                final_action = smc_filter.get("final_action") or "NO_CHANGE"
                action_mode  = smc_filter.get("action") or "no_change"
                adj          = smc_filter.get("adj", 0)
                detail       = smc_filter.get("detail") or "—"
                section(panel, "SMC Filter Detail", TEXT_DIM)
                row(panel, "Swing SMC Filter", "Applied", ACCENT)
                row(panel, "Final Swing Action", final_action,
                    RED if str(final_action).startswith("REJECTED") else (GOLD if smc_filter.get("ds_warning") else ACCENT), wrap=520)
                row(panel, "Action Mode", action_mode, TEXT_DIM)
                row(panel, "SMC Score Adj", f"{adj:+d}", "#4caf50" if adj > 0 else RED if adj < 0 else TEXT_DIM)
                row(panel, "1H bias",  smc_filter.get("h1_bias",  "—"), ACCENT if "bearish" in str(smc_filter.get("h1_bias",  "")) else RED if "bullish" in str(smc_filter.get("h1_bias",  "")) else TEXT_DIM)
                row(panel, "15m bias", smc_filter.get("m15_bias", "—"), ACCENT if "bearish" in str(smc_filter.get("m15_bias", "")) else RED if "bullish" in str(smc_filter.get("m15_bias", "")) else TEXT_DIM)
                row(panel, "5m bias",  smc_filter.get("m5_bias",  "—"), ACCENT if "bearish" in str(smc_filter.get("m5_bias",  "")) else RED if "bullish" in str(smc_filter.get("m5_bias",  "")) else TEXT_DIM)
                row(panel, "EMA20/EMA50 15m", smc_filter.get("ema15_status", "unavailable"), TEXT_DIM, wrap=520)
                row(panel, "EMA20/EMA50 5m",  smc_filter.get("ema5_status",  "unavailable"), TEXT_DIM, wrap=520)
                row(panel, "Demand 1H", fmt_zone(smc_filter.get("nd_1h")), RED  if (smc_filter.get("inside_demand_1h") or smc_filter.get("near_demand_1h")) else TEXT_DIM, wrap=520)
                row(panel, "Supply 1H", fmt_zone(smc_filter.get("ns_1h")), ACCENT if (smc_filter.get("inside_supply_1h") or smc_filter.get("near_supply_1h")) else TEXT_DIM, wrap=520)
                row(panel, "Demand Flags", f"inside={bool_txt(smc_filter.get('inside_demand_1h'))} | near={bool_txt(smc_filter.get('near_demand_1h'))} | conf={smc_filter.get('demand_confidence', 0)} | hard_reject={bool_txt(smc_filter.get('demand_hard_reject_allowed'))}", TEXT_DIM, wrap=520)
                row(panel, "Supply Flags", f"inside={bool_txt(smc_filter.get('inside_supply_1h'))} | near={bool_txt(smc_filter.get('near_supply_1h'))} | conf={smc_filter.get('supply_confidence', 0)} | hard_reject={bool_txt(smc_filter.get('supply_hard_reject_allowed'))}", TEXT_DIM, wrap=520)
                row(panel, "Filter Detail", detail, GOLD if smc_filter.get("ds_warning") else TEXT_DIM, wrap=520)

            # ── Support Context — بيانات خام/مساندة ─────────────────────────
            section(panel, "Support Context", TEXT_DIM)
            smc_available = bool(smc_context.get("available"))
            row(panel, "SMC Source Available", bool_txt(smc_available), ACCENT if smc_available else TEXT_DIM)
            if smc_context.get("error"):
                row(panel, "SMC Error", smc_context.get("error"), GOLD, wrap=520)
            ds1_available = bool(ds_1h_ctx.get("available"))
            row(panel, "DS 1H Available", bool_txt(ds1_available), ACCENT if ds1_available else GOLD)
            if not ds1_available and ds_1h_ctx.get("reason"):
                row(panel, "DS 1H Reason", ds_1h_ctx.get("reason", "—"), GOLD, wrap=520)
            if ds1_available:
                row(panel, "DS 1H Demand", fmt_zone(ds_1h_ctx.get("nearest_demand")), TEXT_DIM, wrap=520)
                row(panel, "DS 1H Supply", fmt_zone(ds_1h_ctx.get("nearest_supply")), TEXT_DIM, wrap=520)
            ds15_available = bool(ds_15m_ctx.get("available"))
            row(panel, "DS 15m Available", bool_txt(ds15_available), ACCENT if ds15_available else GOLD)
            if not ds15_available and ds_15m_ctx.get("reason"):
                row(panel, "DS 15m Reason", ds_15m_ctx.get("reason", "—"), GOLD, wrap=520)
            if ds15_available:
                row(panel, "DS 15m Demand", fmt_zone(ds_15m_ctx.get("nearest_demand")), TEXT_DIM, wrap=520)
                row(panel, "DS 15m Supply", fmt_zone(ds_15m_ctx.get("nearest_supply")), TEXT_DIM, wrap=520)

            tk.Frame(card, bg=card_color, height=6).pack()

        # ── RC14: Session Summary Panel ───────────────────────────────────────
        summary = results.get("_session_summary") or {}
        if summary:
            self._render_session_summary(summary)

        note = tk.Frame(self._swing_diag_cards, bg=BG2)
        note.pack(fill="x", padx=4, pady=(12, 4))
        tk.Label(
            note,
            text="ℹ️ Swing Paper مفعّل لـ SPY/QQQ/IWM/DIA/AAPL/NVDA/GLD | AAPL/NVDA/GLD = Swing-only؛ AAPL/NVDA فقط لهما خيار تعطيل يدوي للإعلانات؛ SPX = 0DTE فقط",
            bg=BG2,
            fg=GOLD,
            font=("Segoe UI", 9, "bold"),
            pady=10,
        ).pack()

    def _render_session_summary(self, summary: dict):
        """يرسم لوحة Swing D/S Session Summary أسفل البطاقات."""
        counts           = summary.get("counts", {})
        successful_evals = summary.get("successful_evals", 0)
        coverage_pct     = summary.get("coverage_pct")
        strong_rate_pct  = summary.get("strong_rate_pct")
        unavail_reasons  = summary.get("unavail_reasons", {})
        run_id           = summary.get("run_id", "—")
        total            = summary.get("total_symbols", 0)
        unavailable_n    = counts.get("UNAVAILABLE", 0)
        not_eval_n       = counts.get("NOT_EVALUATED", 0)

        # لون الـ Coverage
        if coverage_pct is None:
            cov_txt = "N/A"
            cov_fg  = TEXT_DIM
        elif coverage_pct >= 80:
            cov_txt = f"{coverage_pct}%"
            cov_fg  = ACCENT
        elif coverage_pct >= 50:
            cov_txt = f"{coverage_pct}%"
            cov_fg  = GOLD
        else:
            cov_txt = f"{coverage_pct}%"
            cov_fg  = RED

        panel = tk.Frame(self._swing_diag_cards, bg=BG2, relief="flat", bd=0)
        panel.pack(fill="x", padx=4, pady=(4, 0))

        # عنوان اللوحة
        hdr = tk.Frame(panel, bg=BG2)
        hdr.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(hdr, text="Current App Session — D/S Coverage", bg=BG2, fg=BLUE,
                 font=("Segoe UI", 10, "bold")).pack(side="left")
        tk.Label(hdr, text=f"Run: {run_id}", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8)).pack(side="right")

        tk.Frame(panel, bg=CARD, height=1).pack(fill="x", padx=14)

        body = tk.Frame(panel, bg=BG2)
        body.pack(fill="x", padx=20, pady=(6, 10))

        left  = tk.Frame(body, bg=BG2)
        left.pack(side="left", fill="both", expand=True, anchor="n")
        right = tk.Frame(body, bg=BG2)
        right.pack(side="left", fill="both", expand=True, anchor="n")

        def srow(parent, label, val, fg=TEXT_DIM):
            r = tk.Frame(parent, bg=BG2)
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, bg=BG2, fg=TEXT_DIM,
                     font=("Segoe UI", 8), width=22, anchor="w").pack(side="left")
            tk.Label(r, text=str(val), bg=BG2, fg=fg,
                     font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")

        # العمود الأيسر: العدادات
        srow(left, "Symbols Tracked",     total)
        srow(left, "Successful Evals",    successful_evals, ACCENT if successful_evals == total else GOLD)
        srow(left, "  PASSED",            counts.get("PASSED",         0), ACCENT)
        srow(left, "  NO_ACTIVE_ZONE",    counts.get("NO_ACTIVE_ZONE", 0), ACCENT)
        srow(left, "  WARNING_ZONE",      counts.get("WARNING_ZONE",   0), GOLD)
        srow(left, "  HARD_REJECTED",     counts.get("HARD_REJECTED",  0), RED)
        srow(left, "Unavailable",         unavailable_n, GOLD if unavailable_n > 0 else TEXT_DIM)
        srow(left, "Not Evaluated",       not_eval_n,    TEXT_DIM)

        # العمود الأيمن: Coverage + Strong Rate + Unavailable breakdown
        srow(right, "D/S Coverage",
             f"{successful_evals}/{successful_evals + unavailable_n} — {cov_txt}",
             cov_fg)

        if strong_rate_pct is not None:
            sr_fg = ACCENT if strong_rate_pct >= 75 else GOLD if strong_rate_pct >= 50 else TEXT_DIM
            srow(right, "Strong Rate",
                 f"{strong_rate_pct}%  (PASSED+NO_ZONE+REJECT / evals)",
                 sr_fg)
        else:
            srow(right, "Strong Rate", "N/A — no successful evals", TEXT_DIM)

        # System Check one-liner
        if coverage_pct is not None:
            check_line = f"D/S Coverage: {successful_evals}/{successful_evals + unavailable_n} completed — {coverage_pct}%"
        else:
            check_line = "D/S Coverage: no data yet"

        tk.Frame(right, bg=CARD, height=1).pack(fill="x", pady=(6, 2))
        tk.Label(right, text="System Check:", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w")
        tk.Label(right, text=check_line, bg=BG2, fg=cov_fg,
                 font=("Segoe UI", 9, "bold"), anchor="w",
                 wraplength=380).pack(anchor="w")

        # تفصيل أسباب UNAVAILABLE
        if unavail_reasons:
            tk.Frame(right, bg=CARD, height=1).pack(fill="x", pady=(6, 2))
            tk.Label(right, text="Unavailable Breakdown:", bg=BG2, fg=GOLD,
                     font=("Segoe UI", 8, "bold")).pack(anchor="w")
            for reason, cnt in unavail_reasons.items():
                tk.Label(right, text=f"  {reason}: {cnt}", bg=BG2, fg=GOLD,
                         font=("Segoe UI", 8), anchor="w").pack(anchor="w")

    # ── Settings Tab ─────────────────────────────────────────
    def _build_settings_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  ⚙ الإعدادات  ")

        # Scrollable container
        canvas = tk.Canvas(frame, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=BG)
        canvas_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_frame_configure(e):
            canvas.configure(scrollregion=canvas.bbox("all"))
        def _on_canvas_configure(e):
            canvas.itemconfig(canvas_win, width=e.width)
        def _on_mousewheel(e):
            canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        inner.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.bind("<MouseWheel>", _on_mousewheel)

        # padding داخلي
        pad = tk.Frame(inner, bg=BG)
        pad.pack(expand=True, fill="both", padx=60, pady=30)
        inner = pad  # استخدم pad بدلاً من inner

        tk.Label(inner, text="إعدادات البوت", bg=BG, fg=WHITE,
                 font=("Segoe UI", 16, "bold")).pack(anchor="e", pady=(0, 20))

        self.settings_vars = {}
        fields = [
            ("tasty_client_secret", "Tastytrade Client Secret", True),
            ("tasty_refresh_token", "Tastytrade Refresh Token", True),
            ("telegram_token", "Telegram Bot Token", False),
            ("telegram_chat_id", "Telegram Chat ID", False),
            ("risk_per_trade", "المخاطرة لكل صفقة ($)", False),
            ("target_short_delta", "Target Short Delta للـ IC", False),
            ("wing_width_spx",  "Wing Width — SPX",            False),
            ("wing_width_spy",  "Wing Width — SPY",            False),
            ("wing_width_qqq",  "Wing Width — QQQ",            False),
            ("paper_balance",   "رصيد Paper Trading ($)",      False),
            ("sigma_debug_telegram", "إرسال جدول 1.5σ عبر Telegram (1=نعم)", False),
        ]

        for key, label, is_pass in fields:
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="x", pady=6)
            tk.Label(row, text=label, bg=BG, fg=TEXT,
                     font=("Segoe UI", 10), width=30, anchor="e").pack(side="right", padx=10)
            var = tk.StringVar(value=get_setting(key))
            entry = tk.Entry(row, textvariable=var, bg=CARD, fg=WHITE,
                             insertbackground=WHITE, relief="flat",
                             font=("Segoe UI", 10), show="*" if is_pass else "")
            entry.pack(side="right", fill="x", expand=True, ipady=8, padx=(0, 4))
            self.settings_vars[key] = var

        # RC13e — AAPL/NVDA Swing permissions on one line.
        # Checked = trading allowed; unchecked = manually blocked for announcements/earnings.
        equity_row = tk.Frame(inner, bg=BG)
        equity_row.pack(fill="x", pady=8)
        tk.Label(equity_row, text="السماح بتداول Swing للأسهم الفردية:", bg=BG, fg=TEXT,
                 font=("Segoe UI", 10), width=30, anchor="e").pack(side="right", padx=10)

        legacy_blocked = {x.strip().upper() for x in str(get_setting("swing_manual_block_symbols", "") or "").replace(";", ",").split(",") if x.strip()}
        for symbol, key in (("AAPL", "enable_aapl_swing"), ("NVDA", "enable_nvda_swing")):
            raw = str(get_setting(key, "") or "").strip().lower()
            enabled = (symbol not in legacy_blocked) if raw == "" else raw in ("1", "true", "yes", "on")
            var = tk.BooleanVar(value=enabled)
            tk.Checkbutton(
                equity_row, text=f"{symbol} Swing", variable=var,
                bg=BG, fg=WHITE, activebackground=BG, activeforeground=WHITE,
                selectcolor=CARD, font=("Segoe UI", 10, "bold"),
                anchor="e"
            ).pack(side="right", padx=(4, 14))
            self.settings_vars[key] = var

        tk.Label(equity_row, text="✓ = مسموح   بدون ✓ = متوقف مؤقتًا", bg=BG, fg=TEXT_DIM,
                 font=("Segoe UI", 8), anchor="e").pack(side="right", padx=8)

        # ── Risk Settings ──────────────────────────────────────
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(16, 8))
        tk.Label(inner, text="⚖️  إدارة المخاطرة", bg=BG, fg=GOLD,
                 font=("Segoe UI", 12, "bold")).pack(anchor="e", pady=(0, 8))

        risk_fields = [
            ("account_size",           "حجم الحساب ($)"),
            ("max_risk_per_trade_pct", "Max Risk لكل صفقة (%)"),
            ("max_trades_per_day",     "أقصى صفقات يومياً (0 = غير محدود)"),
            ("max_daily_loss_pct",     "Max Daily Loss (%)"),
            ("max_open_trades",        "أقصى صفقات مفتوحة في آن واحد (0 = غير محدود)"),
            ("max_per_symbol",         "أقصى صفقات مفتوحة لكل رمز (0 = غير محدود)"),
            ("max_open_swing_total",   "أقصى صفقات Swing مفتوحة إجمالاً (0 = غير محدود)"),
            ("allow_duplicate_open_strategies", "تكرار نفس الاستراتيجية المفتوحة (1=نعم، 0=لا)"),
            ("prevent_exact_duplicate_open_trade", "حد نفس الصفقة المفتوحة بالضبط (0=تعطيل، 3=يسمح بثلاث)"),
            ("duplicate_signal_cooldown_minutes", "منع تكرار نفس الصفقة خلال X دقيقة (0=تعطيل)"),
            ("prevent_same_trade_same_day", "منع نفس الصفقة طوال اليوم (1=نعم، 0=لا)"),
        ]
        for key, label in risk_fields:
            row = tk.Frame(inner, bg=BG)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label, bg=BG, fg=TEXT,
                     font=("Segoe UI", 10), width=30, anchor="e").pack(side="right", padx=10)
            var = tk.StringVar(value=get_setting(key))
            tk.Entry(row, textvariable=var, bg=CARD, fg=GOLD,
                     insertbackground=WHITE, relief="flat",
                     font=("Segoe UI", 10)).pack(side="right", fill="x", expand=True, ipady=8, padx=(0, 4))
            self.settings_vars[key] = var

        # Risk preview
        self.risk_preview = tk.Label(inner, text="", bg=BG, fg=TEXT_DIM,
                                     font=("Segoe UI", 8))
        self.risk_preview.pack(anchor="e", pady=2)
        self._update_risk_preview()

        # Save button
        tk.Button(inner, text="💾  حفظ الإعدادات", bg=ACCENT, fg="#000",
                  font=("Segoe UI", 11, "bold"), relief="flat", cursor="hand2",
                  command=self._save_settings, pady=10).pack(fill="x", pady=20)

        # Test Telegram
        tk.Button(inner, text="✈  اختبار الاتصال بالتيليغرام", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 10), relief="flat", cursor="hand2",
                  command=self._test_telegram, pady=8).pack(fill="x", pady=(0,6))

        # Test Tastytrade
        tk.Button(inner, text="🔗  اختبار الاتصال بـ Tastytrade", bg=CARD, fg=BLUE,
                  font=("Segoe UI", 10), relief="flat", cursor="hand2",
                  command=self._test_tastytrade, pady=8).pack(fill="x")

        # Settings-page mouse wheel support.
        # Bind directly to every widget inside Settings, including Entry fields,
        # because on Windows the wheel event may be delivered to the focused
        # input widget rather than the Canvas itself.
        self.settings_canvas = canvas

        def _settings_wheel(event, direction=None):
            try:
                if direction is None:
                    delta = getattr(event, "delta", 0)
                    if delta == 0:
                        return None
                    units = int(-1 * (delta / 120))
                    if units == 0:
                        units = -1 if delta > 0 else 1
                else:
                    units = direction
                canvas.yview_scroll(units, "units")
                return "break"
            except Exception:
                return None

        def _bind_settings_mousewheel(widget):
            try:
                widget.bind("<MouseWheel>", _settings_wheel)
                widget.bind("<Button-4>", lambda e: _settings_wheel(e, -1))
                widget.bind("<Button-5>", lambda e: _settings_wheel(e, 1))
            except Exception:
                pass
            try:
                for child in widget.winfo_children():
                    _bind_settings_mousewheel(child)
            except Exception:
                pass

        _bind_settings_mousewheel(frame)

    def _update_risk_preview(self):
        if not hasattr(self, "risk_preview"):
            return
        try:
            acct = float(self.settings_vars.get("account_size", tk.StringVar(value="1500")).get() or 1500)
            pct  = float(self.settings_vars.get("max_risk_per_trade_pct", tk.StringVar(value="2")).get() or 2)
            dpct = float(self.settings_vars.get("max_daily_loss_pct", tk.StringVar(value="4")).get() or 4)
            mtd  = int(self.settings_vars.get("max_trades_per_day", tk.StringVar(value="0")).get() or 0)
            mot  = int(self.settings_vars.get("max_open_trades", tk.StringVar(value="0")).get() or 0)
            mps  = int(self.settings_vars.get("max_per_symbol", tk.StringVar(value="0")).get() or 0)
            dup  = str(self.settings_vars.get("allow_duplicate_open_strategies", tk.StringVar(value="1")).get() or "1").strip()
            exact_dup = str(self.settings_vars.get("prevent_exact_duplicate_open_trade", tk.StringVar(value="3")).get() or "3").strip()
            cooldown = int(float(self.settings_vars.get("duplicate_signal_cooldown_minutes", tk.StringVar(value="0")).get() or 0))
            same_day = str(self.settings_vars.get("prevent_same_trade_same_day", tk.StringVar(value="0")).get() or "0").strip()
            risk_dollar  = round(acct * pct / 100, 2)
            daily_dollar = round(acct * dpct / 100, 2)
            fmt = lambda n: "∞" if int(n) == 0 else str(int(n))
            dup_txt = "نعم" if dup in ("1", "true", "True", "yes", "YES", "نعم") else "لا"
            try:
                exact_n = int(float(exact_dup or 0))
            except Exception:
                exact_n = 1 if exact_dup in ("true", "True", "yes", "YES", "نعم") else 0
            exact_txt = "معطّل" if exact_n <= 0 else f"حد {exact_n}"
            same_day_txt = "نعم" if same_day in ("1", "true", "True", "yes", "YES", "نعم") else "لا"
            self.risk_preview.config(
                text=f"Max Risk/صفقة: ${risk_dollar:.0f}  |  Max Daily Loss: ${daily_dollar:.0f}  |  صفقات/يوم: {fmt(mtd)}  |  مفتوحة: {fmt(mot)}  |  لكل رمز: {fmt(mps)}  |  تكرار الاستراتيجية: {dup_txt}  |  منع نفس الصفقة: {exact_txt}  |  Cooldown: {cooldown}د  |  نفس اليوم: {same_day_txt}"
            )
        except Exception:
            pass

    # ── قواعد التحقق: (key, label, type, min, max) ─────────────────────────
    _SETTINGS_RULES = [
        ("account_size",           "حجم الحساب",             float, 100,    10_000_000),
        ("max_risk_per_trade_pct", "Max Risk لكل صفقة (%)",  float, 0.1,    20.0),
        # 0 means unlimited for paper-study mode.
        ("max_trades_per_day",     "أقصى صفقات يومياً",      int,   0,      999999),
        ("max_daily_loss_pct",     "Max Daily Loss (%)",      float, 0.1,    50.0),
        ("max_open_trades",        "أقصى صفقات مفتوحة",      int,   0,      999999),
        ("max_per_symbol",         "أقصى صفقات لكل رمز",      int,   0,      999999),
        ("allow_duplicate_open_strategies", "تكرار نفس الاستراتيجية", int, 0, 1),
        ("prevent_exact_duplicate_open_trade", "حد نفس الصفقة المفتوحة", int, 0, 999999),
        ("duplicate_signal_cooldown_minutes", "مدة منع تكرار نفس الصفقة", int, 0, 10080),
        ("prevent_same_trade_same_day", "منع نفس الصفقة طوال اليوم", int, 0, 1),
        ("paper_balance",          "رصيد Paper Trading",      float, 100,    10_000_000),
        ("risk_per_trade",         "المخاطرة لكل صفقة ($)",  float, 1,      100_000),
        ("wing_width_spx",         "Wing Width SPX",          int,   1,      50),
        ("wing_width_spy",         "Wing Width SPY",          int,   1,      20),
        ("wing_width_qqq",         "Wing Width QQQ",          int,   1,      20),
    ]

    def _validate_settings(self) -> list:
        """يُعيد قائمة أخطاء، فارغة = كل شيء صحيح."""
        errors = []
        for key, label, typ, lo, hi in self._SETTINGS_RULES:
            var = self.settings_vars.get(key)
            if not var:
                continue
            raw = var.get().strip()
            if not raw:
                continue
            try:
                val = typ(raw)
            except (ValueError, TypeError):
                errors.append(f"{label}: '{raw}' ليس رقماً صحيحاً")
                continue
            if not (lo <= val <= hi):
                errors.append(f"{label}: {val} خارج النطاق [{lo} — {hi}]")
        return errors

    def _save_settings(self):
        # تحقق من الإدخال أولاً
        errors = self._validate_settings()
        if errors:
            messagebox.showerror(
                "خطأ في الإعدادات",
                "يرجى تصحيح الأخطاء التالية:\n\n" + "\n".join(f"• {e}" for e in errors)
            )
            return

        for key, var in self.settings_vars.items():
            value = var.get()
            if isinstance(var, tk.BooleanVar):
                save_setting(key, "1" if bool(value) else "0")
            else:
                save_setting(key, str(value).strip())

        # Retire the old comma-separated manual block field. The checkboxes above
        # are now the single source of truth for AAPL/NVDA Swing permissions.
        save_setting("swing_manual_block_symbols", "")
        # مسارات التداول الورقي مخفية من الواجهة ومفعّلة داخليًا دائماً.
        # لا توجد مسارات Auto/Real في نسخة Paper-only.
        for key in (
            "path_spx_0dte_paper",
            "path_spy_0dte_paper",
            "path_qqq_0dte_paper",
            "path_iwm_0dte_paper",
            "path_spy_swing_paper",
            "path_qqq_swing_paper",
            "path_iwm_swing_paper",
            "path_dia_swing_paper",
            "path_aapl_swing_paper",
            "path_nvda_swing_paper",
            "path_gld_swing_paper",
        ):
            save_setting(key, "1")
        from core.analyzer import invalidate_session
        invalidate_session()
        self._update_risk_preview()
        messagebox.showinfo("تم", "✅ تم حفظ الإعدادات\nسيتم إعادة تسجيل الدخول لـ Tastytrade عند التحليل القادم")

    def _test_tastytrade(self):
        client_secret  = self.settings_vars.get("tasty_client_secret", tk.StringVar()).get().strip()
        refresh_token  = self.settings_vars.get("tasty_refresh_token", tk.StringVar()).get().strip()
        if not client_secret or not refresh_token:
            messagebox.showwarning("تنبيه", "يرجى إدخال Client Secret و Refresh Token أولاً")
            return
        self.status_dot.config(text="● جاري الاتصال...", fg=GOLD)
        self.root.update()
        def worker():
            try:
                from core.analyzer import get_spx_price, invalidate_session, _get_access_token
                invalidate_session()
                tok   = _get_access_token(client_secret, refresh_token, force=True)
                price = get_spx_price(tok)
                if price:
                    self.root.after(0, lambda: messagebox.showinfo(
                        "نجاح ✅", f"تم الاتصال بـ Tastytrade بنجاح!\nسعر SPX الحالي: ${price:,.2f}"))
                else:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "تحذير", "تم تسجيل الدخول لكن تعذّر جلب سعر SPX\n(قد يكون السوق مغلقاً)"))
            except Exception as e:
                err = str(e)
                self.root.after(0, lambda: messagebox.showerror("خطأ", f"فشل الاتصال:\n{err}"))
            finally:
                self.root.after(0, lambda: self.status_dot.config(text="● جاهر", fg=ACCENT))
        threading.Thread(target=worker, daemon=True).start()


    # ── Debug Tab ────────────────────────────────────────────
    def _build_debug_tab(self):
        frame = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(frame, text="  🧪 فحص النظام  ")

        toolbar = tk.Frame(frame, bg=BG2, height=52)
        toolbar.pack(fill="x")
        toolbar.pack_propagate(False)

        tk.Label(toolbar, text="فحص النظام والبيانات", bg=BG2, fg=TEXT_DIM,
                 font=("Segoe UI", 9)).pack(side="left", padx=14)

        tk.Button(toolbar, text="🔎 تشغيل الفحص", bg=ACCENT, fg="#000",
                  font=("Segoe UI", 9, "bold"), relief="flat", cursor="hand2",
                  command=self._run_debug_check, padx=12).pack(side="right", padx=10, pady=10)
        tk.Button(toolbar, text="🔄 تحديث سجل التحليلات", bg=CARD, fg=TEXT,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._refresh_analysis_logs, padx=12).pack(side="right", padx=4, pady=10)
        tk.Button(toolbar, text="⛶ عرض كامل", bg="#24344f", fg=WHITE,
                  font=("Segoe UI", 9), relief="flat", cursor="hand2",
                  command=self._open_debug_full_view, padx=12).pack(side="right", padx=4, pady=10)

        # v3.23: fixed flexible layout for System Check.
        # The previous page used a scrollable body with fixed-height widgets, so the result
        # could look clipped.  This layout lets the result and saved-analysis panels expand.
        body = tk.PanedWindow(frame, orient=tk.HORIZONTAL, bg=BG, sashwidth=6,
                              bd=0, relief="flat", opaqueresize=True)
        body.pack(fill="both", expand=True, padx=8, pady=8)

        # Left: diagnostic output
        left = tk.Frame(body, bg=CARD)
        body.add(left, minsize=650, stretch="always")
        tk.Label(left, text="نتيجة الفحص", bg=CARD, fg=WHITE,
                 font=("Segoe UI", 12, "bold")).pack(anchor="e", padx=12, pady=(10, 4))

        debug_box = tk.Frame(left, bg=BG3)
        debug_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.debug_text = tk.Text(debug_box, bg=BG3, fg=TEXT, insertbackground=WHITE,
                                  relief="flat", font=("Consolas", 10), wrap="none",
                                  undo=False)
        debug_y = ttk.Scrollbar(debug_box, orient="vertical", command=self.debug_text.yview)
        debug_x = ttk.Scrollbar(debug_box, orient="horizontal", command=self.debug_text.xview)
        self.debug_text.configure(yscrollcommand=debug_y.set, xscrollcommand=debug_x.set)
        debug_y.pack(side="right", fill="y")
        debug_x.pack(side="bottom", fill="x")
        self.debug_text.pack(side="left", fill="both", expand=True)
        self.debug_text.insert("1.0", "اضغط تشغيل الفحص لمعرفة حالة Tastytrade والسعر والـ Option Chain والـ Greeks.\n")
        self.debug_text.config(state="disabled")

        # Right: recent analysis logs
        right = tk.Frame(body, bg=CARD)
        body.add(right, minsize=360)
        tk.Label(right, text="آخر التحليلات المحفوظة", bg=CARD, fg=WHITE,
                 font=("Segoe UI", 12, "bold")).pack(anchor="e", padx=12, pady=(10, 4))

        log_box = tk.Frame(right, bg=CARD)
        log_box.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ("الوقت", "SPX", "Pin", "EM", "ZeroG", "Decision")
        self.analysis_log_tree = ttk.Treeview(log_box, columns=cols, show="headings")
        log_y = ttk.Scrollbar(log_box, orient="vertical", command=self.analysis_log_tree.yview)
        log_x = ttk.Scrollbar(log_box, orient="horizontal", command=self.analysis_log_tree.xview)
        self.analysis_log_tree.configure(yscrollcommand=log_y.set, xscrollcommand=log_x.set)
        for col, width in zip(cols, [125, 70, 55, 60, 70, 170]):
            self.analysis_log_tree.heading(col, text=col, anchor="center")
            self.analysis_log_tree.column(col, width=width, anchor="center", stretch=False)
        log_y.pack(side="right", fill="y")
        log_x.pack(side="bottom", fill="x")
        self.analysis_log_tree.pack(side="left", fill="both", expand=True)
        self._refresh_analysis_logs()

    def _set_debug_text(self, text):
        self.debug_text.config(state="normal")
        self.debug_text.delete("1.0", "end")
        self.debug_text.insert("1.0", text)
        self.debug_text.config(state="disabled")

    def _open_debug_full_view(self):
        """يفتح نتيجة فحص النظام في نافذة كبيرة مستقلة قابلة للتمرير."""
        try:
            current = self.debug_text.get("1.0", "end-1c")
        except Exception:
            current = "لا توجد نتيجة فحص حالية."
        win = tk.Toplevel(self.root)
        win.title("فحص النظام — عرض كامل")
        win.geometry("1100x720")
        win.configure(bg=BG)

        top = tk.Frame(win, bg=BG2, height=42)
        top.pack(fill="x")
        top.pack_propagate(False)
        tk.Label(top, text="نتيجة فحص النظام — عرض كامل", bg=BG2, fg=WHITE,
                 font=("Segoe UI", 11, "bold")).pack(side="right", padx=12)
        tk.Button(top, text="إغلاق", bg=CARD, fg=TEXT, relief="flat",
                  command=win.destroy, padx=12).pack(side="left", padx=10, pady=7)

        box = tk.Frame(win, bg=BG3)
        box.pack(fill="both", expand=True, padx=10, pady=10)
        txt = tk.Text(box, bg=BG3, fg=TEXT, insertbackground=WHITE,
                      relief="flat", font=("Consolas", 10), wrap="none")
        y = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        x = ttk.Scrollbar(box, orient="horizontal", command=txt.xview)
        txt.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        y.pack(side="right", fill="y")
        x.pack(side="bottom", fill="x")
        txt.pack(side="left", fill="both", expand=True)
        txt.insert("1.0", current)
        txt.config(state="disabled")


    def _run_debug_check(self):
        self.status_dot.config(text="● جاري الفحص...", fg=GOLD)
        self._set_debug_text("جاري فحص Tastytrade والبيانات...\n")
        self.root.update()

        def worker():
            from core.analyzer import run_diagnostics  # RC15i.4: lazy
            result = run_diagnostics()
            self.root.after(0, lambda: self._on_debug_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_debug_done(self, result):
        lines = []
        lines.append(f"Timestamp: {result.get('timestamp', '')}")
        lines.append(f"Tastytrade Login: {result.get('auth')}")
        lines.append(f"Option Chain: {result.get('chain')}")
        lines.append(f"Calls Count: {result.get('chain_calls')}")
        lines.append(f"Puts Count: {result.get('chain_puts')}")
        price = result.get('spx_price')
        if price:
            lines.append(f"SPX Price: {price}")
        else:
            lines.append("SPX Price: FAILED")
        lines.append(f"Price Source: {result.get('price_source')}")
        ps  = result.get('pin_score')
        mag = result.get('magnetic')
        pin_src = (result.get('debug') or {}).get('pin_score_source', 'live')
        lines.append(f"Pin Score:    {ps} / 100  (magnetic={mag})  [{pin_src}]")

        # Pin Score breakdown
        pb = (result.get('debug') or {}).get('pin_breakdown', {})
        if pb:
            lines.append(f"  Distance to Magnet: {pb.get('distance_to_magnet','?')} pts  (EM used: {pb.get('em_used','?')})")
            lines.append(f"  +{pb.get('distance_score',0):.1f}  Distance Score")
            lines.append(f"  +{pb.get('oi_concentration',0):.1f}  OI Concentration")
            lines.append(f"  +{pb.get('gex_support',0):.1f}  GEX Support")
            lines.append(f"  +{pb.get('balance_score',0):.1f}  Call/Put Balance")
            lines.append(f"  ={pb.get('total','?')}  Total")

        # ── Market Data Status ────────────────────────────────────────
        dq_rep = result.get("data_quality_report", {})
        mode_d = dq_rep.get("mode", "UNKNOWN")
        qpct   = dq_rep.get("quality_pct", 0)
        checks = dq_rep.get("checks", {})
        lines.append("")
        lines.append(f"Market Data Status:  [{mode_d}]  Quality: {qpct}%")
        lines.append("  " + "─" * 35)
        check_labels = {
            "price":  "SPX Price",
            "chain":  "Option Chain",
            "delta":  "Delta",
            "gamma":  "Gamma",
            "prices": "Bid/Ask/Mid",
            "vix":    "VIX",
            "ema":    "EMA20/50",
            "oi":     "Open Interest",
        }
        for key, label in check_labels.items():
            ok = checks.get(key, False)
            icon = "✓" if ok else "✗"
            lines.append(f"  {icon}  {label}")
        lines.append("")
        lines.append("")
        lines.append("Market Context:")
        lines.append(f"  VIX:            {result.get('vix')}")
        lines.append(f"  IV Now:         {result.get('iv_current')}")
        _iv_rank = result.get('iv_rank')
        _iv_rank_txt = f"{_iv_rank:.1f}" if isinstance(_iv_rank, (int, float)) else "N/A"
        lines.append(f"  IV Rank:        {_iv_rank_txt}  [{result.get('iv_rank_source')}]")
        _iv_pct = result.get('iv_percentile')
        _iv_reg = result.get('iv_regime', 'Unknown')
        _iv_reg_data = result.get('iv_regime_data') or {}
        lines.append(f"  IV Percentile:  {_iv_pct:.1f}%" if _iv_pct is not None else "  IV Percentile:  N/A")
        lines.append(f"  IV Regime:      {_iv_reg}")
        lines.append(f"  IV Reason:      {_iv_reg_data.get('reason', '—')}")
        lines.append("")
        lines.append("Expected Move:")
        dbg_lvl = (result.get('debug') or {})
        em_val = result.get('expected_move')
        em_src = result.get('expected_move_source', '')
        straddle = (result.get('levels') or {}).get('straddle_mid')
        atm_k    = (result.get('levels') or {}).get('em_atm_strike')
        lines.append(f"  EM:       ±{em_val}  [{em_src}]")
        if straddle: lines.append(f"  Straddle: {straddle:.2f}  (ATM strike: {atm_k})")
        lines.append(f"  EM Floor: {round((result.get('price',0) or 0)*0.004, 1)} (0.4% floor للـ Pin Score)")
        lines.append(f"  EMA20:         {result.get('ema20')}")
        lines.append(f"  EMA50:         {result.get('ema50')}")
        lines.append(f"  Daily Trend:   {result.get('daily_trend')}")
        lines.append(f"  EMA20 (15m):   {result.get('ema20_15m')}")
        lines.append(f"  EMA50 (15m):   {result.get('ema50_15m')}")
        lines.append(f"  15m Trend:     {result.get('intraday_trend')}")
        lines.append(f"  Combined:      {result.get('trend')}")
        dbg_pre = result.get('debug') or {}
        oi_age = dbg_pre.get("oi_cache_age_minutes")
        lines.append(f"  OI Cache: {'عمره ' + str(oi_age) + ' دقيقة' if oi_age is not None else 'لم يُستخدم'}")
        lines.append(f"  GEX Quality: {result.get('gex_quality', 'unknown')}  (real_oi > volume_proxy > gamma_proxy)")
        chain_in_result = result.get('_chain')
        lines.append(f"  Chain in result: {'YES (' + str(len(chain_in_result.get('calls',[]))) + ' calls, ' + str(len(chain_in_result.get('puts',[]))) + ' puts)' if chain_in_result else 'NO'}")
        if chain_in_result:
            has_prices_diag = any(o.get("mid") is not None for o in chain_in_result.get("calls",[]) + chain_in_result.get("puts",[]))
            has_delta_diag  = any(o.get("delta") is not None for o in chain_in_result.get("calls",[]) + chain_in_result.get("puts",[]))
            lines.append(f"  Prices in chain: {has_prices_diag}  |  Delta in chain: {has_delta_diag}")
        dbg2 = (result.get('debug') or {}).get('gex_diagnostic') or {}
        if dbg2:
            lines.append("")
            lines.append("GEX Breakdown:")
            lines.append(f"  Call signed GEX: {dbg2.get('call_signed_gex', 'N/A'):,.0f}" if isinstance(dbg2.get('call_signed_gex'), (int,float)) else f"  Call signed GEX: N/A")
            lines.append(f"  Put  signed GEX: {dbg2.get('put_signed_gex', 'N/A'):,.0f}"  if isinstance(dbg2.get('put_signed_gex'),  (int,float)) else f"  Put  signed GEX: N/A")
            lines.append(f"  Net  GEX:        {dbg2.get('net_gex', 'N/A'):,.0f}"         if isinstance(dbg2.get('net_gex'),         (int,float)) else f"  Net  GEX: N/A")
            lines.append(f"  Gross GEX:       {dbg2.get('gross_gex', 'N/A'):,.0f}"       if isinstance(dbg2.get('gross_gex'),       (int,float)) else f"  Gross GEX: N/A")
            lines.append(f"  Balance Ratio:   {dbg2.get('balance_ratio', 'N/A')}")
            lines.append(f"  Net=0 Reason:    {dbg2.get('reason', 'N/A')}")
            lines.append(f"  Calls Near:      {dbg2.get('calls_near_count', 'N/A')}")
            lines.append(f"  Puts Near:       {dbg2.get('puts_near_count', 'N/A')}")
            lines.append(f"  Has Any GEX:     {dbg2.get('has_any_gex', 'N/A')}")

        # ── GEX per Strike ─────────────────────────────────────────────
        gex_by_strike = (
            result.get("gex_by_strike")
            or (result.get("levels") or {}).get("gex_by_strike", [])
        )
        gamma_src = (
            result.get("gamma_source")
            or (result.get("levels") or {}).get("gamma_source", {})
        )
        call_sgex = result.get("call_signed_gex") or (result.get("levels") or {}).get("call_signed_gex")
        put_sgex  = result.get("put_signed_gex")  or (result.get("levels") or {}).get("put_signed_gex")
        net_gex_v = result.get("net_gex")         or (result.get("levels") or {}).get("net_gex")
        gross_v   = result.get("gross_gex")       or (result.get("levels") or {}).get("gross_gex") or 1

        lines.append("")
        lines.append("=" * 55)
        lines.append("   GEX Analysis — Top 10 Strikes")
        lines.append("=" * 55)

        if gex_by_strike:
            lines.append(f"  {'Strike':>8}   {'Net Signed GEX':>16}   {'Bar':}")
            lines.append("  " + "-" * 52)
            for row in gex_by_strike[:10]:
                strike = row.get("strike", 0)
                gex_v  = row.get("net_signed_gex", 0)
                sign   = "+" if gex_v > 0 else "-"
                icon   = "▲" if gex_v > 0 else "▼"
                bar_n  = min(int(abs(gex_v) / max(gross_v, 1) * 30), 30)
                bar    = icon * max(bar_n, 1)
                lines.append(f"  {strike:>8,.0f}   {sign}{abs(gex_v):>15,.0f}   {bar}")
        else:
            lines.append("  لا توجد بيانات GEX per strike")

        lines.append("  " + "─" * 52)
        lines.append(f"  {'Call GEX Total':>20}: {call_sgex:>+16,.0f}" if isinstance(call_sgex, (int, float)) else "  Call GEX Total: N/A")
        lines.append(f"  {'Put  GEX Total':>20}: {put_sgex:>+16,.0f}"  if isinstance(put_sgex,  (int, float)) else "  Put  GEX Total: N/A")
        lines.append("  " + "─" * 52)
        lines.append(f"  {'Net GEX':>20}: {net_gex_v:>+16,.0f}" if isinstance(net_gex_v, (int, float)) else "  Net GEX: N/A")
        lines.append(f"  {'Gross GEX':>20}: {gross_v:>16,.0f}")
        lines.append(f"  {'Zero Gamma':>20}: {(result.get('levels') or {}).get('zero_gamma', 'N/A')}")

        if gamma_src:
            lines.append("")
            real_c = gamma_src.get('real_dxlink', 0)
            proxy_c = gamma_src.get('proxy_100', 0)
            quality = "Real DXLink" if real_c > 0 else "Proxy"
            lines.append(f"  Gamma quality: {quality}  (real={real_c}, proxy={proxy_c})")
            if proxy_c > 0:
                lines.append(f"  ⚠️  OI غير متاح — GEX مقياسه ~500x أصغر من SpotGamma")
        lines.append("=" * 55)
        g = result.get('greeks_available') or {}
        lines.append("")
        lines.append("Greeks / Data Availability:")
        lines.append(f"  Delta:  {g.get('delta')}")
        lines.append(f"  Gamma:  {g.get('gamma')}")
        lines.append(f"  IV:     {g.get('iv')}")
        lines.append(f"  Prices: {g.get('prices')}")
        dbg = result.get('debug') or {}
        errs = dbg.get('errors') or []
        lines.append("")
        lines.append("API Events:")
        if errs:
            for e in errs[-10:]:
                lines.append(f"  - {e}")
        else:
            lines.append("  لا توجد أخطاء مسجلة.")

        self._set_debug_text("\n".join(lines))
        self.status_dot.config(text="● جاهر", fg=ACCENT)

    def _refresh_analysis_logs(self):
        if not hasattr(self, 'analysis_log_tree'):
            return
        for row in self.analysis_log_tree.get_children():
            self.analysis_log_tree.delete(row)
        try:
            logs = get_recent_analysis_logs(20)
        except Exception:
            logs = []
        for item in logs:
            self.analysis_log_tree.insert("", "end", values=(
                item.get("timestamp", ""),
                f"{item.get('spx_price') or '':}",
                item.get("pin_score", ""),
                item.get("expected_move", ""),
                item.get("zero_gamma", ""),
                item.get("decision", ""),
            ))

    # ── Actions ──────────────────────────────────────────────
    def _mode_label(self) -> str:
        from core.strategy_engine import get_threshold_mode, THRESHOLDS
        m = get_threshold_mode()
        t = THRESHOLDS[m]
        if m == "conservative":
            return f"🛡 محافظ  (IC:{t['Iron Condor']} BP:{t['Bull Put Spread']} CD:{t['Call Debit Spread']})"
        return f"🧪 اختبار  (IC:{t['Iron Condor']} BP:{t['Bull Put Spread']} CD:{t['Call Debit Spread']})"

    def _toggle_mode(self):
        from core.strategy_engine import get_threshold_mode, set_threshold_mode
        from core.database import save_setting
        new_mode = "test" if get_threshold_mode() == "conservative" else "conservative"
        set_threshold_mode(new_mode)
        save_setting("threshold_mode", new_mode)
        self.mode_btn.config(text=self._mode_label())
        color = GOLD if new_mode == "test" else BLUE
        self.mode_btn.config(fg=color, bg="#2a1a0a" if new_mode == "test" else "#1a1a3a")

    # ── Auto Scheduler ───────────────────────────────────────
    AUTO_INTERVAL_MS = 5 * 60 * 1000   # 5 دقائق

    def _toggle_auto(self):
        if self._auto_running:
            self._stop_auto()
        else:
            self._start_auto()

    def _start_auto(self):
        self._auto_running = True
        self.auto_btn.config(
            text="⏹  إيقاف التلقائي", bg="#3a1a1a", fg=RED,
            activebackground="#4a2020",
        )
        self.auto_status.config(text="▶ نشط — Paper Auto-Scan — يحلل كل 5 دقائق عند فتح السوق")
        self._run_auto_cycle()

    def _stop_auto(self):
        self._auto_running = False
        if self._auto_job_id:
            self.root.after_cancel(self._auto_job_id)
            self._auto_job_id = None
        self.auto_btn.config(
            text="⏱  Paper Auto-Scan (كل 5 دقائق)", bg="#1a3a5a", fg=BLUE,
            activebackground="#1e4570",
        )
        self.auto_status.config(text="")


    def _analysis_begin(self, source="manual"):
        """Prevent duplicate overlapping full_analysis workers from the UI."""
        try:
            with self._analysis_guard_lock:
                if self._analysis_in_progress:
                    msg = "⏭ تحليل جارٍ بالفعل — تم تجاهل الطلب المكرر"
                    try:
                        self.status_dot.config(text="● " + msg, fg=TEXT_DIM)
                    except Exception:
                        pass
                    if hasattr(self, "auto_status"):
                        try:
                            self.auto_status.config(text=msg, fg=TEXT_DIM)
                        except Exception:
                            pass
                    print(f"[analysis_guard] duplicate {source} analysis request ignored")
                    return False
                self._analysis_in_progress = True
                return True
        except Exception:
            return True

    def _analysis_end(self):
        try:
            with self._analysis_guard_lock:
                self._analysis_in_progress = False
        except Exception:
            self._analysis_in_progress = False

    def _run_auto_cycle(self):
        if not self._auto_running:
            return
        from core.analyzer import is_us_market_open
        if is_us_market_open():
            if not self._analysis_begin("auto"):
                self._auto_job_id = self.root.after(self.AUTO_INTERVAL_MS, self._run_auto_cycle)
                return
            self.auto_status.config(
                text=f"▶ نشط — آخر تشغيل {datetime.now().strftime('%H:%M:%S')}"
            )
            self.status_dot.config(text="● جاري التحليل التلقائي...", fg=GOLD)

            def _make_prog_cb(root, dot):
                """RC15i.5: safe progress callback — called from worker thread."""
                def _cb(msg):
                    root.after(0, lambda m=msg: dot.config(text=m, fg=GOLD))
                return _cb

            def worker():
                from core.analyzer import full_analysis  # RC15i.4: lazy
                from core.trade_monitor import _set_analysis_running  # RC15i.5 safety
                try:
                    result = full_analysis(
                        progress_cb=_make_prog_cb(self.root, self.status_dot)  # RC15i.5
                    )
                except Exception as _e:
                    _set_analysis_running(False)  # RC15i.5: cleanup on unexpected exception
                    result = {"error": str(_e)}
                self.root.after(0, lambda: self._on_analysis_done(result, send_tg=True))

            threading.Thread(target=worker, daemon=True).start()
        else:
            self.auto_status.config(
                text=f"⏸ السوق مغلق — تحليل فقط، لا تسجيل Paper Trade {datetime.now().strftime('%H:%M:%S')}"
            )

        self._auto_job_id = self.root.after(self.AUTO_INTERVAL_MS, self._run_auto_cycle)

    def _run_analysis(self):
        if not self._analysis_begin("manual"):
            return
        self.status_dot.config(text="● جاري التحليل...", fg=GOLD)
        self.root.update()

        def worker():
            from core.analyzer import full_analysis  # RC15i.4: lazy
            from core.trade_monitor import _set_analysis_running  # RC15i.5 safety
            def _prog(msg):  # RC15i.5
                self.root.after(0, lambda m=msg: self.status_dot.config(text=m, fg=GOLD))
            try:
                result = full_analysis(progress_cb=_prog)
            except Exception as _e:
                _set_analysis_running(False)  # RC15i.5: cleanup on unexpected exception
                result = {"error": str(_e)}
            self.root.after(0, lambda: self._on_analysis_done(result, send_tg=True))

        threading.Thread(target=worker, daemon=True).start()

    def _run_quick_analysis(self):
        if not self._analysis_begin("quick"):
            return
        self.status_dot.config(text="● جاري التحليل...", fg=GOLD)
        self.root.update()

        def worker():
            from core.analyzer import full_analysis  # RC15i.4: lazy
            from core.trade_monitor import _set_analysis_running  # RC15i.5 safety
            def _prog(msg):  # RC15i.5
                self.root.after(0, lambda m=msg: self.status_dot.config(text=m, fg=GOLD))
            try:
                result = full_analysis(progress_cb=_prog)
            except Exception as _e:
                _set_analysis_running(False)  # RC15i.5: cleanup on unexpected exception
                result = {"error": str(_e)}
            self.root.after(0, lambda: self._on_analysis_done(result, send_tg=False))

        threading.Thread(target=worker, daemon=True).start()

    def _on_analysis_done(self, result, send_tg=False):
        self._analysis_end()
        self._show_analysis_result(result)
        if "error" not in result:
            save_analysis_log(result)
        self.status_dot.config(text="● جاهر", fg=ACCENT)

        # ── Auto Signal Check — يشتغل دائماً (يدوي أو تلقائي) ──────────────
        if "error" not in result:
            try:
                from core.trade_monitor import check_and_log_signal, monitor_open_trades, _qualify
                from core.risk_manager import check_risk

                # تشخيص مرئي: لماذا لم تُسجّل الصفقة؟
                diag_lines = []
                candidates = [
                    ("SPX", result.get("strategy"),
                     result.get("data_quality_report", {}), result.get("price", 0)),
                    ("SPY", (result.get("spy") or {}).get("strategy"),
                     (result.get("spy") or {}).get("data_quality_report", {}),
                     (result.get("spy") or {}).get("price", 0)),
                    ("QQQ", (result.get("qqq") or {}).get("strategy"),
                     (result.get("qqq") or {}).get("data_quality_report", {}),
                     (result.get("qqq") or {}).get("price", 0)),
                ]
                for sym, strat, dq, price in candidates:
                    if not strat:
                        diag_lines.append(f"{sym}: لا بيانات")
                        continue
                    score   = strat.get("score", 0) if strat else 0
                    s_name  = strat.get("strategy", "?") if strat else "?"
                    ok, reason = _qualify(strat, dq or {}, sym)
                    print(f"[auto_qualify] {sym} {s_name} score={score} ok={ok} reason={reason}")
                    if ok:
                        risk = check_risk(strat, sym)
                        if not risk["allowed"]:
                            r_txt = self._compact_ui_text(risk.get("reason", "risk blocked"), 42)
                            diag_lines.append(f"{sym}: Risk blocked — {r_txt}")
                            print(f"[auto_qualify] {sym} RISK BLOCKED: {risk['reason']}")
                        else:
                            diag_lines.append(f"{sym}: ✓ {s_name} {float(score or 0):.0f}")
                    else:
                        diag_lines.append(self._compact_strategy_summary(sym, strat, reason))

                # عرض التشخيص في auto_status مع لون واضح
                if hasattr(self, "auto_status"):
                    has_ok = any("سيُسجَّل" in d for d in diag_lines)
                    self.auto_status.config(
                        text="  |  ".join(diag_lines),
                        fg=ACCENT if has_ok else RED
                    )

                # تسجيل الصفقة — فقط إذا السوق مفتوح.
                # check_and_log_signal نفسه يحتوي Market Gate احتياطي، لكن هذا الفرع
                # يمنع أيضاً ظهور رسالة تسجيل خاطئة في الصفحة الرئيسية وقت الإغلاق.
                logged = check_and_log_signal(result)

                # عرض رسالة السوق المغلق بوضوح: تحليل فقط، لا تسجيل Paper Trade.
                if result.get("_market_closed") or (isinstance(logged, dict) and logged.get("market_closed")):
                    msg = result.get("_market_closed_msg", "السوق مغلق — تحليل فقط، لم يتم تسجيل أي صفقة ورقية")
                    ar_msg = "⏸ السوق مغلق — تحليل فقط، لم يتم تسجيل أي صفقة ورقية"
                    try:
                        self.status_dot.config(text=f"● {ar_msg}", fg=TEXT_DIM)
                    except Exception:
                        pass
                    if hasattr(self, "auto_status"):
                        self.auto_status.config(text=ar_msg, fg=TEXT_DIM)
                    logged = None

                # RC15i.9: Daily Session Journal / Decision Memory
                # يسجل كل دورة تحليل لكل رمز بعد قرار التسجيل/الرفض.
                try:
                    from core.session_journal import write_decision_journal
                    _journal = write_decision_journal(result, source="ui_analysis_done")
                    if _journal.get("ok"):
                        print(f"[session_journal] wrote {_journal.get('rows')} rows | {_journal.get('csv_path')}")
                    else:
                        print(f"[session_journal] skipped/error: {_journal}")
                except Exception as _sj_err:
                    print(f"[session_journal error] {_sj_err}")

                monitor_open_trades(result)

                # مراقبة Paper Trades وإغلاقها تلقائياً
                try:
                    from core.trade_monitor import monitor_paper_trades
                    paper_closed = monitor_paper_trades(result)
                    if paper_closed:
                        print(f"[paper_monitor] أُغلقت {len(paper_closed)} توصية paper")
                        self._refresh_paper()
                        try:
                            self._refresh_trades()
                            self._refresh_stats()
                        except Exception:
                            pass
                except Exception as _pe:
                    print(f"[paper_monitor error] {_pe}")

                # عرض نتيجة التسجيل الورقي على الصفحة الرئيسية بوضوح
                logged_list = []
                try:
                    logged_list = result.get("_paper_logged_trades") or (logged or {}).get("logged_trades") or []
                except Exception:
                    logged_list = []

                if logged or logged_list:
                    self._refresh_auto_trades()
                    try:
                        self._refresh_paper()
                    except Exception:
                        pass
                    if not logged_list and isinstance(logged, dict):
                        logged_list = [logged]
                    parts = []
                    for t in logged_list[:3]:
                        parts.append(f"{t.get('symbol','?')} {t.get('strategy','?')} {float(t.get('score') or 0):.0f}")
                    more = f" +{len(logged_list)-3} أخرى" if len(logged_list) > 3 else ""
                    msg = "✅ Paper Registered: " + " | ".join(parts) + more
                    if hasattr(self, "auto_status"):
                        self.auto_status.config(text=msg, fg=ACCENT)
                    try:
                        self.status_dot.config(text="● Paper Registration — تم تسجيل الصفقة ورقياً", fg=ACCENT)
                    except Exception:
                        pass
                    print(f"[paper_auto_scan] {msg}")
                else:
                    # تحديث جدول الرفض دائماً بعد كل تحليل
                    self._refresh_auto_trades()
            except Exception as _tm_err:
                print(f"[trade_monitor error] {_tm_err}")
                import traceback; traceback.print_exc()
                try:
                    from core.bot_logger import log_error, wd_error
                    log_error("_on_analysis_done / trade_monitor", _tm_err)
                    wd_error(str(_tm_err)[:100])
                except Exception:
                    pass

        # تحديث تبويب الاستراتيجيات
        if "error" not in result:
            try:
                self._show_strategy_comparison(result)
            except Exception as e:
                print(f"[strategy tab error] {e}")

        if send_tg and "error" not in result:
            token = get_setting("telegram_token")
            if token:
                from core.telegram_bot import send_analysis  # RC15i.4: lazy
                ok, msg = send_analysis(result)
                if not ok:
                    messagebox.showwarning("تيليغرام", f"التحليل تم لكن فشل الإرسال:\n{msg}")

    def _test_telegram(self):
        token = get_setting("telegram_token")
        chat_id = get_setting("telegram_chat_id")
        if not token or not chat_id:
            messagebox.showwarning("تنبيه", "يرجى إدخال Telegram Token و Chat ID في الإعدادات أولاً")
            return
        from core.telegram_bot import test_connection  # RC15i.4: lazy
        ok, msg = test_connection(token, chat_id)
        if ok:
            messagebox.showinfo("نجاح", f"✅ {msg}")
        else:
            messagebox.showerror("خطأ", msg)

    def _open_trade_dialog(self):
        TradeDialog(self.root, on_save=self._on_trade_saved)

    def _on_trade_saved(self, trade):
        save_trade(
            date=trade["date"],
            time_str=trade["time"],
            result=trade["result"],
            r_value=trade["r_value"],
            amount=trade["amount"],
            notes=trade["notes"],
            strategy=trade["strategy"],
        )
        self._refresh_stats()
        self._refresh_trades()
        
        # Send to Telegram if configured
        token = get_setting("telegram_token")
        if token:
            from core.telegram_bot import send_trade_notification  # RC15i.4: lazy
            send_trade_notification(trade)

    def _refresh_stats(self):
        stats = get_stats()
        net_r = stats["net_r"]

        self.stat_cards["net_r"].config(
            text=f"{net_r:+.2f}R",
            fg=ACCENT if net_r >= 0 else RED
        )
        self.stat_cards["win_rate"].config(text=f"{stats['win_rate']}%")
        self.stat_cards["total"].config(text=str(stats["total"]))

        # الرصيد
        try:
            from core.database import get_paper_balance
            bal = get_paper_balance()
            self.stat_cards["balance"].config(
                text=f"${bal['available']:,.0f}",
                fg=ACCENT if bal["available"] >= bal["initial"] * 0.5 else RED
            )
            self.stat_cards["reserved"].config(
                text=f"${bal['reserved']:,.0f}",
                fg=GOLD if bal["reserved"] > 0 else TEXT_DIM
            )
        except Exception:
            pass

        # إجمالي المخاطرة (0DTE + Swing)
        try:
            from core.database import get_total_risk_report
            rpt = get_total_risk_report()
            if hasattr(self, "risk_labels"):
                self.risk_labels["risk_0dte"].config(
                    text=f"${rpt['open_risk_0dte']:,.0f}",
                    fg=RED if rpt["open_risk_0dte"] > 0 else TEXT_DIM)
                self.risk_labels["risk_swing"].config(
                    text=f"${rpt['open_risk_swing']:,.0f}",
                    fg=GOLD if rpt["open_risk_swing"] > 0 else TEXT_DIM)
                self.risk_labels["risk_total"].config(
                    text=f"${rpt['total_open_risk']:,.0f}",
                    fg="#ff8c00" if rpt["total_open_risk"] > 0 else TEXT_DIM)
                self.risk_labels["trades_0dte"].config(
                    text=str(rpt["trades_0dte"]))
                self.risk_labels["trades_swing"].config(
                    text=str(rpt["trades_swing"]))
            # أيضاً balance bar في Auto Trades
            if hasattr(self, "balance_labels"):
                initial = bal.get("initial", 10000)
                pnl     = bal.get("balance", initial) - initial
                self.balance_labels["bal_initial"].config(text=f"${initial:,.0f}")
                self.balance_labels["bal_available"].config(
                    text=f"${bal.get('available',0):,.0f}",
                    fg=ACCENT if bal.get("available",0) >= initial * 0.5 else RED)
                self.balance_labels["bal_reserved"].config(
                    text=f"${bal.get('reserved',0):,.0f}",
                    fg=GOLD if bal.get("reserved",0) > 0 else TEXT_DIM)
                self.balance_labels["bal_pnl"].config(
                    text=f"${pnl:+,.0f}",
                    fg=ACCENT if pnl >= 0 else RED)
        except Exception:
            pass

    # ── تحديث مزدوج: UI كل 5 ث، API adaptive ────────────────────
    _PRICE_REFRESH_INTERVAL       = 7_000    # full refresh: legacy + Swing + all paper
    _FAST_0DTE_REFRESH_INTERVAL   = 1_000    # RC15i.10: 0DTE exit check every ~1s
    _FULL_REFRESH_MIN_SECONDS     = 7.0      # throttle heavy refresh while fast 0DTE loop is active
    _UI_REFRESH_INTERVAL          = 5_000    # تحديث الجدول من قاعدة البيانات

    def _has_open_0dte_paper_trade(self):
        """True when any open Paper Trade is 0DTE; used to enable fast TP/SL polling."""
        try:
            rows = get_paper_trades(status="open")
            for t in rows or []:
                mode = str(t.get("selected_mode") or t.get("mode") or "").strip().upper()
                if mode == "0DTE":
                    return True
        except Exception:
            return False
        return False

    def _next_price_refresh_interval_ms(self):
        return self._FAST_0DTE_REFRESH_INTERVAL if self._has_open_0dte_paper_trade() else self._PRICE_REFRESH_INTERVAL

    def _schedule_next_price_refresh(self):
        if getattr(self, "_closing", False):
            return
        try:
            self.root.after(self._next_price_refresh_interval_ms(), self._start_auto_refresh)
        except Exception:
            pass

    def _start_ui_refresh(self):
        """تحديث جداول الواجهة من قاعدة البيانات كل 5 ثوانٍ — بدون API.

        RC15d: Paper Trades must repaint even when no trade is closed.
        The background price loop may update current_value/P&L/LastCh in
        trades.db, but previously the Paper Trades tab was only repainted when
        paper_closed was non-empty. That made open 0DTE trades look stale while
        the database/exit monitor could already have newer values.
        """
        # RC15i.2: do not touch destroyed widgets or schedule another after()
        # callback once the main window is closing.
        if getattr(self, "_closing", False):
            return
        try:
            self._refresh_auto_trades()
        except Exception:
            pass
        try:
            if hasattr(self, "_refresh_paper"):
                self._refresh_paper()
        except Exception:
            pass
        if not getattr(self, "_closing", False):
            try:
                self.root.after(self._UI_REFRESH_INTERVAL, self._start_ui_refresh)
            except Exception:
                pass

    def _start_auto_refresh(self):
        """Adaptive price refresh. 0DTE exits are checked fast; heavy refresh is throttled."""
        if getattr(self, "_closing", False):
            return

        def worker():
            try:
                from core.trade_monitor import (refresh_open_trade_prices,
                                                 refresh_paper_trade_prices)
                fast_0dte = self._has_open_0dte_paper_trade()
                now_ts = time.time()
                do_full = (
                    (not fast_0dte)
                    or (now_ts - float(getattr(self, "_last_full_price_refresh_ts", 0.0)) >= self._FULL_REFRESH_MIN_SECONDS)
                )

                closed = []
                paper_closed = []
                if do_full:
                    # Heavy cycle: legacy open-trade refresh + Swing + all Paper Trades.
                    closed = refresh_open_trade_prices()
                    paper_closed = refresh_paper_trade_prices()
                    self._last_full_price_refresh_ts = now_ts
                else:
                    # Fast cycle: only open 0DTE Paper Trades. This keeps TP/SL responsive
                    # without pulling Swing chains every second.
                    paper_closed = refresh_paper_trade_prices(only_0dte=True)

                # RC15d/RC15i.10: repaint Paper Trades after each price cycle.
                if not getattr(self, "_closing", False):
                    try:
                        self.root.after(0, self._refresh_paper)
                    except Exception:
                        pass

                if (closed or paper_closed) and not getattr(self, "_closing", False):
                    try:
                        self.root.after(0, self._refresh_trades)
                        self.root.after(0, self._refresh_stats)
                    except Exception:
                        pass
            except Exception as e:
                print(f"[price_refresh] {e}")
                try:
                    from core.bot_logger import log_error
                    log_error("_start_auto_refresh", e)
                except Exception:
                    pass
            finally:
                # Schedule after the worker finishes; avoids stacked overlapping threads
                # when an API call takes longer than the 1s fast interval.
                if not getattr(self, "_closing", False):
                    try:
                        self.root.after(0, self._schedule_next_price_refresh)
                    except Exception:
                        pass

        threading.Thread(target=worker, daemon=True).start()


# ── Trade Dialog ──────────────────────────────────────────────
class TradeDialog(tk.Toplevel):
    def __init__(self, parent, on_save=None):
        super().__init__(parent)
        self.on_save = on_save
        self.title("تسجيل صفقة منفذة")
        self.geometry("460x480")
        self.configure(bg=BG2)
        self.resizable(False, False)
        self.grab_set()
        self._build()

    def _build(self):
        tk.Label(self, text="تسجيل صفقة منفذة", bg=BG2, fg=WHITE,
                 font=("Segoe UI", 14, "bold")).pack(pady=(20, 15))

        form = tk.Frame(self, bg=BG2)
        form.pack(fill="x", padx=30)

        self.vars = {}
        now = datetime.now()

        fields = [
            ("date", "التاريخ", now.strftime("%Y-%m-%d")),
            ("time", "الوقت", now.strftime("%H:%M")),
            ("strategy", "الاستراتيجية", "Iron Condor"),
            ("amount", "المبلغ ($)", "100"),
            ("r_value", "قيمة R", "1.0"),
            ("notes", "ملاحظات", ""),
        ]

        for key, label, default in fields:
            row = tk.Frame(form, bg=BG2)
            row.pack(fill="x", pady=5)
            tk.Label(row, text=label, bg=BG2, fg=TEXT,
                     font=("Segoe UI", 10), width=18, anchor="e").pack(side="right", padx=8)
            var = tk.StringVar(value=default)
            tk.Entry(row, textvariable=var, bg=CARD, fg=WHITE,
                     insertbackground=WHITE, relief="flat",
                     font=("Segoe UI", 10)).pack(side="right", fill="x", expand=True, ipady=7)
            self.vars[key] = var

        # Result toggle
        result_row = tk.Frame(form, bg=BG2)
        result_row.pack(fill="x", pady=8)
        tk.Label(result_row, text="النتيجة", bg=BG2, fg=TEXT,
                 font=("Segoe UI", 10), width=18, anchor="e").pack(side="right", padx=8)
        self.result_var = tk.StringVar(value="ربح")
        for val, color in [("ربح", ACCENT), ("خسارة", RED)]:
            tk.Radiobutton(result_row, text=val, variable=self.result_var, value=val,
                           bg=BG2, fg=color, activebackground=BG2, activeforeground=color,
                           selectcolor=CARD, font=("Segoe UI", 10, "bold")).pack(side="right", padx=8)

        # Buttons
        btn_row = tk.Frame(self, bg=BG2)
        btn_row.pack(fill="x", padx=30, pady=20)
        tk.Button(btn_row, text="إلغاء", bg=CARD, fg=TEXT,
                  relief="flat", cursor="hand2", font=("Segoe UI", 10),
                  command=self.destroy, padx=20, pady=8).pack(side="left")
        tk.Button(btn_row, text="💾  حفظ الصفقة", bg=GOLD, fg="#000",
                  relief="flat", cursor="hand2", font=("Segoe UI", 10, "bold"),
                  command=self._save, padx=20, pady=8).pack(side="right")

    def _save(self):
        try:
            trade = {
                "date": self.vars["date"].get(),
                "time": self.vars["time"].get(),
                "strategy": self.vars["strategy"].get() or "Iron Condor",
                "amount": float(self.vars["amount"].get()),
                "r_value": float(self.vars["r_value"].get()),
                "notes": self.vars["notes"].get(),
                "result": self.result_var.get(),
                "type": "MANUAL",
            }
            # Make R negative for losses
            if trade["result"] == "خسارة" and trade["r_value"] > 0:
                trade["r_value"] = -trade["r_value"]

            if self.on_save:
                self.on_save(trade)
            self.destroy()
        except ValueError as e:
            messagebox.showerror("خطأ", f"تحقق من القيم المدخلة:\n{e}", parent=self)
