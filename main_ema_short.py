import os
import json
import time  # Fixed: Added missing import
import pandas as pd
import sqlite3
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
from data.fetcher import get_data
from strategies.indicators import calculate_indicators, generate_signals
from utils.telegram_bot import send_telegram_msg
from utils.logger import setup_logger
from utils.heartbeat import update_heartbeat, system_log
from utils.instrument_lookup import CSV_PATH

# Add MasterConfiguration lib to path
import sys
sys.path.append(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from master_resource import get_kite_config

# Initialize Logger
logger = setup_logger("signal_monitor")

# Database Path Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'trading_system.db')

# Index Configuration
INDICES = {
    "NIFTY": {"token": 256265, "step": 50, "symbol": "NIFTY"},
    "BANKNIFTY": {"token": 260105, "step": 100, "symbol": "BANKNIFTY"},
    "FINNIFTY": {"token": 257801, "step": 50, "symbol": "FINNIFTY"},
    "SENSEX": {"token": 265, "step": 100, "symbol": "SENSEX"},
    "MIDCPNIFTY": {"token": 288009, "step": 25, "symbol": "MIDCPNIFTY"}
}

def get_futures_token(symbol):
    """
    Finds the instrument token for the current month's future of a given symbol.
    """
    try:
        if not os.path.exists(CSV_PATH): return None
        df = pd.read_csv(CSV_PATH)
        
        # Filter for symbol and instrument_type 'FUT'
        futs = df[(df['symbol'] == symbol) & (df['instrument_type'] == 'FUT')]
        if futs.empty: return None
        
        # Sort by expiry to get current month
        futs['expiry_date'] = pd.to_datetime(futs['expiry_date'])
        futs = futs[futs['expiry_date'] >= pd.Timestamp.now().normalize()]
        futs = futs.sort_values(by='expiry_date')
        
        if not futs.empty:
            return int(futs.iloc[0]['instrument_token'])
    except Exception as e:
        logger.warning(f"Futures lookup failed for {symbol}: {e}")
    return None

def is_trading_allowed():
    """
    Returns True if current time is between 9:20 AM and 2:30 PM (No trade zone after 14:30).
    """
    now = datetime.now()
    if now.weekday() >= 5: return False
    
    start_time = now.replace(hour=9, minute=20, second=0, microsecond=0)
    end_time = now.replace(hour=14, minute=30, second=0, microsecond=0)
    return start_time <= now <= end_time

def is_market_open():
    """
    Checks if the current time is within Indian Market Hours (9:15 AM - 3:30 PM).
    """
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday or Sunday
        return False
    
    start_time = now.replace(hour=9, minute=15, second=0, microsecond=0)
    end_time = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return start_time <= now <= end_time

def get_15m_levels(df_15m):
    """
    Calculates Support and Resistance on the 15-minute timeframe
    using a 21-candle lookback.
    """
    if df_15m is None or df_15m.empty:
        return None, None
    sup = df_15m['low'].rolling(window=21).min().iloc[-1]
    res = df_15m['high'].rolling(window=21).max().iloc[-1]
    return sup, res

def get_sector_bias(kite):
    """
    Head of Research Suggestion: Sectoral Confirmation.
    Checks if Reliance and HDFC Bank are aligned (Bullish/Bearish).
    """
    try:
        # RELIANCE: 738561, HDFCBANK: 341249
        leaders = {"RELIANCE": 738561, "HDFCBANK": 341249}
        biases = []
        for name, token in leaders.items():
            df = get_data(kite, token, "5minute")
            if not df.empty and len(df) > 20:
                ema20 = df['close'].ewm(span=20, adjust=False).mean().iloc[-1]
                curr = df['close'].iloc[-1]
                biases.append(1 if curr > ema20 else -1)
        
        # If both agree, return strong bias. Else return neutral.
        if sum(biases) == 2: return 1
        if sum(biases) == -2: return -1
        return 0
    except Exception as e:
        logger.warning(f"Sector bias check failed: {e}")
        return 0

def main():
    logger.info("Signal Monitor Started")
    update_heartbeat("signal_monitor", "INITIALIZING", "Setting up system connection")
    system_log("signal_monitor", "INFO", "Signal Monitor Agent started")
    
    # 1. Initialize Kite Connection
    try:
        cfg  = get_kite_config()
        kite = KiteConnect(api_key=cfg["api_key"])
        kite.set_access_token(cfg["access_token"])
        logger.info("Kite Connect session active for: User")
    except Exception as e:
        logger.error(f"Critical Error: Could not initialize Kite Client. {e}")
        return

    # 2. Main Strategy Loop
    while True:
        try:
            update_heartbeat("signal_monitor", "IDLE", "Checking session hours")
            
            # Market Hours Check
            if not is_market_open():
                update_heartbeat("signal_monitor", "IDLE", "Market is Closed")
                time.sleep(60)
                continue

            # 3.1 Fetch INDIAVIX and Store
            try:
                vix_data = kite.ltp(["NSE:INDIA VIX"])
                if "NSE:INDIA VIX" in vix_data:
                    vix_value = vix_data["NSE:INDIA VIX"]['last_price']
                    conn = sqlite3.connect(DB_PATH, timeout=30)
                    conn.execute("PRAGMA journal_mode=WAL")
                    cur = conn.cursor()
                    cur.execute("INSERT OR REPLACE INTO vix_history (timestamp, vix_value) VALUES (?, ?)", 
                                (datetime.now(), vix_value))
                    conn.commit()
                    conn.close()
                    system_log("signal_monitor", "INFO", f"India VIX updated: {vix_value}")
            except Exception as vix_e:
                logger.warning(f"Failed to fetch VIX: {vix_e}")

                update_heartbeat("signal_monitor", "SCANNING", f"Current Cycle: {len(INDICES)} indices")
            scanned_count = 0
            for index_name, config in INDICES.items():
                try:
                    token = config['token']
                    step = config['step']
                    
                    # Fetch data
                    df_5m = get_data(kite, token, "5minute")
                    df_15m = get_data(kite, token, "15minute")
                    
                    if df_5m.empty or df_15m.empty:
                        logger.warning(f"No data returned for {index_name}")
                        continue
                    
                    scanned_count += 1
                    
                    # 4. Indicators and Levels (with Futures Volume for VWAP)
                    # For Spot Indices, we fetch Futures volume for VWAP calculation
                    volume_df = None
                    if df_5m['volume'].sum() == 0:
                        fut_token = get_futures_token(config['symbol'])
                        if fut_token:
                            volume_df = get_data(kite, fut_token, "5minute")
                            if not volume_df.empty:
                                logger.info(f"Using Futures volume for {index_name} VWAP")

                    df_5m = calculate_indicators(df_5m, volume_df=volume_df) 
                    sup_15m, res_15m = get_15m_levels(df_15m)
                    
                    latest_row = df_5m.iloc[-1]
                    current_price = latest_row['close']
                    
                    # Store ORB levels for SL calculation
                    orb_h = latest_row.get('orb_high')
                    orb_l = latest_row.get('orb_low')
                    
                    # 5. Signal Management with Time Filter
                    signal = 0
                    scenario = "NO_SIGNAL"
                    
                    if is_trading_allowed():
                        # Only apply leader bias to NIFTY for now
                        leader_bias = 0
                        if index_name == "NIFTY":
                            leader_bias = get_sector_bias(kite)
                            if leader_bias != 0:
                                logger.info(f"Sectoral Confirmation: {'BULLISH' if leader_bias == 1 else 'BEARISH'}")
                                
                        signal, scenario = generate_signals(df_5m, sup_15m, res_15m, leader_bias=leader_bias)
                    else:
                        if scanned_count == 1: # Log once per cycle
                             logger.debug("Trading not allowed (outside 9:20 - 14:30 window)")

                    if signal != 0:
                        signal_type = "SHORT" if signal == -1 else "LONG"
                        strategy_full_name = f"{index_name}_{scenario}"
                        
                        try:
                            # Use connection-per-loop for thread safety
                            conn = sqlite3.connect(DB_PATH, timeout=30)
                            conn.execute("PRAGMA journal_mode=WAL")
                            cur = conn.cursor()
                            
                            # Check for DUPLICATE within 5 mins
                            time_threshold = datetime.now() - timedelta(minutes=5)
                            cur.execute('''SELECT COUNT(*) FROM signal_table 
                                           WHERE signal_type = ? AND strategy_name = ? AND signal_time > ?''', 
                                        (signal_type, strategy_full_name, time_threshold))
                            
                            if cur.fetchone()[0] > 0:
                                conn.close()
                                continue

                            # Define Strike Suggestions
                            atm_strike = round(current_price / step) * step
                            suggestion = f"ATM: {atm_strike}, ATM+1: {atm_strike+step}, ATM-1: {atm_strike-step}"

                            logger.info(f"SIGNAL DETECTED [{index_name}]: {signal_type} | Strategy: {scenario}")
                            logger.info(f"Suggested Strikes: {suggestion}")

                            cur.execute('''INSERT INTO signal_table 
                                (nifty_spot, ema_9, ema_21, support_15m, resistance_15m, orb_high, orb_low, strategy_name, signal_type, signal_time, order_placed)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'N')''',
                                (current_price, latest_row['ema_9'], latest_row['ema_21'], sup_15m, res_15m,
                                 orb_h, orb_l, strategy_full_name, signal_type, datetime.now()))
                            
                            conn.commit()
                            conn.close()
                                
                            system_log("signal_monitor", "SIGNAL", f"New {signal_type} for {index_name}")
                            logger.info(f"Signal {signal_type} ({strategy_full_name}) logged to database.")

                            # Send Telegram Alert
                            msg_prefix = f"ALERT: {signal_type} SIGNAL ({index_name})"
                            level_text = f"Break below: {sup_15m}" if signal == -1 else f"Break above: {res_15m}"
                            msg = (f"{msg_prefix}\n"
                                   f"Strategy: {scenario}\n"
                                   f"Price: {current_price}\n"
                                   f"{level_text}\n"
                                   f"Strikes: {suggestion}\n"
                                   f"EMA9: {round(latest_row['ema_9'], 2)} | EMA21: {round(latest_row['ema_21'], 2)}")
                            send_telegram_msg(msg)

                        except Exception as db_err:
                            logger.error(f"DB Error for {index_name}: {db_err}")

                except Exception as index_e:
                    logger.error(f"Error in {index_name} block: {index_e}")

            logger.info(f"Scan Cycle Complete. Indices Active: {scanned_count}/{len(INDICES)}")
            time.sleep(30) # Poll interval

        except Exception as e:
            logger.error(f"Execution Error: {e}")
            time.sleep(30)

if __name__ == "__main__":
    main()
