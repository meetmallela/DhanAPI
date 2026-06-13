"""
build_proposed_signals_csv.py
------------------------------
Re-processes all signals from the last 14 days in MySQL and exports a
proposed_signals_to_orders.csv with columns:

  channel_name | received_at | raw_message | parsed_symbol | parsed_strike |
  parsed_option_type | parsed_entry | parsed_sl | parsed_target |
  parsed_expiry | resolved_security_id | resolved_exchange | resolved_tradingsymbol |
  resolved_lot_size | skip_reason | proposed_action

Run from DhanAPI directory:
    python build_proposed_signals_csv.py
"""

import sys, io, csv, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")

import mysql.connector
import pandas as pd
from datetime import date, datetime

# ── Connect to MySQL ──────────────────────────────────────────────────────────
conn = mysql.connector.connect(
    host="127.0.0.1", port=3306, user="root",
    password="Krishna@123", database="trading_live", autocommit=False
)
cur = conn.cursor()

print("Fetching signals from last 14 days...")
cur.execute("""
    SELECT id, channel_id, channel_name, message_id,
           raw_text, parsed_data, timestamp, processed, order_status
    FROM   signals
    WHERE  timestamp >= DATE_SUB(NOW(), INTERVAL 14 DAY)
      AND  channel_name IS NOT NULL
      AND  raw_text IS NOT NULL
    ORDER  BY timestamp ASC
""")
rows = cur.fetchall()
conn.close()
print(f"  {len(rows)} signals fetched")

# ── Load StrikeLookup (Dhan) + Kite CSV (no bridge) ──────────────────────────
from core.strike_lookup import StrikeLookup
from channel_parsers import parse_mcx_premium
sl = StrikeLookup()
_kite_df = pd.read_csv(
    r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\valid_instruments.csv",
    low_memory=False
)
_kite_df['expiry_date'] = pd.to_datetime(_kite_df['expiry_date'], errors='coerce').dt.date
_kite_df_fut = _kite_df[_kite_df['instrument_type'] == 'FUT'].copy()
_kite_df_opt = _kite_df[_kite_df['instrument_type'].isin(['CE','PE'])].copy()

today = date.today()

_MCX_LOT_SIZES = {
    "NATURALGAS": 1250, "NATURALGASM": 250,
    "CRUDEOIL": 100, "CRUDEOILM": 10,
    "GOLD": 100, "GOLDM": 10,
    "SILVER": 30000, "SILVERM": 5000,
    "COPPER": 2500, "ZINC": 5000, "LEAD": 5000, "NICKEL": 1500, "ALUMINIUM": 5000,
}

def _resolve(sym, strike, opt_type, expiry, inst_type):
    """Attempt Dhan then Kite resolution. Returns (security_id, tradingsymbol, exchange, lot_size) or None."""
    INDEX = {'NIFTY','BANKNIFTY','FINNIFTY','SENSEX','MIDCPNIFTY','BANKEX'}
    MCX   = {'COPPER','CRUDEOIL','CRUDEOILM','GOLD','GOLDM','SILVER','SILVERM',
              'NATURALGAS','ZINC','LEAD','NICKEL','ALUMINIUM'}

    if sym in MCX:
        # Resolve MCX from local Kite CSV + Dhan scrip (no live API needed)
        opt = option_type.upper().replace('CALL','CE').replace('PUT','PE') if option_type else ''
        if not strike or opt not in ('CE','PE'):
            return None
        exp_dt = datetime.strptime(expiry[:10], "%Y-%m-%d").date() if expiry else None
        mask = (
            (_kite_df['symbol'] == sym) &
            (_kite_df['option_type'] == opt) &
            (_kite_df['strike'] == float(strike)) &
            (_kite_df['exchange'] == 'MCX') &
            (_kite_df['expiry_date'] >= today)
        )
        cands = _kite_df[mask].copy()
        if cands.empty:
            return None
        if exp_dt is not None:
            cands['_diff'] = cands['expiry_date'].apply(lambda d: abs((d - exp_dt).days))
            row = cands.loc[cands['_diff'].idxmin()]
        else:
            row = cands.sort_values('expiry_date').iloc[0]
        ts = str(row['tradingsymbol'])
        dr = sl.get_by_trading_symbol(ts)
        sid  = dr['security_id'] if dr else f"MCX_{ts.replace('-','_')}"
        lots = _MCX_LOT_SIZES.get(sym, int(row.get('lot_size', 1)))
        return sid, ts, 'MCX', lots

    if inst_type == 'FUTURES':
        INDEX_F = {
            "NIFTY":{"security_id":"13","exchange_segment":"NSE_FNO","lot_size":75},
            "BANKNIFTY":{"security_id":"25","exchange_segment":"NSE_FNO","lot_size":35},
            "SENSEX":{"security_id":"51","exchange_segment":"BSE_FNO","lot_size":20},
        }
        if sym in INDEX_F:
            r = INDEX_F[sym]
            return r['security_id'], f"{sym} FUT", r['exchange_segment'], r['lot_size']
        r = sl.get_stock_future(sym, expiry)
        if r:
            return r['security_id'], r['trading_symbol'], r['exchange_segment'], r['lot_size']
        return None

    if not strike or not opt_type:
        return None
    opt = opt_type.upper().replace('CALL','CE').replace('PUT','PE')

    # Dhan OPTIDX (index options)
    if sym in INDEX:
        exp_str = expiry[:10] if expiry else sl.get_nearest_expiry(sym)
        r = sl.get_atm_option(sym, float(strike), opt, exp_str, itm_shift=False)
        if r:
            return r['security_id'], r['trading_symbol'], r['exchange_segment'], r['lot_size']

    # Dhan OPTSTK (stock options)
    exp_str = expiry[:10] if expiry else sl.get_nearest_stock_expiry(sym)
    r = sl.get_stock_option(sym, float(strike), opt, exp_str)
    if r:
        return r['security_id'], r['trading_symbol'], r['exchange_segment'], r['lot_size']

    # Kite CSV fallback
    exp_dt = datetime.strptime(exp_str, "%Y-%m-%d").date() if exp_str else None
    mask = (_kite_df_opt['symbol']==sym) & (_kite_df_opt['option_type']==opt) & \
           (_kite_df_opt['strike']==float(strike)) & (_kite_df_opt['expiry_date']>=today)
    kc = _kite_df_opt[mask].sort_values('expiry_date')
    if not kc.empty:
        row  = kc.iloc[0]
        ts   = row['tradingsymbol']
        dr   = sl.get_by_trading_symbol(ts)
        sid  = dr['security_id'] if dr else f"KITE_{ts}"
        exch = dr['exchange_segment'] if dr else \
               {'NFO':'NSE_FNO','BFO':'BSE_FNO'}.get(row['exchange'],'NSE_FNO')
        lot  = int(row['lot_size'])
        return sid, ts, exch, lot

    return None

# ── Build output rows ─────────────────────────────────────────────────────────
out_rows = []
stats = {"total": 0, "resolved": 0, "skip": 0, "no_parse": 0}

for (sig_id, ch_id, ch_name, msg_id,
     raw_text, parsed_json, ts, processed, order_status) in rows:

    stats["total"] += 1
    raw = (raw_text or "").replace('\n', ' | ').strip()

    parsed = None

    # For MCX PREMIUM: always re-parse raw_text with the new dedicated parser
    # (old parsed_data in DB has wrong symbol = 'MAY'/'JUN' due to old parser)
    if ch_name and 'MCX PREMIUM' in ch_name.upper():
        parsed = parse_mcx_premium(raw_text or "")

    if parsed is None and parsed_json:
        try:
            parsed = json.loads(parsed_json)
        except Exception:
            pass

    if not parsed:
        stats["no_parse"] += 1
        out_rows.append({
            "channel_name":   ch_name, "received_at": str(ts), "raw_message": raw[:300],
            "parsed_symbol":  "", "parsed_strike": "", "parsed_option_type": "",
            "parsed_entry":   "", "parsed_sl": "", "parsed_target": "",
            "parsed_expiry":  "", "parsed_instrument_type": "",
            "resolved_security_id": "", "resolved_exchange": "",
            "resolved_tradingsymbol": "", "resolved_lot_size": "",
            "skip_reason": "NO_PARSE", "proposed_action": "SKIP",
            "original_processed": processed, "original_status": order_status or "",
        })
        continue

    sym      = parsed.get('symbol','')
    strike   = parsed.get('strike')
    opt      = (parsed.get('option_type') or '').upper()
    entry    = parsed.get('entry_price')
    sl_price = parsed.get('stop_loss')
    target   = parsed.get('target')
    expiry   = parsed.get('expiry_date','')
    itype    = parsed.get('instrument_type','OPTIONS')

    # Try resolution
    resolved = None
    skip_reason = ""
    if sym:
        try:
            resolved = _resolve(sym, strike, opt, expiry, itype)
        except Exception as e:
            skip_reason = f"RESOLVE_ERROR: {e}"

    if resolved:
        stats["resolved"] += 1
        sec_id, ts_sym, exch, lot = resolved
        action = "PLACE" if (entry and sl_price) else "PLACE_NO_SL"
    else:
        stats["skip"] += 1
        sec_id = ts_sym = exch = lot = ""
        action = "SKIP"
        if not skip_reason:
            skip_reason = order_status or "UNRESOLVABLE"

    out_rows.append({
        "channel_name":   ch_name,
        "received_at":    str(ts),
        "raw_message":    raw[:400],
        "parsed_symbol":  sym,
        "parsed_strike":  strike or "",
        "parsed_option_type": opt,
        "parsed_entry":   entry or "",
        "parsed_sl":      sl_price or "",
        "parsed_target":  target or "",
        "parsed_expiry":  expiry,
        "parsed_instrument_type": itype,
        "resolved_security_id":   sec_id,
        "resolved_exchange":      exch,
        "resolved_tradingsymbol": ts_sym,
        "resolved_lot_size":      lot,
        "skip_reason":    skip_reason,
        "proposed_action": action,
        "original_processed": processed,
        "original_status": order_status or "",
    })

# ── Write CSV ─────────────────────────────────────────────────────────────────
out_path = r"C:\Users\meetm\OneDrive\Desktop\proposed_signals_to_orders.csv"
fields = [
    "channel_name","received_at","raw_message",
    "parsed_symbol","parsed_strike","parsed_option_type",
    "parsed_entry","parsed_sl","parsed_target","parsed_expiry","parsed_instrument_type",
    "resolved_security_id","resolved_exchange","resolved_tradingsymbol","resolved_lot_size",
    "skip_reason","proposed_action","original_processed","original_status",
]
with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
    w = csv.DictWriter(f, fieldnames=fields)
    w.writeheader()
    w.writerows(out_rows)

print(f"\nDone.")
print(f"  Total signals:  {stats['total']}")
print(f"  Resolved:       {stats['resolved']} ({stats['resolved']/stats['total']*100:.1f}%)")
print(f"  Unresolvable:   {stats['skip']}")
print(f"  No parse data:  {stats['no_parse']}")
print(f"\nCSV saved: {out_path}")

# Quick breakdown by channel
df = pd.DataFrame(out_rows)
print("\n=== By channel ===")
grp = df.groupby('channel_name').agg(
    total=('proposed_action','count'),
    place=('proposed_action', lambda x: (x=='PLACE').sum()),
    skip=('proposed_action', lambda x: (x=='SKIP').sum()),
).sort_values('total', ascending=False)
print(grp.to_string())
