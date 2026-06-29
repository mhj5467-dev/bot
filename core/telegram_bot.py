"""
Telegram Reporter - Sends analysis to Telegram channel/group
"""
import requests
from core.database import get_setting


MAX_TG_LEN = 4000  # حد تيليغرام 4096، نترك هامش


def _send_chunk(url: str, chat_id: str, text: str, parse_mode: str = "HTML",
                _retries: int = 3, _backoff: float = 2.0):
    """
    إرسال رسالة واحدة مع retry تلقائي عند انقطاع الشبكة (ConnectionReset, Timeout...).
    يُعيد dict بـ ok=False عند استنفاد المحاولات.
    """
    import time as _time
    import requests as _req
    last_exc = None
    for attempt in range(1, _retries + 1):
        try:
            resp = _req.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
                timeout=12,
            )
            return resp.json()
        except (_req.exceptions.ConnectionError,
                _req.exceptions.Timeout,
                OSError) as exc:
            last_exc = exc
            if attempt < _retries:
                _time.sleep(_backoff * attempt)   # 2s, 4s بين المحاولات
    return {"ok": False, "description": f"Network error after {_retries} retries: {last_exc}"}


def send_message(text, token=None, chat_id=None):
    """إرسال رسالة — يقسمها تلقائياً إذا تجاوزت 4000 حرف."""
    token   = token   or get_setting("telegram_token")
    chat_id = chat_id or get_setting("telegram_chat_id")

    if not token or not chat_id:
        return False, "لم يتم ضبط Telegram Token أو Chat ID في الإعدادات"

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # تقسيم الرسالة الطويلة على فواصل السطور
    chunks = []
    if len(text) <= MAX_TG_LEN:
        chunks = [text]
    else:
        lines = text.split("\n")
        current = ""
        for line in lines:
            if len(current) + len(line) + 1 > MAX_TG_LEN:
                if current:
                    chunks.append(current.strip())
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            chunks.append(current.strip())

    try:
        import re as _re
        last_ok, last_msg = False, ""
        for chunk in chunks:
            data = _send_chunk(url, chat_id, chunk)
            if data.get("ok"):
                last_ok, last_msg = True, "تم الإرسال بنجاح"
            else:
                # إذا فشل HTML: نُزيل الـ tags ونُرسل نصاً نظيفاً
                clean = _re.sub(r"<[^>]+>", "", chunk)
                data2 = _send_chunk(url, chat_id, clean, parse_mode="")
                if data2.get("ok"):
                    last_ok, last_msg = True, "تم الإرسال (بدون تنسيق)"
                else:
                    last_ok  = False
                    last_msg = f"خطأ: {data2.get('description', data.get('description', 'Unknown'))}"
        return last_ok, last_msg
    except Exception as exc:
        return False, f"خطأ: {exc}"


def _compact_legs(strategy: dict) -> str:
    """سطر مختصر للأرجل والأرقام المالية لرسائل التيليغرام."""
    name = strategy.get("strategy", "")
    lines = []

    if name == "Iron Condor":
        sp, lp = strategy.get("short_put"),  strategy.get("long_put")
        sc, lc = strategy.get("short_call"), strategy.get("long_call")
        if sp and lp: lines.append(f"  Sell Put {sp:,.0f} / Buy Put {lp:,.0f}")
        if sc and lc: lines.append(f"  Sell Call {sc:,.0f} / Buy Call {lc:,.0f}")
    elif name == "Bull Put Spread":
        sp, lp = strategy.get("short_put"), strategy.get("long_put")
        if sp and lp: lines.append(f"  Sell Put {sp:,.0f} / Buy Put {lp:,.0f}")
    elif name == "Bear Call Spread":
        sc, lc = strategy.get("short_call"), strategy.get("long_call")
        if sc and lc: lines.append(f"  Sell Call {sc:,.0f} / Buy Call {lc:,.0f}")
    elif name == "Call Debit Spread":
        lc, sc = strategy.get("long_call"), strategy.get("short_call")
        if lc and sc: lines.append(f"  Buy Call {lc:,.0f} / Sell Call {sc:,.0f}")
    elif name == "Put Debit Spread":
        lp, sp = strategy.get("long_put"), strategy.get("short_put")
        if lp and sp: lines.append(f"  Buy Put {lp:,.0f} / Sell Put {sp:,.0f}")

    # الأرقام المالية
    is_credit = name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
    credit = strategy.get("credit")
    debit  = strategy.get("debit")
    ml     = strategy.get("max_loss")
    mg     = strategy.get("max_gain")
    pop    = strategy.get("pop")

    # Width
    width = None
    if name == "Bull Put Spread":
        sp, lp = strategy.get("short_put",0) or 0, strategy.get("long_put",0) or 0
        if sp and lp: width = abs(sp - lp)
    elif name == "Bear Call Spread":
        sc, lc = strategy.get("short_call",0) or 0, strategy.get("long_call",0) or 0
        if sc and lc: width = abs(lc - sc)
    elif name == "Iron Condor":
        sp, lp = strategy.get("short_put", 0) or 0, strategy.get("long_put", 0) or 0
        sc2, lc2 = strategy.get("short_call", 0) or 0, strategy.get("long_call", 0) or 0
        put_w  = abs(sp - lp)  if sp and lp  else 0
        call_w = abs(lc2 - sc2) if sc2 and lc2 else 0
        if put_w or call_w:
            width = max(put_w, call_w)
    elif name in ("Call Debit Spread", "Put Debit Spread"):
        a = strategy.get("long_call") or strategy.get("long_put") or 0
        b = strategy.get("short_call") or strategy.get("short_put") or 0
        if a and b: width = abs(a - b)

    nums = []
    if width:    nums.append(f"Width={width:.0f}")
    if credit:   nums.append(f"Credit={credit:.2f}")
    if debit:    nums.append(f"Debit={debit:.2f}")
    if ml:       nums.append(f"MaxLoss={ml:.2f}")
    if mg:       nums.append(f"MaxProfit={mg:.2f}")
    if pop:      nums.append(f"POP={pop:.0f}%")
    if nums:
        lines.append("  " + "  |  ".join(nums))

    return "\n".join(lines)


def _spread_warnings_short(strat: dict) -> list:
    """استخرج تحذيرات السيولة المختصرة للأرجل."""
    lines = []
    for leg in strat.get("legs_detail", []):
        if not leg:
            continue
        strike = leg.get("strike")
        sp_pct = leg.get("spread_pct")
        if strike is None:
            continue
        if sp_pct is None:
            lines.append(f"  {strike:,.0f} = لا bid/ask ⛔")
        elif sp_pct > 25:
            icon = "⛔" if sp_pct > 50 else "⚠️"
            lines.append(f"  {strike:,.0f} = {sp_pct:.0f}% {icon}")
    return lines


def _format_winner_block(sym_key: str, analysis: dict, et_time: str) -> str:
    """تنسيق بلوك الفائز المختصر."""
    if sym_key == "spx":
        strat = analysis.get("strategy") or {}
        price = analysis.get("price", 0)
    else:
        d     = analysis.get(sym_key) or {}
        strat = d.get("strategy") or {}
        price = d.get("price", 0)

    name     = strat.get("strategy", "")
    score    = strat.get("score", 0)
    is_credit= name in ("Iron Condor", "Bull Put Spread", "Bear Call Spread")
    credit   = strat.get("credit")
    debit    = strat.get("debit")
    ml       = strat.get("max_loss")
    mg       = strat.get("max_gain")
    pop      = strat.get("pop")
    sym      = sym_key.upper()

    lines = [
        f"🎯 <b>أفضل فرصة</b>",
        f"",
        f"<b>{sym} — {name}</b>",
        f"Score: {score}/100",
        f"",
    ]

    # الأرجل — مرتبة حسب نوع الاستراتيجية
    if name == "Put Debit Spread":
        leg_order = ["long_put", "short_put"]
    elif name == "Call Debit Spread":
        leg_order = ["long_call", "short_call"]
    elif name == "Bull Put Spread":
        leg_order = ["short_put", "long_put"]
    elif name == "Bear Call Spread":
        leg_order = ["short_call", "long_call"]
    else:  # Iron Condor
        leg_order = ["short_call", "long_call", "short_put", "long_put"]

    leg_labels = {
        "short_put":  "📟 Sell Put",
        "long_put":   "📟 Buy Put",
        "short_call": "📞 Sell Call",
        "long_call":  "📞 Buy Call",
    }
    for key in leg_order:
        val = strat.get(key)
        if val:
            lines.append(f"{leg_labels[key]} {val:,.0f}")

    lines.append("")

    # الأرقام المالية
    if credit:  lines.append(f"💵 Credit: {credit:.2f}")
    if debit:   lines.append(f"💵 Debit: {debit:.2f}")
    if ml:      lines.append(f"⚖️ Max Loss: ${ml*100:.0f}")
    if mg:      lines.append(f"🎯 Max Gain: ${mg*100:.0f}")
    if pop:     lines.append(f"📈 POP: {pop:.0f}%")

    # تحذيرات السيولة
    spread_warns = _spread_warnings_short(strat)
    if spread_warns:
        lines += ["", "⚠️ Spread:"] + spread_warns

    lines += ["", f"🕒 {et_time} ET"]
    return "\n".join(lines)


def format_analysis_message(analysis):
    """رسالة تيليغرام مختصرة — أفضل فرصة أو لا توجد فرصة."""
    if "error" in analysis:
        return f"❌ {analysis['error']}"

    # وقت ET
    try:
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        is_edt  = 3 < now_utc.month < 11
        et = now_utc + timedelta(hours=-4 if is_edt else -5)
        et_time = et.strftime("%H:%M")
    except Exception:
        et_time = analysis.get("time", "")

    best     = analysis.get("best_opportunity", {})
    winner   = best.get("winner")
    has_opp  = best.get("has_opportunity", False)
    all_cands= best.get("all_candidates", [])

    # ── حالة وجود فرصة ──
    if has_opp and winner:
        sym_key = winner.get("symbol", "SPX").lower()
        return _format_winner_block(sym_key, analysis, et_time)

    # ── لا توجد فرصة ──
    lines = [
        "🚫 <b>لا توجد فرصة حالياً</b>",
        "",
    ]

    # درجات كل رمز
    score_map = {}
    for c in all_cands:
        score_map[c.get("symbol", "?")] = c.get("raw_score", c.get("score", 0))

    for sym in ["SPX", "SPY", "QQQ"]:
        sc = score_map.get(sym, 0)
        icon = "🟢" if sc >= 65 else ("🟡" if sc >= 50 else "🔴")
        lines.append(f"{icon} {sym}: {sc}/100")

    # الحدود الدنيا
    lines += ["", "الحد الأدنى المطلوب:"]
    try:
        from core.strategy_engine import get_strategy_min_score
        lines.append(f"Iron Condor = {get_strategy_min_score('Iron Condor')}")
        lines.append(f"Bull Put    = {get_strategy_min_score('Bull Put Spread')}")
        lines.append(f"Call Debit  = {get_strategy_min_score('Call Debit Spread')}")
    except Exception:
        lines.append("IC=60  BP=55  CD=45")

    lines += ["", f"🕒 {et_time} ET"]
    return "\n".join(lines)

# ── الدالة القديمة محذوفة — استُبدلت بـ format_analysis_message المختصرة ──

def _format_analysis_message_LEGACY(analysis):
    """النسخة القديمة — محفوظة للرجوع إليها فقط."""
    if "error" in analysis:
        return f"❌ {analysis['error']}"

    price = analysis["price"]
    pin_score = analysis["pin_score"]
    magnetic = analysis["magnetic"]
    pin_label = analysis["pin_label"]

    if pin_score >= 70:
        score_icon = "🟢"
    elif pin_score >= 50:
        score_icon = "🟡"
    else:
        score_icon = "🔴"

    # Best Opportunity header
    best      = analysis.get("best_opportunity", {})
    winner    = best.get("winner", {})
    has_opp   = best.get("has_opportunity", False)
    all_cands = best.get("all_candidates", [])

    best_lines = []
    if has_opp:
        best_lines += [
            "🏆 <b>أفضل فرصة حالياً</b>",
            f"{winner.get('emoji','')} <b>{winner.get('symbol','')} — {winner.get('strategy','')}</b>  {winner.get('score',0)}/100",
            f"  {winner.get('decision','')}",
        ]
    else:
        best_lines += ["🚫 <b>لا توجد فرصة حالياً في أي رمز</b>"]

    if all_cands:
        best_lines.append("📊 مقارنة الرموز:")
        for c in all_cands:
            marker = "◀" if c["symbol"] == winner.get("symbol") else " "
            best_lines.append(f"  {marker} {c['symbol']:4}  {c['strategy'][:20]}  {c['score']}/100")

    lines = best_lines + [
        "",
        "━━━━━━━━━━━━━━━━━━",
        f"📊 <b>تفاصيل SPX</b>",
        f"📅 {analysis.get('date')} — {analysis.get('time')}",
        f"",
        f"💰 <b>السعر:</b> ${price:,.2f}",
        f"",
        f"{score_icon} <b>Pin Score: {pin_score}/100</b>",
        f"🧲 <b>المغناطيس:</b> ${magnetic:,}",
        f"{'⚠️ المغناطيس ضعيف — لا تركّب الآن' if pin_score < 50 else pin_label}",
    ]
    
    # Targets UP
    if analysis.get("targets_up"):
        lines += ["", "🎯 <b>الأهداف السعرية — صعود ⬆️</b>"]
        for t in analysis["targets_up"]:
            labels_str = " · ".join(t["labels"])
            lines.append(f"  🔵 ${t['price']:,.0f} · {t['pct']:+.2f}%")
            lines.append(f"  <i>{labels_str} · {t['strength']}</i>")
    
    # Targets DOWN
    if analysis.get("targets_down"):
        lines += ["", "🎯 <b>الأهداف السعرية — هبوط ⬇️</b>"]
        for t in analysis["targets_down"]:
            labels_str = " · ".join(t["labels"])
            lines.append(f"  🔵 ${t['price']:,.0f} · {t['pct']:+.2f}%")
            lines.append(f"  <i>{labels_str} · {t['strength']}</i>")
    
    # Market context
    market_bits = []
    if analysis.get("vix") is not None:
        market_bits.append(f"VIX≈ {analysis.get('vix'):.2f}")
    if analysis.get("iv_current") is not None:
        market_bits.append(f"IV Now≈ {analysis.get('iv_current'):.2f}%")
    if analysis.get("iv_rank") is not None:
        market_bits.append(f"IV Rank≈ {analysis.get('iv_rank'):.1f}")
    if analysis.get("daily_trend"):
        market_bits.append(f"Daily: {analysis.get('daily_trend')}")
    if analysis.get("intraday_trend"):
        market_bits.append(f"15m: {analysis.get('intraday_trend')}")
    if analysis.get("trend"):
        market_bits.append(f"Combined: {analysis.get('trend')}")
    if analysis.get("ema20") is not None and analysis.get("ema50") is not None:
        market_bits.append(f"EMA20/50≈ {analysis.get('ema20'):,.0f}/{analysis.get('ema50'):,.0f}")
    if analysis.get("ema20_15m") is not None:
        market_bits.append(f"EMA20(15m)≈ {analysis.get('ema20_15m'):,.0f}")
    if market_bits:
        lines += ["", "🌐 <b>Market Context</b>"]
        lines.append(" • " + " | ".join(market_bits))

    # Strategy Engine — المحرك الجديد
    strategy = analysis.get("strategy")
    if strategy and not strategy.get("no_trade"):
        legs_txt = _compact_legs(strategy)
        if legs_txt:
            lines += ["", f"📍 <b>SPX {strategy.get('strategy','')}  {strategy.get('score',0)}/100</b>"]
            lines.append(legs_txt)
    if strategy:
        try:
            from core.strategy_engine import format_strategy_telegram
            lines.append(format_strategy_telegram(strategy, analysis.get("price", 0)))
        except Exception:
            # Fallback للعرض البسيط
            s_name = strategy.get("strategy", "")
            s_emoji = strategy.get("emoji", "🎯")
            s_conf = strategy.get("confidence", 0)
            s_decision = strategy.get("decision", "")
            lines += [
                "",
                f"{s_emoji} <b>الاستراتيجية المختارة: {s_name}</b>",
                f"🧭 الثقة: {s_conf}/100",
                f"📋 {s_decision}",
            ]
            reasons = strategy.get("reasons", [])
            if reasons:
                lines.append("📊 الأسباب: " + " | ".join(reasons[:3]))
    else:
        # Fallback: Iron Condor القديم
        condor = analysis.get("iron_condor")
        if condor:
            warnings = condor.get("warnings") or []
            lines += [
                "",
                "🧩 <b>اقتراح Iron Condor — للدراسة فقط</b>",
                f"⬆️ Call: Sell {condor['short_call']:,.0f} / Buy {condor['long_call']:,.0f}",
                f"⬇️ Put: Sell {condor['short_put']:,.0f} / Buy {condor['long_put']:,.0f}",
                f"💵 Credit تقريبي: {condor.get('credit', 0):.2f}",
                f"⚖️ Max Loss تقريبي: {condor.get('max_loss', 0):.2f}",
                f"📈 POP≈ {condor.get('pop', 0):.1f}%",
                f"📊 Score: {condor.get('score', 0)}/100 — {condor.get('decision', '')}",
            ]
            trade_filter = analysis.get("trade_filter") or {}
            if trade_filter:
                lines.append(f"🧭 قرار الخطة: <b>{trade_filter.get('decision', '')}</b>")
            if warnings:
                lines.append("⚠️ " + " | ".join(warnings[:3]))

    em = analysis.get("expected_move")
    if em:
        lines += ["", f"📐 Expected Move تقريبي: ±{em}"]

    flow_lines = []
    if analysis.get("zero_gamma") is not None:
        flow_lines.append(f"Zero Gamma≈ {analysis['zero_gamma']:,.0f}")
    if analysis.get("net_gex") is not None:
        flow_lines.append(f"Net GEX≈ {analysis['net_gex']:,.0f}")
    if analysis.get("net_dex") is not None:
        flow_lines.append(f"Net DEX≈ {analysis['net_dex']:,.0f}")
    if analysis.get("net_vanna") is not None:
        flow_lines.append(f"VannaExp≈ {analysis['net_vanna']:,.0f}")
    if analysis.get("net_charm") is not None:
        flow_lines.append(f"CharmExp≈ {analysis['net_charm']:,.0f}")
    if flow_lines:
        lines += ["", "🧮 <b>Greeks Exposure تقديري</b>"]
        lines += [" • " + x for x in flow_lines]

    wall_lines = []
    for label, key in [("Top Call GEX", "top_call_gex"), ("Top Put GEX", "top_put_gex")]:
        rows = analysis.get(key) or []
        if rows:
            vals = []
            for r in rows[:3]:
                try:
                    vals.append(f"{float(r.get('strike')):,.0f}")
                except Exception:
                    pass
            if vals:
                wall_lines.append(f"{label}: " + ", ".join(vals))
    if wall_lines:
        lines += ["", "🧱 <b>أهم الجدران</b>"]
        lines += [" • " + x for x in wall_lines]

    # SPY / QQQ تحليل كامل
    for sym_key, sym_name in [("spy", "SPY"), ("qqq", "QQQ")]:
        sym_data = analysis.get(sym_key, {})
        if not sym_data or "error" in sym_data:
            continue
        try:
            from core.strategy_engine import format_strategy_telegram
            sym_price   = sym_data.get("price", 0)
            sym_pin     = sym_data.get("pin_score", 0)
            sym_trend   = sym_data.get("trend_label", "")
            sym_gex     = sym_data.get("net_gex") or 0
            sym_iv      = sym_data.get("iv_rank")
            sym_em      = sym_data.get("expected_move")
            sym_strat   = sym_data.get("strategy")
            gex_sign    = "+" if sym_gex > 0 else ""
            pin_icon    = "🟢" if sym_pin >= 70 else ("🟡" if sym_pin >= 50 else "🔴")

            lines += [
                "",
                "━━━━━━━━━━━━━━━━━━",
                f"📊 <b>تحليل {sym_name}</b>  —  ${sym_price:,.2f}",
                f"{pin_icon} Pin: <b>{sym_pin}/100</b>  |  {sym_trend}",
                f"GEX: {gex_sign}{sym_gex:,.0f}" +
                (f"  |  IV Rank: {sym_iv:.0f}" if sym_iv is not None else "") +
                (f"  |  EM: ±{sym_em}" if sym_em else ""),
            ]
            if sym_strat and not sym_strat.get("no_trade"):
                legs_txt = _compact_legs(sym_strat)
                if legs_txt:
                    lines += [
                        f"📍 <b>{sym_strat.get('strategy','')}  {sym_strat.get('score',0)}/100</b>",
                        legs_txt,
                    ]
            if sym_strat:
                lines.append(format_strategy_telegram(sym_strat, sym_price))
        except Exception:
            pass

    lines += [
        "",
        "━━━━━━━━━━━━━━━━━━",
        "🤖 <i>SPX Trading Bot</i>",
    ]
    
    return "\n".join(lines)


def format_sigma_debug_telegram(analysis: dict) -> str:
    """
    رسالة Telegram تفصيلية لحساب 1.5σ — تُرسَل بعد رسالة التحليل الرئيسية.
    تتضمن: السعر، ATM straddle، EM، 1.5σ distance، العقود المختارة، فحوصات القبول/الرفض.
    """
    if not analysis or "error" in analysis:
        return ""

    price  = analysis.get("price", 0)
    sym    = analysis.get("symbol", "SPX")
    levels = analysis.get("levels", {})
    chain  = analysis.get("_chain")
    strat  = analysis.get("strategy") or {}

    if not price:
        return ""

    def fv(v, d=2):
        if v is None: return "N/A"
        try: return f"{float(v):.{d}f}"
        except Exception: return str(v)

    # ── بيانات EM ────────────────────────────────────────────────────────────
    em         = levels.get("expected_move") or 0
    em_src     = levels.get("expected_move_source", "?")
    straddle   = levels.get("straddle_mid")
    atm_strike = levels.get("em_atm_strike")
    expiry     = (chain or {}).get("expiry", "?")
    hours      = (chain or {}).get("hours_to_expiry", 0)

    atm_call = None
    atm_put  = None
    if chain and atm_strike:
        atm_call = next((c for c in chain.get("calls", []) if c.get("strike") == atm_strike), None)
        atm_put  = next((p for p in chain.get("puts",  []) if p.get("strike") == atm_strike), None)

    c_bid = fv((atm_call or {}).get("bid"))
    c_ask = fv((atm_call or {}).get("ask"))
    c_mid = fv((atm_call or {}).get("mid"))
    p_bid = fv((atm_put  or {}).get("bid"))
    p_ask = fv((atm_put  or {}).get("ask"))
    p_mid = fv((atm_put  or {}).get("mid"))

    dist     = round(em * 1.5, 1)
    bp_tgt   = round(price - dist, 1)
    bc_tgt   = round(price + dist, 1)

    # ── بيانات العقود ────────────────────────────────────────────────────────
    strat_name = strat.get("strategy", "?")
    no_trade   = strat.get("no_trade", True)
    score      = strat.get("score", 0)
    credit     = strat.get("credit")
    decision   = strat.get("decision", "")
    warnings   = strat.get("warnings", [])[:5]
    reasons    = strat.get("reasons",  [])[:3]

    def opt_data(opt_type, strike):
        if not chain or strike is None: return None
        pool = chain.get("calls" if opt_type == "call" else "puts", [])
        return next((o for o in pool if o.get("strike") == strike), None)

    sp_strike = strat.get("short_put")
    lp_strike = strat.get("long_put")
    sc_strike = strat.get("short_call")
    lc_strike = strat.get("long_call")
    sp_d = opt_data("put",  sp_strike)
    lp_d = opt_data("put",  lp_strike)
    sc_d = opt_data("call", sc_strike)
    lc_d = opt_data("call", lc_strike)

    def leg_line(label, d, strike):
        if not d and not strike: return f"  {label}: N/A"
        s   = (d or {}).get("symbol", fv(strike, 0))
        bid = fv((d or {}).get("bid"))
        ask = fv((d or {}).get("ask"))
        mid = fv((d or {}).get("mid"))
        dlt = fv((d or {}).get("delta"), 3)
        return f"  {label} {fv(strike,0)}\n    B={bid} A={ask} M={mid} δ={dlt}"

    # ── تجميع الرسالة ────────────────────────────────────────────────────────
    lines = [
        f"🔬 <b>1.5σ Debug — {sym}</b>",
        "",
        f"💰 <b>Price:</b> {fv(price, 2)}",
        f"📅 Expiry: {expiry}  ({fv(hours,1)}h)",
        "",
        "📊 <b>Expected Move</b>",
        f"  ATM Strike: {fv(atm_strike, 0)}",
        f"  Call:  B={c_bid}  A={c_ask}  M={c_mid}",
        f"  Put:   B={p_bid}  A={p_ask}  M={p_mid}",
    ]
    if straddle is not None:
        lines.append(f"  Straddle mid = {fv(straddle, 2)}")
        lines.append(f"  EM = {fv(straddle,2)} × 0.90 = {fv(em, 2)}")
    else:
        lines.append(f"  EM = {fv(em, 2)}  (src: {em_src})")
    lines += [
        "",
        "📐 <b>1.5σ Calculation</b>",
        f"  EM × 1.5 = {fv(em,2)} × 1.5 = <b>{fv(dist,1)}</b>",
        f"  Bull Put  target = {fv(price,2)} − {fv(dist,1)} = <b>{fv(bp_tgt,1)}</b>",
        f"  Bear Call target = {fv(price,2)} + {fv(dist,1)} = <b>{fv(bc_tgt,1)}</b>",
    ]

    # العقود
    has_bp = sp_strike or sc_strike
    if has_bp:
        lines += ["", f"📋 <b>Contracts ({strat_name})</b>"]
        if sp_strike:
            lines.append(leg_line("Short Put ", sp_d, sp_strike))
            lines.append(leg_line("Long  Put ", lp_d, lp_strike))
            wing_p = abs((sp_strike or 0) - (lp_strike or 0)) or 1
            cwr_p  = round((credit or 0) / wing_p * 100, 1) if credit else None
            lines.append(f"  Credit = {fv(credit)}  |  C/W = {fv(cwr_p,1)}%")
        if sc_strike:
            lines.append(leg_line("Short Call", sc_d, sc_strike))
            lines.append(leg_line("Long  Call", lc_d, lc_strike))
            wing_c = abs((lc_strike or 0) - (sc_strike or 0)) or 1
            cwr_c  = round((credit or 0) / wing_c * 100, 1) if credit else None
            lines.append(f"  Credit = {fv(credit)}  |  C/W = {fv(cwr_c,1)}%")

    # فحوصات
    lines += ["", "✅ <b>Filters</b>"]

    sp_dlt_abs = abs(float((sp_d or {}).get("delta") or 0))
    if sp_d and (sp_d or {}).get("delta") is not None:
        sp_ok = "✅" if 0.05 <= sp_dlt_abs <= 0.20 else "❌"
        lines.append(f"  δ Put  = {fv(sp_dlt_abs,3)}  {sp_ok} (0.05–0.20)")
    sc_dlt_abs = abs(float((sc_d or {}).get("delta") or 0))
    if sc_d and (sc_d or {}).get("delta") is not None:
        sc_ok = "✅" if 0.05 <= sc_dlt_abs <= 0.20 else "❌"
        lines.append(f"  δ Call = {fv(sc_dlt_abs,3)}  {sc_ok} (0.05–0.20)")

    cr_ok = "✅" if credit and credit > 0 else "❌"
    lines.append(f"  Credit {cr_ok} = {fv(credit)}")

    liq_fail = any(kw in w for w in warnings for kw in ["سيولة", "bid/ask", "spread"])
    lines.append(f"  Liquidity {'✅' if not liq_fail else '❌'}")

    lines += [
        "",
        f"🏁 <b>Score:</b> {score}/100",
        f"{'🚫 NO TRADE' if no_trade else '✅ TRADE'}  {decision}",
    ]

    if warnings:
        lines += ["", "⚠️ <b>Warnings:</b>"]
        for w in warnings:
            lines.append(f"  • {w}")

    if reasons and not no_trade:
        lines += ["", "📌 <b>Reasons:</b>"]
        for r in reasons[:3]:
            lines.append(f"  • {r}")

    return "\n".join(lines)


def send_sigma_debug(analysis: dict):
    """يُرسل رسالة Telegram تفصيلية بحساب 1.5σ — تُستدعى بعد send_analysis."""
    msg = format_sigma_debug_telegram(analysis)
    if not msg:
        return False, "لا توجد بيانات 1.5σ لإرسالها"
    return send_message(msg)


def send_analysis(analysis):
    """Format and send analysis to Telegram"""
    msg = format_analysis_message(analysis)
    return send_message(msg)


def send_trade_notification(trade):
    """Send trade result to Telegram"""
    result_icon = "✅" if trade["result"] == "ربح" else "❌"
    r_sign = "+" if trade["r_value"] >= 0 else ""
    
    msg = (
        f"📊 <b>صفقة منفذة — {trade['strategy']}</b>\n"
        f"📅 {trade['date']} {trade['time']}\n"
        f"\n"
        f"{result_icon} النتيجة: <b>{trade['result']}</b>\n"
        f"📈 قيمة R: <b>{r_sign}{trade['r_value']}R</b>\n"
        f"💵 المبلغ: <b>${trade['amount']:,.2f}</b>\n"
    )
    if trade.get("notes"):
        msg += f"📝 ملاحظات: {trade['notes']}\n"
    
    return send_message(msg)


def send_paper_close_notification(trade: dict):
    """v3.27: رسالة Telegram عند إغلاق Paper Trade تلقائياً."""
    try:
        result = trade.get("result") or ""
        icon = "✅" if result == "WIN" else ("❌" if result == "LOSS" else "⚪")
        pnl = float(trade.get("pnl_dollar") or trade.get("current_pnl_dollar") or 0)
        pct = float(trade.get("profit_pct") if trade.get("profit_pct") is not None else (trade.get("current_pnl_pct") or 0))
        entry = float(trade.get("credit_debit") or 0)
        exit_v = float(trade.get("exit_price") or trade.get("current_value") or 0)
        msg = (
            f"{icon} <b>Paper Trade Closed</b>\n"
            f"ID: <b>{trade.get('id','—')}</b>\n"
            f"Symbol: <b>{trade.get('symbol','—')}</b>\n"
            f"Strategy: <b>{trade.get('strategy','—')}</b>\n"
            f"Mode: <b>{trade.get('selected_mode','—')}</b>\n"
            f"Expiry/DTE: <b>{trade.get('expiry_date','—')} / {trade.get('dte_at_entry','—')}</b>\n"
            f"\nEntry: <b>{entry:.2f}</b>\n"
            f"Exit: <b>{exit_v:.2f}</b>\n"
            f"P&L: <b>${pnl:+.2f}</b>  (<b>{pct:+.1f}%</b>)\n"
            f"Result: <b>{result or '—'}</b>\n"
            f"Reason: <b>{trade.get('close_reason','—')}</b>"
        )
        return send_message(msg)
    except Exception as exc:
        return False, f"paper close telegram format error: {exc}"


def test_connection(token=None, chat_id=None):
    """Test Telegram connection"""
    ok, msg = send_message(
        "🤖 <b>اختبار الاتصال</b>\n✅ البوت يعمل بشكل صحيح!",
        token=token,
        chat_id=chat_id
    )
    return ok, msg
