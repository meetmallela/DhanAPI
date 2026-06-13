import mysql.connector

conn = mysql.connector.connect(host='127.0.0.1', port=3306, user='root',
                               password='Krishna@123', database='trading_live')
cur = conn.cursor()

# Jun 10 anomaly check
print("=== JUN 10 HIGH PNL — TOP 10 BY ABS(PNL) ===")
cur.execute("""
    SELECT symbol, strategy_name, channel_name, action,
           entry_price, exit_price, pnl, status, created_at
    FROM orders
    WHERE DATE(created_at) = '2026-06-10'
    ORDER BY ABS(pnl) DESC
    LIMIT 10
""")
for r in cur.fetchall():
    src = r[1] or r[2] or "?"
    print(f"  {str(r[8])[:16]}  {src:20}  {r[0]:15}  {r[3]}  entry={r[4]}  exit={r[5]}  PnL={r[6]}  {r[7]}")

print()
print("=== TG SIGNALS vs BOT TRADES — last 10 days (closed only) ===")
cur.execute("""
    SELECT DATE(created_at) as day,
           COALESCE(channel_name, strategy_name, 'BOT') as source,
           COUNT(*) as cnt,
           ROUND(SUM(pnl),1) as total_pnl,
           SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as win,
           SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as loss
    FROM orders
    WHERE created_at >= DATE_SUB(NOW(), INTERVAL 10 DAY)
      AND status IN ('CLOSED','SL_HIT','TARGET_HIT','EXITED','COMPLETED')
    GROUP BY DATE(created_at), source
    ORDER BY day DESC, total_pnl DESC
""")
for r in cur.fetchall():
    print(f"  {r[0]}  {str(r[1]):25}  trades={r[2]}  PnL=Rs {r[3]:10}  W={r[4]} L={r[5]}")

print()
print("=== EOD P&L SUMMARY (all sources combined, last 14 days) ===")
cur.execute("""
    SELECT DATE(created_at) as day,
           COUNT(*) as trades,
           ROUND(SUM(pnl),1) as total_pnl
    FROM orders
    WHERE created_at >= DATE_SUB(NOW(), INTERVAL 14 DAY)
      AND status IN ('CLOSED','SL_HIT','TARGET_HIT','EXITED','COMPLETED')
    GROUP BY DATE(created_at)
    ORDER BY day DESC
""")
for r in cur.fetchall():
    sign = "+" if (r[2] or 0) >= 0 else ""
    print(f"  {r[0]}  trades={r[1]:3}  PnL=Rs {sign}{r[2]}")

print()
print("=== TODAY OPEN POSITIONS ===")
cur.execute("""
    SELECT symbol, COALESCE(strategy_name, channel_name, 'BOT') as src,
           action, entry_price, ltp, pnl, status, created_at
    FROM orders
    WHERE status NOT IN ('CLOSED','SL_HIT','TARGET_HIT','EXITED','COMPLETED','CANCELLED')
    ORDER BY created_at DESC
    LIMIT 20
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(f"  {str(r[7])[:16]}  {str(r[1]):20}  {r[0]:15}  {r[2]}  entry={r[3]}  ltp={r[4]}  PnL={r[5]}  {r[6]}")
else:
    print("  No open positions")

conn.close()
