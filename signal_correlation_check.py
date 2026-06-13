"""
signal_correlation_check.py
----------------------------
For a given date, analyse:
  1. Which strategies entered the same instrument (correlated positions)
  2. The TIME of each entry — were they independent signals or bunched together?
  3. What was the underlying's 1-min candle at the time of each entry
     (was the option already deep in a move, or did they enter fresh?)
  4. Are these strategies using genuinely different data/logic or is it the
     same trigger dressed up in different names?

Run: python signal_correlation_check.py [YYYY-MM-DD]
"""
import io, sys, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")

import pytz, pandas as pd
from datetime import date, datetime, timedelta
from collections import defaultdict
from master_resource import MasterResource

IST      = pytz.timezone("Asia/Kolkata")
DB       = MasterResource.get_trading_db_path()
KITE_DB  = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"
run_date = sys.argv[1] if len(sys.argv) > 1 else "2026-05-15"

# ── Fetch all orders for the day ──────────────────────────────────────────────
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
orders = con.execute("""
    SELECT strategy_name, tradingsymbol, action,
           actual_entry_price, entry_price, quantity, pnl, sl_stage,
           created_at, updated_at
    FROM orders
    WHERE date(created_at) = ?
    ORDER BY tradingsymbol, created_at
""", (run_date,)).fetchall()

# Also fetch raw signals to see channel/parsed_data
signals = con.execute("""
    SELECT id, channel_name, parsed_data, timestamp, order_status
    FROM signals
    WHERE date(timestamp) = ?
    ORDER BY timestamp
""", (run_date,)).fetchall()
con.close()

# ── Group orders by tradingsymbol ─────────────────────────────────────────────
by_sym = defaultdict(list)
for r in orders:
    by_sym[r["tradingsymbol"]].append(dict(r))

print(f"\n{'='*90}")
print(f"  SIGNAL CORRELATION ANALYSIS — {run_date}")
print(f"  {len(orders)} orders  |  {len(by_sym)} unique instruments")
print(f"{'='*90}\n")

# Find instruments with multiple strategies
contested = {sym: rows for sym, rows in by_sym.items() if len(rows) > 1}
print(f"  Instruments with >1 strategy entering: {len(contested)}\n")

def load_underlying_candles(index_name: str, run_date: str) -> pd.DataFrame | None:
    """Load 1-min candles for the underlying index (not the option)."""
    # Common Kite index symbols
    kite_names = {
        "NIFTY":      "NIFTY 50",
        "BANKNIFTY":  "NIFTY BANK",
        "FINNIFTY":   "NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NIFTY MID SELECT",
        "SENSEX":     "SENSEX",
        "BANKEX":     "BSE BANKEX",
    }
    kite_sym = kite_names.get(index_name, index_name)
    try:
        kcon = sqlite3.connect(KITE_DB, timeout=5)
        rows = kcon.execute(
            "SELECT dt, open, high, low, close FROM candles_1min "
            "WHERE tradingsymbol = ? AND date(dt) = ? ORDER BY dt",
            (kite_sym, run_date)
        ).fetchall()
        kcon.close()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close"])
        ts = pd.to_datetime(df["timestamp"])
        df["timestamp"] = ts.dt.tz_convert(IST) if ts.dt.tz is not None else ts.dt.tz_localize(IST)
        return df.dropna(subset=["timestamp"]).reset_index(drop=True)
    except Exception as e:
        return None

def get_underlying_at_time(candles_df: pd.DataFrame, t: datetime) -> dict | None:
    if candles_df is None:
        return None
    row = candles_df[candles_df["timestamp"] <= t]
    if row.empty:
        return None
    r = row.iloc[-1]
    return {"close": r["close"], "open": r["open"], "high": r["high"], "low": r["low"], "ts": r["timestamp"]}

# ── Analyse each contested instrument ────────────────────────────────────────
for sym, rows in sorted(contested.items(), key=lambda x: -len(x[1])):
    base = sym.split("-")[0]
    underlying = load_underlying_candles(base, run_date)

    option_type = sym.split("-")[-1]   # CE or PE
    direction   = "BEARISH" if option_type == "PE" else "BULLISH"

    avg_pnl  = sum(r["pnl"] or 0 for r in rows) / len(rows)
    all_same_dir = len(set(r["action"] for r in rows)) == 1

    print(f"  {sym}  [{option_type} — {direction}]  {len(rows)} strategies")
    print(f"  {'Strategy':<24} {'Entry time':<10} {'EP':>7} {'PnL':>8}  {'Underlying at entry':>22}  {'Stage'}")
    print(f"  {'-'*24} {'-'*10} {'-'*7} {'-'*8}  {'-'*22}  {'-'*14}")

    entry_times = []
    for r in rows:
        try:
            et = datetime.fromisoformat(r["created_at"]).astimezone(IST)
        except Exception:
            et = None
        ep  = r["actual_entry_price"] or r["entry_price"] or 0
        pnl = r["pnl"] or 0
        entry_times.append(et)

        ul = get_underlying_at_time(underlying, et) if et else None
        ul_str = f"{ul['close']:.0f} ({str(ul['ts'])[11:16]})" if ul else "no data"

        print(f"  {r['strategy_name']:<24} {str(et)[11:16] if et else '?':<10} "
              f"{ep:>7.1f} {pnl:>+8.0f}  {ul_str:>22}  {r['sl_stage'] or '?'}")

    # Time spread: are signals bunched or spread?
    valid_times = [t for t in entry_times if t is not None]
    if len(valid_times) > 1:
        spread = (max(valid_times) - min(valid_times)).total_seconds() / 60
        first  = min(valid_times)
        ul_at_first = get_underlying_at_time(underlying, first)

        print(f"\n  Entry time spread : {spread:.1f} minutes  "
              f"({'BUNCHED — all triggered same candle' if spread < 2 else 'SPREAD — staggered entries'})")

        # Underlying move from first entry to 10 minutes later
        if underlying is not None and ul_at_first:
            ul_10min = get_underlying_at_time(underlying, first + timedelta(minutes=10))
            if ul_10min:
                move_10 = ul_10min["close"] - ul_at_first["close"]
                print(f"  Underlying at entry: {ul_at_first['close']:.0f}  "
                      f"→ 10 min later: {ul_10min['close']:.0f}  "
                      f"(move: {move_10:+.0f} pts {'AGAINST '+direction if (direction=='BEARISH' and move_10>0) or (direction=='BULLISH' and move_10<0) else 'WITH '+direction})")

    print()

# ── Timing heatmap: when did signals fire? ───────────────────────────────────
print(f"\n{'='*90}")
print(f"  ENTRY TIMING DISTRIBUTION (all {len(orders)} orders)")
print(f"{'='*90}")
time_buckets = defaultdict(list)
for r in orders:
    try:
        et = datetime.fromisoformat(r["created_at"]).astimezone(IST)
        bucket = f"{et.hour:02d}:{(et.minute // 15)*15:02d}"
        time_buckets[bucket].append(r["strategy_name"])
    except Exception:
        pass

for bucket in sorted(time_buckets):
    strats = time_buckets[bucket]
    print(f"  {bucket}  [{len(strats):2d} orders]  {', '.join(set(strats))[:80]}")

# ── Strategy overlap matrix ───────────────────────────────────────────────────
print(f"\n{'='*90}")
print(f"  SAME-INSTRUMENT CO-ENTRY MATRIX  (how often do pairs co-enter the same strike?)")
print(f"{'='*90}")
all_strats = sorted(set(r["strategy_name"] for r in orders))
pair_count = defaultdict(int)
for sym, rows in contested.items():
    strats_here = [r["strategy_name"] for r in rows]
    for i in range(len(strats_here)):
        for j in range(i+1, len(strats_here)):
            pair = tuple(sorted([strats_here[i], strats_here[j]]))
            pair_count[pair] += 1

if pair_count:
    for pair, count in sorted(pair_count.items(), key=lambda x: -x[1]):
        print(f"  {pair[0]:<28} × {pair[1]:<28}  {count} shared instrument(s)")
else:
    print("  No co-entries found.")

print(f"\n{'='*90}")
print(f"  KEY QUESTION: Are these strategies truly independent?")
print(f"  Check the 'Entry time spread' above — if < 2 min, they all fired on the")
print(f"  same underlying candle → NOT independent, they share the same root trigger.")
print(f"{'='*90}")
