"""Show candle path around SL breach for the 3 '2-candle saves' cases."""
import io, sys, sqlite3
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
import pytz, pandas as pd
from datetime import date
from master_resource import MasterResource

IST      = pytz.timezone("Asia/Kolkata")
KITE_DB  = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"
run_date = date.today().isoformat()

cases = [
    ("SENSEX-May2026-77400-CE",    195, 145),
    ("FINNIFTY-May2026-25500-CE",  442, 407),
    ("BANKNIFTY-May2026-54200-CE", 899, 827),
]

for sym, ep, sl in cases:
    parts = sym.split("-")
    base, strike, opt = parts[0], parts[-2], parts[-1]
    patterns = [f"{base}-%-{strike}-{opt}", f"{base}_{strike}_{opt}_%"]
    kcon = sqlite3.connect(KITE_DB, timeout=5)
    rows = []
    for pat in patterns:
        rows = kcon.execute(
            "SELECT dt, low, high, close FROM candles_1min "
            "WHERE tradingsymbol LIKE ? AND date(dt)=? ORDER BY dt",
            (pat, run_date)
        ).fetchall()
        if rows:
            break
    kcon.close()
    if not rows:
        print(f"{sym}: no data")
        continue

    df = pd.DataFrame(rows, columns=["ts","low","high","close"])
    ts_col = pd.to_datetime(df["ts"])
    df["ts"] = ts_col.dt.tz_convert(IST) if ts_col.dt.tz is not None else ts_col.dt.tz_localize(IST)

    breach_idx = None
    for i, row in df.iterrows():
        if row["close"] <= sl:
            breach_idx = i
            break
    if breach_idx is None:
        print(f"{sym}: SL never breached on close")
        continue

    start = max(0, breach_idx - 3)
    end   = min(len(df), breach_idx + 12)
    day_min   = df["low"].min()
    day_close = df.iloc[-1]["close"]

    pnl_at_sl  = (sl - ep) * 1          # per unit, BUY
    pnl_at_eod = (day_close - ep) * 1

    print(f"\n{sym}")
    print(f"  entry={ep}  SL={sl}({(sl-ep)/ep*100:.1f}%)  day_min={day_min:.0f}({(day_min-ep)/ep*100:.1f}%)  EOD={day_close:.0f}")
    print(f"  Exit@SL={pnl_at_sl:+.0f}/unit  |  Exit@EOD={pnl_at_eod:+.0f}/unit")
    print(f"  {'Time':<6}  {'Close':>6}  {'Low':>6}  Note")
    for i in range(start, end):
        r = df.iloc[i]
        marker = "<-- SL BREACH" if i == breach_idx else ""
        print(f"  {str(r['ts'])[11:16]}   {r['close']:>6.0f}  {r['low']:>6.0f}  {marker}")
    print(f"  ...  EOD close = {day_close:.0f}")
    print(f"  Verdict: 2-candle confirm saves exit at {sl:.0f}, then price → {day_close:.0f}")
    worse = pnl_at_eod < pnl_at_sl
    print(f"  {'WORSE outcome (held to EOD)' if worse else 'BETTER outcome (held to EOD)'} : "
          f"delta = {pnl_at_eod - pnl_at_sl:+.0f}/unit")
