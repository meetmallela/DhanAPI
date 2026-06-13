import os
from core.dhan_client import DhanClient
from datetime import datetime, timedelta

def test_reliance_data():
    client = DhanClient()
    print("Dhan Client initialized for Sandbox.")
    
    try:
        to_date = datetime.now().strftime('%Y-%m-%d')
        from_date = (datetime.now() - timedelta(days=2)).strftime('%Y-%m-%d')
        
        # RELIANCE Security ID = 2885, Segment = NSE_EQ (dhan.NSE)
        print(f"Fetching intraday minute data for RELIANCE from {from_date} to {to_date}...")
        
        data = client.dhan.intraday_minute_data(
            security_id='2885',
            exchange_segment='NSE_EQ',
            instrument_type='EQUITY',
            from_date=from_date,
            to_date=to_date
        )
        
        print(f"Data Response Status: {data.get('status')}")
        if data.get('status') == 'success':
            if data.get('data'):
                print(f"Number of data points: {len(data.get('data'))}")
                print(f"First data point: {data.get('data')[0]}")
            else:
                print("Data list is empty.")
        else:
            print(f"Error Response: {data}")
        
    except Exception as e:
        print(f"Error fetching RELIANCE data: {e}")

if __name__ == "__main__":
    test_reliance_data()
