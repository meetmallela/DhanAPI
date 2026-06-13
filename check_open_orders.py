"""
check_open_orders.py
--------------------
Checks for leftover OPEN orders in the trading DB from the previous session.
Run: C:\ProgramData\anaconda3\python.exe check_open_orders.py
"""
import sqlite3
from master_resource import MasterResource

db_path = MasterResource.get_trading_db_path()
conn    = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur     = conn.cursor()

cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'OPEN'")
n = cur.fetchone()[0]

if n > 0:
    print(f"WARNING  {n} OPEN order(s) from a previous session found in the DB.")
    print()
    print("         These will be picked up by the SL Monitor when it starts.")
    print("         If they are stale, close them manually on the Dhan portal")
    print("         or update their status in the DB:")
    print()
    print("         UPDATE orders SET status='CLOSED' WHERE status='OPEN';")
    print()

    cur.execute("""
        SELECT order_id, symbol, action, entry_price, created_at
        FROM   orders
        WHERE  status = 'OPEN'
        ORDER  BY created_at DESC
    """)
    rows = cur.fetchall()
    print(f"         {'order_id':<20} {'symbol':<15} {'action':<6} {'entry_price':>12}  created_at")
    print(f"         {'-'*20} {'-'*15} {'-'*6} {'-'*12}  {'-'*20}")
    for row in rows:
        print(
            f"         {str(row['order_id']):<20} {str(row['symbol']):<15} "
            f"{str(row['action']):<6} {str(row['entry_price']):>12}  {row['created_at']}"
        )
else:
    print("OK  No leftover open orders. Clean start.")

conn.close()
