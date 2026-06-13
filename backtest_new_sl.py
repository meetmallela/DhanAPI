"""
backtest_new_sl.py — Backtest with ALL 6 SL changes vs current 5% SL.

Changes applied:
  1. SL check on 1-min candle close (not on wick/low)
  2. Min hold: 3 candles before SL is evaluated
  3. Wider initial SL: 8% for index options, 5% for stocks
  4. OTM filter: skip if >3% OTM on near-expiry (<=2 day) options
  5. Late entry filter: skip entries after 14:30 on near-expiry options
  6. ATR v3 trailing: breakeven +3%, trail 3% AM / 1.5% PM at +5%+
"""
import sys, os, sqlite3
from datetime import date, datetime
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI')
from master_resource import get_trading_db_path

CANDLE_DB = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db'
RUN_DATE  = '2026-04-20'

cconn = sqlite3.connect(CANDLE_DB)
cconn.row_factory = sqlite3.Row
tconn = sqlite3.connect(get_trading_db_path())
tconn.row_factory = sqlite3.Row

tcur = tconn.cursor()
tcur.execute("SELECT * FROM whatif_trades WHERE run_date=? AND data_available=1 ORDER BY signal_id",
             (RUN_DATE,))
trades = [dict(r) for r in tcur.fetchall()]

_INDEX_BASES = {'NIFTY', 'BANKNIFTY', 'SENSEX', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX'}
OTM_LIMIT_PCT   = 3.0
NEAR_EXPIRY_DAYS = 2
LATE_ENTRY_HOUR  = 14
LATE_ENTRY_MIN   = 30


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_candle_sym(label):
    parts = label.split('-')
    if len(parts) == 4:
        base, mon, strike, opt = parts
        ccur = cconn.cursor()
        ccur.execute('SELECT DISTINCT tradingsymbol FROM candles_1min WHERE tradingsymbol LIKE ?',
                     (f'{base}_{strike}_{opt}%',))
        r = ccur.fetchone()
        return r[0] if r else None
    return None


def get_candles(sym):
    ccur = cconn.cursor()
    ccur.execute(
        "SELECT dt,open,high,low,close FROM candles_1min "
        "WHERE tradingsymbol=? AND date(dt)=? ORDER BY dt",
        (sym, RUN_DATE))
    return [dict(r) for r in ccur.fetchall()]


def get_index_spot_at(index_sym, entry_dt_tz):
    """Return the 1-min close of index_sym at or just before entry_dt_tz."""
    ccur = cconn.cursor()
    ccur.execute(
        "SELECT close FROM candles_1min "
        "WHERE tradingsymbol=? AND date(dt)=? AND dt <= ? "
        "ORDER BY dt DESC LIMIT 1",
        (index_sym, RUN_DATE, entry_dt_tz))
    r = ccur.fetchone()
    return r[0] if r else None


def detect_index_base(tradingsymbol: str) -> str | None:
    """Return the index base name if this is an index option, else None."""
    ts = tradingsymbol.upper()
    for b in _INDEX_BASES:
        if ts.startswith(b + '_') or ts.startswith(b + '-'):
            return b
    return None


def should_skip(t: dict, parsed_sym: str) -> str | None:
    """
    Returns skip reason (Changes 4+5) or None.
    parsed_sym: the Kite candle symbol like 'NIFTY_24300_PE_2026-04-21'
    """
    # Derive expiry from tradingsymbol  e.g. NIFTY_24300_PE_2026-04-21
    parts = parsed_sym.split('_')
    if len(parts) < 4:
        return None
    try:
        expiry = date.fromisoformat(parts[-1])
    except Exception:
        return None

    days_to_expiry = (expiry - date.fromisoformat(RUN_DATE)).days
    if days_to_expiry > NEAR_EXPIRY_DAYS:
        return None  # not near-expiry, no filter

    # Change 5: time filter
    entry_time = t['entry_time'][11:16]  # 'HH:MM'
    eh, em = map(int, entry_time.split(':'))
    if (eh, em) >= (LATE_ENTRY_HOUR, LATE_ENTRY_MIN):
        return f"LATE_ENTRY after {LATE_ENTRY_HOUR}:{LATE_ENTRY_MIN:02d} (expiry in {days_to_expiry}d)"

    # Change 4: OTM filter
    base = detect_index_base(parsed_sym)
    if not base:
        return None
    strike   = float(parts[1])
    opt_type = parts[2].upper()
    et_tz    = t['entry_time'][:16].replace(' ', 'T') + ':00+05:30'
    spot     = get_index_spot_at(base, et_tz)
    if spot and spot > 0:
        otm_pct = ((strike - spot) / spot * 100) if opt_type == 'CE' else ((spot - strike) / spot * 100)
        if otm_pct > OTM_LIMIT_PCT:
            return f"OTM_FILTER {strike} {opt_type} is {otm_pct:.1f}% OTM (spot={spot:.0f})"

    return None


# ── Simulation: NEW SL logic (all 6 changes) ─────────────────────────────────

def simulate_new_sl(entry, entry_dt_tz, all_candles, is_index):
    """
    Changes 1+2+3+6:
    - Initial SL: 8% index / 5% stock
    - No SL check for first 3 candles
    - SL check on candle close (not low)
    - ATR v3 trailing: breakeven +3%, trail 3%AM/1.5%PM at +5%+
    """
    post = [c for c in all_candles if c['dt'] >= entry_dt_tz]
    if not post:
        return None, None, 'NO_DATA', 0

    sl_pct     = 8.0 if is_index else 5.0
    initial_sl = round(entry * (1 - sl_pct / 100), 2)
    current_sl = initial_sl
    sl_stage   = 'INITIAL'

    for i, candle in enumerate(post):
        ltp  = candle['close']
        hour = int(candle['dt'][11:13])
        pnl_pct = (ltp - entry) / entry * 100

        # Change 6: update trailing SL on every candle
        if pnl_pct >= 5.0:
            trail_pct = 0.015 if hour >= 13 else 0.030
            new_sl    = round(max(ltp * (1 - trail_pct), entry), 2)
            if new_sl > current_sl:
                current_sl = new_sl
                sl_stage   = 'TRAIL_PM' if hour >= 13 else 'TRAIL_AM'
        elif pnl_pct >= 3.0:
            if entry > current_sl:
                current_sl = entry
                sl_stage   = 'BREAKEVEN'

        # Change 2: skip SL check for first 3 candles
        if i < 3:
            continue

        # Change 1: SL hit on candle CLOSE (not on wick)
        if ltp <= current_sl:
            return round(current_sl, 2), candle['dt'][11:16], sl_stage, sl_pct

    last = post[-1]
    return round(last['close'], 2), last['dt'][11:16], 'EOD', sl_pct


# ── Run ───────────────────────────────────────────────────────────────────────

SEP = '-' * 145
print(f"\n{'ALL 6 SL CHANGES — BACKTEST':^145}")
print(SEP)
print(f"{'#Sig':6} {'Symbol':32} {'Lot':4} {'Entry':6} {'Time':5}  "
      f"{'--- CURRENT (5% tick) ---':28} {'--- NEW SL (candle-close+min-hold+v3) ---':42} {'Delta':>9}")
print(SEP)

total_cur = total_new = 0
skipped = []

for t in trades:
    sym = get_candle_sym(t['tradingsymbol'])
    if not sym:
        print(f"  #{t['signal_id']}: no candle data for {t['tradingsymbol']}")
        continue

    # Changes 4+5: filter check
    skip_reason = should_skip(t, sym)
    if skip_reason:
        skipped.append((t['signal_id'], sym, t['entry_time'][11:16], skip_reason,
                        t['pnl_total']))
        total_cur += t['pnl_total']
        # Would-be loss avoided or missed gain — mark as 0 for new system
        print(f"  #{t['signal_id']:<5} {sym[:31]:31} {t['lot_size']:4d} {t['entry_price']:6.1f} "
              f"{t['entry_time'][11:16]:5}  "
              f"[CURRENT] exit={t['exit_price']:7.2f} {t['pnl_per_unit']:+6.2f}/u {t['pnl_total']:+7.0f} "
              f"[{t['exit_reason'][:10]:10}]  "
              f"[NEW] SKIPPED ({skip_reason[:35]})  "
              f"delta={0 - t['pnl_total']:+8.0f}")
        continue

    entry  = t['entry_price']
    lot    = t['lot_size'] or 1
    et_tz  = t['entry_time'][:16].replace(' ', 'T') + ':00+05:30'
    all_c  = get_candles(sym)
    base   = detect_index_base(sym)
    is_idx = base is not None

    cur_exit      = t['exit_price']
    cur_pnl_unit  = t['pnl_per_unit']
    cur_pnl_lot   = t['pnl_total']

    new_exit, new_t, new_reason, sl_pct = simulate_new_sl(entry, et_tz, all_c, is_idx)

    if new_exit:
        new_pnl_unit = new_exit - entry
        new_pnl_lot  = new_pnl_unit * lot
    else:
        new_pnl_unit = new_pnl_lot = 0

    delta = new_pnl_lot - cur_pnl_lot
    tag   = '<<< BETTER' if delta > 50 else ('worse' if delta < -50 else '~')

    total_cur += cur_pnl_lot
    total_new += new_pnl_lot

    sl_label = f"{sl_pct:.0f}%"

    print(f"  #{t['signal_id']:<5} {sym[:31]:31} {lot:4d} {entry:6.1f} {t['entry_time'][11:16]:5}  "
          f"SL={t['sl_initial']:6.2f}(5%) exit={cur_exit:7.2f} {cur_pnl_unit:+6.2f}/u {cur_pnl_lot:+7.0f} "
          f"[{t['exit_reason'][:10]:10}]  "
          f"SL={entry*(1-sl_pct/100):.2f}({sl_label}) exit={new_exit or 0:7.2f} "
          f"{new_pnl_unit:+6.2f}/u {new_pnl_lot:+7.0f} [{new_reason[:10]:10}] "
          f"delta={delta:+8.0f} {tag}")

print(SEP)
print(f"\n  TOTALS (traded signals only):")
print(f"    Current 5% tick-SL  : {total_cur:+,.0f}")
print(f"    New SL (all changes) : {total_new:+,.0f}")
print(f"    Net improvement      : {total_new - total_cur:+,.0f}")

if skipped:
    print(f"\n  SKIPPED by filters (Changes 4+5):")
    skip_cur_total = 0
    for sig, s, et, reason, pnl in skipped:
        skip_cur_total += pnl
        tag = 'LOSS AVOIDED' if pnl < -50 else ('GAIN MISSED' if pnl > 50 else '~')
        print(f"    #{sig:<5} {s[:32]:32} {et:5}  {reason[:55]:55}  cur_pnl={pnl:+7.0f}  [{tag}]")
    print(f"    Skipped signals: {len(skipped)} | "
          f"Their current P&L (loss avoided / gain missed): {skip_cur_total:+,.0f}")
    print(f"\n  GRAND TOTAL if skips had 0 P&L:")
    grand_new   = total_new
    grand_cur   = total_cur
    grand_delta = grand_new - grand_cur + (0 - skip_cur_total)
    print(f"    Current (all 12) : {grand_cur + skip_cur_total:+,.0f}")
    print(f"    New (traded only): {grand_new:+,.0f}  (skipped=0)")
    print(f"    Net improvement  : {grand_delta:+,.0f}")
print()
