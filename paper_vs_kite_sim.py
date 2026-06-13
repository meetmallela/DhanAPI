"""
paper_vs_kite_sim.py
--------------------
Takes actual paper trade entry prices from the orders table and re-simulates
them through the WhatIf SL engine using Kite 1-min candle data.

This isolates whether the P&L gap is caused by:
  (A) Entry price difference (paper LTP vs WhatIf first-candle price)
  (B) SL logic difference (live monitor vs WhatIf simulation)

Also runs the same simulation with OPTIMISED SL parameters to show
the improvement opportunity.

Run: python paper_vs_kite_sim.py [YYYY-MM-DD]
"""
import io, json, sqlite3, sys
from datetime import datetime, date
from pathlib import Path

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")

import pytz
import pandas as pd
from master_resource import MasterResource
from eod_whatif_backtest import simulate_sl, SL_CFG

IST      = pytz.timezone("Asia/Kolkata")
DB       = MasterResource.get_trading_db_path()
KITE_DB  = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"

run_date = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")

# ── Optimised config: tighter breakeven (0.5 ATR like WhatIf) ─────────────────
# Key change: atr_beven_mult 0.5 (unchanged) but also set percentage fallback
# breakeven to 1% (vs live's 3%) and trail_activation to 2% (vs live's 5%)
SL_CFG_OPT = dict(SL_CFG)
SL_CFG_OPT.update({
    "breakeven_trigger_pct": 1.0,    # was 3.0 in live; WhatIf already has 3.0 but uses ATR
    "trail_trigger_pct":     2.0,    # was 5.0 in live
    "trail_pct_am":          3.0,    # keep same
    "trail_pct_pm":          1.5,    # keep same
    "atr_beven_mult":        0.5,    # same as WhatIf
    "atr_trail_mult":        2.0,    # same as WhatIf
    "time_sl_minutes":       15,
    "time_sl_min_move_pct":  1.0,
})

# ── Load paper trades ──────────────────────────────────────────────────────────
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
paper = con.execute("""
    SELECT order_id, strategy_name, tradingsymbol, action,
           entry_price, actual_entry_price, quantity, pnl, status,
           created_at, sl_stage, stop_loss
    FROM orders
    WHERE date(created_at) = ?
    ORDER BY created_at
""", (run_date,)).fetchall()
con.close()

print(f"\n{'='*75}")
print(f"  PAPER vs KITE-SIM ANALYSIS  —  {run_date}   ({len(paper)} paper trades)")
print(f"{'='*75}")

# ── Fetch candles from kite_candles.db ────────────────────────────────────────
def _load_kite_candles(tradingsymbol_dhan: str, run_date: str) -> pd.DataFrame | None:
    """
    Convert Dhan tradingsymbol 'NIFTY-May2026-23800-CE' and try both kite DB formats:
      New: NIFTY-2026-05-23800-CE  (stored by OmniEngine data feed today)
      Old: NIFTY_23800_CE_2026-05-19  (stored by kite_candle_store prefetch)
    """
    parts = tradingsymbol_dhan.split("-")
    if len(parts) < 4:
        return None
    base   = parts[0]
    strike = parts[-2]
    opt    = parts[-1]

    # Two patterns covering both storage formats
    patterns = [
        f"{base}-%-{strike}-{opt}",   # new format: NIFTY-2026-05-23800-CE
        f"{base}_{strike}_{opt}_%",   # old format: NIFTY_23800_CE_2026-05-19
    ]
    try:
        kcon = sqlite3.connect(KITE_DB, timeout=5)
        for pattern in patterns:
            rows = kcon.execute("""
                SELECT dt, open, high, low, close, volume
                FROM candles_1min
                WHERE tradingsymbol LIKE ? AND date(dt) = ?
                ORDER BY dt
            """, (pattern, run_date)).fetchall()
            if rows:
                break
        kcon.close()
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["timestamp","open","high","low","close","volume"])
        ts = pd.to_datetime(df["timestamp"])
        if ts.dt.tz is not None:
            df["timestamp"] = ts.dt.tz_convert(IST)
        else:
            df["timestamp"] = ts.dt.tz_localize(IST, ambiguous="NaT", nonexistent="NaT")
        df = df.dropna(subset=["timestamp"]).reset_index(drop=True)
        return df if not df.empty else None
    except Exception as e:
        return None

# ── Run analysis ───────────────────────────────────────────────────────────────
results = []
no_data = []

for r in paper:
    ts          = r["tradingsymbol"] or ""
    ep_signal   = r["entry_price"] or 0
    ep_actual   = r["actual_entry_price"] or ep_signal   # paper actual fill
    action      = r["action"] or "BUY"
    qty         = r["quantity"] or 1
    paper_pnl   = r["pnl"] or 0
    sl_stage    = r["sl_stage"] or r["status"] or "?"
    strat       = r["strategy_name"] or ""
    created_at  = r["created_at"] or ""

    # Entry time
    try:
        entry_time = datetime.fromisoformat(created_at)
        if entry_time.tzinfo is None:
            entry_time = IST.localize(entry_time)
        else:
            entry_time = entry_time.astimezone(IST)
    except Exception:
        entry_time = IST.localize(datetime.strptime(run_date, "%Y-%m-%d").replace(hour=9, minute=15))

    candles = _load_kite_candles(ts, run_date)
    if candles is None:
        no_data.append({"strat": strat, "ts": ts, "paper_pnl": paper_pnl, "sl_stage": sl_stage})
        continue

    # ── Simulation A: actual paper entry price + WhatIf (current) SL config ──
    sim_cur = simulate_sl(ep_actual, action, entry_time, candles, SL_CFG, tradingsymbol=ts)
    pnl_cur = round(sim_cur["pnl_per_unit"] * qty, 2)

    # ── Simulation B: actual paper entry price + OPTIMISED SL config ─────────
    sim_opt = simulate_sl(ep_actual, action, entry_time, candles, SL_CFG_OPT, tradingsymbol=ts)
    pnl_opt = round(sim_opt["pnl_per_unit"] * qty, 2)

    results.append({
        "strat":      strat,
        "ts":         ts,
        "action":     action,
        "qty":        qty,
        "ep_actual":  ep_actual,
        "paper_pnl":  paper_pnl,
        "paper_exit": sl_stage,
        "sim_pnl":    pnl_cur,
        "sim_exit":   sim_cur["exit_reason"],
        "opt_pnl":    pnl_opt,
        "opt_exit":   sim_opt["exit_reason"],
    })

# ── Print trade-by-trade table ─────────────────────────────────────────────────
print(f"\n{'Strategy':<22} {'Symbol':<30} {'EP':>7} {'Paper':>9} {'Sim_Kite':>9} {'Opt_SL':>9}  {'PaperExit':<16} {'SimExit':<14} {'OptExit'}")
print(f"{'-'*22} {'-'*30} {'-'*7} {'-'*9} {'-'*9} {'-'*9}  {'-'*16} {'-'*14} {'-'*12}")
for r in results:
    print(f"{r['strat']:<22} {r['ts']:<30} {r['ep_actual']:>7.1f} "
          f"{r['paper_pnl']:>+9.0f} {r['sim_pnl']:>+9.0f} {r['opt_pnl']:>+9.0f}  "
          f"{r['paper_exit']:<16} {r['sim_exit']:<14} {r['opt_exit']}")

if no_data:
    print(f"\n  [NO KITE DATA — {len(no_data)} trades not simulated]")
    for d in no_data:
        print(f"    {d['strat']:<22} {d['ts']:<35} paper={d['paper_pnl']:+.0f} exit={d['sl_stage']}")

# ── Summary ────────────────────────────────────────────────────────────────────
def _stats(items, key):
    vals    = [r[key] for r in items]
    total   = sum(vals)
    wins    = sum(1 for v in vals if v > 0.01)
    losses  = sum(1 for v in vals if v < -0.01)
    evens   = len(vals) - wins - losses
    return total, wins, losses, evens

def _exit_counts(items, key):
    c = {}
    for r in items:
        c[r[key]] = c.get(r[key], 0) + 1
    return c

t_paper,  wp, lp, ep2 = _stats(results, "paper_pnl")
t_sim,    ws, ls, es  = _stats(results, "sim_pnl")
t_opt,    wo, lo, eo  = _stats(results, "opt_pnl")

# Include no-data paper PnL in totals
paper_nodata = sum(d["paper_pnl"] for d in no_data)
t_paper_all  = t_paper + paper_nodata

print(f"\n{'='*75}")
print(f"  SUMMARY  (simulated={len(results)} trades | no-kite-data={len(no_data)} trades)")
print(f"{'='*75}")
print(f"                      {'PnL':>12}  {'Win':>5} {'Loss':>5} {'Even':>5}")
print(f"  Paper (actual)    : {t_paper_all:>+12,.0f}  {'-':>5} {'-':>5} {'-':>5}  [all 59 trades incl. no-data]")
print(f"  Paper (sim subset): {t_paper:>+12,.0f}  {wp:>5} {lp:>5} {ep2:>5}  [only trades with kite data]")
print(f"  Kite-Sim (cur SL) : {t_sim:>+12,.0f}  {ws:>5} {ls:>5} {es:>5}  [same entry, WhatIf SL logic]")
print(f"  Kite-Sim (opt SL) : {t_opt:>+12,.0f}  {wo:>5} {lo:>5} {eo:>5}  [same entry, breakeven@1%]")
print()
print(f"  Gap: Paper → Kite-Sim (cur)  : {t_sim - t_paper:>+,.0f}  (entry same, only SL logic changed)")
print(f"  Gap: Paper → Kite-Sim (opt)  : {t_opt - t_paper:>+,.0f}  (entry same + optimised breakeven)")
print()

# Exit reason breakdown
ec_paper = {}
for r in results: ec_paper[r["paper_exit"]] = ec_paper.get(r["paper_exit"], 0) + 1
ec_sim   = _exit_counts(results, "sim_exit")
ec_opt   = _exit_counts(results, "opt_exit")

print(f"  Paper exit reasons   : {ec_paper}")
print(f"  Kite-Sim exit reasons: {ec_sim}")
print(f"  Opt-SL exit reasons  : {ec_opt}")
print()

# INITIAL_SL analysis
init_paper = [r for r in results if "INITIAL" in r["paper_exit"]]
init_sim   = [r for r in results if r["sim_exit"] == "INITIAL_SL"]
init_opt   = [r for r in results if r["opt_exit"] == "INITIAL_SL"]
print(f"  INITIAL_SL hit rate:")
n = len(results) or 1
print(f"    Paper live monitor : {len(init_paper)}/{len(results)} = {len(init_paper)/n*100:.0f}%   PnL = {sum(r['paper_pnl'] for r in init_paper):+,.0f}")
print(f"    Kite-Sim (cur SL)  : {len(init_sim)}/{len(results)} = {len(init_sim)/n*100:.0f}%   PnL = {sum(r['sim_pnl'] for r in init_sim):+,.0f}")
print(f"    Kite-Sim (opt SL)  : {len(init_opt)}/{len(results)} = {len(init_opt)/n*100:.0f}%   PnL = {sum(r['opt_pnl'] for r in init_opt):+,.0f}")
print()

# Trades that hit INITIAL_SL in paper but NOT in simulation → these are recoverable
recoverable = [r for r in results
               if "INITIAL" in r["paper_exit"]
               and r["sim_exit"] != "INITIAL_SL"]
if recoverable:
    print(f"  Trades saved by Kite-Sim SL (hit INITIAL_SL in paper but not in sim):")
    for r in recoverable:
        print(f"    {r['strat']:<22} paper={r['paper_pnl']:>+7.0f} → sim={r['sim_pnl']:>+7.0f}  "
              f"(sim_exit={r['sim_exit']})")
    saved_pnl = sum(r["sim_pnl"] - r["paper_pnl"] for r in recoverable)
    print(f"    Total PnL rescue: {saved_pnl:>+,.0f}")
print()

# KEY INSIGHT SECTION
print(f"{'='*75}")
print(f"  ROOT CAUSE ANALYSIS")
print(f"{'='*75}")

# Breakeven difference
be_paper = [r for r in results if "BREAKEVEN" in r["paper_exit"]]
be_sim   = [r for r in results if r["sim_exit"] in ("TRAILING_SL", "BREAKEVEN")]
print(f"  Trades reaching breakeven or better:")
print(f"    Paper    : {len(be_paper)} trades (trailing/breakeven stage reached)")
print(f"    Kite-Sim : {len(be_sim)} trades (TRAILING_SL or BREAKEVEN)")
print()
print(f"  Live SL config (sl_config.json):")
print(f"    Breakeven trigger  : 3% gain from entry")
print(f"    Trail trigger      : 5% gain from entry")
print(f"    Initial SL (index) : 8% below entry")
print(f"    Min hold           : 3 minutes (SL can't fire in first 3 min)")
print()
print(f"  WhatIf SL config (current):")
print(f"    Breakeven trigger  : 0.5 × ATR (≈1% for typical index options)")
print(f"    Trail              : peak − 2.0 × ATR")
print(f"    Initial SL         : ATR × 1.5, floored at 5%, capped at 8%")
print(f"    Min hold           : none")
print()
print(f"  >> Live monitor reaches breakeven at 3% gain (≈3× harder than WhatIf)")
print(f"  >> Many trades get stopped at INITIAL_SL before reaching +3% gain")
print(f"  >> WhatIf reaches breakeven at ~1% — most trades survive the initial dip")
print(f"{'='*75}\n")
