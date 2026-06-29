"""
config/settings.py
==================
إعدادات البوت الرئيسية - ICT Pro Bot (Abu Hassan Bot)
يقرأ من ملف .env ويُعرّف كل ثوابت النظام
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# تحميل متغيرات البيئة
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# ============================================================
# إعدادات Tastytrade API
# ============================================================
TASTYTRADE_USERNAME = os.getenv("TASTYTRADE_USERNAME", "")
TASTYTRADE_PASSWORD = os.getenv("TASTYTRADE_PASSWORD", "")
TASTYTRADE_SANDBOX = os.getenv("TASTYTRADE_SANDBOX", "false").lower() == "true"
TASTYTRADE_BASE_URL = os.getenv(
    "TASTYTRADE_BASE_URL", "https://api.tastytrade.com"
)
TASTYTRADE_TIMEOUT = 2.5          # ثانية - الحد الأقصى لوقت الاستجابة
TASTYTRADE_PRICE_MISMATCH_THRESHOLD = 3.0  # نقطة - عتبة الفرق في السعر

# ============================================================
# إعدادات DXLink (WebSocket احتياطي)
# ============================================================
DXLINK_URL = os.getenv(
    "DXLINK_URL", "wss://tasty-openapi-ws.dxfeed.com/realtime"
)

# ============================================================
# إعدادات تيليغرام
# ============================================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"

# ============================================================
# أعلام التداول - الأمان أولاً
# ============================================================
LIVE_TRADING_ENABLED = False      # دائماً False - لا تغيّر هذا!
PAPER_TRADING_ENABLED = os.getenv("PAPER_TRADING_ENABLED", "true").lower() == "true"
ICT_SHADOW_MODE = os.getenv("ICT_SHADOW_MODE", "true").lower() == "true"
SCHWAB_ENABLED = False            # غير متاح في السعودية

# ============================================================
# قاعدة البيانات
# ============================================================
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR}/data/ict_pro_bot.db")

# ============================================================
# قوائم المراقبة والرموز
# ============================================================

# 0DTE - التداول اليومي
SYMBOLS_0DTE = ["SPX"]

# Swing - التداول متوسط المدى (21-45 يوم)
SYMBOLS_SWING = ["SPY", "QQQ", "IWM", "DIA"]

# الأسهم الفردية
SYMBOLS_INDIVIDUAL = ["AAPL", "NVDA", "GLD"]

# كل الرموز
ALL_SYMBOLS = SYMBOLS_0DTE + SYMBOLS_SWING + SYMBOLS_INDIVIDUAL

# ============================================================
# فلاتر الخيارات
# ============================================================
MIN_OPTION_VOLUME = 500           # الحد الأدنى للحجم
MIN_OPTION_OI = 1000              # الحد الأدنى للاهتمام المفتوح
MIN_DTE_SWING = 21                # أقل مدة للـ Swing
MAX_DTE_SWING = 45                # أعلى مدة للـ Swing
WING_WIDTH_MIN = 3                # أدنى عرض الجناح (بالدولار)
WING_WIDTH_MAX = 5                # أقصى عرض الجناح (بالدولار)

# ============================================================
# عتبات الـ IV (Implied Volatility)
# ============================================================
IV_HIGH_THRESHOLD = 70            # فوق 70% → استراتيجيات ائتمانية
IV_LOW_THRESHOLD = 30             # دون 30% → استراتيجيات مدينة
# المنطقة الرمادية: 30-70% → انتظار

# ============================================================
# إدارة المراكز
# ============================================================
TAKE_PROFIT_PERCENT = 50          # 50% من الربح الأقصى
STOP_LOSS_CREDIT_MULTIPLIER = 2   # 2x الائتمان المستلم (للإستراتيجيات الائتمانية)
STOP_LOSS_DEBIT_PERCENT = 50      # 50% من المدفوع (للإستراتيجيات المدينة)

# ============================================================
# أوقات الجلسات (بتوقيت ET)
# ============================================================
MORNING_SESSION_START = "09:45"
MORNING_SESSION_END = "11:30"
AFTERNOON_SESSION_START = "14:00"
AFTERNOON_SESSION_END = "15:30"
TIMEZONE = os.getenv("TIMEZONE", "America/New_York")

# ============================================================
# إدارة المخاطر
# ============================================================
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "500"))
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "10000"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "5"))

# ============================================================
# إعدادات SMC / ICT
# ============================================================
# درجات التحليل ICT
ICT_GRADE_A_PLUS_THRESHOLD = 90   # A+: فوق 90% توافق
ICT_GRADE_A_THRESHOLD = 75        # A: فوق 75% توافق
ICT_GRADE_B_THRESHOLD = 60        # B: فوق 60% توافق

# الإطارات الزمنية للتحليل (من الأعلى للأدنى)
TIMEFRAMES = ["1W", "1D", "4H", "1H", "15M", "5M"]

# عدد الشموع للتحليل
CANDLE_LOOKBACK = 100

# ============================================================
# إعدادات GEX
# ============================================================
GEX_PIN_SCORE_THRESHOLD = 0.7     # عتبة Pin Score
GEX_WALL_STRENGTH_MIN = 0.5       # الحد الأدنى لقوة الجدار

# ============================================================
# إعدادات اليومية
# ============================================================
DASHBOARD_REFRESH_SECONDS = 30    # تحديث الواجهة كل 30 ثانية
ANALYSIS_INTERVAL_SECONDS = 60    # تحليل كل 60 ثانية
JOURNAL_FILE_PREFIX = "session_journal"

# ============================================================
# معلومات البوت
# ============================================================
BOT_NAME = os.getenv("BOT_NAME", "ICT Pro Bot - Abu Hassan")
BOT_VERSION = "1.0.0"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ============================================================
# مراحل التطوير (Feature Flags)
# ============================================================
PHASE_M1_SHADOW_MODE = True       # M1: وضع المظلة للـ ICT
PHASE_M2_SIGNALS = True           # M2: إشارات التداول
PHASE_M3_PAPER_TRADING = True     # M3: التداول الورقي
PHASE_M4_BACKTEST = False         # M4: الاختبار التاريخي (قيد التطوير)
PHASE_M5_LIVE = False             # M5: التداول الحقيقي (مغلق دائماً)
