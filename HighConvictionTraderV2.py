import time
import logging
import sys
import pandas as pd
from datetime import datetime, date
from SignalEngine import SignalEngine
from OptionSentimentMonitor import OptionSentimentMonitor
from PaperTrader import PaperTrader
from StrategyModuleV2 import OptionStrategiesV2

# Master Config Import
sys.path.append(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
from master_resource import get_kite_config
from kiteconnect import KiteConnect

# Generate V2 Timestamps
now_str = datetime.now().strftime("%d%m%y_%H_%M_%S")
log_filename = f"high_conviction_V2_log_{now_str}.log"
trade_log_filename = f"paper_trades_V2_{now_str}.csv"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - HC_V2 - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(log_filename, encoding='utf-8'), logging.StreamHandler(sys.stdout)]
)

class HighConvictionTraderV2:
    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol
        self.config = get_kite_config()
        self.kite = KiteConnect(api_key=self.config['api_key'])
        self.kite.set_access_token(self.config['access_token'])
        self.sentiment_monitor = OptionSentimentMonitor(self.kite, symbol=symbol)
        self.paper_trader = PaperTrader(log_file=trade_log_filename)
        self.token = 256265 if symbol == "NIFTY" else 260105
        self.strike_inc = 50 if symbol == "NIFTY" else 100

    def get_mtf_alignment(self):
        """MTF Check: Ensure 15m trend matches the 5m signal"""
        try:
            to_date = datetime.now()
            from_date = to_date - pd.Timedelta(days=5)
            # Fetch 15-minute data
            records = self.kite.historical_data(self.token, from_date, to_date, "15minute")
            df = pd.DataFrame(records)
            df['ema44'] = df['close'].ewm(span=44, adjust=False).mean()
            
            last_price = df.iloc[-1]['close']
            ema44 = df.iloc[-1]['ema44']
            
            if last_price > ema44: return "BULLISH"
            if last_price < ema44: return "BEARISH"
        except Exception as e:
            logging.error(f"MTF Error: {e}")
        return "NEUTRAL"

    def run_iteration(self):
        logging.info(f"--- HC_V2 Iteration: {datetime.now().strftime('%H:%M:%S')} ---")
        self.paper_trader.monitor_positions(self.kite)
        
        # 1. Fetch Spot Data (Signals) and Futures Data (Volume)
        to_date = datetime.now()
        from_date = to_date - pd.Timedelta(days=5)
        
        try:
            # Fetch Spot Index
            records_spot = self.kite.historical_data(self.token, from_date, to_date, "5minute")
            df_spot = pd.DataFrame(records_spot)
            
            # Fetch Futures (for Volume)
            fut_token, fut_symbol = self.sentiment_monitor.get_current_fut_token()
            if fut_token:
                records_fut = self.kite.historical_data(fut_token, from_date, to_date, "5minute")
                df_fut = pd.DataFrame(records_fut)
                
                # Align dataframes by date to ensure Volume matches Price
                df_fut = df_fut.set_index('date')
                df_spot = df_spot.set_index('date')
                df_spot['volume'] = df_fut['volume']
                df_spot = df_spot.reset_index()
                
                logging.info(f"Using Aligned Futures Volume from {fut_symbol}")
            else:
                logging.warning("Could not fetch Futures volume. Volume filter may fail.")

            df = OptionStrategiesV2.ema44_high_conviction_v2(df_spot)
            last_row = df.iloc[-1]
        except Exception as e:
            logging.error(f"Error in run_iteration (Fetching/Processing): {e}")
            return
        
        # 2. Check Price Signal
        price_signal = "NONE"
        if last_row['buy_ce']: price_signal = "BUY_CE"
        elif last_row['buy_pe']: price_signal = "BUY_PE"
        
        if price_signal == "NONE":
            return

        # 3. V2 Enhancement: MTF Alignment
        mtf_trend = self.get_mtf_alignment()
        if (price_signal == "BUY_CE" and mtf_trend != "BULLISH") or \
           (price_signal == "BUY_PE" and mtf_trend != "BEARISH"):
            logging.info(f"V2 FILTER: Discarded {price_signal} due to MTF mismatch ({mtf_trend} on 15m)")
            return

        # 4. Sentiment Check (Option Chain)
        sentiment = self.sentiment_monitor.analyze_sentiment()
        conviction = False
        if price_signal == "BUY_CE" and "BULLISH" in sentiment['signal']: conviction = True
        elif price_signal == "BUY_PE" and "BEARISH" in sentiment['signal']: conviction = True
        
        if conviction:
            logging.info(f"🔥🔥 HC_V2 HIGH CONVICTION! ADX: {last_row['adx']:.2f} | MTF: {mtf_trend}")
            self.execute_smart_entry(price_signal, sentiment['spot'], last_row['atr'])

    def execute_smart_entry(self, signal_type, spot_price, atr):
        """V2: Smart Strike Selection + ATR-based SL"""
        # 1. Select Strike based on Day of Week
        weekday = date.today().weekday() # 0=Mon, 2=Wed, 3=Thu
        strike_offset = 0
        if weekday in [1, 2, 3]: # Tue, Wed, Thu (Near Expiry)
            strike_offset = self.strike_inc # Go 1-strike In-The-Money
            logging.info("V2 SMART STRIKE: Expiry week detected. Selecting ITM strike.")

        option_type = "CE" if signal_type == "BUY_CE" else "PE"
        target_strike = (round(spot_price / self.strike_inc) * self.strike_inc)
        if option_type == "CE": target_strike -= strike_offset
        else: target_strike += strike_offset

        # 2. Get Instrument and Entry Price
        sentiment = self.sentiment_monitor.analyze_sentiment()
        match = next((i for i in sentiment['details'] if i["strike"] == target_strike and i["type"] == option_type), None)
        
        if not match:
            # Fallback to ATM if ITM not in monitoring list
            match = next((i for i in sentiment['details'] if i["type"] == option_type), None)
        
        if match:
            # 3. V2 Enhancement: ATR-based Dynamic SL
            # Calculate SL: 1.5 * ATR from Entry
            # For simplicity, we convert index ATR to option points (approx 0.5 delta)
            sl_points = atr * 0.5 * 1.5
            entry_price = match['ltp']
            sl_price = entry_price - sl_points
            
            # Target remains 1:2 or fixed 50%
            target_price = entry_price + (sl_points * 2)

            self.paper_trader.enter_trade(
                symbol=self.symbol,
                option_symbol=match['symbol'],
                option_type=option_type,
                entry_price=entry_price,
                sl_price=sl_price,
                target_price=target_price
            )
            logging.info(f"[HC_V2] Entered {match['symbol']} at {entry_price}. Dynamic ATR SL: {sl_price:.2f} | Tgt: {target_price:.2f}")

    def start(self):
        logging.info(f"V2 High Conviction Engine Started for {self.symbol}")
        while True:
            now = datetime.now().time()
            if datetime.strptime("09:15", "%H:%M").time() <= now <= datetime.strptime("15:30", "%H:%M").time():
                self.run_iteration()
            time.sleep(60)

if __name__ == "__main__":
    trader = HighConvictionTraderV2(symbol="NIFTY")
    trader.start()
