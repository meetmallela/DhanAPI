import time
import logging
import sys
import pandas as pd
from datetime import datetime, timedelta
from PaperTrader import PaperTrader
from OptionSentimentMonitor import OptionSentimentMonitor
from StrategyModuleV2 import OptionStrategiesV2

# Master Config Import
sys.path.append(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from master_resource import get_kite_config
from kiteconnect import KiteConnect

# Generate V2 Timestamps
now_str = datetime.now().strftime("%d%m%y_%H_%M_%S")
log_filename = f"scalper_V2_log_{now_str}.log"
trade_log_filename = f"paper_trades_scalp_V2_{now_str}.csv"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - SCALP_V2 - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_filename, encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)

class ScalpingEngineV2:
    def __init__(self, symbol="BANKNIFTY"):
        self.symbol = symbol
        self.config = get_kite_config()
        self.kite = KiteConnect(api_key=self.config['api_key'])
        self.kite.set_access_token(self.config['access_token'])
        
        self.token = 260105 if symbol == "BANKNIFTY" else 256265
        self.strike_inc = 100 if symbol == "BANKNIFTY" else 50
        
        self.paper_trader = PaperTrader(log_file=trade_log_filename)
        self.sentiment_monitor = OptionSentimentMonitor(self.kite, symbol=symbol)

    def get_mtf_trend(self):
        """MTF Check: Ensure 5m trend matches the 1m scalp"""
        try:
            to_date = datetime.now()
            from_date = to_date - timedelta(hours=5)
            records = self.kite.historical_data(self.token, from_date, to_date, "5minute")
            df = pd.DataFrame(records)
            df['ema9'] = df['close'].ewm(span=9, adjust=False).mean()
            
            last_price = df.iloc[-1]['close']
            ema9 = df.iloc[-1]['ema9']
            
            if last_price > ema9: return "BULLISH"
            if last_price < ema9: return "BEARISH"
        except Exception as e:
            logging.error(f"Scalp MTF Error: {e}")
        return "NEUTRAL"

    def run_iteration(self):
        logging.info(f"--- Scalp_V2 Iteration: {datetime.now().strftime('%H:%M:%S')} ---")
        self.paper_trader.monitor_positions(self.kite)
        
        # 1. Fetch Spot Data (Signals) and Futures Data (Volume)
        to_date = datetime.now()
        from_date = to_date - timedelta(hours=2)
        
        try:
            # Fetch Spot Index
            records_spot = self.kite.historical_data(self.token, from_date, to_date, "minute")
            if not records_spot: return
            df_spot = pd.DataFrame(records_spot)
            
            # Fetch Futures (for Volume)
            fut_token, fut_symbol = self.sentiment_monitor.get_current_fut_token()
            if fut_token:
                records_fut = self.kite.historical_data(fut_token, from_date, to_date, "minute")
                df_fut = pd.DataFrame(records_fut)
                # Replace Spot volume (0) with Futures volume
                # Ensure dataframes align by date
                df_fut = df_fut.set_index('date')
                df_spot = df_spot.set_index('date')
                df_spot['volume'] = df_fut['volume']
                df_spot = df_spot.reset_index()
                logging.info(f"Using Futures Volume from {fut_symbol}")
            else:
                logging.warning("Could not fetch Futures volume.")

            df = OptionStrategiesV2.add_indicators(df_spot)
            
            # Apply V2 Scalping Filters
            last_row = df.iloc[-1]
            vol_confirmed = last_row['volume'] > (1.2 * last_row['vol_sma'])
            trend_strong = last_row['adx'] > 25
            
            price_signal = "NONE"
            if (last_row['close'] > last_row['ema9']) and (last_row['rsi'] > 60):
                price_signal = "BUY_CE"
            elif (last_row['close'] < last_row['ema9']) and (last_row['rsi'] < 40):
                price_signal = "BUY_PE"
                
            if price_signal == "NONE":
                return

            # 2. V2 Enhancement: MTF Alignment (1m vs 5m)
            mtf_trend = self.get_mtf_trend()
            if (price_signal == "BUY_CE" and mtf_trend != "BULLISH") or \
               (price_signal == "BUY_PE" and mtf_trend != "BEARISH"):
                logging.info(f"V2 SCALP FILTER: Discarded {price_signal} - 5m Trend is {mtf_trend}")
                return

            # 3. V2 Enhancement: Volume and Trend Strength
            if not (vol_confirmed and trend_strong):
                logging.info(f"V2 SCALP FILTER: Low Momentum/Vol (ADX: {last_row['adx']:.2f}, Vol: {last_row['volume']})")
                return

            # 4. Execute Entry
            logging.info(f"⚡⚡ V2 SCALP TRIGGERED! ADX: {last_row['adx']:.2f} | Vol: {last_row['volume']}")
            self.execute_v2_entry(price_signal, last_row['close'], last_row['atr'])
            
        except Exception as e:
            logging.error(f"Error in scalping iteration: {e}")

    def execute_v2_entry(self, signal_type, spot_price, atr):
        if len(self.paper_trader.active_positions) > 0: return

        option_type = "CE" if signal_type == "BUY_CE" else "PE"
        target_strike = round(spot_price / self.strike_inc) * self.strike_inc
        
        # Find ATM instrument
        sentiment = self.sentiment_monitor.analyze_sentiment()
        match = next((i for i in sentiment['details'] if i["strike"] == target_strike and i["type"] == option_type), None)
        
        if match:
            # V2 ATR-based SL for Scalping (1.2 * ATR for tighter stops)
            sl_points = atr * 0.5 * 1.2
            entry_price = match['ltp']
            sl_price = entry_price - sl_points
            target_price = entry_price + (sl_points * 2)

            self.paper_trader.enter_trade(
                symbol=self.symbol,
                option_symbol=match['symbol'],
                option_type=option_type,
                entry_price=entry_price,
                sl_price=sl_price,
                target_price=target_price
            )
            logging.info(f"🔥 V2 SCALP ENTRY: {match['symbol']} at {entry_price}. ATR SL: {sl_price:.2f}")

    def start(self):
        logging.info(f"Scalper V2 Started for {self.symbol} (1-Min Timeframe)")
        market_start = datetime.strptime("09:15", "%H:%M").time()
        market_end = datetime.strptime("15:30", "%H:%M").time()
        
        while True:
            now = datetime.now().time()
            if market_start <= now <= market_end:
                self.run_iteration()
            else:
                logging.info("Market Closed.")
            time.sleep(60)

if __name__ == "__main__":
    scalper = ScalpingEngineV2(symbol="BANKNIFTY")
    scalper.start()
