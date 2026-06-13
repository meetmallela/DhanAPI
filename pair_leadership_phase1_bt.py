import sys
import pandas as pd
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import os

sys.path.insert(0, r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from master_resource import MasterResource

# ================= CONFIG =================

LOOKBACK_MONTHS = 6
INTERVAL = "minute"

STRUCTURE_LOOKBACK = 3
VOL_WINDOW = 20
VOL_MULTIPLIER = 1.2

STOP_LOSS_POINTS = 20

TRADE_START = "09:20"
TRADE_END = "14:00"
FORCE_EXIT = "14:45"

OUTPUT_DIR = "./output/"
DATA_DIR = "./data/raw/"

# ================= DATA =================

def fetch_data(kite, token, symbol, start, end):
    os.makedirs(DATA_DIR, exist_ok=True)
    file_path = f"{DATA_DIR}/{symbol}_1m.csv"

    if os.path.exists(file_path):
        print(f"Loaded {symbol} from disk")
        df = pd.read_csv(file_path, parse_dates=["timestamp"], index_col="timestamp")
        return df

    print(f"Downloading {symbol} from Kite...")
    all_data = []
    current_start = start

    while current_start < end:
        current_end = min(current_start + timedelta(days=60), end)

        data = kite.historical_data(
            instrument_token=token,
            from_date=current_start,
            to_date=current_end,
            interval=INTERVAL
        )

        if data:
            all_data.extend(data)

        current_start = current_end + timedelta(minutes=1)

    df = pd.DataFrame(all_data)
    df["timestamp"] = pd.to_datetime(df["date"])
    df.set_index("timestamp", inplace=True)
    df = df[~df.index.duplicated()].sort_index()
    df = df[["open", "high", "low", "close", "volume"]]

    df.to_csv(file_path)
    print(f"Saved {symbol} to disk")
    return df

# ================= INDICATORS =================

def add_indicators(df):
    df = df.copy()
    df["tp"] = (df["high"] + df["low"] + df["close"]) / 3
    df["vol_avg"] = df["volume"].rolling(VOL_WINDOW).mean()
    return df

def compute_daily_vwap(df):
    df = df.copy()
    df["vwap"] = (
        df.groupby(df.index.date)
        .apply(lambda x: (x["tp"] * x["volume"]).cumsum() / x["volume"].cumsum())
        .reset_index(level=0, drop=True)
    )
    return df

# ================= BIAS LOGIC =================

def get_bias(df, idx):
    if idx < STRUCTURE_LOOKBACK:
        return "NEUTRAL"

    row = df.iloc[idx]
    prev = df.iloc[idx-STRUCTURE_LOOKBACK:idx]

    if (
        row["close"] > row["vwap"] and
        row["close"] > prev["high"].max() and
        row["volume"] > VOL_MULTIPLIER * row["vol_avg"]
    ):
        return "BULLISH"

    if (
        row["close"] < row["vwap"] and
        row["close"] < prev["low"].min() and
        row["volume"] > VOL_MULTIPLIER * row["vol_avg"]
    ):
        return "BEARISH"

    return "NEUTRAL"

# ================= BACKTEST =================

def run_backtest(nifty, reliance, hdfc):
    trades = []

    for day, day_df in nifty.groupby(nifty.index.date):
        traded_today = False
        in_trade = False
        trade = {}

        for ts in day_df.index:
            time_str = ts.strftime("%H:%M")
            if time_str < TRADE_START:
                continue

            idx = nifty.index.get_loc(ts)

            rel_bias = get_bias(reliance, idx)
            hdfc_bias = get_bias(hdfc, idx)

            if rel_bias == hdfc_bias and rel_bias != "NEUTRAL":
                pair_bias = rel_bias
            else:
                pair_bias = None

            # ENTRY
            if not in_trade and not traded_today and pair_bias and time_str <= TRADE_END:
                entry_price = nifty.iloc[idx + 1]["open"]
                sl = entry_price - STOP_LOSS_POINTS if pair_bias == "BULLISH" else entry_price + STOP_LOSS_POINTS

                trade = {
                    "date": day,
                    "entry_time": nifty.index[idx + 1],
                    "direction": pair_bias,
                    "entry_price": entry_price,
                    "stop_loss": sl
                }

                in_trade = True
                traded_today = True
                continue

            # EXIT
            if in_trade:
                price = nifty.loc[ts]["close"]
                sl_hit = price <= trade["stop_loss"] if trade["direction"] == "BULLISH" else price >= trade["stop_loss"]
                bias_lost = not (rel_bias == hdfc_bias == trade["direction"])
                time_exit = time_str >= FORCE_EXIT

                if sl_hit or bias_lost or time_exit:
                    trade["exit_time"] = ts
                    trade["exit_price"] = price
                    trade["pnl_points"] = (
                        price - trade["entry_price"]
                        if trade["direction"] == "BULLISH"
                        else trade["entry_price"] - price
                    )
                    trades.append(trade)
                    in_trade = False

    return pd.DataFrame(trades)

# ================= METRICS =================

def generate_summary(trades):
    if trades.empty:
        return {}

    equity = trades.pnl_points.cumsum()
    return {
        "total_trades": len(trades),
        "win_rate": round((trades.pnl_points > 0).mean(), 2),
        "avg_win": round(trades[trades.pnl_points > 0].pnl_points.mean(), 2),
        "avg_loss": round(trades[trades.pnl_points <= 0].pnl_points.mean(), 2),
        "expectancy": round(trades.pnl_points.mean(), 2),
        "max_drawdown": round((equity - equity.cummax()).min(), 2)
    }

# ================= MAIN =================

def main(kite, tokens):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    end = datetime.now()
    start = end - timedelta(days=LOOKBACK_MONTHS * 30)

    nifty = fetch_data(kite, tokens["NIFTY"], "nifty", start, end)
    reliance = fetch_data(kite, tokens["RELIANCE"], "reliance", start, end)
    hdfc = fetch_data(kite, tokens["HDFCBANK"], "hdfcbank", start, end)

    common_index = nifty.index.intersection(reliance.index).intersection(hdfc.index)
    nifty, reliance, hdfc = nifty.loc[common_index], reliance.loc[common_index], hdfc.loc[common_index]

    nifty = compute_daily_vwap(add_indicators(nifty))
    reliance = compute_daily_vwap(add_indicators(reliance))
    hdfc = compute_daily_vwap(add_indicators(hdfc))

    trades = run_backtest(nifty, reliance, hdfc)
    summary = generate_summary(trades)

    trades.to_csv(f"{OUTPUT_DIR}/scenario3_phase1_trades.csv", index=False)
    pd.DataFrame([summary]).to_csv(f"{OUTPUT_DIR}/scenario3_phase1_summary.csv", index=False)

    print("Backtest completed")
    print(summary)

# ================= ENTRY =================

if __name__ == "__main__":
    cfg  = MasterResource.get_kite_config()
    kite = KiteConnect(api_key=cfg["api_key"])
    kite.set_access_token(cfg["access_token"])


    tokens = {
        "NIFTY": 256265,
        "RELIANCE": 738561,
        "HDFCBANK": 341249
    }

    main(kite, tokens)
