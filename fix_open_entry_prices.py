"""
fix_open_entry_prices.py
------------------------
Backfills entry_price and stop_loss for open orders where entry_price=0.
Uses Kite 1-min last close as the LTP proxy.

Run once after restarting the engine when orders have entry_price=0.

Usage:
    C:\ProgramData\anaconda3\python.exe fix_open_entry_prices.py
"""
import sys
import sqlite3
from datetime import datetime, date

sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from kite_candle_store import resolve_option_token, get_candles, reset_kite, get_kite

DB_PATH   = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\trading.db"
SL_RATIO  = 0.95   # stop_loss = entry_price * SL_RATIO for CE; 1.05 for PE

BSE_SYMS = {"SENSEX", "BANKEX", "SENSEX50"}

# ── Expiry candidates for each symbol (nearest first) ─────────────────────────
# These cover the weekly/monthly expiries for May 2026.
EXPIRY_CANDIDATES = {
    # NSE moved NIFTY weekly to Tuesday; BANKNIFTY/MIDCPNIFTY monthly only in NFO
    "NIFTY":      ["2026-05-12", "2026-05-19", "2026-05-26"],
    "BANKNIFTY":  ["2026-05-26", "2026-05-28"],
    "FINNIFTY":   ["2026-05-26", "2026-05-19", "2026-05-12"],
    "MIDCPNIFTY": ["2026-05-26", "2026-05-28", "2026-05-30"],
    "SENSEX":     ["2026-05-28", "2026-05-07", "2026-05-09"],
    "BANKEX":     ["2026-05-28", "2026-05-08"],
}

def get_kite_ltp(sym: str, strike: float, opt_type: str, today: str) -> float | None:
    """Try each expiry candidate in order. Return first non-zero LTP found."""
    for expiry in EXPIRY_CANDIDATES.get(sym, []):
        token = resolve_option_token(sym, strike, opt_type, expiry)
        if token is None:
            continue
        exchange   = "BFO" if sym in BSE_SYMS else "NFO"
        tradingsym = f"{sym}_{int(strike)}_{opt_type}_{expiry}"
        try:
            df, source = get_candles(token, tradingsym, exchange, today, interval="minute")
            if df is not None and not df.empty:
                ltp = float(df["close"].iloc[-1])
                if ltp > 0:
                    print(f"  [{source}] {tradingsym} LTP={ltp:.2f}")
                    return ltp
        except Exception as e:
            print(f"  WARN: get_candles failed for {tradingsym}: {e}")
    return None


def main():
    today = date.today().isoformat()
    print(f"\nfix_open_entry_prices.py — {today}")
    print("=" * 60)

    # Validate Kite connection
    kite = get_kite()
    if kite is None:
        print("ERROR: Kite not available. Refresh token and retry.")
        sys.exit(1)
    print("Kite OK\n")

    con  = sqlite3.connect(DB_PATH)
    rows = con.execute(
        "SELECT id, tradingsymbol, action FROM orders "
        "WHERE status='OPEN' AND (entry_price=0 OR entry_price IS NULL)"
    ).fetchall()

    if not rows:
        print("No open orders with entry_price=0. Nothing to do.")
        con.close()
        return

    print(f"Found {len(rows)} open orders with entry_price=0:\n")

    updated = 0
    skipped = 0

    for row_id, tradingsym, action in rows:
        # Parse "SYMBOL-MonthYear-Strike-Type" e.g. NIFTY-May2026-24350-CE
        parts = tradingsym.split("-")
        if len(parts) < 4:
            print(f"  SKIP #{row_id}: unexpected format '{tradingsym}'")
            skipped += 1
            continue

        sym      = parts[0]
        opt_type = parts[-1]
        try:
            strike = float(parts[-2])
        except ValueError:
            print(f"  SKIP #{row_id}: cannot parse strike from '{tradingsym}'")
            skipped += 1
            continue

        print(f"#{row_id} {tradingsym} ({action})")

        ltp = get_kite_ltp(sym, strike, opt_type, today)
        if ltp is None or ltp <= 0:
            print(f"  SKIP: no LTP available\n")
            skipped += 1
            continue

        # Stop-loss: for CE/BUY → ltp*0.95; for PE/BUY → ltp*0.95 (absolute loss side same)
        sl = round(ltp * SL_RATIO, 2)
        ltp_r = round(ltp, 2)

        con.execute(
            "UPDATE orders SET entry_price=?, stop_loss=?, updated_at=? WHERE id=?",
            (ltp_r, sl, datetime.now().isoformat(), row_id)
        )
        print(f"  UPDATED: entry_price={ltp_r}  stop_loss={sl}\n")
        updated += 1

    con.commit()
    con.close()

    print("=" * 60)
    print(f"Done. Updated={updated}  Skipped={skipped}")


if __name__ == "__main__":
    main()
