# -*- coding: utf-8 -*-
import sys, io
if hasattr(sys.stdout, 'buffer'):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

"""
compare_entry_signals.py
------------------------
Tests three classic entry signals on NIFTY daily data to answer:

  "Can ORB / VWAP / EMA-crossover reliably identify the ~7 big-move days per month
   needed to make the '5-lot / 100pt / 3-days' options idea work?"

Since intraday 5m data is unavailable from Yahoo Finance for ^NSEI, we use
daily OHLCV data (560 bars, 2024-01-01 to 2026-04-11) with daily-bar
approximations of each signal.  The approximations are clearly labelled so
you know exactly what is being tested.

Signal definitions (daily-bar versions)
----------------------------------------
1. ORB_Gap   -- Opening Range Breakout (proxied via opening gap)
   Real ORB: buy when price breaks out of the first-15-min H/L range.
   Daily proxy: if today's open is > 0.15% above prev close  --> BUY signal
                if today's open is > 0.15% below prev close  --> SELL signal
   Rationale: a strong gap implies price has already broken the prior close
   range; on the daily bar the direction is confirmed by close > open (BUY)
   or close < open (SELL).

2. VWAP_Cross -- VWAP Pullback (proxied via SMA-20 cross)
   Real VWAP: price dips to intraday VWAP, then bounces back above/below.
   Daily proxy: SMA-20 acts as the "daily VWAP equivalent".
                BUY when today's close crosses ABOVE SMA-20 (prev close <= SMA-20)
                SELL when today's close crosses BELOW SMA-20.

3. EMA_Cross -- EMA 9/21 Crossover
   Same on both daily and intraday: identical signal, no approximation needed.
   BUY when EMA-9 crosses above EMA-21, SELL when it crosses below.

For each signal the report shows:
  * How many trading days produced a signal
  * Average daily NIFTY range (High-Low) on signal days vs non-signal days
  * % of 200+ pt "big move" days that had a signal the same day
  * Simulated options P&L (5 lots, ATM, delta=0.5, theta=12/day, spread=4pt)
  * Monthly breakdown: signal count, hit rate, estimated P&L

Run:
    python compare_entry_signals.py
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

FROM_DATE       = "2024-01-01"
TO_DATE         = "2026-04-11"

BIG_MOVE_PTS    = 200       # NIFTY pts -- a day with range >= this is a "big move day"
LOTS            = 5
LOT_SIZE        = 75
UNITS           = LOTS * LOT_SIZE           # 375 units
ATM_ENTRY_PREM  = 175                       # Rs -- approximate ATM premium paid
DELTA           = 0.5                       # ATM delta
THETA_PER_DAY   = 12                        # theta decay in option pts per day
SPREAD_COST     = 4                         # bid/ask spread cost in option pts
BROKERAGE       = 40 * LOTS                 # Rs per trade (both legs combined)

ORB_GAP_PCT     = 0.15      # gap % threshold for ORB_Gap signal
EMA_FAST        = 9
EMA_SLOW        = 21
SMA_VWAP        = 20        # SMA period used as VWAP proxy

# ===============================================================================
# HELPERS
# ===============================================================================

def ema_series(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()

def sma_series(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()

def option_pnl_rs(index_move: float, direction_correct: bool) -> float:
    """
    Estimate option P&L in Rs for 1 trade (5 lots).
    direction_correct: True  -> we bought the right side (CE or PE)
                       False -> we bought the wrong side (option decays)
    """
    if direction_correct:
        gain_pts = DELTA * abs(index_move) - THETA_PER_DAY - SPREAD_COST
        gain_pts = max(gain_pts, -ATM_ENTRY_PREM)   # max loss = premium paid
    else:
        # Wrong direction: option loses value fast
        # Assume 50% premium erosion on a move against us
        gain_pts = -(THETA_PER_DAY + SPREAD_COST + DELTA * abs(index_move) * 0.3)
        gain_pts = max(gain_pts, -ATM_ENTRY_PREM)

    return gain_pts * UNITS - BROKERAGE

# ===============================================================================
# SIGNAL GENERATORS  (all return a pd.Series of 1 / -1 / 0)
# ===============================================================================

def signal_orb_gap(df: pd.DataFrame) -> pd.Series:
    """
    ORB_Gap: strong opening gap implies breakout direction.
    BUY  if open > prev_close * (1 + ORB_GAP_PCT/100)
    SELL if open < prev_close * (1 - ORB_GAP_PCT/100)
    """
    prev_close = df["close"].shift(1)
    gap_up   = df["open"] > prev_close * (1 + ORB_GAP_PCT / 100)
    gap_down = df["open"] < prev_close * (1 - ORB_GAP_PCT / 100)

    sig = pd.Series(0, index=df.index)
    sig[gap_up]   =  1
    sig[gap_down] = -1
    return sig


def signal_vwap_cross(df: pd.DataFrame) -> pd.Series:
    """
    VWAP_Cross: close crosses daily SMA-20 (VWAP proxy).
    BUY  when close crosses above SMA-20 (prev close was below)
    SELL when close crosses below SMA-20 (prev close was above)
    """
    sma = sma_series(df["close"], SMA_VWAP)
    above = df["close"] > sma
    below = df["close"] < sma

    buy_cross  = above  & ~above.shift(1).fillna(False)
    sell_cross = below  & ~below.shift(1).fillna(False)

    sig = pd.Series(0, index=df.index)
    sig[buy_cross]  =  1
    sig[sell_cross] = -1
    return sig


def signal_ema_cross(df: pd.DataFrame) -> pd.Series:
    """
    EMA_Cross: EMA-9 crosses EMA-21.
    BUY  when EMA-9 crosses above EMA-21
    SELL when EMA-9 crosses below EMA-21
    """
    e_fast = ema_series(df["close"], EMA_FAST)
    e_slow = ema_series(df["close"], EMA_SLOW)

    fast_above = e_fast > e_slow
    buy_cross  = fast_above & ~fast_above.shift(1).fillna(False)
    sell_cross = ~fast_above & fast_above.shift(1).fillna(True)

    sig = pd.Series(0, index=df.index)
    sig[buy_cross]  =  1
    sig[sell_cross] = -1
    return sig


def signal_vwap_orb_combo(df: pd.DataFrame) -> pd.Series:
    """
    VWAP+ORB Combo: VWAP cross confirmed by an ORB gap in the same direction.

    Logic:
      A VWAP_Cross signal fires when close crosses SMA-20.
      An ORB_Gap signal fires when today's open gaps > ORB_GAP_PCT from prev close.
      The COMBO fires only when BOTH agree on direction for the same day.

    This adds frequency vs pure VWAP_Cross while keeping direction discipline.
    Three modes:
      Mode A (strict)  -- both signals on the SAME day, same direction
      Mode B (relaxed) -- VWAP cross day, OR ORB gap day with VWAP trend alignment
                          (close still above SMA-20 for BUY, below for SELL)
    We use Mode B (relaxed) to maximise signal count while keeping accuracy high.
    """
    sma    = sma_series(df["close"], SMA_VWAP)
    above  = df["close"] > sma

    # VWAP cross events
    vwap_buy  = above  & ~above.shift(1).fillna(False)
    vwap_sell = ~above & above.shift(1).fillna(True)

    # ORB gap events
    prev_close = df["close"].shift(1)
    gap_up     = df["open"] > prev_close * (1 + ORB_GAP_PCT / 100)
    gap_down   = df["open"] < prev_close * (1 - ORB_GAP_PCT / 100)

    # Combo: VWAP cross day  OR  ORB gap aligned with current VWAP trend
    buy_signal  = vwap_buy  | (gap_up   & above)
    sell_signal = vwap_sell | (gap_down & ~above)

    # Remove conflicts (both fire same day -> skip)
    conflict = buy_signal & sell_signal
    buy_signal  = buy_signal  & ~conflict
    sell_signal = sell_signal & ~conflict

    sig = pd.Series(0, index=df.index)
    sig[buy_signal]  =  1
    sig[sell_signal] = -1
    return sig


SIGNALS = {
    "ORB_Gap":        signal_orb_gap,
    "VWAP_Cross":     signal_vwap_cross,
    "EMA_Cross":      signal_ema_cross,
    "VWAP+ORB_Combo": signal_vwap_orb_combo,
}

# ===============================================================================
# ANALYSIS ENGINE
# ===============================================================================

def analyse_signal(df: pd.DataFrame, signal_fn, name: str) -> dict:
    """
    Compute all stats for one signal against the daily dataset.
    Returns a rich dict consumed by print_report().
    """
    sig = signal_fn(df)

    # Daily range in points
    df = df.copy()
    df["range"]    = df["high"] - df["low"]
    df["move"]     = df["close"] - df["open"]    # signed daily move
    df["abs_move"] = df["move"].abs()
    df["big_move"] = df["range"] >= BIG_MOVE_PTS
    df["signal"]   = sig
    df["month"]    = df["timestamp"].dt.to_period("M")

    signal_days    = df[df["signal"] != 0]
    no_signal_days = df[df["signal"] == 0]

    # How well does the signal align with the actual daily direction?
    # Direction correct: BUY(+1) when close > open, SELL(-1) when close < open
    df["direction_correct"] = (
        ((df["signal"] == 1)  & (df["move"] > 0)) |
        ((df["signal"] == -1) & (df["move"] < 0))
    )
    sig_df = df[df["signal"] != 0].copy()
    if len(sig_df):
        direction_hit_rate = sig_df["direction_correct"].mean()
    else:
        direction_hit_rate = 0.0

    # Big-move detection
    big_days = df[df["big_move"]]
    if len(big_days):
        big_with_signal = big_days[big_days["signal"] != 0]
        big_detection_rate = len(big_with_signal) / len(big_days)
    else:
        big_detection_rate = 0.0

    # P&L simulation: on every signal day, trade options
    total_pnl = 0.0
    pnl_list  = []
    for _, row in sig_df.iterrows():
        correct = bool(row["direction_correct"])
        pnl_rs  = option_pnl_rs(row["abs_move"], correct)
        total_pnl += pnl_rs
        pnl_list.append(pnl_rs)

    # Monthly breakdown
    monthly = defaultdict(lambda: {"signals": 0, "big_moves": 0, "hits": 0, "pnl": 0.0})
    for m, grp in df.groupby("month"):
        monthly[m]["signals"]   = int((grp["signal"] != 0).sum())
        monthly[m]["big_moves"] = int(grp["big_move"].sum())
        monthly[m]["hits"]      = int(((grp["signal"] != 0) & grp["big_move"]).sum()) if len(grp) else 0
        # P&L for signal days in this month
        sig_grp = grp[grp["signal"] != 0]
        for _, row in sig_grp.iterrows():
            correct = bool(row["direction_correct"])
            monthly[m]["pnl"] += option_pnl_rs(row["abs_move"], correct)

    return {
        "name":               name,
        "total_days":         len(df),
        "signal_days":        len(signal_days),
        "signal_pct":         len(signal_days) / len(df) * 100 if len(df) else 0,
        "big_move_days":      int(df["big_move"].sum()),
        "big_detection_rate": big_detection_rate * 100,
        "direction_hit_rate": direction_hit_rate * 100,
        "avg_range_signal":   signal_days["range"].mean() if len(signal_days) else 0,
        "avg_range_no_sig":   no_signal_days["range"].mean() if len(no_signal_days) else 0,
        "avg_abs_move_signal":signal_days["abs_move"].mean() if len(signal_days) else 0,
        "avg_abs_move_no_sig":no_signal_days["abs_move"].mean() if len(no_signal_days) else 0,
        "total_pnl_rs":       total_pnl,
        "pnl_per_signal":     statistics.mean(pnl_list) if pnl_list else 0,
        "pnl_stdev":          statistics.stdev(pnl_list) if len(pnl_list) > 1 else 0,
        "win_trades":         sum(1 for p in pnl_list if p > 0),
        "loss_trades":        sum(1 for p in pnl_list if p <= 0),
        "monthly":            dict(monthly),
        "pnl_list":           pnl_list,
    }


# ===============================================================================
# REPORT PRINTER
# ===============================================================================

W = 72

def _bar(value: float, max_val: float, width: int = 30) -> str:
    """Simple ASCII bar chart."""
    if max_val <= 0:
        return ""
    filled = int(round(value / max_val * width))
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def print_divider(char="="):
    print(char * W)


def print_report(df: pd.DataFrame, results: list[dict]):
    total_days   = len(df)
    df2          = df.copy()
    df2["range"] = df2["high"] - df2["low"]
    df2["move"]  = (df2["close"] - df2["open"]).abs()
    df2["big"]   = df2["range"] >= BIG_MOVE_PTS
    total_big    = int(df2["big"].sum())
    months       = df2["timestamp"].dt.to_period("M").nunique()

    print()
    print_divider("=")
    print(f"  ENTRY SIGNAL COMPARISON  |  NIFTY Daily  {FROM_DATE} to {TO_DATE}")
    print(f"  {total_days} trading days  |  {months} months  |"
          f"  Big-move threshold: {BIG_MOVE_PTS}+ pts range")
    print(f"  Trade model: {LOTS} lots x {LOT_SIZE} units  |"
          f"  delta={DELTA}  theta={THETA_PER_DAY}/day  premium={ATM_ENTRY_PREM}pts")
    print_divider("=")

    # --- DATASET OVERVIEW ---
    print()
    print("  DATASET OVERVIEW")
    print_divider("-")
    avg_range = df2["range"].mean()
    avg_big   = total_big / months
    pct_big   = total_big / total_days * 100
    print(f"  Total trading days  : {total_days}")
    print(f"  Big-move days (200+): {total_big}  ({pct_big:.1f}% of days)")
    print(f"  Avg big moves/month : {avg_big:.1f}")
    print(f"  Avg daily range     : {avg_range:.0f} pts")
    print(f"  Avg daily range (big-move days): "
          f"{df2[df2['big']]['range'].mean():.0f} pts")
    print(f"  Avg daily range (normal days)  : "
          f"{df2[~df2['big']]['range'].mean():.0f} pts")

    # Monthly big-move count table
    print()
    print(f"  {'Month':<12}  {'Big Moves':>9}  {'Avg Range':>9}  {'Max Range':>9}")
    print("  " + "-" * 44)
    by_month = df2.groupby(df2["timestamp"].dt.to_period("M"))
    for m, g in by_month:
        bm = int(g["big"].sum())
        ar = g["range"].mean()
        mr = g["range"].max()
        print(f"  {str(m):<12}  {bm:>9}  {ar:>9.0f}  {mr:>9.0f}")

    # --- PER-SIGNAL SUMMARY TABLE ---
    print()
    print_divider("=")
    print("  SIGNAL COMPARISON SUMMARY")
    print_divider("=")
    print(f"  {'Signal':<14}  {'Signals':>7}  {'Sig%':>6}  "
          f"{'Big Detect%':>11}  {'Dir Hit%':>9}  "
          f"{'AvgRng(sig)':>11}  {'AvgRng(no)':>10}  "
          f"{'Total P&L Rs':>12}")
    print("  " + "-" * (W - 2))
    for r in results:
        print(
            f"  {r['name']:<14}  "
            f"{r['signal_days']:>7}  "
            f"{r['signal_pct']:>5.1f}%  "
            f"{r['big_detection_rate']:>10.1f}%  "
            f"{r['direction_hit_rate']:>8.1f}%  "
            f"{r['avg_range_signal']:>11.0f}  "
            f"{r['avg_range_no_sig']:>10.0f}  "
            f"{r['total_pnl_rs']:>+12,.0f}"
        )
    print()

    # --- DETAILED SECTION PER SIGNAL ---
    for r in results:
        print()
        print_divider("=")
        print(f"  SIGNAL: {r['name']}")
        print_divider("-")
        print(f"  Signal days        : {r['signal_days']}  "
              f"({r['signal_pct']:.1f}% of all days,  "
              f"~{r['signal_days']/months:.1f}/month)")
        print(f"  Big-move detection : {r['big_detection_rate']:.1f}%  "
              f"(of {total_big} big-move days, {r['big_detection_rate']/100*total_big:.0f} had a signal)")
        print(f"  Direction accuracy : {r['direction_hit_rate']:.1f}%  "
              f"(signal predicted correct side that % of the time)")
        print()
        print(f"  Avg daily range on SIGNAL days   : {r['avg_range_signal']:.0f} pts")
        print(f"  Avg daily range on NO-SIGNAL days: {r['avg_range_no_sig']:.0f} pts")
        edge = r['avg_range_signal'] - r['avg_range_no_sig']
        print(f"  Signal selects days with         : {edge:+.0f} pts extra range")
        print()
        print(f"  Options P&L (all signal days, 5 lots):")
        print(f"    Total P&L        : Rs {r['total_pnl_rs']:>+12,.0f}")
        print(f"    Avg per signal   : Rs {r['pnl_per_signal']:>+12,.0f}")
        print(f"    Std dev          : Rs {r['pnl_stdev']:>12,.0f}")
        win_rate = r['win_trades']/(r['win_trades']+r['loss_trades'])*100 \
                   if (r['win_trades']+r['loss_trades']) else 0
        print(f"    Wins / Losses    : {r['win_trades']} / {r['loss_trades']}  "
              f"({win_rate:.1f}% win rate)")
        print()
        print(f"  Monthly breakdown:")
        print(f"  {'Month':<12}  {'Signals':>7}  {'Big Moves':>9}  "
              f"{'Overlap':>7}  {'Monthly P&L':>12}")
        print("  " + "-" * 54)
        monthly_pnls = []
        for m in sorted(r["monthly"].keys()):
            d    = r["monthly"][m]
            sigs = d["signals"]
            bm   = d["big_moves"]
            hits = d["hits"]
            pnl  = d["pnl"]
            monthly_pnls.append(pnl)
            flag = " <-- target" if sigs >= 3 and pnl > 0 else ""
            print(f"  {str(m):<12}  {sigs:>7}  {bm:>9}  {hits:>7}  "
                  f"Rs {pnl:>+10,.0f}{flag}")

        if monthly_pnls:
            profitable_months = sum(1 for p in monthly_pnls if p > 0)
            print(f"  {'':12}  {'':7}  {'':9}  {'':7}  {'-'*13}")
            print(f"  {'Avg/month':<12}  {'':7}  {'':9}  {'':7}  "
                  f"Rs {statistics.mean(monthly_pnls):>+10,.0f}")
            print(f"  Profitable months: {profitable_months}/{len(monthly_pnls)}")

    # --- VERDICT ---
    print()
    print_divider("=")
    print("  VERDICT")
    print_divider("=")
    print()
    print(f"  The idea needs: 3 signal days/month with 200+ pt NIFTY move,")
    print(f"  direction correct ~52%+ of the time, to net Rs ~1,00,000/month.")
    print()
    for r in results:
        avg_monthly_signals = r['signal_days'] / months
        det_rate = r['big_detection_rate']
        dir_acc  = r['direction_hit_rate']
        monthly_pnls = [r["monthly"][m]["pnl"] for m in r["monthly"]]
        avg_pnl = statistics.mean(monthly_pnls) if monthly_pnls else 0
        profitable = sum(1 for p in monthly_pnls if p > 0)

        # Simple scoring
        score = 0
        if avg_monthly_signals >= 3:           score += 1
        if det_rate >= 50:                     score += 1
        if dir_acc >= 52:                      score += 1
        if avg_pnl > 50_000:                   score += 1

        grade = ["POOR", "WEAK", "MARGINAL", "PROMISING", "STRONG"][min(score, 4)]

        print(f"  [{grade}]  {r['name']}")
        print(f"    Avg {avg_monthly_signals:.1f} signals/month  |  "
              f"{det_rate:.0f}% big-move detection  |  "
              f"{dir_acc:.0f}% direction accuracy")
        print(f"    Avg monthly P&L: Rs {avg_pnl:+,.0f}  "
              f"({profitable}/{len(monthly_pnls)} months profitable)")
        print()

    print("  NOTE: These results use DAILY-bar approximations of intraday signals.")
    print("  Intraday 5m backtests would give more precise entry/exit timing.")
    print("  The daily results are useful for signal SELECTION -- which signal")
    print("  best identifies high-momentum days -- not exact profit estimation.")
    print()
    print_divider("=")


# ===============================================================================
# CHARTS
# ===============================================================================

CHART_COLORS = {
    "ORB_Gap":        "#E07B54",   # orange-red
    "VWAP_Cross":     "#4C9BE8",   # blue
    "EMA_Cross":      "#6DBF67",   # green
    "VWAP+ORB_Combo": "#C77DFF",   # purple
}

def plot_results(df: pd.DataFrame, results: list[dict]):
    """
    Generate a 2x3 dashboard of charts and save as PNG.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")          # non-interactive backend — always works
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        from matplotlib.ticker import FuncFormatter
    except ImportError:
        print("  matplotlib not installed — skipping charts (pip install matplotlib)")
        return

    names  = [r["name"] for r in results]
    colors = [CHART_COLORS.get(n, "#888888") for n in names]

    fig = plt.figure(figsize=(18, 13), facecolor="#1A1A2E")
    fig.suptitle(
        f"NIFTY Entry Signal Comparison  |  {FROM_DATE} to {TO_DATE}  |  5 lots x 75 units",
        fontsize=14, fontweight="bold", color="white", y=0.98
    )
    gs = gridspec.GridSpec(3, 3, figure=fig, hspace=0.55, wspace=0.38)

    txt_kw  = dict(color="white")
    ax_kw   = dict(facecolor="#16213E")

    def _style(ax, title):
        ax.set_facecolor("#16213E")
        ax.tick_params(colors="white", labelsize=8)
        ax.set_title(title, color="white", fontsize=9, fontweight="bold", pad=6)
        for spine in ax.spines.values():
            spine.set_edgecolor("#444466")
        ax.yaxis.label.set_color("white")
        ax.xaxis.label.set_color("white")

    def _rs(ax):
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda x, _: f"Rs {x/1000:.0f}k" if abs(x) >= 1000 else f"Rs {x:.0f}"
        ))

    # ── Chart 1 (top-left): Signals/month vs direction accuracy  ──────────────
    ax1 = fig.add_subplot(gs[0, 0])
    _style(ax1, "Signals / Month  vs  Direction Accuracy")
    sig_pm = [r["signal_days"] / 28 for r in results]
    dirs   = [r["direction_hit_rate"] for r in results]
    scatter = ax1.scatter(sig_pm, dirs, s=180, c=colors, zorder=3, edgecolors="white", linewidths=0.6)
    ax1.axhline(52, color="#FFDD57", linestyle="--", linewidth=0.8, label="52% break-even")
    ax1.axvline(3,  color="#FFDD57", linestyle=":",  linewidth=0.8, label="3/month target")
    for i, r in enumerate(results):
        ax1.annotate(r["name"], (sig_pm[i], dirs[i]),
                     textcoords="offset points", xytext=(6, 4),
                     fontsize=7, color="white")
    ax1.set_xlabel("Signals / Month", fontsize=8, **txt_kw)
    ax1.set_ylabel("Direction Accuracy %", fontsize=8, **txt_kw)
    ax1.legend(fontsize=7, facecolor="#16213E", labelcolor="white", framealpha=0.5)

    # ── Chart 2 (top-center): Average P&L per signal  ─────────────────────────
    ax2 = fig.add_subplot(gs[0, 1])
    _style(ax2, "Avg P&L per Signal Day (Rs)")
    vals = [r["pnl_per_signal"] for r in results]
    bars = ax2.bar(names, vals, color=colors, edgecolor="#333355", linewidth=0.6)
    ax2.axhline(0, color="white", linewidth=0.5)
    for bar, v in zip(bars, vals):
        ax2.text(bar.get_x() + bar.get_width()/2, v + (500 if v >= 0 else -2500),
                 f"Rs {v:+,.0f}", ha="center", va="bottom" if v >= 0 else "top",
                 fontsize=7, color="white")
    ax2.tick_params(axis="x", rotation=15)
    _rs(ax2)

    # ── Chart 3 (top-right): Big-move detection rate  ─────────────────────────
    ax3 = fig.add_subplot(gs[0, 2])
    _style(ax3, "Big-Move Detection Rate  (200+ pt days)")
    dets = [r["big_detection_rate"] for r in results]
    bars = ax3.bar(names, dets, color=colors, edgecolor="#333355", linewidth=0.6)
    ax3.axhline(50, color="#FFDD57", linestyle="--", linewidth=0.8, label="50% ref")
    for bar, v in zip(bars, dets):
        ax3.text(bar.get_x() + bar.get_width()/2, v + 0.5,
                 f"{v:.0f}%", ha="center", va="bottom", fontsize=7, color="white")
    ax3.set_ylabel("Detection %", fontsize=8)
    ax3.tick_params(axis="x", rotation=15)
    ax3.legend(fontsize=7, facecolor="#16213E", labelcolor="white", framealpha=0.5)

    # ── Chart 4 (middle, spanning all 3 cols): Monthly P&L per signal  ────────
    ax4 = fig.add_subplot(gs[1, :])
    _style(ax4, "Monthly P&L by Signal (Rs)  — grouped bar chart")
    months_sorted = sorted({m for r in results for m in r["monthly"]})
    month_labels  = [str(m) for m in months_sorted]
    n_sig   = len(results)
    x       = np.arange(len(months_sorted))
    width   = 0.18
    offset  = (n_sig - 1) / 2 * width
    for i, r in enumerate(results):
        monthly_pnls = [r["monthly"].get(m, {}).get("pnl", 0) for m in months_sorted]
        ax4.bar(x + i * width - offset, monthly_pnls, width,
                label=r["name"], color=colors[i], edgecolor="#222244", linewidth=0.4, alpha=0.85)
    ax4.axhline(0, color="white", linewidth=0.5)
    ax4.axhline(100_000, color="#FFDD57", linestyle="--", linewidth=0.7, label="Rs 1L target")
    ax4.set_xticks(x)
    ax4.set_xticklabels(month_labels, rotation=45, ha="right", fontsize=7)
    ax4.legend(fontsize=7.5, facecolor="#16213E", labelcolor="white",
               framealpha=0.6, ncol=5, loc="upper right")
    _rs(ax4)

    # ── Chart 5 (bottom-left + center): Cumulative P&L  ──────────────────────
    ax5 = fig.add_subplot(gs[2, :2])
    _style(ax5, "Cumulative P&L Across All Signal Days (Rs)")
    for r, c in zip(results, colors):
        cum = pd.Series(r["pnl_list"]).cumsum()
        ax5.plot(range(len(cum)), cum, label=r["name"], color=c, linewidth=1.5)
    ax5.axhline(0, color="white", linewidth=0.5)
    ax5.legend(fontsize=8, facecolor="#16213E", labelcolor="white", framealpha=0.6)
    ax5.set_xlabel("Signal # (chronological)", fontsize=8)
    _rs(ax5)

    # ── Chart 6 (bottom-right): Win rate comparison  ──────────────────────────
    ax6 = fig.add_subplot(gs[2, 2])
    _style(ax6, "Win Rate % per Signal")
    win_rates = []
    for r in results:
        total = r["win_trades"] + r["loss_trades"]
        win_rates.append(r["win_trades"] / total * 100 if total else 0)
    bars = ax6.bar(names, win_rates, color=colors, edgecolor="#333355", linewidth=0.6)
    ax6.axhline(50, color="#FFDD57", linestyle="--", linewidth=0.8, label="50% ref")
    for bar, v in zip(bars, win_rates):
        ax6.text(bar.get_x() + bar.get_width()/2, v + 0.5,
                 f"{v:.0f}%", ha="center", va="bottom", fontsize=7, color="white")
    ax6.set_ylabel("Win Rate %", fontsize=8)
    ax6.tick_params(axis="x", rotation=15)
    ax6.legend(fontsize=7, facecolor="#16213E", labelcolor="white", framealpha=0.5)

    # Save
    out_dir = Path(__file__).parent / "backtest_results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"signal_comparison_{FROM_DATE}_to_{TO_DATE}.png"
    fig.savefig(out_path, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n  Chart saved --> {out_path}")
    return out_path


# ===============================================================================
# MAIN
# ===============================================================================

def main():
    print()
    print("  Loading NIFTY daily data from Yahoo Finance"
          f"  {FROM_DATE} -> {TO_DATE} ...")

    df = fetch_yahoo("NIFTY", FROM_DATE, TO_DATE, interval=0, verbose=True)
    if df.empty:
        print("  ERROR: No data loaded. Check internet connection.")
        sys.exit(1)

    print(f"  Loaded {len(df)} bars")
    print()

    print("  Running signal analysis ...")
    results = []
    for name, fn in SIGNALS.items():
        print(f"    - {name} ...", end=" ", flush=True)
        r = analyse_signal(df, fn, name)
        results.append(r)
        print(f"{r['signal_days']} signals  |  "
              f"{r['big_detection_rate']:.0f}% big-move detect  |  "
              f"{r['direction_hit_rate']:.0f}% dir accuracy")

    print_report(df, results)
    plot_results(df, results)


if __name__ == "__main__":
    main()
