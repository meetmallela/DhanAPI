# -*- coding: utf-8 -*-
import sys, io, mysql.connector, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

conn = mysql.connector.connect(host='127.0.0.1', user='root', password='Krishna@123', database='trading_live')
cur = conn.cursor()

print("=== June 5 signals — channel, parsed status, raw text preview ===\n")
cur.execute("""
    SELECT channel_id, channel_name, timestamp, processed, parsed_data,
           SUBSTRING(raw_text, 1, 120) as preview
    FROM signals
    WHERE timestamp >= '2026-06-05 00:00:00'
      AND timestamp <= '2026-06-05 23:59:59'
      AND channel_id IS NOT NULL
    ORDER BY timestamp
""")
rows = cur.fetchall()
for r in rows:
    cid   = r[0]
    cname = str(r[1]).encode('ascii','replace').decode()[:35]
    ts    = str(r[2])[:16]
    proc  = r[3]
    try:
        pd = json.loads(r[4]) if r[4] else {}
        action = pd.get('action','?') if isinstance(pd, dict) else '?'
        symbol = pd.get('tradingsymbol', pd.get('symbol','?')) if isinstance(pd, dict) else '?'
        parsed_ok = f"OK → {action} {symbol}"
    except Exception:
        parsed_ok = f"RAW/UNPARSED"
    preview = str(r[5]).encode('ascii','replace').decode()[:80].replace('\n',' ')
    print(f"  [{cid}] {cname:36s} {ts}  proc={proc}  {parsed_ok}")
    print(f"    msg: {preview}")
    print()

conn.close()
