import sqlite3, sys, io
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from master_resource import MasterResource

db    = MasterResource.get_trading_db_path()
today = "2026-05-15"
con   = sqlite3.connect(db)
con.row_factory = sqlite3.Row

# ── WhatIf breakdown: signal_id < 100000 = TG signal, >= 100000 = OmniEngine order
wi       = con.execute("SELECT signal_id, channel_name, exit_reason, result, pnl_total FROM whatif_trades WHERE run_date=? ORDER BY signal_id", (today,)).fetchall()
tg_wi    = [r for r in wi if r["signal_id"] < 100000]
omni_wi  = [r for r in wi if r["signal_id"] >= 100000]

print(f"WhatIf total : {len(wi)}")
print(f"  TG signals  (signal_id < 100000) : {len(tg_wi)}")
print(f"  OmniEngine orders (>= 100000)    : {len(omni_wi)}")
print()

print("TG signals in WhatIf by channel:")
tg_channels = {}
for r in tg_wi:
    tg_channels.setdefault(r["channel_name"], []).append(r)
for ch, rows in sorted(tg_channels.items(), key=lambda x: -len(x[1])):
    pnl = sum(r["pnl_total"] or 0 for r in rows)
    print(f"  {(ch or '?'):<35} count={len(rows)}  pnl={pnl:+.2f}")
print()

print("OmniEngine strategies in WhatIf:")
omni_strats = {}
for r in omni_wi:
    omni_strats.setdefault(r["channel_name"], []).append(r)
for s, rows in sorted(omni_strats.items(), key=lambda x: -len(x[1])):
    pnl = sum(r["pnl_total"] or 0 for r in rows)
    print(f"  {(s or '?'):<24} count={len(rows)}  pnl={pnl:+.2f}")
print()

# ── Paper trades breakdown
paper      = con.execute("SELECT strategy_name, channel_name FROM orders WHERE date(created_at)=?", (today,)).fetchall()
tg_paper   = [r for r in paper if (r["strategy_name"] or "").startswith("TG:") or (r["channel_name"] or "").startswith("TG:")]
omni_paper = [r for r in paper if not ((r["strategy_name"] or "").startswith("TG:") or (r["channel_name"] or "").startswith("TG:"))]

print(f"Paper trades total: {len(paper)}")
print(f"  TG-sourced paper trades          : {len(tg_paper)}")
print(f"  OmniEngine strategy paper trades : {len(omni_paper)}")
print()

# ── All TG signals received today (signals table)
sigs = con.execute("SELECT channel_name, COUNT(*) as n FROM signals WHERE date(timestamp)=? GROUP BY channel_name ORDER BY n DESC", (today,)).fetchall()
print("All TG signals received today (signals table):")
for r in sigs:
    print(f"  {(r['channel_name'] or '?'):<35} received={r['n']}")

con.close()
