"""
check_strike_lookup.py
----------------------
Verifies that the StrikeLookup can resolve ATM options for major indices.
Run: C:\ProgramData\anaconda3\python.exe check_strike_lookup.py
"""
import logging
logging.disable(logging.CRITICAL)  # suppress download progress during check

from core.strike_lookup import StrikeLookup

lu = StrikeLookup()

# Use a round number — actual spot doesn't matter for a connectivity check
test_cases = [
    ("NIFTY",     22300, "CE"),
    ("NIFTY",     22300, "PE"),
    ("BANKNIFTY", 50000, "CE"),
    ("FINNIFTY",  23000, "CE"),
]

all_ok = True
for symbol, spot, opt_type in test_cases:
    res = lu.get_atm_option(symbol, spot, opt_type)
    if res:
        print(
            f"OK  {symbol:<12} {opt_type}  "
            f"strike={res['strike']:6}  expiry={res['expiry_date']}  "
            f"sec_id={res['security_id']:<8}  {res['trading_symbol']}"
        )
    else:
        print(f"FAILED  {symbol} {opt_type} at spot={spot} — no result found")
        all_ok = False

print()
if all_ok:
    print("All strike lookups passed. Options pipeline is ready.")
else:
    print("Some lookups failed. Check internet connection or scrip master file.")
    print("Re-run after running: C:\\ProgramData\\anaconda3\\python.exe check_scrip_master.py")
