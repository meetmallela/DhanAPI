"""Live trading session tracker — today's signals, orders, P&L."""
import io, sys, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
import mysql.connector
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
now = datetime.now(IST).strftime("%H:%M:%S")

conn = mysql.connector.connect(host='127.0.0.1', port=3306, user='root',
    password='Krishna@123', database='trading_live')
cur = conn.cursor()

print(f"{'='*70}")
print(f"  TRADING SESSION SNAPSHOT  —  {datetime.now(IST).strftime('%d-%b-%Y %H:%M IST')}")
print(f"{'='*70}")

# 1. Today's signals summary
cur.execute("""
    SELECT COUNT(*) total,
           SUM(processed=1)  placed,
           SUM(processed=-1) failed,
           SUM(processed=0)  pending
    FROM signals WHERE DATE(timestamp) = CURDATE()
""")
t, pl, fa, pe = cur.fetchone()
print(f"\n SIGNALS TODAY:  {t} received  |  {pl or 0} placed  |  {fa or 0} failed  |  {pe or 0} pending")

# 2. Failure reasons
cur.execute("""
    SELECT order_status, COUNT(*) cnt
    FROM signals
    WHERE DATE(timestamp) = CURDATE() AND processed = -1
    GROUP BY order_status ORDER BY cnt DESC LIMIT 8
""")
rows = cur.fetchall()
if rows:
    print("\n FAILURE REASONS:")
    for reason, cnt in rows:
        print(f"   {cnt:>3}x  {(reason or '')[:70]}")

# 3. Today's orders (placed trades)
cur.execute("""
    SELECT order_id, symbol, tradingsymbol, action, entry_price,
           stop_loss, target, status, pnl, ltp, channel_name,
           'INTRADAY' pos_type,
           created_at
    FROM orders
    WHERE DATE(created_at) = CURDATE()
    ORDER BY created_at DESC
""")
orders = cur.fetchall()
print(f"\n ORDERS TODAY:  {len(orders)} paper trades placed")
if orders:
    print(f"\n {'Order ID':<14} {'Symbol':<28} {'Entry':>6} {'SL':>6} {'LTP':>7} {'PnL':>8}  {'Status':<8}  Channel")
    print(f" {'-'*115}")
    total_pnl = 0
    for oid, sym, ts, act, ep, sl, tgt, status, pnl, ltp, ch, ptype, cat in orders:
        pnl_val = pnl or 0
        total_pnl += pnl_val
        pnl_str = f"₹{pnl_val:+.0f}" if pnl_val else "open"
        ltp_str = f"{ltp:.1f}" if ltp else "—"
        ts_disp = (ts or sym or '')[:27]
        ch_disp = (ch or '')[:22]
        ptype_tag = f"[{ptype[:2]}]" if ptype != 'INTRADAY' else ""
        print(f" {oid:<14} {ts_disp:<28} {ep or 0:>6.1f} {sl or 0:>6.1f} {ltp_str:>7} {pnl_str:>8}  {status:<8}  {ch_disp} {ptype_tag}")
    print(f"\n {'TOTAL P&L':>65}  ₹{total_pnl:+.0f}")

# 4. Active open positions
cur.execute("""
    SELECT order_id, tradingsymbol, entry_price, stop_loss, ltp, pnl, channel_name
    FROM orders
    WHERE status = 'OPEN'
    ORDER BY created_at DESC
""")
open_pos = cur.fetchall()
print(f"\n OPEN POSITIONS:  {len(open_pos)}")
if open_pos:
    for oid, ts, ep, sl, ltp, pnl, ch in open_pos:
        ltp_str = f"{ltp:.1f}" if ltp else "waiting"
        pnl_str = f"₹{pnl:+.0f}" if pnl else "—"
        print(f"   {oid:<14} {(ts or ''):<30} entry={ep or 0:.1f}  sl={sl or 0:.1f}  ltp={ltp_str}  pnl={pnl_str}  {(ch or '')[:20]}")

# 5. Channels active today
cur.execute("""
    SELECT channel_name, COUNT(*) signals,
           SUM(processed=1) placed, SUM(processed=-1) failed
    FROM signals WHERE DATE(timestamp) = CURDATE()
    GROUP BY channel_name ORDER BY signals DESC
""")
ch_rows = cur.fetchall()
if ch_rows:
    print(f"\n CHANNELS ACTIVE TODAY:")
    print(f"   {'Channel':<42} {'Sig':>4} {'Placed':>7} {'Failed':>7}")
    print(f"   {'-'*65}")
    for ch, sig, pl2, fa2 in ch_rows:
        print(f"   {(ch or 'UNKNOWN')[:42]:<42} {sig:>4} {pl2 or 0:>7} {fa2 or 0:>7}")

conn.close()
