"""Quick diagnostic: show today's paper trades with SL stage, peak, and post-SL candle behaviour."""
import io, sys, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")

import pytz
import pandas as pd
from datetime import date, datetime
from pathlib import Path
from master_resource import MasterResource

IST      = pytz.timezone("Asia/Kolkata")
DB       = MasterResource.get_trading_db_path()
KITE_DB  = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"
run_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT order_id, strategy_name, tradingsymbol, action,
           entry_price, actual_entry_price, quantity, pnl,
           status, sl_stage, stop_loss, peak_price,
           created_at, updated_at
    FROM orders
    WHERE date(created_at) = ?
    ORDER BY created_at
""", (run_date,)).fetchall()
con.close()

print(f"\nDate: {run_date}  |  Total paper trades: {len(rows)}\n")

total_pnl = sum(r["pnl"] or 0 for r in rows)
initial_sl_trades = [r for r in rows if "INITIAL" in (r["sl_stage"] or "")]
other_trades      = [r for r in rows if "INITIAL" not in (r["sl_stage"] or "")]

print(f"{'Strategy':<22} {'Symbol':<30} {'EP':>7} {'Qty':>4} {'Peak':>8} {'SL':>8} {'PnL':>9}  {'Stage':<20} {'Status'}")
print(f"{'-'*22} {'-'*30} {'-'*7} {'-'*4} {'-'*8} {'-'*8} {'-'*9}  {'-'*20} {'-'*8}")

for r in rows:
    ep   = r["actual_entry_price"] or r["entry_price"] or 0
    pnl  = r["pnl"] or 0
    peak = r["peak_price"] or 0
    sl   = r["stop_loss"] or 0
    stage = r["sl_stage"] or r["status"] or "?"
    peak_gain_pct = ((peak - ep) / ep * 100) if ep else 0
    flag = " <<<" if "INITIAL" in stage else ""
    print(f"{r['strategy_name'] or '':<22} {r['tradingsymbol'] or '':<30} {ep:>7.1f} "
          f"{r['quantity'] or 0:>4} {peak:>8.1f} {sl:>8.1f} {pnl:>+9.0f}  "
          f"{stage:<20} {r['status'] or ''}{flag}")

print(f"\n  Total PnL           : {total_pnl:+,.0f}")
print(f"  INITIAL_SL exits    : {len(initial_sl_trades)} trades  "
      f"  PnL = {sum(r['pnl'] or 0 for r in initial_sl_trades):+,.0f}")
print(f"  Other exits         : {len(other_trades)} trades  "
      f"  PnL = {sum(r['pnl'] or 0 for r in other_trades):+,.0f}")

# ── For INITIAL_SL trades: check if price recovered after SL hit ──────────────
print(f"\n{'='*80}")
print(f"  POST-SL RECOVERY CHECK (did price go UP after we got stopped?)")
print(f"{'='*80}")

def load_kite_candles(tradingsymbol_dhan: str, run_date: str) -> pd.DataFrame | None:
    parts = tradingsymbol_dhan.split("-")
    if len(parts) < 4:
        return None
    base, strike, opt = parts[0], parts[-2], parts[-1]
    patterns = [
        f"{base}-%-{strike}-{opt}",
        f"{base}_{strike}_{opt}_%",
    ]
    try:
        kcon = sqlite3.connect(KITE_DB, timeout=5)
        rows_k = []
        for pat in patterns:
            rows_k = kcon.execute(
                "SELECT dt, open, high, low, close FROM candles_1min "
                "WHERE tradingsymbol LIKE ? AND date(dt) = ? ORDER BY dt",
                (pat, run_date)
            ).fetchall()
            if rows_k:
                break
        kcon.close()
        if not rows_k:
            return None
        df = pd.DataFrame(rows_k, columns=["timestamp","open","high","low","close"])
        ts = pd.to_datetime(df["timestamp"])
        df["timestamp"] = ts.dt.tz_convert(IST) if ts.dt.tz is not None else ts.dt.tz_localize(IST)
        return df.dropna(subset=["timestamp"]).reset_index(drop=True)
    except Exception:
        return None

no_data_count = 0
for r in initial_sl_trades:
    ep    = r["actual_entry_price"] or r["entry_price"] or 0
    sl    = r["stop_loss"] or 0
    pnl   = r["pnl"] or 0
    peak  = r["peak_price"] or ep
    ts_str = r["tradingsymbol"] or ""

    # Parse exit time from updated_at
    try:
        exit_time = datetime.fromisoformat(r["updated_at"]).astimezone(IST) if r["updated_at"] else None
    except Exception:
        exit_time = None

    candles = load_kite_candles(ts_str, run_date)
    if candles is None or candles.empty or exit_time is None:
        no_data_count += 1
        print(f"  {ts_str:<40}  ep={ep:.1f}  SL={sl:.1f}  PnL={pnl:+.0f}  [no candle data]")
        continue

    # Candles AFTER exit
    post = candles[candles["timestamp"] > exit_time]
    if post.empty:
        print(f"  {ts_str:<40}  ep={ep:.1f}  SL={sl:.1f}  PnL={pnl:+.0f}  [no post-exit candles]")
        continue

    post_high    = post["high"].max()
    post_close   = post.iloc[-1]["close"]
    recovery_pct = (post_high - sl) / sl * 100 if sl else 0
    would_have   = (post_high - ep) / ep * 100 if ep else 0

    # How far did it recover above SL?
    recovered_above_sl = post_high > sl
    verdict = f"RECOVERED +{recovery_pct:.1f}% above SL  (would have been {would_have:+.1f}% from entry)" \
              if recovered_above_sl else f"stayed below SL  (post-exit high={post_high:.1f})"

    peak_pct = (peak - ep) / ep * 100 if ep else 0
    print(f"  {ts_str:<40}  ep={ep:.0f}  peak={peak:.0f}(+{peak_pct:.1f}%)  "
          f"SL={sl:.0f}  PnL={pnl:+.0f}")
    print(f"    Post-SL: high={post_high:.0f}  close={post_close:.0f}  → {verdict}")
    print()

if no_data_count:
    print(f"  ({no_data_count} trades had no Kite candle data for post-SL analysis)")

# ── Stage distribution ────────────────────────────────────────────────────────
from collections import Counter
stage_counts = Counter(r["sl_stage"] or r["status"] or "?" for r in rows)
print(f"\n  SL stage distribution: {dict(stage_counts)}")
