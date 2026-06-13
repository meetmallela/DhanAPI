"""
VIP RJ Channel — Signal→Trade→P&L timeline analysis
"""
import sqlite3
import json

DB = r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\trading.db'
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

# ── 1. All signals from VIP RJ ────────────────────────────────────────────────
sigs = con.execute("""
    SELECT id, channel_name, timestamp, raw_text, parsed_data, processed
    FROM signals
    WHERE channel_name LIKE '%VIP RJ%'
    ORDER BY timestamp ASC
""").fetchall()

print(f"\n{'='*70}")
print(f"  VIP RJ SIGNALS  ({len(sigs)} total)")
print(f"{'='*70}")
for r in sigs:
    pd = r['parsed_data'] or '{}'
    try:
        pd = json.loads(pd)
    except Exception:
        pd = {}
    raw = str(r['raw_text'] or '')[:180]
    print(f"\n  SIG [{r['id']}] {str(r['timestamp'])[:19]}  processed={r['processed']}")
    print(f"       action={pd.get('action','?')}  symbol={pd.get('symbol','?')}  "
          f"ep={pd.get('entry_price','?')}  sl={pd.get('stop_loss','?')}  tgt={pd.get('target','?')}")
    print(f"       raw: {raw}")

# ── 2. Orders placed from VIP RJ signals ──────────────────────────────────────
print(f"\n\n{'='*70}")
print("  VIP RJ ORDERS  (actual paper trades)")
print(f"{'='*70}")

orders = con.execute("""
    SELECT o.id as oid, o.order_id, o.signal_id, o.tradingsymbol, o.symbol,
           o.action, o.quantity, o.entry_price, o.actual_entry_price,
           o.stop_loss, o.target, o.status, o.exit_price, o.pnl,
           o.entry_placed_at, o.updated_at, o.channel_name, o.strategy_name,
           o.sl_stage,
           s.timestamp as sig_ts, s.raw_text
    FROM orders o
    LEFT JOIN signals s ON o.signal_id = s.id
    WHERE o.channel_name LIKE '%VIP RJ%'
       OR s.channel_name LIKE '%VIP RJ%'
    ORDER BY COALESCE(o.entry_placed_at, s.timestamp) ASC
""").fetchall()

print(f"\n  {len(orders)} orders\n")
total_pnl = 0.0
wins = losses = skipped = 0
for o in orders:
    pnl  = o['pnl'] or 0.0
    ep   = o['actual_entry_price'] or o['entry_price'] or 0
    sl   = o['stop_loss'] or 0
    tgt  = o['target'] or 0
    xp   = o['exit_price'] or 0
    raw  = str(o['raw_text'] or '')[:130]
    status = str(o['status'] or '')
    ts_sig = str(o['sig_ts'] or '')[:19]
    ts_in  = str(o['entry_placed_at'] or '')[:19]
    ts_out = str(o['updated_at'] or '')[:19]

    if status in ('REJECTED', 'CANCELLED'):
        skipped += 1
        result_tag = f"[SKIP/{status}]"
    elif pnl > 0:
        wins += 1
        total_pnl += pnl
        result_tag = f"[WIN  +{pnl:.0f}]"
    elif pnl < 0:
        losses += 1
        total_pnl += pnl
        result_tag = f"[LOSS {pnl:.0f}]"
    else:
        result_tag = f"[OPEN/0]"

    print(f"  {result_tag}  {o['tradingsymbol'] or o['symbol']}  {o['action']}")
    print(f"    Signal : {ts_sig}  raw: {raw}")
    print(f"    Entry  : {ts_in}  @{ep}  SL={sl}  TGT={tgt}")
    print(f"    Exit   : {ts_out}  @{xp}  stage={o['sl_stage']}  status={status}")
    print()

print(f"  SUMMARY: {wins} wins | {losses} losses | {skipped} skipped/rejected")
print(f"  TOTAL P&L (orders table): {total_pnl:+.0f}")

# ── 3. WhatIf (simulated) for VIP RJ ─────────────────────────────────────────
print(f"\n\n{'='*70}")
print("  VIP RJ WHATIF (EOD simulation — what WOULD have happened)")
print(f"{'='*70}")

whatif = con.execute("""
    SELECT wt.id, wt.run_date, wt.signal_id, wt.tradingsymbol, wt.symbol,
           wt.channel_name, wt.action, wt.entry_time, wt.entry_price,
           wt.sl_initial, wt.exit_time, wt.exit_price, wt.exit_reason,
           wt.pnl_total, wt.pnl_pct, wt.result, wt.lot_size,
           s.raw_text
    FROM whatif_trades wt
    LEFT JOIN signals s ON wt.signal_id = s.id
    WHERE wt.channel_name LIKE '%VIP RJ%'
       OR s.channel_name LIKE '%VIP RJ%'
    ORDER BY wt.run_date ASC, wt.entry_time ASC
""").fetchall()

print(f"\n  {len(whatif)} simulated trades\n")
wi_pnl = 0.0
wi_wins = wi_losses = 0
for w in whatif:
    pnl = w['pnl_total'] or 0.0
    wi_pnl += pnl
    tag = "[WIN]" if (w['result'] or '') == 'PROFIT' else "[LOSS]"
    if (w['result'] or '') == 'PROFIT':
        wi_wins += 1
    else:
        wi_losses += 1
    raw = str(w['raw_text'] or '')[:120]
    print(f"  {tag} {w['run_date']}  {w['tradingsymbol'] or w['symbol']}  {w['action']}")
    print(f"       entry={str(w['entry_time'])[:19]} @{w['entry_price']}  sl={w['sl_initial']}")
    print(f"       exit ={str(w['exit_time'])[:19]} @{w['exit_price']}  reason={w['exit_reason']}")
    pnl_pct = w['pnl_pct'] or 0.0
    print(f"       pnl={pnl:+.0f}  ({pnl_pct:+.1f}%)  lots={w['lot_size']}")
    print(f"       raw: {raw}")
    print()

print(f"  WHATIF SUMMARY: {wi_wins} wins | {wi_losses} losses")
print(f"  WHATIF TOTAL P&L: {wi_pnl:+.0f}")

# ── 4. Signal parsing failures ────────────────────────────────────────────────
print(f"\n\n{'='*70}")
print("  VIP RJ — SIGNALS WITH NO ORDER PLACED (parsing failures / skipped)")
print(f"{'='*70}")
placed_sids = set(o['signal_id'] for o in orders if o['signal_id'])
unmatched = [r for r in sigs if r['id'] not in placed_sids]
print(f"\n  {len(unmatched)} signals had no order placed:\n")
for r in unmatched:
    pd = r['parsed_data'] or '{}'
    try:
        pd = json.loads(pd)
    except Exception:
        pd = {}
    print(f"  SIG [{r['id']}] {str(r['timestamp'])[:19]}  processed={r['processed']}")
    print(f"       parsed → {pd}")
    print(f"       raw: {str(r['raw_text'] or '')[:200]}")
    print()

con.close()
