import time
import pandas as pd
from core.dhan_client import DhanClient
from core.simulator import NiftySimulator
from core.order_placer import OrderPlacer
from strategies.ema_crossover import EMACrossoverStrategy
from datetime import datetime

def get_atm_strike(spot_price, strike_interval=50):
    """Calculates the At-The-Money (ATM) strike price."""
    return round(spot_price / strike_interval) * strike_interval

def main():
    print("--- 🤖 Agentic Trading System - Dhan Sandbox (Nifty 50) ---")
    
    # Initialize components
    # We'll use Sandbox=True for testing
    is_sandbox = True
    client = DhanClient(is_sandbox=is_sandbox)
    sim = NiftySimulator(start_price=22000, volatility=0.0005)
    strategy = EMACrossoverStrategy(short_window=9, long_window=21)
    placer = OrderPlacer(is_sandbox=is_sandbox)
    
    # Pre-generate some historical candles for EMA calculation
    print("Pre-generating historical candles for EMA calculation...")
    df = sim.generate_candles(n=50) 
    
    print("Starting Live Monitoring (Simulated)...")
    
    active_position = None # None, 'BUY', 'SELL'
    
    try:
        while True:
            # 1. Fetch latest candle (simulated every 5 seconds for test)
            new_candle = sim.get_latest_candle()
            df = pd.concat([df, pd.DataFrame([new_candle])], ignore_index=True)
            
            # 2. Calculate Signals
            df_with_signals = strategy.calculate_signals(df)
            
            if df_with_signals is not None:
                latest_signal = df_with_signals.iloc[-1]
                timestamp = latest_signal['timestamp'].strftime('%H:%M:%S')
                spot_price = latest_signal['close']
                ema_9 = latest_signal['ema_9']
                ema_21 = latest_signal['ema_21']
                signal = latest_signal['signal']
                
                # 3. Handle Signals
                if signal == 1 and active_position != 'BUY':
                    print(f"\n[{timestamp}] 📈 BUY SIGNAL DETECTED!")
                    print(f"    Spot: {spot_price} | EMA9: {ema_9:.2f} | EMA21: {ema_21:.2f}")
                    
                    atm_strike = get_atm_strike(spot_price)
                    print(f"    Selected ATM Strike: NIFTY {atm_strike} CE")
                    
                    # 4. Trigger Order Placer
                    # For Sandbox, we use a proxy security_id (e.g., RELIANCE=2885)
                    # to demonstrate successful placement.
                    order_id = placer.place_market_order(
                        security_id='2885',
                        exchange_segment='NSE_EQ',
                        transaction_type='BUY',
                        quantity=1
                    )
                    if order_id:
                        active_position = 'BUY'
                    
                elif signal == -1 and active_position != 'SELL':
                    print(f"\n[{timestamp}] 📉 SELL SIGNAL DETECTED!")
                    print(f"    Spot: {spot_price} | EMA9: {ema_9:.2f} | EMA21: {ema_21:.2f}")
                    
                    atm_strike = get_atm_strike(spot_price)
                    print(f"    Selected ATM Strike: NIFTY {atm_strike} PE")
                    
                    # 4. Trigger Order Placer (SELL/PUT)
                    order_id = placer.place_market_order(
                        security_id='2885',
                        exchange_segment='NSE_EQ',
                        transaction_type='SELL',
                        quantity=1
                    )
                    if order_id:
                        active_position = 'SELL'
                
                else:
                    # Just print status periodically (every 10 ticks)
                    if len(df) % 10 == 0:
                        print(f"[{timestamp}] Spot: {spot_price} | EMA9: {ema_9:.2f} | EMA21: {ema_21:.2f} | Position: {active_position or 'None'}")

            # 5. Sleep for a short interval
            time.sleep(2) 
            
    except KeyboardInterrupt:
        print("\nStopping Signal Monitor...")

if __name__ == "__main__":
    main()
