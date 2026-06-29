"""
test_iv_raw.py — يطبع raw response من tastytrade market-metrics لـ SPY و QQQ.
تشغيل:
    python test_iv_raw.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

import json
import requests

API_BASE = "https://api.tastyworks.com"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json",
           "User-Agent": "AbuHassanBot/1.2"}

def _auth_headers(tok):
    return {**HEADERS, "Authorization": f"Bearer {tok}"}

def fetch_raw(tok, symbol):
    print(f"\n{'='*60}")
    print(f"  market-metrics: {symbol}")
    print(f"{'='*60}")
    resp = requests.get(
        f"{API_BASE}/market-metrics",
        params={"symbols[]": symbol},
        headers=_auth_headers(tok),
        timeout=15,
    )
    print(f"  Status: {resp.status_code}")
    if resp.status_code != 200:
        print(f"  Error: {resp.text[:500]}")
        return
    try:
        data = resp.json()
    except Exception as e:
        print(f"  JSON parse error: {e}")
        return

    items = data.get("data", {}).get("items", [])
    if not items:
        print("  items: EMPTY")
        print(f"  Full payload: {json.dumps(data, indent=2)[:3000]}")
        return

    item = items[0]
    print(f"\n  All keys ({len(item)}):")
    for k, v in sorted(item.items()):
        print(f"    {k!r:50s} = {v!r}")

    # التركيز على حقول IV
    iv_keys = [k for k in item if "volatil" in k.lower() or "iv" in k.lower()
               or "rank" in k.lower() or "percentile" in k.lower() or "implied" in k.lower()]
    if iv_keys:
        print(f"\n  IV-related keys:")
        for k in iv_keys:
            print(f"    {k!r:50s} = {item[k]!r}")
    else:
        print("\n  No IV-related keys found!")


if __name__ == "__main__":
    try:
        from core.analyzer import _get_access_token
        tok = _get_access_token()
        print(f"Token OK: {tok[:20]}...")
    except Exception as e:
        print(f"Auth error: {e}")
        sys.exit(1)

    for sym in ["SPY", "QQQ", "SPX", "$SPX.X"]:
        fetch_raw(tok, sym)

    print("\n\nDone.")
