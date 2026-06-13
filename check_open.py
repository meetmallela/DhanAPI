import mysql.connector
conn = mysql.connector.connect(host='127.0.0.1', user='root', password='Krishna@123', database='trading_live')
cur = conn.cursor()

# Any OPEN orders regardless of date
cur.execute("""
    SELECT order_id, tradingsymbol, strategy_name, action, entry_price, status, created_at
    FROM orders WHERE status='OPEN'
    ORDER BY created_at DESC LIMIT 10
""")
rows = cur.fetchall()
print(f"All currently OPEN orders: {len(rows)}")
for r in rows: print(r)

# Today's full summary
cur.execute("""
    SELECT status, COUNT(*) as cnt, SUM(pnl) as total_pnl
    FROM orders WHERE DATE(created_at)='2026-06-05'
    GROUP BY status
""")
print("\nToday's order status summary:")
for r in cur.fetchall(): print(r)

conn.close()
