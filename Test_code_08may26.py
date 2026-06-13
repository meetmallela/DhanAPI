import sys
import json
import requests
from pathlib import Path

# Fix indentation and ensure path is handled correctly
sys.path.insert(0, r'C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib')
from master_resource import MasterResource

def update_bot_config():
    cfg_path = MasterResource.MASTER_ROOT / 'config' / 'health_monitor_config.json'
    
    # Load configuration
    with open(cfg_path, 'r') as f: 
        cfg = json.load(f)
    
    token = cfg['bot_token'].strip()
    url = f'https://api.telegram.org/bot{token}/getUpdates'
    
    try:
        response = requests.get(url, timeout=10).json()
        updates = response.get('result', [])
        
        if updates:
            # Look for the last entry that actually contains a 'message'
            last_message_update = next((u for u in reversed(updates) if 'message' in u), None)
            
            if last_message_update:
                chat = last_message_update['message']['chat']
                chat_id = str(chat['id'])
                
                # Update and save config
                cfg['alert_chat_id'] = chat_id
                with open(cfg_path, 'w') as f: 
                    json.dump(cfg, f, indent=4)
                
                print(f"Saved chat_id={chat_id} ({chat.get('first_name', 'Unknown')})")
            else:
                print("Updates found, but none contain a valid 'message'.")
        else:
            print("Still no messages — send /start to the bot first.")
            
    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == "__main__":
    update_bot_config()