import sqlite3, io, sys
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
db = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"
con = sqlite3.connect(db)
rows = con.execute("SELECT DISTINCT tradingsymbol FROM candles_1min WHERE date(dt)='2026-05-15' ORDER BY tradingsymbol LIMIT 30").fetchall()
for r in rows:
    print(r[0])
print()
print("Total distinct symbols today:", con.execute("SELECT COUNT(DISTINCT tradingsymbol) FROM candles_1min WHERE date(dt)='2026-05-15'").fetchone()[0])
print("NIFTY pattern test:", con.execute("SELECT tradingsymbol FROM candles_1min WHERE tradingsymbol LIKE 'NIFTY_23800_CE_%' AND date(dt)='2026-05-15' LIMIT 3").fetchall())
con.close()
