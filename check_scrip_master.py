"""
check_scrip_master.py
---------------------
Checks whether the Dhan scrip master CSV is present and fresh.
Run: C:\ProgramData\anaconda3\python.exe check_scrip_master.py
"""
from pathlib import Path
from datetime import datetime
from master_resource import MasterResource

p = Path(MasterResource.MASTER_ROOT) / "data" / "dhan_scrip_master.csv"

if p.exists():
    age_hours = (datetime.now().timestamp() - p.stat().st_mtime) / 3600
    size_mb   = p.stat().st_size / 1024 / 1024
    print(f"OK  Scrip master found")
    print(f"    Size    : {size_mb:.1f} MB")
    print(f"    Age     : {age_hours:.1f} hours old")
    if age_hours > 24:
        print("    WARNING : File is older than 24 hours.")
        print("              It will auto-refresh when the engine or SL monitor starts.")
    else:
        print("    Status  : Fresh (within 24 hours)")
else:
    print("WARNING  Scrip master not found at:")
    print(f"         {p}")
    print()
    print("         It will be downloaded automatically when the engine starts.")
    print("         Make sure you have an internet connection.")
