"""
feature_analysis.py
-------------------
Phase 1: Pre-entry feature extraction for all 20-Apr trades.

For each trade, reads the 30 candles BEFORE entry (option + parent index),
computes key technical features, labels WIN/LOSS, and prints a
side-by-side comparison table so patterns can be spotted visually.

Features computed:
  1. Index EMA9 vs EMA21 trend at entry (BULLISH / BEARISH / FLAT)
  2. Index direction last 5 candles (price momentum)
  3. Option relative volume at entry (vs avg of prior 15 candles)
  4. Option price vs VWAP at entry (ABOVE / BELOW)
  5. Option premium % drop from day's open to entry
  6. ATR regime (high / normal — vs median ATR of day)
  7. Volume trend (last 5 candles: RISING / FALLING / FLAT)
  8. Opening 5-min candle direction (UP / DOWN / DOJI)

Run:  python feature_analysis.py
"""
import sys, os, sqlite3
import math
from datetime import datetime
os.environ['PYTHONIOENCODING'] = 'utf-8'
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI')
from master_resource import get_trading_db_path

CANDLE_DB = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db'
RUN_DATE  = '2026-04-23'

cconn = sqlite3.connect(CANDLE_DB)
cconn.row_factory = sqlite3.Row
tconn = sqlite3.connect(get_trading_db_path())
tconn.row_factory = sqlite3.Row

tcur = tconn.cursor()
tcur.execute("SELECT * FROM whatif_trades WHERE run_date=? AND data_available=1 ORDER BY signal_id",
             (RUN_DATE,))
trades = [dict(r) for r in tcur.fetchall()]

# Map option base → index symbol in candles DB
_INDEX_MAP = {
    'NIFTY':     'NIFTY',
    'BANKNIFTY': 'BANKNIFTY',
    'SENSEX':    'SENSEX',
    'FINNIFTY':  'FINNIFTY',
    'MIDCPNIFTY':'MIDCPNIFTY',
    'BANKEX':    'BANKEX',
}
_INDEX_BASES = set(_INDEX_MAP.keys())


# ── DB helpers ────────────────────────────────────────────────────────────────

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


def get_candles_before(sym, before_dt_tz, n=35):
    """Return up to n 1-min candles strictly before before_dt_tz, ordered oldest-first."""
    ccur = cconn.cursor()
    ccur.execute("""
        SELECT dt,open,high,low,close,volume
        FROM candles_1min
        WHERE tradingsymbol=? AND date(dt)=? AND dt < ?
        ORDER BY dt DESC LIMIT ?
    """, (sym, RUN_DATE, before_dt_tz, n))
    rows = [dict(r) for r in ccur.fetchall()]
    return list(reversed(rows))  # oldest first


def get_candles_from(sym, from_dt_tz, n=5):
    """Return first n candles from from_dt_tz onwards (for opening candle etc.)."""
    ccur = cconn.cursor()
    ccur.execute("""
        SELECT dt,open,high,low,close,volume
        FROM candles_1min
        WHERE tradingsymbol=? AND date(dt)=? AND dt >= ?
        ORDER BY dt LIMIT ?
    """, (sym, RUN_DATE, from_dt_tz, n))
    return [dict(r) for r in ccur.fetchall()]


def get_day_open_candle(sym):
    """First 1-min candle of the day."""
    ccur = cconn.cursor()
    ccur.execute("""
        SELECT * FROM candles_1min
        WHERE tradingsymbol=? AND date(dt)=? ORDER BY dt LIMIT 1
    """, (sym, RUN_DATE))
    r = ccur.fetchone()
    return dict(r) if r else None


# ── Technical helpers ─────────────────────────────────────────────────────────

def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def vwap(candles):
    """VWAP from a list of candles."""
    cum_pv = cum_v = 0.0
    for c in candles:
        typical = (c['high'] + c['low'] + c['close']) / 3
        v = c['volume'] or 0
        cum_pv += typical * v
        cum_v  += v
    return cum_pv / cum_v if cum_v else None


def atr(candles):
    if len(candles) < 2:
        return None
    trs = [candles[0]['high'] - candles[0]['low']]
    for i in range(1, len(candles)):
        hl  = candles[i]['high'] - candles[i]['low']
        hpc = abs(candles[i]['high'] - candles[i-1]['close'])
        lpc = abs(candles[i]['low']  - candles[i-1]['close'])
        trs.append(max(hl, hpc, lpc))
    return sum(trs) / len(trs)


def rel_volume(candles, lookback=15):
    """Volume of last candle vs average of prior `lookback` candles."""
    if len(candles) < lookback + 1:
        return None
    avg = sum(c['volume'] for c in candles[-lookback-1:-1]) / lookback
    last_vol = candles[-1]['volume']
    return last_vol / avg if avg else None


def volume_trend(candles, n=5):
    """Returns RISING/FALLING/FLAT based on linear slope of last n volumes."""
    if len(candles) < n:
        return 'N/A'
    vols = [c['volume'] for c in candles[-n:]]
    slope = sum((vols[i] - vols[0]) * i for i in range(n))
    pct   = (vols[-1] - vols[0]) / vols[0] * 100 if vols[0] else 0
    if pct > 20:
        return 'RISING'
    elif pct < -20:
        return 'FALLING'
    return 'FLAT'


def index_trend(idx_candles):
    """EMA9 vs EMA21 on index candles. Returns BULLISH/BEARISH/FLAT."""
    if len(idx_candles) < 21:
        return 'N/A'
    closes = [c['close'] for c in idx_candles]
    e9  = ema(closes, 9)
    e21 = ema(closes, 21)
    if e9 is None or e21 is None:
        return 'N/A'
    diff_pct = (e9 - e21) / e21 * 100
    if diff_pct > 0.05:
        return 'BULLISH'
    elif diff_pct < -0.05:
        return 'BEARISH'
    return 'FLAT'


def index_momentum(idx_candles, n=5):
    """% change of index close over last n candles."""
    if len(idx_candles) < n:
        return None
    c0 = idx_candles[-n]['close']
    c1 = idx_candles[-1]['close']
    return (c1 - c0) / c0 * 100 if c0 else None


def opening_candle_dir(sym):
    """Direction of the first 5-min block (09:15-09:20)."""
    first5 = get_candles_from(sym, f'{RUN_DATE}T09:15:00+05:30', 5)
    if not first5:
        return 'N/A'
    op  = first5[0]['open']
    cl  = first5[-1]['close']
    pct = (cl - op) / op * 100 if op else 0
    if pct > 0.3:
        return f'UP({pct:.1f}%)'
    elif pct < -0.3:
        return f'DN({pct:.1f}%)'
    return f'DOJI({pct:.1f}%)'


# ── Main ──────────────────────────────────────────────────────────────────────

WIN_THRESH  = 50    # pnl_total > 50 = WIN (per lot)
LOSS_THRESH = -50

results = []

for t in trades:
    sym = get_candle_sym(t['tradingsymbol'])
    if not sym:
        continue

    entry     = t['entry_price']
    et_tz     = t['entry_time'][:16].replace(' ', 'T') + ':00+05:30'
    pnl_total = t['pnl_total']
    result    = 'WIN' if pnl_total > WIN_THRESH else ('LOSS' if pnl_total < LOSS_THRESH else 'FLAT')

    # Option candles before entry
    opt_candles = get_candles_before(sym, et_tz, 35)

    # Index candles
    parts     = sym.split('_')
    base      = next((b for b in _INDEX_BASES if sym.startswith(b + '_')), None)
    idx_sym   = _INDEX_MAP.get(base) if base else None
    idx_candles = get_candles_before(idx_sym, et_tz, 35) if idx_sym else []

    # --- Feature 1: Index EMA trend ---
    feat_idx_trend = index_trend(idx_candles) if idx_candles else 'N/A(stock)'

    # --- Feature 2: Index 5-candle momentum ---
    feat_idx_mom = index_momentum(idx_candles, 5)
    feat_idx_mom_str = f'{feat_idx_mom:+.2f}%' if feat_idx_mom is not None else 'N/A'

    # --- Feature 3: Relative volume ---
    feat_rvol = rel_volume(opt_candles, 15)
    feat_rvol_str = f'{feat_rvol:.2f}x' if feat_rvol else 'N/A'

    # --- Feature 4: Option vs VWAP ---
    # VWAP uses all candles from open to entry
    all_to_entry = get_candles_from(sym, f'{RUN_DATE}T09:15:00+05:30',
                                    len(opt_candles) + 5)
    vwap_val = vwap(all_to_entry)
    if vwap_val and entry:
        feat_vwap = 'ABOVE' if entry > vwap_val else 'BELOW'
        feat_vwap_str = f'{feat_vwap}({(entry - vwap_val)/vwap_val*100:+.1f}%)'
    else:
        feat_vwap_str = 'N/A'

    # --- Feature 5: Premium drop from day open ---
    day_open_c = get_day_open_candle(sym)
    day_open   = day_open_c['open'] if day_open_c else None
    if day_open and entry:
        drop_pct = (entry - day_open) / day_open * 100
        feat_drop = f'{drop_pct:+.1f}%'
    else:
        feat_drop = 'N/A'

    # --- Feature 6: ATR regime ---
    feat_atr_val = atr(opt_candles[-14:]) if len(opt_candles) >= 14 else None
    feat_atr_pct = (feat_atr_val / entry * 100) if feat_atr_val and entry else None
    feat_atr_str = f'{feat_atr_pct:.1f}%ATR' if feat_atr_pct else 'N/A'

    # --- Feature 7: Volume trend ---
    feat_vtrd = volume_trend(opt_candles, 5)

    # --- Feature 8: Opening candle direction ---
    feat_open_dir = opening_candle_dir(sym)

    results.append({
        'sig':          t['signal_id'],
        'sym':          sym,
        'entry':        entry,
        'time':         t['entry_time'][11:16],
        'pnl':          pnl_total,
        'result':       result,
        'idx_trend':    feat_idx_trend,
        'idx_mom':      feat_idx_mom_str,
        'rvol':         feat_rvol_str,
        'vwap':         feat_vwap_str,
        'drop':         feat_drop,
        'atr':          feat_atr_str,
        'vtrd':         feat_vtrd,
        'open_dir':     feat_open_dir,
    })


# ── Print feature table ───────────────────────────────────────────────────────

SEP = '-' * 175
print(f'\n{"PHASE 1 — PRE-ENTRY FEATURE ANALYSIS  |  Date: "+RUN_DATE:^175}')
print(SEP)
print(f'{"#":5} {"Symbol":32} {"Time":5} {"Entry":6} {"P&L":>7} {"RESULT":6} | '
      f'{"IDX_TREND":10} {"IDX_5m":8} {"RVOL":7} {"vs_VWAP":12} {"DROP_OPEN":9} {"ATR%":8} {"VOL_TRD":8} {"OPEN_5m":10}')
print(SEP)

for r in results:
    flag = '**' if r['result'] == 'WIN' else ('  ' if r['result'] == 'LOSS' else ' ~')
    print(f'{flag}#{r["sig"]:<4} {r["sym"][:31]:31} {r["time"]:5} {r["entry"]:6.1f} '
          f'{r["pnl"]:+7.0f} {r["result"]:6} | '
          f'{r["idx_trend"]:10} {r["idx_mom"]:8} {r["rvol"]:7} {r["vwap"]:12} '
          f'{r["drop"]:9} {r["atr"]:8} {r["vtrd"]:8} {r["open_dir"]:10}')

print(SEP)

# ── Pattern summary ───────────────────────────────────────────────────────────
wins  = [r for r in results if r['result'] == 'WIN']
losses= [r for r in results if r['result'] == 'LOSS']

print(f'\nPATTERN SUMMARY  ({len(wins)} wins  /  {len(losses)} losses)\n')

def pct_match(rows, key, values):
    if not rows: return 'N/A'
    matches = sum(1 for r in rows if any(v in r[key] for v in values))
    return f'{matches}/{len(rows)} ({matches/len(rows)*100:.0f}%)'

headers = [
    ('Index BULLISH at entry', 'idx_trend', ['BULLISH']),
    ('Index momentum POSITIVE (>0)', 'idx_mom', ['+']),
    ('Relative volume > 1x', 'rvol', ['1.', '2.', '3.', '4.', '5.']),
    ('Entry ABOVE VWAP', 'vwap', ['ABOVE']),
    ('Option DOWN from day open', 'drop', ['-']),
    ('Volume trend RISING', 'vtrd', ['RISING']),
    ('Opening 5-min UP', 'open_dir', ['UP']),
]

print(f'  {"Feature":<40} {"WINS":>10} {"LOSSES":>10}')
print(f'  {"-"*62}')
for label, key, values in headers:
    w = pct_match(wins,   key, values)
    l = pct_match(losses, key, values)
    flag = '  <-- edge' if wins and losses else ''
    # highlight if win% - loss% > 30 points
    try:
        wp = int(w.split('(')[1].split('%')[0])
        lp = int(l.split('(')[1].split('%')[0])
        flag = '  <<< EDGE' if (wp - lp) > 30 else ('  !! TRAP' if (lp - wp) > 30 else '')
    except Exception:
        flag = ''
    print(f'  {label:<40} {w:>10} {l:>10} {flag}')

print()
print('  Interpretation guide:')
print('  <<< EDGE  = feature is significantly more common in WINNING trades')
print('  !! TRAP   = feature is more common in LOSING trades (avoid if present)')
print()
