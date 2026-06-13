"""
pattern_discovery_v2.py
-----------------------
Improved v2 versions of all 5 original patterns + new GammaSqueeze strategy.

Changes from v1:
  ExpiryBlast_v2  : 14:45-15:15 window; BB-width pre-coiling filter; BANKNIFTY+SENSEX only
  ORB15_v2        : gap filter <0.5%; 3-bar/6-bar tiered hold; NIFTY+BANKNIFTY only
  GapFill_v2      : BANKNIFTY+SENSEX only; NIFTY disabled; v1 immediate-open entry retained (5-min filter reverted per 12:36 assessment)
  ATRSqueeze_v2   : volume 1.8x SMA confirmation; VIX-adaptive percentile parameter
  VWAPReclaim_v2  : 3-bar confirm (NIFTY) / 5-bar (BANKNIFTY,SENSEX); body% > 0.65
  GammaSqueeze    : NEW -- ADX>45, RSI 35-50, 3-bar vol/price surge; Mon/Wed/Fri 14:45-15:15
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT   = Path(__file__).parent
_MASTER = _ROOT.parent / "MasterConfiguration"
_LIB    = _MASTER / "lib"
if str(_LIB) not in sys.path:
    sys.path.insert(0, str(_LIB))

# Re-export helpers from v1 so callers can import from one place
from pattern_discovery import (
    add_indicators,
    summarise,
    _atr,
    _rsi,
    _bb,
    _ema,
    _vwap_daily,
    _EXPIRY_WEEKDAY,
)
from historical_data_fetch import load  # noqa: F401


# ---------------------------------------------------------------------------
# ADX helper
# ---------------------------------------------------------------------------

def _adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    up   = high.diff()
    down = -low.diff()
    plus_dm  = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    tr = pd.concat(
        [high - low, (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    atr_s    = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr_s.replace(0, np.nan)
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr_s.replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(span=period, adjust=False).mean()


def add_indicators_v2(df: pd.DataFrame) -> pd.DataFrame:
    """add_indicators plus ADX14."""
    df = add_indicators(df)
    df["adx14"] = _adx(df, 14)
    return df


# ---------------------------------------------------------------------------
# Pattern 1 v2: ExpiryBlast_v2
# ---------------------------------------------------------------------------

def find_expiry_blast_v2(df5m: pd.DataFrame, symbol: str,
                         forward_bars: int = 6) -> pd.DataFrame:
    """
    ExpiryBlast v2.
    - Window    : any 5m bar 14:45-15:15 IST (was rigid 15:00)
    - Pre-coil  : BB width of setup bar < 15th-percentile of last 50 bars
    - Symbol    : BANKNIFTY or SENSEX only (skip NIFTY)
    - Keep      : move > 1.5x ATR14, body% > 0.60, RSI 30-70
    """
    if symbol == "NIFTY":
        return pd.DataFrame()

    exp_wd  = _EXPIRY_WEEKDAY.get(symbol, 1)
    records = []

    # Pre-compute 25th-percentile BB width rolling window (50 bars)
    # Relaxed from 15th -> 25th per re-assessment: increases signal frequency in low-VIX environments
    df = df5m.copy()
    bb_wid_pctile15 = df["bb_wid"].rolling(50).quantile(0.25)

    grp_days = df5m.groupby(df5m["timestamp"].dt.date)

    for day, dg in grp_days:
        if pd.Timestamp(day).weekday() != exp_wd:
            continue
        dg = dg.sort_values("timestamp").reset_index(drop=True)

        # Candidate blast bars: 14:45 through 15:15
        blast_cands = dg[
            (
                (dg["timestamp"].dt.hour == 14) & (dg["timestamp"].dt.minute >= 45)
            ) | (
                (dg["timestamp"].dt.hour == 15) & (dg["timestamp"].dt.minute <= 15)
            )
        ]
        if blast_cands.empty:
            continue

        for _, sig in blast_cands.iterrows():
            # --- Setup-bar checks (the bar itself is the setup + signal bar) ---
            if not (30 <= sig["rsi14"] <= 70):
                continue

            bb_pct_val = sig["bb_pct"]
            if pd.isna(bb_pct_val) or not (0.15 <= bb_pct_val <= 0.85):
                continue

            # BB width pre-coiling filter
            # Look up the rolling 15th-pctile for this bar's position in df
            bar_idx = df.index[df["timestamp"] == sig["timestamp"]]
            if bar_idx.empty:
                continue
            pctile_val = bb_wid_pctile15.loc[bar_idx[0]]
            if pd.isna(pctile_val):
                continue
            if sig["bb_wid"] >= pctile_val:
                continue   # not compressed enough

            # Blast confirmation
            move = abs(sig["close"] - sig["open"])
            if move < 1.5 * sig["atr14"] or sig["body_pct"] < 0.60:
                continue

            direction = "BULLISH" if sig["close"] > sig["open"] else "BEARISH"

            # Outcome: forward_bars after this bar
            sig_pos   = dg.index[dg["timestamp"] == sig["timestamp"]]
            if sig_pos.empty:
                continue
            sig_loc   = dg.index.get_loc(sig_pos[0])
            fwd_start = sig_loc + 1
            fwd_end   = min(fwd_start + forward_bars, len(dg))
            fwd_bars  = dg.iloc[fwd_start:fwd_end]
            if fwd_bars.empty:
                continue

            if direction == "BULLISH":
                outcome_pts = fwd_bars["high"].max() - sig["close"]
            else:
                outcome_pts = sig["close"] - fwd_bars["low"].min()

            records.append({
                "date":        str(day),
                "pattern":     "ExpiryBlast_v2",
                "symbol":      symbol,
                "time":        str(sig["timestamp"].time()),
                "direction":   direction,
                "entry_price": sig["close"],
                "atr":         round(sig["atr14"], 2),
                "rsi_pre":     round(sig["rsi14"], 1),
                "bb_pct_pre":  round(bb_pct_val, 3),
                "bb_wid":      round(sig["bb_wid"], 4),
                "bb_wid_p15":  round(pctile_val, 4),
                "move_pts":    round(move, 1),
                "move_atr":    round(move / sig["atr14"], 2) if sig["atr14"] else 0,
                "outcome_pts": round(outcome_pts, 1),
                "win":         outcome_pts > sig["atr14"],
                "skipped":     False,
            })
            break   # first qualifying bar wins

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Pattern 2 v2: ORB15_v2
# ---------------------------------------------------------------------------

def find_orb15_v2(df5m: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    ORB15 v2 -- per ORB15_Optimization_Report_31May2026.md
    Ultra-Low Drawdown preset (smooth capital growth, WR>60%, tight DD).

    - ORB window  : 5-minute (first 9:15 bar only)
    - Gap filter  : 0.3% max gap vs prev close
    - Sluggish cut: if price moves < 0.3x ATR in 15 min -> exit at 15-min mark
    - TP          : 1.0x ATR (NIFTY) / 1.5x ATR (BANKNIFTY)
    - SL          : 1.5x ATR (structural volatility cushion, both symbols)
    - SENSEX      : disabled (no edge confirmed)
    """
    if symbol == "SENSEX":
        return pd.DataFrame()

    tp_mult = 1.0 if symbol == "NIFTY" else 1.5

    records  = []
    grp_days = df5m.groupby(df5m["timestamp"].dt.date)
    dates    = sorted(grp_days.groups.keys())

    for i, day in enumerate(dates):
        if i == 0:
            continue

        prev_close = grp_days.get_group(dates[i - 1]).iloc[-1]["close"]
        dg         = grp_days.get_group(day).sort_values("timestamp").reset_index(drop=True)

        # Gap filter: 0.3% max
        first_open = dg.iloc[0]["open"]
        gap_pct    = abs((first_open - prev_close) / prev_close * 100)
        if gap_pct > 0.30:
            records.append({
                "date": str(day), "pattern": "ORB15_v2", "symbol": symbol,
                "skipped": True, "skip_reason": f"gap={gap_pct:.2f}%>0.3%",
                "win": False, "outcome_pts": 0,
            })
            continue

        # ORB = first 5-minute bar (9:15)
        orb_rows = dg[(dg["timestamp"].dt.hour == 9) & (dg["timestamp"].dt.minute == 15)]
        if orb_rows.empty:
            continue
        orb      = orb_rows.iloc[0]
        orb_high = orb["high"]
        orb_low  = orb["low"]
        orb_rng  = orb_high - orb_low

        # Scan for first breakout bar from 9:20 onwards
        post = dg[dg["timestamp"].dt.time > pd.Timestamp("09:15").time()].reset_index(drop=True)
        if post.empty:
            continue

        for i_bar, bar in post.iterrows():
            if bar["rsi14"] > 70 or bar["rsi14"] < 30:
                continue
            if bar["close"] > orb_high and bar["ema9"] > bar["ema21"]:
                direction = "BULLISH"
            elif bar["close"] < orb_low and bar["ema9"] < bar["ema21"]:
                direction = "BEARISH"
            else:
                continue

            atr   = bar["atr14"]
            tp    = atr * tp_mult     # profit target in pts
            sl    = atr * 1.5         # stop loss in pts (volatility-based, not opposite ORB)

            fwd3  = post.iloc[i_bar + 1: i_bar + 4]   # 15 min
            fwd6  = post.iloc[i_bar + 1: i_bar + 7]   # 30 min

            if fwd3.empty:
                break

            if direction == "BULLISH":
                fav3 = fwd3["high"].max() - bar["close"] if not fwd3.empty else 0
                adv3 = bar["close"] - fwd3["low"].min() if not fwd3.empty else 0
                fav6 = fwd6["high"].max() - bar["close"] if not fwd6.empty else fav3
            else:
                fav3 = bar["close"] - fwd3["low"].min() if not fwd3.empty else 0
                adv3 = fwd3["high"].max() - bar["close"] if not fwd3.empty else 0
                fav6 = bar["close"] - fwd6["low"].min() if not fwd6.empty else fav3

            # Outcome logic:
            # 1. TP hit within 15 min -> WIN
            # 2. SL hit within 15 min before TP -> LOSS
            # 3. Sluggish (<0.3 ATR move in 15 min) -> cut at 15m, small loss/gain
            # 4. Neither after 15 min -> hold to 30 min, check TP again
            # Sluggish cut: if 15-min move < 0.3 ATR, exit early (prevents stagnant reversals)
            if fav3 < atr * 0.3 and adv3 < sl:
                outcome_pts = fav3 - adv3 * 0.5   # exit near 15m close (approximate)
                win = outcome_pts > 0
            elif adv3 >= sl:
                # Stopped out within 15 min
                outcome_pts = -sl
                win = False
            elif fav3 >= tp:
                # TP hit within 15 min
                outcome_pts = tp
                win = True
            elif fav6 >= tp:
                # TP hit within 30 min
                outcome_pts = tp
                win = True
            elif adv3 >= sl:
                # Stopped out 15-30 min
                outcome_pts = -sl
                win = False
            else:
                # Exit at 30-min mark: win if moved > 0.5x TP in our favor
                outcome_pts = fav6 - adv3 * 0.3
                win = fav6 >= tp * 0.5

            records.append({
                "date":        str(day),
                "pattern":     "ORB15_v2",
                "symbol":      symbol,
                "time":        str(bar["timestamp"].time()),
                "direction":   direction,
                "entry_price": round(bar["close"], 1),
                "orb_high":    round(orb_high, 1),
                "orb_low":     round(orb_low, 1),
                "orb_range":   round(orb_rng, 1),
                "gap_pct":     round(gap_pct, 2),
                "atr":         round(atr, 2),
                "tp_pts":      round(tp, 1),
                "sl_pts":      round(sl, 1),
                "fav3":        round(fav3, 1),
                "fav6":        round(fav6, 1),
                "outcome_pts": round(outcome_pts, 1),
                "win":         win,
                "skipped":     False,
            })
            break

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Pattern 3 v2: GapFill_v2
# ---------------------------------------------------------------------------

def find_gap_fill_v2(df5m: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    GapFill v2 -- per GapFill_Optimization_Report_31May2026.md

    NIFTY    : RE-ENABLED. Gap >= 0.35%. Immediate open entry.
               Target: 60% of gap (partial fill). SL: 3.0x ATR14. Hold: 120 min (24 bars).
    BANKNIFTY: Gap band 0.35%-0.6% (exclude large gaps >0.6%). 5-min reversal entry.
               Target: 60% of gap. SL: 1.0x ATR14. Hold: 60 min (12 bars).
    SENSEX   : Unchanged. Gap >= 0.3%. Immediate open. Target: prev close (100%). Hold: 90 min.
    """
    # Per-symbol config
    # Re-assessment (v104 -> v105):
    # BANKNIFTY gap band changed 0.35-0.6% -> 0.5-1.0% (sweet spot bucket, +30 pts exp)
    # Wed(2) and Fri(4) disabled for all GapFill symbols (massive -36 to -55 pts DoW drag)
    DISABLED_WEEKDAYS = {2, 4}   # Wednesday, Friday
    CFG = {
        "NIFTY":     dict(min_gap=0.35, max_gap=99.0, entry="immediate",
                          target_pct=0.60, sl_atr=3.0, hold_bars=24),
        "BANKNIFTY": dict(min_gap=0.50, max_gap=1.00,  entry="reversal_5m",
                          target_pct=0.60, sl_atr=1.0, hold_bars=12),
        "SENSEX":    dict(min_gap=0.30, max_gap=99.0, entry="immediate",
                          target_pct=1.00, sl_atr=1.5, hold_bars=18),
    }
    cfg = CFG.get(symbol)
    if cfg is None:
        return pd.DataFrame()

    records  = []
    grp_days = df5m.groupby(df5m["timestamp"].dt.date)
    dates    = sorted(grp_days.groups.keys())

    for i in range(1, len(dates)):
        prev_df  = grp_days.get_group(dates[i - 1])
        curr_df  = grp_days.get_group(dates[i]).sort_values("timestamp").reset_index(drop=True)

        prev_close = prev_df.iloc[-1]["close"]
        first_bar  = curr_df.iloc[0]
        first_open = first_bar["open"]
        gap_abs    = abs(first_open - prev_close)
        gap_pct    = (first_open - prev_close) / prev_close * 100

        # Day-of-week filter: disable Wed(2) and Fri(4) -- massive negative DoW drag
        if pd.Timestamp(dates[i]).weekday() in DISABLED_WEEKDAYS:
            records.append({"date": str(dates[i]), "pattern": "GapFill_v2",
                             "symbol": symbol, "skipped": True,
                             "skip_reason": "Wed_Fri_disabled", "gap_pct": round(gap_pct, 2),
                             "win": False, "outcome_pts": 0})
            continue

        # Gap size band filter
        if abs(gap_pct) < cfg["min_gap"] or abs(gap_pct) > cfg["max_gap"]:
            continue

        direction = "SHORT" if gap_pct > 0 else "LONG"

        # Entry logic
        if cfg["entry"] == "reversal_5m":
            # BANKNIFTY: first 5m bar must close against the gap direction
            if direction == "SHORT" and first_bar["close"] >= first_bar["open"]:
                records.append({"date": str(dates[i]), "pattern": "GapFill_v2",
                                 "symbol": symbol, "skipped": True,
                                 "skip_reason": "5m_no_reversal", "gap_pct": round(gap_pct, 2),
                                 "win": False, "outcome_pts": 0})
                continue
            if direction == "LONG" and first_bar["close"] <= first_bar["open"]:
                records.append({"date": str(dates[i]), "pattern": "GapFill_v2",
                                 "symbol": symbol, "skipped": True,
                                 "skip_reason": "5m_no_reversal", "gap_pct": round(gap_pct, 2),
                                 "win": False, "outcome_pts": 0})
                continue
            entry_price = first_bar["close"]
            fwd         = curr_df.iloc[1: cfg["hold_bars"] + 1]
        else:
            entry_price = first_open
            fwd         = curr_df.iloc[: cfg["hold_bars"]]

        if fwd.empty:
            continue

        # Target: partial fill (60% or 100% of gap)
        target_price = (entry_price - gap_abs * cfg["target_pct"]  if direction == "SHORT"
                        else entry_price + gap_abs * cfg["target_pct"])

        atr_val = first_bar.get("atr14", 0) or 0

        # Outcome: did price reach target within hold window?
        if direction == "SHORT":
            filled      = not fwd.empty and (fwd["low"] <= target_price).any()
            outcome_pts = (entry_price - target_price if filled
                           else entry_price - (fwd["close"].iloc[-1] if not fwd.empty else entry_price))
        else:
            filled      = not fwd.empty and (fwd["high"] >= target_price).any()
            outcome_pts = (target_price - entry_price if filled
                           else (fwd["close"].iloc[-1] if not fwd.empty else entry_price) - entry_price)

        # Win: target hit OR positive outcome (SENSEX/NIFTY partial fills count)
        win = filled or outcome_pts > 0

        records.append({
            "date":         str(dates[i]),
            "pattern":      "GapFill_v2",
            "symbol":       symbol,
            "time":         str(first_bar["timestamp"].time()),
            "direction":    direction,
            "entry_price":  round(entry_price, 1),
            "prev_close":   round(prev_close, 1),
            "gap_pct":      round(gap_pct, 2),
            "gap_abs":      round(gap_abs, 1),
            "target_price": round(target_price, 1),
            "target_pct":   cfg["target_pct"],
            "atr":          round(atr_val, 2),
            "filled":       filled,
            "outcome_pts":  round(outcome_pts, 1),
            "win":          win,
            "skipped":      False,
            "entry_method": cfg["entry"],
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Pattern 4 v2: ATRSqueeze_v2
# ---------------------------------------------------------------------------

def find_atr_squeeze_v2(df5m: pd.DataFrame, symbol: str,
                        lookback: int = 50, forward_bars: int = 12,
                        percentile: float = 20.0) -> pd.DataFrame:
    """
    ATRSqueeze v2.
    - Volume confirm : breakout bar volume > 1.8x 20-bar vol SMA.
                       Bypassed for zero-volume indices (spot NIFTY/BANKNIFTY/SENSEX).
    - Percentile     : default 20. VIX-adaptive (live: 15th if VIX<15, 25th if VIX>15).
    - Keep           : BB-mid cross, RSI not exhausted.
    """
    records   = []
    df        = df5m.copy()
    pct_frac  = percentile / 100.0
    wid_pctile = df["bb_wid"].rolling(lookback).quantile(pct_frac)

    # Volume SMA (20-bar) -- used for zero-volume check
    vol_sma20 = df["volume"].rolling(20).mean()
    # Check if the whole series is zero-volume
    is_zero_vol_index = (df["volume"].sum() == 0)

    squeeze_mask = df["bb_wid"] < wid_pctile

    for i in range(lookback, len(df) - forward_bars):
        if not squeeze_mask.iloc[i]:
            continue
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]

        # Breakout from squeeze
        if bar["close"] > bar["bb_mid"] and prev["close"] <= prev["bb_mid"]:
            direction = "BULLISH"
        elif bar["close"] < bar["bb_mid"] and prev["close"] >= prev["bb_mid"]:
            direction = "BEARISH"
        else:
            continue

        # RSI not exhausted
        if direction == "BULLISH" and bar["rsi14"] > 72:
            continue
        if direction == "BEARISH" and bar["rsi14"] < 28:
            continue

        # Volume confirmation (bypass for zero-volume indices)
        if not is_zero_vol_index:
            vol_sma_val = vol_sma20.iloc[i]
            if pd.notna(vol_sma_val) and vol_sma_val > 0:
                if bar["volume"] <= 1.8 * vol_sma_val:
                    records.append({
                        "date":    bar["timestamp"].strftime("%Y-%m-%d"),
                        "pattern": "ATRSqueeze_v2", "symbol": symbol,
                        "skipped": True, "skip_reason": "low_volume",
                        "win": False, "outcome_pts": 0,
                    })
                    continue

        fwd = df.iloc[i + 1: i + 1 + forward_bars]
        if direction == "BULLISH":
            outcome_pts = fwd["high"].max() - bar["close"]
        else:
            outcome_pts = bar["close"] - fwd["low"].min()

        sl_est = bar["atr14"] * 1.5

        records.append({
            "date":        bar["timestamp"].strftime("%Y-%m-%d"),
            "pattern":     "ATRSqueeze_v2",
            "symbol":      symbol,
            "time":        str(bar["timestamp"].time()),
            "direction":   direction,
            "entry_price": round(bar["close"], 1),
            "bb_wid":      round(bar["bb_wid"], 4),
            "percentile":  percentile,
            "atr":         round(bar["atr14"], 2),
            "rsi":         round(bar["rsi14"], 1),
            "vol_sma20":   round(vol_sma20.iloc[i], 1) if not is_zero_vol_index else 0,
            "bar_vol":     round(bar["volume"], 1),
            "outcome_pts": round(outcome_pts, 1),
            "win":         outcome_pts > sl_est,
            "skipped":     False,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Pattern 5 v2: VWAPReclaim_v2
# ---------------------------------------------------------------------------

def find_vwap_reclaim_v2(df5m: pd.DataFrame, symbol: str,
                         forward_bars: int = 6) -> pd.DataFrame:
    """
    VWAPReclaim v2.
    - Confirm bars: 3 for NIFTY, 5 for BANKNIFTY and SENSEX
    - Body% filter: reclaim candle body_pct > 0.65
    - Keep       : RSI>45 reclaim, RSI<55 rejection, time gate 9:30-14:30
    """
    confirm_bars = 3 if symbol == "NIFTY" else 5
    records      = []
    df           = df5m.copy()

    below         = df["close"] < df["vwap"]
    consec_below  = below.rolling(confirm_bars).sum()
    above         = df["close"] > df["vwap"]
    consec_above  = above.rolling(confirm_bars).sum()

    for i in range(confirm_bars, len(df) - forward_bars):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        ts   = bar["timestamp"]

        # Time gate 9:30-14:30
        t = ts.time()
        gate_start = pd.Timestamp("09:30").time()
        gate_end   = pd.Timestamp("14:30").time()
        if not (gate_start <= t <= gate_end):
            continue

        # --- Reclaim ---
        if consec_below.iloc[i - 1] >= confirm_bars:
            if bar["close"] > bar["vwap"] and prev["close"] <= prev["vwap"]:
                if bar["rsi14"] >= 45 and bar["body_pct"] > 0.65:
                    fwd = df.iloc[i + 1: i + 1 + forward_bars]
                    outcome_pts = fwd["high"].max() - bar["close"]
                    records.append({
                        "date":        ts.strftime("%Y-%m-%d"),
                        "pattern":     "VWAPReclaim_v2",
                        "symbol":      symbol,
                        "time":        str(ts.time()),
                        "direction":   "BULLISH",
                        "entry_price": round(bar["close"], 1),
                        "vwap":        round(bar["vwap"], 1),
                        "body_pct":    round(bar["body_pct"], 3),
                        "confirm_bars":confirm_bars,
                        "atr":         round(bar["atr14"], 2),
                        "rsi":         round(bar["rsi14"], 1),
                        "outcome_pts": round(outcome_pts, 1),
                        "win":         outcome_pts > bar["atr14"],
                        "skipped":     False,
                    })

        # --- Rejection ---
        if consec_above.iloc[i - 1] >= confirm_bars:
            if bar["close"] < bar["vwap"] and prev["close"] >= prev["vwap"]:
                if bar["rsi14"] <= 55 and bar["body_pct"] > 0.65:
                    fwd = df.iloc[i + 1: i + 1 + forward_bars]
                    outcome_pts = bar["close"] - fwd["low"].min()
                    records.append({
                        "date":        ts.strftime("%Y-%m-%d"),
                        "pattern":     "VWAPReclaim_v2",
                        "symbol":      symbol,
                        "time":        str(ts.time()),
                        "direction":   "BEARISH",
                        "entry_price": round(bar["close"], 1),
                        "vwap":        round(bar["vwap"], 1),
                        "body_pct":    round(bar["body_pct"], 3),
                        "confirm_bars":confirm_bars,
                        "atr":         round(bar["atr14"], 2),
                        "rsi":         round(bar["rsi14"], 1),
                        "outcome_pts": round(outcome_pts, 1),
                        "win":         outcome_pts > bar["atr14"],
                        "skipped":     False,
                    })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Pattern 6: GammaSqueeze (NEW)
# ---------------------------------------------------------------------------

def find_gamma_squeeze(df5m: pd.DataFrame, symbol: str,
                       forward_bars: int = 6) -> pd.DataFrame:
    """
    GammaSqueeze -- NEW strategy.
    Active    : Monday(0), Wednesday(2), Friday(4) only (expiry settlement days).
    Window    : 14:45-15:15 IST.
    Conditions:
      - ADX14 > 45
      - RSI14 in 35-50 (neutral, room to run)
      - 3 consecutive bars moving in same direction (close > close[-1])
      - For non-zero-volume: last 3 bars each have volume > 20-bar vol SMA
    Direction : BULLISH if 3 bars rising, BEARISH if falling.
    Win       : outcome_pts > 1x ATR14.
    """
    records  = []
    df       = df5m.copy()

    vol_sma20        = df["volume"].rolling(20).mean()
    is_zero_vol_index = (df["volume"].sum() == 0)

    # We need at least 3 bars back to check consecutive direction
    for i in range(3, len(df) - forward_bars):
        bar = df.iloc[i]
        ts  = bar["timestamp"]

        # Day-of-week filter: Mon=0, Wed=2, Fri=4
        if ts.weekday() not in (0, 2, 4):
            continue

        # Time window 14:45-15:15
        t = ts.time()
        win_start = pd.Timestamp("14:45").time()
        win_end   = pd.Timestamp("15:15").time()
        if not (win_start <= t <= win_end):
            continue

        # ADX > 45
        adx_val = bar.get("adx14", np.nan)
        if pd.isna(adx_val) or adx_val <= 45:
            continue

        # RSI 35-50
        if not (35 <= bar["rsi14"] <= 50):
            continue

        # 3 consecutive bars same direction
        b0 = df.iloc[i]
        b1 = df.iloc[i - 1]
        b2 = df.iloc[i - 2]
        b3 = df.iloc[i - 3]

        rising  = (b0["close"] > b1["close"]) and (b1["close"] > b2["close"]) and (b2["close"] > b3["close"])
        falling = (b0["close"] < b1["close"]) and (b1["close"] < b2["close"]) and (b2["close"] < b3["close"])

        if not rising and not falling:
            continue

        direction = "BULLISH" if rising else "BEARISH"

        # Volume surge check (bypass for zero-volume indices)
        if not is_zero_vol_index:
            vol_ok = True
            for j in (i, i - 1, i - 2):
                sma_v = vol_sma20.iloc[j]
                if pd.notna(sma_v) and sma_v > 0:
                    if df.iloc[j]["volume"] <= sma_v:
                        vol_ok = False
                        break
            if not vol_ok:
                continue

        fwd = df.iloc[i + 1: i + 1 + forward_bars]
        if fwd.empty:
            continue

        if direction == "BULLISH":
            outcome_pts = fwd["high"].max() - bar["close"]
        else:
            outcome_pts = bar["close"] - fwd["low"].min()

        records.append({
            "date":        ts.strftime("%Y-%m-%d"),
            "pattern":     "GammaSqueeze",
            "symbol":      symbol,
            "time":        str(ts.time()),
            "day_of_week": ts.strftime("%A"),
            "direction":   direction,
            "entry_price": round(bar["close"], 1),
            "adx14":       round(adx_val, 1),
            "rsi14":       round(bar["rsi14"], 1),
            "atr":         round(bar["atr14"], 2),
            "outcome_pts": round(outcome_pts, 1),
            "win":         outcome_pts > bar["atr14"],
            "skipped":     False,
        })

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Convenience: run all v2 patterns for a symbol
# ---------------------------------------------------------------------------

def run_all_v2(df5m: pd.DataFrame, symbol: str) -> dict:
    """Return dict of pattern_name -> DataFrame of signals."""
    return {
        "ExpiryBlast_v2":  find_expiry_blast_v2(df5m, symbol),
        "ORB15_v2":        find_orb15_v2(df5m, symbol),
        "GapFill_v2":      find_gap_fill_v2(df5m, symbol),
        "ATRSqueeze_v2":   find_atr_squeeze_v2(df5m, symbol),
        "VWAPReclaim_v2":  find_vwap_reclaim_v2(df5m, symbol),
        "GammaSqueeze":    find_gamma_squeeze(df5m, symbol),
    }


# ---------------------------------------------------------------------------
# Quick smoke-test when run directly
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime, timedelta

    today     = datetime.now()
    to_date   = today.strftime("%Y-%m-%d")
    from_date = (today - timedelta(days=6 * 31)).strftime("%Y-%m-%d")

    for sym in ("NIFTY", "BANKNIFTY", "SENSEX"):
        print(f"\n=== {sym} ===")
        df5m = load(sym, "5min", from_date, to_date)
        if df5m.empty:
            print("  no data in cache")
            continue
        df5m = df5m.sort_values("timestamp").reset_index(drop=True)
        df5m = add_indicators_v2(df5m)

        results = run_all_v2(df5m, sym)
        for name, df_t in results.items():
            if df_t.empty:
                print(f"  {name:20s}: 0 signals")
                continue
            active = df_t[~df_t.get("skipped", False)] if "skipped" in df_t.columns else df_t
            active = active[active["skipped"] == False] if "skipped" in active.columns else active
            s = summarise(active)
            print(f"  {name:20s}: {s['count']:3d} signals  WR={s['win_rate']:5.1f}%  "
                  f"Exp={s['expectancy_pts']:+6.1f}pts")
