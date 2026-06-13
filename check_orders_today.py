import sqlite3
from master_resource import MasterResource

conn = sqlite3.connect(MasterResource.get_trading_db_path())
conn.row_factory = sqlite3.Row

rows = conn.execute(
    "SELECT strategy_name, symbol, action, status, order_id, created_at "
    "FROM orders WHERE DATE(created_at) = '2026-05-05' ORDER BY created_at DESC LIMIT 20"
).fetchall()
if rows:
    for r in rows:
        ts = str(r["created_at"])[:19]
        sn = str(r["strategy_name"] or "")[:25]
        print(f"  {ts}  {sn:<25}  {r['symbol']:<15}  {r['action']}  {r['status']}  id={r['order_id']}")
else:
    print("  No orders for 2026-05-05")

rows2 = conn.execute(
    "SELECT strategy, COUNT(*) as cnt FROM strategy_signals "
    "WHERE DATE(ts) = '2026-05-05' GROUP BY strategy"
).fetchall()
print()
if rows2:
    print("strategy_signals for 2026-05-05:")
    for r in rows2:
        print(f"  {r['strategy']}: {r['cnt']} rows")
else:
    print("strategy_signals: 0 rows for 2026-05-05")
conn.close()
