"""
compare_whatif_paper.py
-----------------------
Diagnostic: compare today's WhatIf results vs Paper trades.
Shows why the P&L numbers differ.

Run: python compare_whatif_paper.py [YYYY-MM-DD]
"""
import io
import sqlite3
import sys
from datetime import date

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\DhanAPI")
from master_resource import MasterResource

DB_PATH  = MasterResource.get_trading_db_path()
run_date = sys.argv[1] if len(sys.argv) > 1 else date.today().strftime("%Y-%m-%d")

con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row

print(f"\n{'='*70}")
print(f"  PAPER vs WHAT-IF COMPARISON  —  {run_date}")
print(f"{'='*70}")

# ── 1. Paper trades ────────────────────────────────────────────────────────
paper_rows = con.execute("""
    SELECT strategy_name, tradingsymbol, action, entry_price,
           actual_entry_price, quantity, pnl, status, created_at,
           exit_price, sl_stage, peak_price, stop_loss
    FROM orders
    WHERE date(created_at) = ?
    ORDER BY created_at
""", (run_date,)).fetchall()

paper_pnl   = sum((r["pnl"] or 0) for r in paper_rows)
paper_capital = sum((r["actual_entry_price"] or r["entry_price"] or 0) * (r["quantity"] or 0)
                    for r in paper_rows)

print(f"\n[PAPER TRADES]  count={len(paper_rows)}  PnL={paper_pnl:+.2f}  "
      f"Capital deployed=Rs {paper_capital:,.0f}")
print(f"  {'Strategy':<24} {'Symbol':<34} {'Qty':>4} {'EP':>8} {'ActEP':>8} "
      f"{'Capital':>10} {'PnL':>9} {'Exit'}")
print(f"  {'-'*24} {'-'*34} {'-'*4} {'-'*8} {'-'*8} {'-'*10} {'-'*9} {'-'*16}")
for r in paper_rows:
    ep      = r["actual_entry_price"] or r["entry_price"] or 0
    capital = ep * (r["quantity"] or 0)
    exit_note = f"sl_stage={r['sl_stage']}" if r["sl_stage"] else r["status"] or "-"
    print(f"  {(r['strategy_name'] or ''):<24} {(r['tradingsymbol'] or ''):<34} "
          f"{r['quantity']:>4} {r['entry_price']:>8.2f} {ep:>8.2f} "
          f"{capital:>10,.0f} {(r['pnl'] or 0):>+9.2f} {exit_note}")

# ── 2. WhatIf ─────────────────────────────────────────────────────────────
wi_rows = con.execute("""
    SELECT channel_name, tradingsymbol, action, entry_price, lot_size,
           pnl_per_unit, pnl_total, result, exit_reason, data_quality
    FROM whatif_trades
    WHERE run_date = ?
    ORDER BY id
""", (run_date,)).fetchall()

wi_pnl     = sum((r["pnl_total"] or 0) for r in wi_rows)
wi_capital  = sum((r["entry_price"] or 0) * (r["lot_size"] or 0) for r in wi_rows)

print(f"\n[WHAT-IF]  count={len(wi_rows)}  PnL={wi_pnl:+.2f}  "
      f"Capital (hypothetical)=Rs {wi_capital:,.0f}")
print(f"  {'Strategy':<24} {'Symbol':<34} {'Lot':>4} {'EP':>8} {'PnL/u':>8} "
      f"{'LotPnL':>9} {'Result':<10} {'Exit':<14} DQ")
print(f"  {'-'*24} {'-'*34} {'-'*4} {'-'*8} {'-'*8} {'-'*9} {'-'*10} {'-'*14} {'-'*8}")
for r in wi_rows:
    lot  = r["lot_size"] or 0
    ep2  = r["entry_price"] or 0
    dq   = r["data_quality"] or ""
    print(f"  {(r['channel_name'] or ''):<24} {(r['tradingsymbol'] or ''):<34} "
          f"{lot:>4} {ep2:>8.2f} {(r['pnl_per_unit'] or 0):>+8.2f} "
          f"{(r['pnl_total'] or 0):>+9.2f} {(r['result'] or ''):<10} "
          f"{(r['exit_reason'] or ''):<14} {dq}")

# ── 3. Gap decomposition ───────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  GAP DECOMPOSITION")
print(f"{'='*70}")

paper_strats = {r["strategy_name"]: r for r in paper_rows}
wi_strats    = {}
for r in wi_rows:
    ch = r["channel_name"]
    wi_strats.setdefault(ch, []).append(r)

# 3a. Strategies executed in paper but NOT in WhatIf
paper_only = [s for s in paper_strats if s not in wi_strats]
if paper_only:
    print(f"\n  [A] In paper but NOT in WhatIf ({len(paper_only)} strats):")
    pnl_a = sum((paper_strats[s]["pnl"] or 0) for s in paper_only)
    for s in paper_only:
        r = paper_strats[s]
        exit_note = f"sl_stage={r['sl_stage']}" if r["sl_stage"] else r["status"] or "-"
        print(f"      {s:<24} paper_pnl={r['pnl'] or 0:+.2f} status={exit_note}")
    print(f"      Subtotal: {pnl_a:+.2f}")

# 3b. Strategies in WhatIf but NOT in paper (MetaAgent filtered or skipped)
wi_only = [s for s in wi_strats if s not in paper_strats]
if wi_only:
    pnl_b = sum(sum(r["pnl_total"] or 0 for r in wi_strats[s]) for s in wi_only)
    wins_b = sum(1 for s in wi_only for r in wi_strats[s] if r["result"] == "PROFIT")
    print(f"\n  [B] In WhatIf but NOT executed in paper ({len(wi_only)} strats) "
          f"[MetaAgent filtered / not placed]:")
    print(f"      If these had been traded WhatIf would add: {pnl_b:+.2f}  ({wins_b} profitable)")
    for s in wi_only:
        s_pnl = sum(r["pnl_total"] or 0 for r in wi_strats[s])
        print(f"      {s:<24} wi_pnl={s_pnl:+.2f}  ({', '.join(r['exit_reason'] or '' for r in wi_strats[s])})")

# 3c. Same strategy, different PnL (entry price gap + SL behaviour diff)
common = [s for s in paper_strats if s in wi_strats]
if common:
    print(f"\n  [C] Common strategies — entry price & SL differences ({len(common)} strats):")
    print(f"  {'Strategy':<24} {'PaperPnL':>10} {'WI_PnL':>10} {'Diff':>10} "
          f"{'P_EP':>8} {'WI_EP':>8} {'P_exit':<16} {'WI_exit'}")
    print(f"  {'-'*24} {'-'*10} {'-'*10} {'-'*10} {'-'*8} {'-'*8} {'-'*16} {'-'*14}")
    for s in common:
        pr      = paper_strats[s]
        wl      = wi_strats[s]
        p_pnl   = pr["pnl"] or 0
        wi_pnl_s = sum(r["pnl_total"] or 0 for r in wl)
        diff    = wi_pnl_s - p_pnl
        p_ep    = pr["actual_entry_price"] or pr["entry_price"] or 0
        wi_ep   = wl[0]["entry_price"] or 0
        p_exit  = (f"sl_stage={pr['sl_stage']}" if pr["sl_stage"] else pr["status"] or "-")
        wi_exit = ", ".join(r["exit_reason"] or "" for r in wl)
        print(f"  {s:<24} {p_pnl:>+10.2f} {wi_pnl_s:>+10.2f} {diff:>+10.2f} "
              f"{p_ep:>8.2f} {wi_ep:>8.2f} {p_exit:<16} {wi_exit}")

# ── 4. Summary ────────────────────────────────────────────────────────────
print(f"\n{'='*70}")
print(f"  SUMMARY")
print(f"  Paper PnL     : {paper_pnl:+.2f}  ({len(paper_rows)} trades)")
print(f"  WhatIf PnL    : {wi_pnl:+.2f}  ({len(wi_rows)} trades)")
print(f"  Gap           : {wi_pnl - paper_pnl:+.2f}")
print()

# Reasons breakdown for paper
if paper_rows:
    reasons = {}
    for r in paper_rows:
        k = (f"sl_stage={r['sl_stage']}" if r["sl_stage"] else r["status"] or "?")
        reasons[k] = reasons.get(k, 0) + 1
    print(f"  Paper exit reasons: {reasons}")

# Exit reasons breakdown for WhatIf
if wi_rows:
    wi_reasons = {}
    for r in wi_rows:
        k = r["exit_reason"] or "?"
        wi_reasons[k] = wi_reasons.get(k, 0) + 1
    print(f"  WhatIf exit reasons: {wi_reasons}")

# Capital summary per paper trade
print(f"\n  PAPER CAPITAL PER TRADE:")
print(f"  {'Strategy':<24} {'Symbol':<30} {'Qty':>4} {'Entry':>8} {'Capital':>12}")
print(f"  {'-'*24} {'-'*30} {'-'*4} {'-'*8} {'-'*12}")
total_capital = 0
for r in paper_rows:
    ep  = r["actual_entry_price"] or r["entry_price"] or 0
    cap = ep * (r["quantity"] or 0)
    total_capital += cap
    exit_note = f"sl_stage={r['sl_stage']}" if r["sl_stage"] else r["status"] or "-"
    print(f"  {(r['strategy_name'] or ''):<24} {(r['tradingsymbol'] or ''):<30} "
          f"{r['quantity']:>4} {ep:>8.2f} Rs {cap:>10,.0f}  pnl={r['pnl'] or 0:+.2f}  {exit_note}")
print(f"  {'TOTAL':.<24} {'':.<30} {'':>4} {'':>8} Rs {total_capital:>10,.0f}")
print(f"{'='*70}\n")

con.close()
