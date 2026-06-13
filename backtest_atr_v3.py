"""
ATR v3 SL Backtest — applies sl_monitor_with_trailing_ATR_v3 logic to today's whatif trades.
"""
import sys, os, sqlite3
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI')
from master_resource import get_trading_db_path

CANDLE_DB = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db'
cconn = sqlite3.connect(CANDLE_DB)
cconn.row_factory = sqlite3.Row

tconn = sqlite3.connect(get_trading_db_path())
tconn.row_factory = sqlite3.Row
tcur = tconn.cursor()
tcur.execute("SELECT * FROM whatif_trades WHERE run_date='2026-04-20' AND data_available=1 ORDER BY signal_id")
trades = [dict(r) for r in tcur.fetchall()]


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
    ccur.execute('SELECT dt,open,high,low,close FROM candles_1min WHERE tradingsymbol=? AND date(dt)="2026-04-20" ORDER BY dt', (sym,))
    return [dict(r) for r in ccur.fetchall()]


def agg_to_5min(candles_1m):
    bars = []
    for i in range(0, len(candles_1m) - (len(candles_1m) % 5), 5):
        grp = candles_1m[i:i+5]
        if len(grp) < 5:
            continue
        bars.append({
            'open': grp[0]['open'],
            'high': max(c['high'] for c in grp),
            'low':  min(c['low']  for c in grp),
            'close': grp[-1]['close']
        })
    return bars[-14:] if len(bars) >= 14 else bars


def calc_atr(bars):
    if len(bars) < 2:
        return None
    trs = [bars[0]['high'] - bars[0]['low']]
    for i in range(1, len(bars)):
        hl  = bars[i]['high'] - bars[i]['low']
        hpc = abs(bars[i]['high'] - bars[i-1]['close'])
        lpc = abs(bars[i]['low']  - bars[i-1]['close'])
        trs.append(max(hl, hpc, lpc))
    return sum(trs) / len(trs)


def simulate_atr_v3(entry, entry_dt_tz, all_candles, is_option=True):
    """
    ATR v3 rules:
    - Initial SL: 1.5x ATR, capped [5%, 8%] for options
    - +3%: move to breakeven
    - +5%+: trail 3% AM / 1.5% PM
    Returns (exit_price, exit_time, exit_reason, initial_sl, sl_pct, method)
    """
    pre     = [c for c in all_candles if c['dt'] < entry_dt_tz]
    post    = [c for c in all_candles if c['dt'] >= entry_dt_tz]
    if not post:
        return None, None, 'NO_DATA', None, 0, 'N/A'

    pre_5m  = agg_to_5min(pre)
    atr     = calc_atr(pre_5m)

    FIXED_PCT   = 0.05
    ATR_MULT    = 1.5
    ATR_CAP_PCT = 0.08

    fixed_sl = entry * (1 - FIXED_PCT)

    if atr and atr > 0:
        atr_sl = entry - ATR_MULT * atr
        if is_option:
            floor_sl = entry * (1 - ATR_CAP_PCT)
            if atr_sl < floor_sl:
                atr_sl = floor_sl
        initial_sl = atr_sl if atr_sl < fixed_sl else fixed_sl
        sl_pct   = (entry - initial_sl) / entry * 100
        method   = f'ATR {sl_pct:.1f}%'
    else:
        initial_sl = fixed_sl
        sl_pct     = 5.0
        method     = 'FIXED 5%'

    current_sl   = initial_sl
    current_mode = 'INITIAL'

    for candle in post:
        ltp     = candle['close']
        lo      = candle['low']
        pnl_pct = (ltp - entry) / entry * 100
        hour    = int(candle['dt'][11:13])

        # SL hit check (candle low)
        if lo <= current_sl:
            reason = current_mode if current_mode != 'INITIAL' else 'INITIAL_SL'
            return round(current_sl, 2), candle['dt'][11:16], reason, initial_sl, sl_pct, method

        # Trailing logic (mirrors v3 calculate_trailing_sl)
        if pnl_pct >= 5.0:
            trail_pct = 0.015 if hour >= 13 else 0.030
            new_sl    = max(ltp * (1 - trail_pct), entry)
            new_sl    = round(new_sl, 2)
            if new_sl > current_sl:
                current_sl   = new_sl
                current_mode = 'TRAILING_PM' if hour >= 13 else 'TRAILING_SL'
        elif pnl_pct >= 3.0:
            if entry > current_sl:
                current_sl   = entry
                current_mode = 'BREAKEVEN'

    last = post[-1]
    return round(last['close'], 2), last['dt'][11:16], 'EOD', initial_sl, sl_pct, method


# ── Run ────────────────────────────────────────────────────────────────────
HDR = (f"{'#Sig':6} {'Instrument':30} {'Lot':4} {'Entry':6} {'Time':5}  "
       f"{'-- CURRENT (5% SL) --':25} {'---------- ATR v3 SL ----------':35} {'Delta':8}")
print(HDR)
print('-' * 130)

total_cur = total_atr = 0

for t in trades:
    sym = get_candle_sym(t['tradingsymbol'])
    if not sym:
        print(f"#{t['signal_id']}: no candle data for {t['tradingsymbol']}")
        continue

    entry    = t['entry_price']
    lot      = t['lot_size'] or 1
    et_raw   = t['entry_time'][:16]                        # '2026-04-20T09:35'
    et_tz    = et_raw.replace(' ', 'T') + ':00+05:30'

    all_c    = get_candles(sym)

    cur_exit     = t['exit_price']
    cur_reason   = t['exit_reason']
    cur_pnl_unit = t['pnl_per_unit']
    cur_pnl_lot  = t['pnl_total']

    atr_exit, atr_t, atr_reason, init_sl, sl_pct, method = simulate_atr_v3(entry, et_tz, all_c)

    if atr_exit:
        atr_pnl_unit = atr_exit - entry
        atr_pnl_lot  = atr_pnl_unit * lot
    else:
        atr_pnl_unit = atr_pnl_lot = 0

    delta = atr_pnl_lot - cur_pnl_lot
    tag   = '<<< BETTER' if delta > 50 else ('worse' if delta < -50 else '~')

    total_cur += cur_pnl_lot
    total_atr += atr_pnl_lot

    print(
        f"#{t['signal_id']:<5} {sym[:29]:29} {lot:4d} {entry:6.1f} {et_raw[11:16]:5}  "
        f"SL={t['sl_initial']:6.2f}(5.0%) exit={cur_exit:7.2f} {cur_pnl_unit:+6.2f}/u {cur_pnl_lot:+7.0f} [{cur_reason[:10]:10}]  "
        f"SL={init_sl:.2f}({sl_pct:.1f}%,{method[:8]:8}) exit={atr_exit or 0:7.2f} {atr_pnl_unit:+6.2f}/u {atr_pnl_lot:+7.0f} [{atr_reason[:12]:12}] "
        f"delta={delta:+7.0f} {tag}"
    )

print('-' * 130)
print(f"TOTALS:  Current 5% SL = {total_cur:+,.0f}  |  ATR v3 SL = {total_atr:+,.0f}  |  Net improvement = {total_atr-total_cur:+,.0f}")
