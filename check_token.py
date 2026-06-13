"""
check_token.py
--------------
Verifies that the Dhan API token in .env is valid and returns fund data.
Run: C:\ProgramData\anaconda3\python.exe check_token.py
"""
from core.dhan_client import DhanClient

c = DhanClient()
r = c.dhan.get_fund_limits()

if r.get("status") == "success":
    d = r["data"]
    print("OK  Token is valid")
    print("    Available Balance :", d.get("availabelBalance"))
    print("    SOD Limit         :", d.get("sodLimit"))
    print("    Used Margin       :", d.get("utilizedAmount"))
else:
    print("FAILED  Token check failed")
    print("        Response:", r.get("remarks"))
    print()
    print("Fix: open .env and set DHAN_CLIENT_ID to the ID shown at developer.dhan.co")
