import sqlite3
import json
import time
from datetime import datetime
from master_resource import MasterResource

def push_test_signal():
    db_path = MasterResource.get_trading_db_path()
    conn = sqlite3.connect(db_path, timeout=30)
    cursor = conn.cursor()
    
    # Simulate a signal like the one from yaatra_parser or channel_parsers
    test_symbol = "NIFTY_TEST_EQUITY"
    test_action = "BUY"
    test_price = 22000.50
    
    parsed_data = {
        "symbol": test_symbol,
        "action": test_action,
        "price": test_price,
        "strike": "ATM",
        "expiry": "CURRENT"
    }
    
    # signals columns: channel_id, channel_name, message_id, raw_text, parsed_data, timestamp, processed, ...
    raw_text = f"AUTO-TEST: Buy {test_symbol} at {test_price}"
    
    print(f"Pushing test signal to: {db_path}")
    
    cursor.execute('''
        INSERT INTO signals (channel_id, channel_name, message_id, raw_text, parsed_data, timestamp, processed)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', ('007', 'SIMULATOR', 12345, raw_text, json.dumps(parsed_data), datetime.now().isoformat(), 0))
    
    conn.commit()
    conn.close()
    print(f"✅ Test signal pushed for {test_symbol}!")

if __name__ == "__main__":
    push_test_signal()
