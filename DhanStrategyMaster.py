import pandas as pd
import numpy as np
import time
import logging
from datetime import datetime
from core.dhan_client import DhanClient
from core.order_placer import OrderPlacer
from master_resource import MasterResource

# Setup Logger
logger = MasterResource.setup_shared_logger("dhan_strategy_master")

class DhanStrategyMaster:
    def __init__(self, is_sandbox=True):
        self.client = DhanClient(is_sandbox=is_sandbox)
        self.placer = OrderPlacer(is_sandbox=is_sandbox)
        self.is_sandbox = is_sandbox
        
        # Data storage for multiple instruments
        self.data = {
            "NIFTY": pd.DataFrame(),
            "RELIANCE": pd.DataFrame(),
            "HDFC": pd.DataFrame()
        }
        
        logger.info(f"🚀 Dhan Strategy Master Initialized (Sandbox={is_sandbox})")

    def calculate_vwap(self, df):
        """Calculates Daily VWAP."""
        if df.empty: return df
        df = df.copy()
        df['tp'] = (df['high'] + df['low'] + df['close']) / 3
        df['vwap'] = (df['tp'] * df['volume']).cumsum() / df['volume'].cumsum()
        return df

    def get_pair_leadership_bias(self):
        """
        Logic from pair_leadership_phase1_bt.py
        Checks if RELIANCE and HDFC have the same bias.
        """
        rel_df = self.data["RELIANCE"]
        hdfc_df = self.data["HDFC"]
        
        if len(rel_df) < 20 or len(hdfc_df) < 20:
            return "NEUTRAL"
            
        rel_last = rel_df.iloc[-1]
        hdfc_last = hdfc_df.iloc[-1]
        
        rel_bias = "NEUTRAL"
        if rel_last['close'] > rel_last['vwap'] and rel_last['close'] > rel_df.iloc[-5:-1]['high'].max():
            rel_bias = "BULLISH"
        elif rel_last['close'] < rel_last['vwap'] and rel_last['close'] < rel_df.iloc[-5:-1]['low'].min():
            rel_bias = "BEARISH"
            
        hdfc_bias = "NEUTRAL"
        if hdfc_last['close'] > hdfc_last['vwap'] and hdfc_last['close'] > hdfc_df.iloc[-5:-1]['high'].max():
            hdfc_bias = "BULLISH"
        elif hdfc_last['close'] < hdfc_last['vwap'] and hdfc_last['close'] < hdfc_df.iloc[-5:-1]['low'].min():
            hdfc_bias = "BEARISH"
            
        if rel_bias == hdfc_bias:
            return rel_bias
        return "NEUTRAL"

    def get_ema_9_21_signal(self):
        """Logic for EMA 9/21 Crossover."""
        df = self.data["NIFTY"]
        if len(df) < 21: return 0
        
        ema_9 = df['close'].ewm(span=9, adjust=False).mean()
        ema_21 = df['close'].ewm(span=21, adjust=False).mean()
        
        curr_9, prev_9 = ema_9.iloc[-1], ema_9.iloc[-2]
        curr_21, prev_21 = ema_21.iloc[-1], ema_21.iloc[-2]
        
        if curr_9 > curr_21 and prev_9 <= prev_21:
            return 1 # BUY
        if curr_9 < curr_21 and prev_9 >= prev_21:
            return -1 # SELL
        return 0

    def run_engine(self):
        """Main loop to fetch data and check all strategies."""
        logger.info("Starting Multi-Strategy Engine...")
        while True:
            try:
                # 1. Fetch Data (Simulated for holiday/demo)
                self.sync_market_data()
                
                # 2. Strategy 1: Pair Leadership
                pair_bias = self.get_pair_leadership_bias()
                if pair_bias != "NEUTRAL":
                    logger.info(f"🎯 PAIR LEADERSHIP SIGNAL: {pair_bias}")
                    self.execute_trade("NIFTY", "BUY" if pair_bias == "BULLISH" else "SELL", "PairLeadership")
                
                # 3. Strategy 2: EMA 9/21 Crossover
                ema_signal = self.get_ema_9_21_signal()
                if ema_signal != 0:
                    logger.info(f"🎯 EMA 9/21 SIGNAL: {'BUY' if ema_signal == 1 else 'SELL'}")
                    self.execute_trade("NIFTY", "BUY" if ema_signal == 1 else "SELL", "EMA_9_21")
                
                # 4. Strategy 3: Option Buying (Placeholder for V2 logic)
                # ...
                
            except Exception as e:
                logger.error(f"Engine Error: {e}")
            
            time.sleep(10)

    def sync_market_data(self):
        """Simulates or fetches live market data for all instruments."""
        # For demonstration on a holiday, we generate fake ticks
        for symbol in self.data:
            new_tick = {
                "high": 100 + np.random.rand(),
                "low": 98 + np.random.rand(),
                "close": 99 + np.random.rand(),
                "volume": 1000 * np.random.rand(),
                "timestamp": datetime.now()
            }
            # Add to dataframe and calculate VWAP
            df = pd.concat([self.data[symbol], pd.DataFrame([new_tick])], ignore_index=True)
            self.data[symbol] = self.calculate_vwap(df)

    def execute_trade(self, symbol, action, strategy_name):
        """Logs signal to master DB and places Dhan order."""
        logger.info(f"🚀 Executing {action} for {symbol} via {strategy_name}")
        
        # 1. Place Dhan Sandbox Order
        # Using Reliance (2885) as a test proxy in Sandbox
        order_id = self.placer.place_market_order(
            security_id='2885',
            exchange_segment='NSE_EQ',
            transaction_type=action,
            quantity=1
        )
        
        if order_id:
            # 2. Log to Master DB (signals table)
            try:
                conn = sqlite3.connect(MasterResource.get_trading_db_path())
                cursor = conn.cursor()
                parsed_data = {"symbol": symbol, "action": action, "strategy": strategy_name}
                cursor.execute('''
                    INSERT INTO signals (channel_name, raw_text, parsed_data, timestamp, processed, order_id, order_status)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (strategy_name, f"Auto-Trade: {strategy_name}", json.dumps(parsed_data), datetime.now().isoformat(), 1, order_id, 'PLACED'))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"DB Logging Error: {e}")

if __name__ == "__main__":
    engine = DhanStrategyMaster(is_sandbox=True)
    engine.run_engine()
