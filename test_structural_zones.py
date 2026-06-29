"""RC15j Phase 2B.1 — Synthetic candle test for structural zone detection.

IMPORTANT: _struct_swing_highs/lows uses n=2 neighbors on EACH side.
So a swing high at index i needs i >= 2 AND i <= len-3.
Each swing point requires 2 candles on each side that are clearly below/above it.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from core.demand_supply import (
    _struct_swing_highs,
    _struct_swing_lows,
    _detect_structural_demand,
    _detect_structural_supply,
    detect_structural_zones_shadow,
)

PASS = "PASS"
FAIL = "FAIL"
results = []


def C(o, h, l, c, t=0):
    return {"open": o, "high": h, "low": l, "close": c, "time": t * 3600000}


# ── T0: Verify swing detector works on simple sequence ───────────────────────
# Swing high at idx=5 (h=20), surrounded by lowers on both sides (n=2)
simple = [
    C(10, 12, 9,  11, 0),  # 0
    C(11, 13, 10, 12, 1),  # 1
    C(12, 14, 11, 13, 2),  # 2
    C(13, 15, 12, 14, 3),  # 3
    C(14, 16, 13, 15, 4),  # 4
    C(15, 20, 14, 19, 5),  # 5 SWING HIGH h=20
    C(19, 16, 15, 16, 6),  # 6
    C(16, 15, 14, 15, 7),  # 7
    C(15, 14, 13, 14, 8),  # 8
]
sh = _struct_swing_highs(simple, n=2)
ok0 = (5 in sh)
results.append(("T0 Swing high detector (idx=5)", ok0, {"sh": sh}))

# ── T1: Valid Demand — proper layout with n=2 room ───────────────────────────
#
# Need before base_candle (idx=16):
#   2 swing highs: at idx 5 (h=20) and idx 11 (h=22)
#   2 swing lows:  at idx 2 (l=5)  and idx 8  (l=4)
# BOS at idx 17: close=23 > sh@idx5=20 and sh@idx11=22 (both < base_idx=16)
#
t1 = [
    #       o    h    l    c    t
    C(10,  12,  7,  11,  0),   # 0
    C(11,  10,  6,   7,  1),   # 1
    C( 7,   9,  5,   8,  2),   # 2  swing LOW  l=5  (idx0 l=7>5, idx1 l=6>5, idx3 l=6>5, idx4 l=7>5)
    C( 8,  11,  6,  10,  3),   # 3
    C(10,  13,  7,  12,  4),   # 4
    C(12,  20, 11,  19,  5),   # 5  swing HIGH h=20 (idx3 h=11<20,idx4 h=13<20,idx6 h=14<20,idx7 h=12<20)
    C(19,  14, 12,  13,  6),   # 6
    C(13,  12, 10,  11,  7),   # 7
    C(11,  10,  4,   5,  8),   # 8  swing LOW  l=4  (idx6 l=12>4,idx7 l=10>4,idx9 l=8>4,idx10 l=7>4)
    C( 5,  11,  8,  10,  9),   # 9
    C(10,  13,  7,  12, 10),   # 10
    C(12,  22, 11,  21, 11),   # 11 swing HIGH h=22 (idx9 h=11<22,idx10 h=13<22,idx12 h=13<22,idx13 h=11<22)
    C(21,  13, 11,  12, 12),   # 12
    C(12,  11,  9,  10, 13),   # 13
    C(10,  12,  8,  11, 14),   # 14
    C(11,  10,  8,   9, 15),   # 15
    C( 9,  11,  8,   8, 16),   # 16 BEARISH base (close=8 < open=9)
    C( 8,  25,  8,  23, 17),   # 17 BOS close=23 > sh@idx5=20 and sh@idx11=22
]

# Verify swing detection before testing demand
sh1 = _struct_swing_highs(t1, n=2)
sl1 = _struct_swing_lows(t1, n=2)
sh_before_base = [i for i in sh1 if i < 16]
sl_before_base = [i for i in sl1 if i < 16]

d1 = _detect_structural_demand(t1)
ok1 = (
    d1["found"] is True
    and d1["base_candle_idx"] == 16
    and d1["bos_candle_idx"]  == 17
    and d1["left_structure_high_count_before_base"] >= 2
    and d1["left_structure_low_count_before_base"]  >= 2
    and d1["bos_level_broken"] <= 22.0
)
results.append((
    "T1 Demand valid zone",
    ok1,
    {"found": d1["found"], "base": d1.get("base_candle_idx"), "bos": d1.get("bos_candle_idx"),
     "sh_before_base": sh_before_base, "sl_before_base": sl_before_base,
     "reason": d1.get("reason", ""), "bos_lvl": d1.get("bos_level_broken")},
))


# ── T2: Demand rejected — BOS close does NOT exceed any pre-base swing high ──
# Pre-base swing highs at h=30 (very high), BOS close=23 < 30 → reject
t2 = [
    C(10,  12,  7,  11,  0),
    C(11,  10,  6,   7,  1),
    C( 7,   9,  5,   8,  2),   # swing LOW
    C( 8,  11,  6,  10,  3),
    C(10,  13,  7,  12,  4),
    C(12,  30, 11,  29,  5),   # swing HIGH h=30 (very high)
    C(29,  14, 12,  13,  6),
    C(13,  12, 10,  11,  7),
    C(11,  10,  4,   5,  8),   # swing LOW
    C( 5,  11,  8,  10,  9),
    C(10,  13,  7,  12, 10),
    C(12,  30, 11,  29, 11),   # swing HIGH h=30
    C(29,  13, 11,  12, 12),
    C(12,  11,  9,  10, 13),
    C(10,  12,  8,  11, 14),
    C(11,  10,  8,   9, 15),
    C( 9,  11,  8,   8, 16),   # BEARISH base
    C( 8,  25,  8,  23, 17),   # BOS close=23 < 30 → can't break pre-base sh
]
d2 = _detect_structural_demand(t2)
ok2 = d2["found"] is False
results.append(("T2 Demand rejects (BOS < pre-base sh)", ok2, d2))


# ── T3: Valid Supply — proper swing highs/lows (n=2 both sides) ──────────────
# swing HIGH at idx 2 (h=28): neighbors idx0 h=22<28✓ idx1 h=24<28✓ idx3 h=24<28✓ idx4 h=22<28✓
# swing HIGH at idx 8 (h=30): neighbors idx6 h=14<30✓ idx7 h=16<30✓ idx9 h=17<30✓ idx10 h=15<30✓
# swing LOW  at idx 5 (l=5):  neighbors idx3 l=21>5✓ idx4 l=18>5✓ idx6 l=10>5✓ idx7 l=14>5✓
# swing LOW  at idx 11 (l=3): neighbors idx9 l=14>3✓ idx10 l=12>3✓ idx12 l=10>3✓ idx13 l=14>3✓
# base = idx 16 (bullish), BOS = idx 17 close=2 < sl@5=5 and sl@11=3
t3 = [
    C(20, 22, 18, 21,  0),   # 0 h=22
    C(21, 24, 20, 23,  1),   # 1 h=24
    C(23, 28, 22, 27,  2),   # 2  swing HIGH h=28
    C(27, 24, 21, 22,  3),   # 3 h=24
    C(22, 22, 18, 19,  4),   # 4 h=22
    C(19, 10,  5,  6,  5),   # 5  swing LOW  l=5
    C( 6, 14, 10, 13,  6),   # 6
    C(13, 16, 14, 15,  7),   # 7
    C(15, 30, 14, 25,  8),   # 8  swing HIGH h=30
    C(25, 17, 14, 15,  9),   # 9
    C(15, 15, 12, 13, 10),   # 10
    C(13, 10,  3,  4, 11),   # 11 swing LOW  l=3
    C( 4, 13, 10, 12, 12),   # 12
    C(12, 16, 14, 15, 13),   # 13
    C(15, 17, 15, 16, 14),   # 14
    C(16, 18, 16, 17, 15),   # 15
    C(17, 20, 17, 19, 16),   # 16 BULLISH base (close=19 > open=17)
    C(19, 15,  1,  2, 17),   # 17 BOS close=2 < sl@idx5=5 and sl@idx11=3
]
sh3 = _struct_swing_highs(t3, n=2)
sl3 = _struct_swing_lows(t3, n=2)
sh_before_base3 = [i for i in sh3 if i < 16]
sl_before_base3 = [i for i in sl3 if i < 16]

s3 = _detect_structural_supply(t3)
ok3 = (
    s3["found"] is True
    and s3["base_candle_idx"] == 16
    and s3["bos_candle_idx"]  == 17
    and s3["left_structure_low_count_before_base"]  >= 2
    and s3["left_structure_high_count_before_base"] >= 2
)
results.append((
    "T3 Supply valid zone",
    ok3,
    {"found": s3["found"], "base": s3.get("base_candle_idx"), "bos": s3.get("bos_candle_idx"),
     "sh_before_base": sh_before_base3, "sl_before_base": sl_before_base3,
     "reason": s3.get("reason", ""), "bos_lvl": s3.get("bos_level_broken")},
))


# ── T4: Supply rejected — BOS close does NOT go below any pre-base swing low ─
# Pre-base swing lows at l=1 (very low), BOS close=2 > 1 → reject
t4 = [
    C(25,  30, 22,  28,  0),
    C(28,  28, 24,  25,  1),
    C(25,  29, 24,  26,  2),   # swing HIGH
    C(26,  26, 23,  24,  3),
    C(24,  25, 22,  23,  4),
    C(23,  10,  1,   6,  5),   # swing LOW l=1 (very low)
    C( 6,  14, 10,  13,  6),
    C(13,  16, 14,  15,  7),
    C(15,  28, 14,  25,  8),   # swing HIGH
    C(25,  22, 17,  19,  9),
    C(19,  18, 15,  16, 10),
    C(16,  12,  1,   4, 11),   # swing LOW l=1 (very low)
    C( 4,  14, 10,  13, 12),
    C(13,  16, 14,  15, 13),
    C(15,  17, 15,  16, 14),
    C(16,  18, 16,  17, 15),
    C(17,  20, 17,  19, 16),   # BULLISH base
    C(19,  15,  2,   2, 17),   # BOS close=2 > pre-base sl=1 → cannot break → reject
]
s4 = _detect_structural_supply(t4)
ok4 = s4["found"] is False
results.append(("T4 Supply rejects (BOS > pre-base sl)", ok4, s4))


# ── T5: Ratio guards ─────────────────────────────────────────────────────────
r5a = detect_structural_zones_shadow("SPY", spx_price=None,  spy_price=None)
r5b = detect_structural_zones_shadow("SPY", spx_price=5500,  spy_price=550)   # ratio=10.0
r5c = detect_structural_zones_shadow("SPY", spx_price=5500,  spy_price=1)     # ratio=5500 bad
ok5a = r5a["struct_shadow_converted"] is False and "MISSING" in r5a["struct_shadow_reason"]
ok5b = r5b["struct_shadow_converted"] is True  and abs(r5b["struct_shadow_ratio"] - 10.0) < 0.01
ok5c = r5c["struct_shadow_converted"] is False and "INVALID" in r5c["struct_shadow_reason"]
results.append(("T5a ratio=None  => not converted", ok5a, {"reason": r5a["struct_shadow_reason"]}))
results.append(("T5b ratio=10.0  => converted",     ok5b, {"ratio": r5b["struct_shadow_ratio"]}))
results.append(("T5c ratio=5500  => rejected",       ok5c, {"reason": r5c["struct_shadow_reason"]}))

# ── T6: Invalid ratio → SPX zone fields must be None (not raw SPY) ───────────
r6 = detect_structural_zones_shadow("SPY", spx_price=5500, spy_price=1)  # ratio=5500 invalid
ok6 = (
    r6["struct_shadow_converted"] is False
    and r6.get("struct_1h_demand_low")  is None
    and r6.get("struct_1h_supply_low")  is None
    and r6.get("struct_15m_demand_low") is None
    and r6.get("struct_15m_supply_low") is None
)
results.append(("T6 Invalid ratio => SPX zone fields None", ok6,
                {"1h_demand_low": r6.get("struct_1h_demand_low"),
                 "converted": r6["struct_shadow_converted"]}))


# ── T7: Phase 2B.2 — BOS Locality Filter ─────────────────────────────────────
# T1 candles: sh@idx5 (h=20) age=16-5=11, sh@idx11 (h=22) age=16-11=5
# bos_level = min(20,22) = 20 → bos_level_idx=5 → age_bars=11

# T7a: Valid Demand — max_bos_lookback_bars=40, age=11 <= 40 → valid
d7a = _detect_structural_demand(t1, max_bos_lookback_bars=40)
ok7a = (
    d7a["found"] is True
    and d7a.get("bos_local_valid") is True
    and d7a.get("zone_status") == "valid"
    and d7a.get("bos_level_age_bars") == 11
    and d7a.get("bos_level_idx") == 5
)
results.append((
    "T7a Demand valid within lookback (age=11 <= 40)",
    ok7a,
    {"found": d7a["found"], "bos_local_valid": d7a.get("bos_local_valid"),
     "age": d7a.get("bos_level_age_bars"), "idx": d7a.get("bos_level_idx"),
     "zone_status": d7a.get("zone_status")},
))

# T7b: Demand rejected — max_bos_lookback_bars=3, age=11 > 3 → rejected
d7b = _detect_structural_demand(t1, max_bos_lookback_bars=3)
ok7b = (
    d7b["found"] is False
    and d7b.get("bos_local_valid") is False
    and d7b.get("zone_status") == "rejected"
    and d7b.get("zone_reject_reason") == "BOS_LEVEL_OUTSIDE_LOCAL_LOOKBACK"
)
results.append((
    "T7b Demand rejected — BOS level too old (age=11 > max=3)",
    ok7b,
    {"found": d7b["found"], "bos_local_valid": d7b.get("bos_local_valid"),
     "zone_status": d7b.get("zone_status"), "reason": d7b.get("zone_reject_reason")},
))

# T3 candles: sl@idx5 (l=5) age=16-5=11, sl@idx11 (l=3) age=16-11=5
# bos_level = max(5,3) = 5 → bos_level_idx=5 → age_bars=11

# T7c: Valid Supply — max_bos_lookback_bars=40, age=11 <= 40 → valid
s7c = _detect_structural_supply(t3, max_bos_lookback_bars=40)
ok7c = (
    s7c["found"] is True
    and s7c.get("bos_local_valid") is True
    and s7c.get("zone_status") == "valid"
    and s7c.get("bos_level_age_bars") == 11
    and s7c.get("bos_level_idx") == 5
)
results.append((
    "T7c Supply valid within lookback (age=11 <= 40)",
    ok7c,
    {"found": s7c["found"], "bos_local_valid": s7c.get("bos_local_valid"),
     "age": s7c.get("bos_level_age_bars"), "idx": s7c.get("bos_level_idx"),
     "zone_status": s7c.get("zone_status")},
))

# T7d: Supply rejected — max_bos_lookback_bars=3, age=11 > 3 → rejected
s7d = _detect_structural_supply(t3, max_bos_lookback_bars=3)
ok7d = (
    s7d["found"] is False
    and s7d.get("bos_local_valid") is False
    and s7d.get("zone_status") == "rejected"
    and s7d.get("zone_reject_reason") == "BOS_LEVEL_OUTSIDE_LOCAL_LOOKBACK"
)
results.append((
    "T7d Supply rejected — BOS level too old (age=11 > max=3)",
    ok7d,
    {"found": s7d["found"], "bos_local_valid": s7d.get("bos_local_valid"),
     "zone_status": s7d.get("zone_status"), "reason": s7d.get("zone_reject_reason")},
))


# ── Print ─────────────────────────────────────────────────────────────────────
print()
print("=" * 65)
print("  RC15j Phase 2B.1/2B.2 -- Structural Zone Detection Tests")
print("=" * 65)
for name, ok, diag in results:
    tag = PASS if ok else FAIL
    print(f"  [{tag}]  {name}")
    if not ok:
        print(f"           => {diag}")
print()
all_pass = all(r[1] for r in results)
print("ALL PASS" if all_pass else "SOME FAILED")
print()
