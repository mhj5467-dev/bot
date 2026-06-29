"""Check Tastytrade shadow environment without placing orders.
Run from bot root:
    python tools/test_tasty_shadow_env.py
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

ROOT = Path(__file__).resolve().parents[1]
if load_dotenv:
    for candidate in (ROOT / ".env", ROOT / "config" / ".env"):
        if candidate.exists():
            load_dotenv(candidate)

print("=" * 60)
print("Tastytrade Shadow Provider Environment Check")
print("=" * 60)
for key in (
    "TASTY_SHADOW_ENABLED",
    "TASTYTRADE_USERNAME",
    "TASTYTRADE_PASSWORD",
    "TASTYTRADE_SANDBOX",
    "TASTY_SHADOW_SPX_EVENT_SYMBOL",
):
    val = os.getenv(key)
    if key == "TASTYTRADE_PASSWORD" and val:
        shown = "***SET***"
    elif key == "TASTYTRADE_USERNAME" and val:
        shown = val[:3] + "***" + val[-6:]
    else:
        shown = val
    print(f"{key}: {shown}")

print("\nImport check:")
try:
    import tastytrade  # type: ignore
    print("tastytrade package: OK")
except Exception as exc:
    print(f"tastytrade package: NOT AVAILABLE ({exc.__class__.__name__})")

print("\nNo connection attempt is made by this script.")
print("No orders can be placed by this script.")
