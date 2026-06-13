"""
fix_db_corrections.py
---------------------
DB correction script for paper trading system.
Problem 1: Fix PNL for 17 orders with correct entry/exit prices.
Problem 2: Fix entry_price for 39 May-7 orders (spot price was recorded, not option price).
"""

import sys
import sqlite3
import time
from datetime import datetime, date, timedelta

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# ─── Config ────────────────────────────────────────────────────────────────────
DB_PATH  = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\trading.db'
LIB_PATH = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib'
sys.path.insert(0, LIB_PATH)

import kite_candle_store as kcs

# ══════════════════════════════════════════════════════════════════════════════
# PROBLEM 1 — Fix PNL for 17 orders
# ══════════════════════════════════════════════════════════════════════════════
PROB1_IDS = [346,347,348,349,350,354,356,357,358,359,360,361,363,364,365,366,367]

print("=" * 70)
print("PROBLEM 1: Recalculate PNL for 17 orders")
print("=" * 70)

con = sqlite3.connect(DB_PATH, timeout=15)
con.row_factory = sqlite3.Row

# Fetch current data for these orders
placeholders = ",".join("?" for _ in PROB1_IDS)
rows = con.execute(
    f"SELECT id, tradingsymbol, entry_price, exit_price, quantity, pnl "
    f"FROM orders WHERE id IN ({placeholders})",
    PROB1_IDS,
).fetchall()

prob1_total_old_pnl = 0.0
prob1_total_new_pnl = 0.0
prob1_fixed = 0

for row in rows:
    oid        = row["id"]
    sym        = row["tradingsymbol"]
    entry      = float(row["entry_price"] or 0)
    exit_p     = float(row["exit_price"] or 0)
    qty        = int(row["quantity"] or 0)
    old_pnl    = float(row["pnl"] or 0)
    new_pnl    = round((exit_p - entry) * qty, 2)
    prob1_total_old_pnl += old_pnl
    prob1_total_new_pnl += new_pnl

    print(f"  ID={oid:<5} {sym:<35} entry={entry:>8.2f}  exit={exit_p:>8.2f}  "
          f"qty={qty:>3}  old_pnl={old_pnl:>9.2f}  new_pnl={new_pnl:>9.2f}")

    con.execute(
        "UPDATE orders SET pnl=?, updated_at=datetime('now','localtime') WHERE id=?",
        (new_pnl, oid),
    )
    prob1_fixed += 1

con.commit()
print(f"\n  >> Problem 1: {prob1_fixed} orders updated.")
print(f"     Old total PNL: {prob1_total_old_pnl:.2f}")
print(f"     New total PNL: {prob1_total_new_pnl:.2f}")
print(f"     PNL change:    {prob1_total_new_pnl - prob1_total_old_pnl:.2f}")


# ══════════════════════════════════════════════════════════════════════════════
# PROBLEM 2 — Fix 39 May-7 orders with wrong entry_price (spot used instead of option)
# ══════════════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print("PROBLEM 2: Fix entry_price for May-7 orders (spot→option price)")
print("=" * 70)

# Step 1: Query the 39 orders
may7_rows = con.execute(
    """
    SELECT id, tradingsymbol, created_at, entry_price, exit_price, quantity, pnl
    FROM orders
    WHERE DATE(created_at) = '2026-05-07'
      AND entry_price > 5000
      AND status = 'CLOSED'
    ORDER BY id
    """
).fetchall()

print(f"\nFound {len(may7_rows)} May-7 orders to fix.\n")
if not may7_rows:
    print("No orders found matching criteria. Exiting Problem 2.")
    con.close()
    sys.exit(0)

# Print the orders we'll fix
for row in may7_rows:
    print(f"  ID={row['id']:<5} {row['tradingsymbol']:<35} "
          f"created_at={row['created_at']}  "
          f"entry={row['entry_price']:>8.2f}  exit={row['exit_price']:>8.2f}  qty={row['quantity']}")

# Step 2: Get Kite instruments
print("\nConnecting to Kite and fetching NFO instruments...")
kite = kcs.get_kite()
if kite is None:
    print("ERROR: Could not connect to Kite. Aborting Problem 2.")
    con.close()
    sys.exit(1)

import pandas as pd
insts = pd.DataFrame(kite.instruments('NFO'))
insts["expiry"] = pd.to_datetime(insts["expiry"]).dt.date
print(f"  Loaded {len(insts):,} NFO instruments.")

# Check FINNIFTY name in instruments
finnifty_sample = insts[insts['tradingsymbol'].str.startswith('FINNIFTY')].head(3)
print(f"\n  FINNIFTY sample rows:")
print(finnifty_sample[['tradingsymbol','name','strike','instrument_type','expiry']].to_string(index=False))

midcp_sample = insts[insts['tradingsymbol'].str.startswith('MIDCP')].head(3)
print(f"\n  MIDCPNIFTY sample rows:")
print(midcp_sample[['tradingsymbol','name','strike','instrument_type','expiry']].to_string(index=False))


# Step 3: Parse tradingsymbol → (name, strike, option_type)
# Format: NIFTY-May2026-24300-CE  or  BANKNIFTY-May2026-54000-CE
import re

def parse_symbol(sym):
    """
    Parse tradingsymbol like 'NIFTY-May2026-24300-CE'
    Returns (kite_name, strike, option_type) or None.
    """
    m = re.match(r'^([\w]+)-(\w+\d+)-(\d+(?:\.\d+)?)-([CP]E)$', sym)
    if not m:
        return None
    raw_name   = m.group(1)   # e.g. NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY
    strike     = float(m.group(3))
    option_type = m.group(4)  # CE or PE

    # Map to Kite 'name' column — check from sample above
    # Default: same name; FINNIFTY might be 'NIFTY FIN SERVICE'
    # We'll resolve dynamically below
    return raw_name, strike, option_type


# Get unique symbols
unique_syms = list({row['tradingsymbol'] for row in may7_rows})
print(f"\n  Unique symbols ({len(unique_syms)}):")
for s in sorted(unique_syms):
    print(f"    {s}")


# Build token lookup: symbol → instrument_token
# For each symbol, find the May 2026 expiry closest after May 7
MAY7 = date(2026, 5, 7)

# First identify exact Kite name for each prefix
name_map = {}  # raw_name → kite_name

def find_kite_name(raw_name, insts_df):
    """Find the 'name' field in instruments that matches our raw index name."""
    # Direct match first
    hits = insts_df[insts_df['name'] == raw_name]
    if not hits.empty:
        return raw_name
    # Try partial match
    hits = insts_df[insts_df['name'].str.contains(raw_name, case=False, na=False)]
    if not hits.empty:
        return hits.iloc[0]['name']
    return None

token_map = {}  # tradingsymbol → instrument_token

for sym in sorted(unique_syms):
    parsed = parse_symbol(sym)
    if parsed is None:
        print(f"  WARN: Cannot parse symbol '{sym}'")
        continue
    raw_name, strike, option_type = parsed

    # Find kite name if not already resolved
    if raw_name not in name_map:
        kite_name = find_kite_name(raw_name, insts)
        if kite_name is None:
            print(f"  WARN: No Kite 'name' found for '{raw_name}'")
            name_map[raw_name] = None
        else:
            name_map[raw_name] = kite_name
            print(f"  Name map: {raw_name} → '{kite_name}'")

    kite_name = name_map.get(raw_name)
    if kite_name is None:
        continue

    # Filter instruments
    mask = (
        (insts['name'] == kite_name) &
        (insts['strike'] == strike) &
        (insts['instrument_type'] == option_type) &
        (insts['expiry'] > MAY7) &
        (insts['expiry'].apply(lambda d: d.month == 5 and d.year == 2026))
    )
    candidates = insts[mask].sort_values('expiry')

    if candidates.empty:
        print(f"  WARN: No token found for {sym} (name={kite_name}, strike={strike}, type={option_type}, after May 7)")
        continue

    # Pick the closest expiry after May 7
    chosen = candidates.iloc[0]
    token = int(chosen['instrument_token'])
    token_map[sym] = token
    print(f"  Token: {sym} → token={token}  expiry={chosen['expiry']}  tradingsymbol={chosen['tradingsymbol']}")


print(f"\n  Resolved {len(token_map)}/{len(unique_syms)} tokens.")


# Step 4: Fetch 1-min historical data for each unique token on 2026-05-07
import pytz
IST = pytz.timezone("Asia/Kolkata")

# unique tokens
unique_tokens = list({v: k for k, v in token_map.items()})  # deduplicated token→sym mapping
token_to_sym  = {}
for sym, tok in token_map.items():
    token_to_sym.setdefault(tok, sym)

print(f"\nFetching 1-min candles for {len(token_to_sym)} unique tokens on 2026-05-07...")
candle_lookup = {}   # token → {HH:MM → close_price}

for tok, sym in token_to_sym.items():
    try:
        data = kite.historical_data(tok, '2026-05-07', '2026-05-07', 'minute')
        if not data:
            print(f"  WARN: No candle data returned for token={tok} ({sym})")
            candle_lookup[tok] = {}
            continue
        df_c = pd.DataFrame(data)
        df_c['date'] = pd.to_datetime(df_c['date'])
        if df_c['date'].dt.tz is None:
            df_c['date'] = df_c['date'].dt.tz_localize(IST)
        else:
            df_c['date'] = df_c['date'].dt.tz_convert(IST)
        time_price = {}
        for _, r in df_c.iterrows():
            hhmm = r['date'].strftime('%H:%M')
            time_price[hhmm] = float(r['close'])
        candle_lookup[tok] = time_price
        print(f"  Fetched {len(time_price)} candles for {sym} (token={tok})")
        time.sleep(0.2)   # rate-limit
    except Exception as e:
        print(f"  ERROR fetching token={tok} ({sym}): {e}")
        candle_lookup[tok] = {}


def lookup_price(token, time_str, window_mins=2):
    """
    Look up close price at time_str (HH:MM).
    Falls back to nearest candle within ±window_mins if exact not found.
    """
    tbl = candle_lookup.get(token, {})
    if not tbl:
        return None
    if time_str in tbl:
        return tbl[time_str]
    # Try ±window_mins
    target_dt = datetime.strptime(time_str, '%H:%M')
    best_price = None
    best_diff  = float('inf')
    for t_str, price in tbl.items():
        try:
            t_dt = datetime.strptime(t_str, '%H:%M')
            diff = abs((t_dt - target_dt).total_seconds()) / 60
            if diff <= window_mins and diff < best_diff:
                best_diff  = diff
                best_price = price
        except Exception:
            pass
    return best_price


# Step 5: Update each of the 39 orders
print()
print("=" * 70)
print("Applying corrections to DB...")
print("=" * 70)

prob2_total_old_pnl = 0.0
prob2_total_new_pnl = 0.0
prob2_fixed   = 0
prob2_skipped = 0
EOD_TS = '2026-05-07T15:25:00'

rows_summary = []

for row in may7_rows:
    oid       = row['id']
    sym       = row['tradingsymbol']
    created   = row['created_at']    # e.g. '2026-05-07T10:56:23.123456'
    old_entry = float(row['entry_price'] or 0)
    exit_p    = float(row['exit_price']  or 0)
    qty       = int(row['quantity'] or 0)
    old_pnl   = float(row['pnl'] or 0)

    # Parse HH:MM from created_at
    try:
        entry_time = datetime.fromisoformat(created).strftime('%H:%M')
    except Exception:
        entry_time = created[11:16]   # fallback slice

    tok = token_map.get(sym)
    if tok is None:
        print(f"  SKIP  ID={oid:<5} {sym:<35} — no token resolved")
        prob2_skipped += 1
        rows_summary.append({
            'id': oid, 'sym': sym,
            'old_entry': old_entry, 'new_entry': None,
            'old_pnl': old_pnl, 'new_pnl': None,
            'status': 'SKIPPED_NO_TOKEN'
        })
        continue

    new_entry = lookup_price(tok, entry_time)
    if new_entry is None:
        print(f"  SKIP  ID={oid:<5} {sym:<35} — no candle at {entry_time} (token={tok})")
        prob2_skipped += 1
        rows_summary.append({
            'id': oid, 'sym': sym,
            'old_entry': old_entry, 'new_entry': None,
            'old_pnl': old_pnl, 'new_pnl': None,
            'status': 'SKIPPED_NO_CANDLE'
        })
        continue

    new_pnl = round((exit_p - new_entry) * qty, 2)
    prob2_total_old_pnl += old_pnl
    prob2_total_new_pnl += new_pnl

    print(f"  FIX   ID={oid:<5} {sym:<35} t={entry_time}  "
          f"old_entry={old_entry:>8.2f}  new_entry={new_entry:>8.2f}  "
          f"old_pnl={old_pnl:>9.2f}  new_pnl={new_pnl:>9.2f}")

    con.execute(
        "UPDATE orders SET entry_price=?, pnl=?, updated_at=? WHERE id=?",
        (new_entry, new_pnl, EOD_TS, oid),
    )
    prob2_fixed += 1
    rows_summary.append({
        'id': oid, 'sym': sym,
        'old_entry': old_entry, 'new_entry': new_entry,
        'old_pnl': old_pnl, 'new_pnl': new_pnl,
        'status': 'FIXED'
    })

con.commit()
con.close()

# ══════════════════════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════════════════════
print()
print("=" * 70)
print("FINAL SUMMARY")
print("=" * 70)
print(f"\nProblem 1 — PNL recalculation:")
print(f"  Orders fixed      : {prob1_fixed}")
print(f"  Old total PNL     : {prob1_total_old_pnl:>12.2f}")
print(f"  New total PNL     : {prob1_total_new_pnl:>12.2f}")
print(f"  PNL delta         : {prob1_total_new_pnl - prob1_total_old_pnl:>+12.2f}")

print(f"\nProblem 2 — Entry price correction (May 7 orders):")
print(f"  Orders found      : {len(may7_rows)}")
print(f"  Orders fixed      : {prob2_fixed}")
print(f"  Orders skipped    : {prob2_skipped}")
print(f"  Old total PNL     : {prob2_total_old_pnl:>12.2f}")
print(f"  New total PNL     : {prob2_total_new_pnl:>12.2f}")
print(f"  PNL delta         : {prob2_total_new_pnl - prob2_total_old_pnl:>+12.2f}")

grand_old = prob1_total_old_pnl + prob2_total_old_pnl
grand_new = prob1_total_new_pnl + prob2_total_new_pnl
print(f"\nCOMBINED:")
print(f"  Total orders fixed: {prob1_fixed + prob2_fixed}")
print(f"  Grand old PNL     : {grand_old:>12.2f}")
print(f"  Grand new PNL     : {grand_new:>12.2f}")
print(f"  Grand PNL delta   : {grand_new - grand_old:>+12.2f}")
print()
