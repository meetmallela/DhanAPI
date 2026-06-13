import os
from core.dhan_client import DhanClient
from datetime import datetime, timedelta

def test_nifty_data():
    client = DhanClient()
    print("Dhan Client initialized for Sandbox.")
    
    try:
        to_date = datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        
        print(f"Fetching intraday minute data for Nifty 50 from {from_date} to {to_date}...")
        
        # Dhan library signature for intraday_minute_data: 
        # dhan.intraday_minute_data(security_id, exchange_segment, instrument_type, from_date, to_date)
        
        data = client.dhan.intraday_minute_data(
            security_id='13',
            exchange_segment='IDX_I',
            instrument_type='INDEX',
            from_date=from_date,
            to_date=to_date
        )
        
        print(f"Data Response Status: {data.get('status')}")
        if data.get('status') == 'success':
            # Check the keys in the data to see what's returned
            if data.get('data'):
                print(f"Number of data points: {len(data.get('data'))}")
                # Some keys might be 'start_time', 'open', 'high', 'low', 'close', 'volume'
                print(f"First data point: {data.get('data')[0]}")
            else:
                print("Data list is empty.")
        else:
            print(f"Error Response: {data}")
        
    except Exception as e:
        print(f"Error fetching NIFTY data: {e}")

if __name__ == "__main__":
    test_nifty_data()
