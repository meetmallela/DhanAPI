"""
pattern_discovery.py
--------------------
Discovers and scores 5 trading patterns in the training window (recent 6 months).

Uses hist_cache.db populated by historical_data_fetch.py.

Patterns analyzed:
  1. ExpiryBlast   -- Expiry-day late-session ATR squeeze -> CE/PE blast
  2. ORB15         -- 15-minute opening range breakout
  3. GapFill       -- Gap >0.3% that fills within 90 minutes
  4. ATRSqueeze    -- Bollinger Band squeeze -> directional expansion
  5. VWAPReclaim   -- Price crosses VWAP with directional momentum

Run:
    python pattern_discovery.py
    python pattern_discovery.py --symbol BANKNIFTY --months 6

Output:
    MasterConfiguration/reports/pattern_discovery_<DATE>.md
    backtest/pattern_stats.json  (used by pattern_backtest.py)
"""

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

_ROOT   = Path(__file__).parent
_MASTER = _ROOT.parent / "MasterConfiguration"
_LIB    = _MASTER / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

from historical_data_fetch import load

REPORT_DIR = _MASTER / "reports"
STATS_FILE = _ROOT / "backtest" / "pattern_stats.json"

# NSE expiry days -- weekly NIFTY: Tuesday, BANKNIFTY: Wednesday
# (These changed in 2025; Tuesday confirmed for NIFTY from NiftyBlast1 analysis)
_EXPIRY_WEEKDAY = {
    "NIFTY":      1,   # Tuesday
    "BANKNIFTY":  2,   # Wednesday
    "FINNIFTY":   1,   # Tuesday
    "SENSEX":     1,   # Tuesday (BSE moved SENSEX weekly to Tuesday in 2024)
}


# -- Indicator helpers ---------------------------------------------------------

def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, pc = df["high"], df["low"], df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain  = delta.clip(lower=0).ewm(span=period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _bb(close: pd.Series, period: int = 20, std: float = 2.0):
    mid   = close.rolling(period).mean()
    sigma = close.rolling(period).std()
    return mid - std * sigma, mid, mid + std * sigma


def _ema(close: pd.Series, period: int) -> pd.Series:
    return close.ewm(span=period, adjust=False).mean()


def _vwap_daily(df: pd.DataFrame) -> pd.Series:
    """Intraday VWAP (resets each day). For zero-volume indices uses TWAP."""
    out = pd.Series(np.nan, index=df.index)
    for date, grp in df.groupby(df["timestamp"].dt.date):
        idx = grp.index
        vol = grp["volume"]
        if vol.sum() > 0:
            tp  = (grp["high"] + grp["low"] + grp["close"]) / 3
            out.loc[idx] = (tp * vol).cumsum() / vol.cumsum()
        else:
            tp       = (grp["high"] + grp["low"] + grp["close"]) / 3
            out.loc[idx] = tp.expanding().mean()
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["atr14"]  = _atr(df, 14)
    df["rsi14"]  = _rsi(df["close"], 14)
    df["ema9"]   = _ema(df["close"], 9)
    df["ema21"]  = _ema(df["close"], 21)
    df["ema50"]  = _ema(df["close"], 50)
    bb_lo, bb_mid, bb_hi = _bb(df["close"], 20, 2.0)
    df["bb_lo"]  = bb_lo
    df["bb_mid"] = bb_mid
    df["bb_hi"]  = bb_hi
    df["bb_pct"] = (df["close"] - bb_lo) / (bb_hi - bb_lo).replace(0, np.nan)
    df["bb_wid"] = (bb_hi - bb_lo) / bb_mid.replace(0, np.nan)
    df["vwap"]   = _vwap_daily(df)
    df["body"]   = (df["close"] - df["open"]).abs()
    df["range"]  = df["high"] - df["low"]
    df["body_pct"] = df["body"] / df["range"].replace(0, np.nan)
    return df


# -- Pattern 1: ExpiryBlast ----------------------------------------------------

def find_expiry_blast(df5m: pd.DataFrame, symbol: str,
                      forward_bars: int = 6) -> pd.DataFrame:
    """
    Setup: Expiry day, 14:55 bar is the coiling/setup bar (RSI neutral, BB mid).
    Signal: 15:00 5m bar is the BLAST bar — strong directional move > 1.5x ATR.
    Outcome: max favourable excursion in forward_bars bars after 15:00.

    NiftyBlast1 reference: 2026-05-18, NIFTY +52 pts at 15:00 (3.6x ATR on 1m).
    On 5m basis the 15:00 bar captures the first minute blast + 4 min follow-through.
    """
    exp_wd   = _EXPIRY_WEEKDAY.get(symbol, 1)
    records  = []
    grp_days = df5m.groupby(df5m["timestamp"].dt.date)

    for day, dg in grp_days:
        if pd.Timestamp(day).weekday() != exp_wd:
            continue
        dg = dg.sort_values("timestamp").reset_index(drop=True)

        # Pre-blast setup bar: 14:55 (the coiling / compression bar)
        pre_rows = dg[
            (dg["timestamp"].dt.hour == 14) &
            (dg["timestamp"].dt.minute == 55)
        ]
        if pre_rows.empty:
            continue
        last_pre = pre_rows.iloc[0]

        # Setup checks: RSI neutral, price not already extended
        if not (30 <= last_pre["rsi14"] <= 70):
            continue
        bb_pct = last_pre["bb_pct"]
        if pd.isna(bb_pct) or not (0.15 <= bb_pct <= 0.85):
            continue

        # Signal bar: the 15:00 5m bar (hour=15, minute=0) — this is the BLAST
        sig_rows = dg[
            (dg["timestamp"].dt.hour == 15) &
            (dg["timestamp"].dt.minute == 0)
        ]
        if sig_rows.empty:
            continue
        sig = sig_rows.iloc[0]

        # Blast confirmation: directional move > 1.5x ATR14 of setup bar, body > 0.60
        move = abs(sig["close"] - sig["open"])
        if move < 1.5 * last_pre["atr14"] or sig["body_pct"] < 0.60:
            # No blast on this day -- record as MISS (for base-rate context)
            continue

        direction = "BULLISH" if sig["close"] > sig["open"] else "BEARISH"

        # Outcome: max favourable excursion in bars after the blast bar
        sig_pos   = sig_rows.index[0]   # position in dg
        fwd_start = sig_pos + 1
        fwd_end   = min(fwd_start + forward_bars, len(dg))
        fwd_bars  = dg.iloc[fwd_start:fwd_end]
        if fwd_bars.empty:
            continue

        if direction == "BULLISH":
            outcome_pts = fwd_bars["high"].max() - sig["close"]
        else:
            outcome_pts = sig["close"] - fwd_bars["low"].min()

        records.append({
            "date":       str(day),
            "pattern":    "ExpiryBlast",
            "symbol":     symbol,
            "time":       str(sig["timestamp"].time()),
            "direction":  direction,
            "entry_price":sig["close"],
            "atr":        round(last_pre["atr14"], 2),
            "rsi_pre":    round(last_pre["rsi14"], 1),
            "bb_pct_pre": round(last_pre["bb_pct"], 3),
            "move_pts":   round(move, 1),
            "move_atr":   round(move / last_pre["atr14"], 2),
            "outcome_pts":round(outcome_pts, 1),
            "win":        outcome_pts > last_pre["atr14"],
        })

    return pd.DataFrame(records)


# -- Pattern 2: ORB15 ----------------------------------------------------------

def find_orb15(df5m: pd.DataFrame, symbol: str,
               forward_bars: int = 6) -> pd.DataFrame:
    """
    Opening range = first 15 min (3 x 5m bars after 9:15 IST).
    Breakout: close above ORB high (bullish) or below ORB low (bearish).
    SL: opposite ORB level. TP check: next forward_bars bars.
    """
    records  = []
    grp_days = df5m.groupby(df5m["timestamp"].dt.date)

    for day, dg in grp_days:
        dg = dg.sort_values("timestamp").reset_index(drop=True)
        # ORB window: 9:15, 9:20, 9:25 (first three 5m bars)
        orb_bars = dg[
            (dg["timestamp"].dt.hour == 9) &
            (dg["timestamp"].dt.minute.isin([15, 20, 25]))
        ]
        if len(orb_bars) < 3:
            continue

        orb_high = orb_bars["high"].max()
        orb_low  = orb_bars["low"].min()
        orb_rng  = orb_high - orb_low

        # Post-ORB bars (from 9:30 onwards)
        post = dg[dg["timestamp"].dt.time >= pd.Timestamp("09:30").time()].reset_index(drop=True)
        if post.empty:
            continue

        # Find first breakout bar
        for i, bar in post.iterrows():
            if bar["rsi14"] > 70 or bar["rsi14"] < 30:
                continue   # already extended
            if bar["close"] > orb_high and bar["ema9"] > bar["ema21"]:
                direction = "BULLISH"
            elif bar["close"] < orb_low and bar["ema9"] < bar["ema21"]:
                direction = "BEARISH"
            else:
                continue

            fwd = post.iloc[i + 1: i + 1 + forward_bars]
            if fwd.empty:
                break

            if direction == "BULLISH":
                outcome_pts = fwd["high"].max() - bar["close"]
                sl_dist     = bar["close"] - orb_low
            else:
                outcome_pts = bar["close"] - fwd["low"].min()
                sl_dist     = orb_high - bar["close"]

            records.append({
                "date":       str(day),
                "pattern":    "ORB15",
                "symbol":     symbol,
                "time":       str(bar["timestamp"].time()),
                "direction":  direction,
                "entry_price":bar["close"],
                "orb_range":  round(orb_rng, 1),
                "atr":        round(bar["atr14"], 2),
                "rsi":        round(bar["rsi14"], 1),
                "sl_dist":    round(sl_dist, 1),
                "outcome_pts":round(outcome_pts, 1),
                "win":        outcome_pts > bar["atr14"],
            })
            break   # one signal per day per pattern

    return pd.DataFrame(records)


# -- Pattern 3: GapFill --------------------------------------------------------

def find_gap_fill(df5m: pd.DataFrame, symbol: str,
                  forward_bars: int = 18) -> pd.DataFrame:
    """
    Gap detected by comparing first 5m bar open vs previous day close.
    Gap > 0.3%: expect mean reversion (fill) within 90 min (18 x 5m bars).
    """
    records  = []
    grp_days = df5m.groupby(df5m["timestamp"].dt.date)
    dates    = sorted(grp_days.groups.keys())

    for i in range(1, len(dates)):
        prev_day_df = grp_days.get_group(dates[i - 1])
        curr_day_df = grp_days.get_group(dates[i])

        prev_close = prev_day_df.iloc[-1]["close"]
        first_open = curr_day_df.iloc[0]["open"]

        gap_pct = (first_open - prev_close) / prev_close * 100
        if abs(gap_pct) < 0.30:
            continue

        direction   = "SHORT" if gap_pct > 0 else "LONG"   # trade the fill
        entry_price = first_open
        fill_price  = prev_close

        fwd = curr_day_df.iloc[:forward_bars]

        if direction == "SHORT":
            filled = (fwd["low"] <= fill_price).any()
            if filled:
                fill_bar  = fwd[fwd["low"] <= fill_price].iloc[0]
                fill_time = fill_bar["timestamp"]
                outcome_pts = first_open - fill_price
            else:
                fill_time   = None
                # Partial: how far did it close?
                outcome_pts = first_open - fwd["close"].iloc[-1]
        else:
            filled = (fwd["high"] >= fill_price).any()
            if filled:
                fill_bar  = fwd[fwd["high"] >= fill_price].iloc[0]
                fill_time = fill_bar["timestamp"]
                outcome_pts = fill_price - first_open
            else:
                fill_time   = None
                outcome_pts = fwd["close"].iloc[-1] - first_open

        records.append({
            "date":       str(dates[i]),
            "pattern":    "GapFill",
            "symbol":     symbol,
            "time":       str(curr_day_df.iloc[0]["timestamp"].time()),
            "direction":  direction,
            "entry_price":round(entry_price, 1),
            "prev_close": round(prev_close, 1),
            "gap_pct":    round(gap_pct, 2),
            "filled":     filled,
            "fill_time":  str(fill_time.time()) if fill_time is not None else "no",
            "outcome_pts":round(outcome_pts, 1),
            "win":        filled or outcome_pts > 0,
        })

    return pd.DataFrame(records)


# -- Pattern 4: ATR Squeeze ----------------------------------------------------

def find_atr_squeeze(df5m: pd.DataFrame, symbol: str,
                     lookback: int = 50, forward_bars: int = 12) -> pd.DataFrame:
    """
    Bollinger Band squeeze: bb_width < 20th percentile of recent lookback bars.
    Breakout: close above/below BB mid + EMA alignment.
    """
    records   = []
    df        = df5m.copy()
    wid_pctile = df["bb_wid"].rolling(lookback).quantile(0.20)

    squeeze_mask = df["bb_wid"] < wid_pctile

    for i in range(lookback, len(df) - forward_bars):
        if not squeeze_mask.iloc[i]:
            continue
        bar     = df.iloc[i]
        prev    = df.iloc[i - 1]

        # Breakout from squeeze
        if bar["close"] > bar["bb_mid"] and prev["close"] <= prev["bb_mid"]:
            direction = "BULLISH"
        elif bar["close"] < bar["bb_mid"] and prev["close"] >= prev["bb_mid"]:
            direction = "BEARISH"
        else:
            continue

        # RSI not already exhausted
        if direction == "BULLISH" and bar["rsi14"] > 72:
            continue
        if direction == "BEARISH" and bar["rsi14"] < 28:
            continue

        fwd = df.iloc[i + 1: i + 1 + forward_bars]
        if direction == "BULLISH":
            outcome_pts = fwd["high"].max() - bar["close"]
        else:
            outcome_pts = bar["close"] - fwd["low"].min()

        sl_est = bar["atr14"] * 1.5

        records.append({
            "date":       bar["timestamp"].strftime("%Y-%m-%d"),
            "pattern":    "ATRSqueeze",
            "symbol":     symbol,
            "time":       str(bar["timestamp"].time()),
            "direction":  direction,
            "entry_price":round(bar["close"], 1),
            "bb_wid":     round(bar["bb_wid"], 4),
            "atr":        round(bar["atr14"], 2),
            "rsi":        round(bar["rsi14"], 1),
            "outcome_pts":round(outcome_pts, 1),
            "win":        outcome_pts > sl_est,
        })

    return pd.DataFrame(records)


# -- Pattern 5: VWAP Reclaim ---------------------------------------------------

def find_vwap_reclaim(df5m: pd.DataFrame, symbol: str,
                      forward_bars: int = 6) -> pd.DataFrame:
    """
    Price was below VWAP for >=3 consecutive bars, then closes above it (reclaim).
    Confirmation: RSI > 45. Time gate: 9:30-14:30.
    """
    records  = []
    df       = df5m.copy()
    below    = df["close"] < df["vwap"]
    consec_below = below.rolling(3).sum()

    for i in range(3, len(df) - forward_bars):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = bar["timestamp"]

        # Time gate
        if not (9 <= ts.hour <= 14) or (ts.hour == 14 and ts.minute > 30):
            continue

        # Reclaim: was below, now above
        if not (consec_below.iloc[i - 1] >= 3):
            continue
        if not (bar["close"] > bar["vwap"] and prev["close"] <= prev["vwap"]):
            continue
        if bar["rsi14"] < 45:
            continue

        fwd = df.iloc[i + 1: i + 1 + forward_bars]
        outcome_pts = fwd["high"].max() - bar["close"]

        records.append({
            "date":       ts.strftime("%Y-%m-%d"),
            "pattern":    "VWAPReclaim",
            "symbol":     symbol,
            "time":       str(ts.time()),
            "direction":  "BULLISH",
            "entry_price":round(bar["close"], 1),
            "vwap":       round(bar["vwap"], 1),
            "dist_vwap":  round(bar["close"] - bar["vwap"], 1),
            "atr":        round(bar["atr14"], 2),
            "rsi":        round(bar["rsi14"], 1),
            "outcome_pts":round(outcome_pts, 1),
            "win":        outcome_pts > bar["atr14"],
        })

    # Also find VWAP rejections (above -> below)
    above       = df["close"] > df["vwap"]
    consec_above = above.rolling(3).sum()

    for i in range(3, len(df) - forward_bars):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = bar["timestamp"]
        if not (9 <= ts.hour <= 14) or (ts.hour == 14 and ts.minute > 30):
            continue
        if not (consec_above.iloc[i - 1] >= 3):
            continue
        if not (bar["close"] < bar["vwap"] and prev["close"] >= prev["vwap"]):
            continue
        if bar["rsi14"] > 55:
            continue

        fwd = df.iloc[i + 1: i + 1 + forward_bars]
        outcome_pts = bar["close"] - fwd["low"].min()

        records.append({
            "date":       ts.strftime("%Y-%m-%d"),
            "pattern":    "VWAPRejection",
            "symbol":     symbol,
            "time":       str(ts.time()),
            "direction":  "BEARISH",
            "entry_price":round(bar["close"], 1),
            "vwap":       round(bar["vwap"], 1),
            "dist_vwap":  round(bar["vwap"] - bar["close"], 1),
            "atr":        round(bar["atr14"], 2),
            "rsi":        round(bar["rsi14"], 1),
            "outcome_pts":round(outcome_pts, 1),
            "win":        outcome_pts > bar["atr14"],
        })

    return pd.DataFrame(records)


# -- Stats summary -------------------------------------------------------------

def summarise(df_trades: pd.DataFrame) -> dict:
    if df_trades.empty:
        return {"count": 0, "win_rate": 0, "avg_win_pts": 0,
                "avg_loss_pts": 0, "expectancy_pts": 0}
    wins  = df_trades[df_trades["win"]]
    loss  = df_trades[~df_trades["win"]]
    wr    = len(wins) / len(df_trades) * 100
    avg_w = wins["outcome_pts"].mean() if not wins.empty else 0
    avg_l = loss["outcome_pts"].mean() if not loss.empty else 0
    exp   = (wr / 100) * avg_w - (1 - wr / 100) * abs(avg_l)
    return {
        "count":         len(df_trades),
        "win_count":     len(wins),
        "loss_count":    len(loss),
        "win_rate":      round(wr, 1),
        "avg_win_pts":   round(avg_w, 1),
        "avg_loss_pts":  round(avg_l, 1),
        "expectancy_pts":round(exp, 1),
        "best_pts":      round(df_trades["outcome_pts"].max(), 1),
        "worst_pts":     round(df_trades["outcome_pts"].min(), 1),
    }


# -- Report writer -------------------------------------------------------------

def write_report(all_results: dict, symbol: str, from_date: str, to_date: str):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")
    path = REPORT_DIR / f"pattern_discovery_{symbol}_{today_str}.md"

    lines = [
        f"# Pattern Discovery -- {symbol}",
        f"**Training window:** {from_date} -> {to_date}",
        f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M IST')}",
        "",
    ]
    for pattern, data in all_results.items():
        s = data["stats"]
        lines += [
            f"## {pattern}",
            f"- Count: {s['count']} | Win rate: **{s['win_rate']}%** | Expectancy: {s['expectancy_pts']} pts",
            f"- Avg win: {s['avg_win_pts']} pts | Avg loss: {s['avg_loss_pts']} pts",
            f"- Best: {s.get('best_pts','?')} pts | Worst: {s.get('worst_pts','?')} pts",
            "",
        ]
        df_t = data.get("trades")
        if df_t is not None and not df_t.empty:
            lines.append("**Sample trades (last 5):**")
            lines.append("```")
            cols = [c for c in ["date", "time", "direction", "entry_price",
                                 "outcome_pts", "win"] if c in df_t.columns]
            lines.append(df_t.tail(5)[cols].to_string(index=False))
            lines.append("```")
            lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")
    print(f"Report: {path}")
    return path


# -- Main ----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Discover patterns in training data")
    parser.add_argument("--symbol", default="NIFTY")
    parser.add_argument("--months", type=int, default=6,
                        help="Training window in months (default: 6)")
    args = parser.parse_args()

    today     = datetime.now()
    to_date   = today.strftime("%Y-%m-%d")
    from_date = (today - timedelta(days=args.months * 31)).strftime("%Y-%m-%d")

    print(f"=== Pattern Discovery: {args.symbol} ===")
    print(f"Training window: {from_date} -> {to_date}")
    print()

    # Load 5m candles
    df5m = load(args.symbol, "5min", from_date, to_date)
    if df5m.empty:
        print("No 5m data in cache. Run historical_data_fetch.py first.")
        sys.exit(1)

    print(f"Loaded {len(df5m)} x 5m bars for {args.symbol}")
    df5m = df5m.sort_values("timestamp").reset_index(drop=True)
    df5m = add_indicators(df5m)
    print(f"Indicators computed. Running pattern scans...\n")

    pattern_fns = [
        ("ExpiryBlast",  lambda df: find_expiry_blast(df, args.symbol)),
        ("ORB15",        lambda df: find_orb15(df, args.symbol)),
        ("GapFill",      lambda df: find_gap_fill(df, args.symbol)),
        ("ATRSqueeze",   lambda df: find_atr_squeeze(df, args.symbol)),
        ("VWAPReclaim",  lambda df: find_vwap_reclaim(df, args.symbol)),
    ]

    all_results = {}
    all_stats   = {}

    for name, fn in pattern_fns:
        df_trades = fn(df5m)
        s         = summarise(df_trades)
        all_results[name] = {"stats": s, "trades": df_trades}
        all_stats[name]   = s
        verdict = "OK EDGE" if s["expectancy_pts"] > 5 and s["win_rate"] >= 45 else \
                  "~ MARGINAL" if s["expectancy_pts"] > 0 else "X WEAK"
        print(f"  {name:15s}: {s['count']:4d} signals | WR={s['win_rate']:5.1f}% | "
              f"Exp={s['expectancy_pts']:+6.1f}pts  {verdict}")

    print()

    # Save stats for backtester
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "symbol":     args.symbol,
        "from_date":  from_date,
        "to_date":    to_date,
        "generated":  datetime.now().isoformat(),
        "patterns":   all_stats,
    }
    STATS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"Stats saved: {STATS_FILE}")

    write_report(all_results, args.symbol, from_date, to_date)
    print("\nNext: run pattern_backtest.py to validate on months 7-12")


if __name__ == "__main__":
    main()
