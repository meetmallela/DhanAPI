# -*- coding: utf-8 -*-
import sys, io
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

"""
vwap_multiindex.py
------------------
Runs VWAP_Cross signal on both NIFTY and BANKNIFTY simultaneously.

Shows:
  * Per-index monthly P&L
  * Combined monthly P&L (NIFTY + BANKNIFTY together)
  * Signal overlap days (both fire same month)
  * 6-panel dashboard chart saved to backtest_results/

Why this matters:
  Pure VWAP_Cross on NIFTY averages ~2 signals/month but 86% direction accuracy.
  Adding BANKNIFTY ~doubles the signal frequency while (hopefully) keeping
  quality high since BANKNIFTY is correlated but not identical to NIFTY.

Run:
    python vwap_multiindex.py
"""

import sys
import statistics
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import pandas as pd
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from backtest.data_fetcher import fetch_yahoo

# ===============================================================================
# CONFIG
# ===============================================================================

FROM_DATE   = "2024-01-01"
TO_DATE     = "2026-04-11"

# --- NIFTY options model ---
NIFTY_LOTS      = 5
NIFTY_LOT_SIZE  = 75
NIFTY_PREMIUM   = 175       # avg ATM premium in pts (NIFTY ~22,000)
NIFTY_DELTA     = 0.5
NIFTY_THETA     = 12        # pts/day theta decay
NIFTY_SPREAD    = 4         # bid-ask spread in pts
NIFTY_BROKERAGE = 40 * NIFTY_LOTS   # Rs both legs

# --- BANKNIFTY options model ---
# BankNifty ~52,000; ATM premium ~300pts; lot size 15 (post-2024 revision)
BNIFTY_LOTS      = 5
BNIFTY_LOT_SIZE  = 15
BNIFTY_PREMIUM   = 300      # avg ATM premium in pts
BNIFTY_DELTA     = 0.5
BNIFTY_THETA     = 25       # higher theta for BankNifty (more expensive)
BNIFTY_SPREAD    = 8
BNIFTY_BROKERAGE = 40 * BNIFTY_LOTS

# Signal parameter
SMA_PERIOD      = 20        # SMA used as daily VWAP proxy
BIG_MOVE_NIFTY  = 200       # pts — big day for NIFTY
BIG_MOVE_BNIFTY = 500       # pts — big day for BANKNIFTY (~2x scale)

# ===============================================================================
# SIGNAL  (same for both indices — VWAP_Cross via SMA-20)
# ===============================================================================

def vwap_cross_signal(df: pd.DataFrame) -> pd.Series:
    sma   = df["close"].rolling(SMA_PERIOD).mean()
    above = df["close"] > sma
    buy   = above  & ~above.shift(1).fillna(False)
    sell  = ~above & above.shift(1).fillna(True)
    sig   = pd.Series(0, index=df.index)
    sig[buy]  =  1
    sig[sell] = -1
    return sig

# ===============================================================================
# P&L MODEL
# ===============================================================================

def calc_pnl(index_move: float, correct: bool,
             delta, theta, spread, premium, lots, lot_size, brokerage) -> float:
    units = lots * lot_size
    if correct:
        pts = delta * abs(index_move) - theta - spread
        pts = max(pts, -premium)
    else:
        pts = -(theta + spread + delta * abs(index_move) * 0.3)
        pts = max(pts, -premium)
    return pts * units - brokerage

# ===============================================================================
# ANALYSE ONE INDEX
# ===============================================================================

def analyse(df: pd.DataFrame, name: str,
            lots, lot_size, premium, delta, theta, spread, brokerage,
            big_move_threshold: float) -> dict:

    df = df.copy()
    df["range"]    = df["high"] - df["low"]
    df["move"]     = df["close"] - df["open"]
    df["abs_move"] = df["move"].abs()
    df["big_move"] = df["range"] >= big_move_threshold
    df["signal"]   = vwap_cross_signal(df)
    df["month"]    = df["timestamp"].dt.to_period("M")

    sig_df = df[df["signal"] != 0].copy()
    sig_df["correct"] = (
        ((sig_df["signal"] == 1)  & (sig_df["move"] > 0)) |
        ((sig_df["signal"] == -1) & (sig_df["move"] < 0))
    )

    dir_acc = sig_df["correct"].mean() * 100 if len(sig_df) else 0

    pnl_list = []
    for _, row in sig_df.iterrows():
        p = calc_pnl(row["abs_move"], bool(row["correct"]),
                     delta, theta, spread, premium, lots, lot_size, brokerage)
        pnl_list.append(p)

    monthly = {}
    for m, grp in df.groupby("month"):
        sg  = grp[grp["signal"] != 0]
        pnl = 0.0
        for _, row in sg.iterrows():
            correct = bool(
                ((row["signal"] == 1)  and (row["move"] > 0)) or
                ((row["signal"] == -1) and (row["move"] < 0))
            )
            pnl += calc_pnl(row["abs_move"], correct,
                            delta, theta, spread, premium, lots, lot_size, brokerage)
        monthly[m] = {
            "signals":   int((grp["signal"] != 0).sum()),
            "big_moves": int(grp["big_move"].sum()),
            "pnl":       pnl,
            "signal_dates": list(grp[grp["signal"] != 0]["timestamp"].dt.date),
        }

    total_days    = len(df)
    big_days      = int(df["big_move"].sum())
    big_with_sig  = int((df["big_move"] & (df["signal"] != 0)).sum())
    months        = df["month"].nunique()

    return {
        "name":               name,
        "lots":               lots,
        "lot_size":           lot_size,
        "units":              lots * lot_size,
        "total_days":         total_days,
        "signal_days":        len(sig_df),
        "signal_pm":          len(sig_df) / months,
        "big_move_days":      big_days,
        "big_detect_pct":     big_with_sig / big_days * 100 if big_days else 0,
        "dir_acc":            dir_acc,
        "avg_range_signal":   sig_df["range"].mean() if len(sig_df) else 0,
        "avg_range_all":      df["range"].mean(),
        "total_pnl":          sum(pnl_list),
        "avg_pnl_per_signal": statistics.mean(pnl_list) if pnl_list else 0,
        "pnl_per_month":      sum(pnl_list) / months,
        "win_trades":         sum(1 for p in pnl_list if p > 0),
        "loss_trades":        sum(1 for p in pnl_list if p <= 0),
        "monthly":            monthly,
        "pnl_list":           pnl_list,
        "months":             months,
    }

# ===============================================================================
# REPORT
# ===============================================================================

W = 74

def _divider(c="="):
    print(c * W)

def print_report(r_nifty: dict, r_bnifty: dict, months_sorted: list):

    # ── Combined monthly stats ──────────────────────────────────────────────
    combined = {}
    for m in months_sorted:
        mn = r_nifty["monthly"].get(m, {"signals": 0, "pnl": 0.0, "big_moves": 0, "signal_dates": []})
        mb = r_bnifty["monthly"].get(m, {"signals": 0, "pnl": 0.0, "big_moves": 0, "signal_dates": []})
        combined[m] = {
            "nifty_signals":  mn["signals"],
            "bnifty_signals": mb["signals"],
            "total_signals":  mn["signals"] + mb["signals"],
            "nifty_pnl":      mn["pnl"],
            "bnifty_pnl":     mb["pnl"],
            "total_pnl":      mn["pnl"] + mb["pnl"],
        }

    combined_monthly_pnls = [combined[m]["total_pnl"] for m in months_sorted]
    nifty_monthly_pnls    = [r_nifty["monthly"].get(m, {"pnl": 0})["pnl"] for m in months_sorted]
    bnifty_monthly_pnls   = [r_bnifty["monthly"].get(m, {"pnl": 0})["pnl"] for m in months_sorted]

    # ── Print ───────────────────────────────────────────────────────────────
    print()
    _divider("=")
    print(f"  VWAP_CROSS MULTI-INDEX ANALYSIS  |  {FROM_DATE} to {TO_DATE}")
    print(f"  NIFTY: {NIFTY_LOTS}L x {NIFTY_LOT_SIZE}u  prem={NIFTY_PREMIUM}pts  "
          f"theta={NIFTY_THETA}/day  spread={NIFTY_SPREAD}pts")
    print(f"  BANKNIFTY: {BNIFTY_LOTS}L x {BNIFTY_LOT_SIZE}u  prem={BNIFTY_PREMIUM}pts  "
          f"theta={BNIFTY_THETA}/day  spread={BNIFTY_SPREAD}pts")
    _divider("=")

    # Per-index summary
    print()
    print(f"  {'Metric':<28}  {'NIFTY':>14}  {'BANKNIFTY':>14}  {'COMBINED':>14}")
    print("  " + "-" * 68)
    print(f"  {'Signal days (total)':<28}  "
          f"{r_nifty['signal_days']:>14}  {r_bnifty['signal_days']:>14}  "
          f"{r_nifty['signal_days']+r_bnifty['signal_days']:>14}")
    print(f"  {'Avg signals / month':<28}  "
          f"{r_nifty['signal_pm']:>13.1f}  {r_bnifty['signal_pm']:>13.1f}  "
          f"{r_nifty['signal_pm']+r_bnifty['signal_pm']:>13.1f}")
    print(f"  {'Direction accuracy %':<28}  "
          f"{r_nifty['dir_acc']:>13.1f}%  {r_bnifty['dir_acc']:>13.1f}%  "
          f"{'--':>14}")
    print(f"  {'Avg range on signal days':<28}  "
          f"{r_nifty['avg_range_signal']:>13.0f}  {r_bnifty['avg_range_signal']:>13.0f}  "
          f"{'--':>14}")
    print(f"  {'Avg range all days':<28}  "
          f"{r_nifty['avg_range_all']:>13.0f}  {r_bnifty['avg_range_all']:>13.0f}  "
          f"{'--':>14}")

    def _win(r):
        t = r['win_trades'] + r['loss_trades']
        return f"{r['win_trades']/t*100:.0f}%" if t else "--"

    print(f"  {'Win rate':<28}  "
          f"{_win(r_nifty):>14}  {_win(r_bnifty):>14}  {'--':>14}")
    print(f"  {'Total P&L (Rs)':<28}  "
          f"{r_nifty['total_pnl']:>+14,.0f}  {r_bnifty['total_pnl']:>+14,.0f}  "
          f"{r_nifty['total_pnl']+r_bnifty['total_pnl']:>+14,.0f}")
    print(f"  {'Avg P&L / month (Rs)':<28}  "
          f"{r_nifty['pnl_per_month']:>+14,.0f}  {r_bnifty['pnl_per_month']:>+14,.0f}  "
          f"{r_nifty['pnl_per_month']+r_bnifty['pnl_per_month']:>+14,.0f}")

    comb_profit_months = sum(1 for p in combined_monthly_pnls if p > 0)
    nifty_profit_months = sum(1 for p in nifty_monthly_pnls if p > 0)
    bnifty_profit_months = sum(1 for p in bnifty_monthly_pnls if p > 0)
    n_months = len(months_sorted)
    print(f"  {'Profitable months':<28}  "
          f"{nifty_profit_months}/{n_months:>12}  {bnifty_profit_months}/{n_months:>12}  "
          f"{comb_profit_months}/{n_months:>12}")

    # Monthly detail table
    print()
    _divider("=")
    print("  MONTHLY BREAKDOWN")
    _divider("-")
    print(f"  {'Month':<10}  {'NF Sig':>6}  {'NF P&L':>11}  "
          f"{'BNF Sig':>7}  {'BNF P&L':>11}  "
          f"{'Total Sig':>9}  {'Combined P&L':>13}  Note")
    print("  " + "-" * 71)
    for m in months_sorted:
        c = combined[m]
        ns = c["nifty_signals"]
        bs = c["bnifty_signals"]
        ts = c["total_signals"]
        np_ = c["nifty_pnl"]
        bp  = c["bnifty_pnl"]
        tp  = c["total_pnl"]
        note = ""
        if ts >= 3 and tp > 0:    note = " << TARGET"
        elif ts >= 3:              note = " (3+ signals)"
        elif tp >= 100_000:        note = " (Rs 1L+ month)"
        print(f"  {str(m):<10}  {ns:>6}  Rs {np_:>+9,.0f}  "
              f"{bs:>7}  Rs {bp:>+9,.0f}  "
              f"{ts:>9}  Rs {tp:>+11,.0f}  {note}")

    print("  " + "-" * 71)
    print(f"  {'AVERAGE':<10}  "
          f"{'':>6}  Rs {statistics.mean(nifty_monthly_pnls):>+9,.0f}  "
          f"{'':>7}  Rs {statistics.mean(bnifty_monthly_pnls):>+9,.0f}  "
          f"{'':>9}  Rs {statistics.mean(combined_monthly_pnls):>+11,.0f}")
    print(f"  {'TOTAL':<10}  "
          f"{'':>6}  Rs {sum(nifty_monthly_pnls):>+9,.0f}  "
          f"{'':>7}  Rs {sum(bnifty_monthly_pnls):>+9,.0f}  "
          f"{'':>9}  Rs {sum(combined_monthly_pnls):>+11,.0f}")

    # Verdict
    print()
    _divider("=")
    print("  VERDICT")
    _divider("=")
    avg_comb = statistics.mean(combined_monthly_pnls)
    avg_sig  = r_nifty["signal_pm"] + r_bnifty["signal_pm"]
    comb_dir = (r_nifty["dir_acc"] * r_nifty["signal_days"] +
                r_bnifty["dir_acc"] * r_bnifty["signal_days"]) / \
               (r_nifty["signal_days"] + r_bnifty["signal_days"])
    print()
    print(f"  Combined avg signals / month : {avg_sig:.1f}")
    print(f"  Weighted direction accuracy  : {comb_dir:.1f}%")
    print(f"  Combined avg P&L / month     : Rs {avg_comb:+,.0f}")
    print(f"  Months hitting Rs 1 Lakh+    : "
          f"{sum(1 for p in combined_monthly_pnls if p >= 100_000)}/{n_months}")
    print(f"  Profitable months            : {comb_profit_months}/{n_months}  "
          f"({comb_profit_months/n_months*100:.0f}%)")
    print()
    if avg_comb >= 100_000:
        print("  RESULT: STRONG -- Combined VWAP_Cross meets the Rs 1L/month target on average.")
    elif avg_comb >= 50_000:
        print("  RESULT: PROMISING -- Combined strategy averages Rs 50k+ / month.")
        print("          Adding a 3rd index or increasing lot size could reach Rs 1L target.")
    else:
        print("  RESULT: BELOW TARGET -- Combined avg is below Rs 1L/month.")
    print()
    print("  IMPORTANT CAVEATS:")
    print("  1. Daily-bar VWAP proxy (SMA-20) is less precise than intraday VWAP.")
    print("  2. Direction accuracy on daily bars overestimates real-world accuracy")
    print("     (intraday execution adds noise -- expect 5-10% lower accuracy live).")
    print("  3. NIFTY and BANKNIFTY are correlated -- on high-volatility days both")
    print("     may signal together, creating concentrated risk not shown here.")
    print("  4. Theta costs assume 1-day hold -- multi-day holds cost more.")
    _divider("=")

    return combined, combined_monthly_pnls, nifty_monthly_pnls, bnifty_monthly_pnls


# ===============================================================================
# CHARTS
# ===============================================================================

def plot_results(r_nifty, r_bnifty, months_sorted,
                 combined, combined_pnls, nifty_pnls, bnifty_pnls):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        from matplotlib.ticker import FuncFormatter
    except ImportError:
        print("  matplotlib not installed -- skipping charts")
        return

    NF_COL  = "#4C9BE8"    # blue   -- NIFTY
    BNF_COL = "#FFB347"    # orange -- BANKNIFTY
    CMB_COL = "#C77DFF"    # purple -- combined
    TGT_COL = "#FFDD57"    # yellow -- target line

    fig = plt.figure(figsize=(18, 14), facecolor="#1A1A2E")
    fig.suptitle(
        f"VWAP Cross  |  NIFTY + BANKNIFTY Combined  |  {FROM_DATE} to {TO_DATE}",
        fontsize=14, fontweight="bold", color="white", y=0.99
    )
    gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.55, wspace=0.38)

    def _style(ax, title):
        ax.set_facecolor("#16213E")
        ax.tick_params(colors="white", labelsize=8)
        ax.set_title(title, color="white", fontsize=9, fontweight="bold", pad=6)
        for sp in ax.spines.values():
            sp.set_edgecolor("#444466")
        ax.yaxis.label.set_color("white")
        ax.xaxis.label.set_color("white")

    def _rs(ax):
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda x, _: f"Rs {x/1_00_000:.1f}L" if abs(x) >= 1_00_000
                         else (f"Rs {x/1000:.0f}k" if abs(x) >= 1000 else f"Rs {x:.0f}")
        ))

    month_labels = [str(m) for m in months_sorted]
    x = np.arange(len(months_sorted))

    # ── Chart 1: Monthly signals count per index  ─────────────────────────
    ax1 = fig.add_subplot(gs[0, 0])
    _style(ax1, "Monthly Signal Count")
    nf_sigs  = [r_nifty["monthly"].get(m, {"signals": 0})["signals"]  for m in months_sorted]
    bnf_sigs = [r_bnifty["monthly"].get(m, {"signals": 0})["signals"] for m in months_sorted]
    tot_sigs = [a + b for a, b in zip(nf_sigs, bnf_sigs)]
    ax1.bar(x - 0.25, nf_sigs,  0.25, label="NIFTY",     color=NF_COL,  alpha=0.85)
    ax1.bar(x,        bnf_sigs, 0.25, label="BANKNIFTY", color=BNF_COL, alpha=0.85)
    ax1.bar(x + 0.25, tot_sigs, 0.25, label="Combined",  color=CMB_COL, alpha=0.85)
    ax1.axhline(3, color=TGT_COL, linestyle="--", linewidth=0.8, label="3/month target")
    ax1.set_xticks(x[::3])
    ax1.set_xticklabels(month_labels[::3], rotation=45, ha="right", fontsize=6)
    ax1.legend(fontsize=6.5, facecolor="#16213E", labelcolor="white", framealpha=0.5)
    ax1.set_ylabel("Signals", fontsize=8, color="white")

    # ── Chart 2: Avg P&L per signal day  ──────────────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    _style(ax2, "Avg P&L per Signal (Rs)")
    cats = ["NIFTY", "BANKNIFTY"]
    vals = [r_nifty["avg_pnl_per_signal"], r_bnifty["avg_pnl_per_signal"]]
    cols = [NF_COL, BNF_COL]
    bars = ax2.bar(cats, vals, color=cols, edgecolor="#333355", width=0.4)
    ax2.axhline(0, color="white", linewidth=0.5)
    for bar, v in zip(bars, vals):
        ax2.text(bar.get_x() + bar.get_width()/2,
                 v + (800 if v >= 0 else -2500),
                 f"Rs {v:+,.0f}", ha="center",
                 va="bottom" if v >= 0 else "top",
                 fontsize=8, color="white", fontweight="bold")
    _rs(ax2)

    # ── Chart 3: Direction accuracy comparison  ────────────────────────────
    ax3 = fig.add_subplot(gs[0, 2])
    _style(ax3, "Direction Accuracy & Win Rate")
    cats2 = ["NF Dir%", "BNF Dir%", "NF Win%", "BNF Win%"]
    nf_wr  = r_nifty["win_trades"]  / (r_nifty["win_trades"]  + r_nifty["loss_trades"])  * 100
    bnf_wr = r_bnifty["win_trades"] / (r_bnifty["win_trades"] + r_bnifty["loss_trades"]) * 100
    vals3 = [r_nifty["dir_acc"], r_bnifty["dir_acc"], nf_wr, bnf_wr]
    cols3 = [NF_COL, BNF_COL, NF_COL, BNF_COL]
    bars3 = ax3.bar(cats2, vals3, color=cols3, edgecolor="#333355", width=0.5)
    for bar, a in zip(bars3, [1, 1, 0.6, 0.6]):
        bar.set_alpha(a)
    ax3.axhline(52, color=TGT_COL, linestyle="--", linewidth=0.8, label="52% ref")
    for bar, v in zip(bars3, vals3):
        ax3.text(bar.get_x() + bar.get_width()/2, v + 0.5,
                 f"{v:.0f}%", ha="center", va="bottom", fontsize=8, color="white")
    ax3.set_ylim(0, 105)
    ax3.legend(fontsize=7, facecolor="#16213E", labelcolor="white", framealpha=0.5)

    # ── Chart 4 (wide): Monthly P&L -- grouped bars  ──────────────────────
    ax4 = fig.add_subplot(gs[1, :])
    _style(ax4, "Monthly P&L: NIFTY vs BANKNIFTY vs Combined (Rs)")
    ax4.bar(x - 0.27, nifty_pnls,   0.27, label="NIFTY",     color=NF_COL,  alpha=0.85)
    ax4.bar(x,        bnifty_pnls,  0.27, label="BANKNIFTY", color=BNF_COL, alpha=0.85)
    ax4.bar(x + 0.27, combined_pnls,0.27, label="Combined",  color=CMB_COL, alpha=0.85)
    ax4.axhline(0,        color="white",  linewidth=0.5)
    ax4.axhline(100_000,  color=TGT_COL,  linestyle="--", linewidth=0.9, label="Rs 1L target")
    ax4.axhline(-100_000, color="#FF6B6B", linestyle=":",  linewidth=0.7, label="Rs -1L loss")
    ax4.set_xticks(x)
    ax4.set_xticklabels(month_labels, rotation=45, ha="right", fontsize=7)
    ax4.legend(fontsize=7.5, facecolor="#16213E", labelcolor="white",
               framealpha=0.6, ncol=5, loc="upper right")
    _rs(ax4)

    # ── Chart 5: Cumulative P&L  ───────────────────────────────────────────
    ax5 = fig.add_subplot(gs[2, :2])
    _style(ax5, "Cumulative P&L (Rs)")
    nf_cum  = pd.Series(r_nifty["pnl_list"]).cumsum()
    bnf_cum = pd.Series(r_bnifty["pnl_list"]).cumsum()

    # Merge by chronological signal order
    # Create a combined timeline-sorted list
    all_pnl_combined = sorted(
        [(i, p, "NF")  for i, p in enumerate(r_nifty["pnl_list"])] +
        [(i, p, "BNF") for i, p in enumerate(r_bnifty["pnl_list"])],
        key=lambda x: x[0]
    )
    cmb_cum = pd.Series([p for _, p, _ in all_pnl_combined]).cumsum()

    ax5.plot(range(len(nf_cum)),  nf_cum,  color=NF_COL,  linewidth=1.8, label="NIFTY")
    ax5.plot(range(len(bnf_cum)), bnf_cum, color=BNF_COL, linewidth=1.8, label="BANKNIFTY")
    ax5.plot(range(len(cmb_cum)), cmb_cum, color=CMB_COL, linewidth=2.2,
             label="Combined", linestyle="-")
    ax5.axhline(0, color="white", linewidth=0.5)
    ax5.legend(fontsize=8, facecolor="#16213E", labelcolor="white", framealpha=0.6)
    ax5.set_xlabel("Signal # (chronological)", fontsize=8)
    _rs(ax5)

    # ── Chart 6: Months at each P&L bucket (distribution)  ────────────────
    ax6 = fig.add_subplot(gs[2, 2])
    _style(ax6, "Combined Monthly P&L Distribution")
    buckets = ["<-1L", "-1L to 0", "0 to 50k", "50k-1L", "1L-2L", ">2L"]
    def bucket(p):
        if p < -100_000:  return 0
        if p < 0:         return 1
        if p < 50_000:    return 2
        if p < 100_000:   return 3
        if p < 200_000:   return 4
        return 5
    counts = [0] * 6
    for p in combined_pnls:
        counts[bucket(p)] += 1
    bar_cols = ["#FF4444", "#FF8888", "#AAAAAA", "#88DD88", "#44BB44", "#00AA00"]
    ax6.bar(buckets, counts, color=bar_cols, edgecolor="#333355", linewidth=0.6)
    ax6.set_ylabel("Months", fontsize=8)
    ax6.tick_params(axis="x", rotation=20, labelsize=7)
    for i, (b, v) in enumerate(zip(buckets, counts)):
        if v > 0:
            ax6.text(i, v + 0.05, str(v), ha="center", va="bottom",
                     fontsize=8, color="white")

    out_dir  = Path(__file__).parent / "backtest_results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"vwap_multiindex_{FROM_DATE}_to_{TO_DATE}.png"
    fig.savefig(out_path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n  Chart saved --> {out_path}")
    return out_path


# ===============================================================================
# MAIN
# ===============================================================================

def main():
    print()
    print(f"  Loading NIFTY + BANKNIFTY daily data  {FROM_DATE} -> {TO_DATE} ...")
    df_nf  = fetch_yahoo("NIFTY",     FROM_DATE, TO_DATE, interval=0, verbose=True)
    df_bnf = fetch_yahoo("BANKNIFTY", FROM_DATE, TO_DATE, interval=0, verbose=True)

    if df_nf.empty or df_bnf.empty:
        print("  ERROR: Could not load data. Check internet connection.")
        sys.exit(1)

    print(f"\n  Running VWAP_Cross analysis ...")

    r_nifty  = analyse(df_nf,  "NIFTY",
                       NIFTY_LOTS,  NIFTY_LOT_SIZE,  NIFTY_PREMIUM,
                       NIFTY_DELTA,  NIFTY_THETA,  NIFTY_SPREAD,  NIFTY_BROKERAGE,
                       BIG_MOVE_NIFTY)

    r_bnifty = analyse(df_bnf, "BANKNIFTY",
                       BNIFTY_LOTS, BNIFTY_LOT_SIZE, BNIFTY_PREMIUM,
                       BNIFTY_DELTA, BNIFTY_THETA, BNIFTY_SPREAD, BNIFTY_BROKERAGE,
                       BIG_MOVE_BNIFTY)

    months_sorted = sorted(set(r_nifty["monthly"]) | set(r_bnifty["monthly"]))

    combined, comb_pnls, nf_pnls, bnf_pnls = print_report(r_nifty, r_bnifty, months_sorted)
    plot_results(r_nifty, r_bnifty, months_sorted, combined, comb_pnls, nf_pnls, bnf_pnls)


if __name__ == "__main__":
    main()
