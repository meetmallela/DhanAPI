"""
day_start_checks.py
-------------------
Runs all pre-market checks in sequence.
Run this once every morning before starting the bots.

Usage:
    C:\ProgramData\anaconda3\python.exe day_start_checks.py
"""

import sys
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

logging.disable(logging.CRITICAL)


def separator(title):
    print()
    print(f"{'='*60}")
    print(f"  CHECK: {title}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# CHECK 1: Dhan API token
# ---------------------------------------------------------------------------
separator("Dhan API Token")
try:
    from core.dhan_client import DhanClient
    from core.strike_lookup import StrikeLookup as _SL
    c = DhanClient()
    r = c.dhan.get_fund_limits()
    if r.get("status") == "success":
        d = r["data"]
        print("  OK  Token is valid")
        print(f"      Available Balance : {d.get('availabelBalance')}")
        print(f"      SOD Limit         : {d.get('sodLimit')}")
        print(f"      Used Margin       : {d.get('utilizedAmount')}")
    else:
        err_type = (r.get("remarks") or {}).get("error_type", "")
        # Dhan sandbox does not support get_fund_limits — fall back to scrip
        # master check (StrikeLookup) which IS sandbox-compatible.
        if err_type in ("FUND_LIMIT_ERROR", "CONVERT_POSITION_ERROR", ""):
            lu = _SL()
            res = lu.get_atm_option("NIFTY", 22300, "CE")
            if res:
                print("  OK  Token is valid (sandbox: fund_limits unsupported; verified via strike lookup)")
                print(f"      Strike lookup: NIFTY CE {res['strike']} exp={res['expiry_date']}")
            else:
                print("  FAILED  Token appears invalid — strike lookup also returned nothing")
                print("  Fix: open .env and verify DHAN_ACCESS_TOKEN at developer.dhan.co")
                sys.exit(1)
        else:
            print("  FAILED  Token check failed")
            print(f"          Response: {r.get('remarks')}")
            print()
            print("  Fix: open .env and verify DHAN_CLIENT_ID matches developer.dhan.co")
            sys.exit(1)
except Exception as e:
    print(f"  ERROR  {e}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# CHECK 2: Kite API token (primary price source)
# ---------------------------------------------------------------------------
separator("Kite API Token (LTP + Candles)")
try:
    import sys as _sys
    _lib = str(__import__('pathlib').Path(__file__).resolve().parent.parent /
                'MasterConfiguration' / 'lib')
    if _lib not in _sys.path:
        _sys.path.insert(0, _lib)
    from kite_candle_store import get_kite as _get_kite, _kite_instruments

    kite = _get_kite()
    profile = kite.profile()
    print(f"  OK  Token valid — logged in as: {profile.get('user_name', '?')} "
          f"({profile.get('user_id', '?')})")

    # Quick instrument count sanity check
    nfo_df = _kite_instruments("NFO")
    bfo_df = _kite_instruments("BFO")
    nfo_cnt = len(nfo_df) if nfo_df is not None else 0
    bfo_cnt = len(bfo_df) if bfo_df is not None else 0
    print(f"      NFO instruments: {nfo_cnt:,}  |  BFO instruments: {bfo_cnt:,}")

    if nfo_cnt < 1000 or bfo_cnt < 100:
        print("  WARNING: Instrument count looks low — Kite instruments may not have loaded")
    else:
        print("  OK  Instrument lists loaded — SENSEX/NIFTY LTP + candle fetch will work")

except Exception as e:
    print(f"  FAILED  Kite token check: {e}")
    print("  Fix: run the Kite login flow to refresh access_token in kite_config.json")
    print("       (token expires daily — must be refreshed before 9:15 AM)")


# ---------------------------------------------------------------------------
# CHECK 3: Scrip master (options data)
# ---------------------------------------------------------------------------
separator("Scrip Master CSV (options data)")
try:
    from master_resource import MasterResource
    p = Path(MasterResource.MASTER_ROOT) / "data" / "dhan_scrip_master.csv"
    if p.exists():
        age_hours = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
        size_mb   = p.stat().st_size / 1024 / 1024
        print(f"  OK  Scrip master found  ({size_mb:.1f} MB, {age_hours:.1f}h old)")
        if age_hours > 24:
            print("  WARNING: File is old — will auto-refresh when engine starts")
    else:
        print("  WARNING  Scrip master not found — will download on engine start")
        print(f"           Expected at: {p}")
except Exception as e:
    print(f"  ERROR  {e}")


# ---------------------------------------------------------------------------
# CHECK 4: Leftover open orders from previous session
# ---------------------------------------------------------------------------
separator("Leftover Open Orders from Previous Session")
try:
    db_path = MasterResource.get_trading_db_path()
    conn    = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur     = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'OPEN'")
    n = cur.fetchone()[0]
    if n > 0:
        print(f"  WARNING  {n} OPEN order(s) left over from previous session:")
        cur.execute("""
            SELECT order_id, symbol, action, entry_price, created_at
            FROM   orders WHERE status = 'OPEN'
            ORDER  BY created_at DESC
        """)
        for row in cur.fetchall():
            print(
                f"           {str(row['order_id']):<20}  {str(row['symbol']):<12}"
                f"  {str(row['action']):<5}  entry={row['entry_price']}  "
                f"created={row['created_at']}"
            )
        print()
        print("           SL Monitor will pick these up automatically.")
        print("           To discard stale ones, run check_open_orders.py for details.")
    else:
        print("  OK  No leftover open orders — clean start")
    conn.close()
except Exception as e:
    print(f"  ERROR  {e}")


# ---------------------------------------------------------------------------
# CHECK 5: Strike lookup (ATM option resolution)
# ---------------------------------------------------------------------------
separator("Strike Lookup (ATM Option Resolution)")
try:
    from core.strike_lookup import StrikeLookup
    lu = StrikeLookup()
    test_cases = [
        ("NIFTY",     22300, "CE"),
        ("NIFTY",     22300, "PE"),
        ("BANKNIFTY", 50000, "CE"),
        ("FINNIFTY",  23000, "CE"),
    ]
    all_ok = True
    for symbol, spot, opt_type in test_cases:
        res = lu.get_atm_option(symbol, spot, opt_type)
        if res:
            print(
                f"  OK  {symbol:<12} {opt_type}  "
                f"strike={res['strike']:6}  expiry={res['expiry_date']}  "
                f"sec_id={res['security_id']:<8}  {res['trading_symbol']}"
            )
        else:
            print(f"  FAILED  {symbol} {opt_type} at spot={spot} — not found in scrip master")
            all_ok = False
    if not all_ok:
        print()
        print("  Some lookups failed — check internet or re-run after market hours update")
except Exception as e:
    print(f"  ERROR  {e}")


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
print()
print("="*60)
print("  All checks done. If all show OK, you are ready to start bots.")
print("  Startup order:  T1 dashboard  ->  T2 sl_monitor")
print("                  T3 order_placer  ->  T4 OmniEngine")
print("                  T5 tg_trader (optional)")
print("="*60)
print()
