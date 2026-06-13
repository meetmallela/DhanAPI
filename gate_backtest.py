"""
gate_backtest.py
----------------
Replay the three new entry gates against all historical orders and show
what PnL would look like if the gates had been active on every trading day.

Gates simulated:
  1. DEDUP   — same strategy + same tradingsymbol already entered today
  2. MOMENTUM — underlying's last two 1-min candles must confirm trade direction
  3. BURST   — more than MAX_BURST algo orders in the past 5 minutes → skip

Run: python gate_backtest.py [YYYY-MM-DD]   (default: all days)
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")

import sqlite3, pytz
import pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict
from master_resource import MasterResource

IST      = pytz.timezone("Asia/Kolkata")
DB       = MasterResource.get_trading_db_path()
KITE_DB  = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"

# Config
MAX_BURST = 4          # orders per 5-min window
filter_date = sys.argv[1] if len(sys.argv) > 1 else None   # None = all days

# ── Load orders ───────────────────────────────────────────────────────────────
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

if filter_date:
    orders = con.execute("""
        SELECT strategy_name, tradingsymbol, symbol, action,
               actual_entry_price, entry_price, pnl, created_at
        FROM orders
        WHERE date(created_at) = ? AND strategy_name != 'TG:SIGNAL'
        ORDER BY created_at
    """, (filter_date,)).fetchall()
else:
    orders = con.execute("""
        SELECT strategy_name, tradingsymbol, symbol, action,
               actual_entry_price, entry_price, pnl, created_at
        FROM orders
        WHERE strategy_name != 'TG:SIGNAL'
        ORDER BY created_at
    """).fetchall()

con.close()
orders = [dict(r) for r in orders]

# ── Load underlying 1-min candles (all days) ──────────────────────────────────
kcon = sqlite3.connect(KITE_DB, timeout=10)
krows = kcon.execute(
    "SELECT tradingsymbol, dt, open, high, low, close FROM candles_1min "
    "WHERE tradingsymbol IN ('NIFTY','BANKNIFTY','FINNIFTY','MIDCPNIFTY','SENSEX','BANKEX')"
).fetchall()
kcon.close()

# Build per-symbol DataFrame keyed by datetime-aware timestamp
candles_by_sym = defaultdict(list)
for sym, dt, o, h, l, c in krows:
    candles_by_sym[sym].append((dt, o, h, l, c))

dfs = {}
for sym, rows in candles_by_sym.items():
    df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close"])
    ts = pd.to_datetime(df["timestamp"], format="mixed", utc=True)
    df["timestamp"] = ts.dt.tz_convert(IST)
    dfs[sym] = df.sort_values("timestamp").reset_index(drop=True)

def get_last_two_candles(sym: str, at: datetime):
    """Return (prev_close, last_close) of the two most recent 1-min candles up to 'at'."""
    df = dfs.get(sym)
    if df is None:
        return None, None
    past = df[df["timestamp"] <= at]
    if len(past) < 2:
        return None, None
    return float(past.iloc[-2]["close"]), float(past.iloc[-1]["close"])

# ── Simulate gates per-order ──────────────────────────────────────────────────
print(f"\n{'='*100}")
print(f"  GATE BACKTEST — {len(orders)} algo orders  ({filter_date or 'all days'})")
print(f"{'='*100}")

results = []
# State that resets each day
seen_today_key = set()     # (date, strategy, tradingsymbol) for DEDUP
today_date     = None

for r in orders:
    try:
        et_raw = datetime.fromisoformat(r["created_at"])
        et     = et_raw.astimezone(IST) if et_raw.tzinfo else pytz.utc.localize(et_raw).astimezone(IST)
    except Exception:
        et = None

    pnl  = r["pnl"] or 0
    sym  = (r["symbol"] or "").split("-")[0].strip()   # BANKNIFTY, NIFTY, ...
    tsym = r["tradingsymbol"] or ""
    strat = r["strategy_name"]
    option_type = tsym.split("-")[-1] if "-" in tsym else ("CE" if "CE" in tsym else "PE")

    # Reset day-specific state
    if et:
        day = et.date()
        if day != today_date:
            seen_today_key = set()
            burst_window   = []          # list of datetimes of recent orders
            today_date     = day
    else:
        day = None

    gates_hit = []

    # --- Gate 1: DEDUP (same strategy + same tradingsymbol today) ---
    dedup_key = (str(day), strat, tsym)
    if dedup_key in seen_today_key:
        gates_hit.append("DEDUP")
    else:
        seen_today_key.add(dedup_key)

    # --- Gate 2: MOMENTUM (underlying must confirm direction) ---
    if et:
        prev_c, last_c = get_last_two_candles(sym, et)
        if prev_c is not None and last_c is not None:
            rising  = last_c > prev_c
            falling = last_c < prev_c
            if option_type == "CE" and not rising:
                gates_hit.append(f"MOMENTUM({sym} {prev_c:.0f}->{last_c:.0f} flat/fall for CE)")
            elif option_type == "PE" and not falling:
                gates_hit.append(f"MOMENTUM({sym} {prev_c:.0f}->{last_c:.0f} flat/rise for PE)")
        else:
            # No candle data — don't block, just note
            pass

    # --- Gate 3: BURST (>= MAX_BURST orders in last 5 min) ---
    if et:
        cutoff = et - timedelta(minutes=5)
        burst_window = [t for t in burst_window if t > cutoff]   # noqa: F821
        if len(burst_window) >= MAX_BURST:
            gates_hit.append(f"BURST({len(burst_window)} in 5min)")
        burst_window.append(et)

    results.append({
        "date":    str(day),
        "time":    str(et)[11:16] if et else "?",
        "strategy": strat,
        "symbol":   sym,
        "tradingsymbol": tsym,
        "option_type": option_type,
        "pnl":     pnl,
        "gates":   gates_hit,
        "blocked": len(gates_hit) > 0,
    })

# ── Summary per day ───────────────────────────────────────────────────────────
print(f"\n{'DATE':<12} {'ORDERS':>6} {'PASSED':>6} {'BLOCKED':>7}  "
      f"{'ACTUAL PnL':>12}  {'GATE-ADJ PnL':>13}  {'DELTA':>10}")
print("-"*80)

by_date = defaultdict(list)
for r in results:
    by_date[r["date"]].append(r)

grand_actual = grand_adj = 0
for d in sorted(by_date):
    rows = by_date[d]
    actual_pnl  = sum(r["pnl"] for r in rows)
    blocked_pnl = sum(r["pnl"] for r in rows if r["blocked"])
    adj_pnl     = actual_pnl - blocked_pnl
    passed      = sum(1 for r in rows if not r["blocked"])
    blocked_cnt = sum(1 for r in rows if r["blocked"])
    delta       = adj_pnl - actual_pnl
    grand_actual += actual_pnl
    grand_adj    += adj_pnl
    print(f"  {d:<10} {len(rows):>6} {passed:>6} {blocked_cnt:>7}  "
          f"{actual_pnl:>+12,.0f}  {adj_pnl:>+13,.0f}  {delta:>+10,.0f}")

print("-"*80)
delta_total = grand_adj - grand_actual
print(f"  {'TOTAL':<10} {len(results):>6} "
      f"{sum(1 for r in results if not r['blocked']):>6} "
      f"{sum(1 for r in results if r['blocked']):>7}  "
      f"{grand_actual:>+12,.0f}  {grand_adj:>+13,.0f}  {delta_total:>+10,.0f}")

# ── Which gate fires most? ────────────────────────────────────────────────────
print(f"\n--- Gate firing breakdown ({sum(1 for r in results if r['blocked'])} blocked orders) ---")
gate_stats = {"DEDUP": {"cnt":0,"pnl_saved":0}, "MOMENTUM": {"cnt":0,"pnl_saved":0}, "BURST": {"cnt":0,"pnl_saved":0}}
for r in results:
    if r["blocked"]:
        for g in r["gates"]:
            key = g.split("(")[0]
            if key in gate_stats:
                gate_stats[key]["cnt"]       += 1
                gate_stats[key]["pnl_saved"] += r["pnl"]   # negative pnl = loss saved

for g, s in gate_stats.items():
    print(f"  {g:<10}  {s['cnt']:3d} blocks  PnL of blocked orders: {s['pnl_saved']:+,.0f}  "
          f"(positive = losses saved, negative = gains missed)")

# ── Per-strategy impact ───────────────────────────────────────────────────────
print(f"\n--- Per-strategy gate impact ---")
print(f"{'Strategy':<28} {'Total':>5} {'Blocked':>7} {'Actual PnL':>12}  {'Adj PnL':>12}  {'Delta':>10}")
print("-"*82)
by_strat = defaultdict(list)
for r in results:
    by_strat[r["strategy"]].append(r)

for strat, rows in sorted(by_strat.items(), key=lambda x: sum(r["pnl"] for r in x[1])):
    actual   = sum(r["pnl"] for r in rows)
    blocked  = sum(r["pnl"] for r in rows if r["blocked"])
    adj      = actual - blocked
    bcnt     = sum(1 for r in rows if r["blocked"])
    print(f"  {strat:<26}  {len(rows):>5}  {bcnt:>7}  {actual:>+12,.0f}  {adj:>+12,.0f}  {adj-actual:>+10,.0f}")

# ── Blocked order detail (recent days only to keep output readable) ───────────
show_days = sorted(by_date.keys())[-3:]
print(f"\n--- Blocked order detail (last 3 days: {', '.join(show_days)}) ---")
print(f"  {'Date':<10} {'Time':>5}  {'Strategy':<24} {'Symbol':<25} {'PnL':>8}  Gate")
print(f"  {'-'*10} {'-'*5}  {'-'*24} {'-'*25} {'-'*8}  {'-'*40}")
for d in show_days:
    for r in by_date[d]:
        if r["blocked"]:
            print(f"  {r['date']:<10} {r['time']:>5}  {r['strategy']:<24} "
                  f"{r['tradingsymbol']:<25} {r['pnl']:>+8,.0f}  {'; '.join(r['gates'])}")

print(f"\n{'='*100}")
print(f"  CONCLUSION: Gates would have adjusted PnL by {delta_total:+,.0f} across {len(results)} orders on {len(by_date)} days")
print(f"  (positive delta = losses avoided; negative delta = gains also filtered out)")
print(f"{'='*100}")
