import sqlite3, io, sys
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
db = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\kite_candles.db"
con = sqlite3.connect(db)

run_date = "2026-05-15"

# Test each pattern against NIFTY 23800 CE
for p in ["NIFTY-%-23800-CE", "NIFTY_23800_CE_%"]:
    rows = con.execute("SELECT tradingsymbol, dt FROM candles_1min WHERE tradingsymbol LIKE ? LIMIT 3", (p,)).fetchall()
    print(f"Pattern '{p}' -> {rows}")

# Show what dt values look like
sample = con.execute("SELECT dt FROM candles_1min LIMIT 3").fetchall()
print("Sample dt values:", sample)

# Try without date filter
rows2 = con.execute("SELECT tradingsymbol, COUNT(*) FROM candles_1min WHERE tradingsymbol LIKE 'NIFTY-%-23800-CE' GROUP BY tradingsymbol").fetchall()
print("NIFTY-%-23800-CE (no date filter):", rows2)

# Try with date filter
rows3 = con.execute(f"SELECT tradingsymbol, COUNT(*) FROM candles_1min WHERE tradingsymbol LIKE 'NIFTY-%-23800-CE' AND date(dt)='{run_date}' GROUP BY tradingsymbol").fetchall()
print(f"NIFTY-%-23800-CE date={run_date}:", rows3)

# Check what dates exist for this pattern
rows4 = con.execute("SELECT date(dt), COUNT(*) FROM candles_1min WHERE tradingsymbol LIKE 'NIFTY-%-23800-CE' GROUP BY date(dt) ORDER BY date(dt) DESC LIMIT 5").fetchall()
print("Dates available for NIFTY-%-23800-CE:", rows4)

con.close()
