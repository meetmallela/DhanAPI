"""
sl_breach_analysis.py
---------------------
For each INITIAL_SL trade today, look at the 1-min candles around the SL hit:
  - How many consecutive candles were below SL before recovery?
  - What was the price N candles after the SL level was breached?
  - Would a 2-candle confirmation have saved the trade?

This answers: is a candle-confirmation SL the right fix?
"""
import io, sys, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")

import pytz, pandas as pd
from datetime import date, datetime, timedelta
from master_resource import MasterResource

IST      = pytz.timezone("Asia/Kolkata")
DB       = MasterResource.get_trading_db_path()
KITE_DB  = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"
run_date = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()

# ── Fetch today's INITIAL_SL trades ──────────────────────────────────────────
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
rows = con.execute("""
    SELECT tradingsymbol, action, actual_entry_price, entry_price,
           stop_loss, peak_price, pnl, quantity, updated_at, created_at
    FROM orders
    WHERE date(created_at) = ? AND sl_stage = 'INITIAL'
    ORDER BY created_at
""", (run_date,)).fetchall()
con.close()

def load_candles(ts_dhan: str, run_date: str):
    parts = ts_dhan.split("-")
    if len(parts) < 4:
        return None
    base, strike, opt = parts[0], parts[-2], parts[-1]
    patterns = [f"{base}-%-{strike}-{opt}", f"{base}_{strike}_{opt}_%"]
    try:
        kcon = sqlite3.connect(KITE_DB, timeout=5)
        for pat in patterns:
            r = kcon.execute(
                "SELECT dt, open, high, low, close FROM candles_1min "
                "WHERE tradingsymbol LIKE ? AND date(dt)=? ORDER BY dt",
                (pat, run_date)
            ).fetchall()
            if r:
                break
        kcon.close()
        if not r:
            return None
        df = pd.DataFrame(r, columns=["timestamp","open","high","low","close"])
        ts = pd.to_datetime(df["timestamp"])
        df["timestamp"] = ts.dt.tz_convert(IST) if ts.dt.tz is not None else ts.dt.tz_localize(IST)
        return df.dropna(subset=["timestamp"]).reset_index(drop=True)
    except Exception:
        return None

# ── Deduplicate: same symbol traded by multiple strategies → analyse once ────
seen = {}
for r in rows:
    key = r["tradingsymbol"]
    if key not in seen:
        seen[key] = r

print(f"\n{'='*90}")
print(f"  SL BREACH PATTERN ANALYSIS — {run_date}")
print(f"  {len(rows)} INITIAL_SL trades | {len(seen)} unique instruments")
print(f"{'='*90}\n")

# Counters for summary
saved_by_2candle  = 0
saved_by_3candle  = 0
saved_by_5min     = 0
saved_by_wider_sl = 0  # would survive if SL were 12% instead of 8%
genuine_loss      = 0
no_data           = 0

details = []

for sym, r in seen.items():
    ep    = r["actual_entry_price"] or r["entry_price"] or 0
    sl    = r["stop_loss"] or 0
    pnl   = r["pnl"] or 0
    qty   = r["quantity"] or 1

    candles = load_candles(sym, run_date)
    if candles is None:
        no_data += 1
        continue

    # Entry time
    try:
        entry_time = datetime.fromisoformat(r["created_at"]).astimezone(IST)
    except Exception:
        continue

    # Candles from entry onward
    df = candles[candles["timestamp"] >= entry_time].reset_index(drop=True)
    if df.empty:
        no_data += 1
        continue

    # ── Replay: find when SL was first breached ───────────────────────────────
    first_breach_idx = None
    consecutive = 0
    max_consecutive = 0
    confirm2_saved  = False
    confirm3_saved  = False

    for i, row in df.iterrows():
        is_long = r["action"] == "BUY"
        breached = (is_long and row["low"] <= sl) or (not is_long and row["high"] >= sl)
        close_breached = (is_long and row["close"] <= sl) or (not is_long and row["close"] >= sl)

        if close_breached:
            if first_breach_idx is None:
                first_breach_idx = i
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
        else:
            consecutive = 0

        # 2-candle confirm: if only 1 close below SL before recovery
        if first_breach_idx is not None and consecutive == 0:
            confirm2_saved = (max_consecutive < 2)
            confirm3_saved = (max_consecutive < 3)
            break

    if first_breach_idx is None:
        # SL was never breached on candle close — shouldn't happen for INITIAL_SL trades
        no_data += 1
        continue

    # ── Price 5 candles after first breach ───────────────────────────────────
    end_idx = min(first_breach_idx + 5, len(df) - 1)
    price_5min_after = df.iloc[end_idx]["close"]
    min_low_after5   = df.iloc[first_breach_idx:end_idx+1]["low"].min()

    # Would 5-min hold have saved it? (price above sl 5 candles after breach)
    saved_5min = price_5min_after > sl

    # Would wider SL (12%) have survived?
    wider_sl = ep * (1 - 0.12) if r["action"] == "BUY" else ep * (1 + 0.12)
    min_low_all = df["low"].min()
    survived_wider = (r["action"] == "BUY" and min_low_all > wider_sl) or \
                     (r["action"] != "BUY" and df["high"].max() < wider_sl)

    # EOD close
    eod_close = df.iloc[-1]["close"]
    eod_pnl_unit = (eod_close - ep) if r["action"] == "BUY" else (ep - eod_close)

    sl_pct = abs(ep - sl) / ep * 100 if ep else 0
    wider_sl_pct = 12.0

    tag_parts = []
    if confirm2_saved:
        tag_parts.append("2-CANDLE-SAVES")
        saved_by_2candle += 1
    if confirm3_saved and not confirm2_saved:
        tag_parts.append("3-CANDLE-SAVES")
        saved_by_3candle += 1
    if saved_5min:
        tag_parts.append("5MIN-SAVES")
        saved_by_5min += 1
    if survived_wider:
        tag_parts.append(f"12%SL-SAVES(min={min_low_all:.0f}>wider_sl={wider_sl:.0f})")
        saved_by_wider_sl += 1
    if not confirm2_saved and not survived_wider:
        genuine_loss += 1

    tag = " | ".join(tag_parts) if tag_parts else "GENUINE-LOSS"

    details.append({
        "sym": sym, "ep": ep, "sl": sl, "sl_pct": sl_pct,
        "wider_sl": wider_sl, "max_consec": max_consecutive,
        "confirm2_saved": confirm2_saved, "survived_wider": survived_wider,
        "eod_close": eod_close, "eod_pnl": eod_pnl_unit, "tag": tag,
        "pnl_actual": pnl, "qty": qty,
    })

    print(f"  {sym:<42} ep={ep:.0f}  SL={sl:.0f}({sl_pct:.1f}%)  12%SL={wider_sl:.0f}")
    print(f"    consec-closes-below-SL={max_consecutive}  eod-close={eod_close:.0f}  "
          f"eod-pnl-unit={eod_pnl_unit:+.0f}  [{tag}]")
    print()

# ── Summary ───────────────────────────────────────────────────────────────────
n = len(details)
if n == 0:
    print("No data to summarise.")
    sys.exit()

print(f"{'='*90}")
print(f"  SUMMARY  ({n} unique instruments with INITIAL_SL)")
print(f"{'='*90}")
print(f"  2-candle confirm would save   : {saved_by_2candle}/{n} = {saved_by_2candle/n*100:.0f}%")
print(f"  3-candle confirm would save   : {saved_by_2candle + saved_by_3candle}/{n} = {(saved_by_2candle+saved_by_3candle)/n*100:.0f}%")
print(f"  5-min hold would save         : {saved_by_5min}/{n} = {saved_by_5min/n*100:.0f}%")
print(f"  Wider 12% SL would save       : {saved_by_wider_sl}/{n} = {saved_by_wider_sl/n*100:.0f}%")
print(f"  Genuine losses (nothing saves): {genuine_loss}/{n} = {genuine_loss/n*100:.0f}%")
print()

# PnL impact of each fix (using actual trade lot counts from rows, not deduplicated)
pnl_saved_2candle = 0
pnl_saved_wider   = 0
pnl_total_initial = sum(r["pnl"] or 0 for r in rows)

for r in rows:
    d = next((x for x in details if x["sym"] == r["tradingsymbol"]), None)
    if not d:
        continue
    qty  = r["quantity"] or 1
    ep   = r["actual_entry_price"] or r["entry_price"] or 0
    sl   = r["stop_loss"] or 0
    if d["confirm2_saved"]:
        # If we hadn't exited, trade holds to EOD — use EOD pnl
        pnl_at_eod   = d["eod_pnl"] * qty
        pnl_at_sl    = r["pnl"] or 0
        pnl_saved_2candle += (pnl_at_eod - pnl_at_sl)
    if d["survived_wider"]:
        pnl_at_eod   = d["eod_pnl"] * qty
        pnl_at_sl    = r["pnl"] or 0
        pnl_saved_wider   += (pnl_at_eod - pnl_at_sl)

print(f"  INITIAL_SL total actual PnL   : {pnl_total_initial:+,.0f}")
print(f"  PnL rescued by 2-candle confirm: {pnl_saved_2candle:+,.0f}  (if trade held to EOD after no-confirm)")
print(f"  PnL rescued by 12% SL          : {pnl_saved_wider:+,.0f}  (if trade held to EOD if wider SL not hit)")
print()
print(f"  Note: EOD as exit is conservative — trail/breakeven would have locked in more.")
print(f"{'='*90}")
