"""
simulate_may11_new_trail.py
----------------------------
Re-runs May 11 What-If trades with the NEW config-driven trailing SL
(trail_pct_am=5%, trail_pct_pm=5%, trail_pct_final=3%) and compares
against the original 1.5% PM trail that was hardcoded before today's fix.

Run:  python simulate_may11_new_trail.py
"""
import sys, os, sqlite3
from datetime import date, datetime

os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI')
from master_resource import MasterResource

CANDLE_DB = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db'
RUN_DATE  = '2026-05-11'

# ── New config values (from sl_config.json as of today) ───────────────────────
TRAIL_ACT_PCT    = 5.0   # gain% to activate trail
BEVEN_ACT_PCT    = 3.0   # gain% to activate breakeven
TRAIL_PCT_AM     = 5.0   # before 13:00
TRAIL_PCT_PM     = 5.0   # 13:00-14:59
TRAIL_PCT_FINAL  = 3.0   # 15:00+

# ── Old hardcoded values (for comparison) ─────────────────────────────────────
OLD_TRAIL_AM  = 3.0
OLD_TRAIL_PM  = 1.5   # this was the culprit

cconn = sqlite3.connect(CANDLE_DB)
cconn.row_factory = sqlite3.Row
tconn = sqlite3.connect(MasterResource.get_trading_db_path())
tconn.row_factory = sqlite3.Row


def get_candle_sym(label: str) -> str | None:
    """Convert 'BASE-MON-STRIKE-TYPE' label to kite_candles.db symbol."""
    parts = label.split('-')
    if len(parts) < 4:
        return None
    base, _mon, strike, opt = parts[0], parts[1], parts[2], parts[3]
    ccur = cconn.cursor()
    ccur.execute(
        "SELECT DISTINCT tradingsymbol FROM candles_1min WHERE tradingsymbol LIKE ?",
        (f'{base}_{strike}_{opt}%',))
    r = ccur.fetchone()
    return r[0] if r else None


def get_candles(sym: str) -> list[dict]:
    ccur = cconn.cursor()
    ccur.execute(
        "SELECT dt,open,high,low,close FROM candles_1min "
        "WHERE tradingsymbol=? AND date(dt)=? ORDER BY dt",
        (sym, RUN_DATE))
    return [dict(r) for r in ccur.fetchall()]


def simulate(entry: float, entry_dt_tz: str, candles: list[dict],
             trail_am: float, trail_pm: float, trail_final: float,
             trail_act: float, beven_act: float, is_index: bool):
    """
    Simulate BUY trade from entry_dt_tz forward using the given trail params.
    Returns (exit_price, exit_time, reason, peak_pct_reached).
    """
    post = [c for c in candles if c['dt'] >= entry_dt_tz]
    if not post:
        return None, None, 'NO_DATA', 0.0

    sl_pct     = 8.0 if is_index else 5.0
    current_sl = round(entry * (1 - sl_pct / 100), 2)
    sl_stage   = 'INITIAL'
    peak_pct   = 0.0

    for i, c in enumerate(post):
        ltp    = c['close']
        hour   = int(c['dt'][11:13])
        gain   = (ltp - entry) / entry * 100

        peak_pct = max(peak_pct, gain)

        # Trail selection
        if hour >= 15:
            t_pct = trail_final
        elif hour >= 13:
            t_pct = trail_pm
        else:
            t_pct = trail_am

        # Stage updates
        if gain >= trail_act:
            new_sl = round(max(ltp * (1 - t_pct / 100), entry), 2)
            if new_sl > current_sl:
                current_sl = new_sl
                sl_stage = 'TRAIL_FINAL' if hour >= 15 else ('TRAIL_PM' if hour >= 13 else 'TRAIL_AM')
        elif gain >= beven_act:
            if entry > current_sl:
                current_sl = entry
                sl_stage = 'BREAKEVEN'

        # Skip SL check for first 3 candles (min_hold_candles=3)
        if i < 3:
            continue

        # SL hit on candle close
        if ltp <= current_sl:
            return round(current_sl, 2), c['dt'][11:16], sl_stage, peak_pct

    last = post[-1]
    return round(last['close'], 2), last['dt'][11:16], 'EOD', peak_pct


# ── Load trades ────────────────────────────────────────────────────────────────
tcur = tconn.cursor()
tcur.execute(
    "SELECT * FROM whatif_trades WHERE run_date=? AND data_available=1 ORDER BY signal_id",
    (RUN_DATE,))
trades = [dict(r) for r in tcur.fetchall()]

_INDEX_BASES = {'NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX'}

SEP = '-' * 155
print(f"\n{'MAY 11 — NEW TRAIL SL SIMULATION (5% AM/PM, 3% FINAL)':^155}")
print(SEP)
print(f"{'Sig':5} {'Symbol':33} {'Lot':4} {'Entry':7} {'EntrT':5}  "
      f"{'ORIGINAL (1.5% PM)':>38}  "
      f"{'NEW (5%/5%/3%)':>38}  "
      f"{'Delta':>8}  {'Peak%':>6}")
print(SEP)

total_orig = total_new = 0.0
no_data    = []

for t in trades:
    sym = get_candle_sym(t['tradingsymbol'])
    if not sym:
        no_data.append(t['signal_id'])
        continue

    entry  = float(t['entry_price'])
    lot    = int(t['lot_size'] or 1)
    et_tz  = t['entry_time'][:16].replace(' ', 'T') + ':00+05:30'
    candles = get_candles(sym)

    is_idx = any(sym.upper().startswith(b + '_') for b in _INDEX_BASES)

    # Original (old hard-coded logic)
    orig_exit, orig_t, orig_reason, _ = simulate(
        entry, et_tz, candles,
        trail_am=OLD_TRAIL_AM, trail_pm=OLD_TRAIL_PM, trail_final=OLD_TRAIL_PM,
        trail_act=5.0, beven_act=3.0, is_index=is_idx)

    # New config-driven logic
    new_exit, new_t, new_reason, peak_pct = simulate(
        entry, et_tz, candles,
        trail_am=TRAIL_PCT_AM, trail_pm=TRAIL_PCT_PM, trail_final=TRAIL_PCT_FINAL,
        trail_act=TRAIL_ACT_PCT, beven_act=BEVEN_ACT_PCT, is_index=is_idx)

    # Compare against the ACTUAL whatif result (which used the old code)
    actual_orig_pnl = float(t['pnl_total'])
    total_orig += actual_orig_pnl

    if orig_exit:
        orig_pnl = round((orig_exit - entry) * lot, 2)
    else:
        orig_pnl = 0.0

    if new_exit:
        new_pnl  = round((new_exit - entry) * lot, 2)
    else:
        new_pnl  = 0.0

    total_new += new_pnl
    delta      = new_pnl - actual_orig_pnl
    tag        = '<<BETTER' if delta > 100 else ('worse' if delta < -100 else '~')

    print(f"  #{t['signal_id']:<4} {sym[:32]:32} {lot:4d} {entry:7.2f} {t['entry_time'][11:16]:5}  "
          f"exit={orig_exit or 0:7.2f}@{orig_t or '?':5} {orig_pnl:+8.0f} [{orig_reason[:10]:10}]  "
          f"exit={new_exit or 0:7.2f}@{new_t or '?':5} {new_pnl:+8.0f} [{new_reason[:10]:10}]  "
          f"delta={delta:+8.0f}  peak={peak_pct:+5.1f}%  {tag}")

print(SEP)
print()
# Use the actual database P&L as orig baseline (What-If recorded value)
tcur.execute("SELECT SUM(pnl_total) FROM whatif_trades WHERE run_date=? AND data_available=1", (RUN_DATE,))
db_total = tcur.fetchone()[0] or 0

print(f"  {'DB What-If (recorded, old trail 1.5% PM):':<48} {db_total:>+10,.0f}")
print(f"  {'Simulated OLD (re-run, old trail 1.5% PM):':<48} {total_orig:>+10,.0f}")
print(f"  {'Simulated NEW (5%/5%/3% trail):':<48} {total_new:>+10,.0f}")
print(f"  {'Improvement vs DB recorded:':<48} {total_new - db_total:>+10,.0f}")
print()
if no_data:
    print(f"  No candle data for signal IDs: {no_data}")
print()
