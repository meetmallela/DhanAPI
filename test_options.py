import os
from core.dhan_client import DhanClient

def test_option_chain():
    client = DhanClient()
    print("Testing option chain in Sandbox...")
    
    # NIFTY 50 (Security ID 13, Segment IDX_I)
    try:
        # Sign: (under_security_id, under_exchange_segment, expiry)
        response = client.dhan.option_chain(
            under_security_id='13',
            under_exchange_segment='IDX_I',
            expiry='2026-04-09' # Next Nifty weekly expiry
        )
        print(f"Option Chain Response Status: {response.get('status')}")
        if response.get('status') == 'success':
            print(f"Data sample (first strike): {response.get('data')[0] if response.get('data') else 'Empty'}")
        else:
            print(f"Error: {response}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_option_chain()
