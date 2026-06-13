"""
run_backtest.py
---------------
Main entry point for backtesting strategy ideas.

How to use
----------
1. Pick a STRATEGY_NAME from the list below (or add your own class).
2. Set the parameters at the top of the file.
3. Run:  python run_backtest.py

Each strategy is a self-contained block of signal logic.
When you have a new idea in plain text, paste it here as a new strategy class.

Available strategies (add more as ideas come in):
  EMA_9_21          — EMA 9/21 crossover, long & short
  RSI_Reversal      — RSI oversold/overbought reversal
  VWAP_Pullback     — Price crosses VWAP, enter on pullback
  Supertrend_MACD   — Supertrend + MACD confluence
  ORB_Breakout      — 15-minute Opening Range Breakout
"""

import pandas as pd
import sys

from backtest.data_fetcher  import fetch
from backtest.engine        import BacktestEngine
from backtest.report        import BacktestReport
from backtest.indicators    import (
    ema, rsi, macd, supertrend, vwap, atr, bollinger, stochastic, opening_range
)

# ═══════════════════════════════════════════════════════════════════════
# CONFIG — edit these
# ═══════════════════════════════════════════════════════════════════════

STRATEGY_NAME   = "EMA_9_21"         # which strategy block to run
SYMBOL          = "NIFTY"            # NIFTY | BANKNIFTY | FINNIFTY | SENSEX | RELIANCE | HDFC
INTERVAL        = 5                  # candle size in minutes: 1, 5, 15, 25, 60
FROM_DATE       = "2026-03-01"       # YYYY-MM-DD  (Dhan limit: ~60 days back for 5m)
TO_DATE         = "2026-04-11"       # YYYY-MM-DD

SL_PCT          = 0.5                # stop-loss  % from entry (e.g. 0.5 = 0.5%)
TP_PCT          = 1.0                # take-profit % from entry
SLIPPAGE_PCT    = 0.05               # one-way slippage % (realistic for options)
LOT_SIZE        = 1                  # set to actual lot size for ₹ P&L (e.g. 75 for NIFTY)
ALLOW_SHORT     = True               # False = long-only
INTRADAY_ONLY   = True               # True = force-close all positions at EOD
SHOW_TRADES     = True               # print individual trade log

# ═══════════════════════════════════════════════════════════════════════
# STRATEGY DEFINITIONS
# Add your plain-text idea as a new class here.
# Each class must implement: generate_signals(df) -> pd.Series
#   Return  1 = BUY (go long)
#          -1 = SELL (go short)
#           0 = no action
# ═══════════════════════════════════════════════════════════════════════

class EMA_9_21:
    """
    Plain-text idea:
      BUY  when EMA-9 crosses ABOVE EMA-21.
      SELL when EMA-9 crosses BELOW EMA-21.
      One position at a time, exit at SL or TP.
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = df.copy()
        df["e9"]  = ema(df["close"], 9)
        df["e21"] = ema(df["close"], 21)

        # Cross detection: current bar e9 > e21, previous bar e9 <= e21
        bullish_cross = (df["e9"] > df["e21"]) & (df["e9"].shift(1) <= df["e21"].shift(1))
        bearish_cross = (df["e9"] < df["e21"]) & (df["e9"].shift(1) >= df["e21"].shift(1))

        sig = pd.Series(0, index=df.index)
        sig[bullish_cross] =  1
        sig[bearish_cross] = -1
        return sig


class RSI_Reversal:
    """
    Plain-text idea:
      BUY  when RSI-14 dips below 35 then closes back above 35 (oversold bounce).
      SELL when RSI-14 rises above 65 then closes back below 65 (overbought fade).
      Avoids entering during lunchtime chop (11:30–13:00).
    """
    OVERSOLD  = 35
    OVERBOUGHT= 65

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = df.copy()
        df["r"] = rsi(df["close"], 14)
        df["t"] = pd.to_datetime(df["timestamp"]).dt.time

        from datetime import time as dtime
        lunch_start = dtime(11, 30)
        lunch_end   = dtime(13,  0)
        in_lunch = (df["t"] >= lunch_start) & (df["t"] <= lunch_end)

        buy_sig  = (df["r"] > self.OVERSOLD)  & (df["r"].shift(1) <= self.OVERSOLD)
        sell_sig = (df["r"] < self.OVERBOUGHT) & (df["r"].shift(1) >= self.OVERBOUGHT)

        sig = pd.Series(0, index=df.index)
        sig[buy_sig  & ~in_lunch] =  1
        sig[sell_sig & ~in_lunch] = -1
        return sig


class VWAP_Pullback:
    """
    Plain-text idea:
      After 10:00 IST (enough data for VWAP to stabilize):
        BUY  when price dips to VWAP and the next bar closes ABOVE VWAP.
        SELL when price rises to VWAP and the next bar closes BELOW VWAP.
      Filter: only trade in the direction of the first-hour bias
        (if 9:15–10:00 close > VWAP, bias is BULLISH → only longs).
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = df.copy()
        df["vwap"] = vwap(df)

        from datetime import time as dtime
        df["time"] = pd.to_datetime(df["timestamp"]).dt.time
        after_warmup = df["time"] >= dtime(10, 0)

        # Touched VWAP = low <= vwap on previous bar, close > vwap now
        prev_touched_from_below = df["low"].shift(1) <= df["vwap"].shift(1)
        prev_touched_from_above = df["high"].shift(1) >= df["vwap"].shift(1)

        buy_sig  = prev_touched_from_below & (df["close"] > df["vwap"])
        sell_sig = prev_touched_from_above & (df["close"] < df["vwap"])

        sig = pd.Series(0, index=df.index)
        sig[buy_sig  & after_warmup] =  1
        sig[sell_sig & after_warmup] = -1
        return sig


class Supertrend_MACD:
    """
    Plain-text idea:
      BUY  when Supertrend flips bullish AND MACD histogram turns positive.
      SELL when Supertrend flips bearish AND MACD histogram turns negative.
      Confluence of both filters reduces false signals.
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = df.copy()
        st   = supertrend(df, period=7, multiplier=3.0)
        m    = macd(df["close"], fast=12, slow=26, signal=9)
        df["st_dir"]   = st["direction"]
        df["macd_hist"]= m["histogram"]

        # Supertrend flip to bullish + MACD histogram > 0
        st_bull_flip = (df["st_dir"] == 1) & (df["st_dir"].shift(1) == -1)
        st_bear_flip = (df["st_dir"] == -1) & (df["st_dir"].shift(1) == 1)

        sig = pd.Series(0, index=df.index)
        sig[st_bull_flip & (df["macd_hist"] > 0)] =  1
        sig[st_bear_flip & (df["macd_hist"] < 0)] = -1
        return sig


class ORB_Breakout:
    """
    Plain-text idea:
      Define the Opening Range as the high and low of the first 15 minutes (9:15–9:30).
      After 9:30:
        BUY  on the first candle that closes ABOVE the ORB high.
        SELL on the first candle that closes BELOW the ORB low.
      One trade per day — do not re-enter after exit.
    """
    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        df = df.copy()
        orb = opening_range(df, minutes=15)
        df["orb_high"] = orb["orb_high"]
        df["orb_low"]  = orb["orb_low"]

        from datetime import time as dtime
        df["time"] = pd.to_datetime(df["timestamp"]).dt.time
        after_orb  = df["time"] > dtime(9, 30)

        buy_sig  = (df["close"] > df["orb_high"]) & after_orb
        sell_sig = (df["close"] < df["orb_low"])  & after_orb

        sig = pd.Series(0, index=df.index)
        # Only signal once per day (first breakout)
        df["date"] = pd.to_datetime(df["timestamp"]).dt.date
        for date, grp in df.groupby("date"):
            idx = grp.index
            buy_idx  = idx[buy_sig.loc[idx]].tolist()
            sell_idx = idx[sell_sig.loc[idx]].tolist()
            first_event = None
            if buy_idx and sell_idx:
                first_event = ("buy", min(buy_idx)) if min(buy_idx) < min(sell_idx) \
                              else ("sell", min(sell_idx))
            elif buy_idx:
                first_event = ("buy", min(buy_idx))
            elif sell_idx:
                first_event = ("sell", min(sell_idx))

            if first_event:
                kind, fidx = first_event
                sig.loc[fidx] = 1 if kind == "buy" else -1

        return sig


# ═══════════════════════════════════════════════════════════════════════
# STRATEGY REGISTRY — map name → class
# ═══════════════════════════════════════════════════════════════════════
STRATEGIES = {
    "EMA_9_21":        EMA_9_21,
    "RSI_Reversal":    RSI_Reversal,
    "VWAP_Pullback":   VWAP_Pullback,
    "Supertrend_MACD": Supertrend_MACD,
    "ORB_Breakout":    ORB_Breakout,
}


# ═══════════════════════════════════════════════════════════════════════
# RUNNER
# ═══════════════════════════════════════════════════════════════════════

def main():
    strat_name = sys.argv[1] if len(sys.argv) > 1 else STRATEGY_NAME

    if strat_name not in STRATEGIES:
        print(f"Unknown strategy '{strat_name}'. Available: {list(STRATEGIES)}")
        sys.exit(1)

    print("=" * 70)
    print(f"  Dhan Backtester — {strat_name}")
    print(f"  {SYMBOL}  {INTERVAL}m  |  {FROM_DATE} → {TO_DATE}")
    print(f"  SL={SL_PCT}%  TP={TP_PCT}%  Slippage={SLIPPAGE_PCT}%  Lot={LOT_SIZE}")
    print("=" * 70)

    # 1. Fetch data
    print("\n[1] Loading market data...")
    df = fetch(SYMBOL, INTERVAL, FROM_DATE, TO_DATE)
    if df.empty:
        print("  No data returned. Check symbol, dates, and API credentials.")
        sys.exit(1)

    # 2. Generate signals
    print("\n[2] Generating signals...")
    strategy = STRATEGIES[strat_name]()
    signals  = strategy.generate_signals(df)
    sig_count = (signals != 0).sum()
    print(f"  {sig_count} signals generated on {len(df)} bars")

    # 3. Run simulation
    print("\n[3] Running backtest simulation...")
    engine = BacktestEngine(
        sl_pct       = SL_PCT,
        tp_pct       = TP_PCT,
        slippage_pct = SLIPPAGE_PCT,
        allow_short  = ALLOW_SHORT,
    )
    if INTRADAY_ONLY:
        trades = engine.run_intraday(df, signals)
    else:
        trades = engine.run(df, signals)

    # 4. Report
    print("\n[4] Results:")
    report = BacktestReport(trades, strategy_name=strat_name,
                            lot_size=LOT_SIZE)
    report.print_summary()
    if SHOW_TRADES:
        report.print_trades()

    report.save()


if __name__ == "__main__":
    main()
