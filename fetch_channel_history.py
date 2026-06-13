"""
fetch_channel_history.py
------------------------
One-shot script: connect to Telegram, download the last N messages from
specified channels, print the channel IDs, and save samples to
  channel_samples/<channel_name>.txt

Run once after adding new channels so we can build parsers for them.

Usage:
    python fetch_channel_history.py
    python fetch_channel_history.py --limit 100
"""

import asyncio
import argparse
import io
import os
import sys
from pathlib import Path
from datetime import datetime

# Force UTF-8 stdout so emoji/arrows in channel messages don't crash prints
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ── Path setup ────────────────────────────────────────────────────────────────
DIR        = Path(__file__).parent
MASTER_LIB = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib")
if str(MASTER_LIB) not in sys.path:
    sys.path.insert(0, str(MASTER_LIB))

from master_resource import MasterResource
from telethon import TelegramClient

cfg            = MasterResource.get_telegram_config()
API_ID         = cfg['api_id']
API_HASH       = cfg['api_hash']
PHONE          = cfg.get('phone', cfg.get('PHONE', ''))

# ── Channels to fetch (add new ones here) ─────────────────────────────────────
CHANNELS = [
    # Public channels by username
    "freemcxcalls1",
    "expiryking06",
    "mcx_forex_strong_level",
    # Private channels by integer ID (for comparison/verification)
    -1003053351657,   # Investing Korner
    -1001404315099,   # FUTURES SEGMENT BATCH
    -1003800707569,   # STOCK MARKET TRADING TIPS
    -1003770951544,   # Market Yaatra Official
    # ── Added 2026-06-05 ───────────────────────────────────────────────────────
    -1003282204738,   # JP Paper trade (refresh parser based on current messages)
    -1003115553842,   # Premium Nifty Banknifty group no 3
    # ── Added 2026-06-06 ───────────────────────────────────────────────────────
    -1002670475451,   # New channel (parser TBD - msg ~11200)
]

OUT_DIR = DIR / "channel_samples"
OUT_DIR.mkdir(exist_ok=True)


async def fetch(limit: int):
    client = TelegramClient(str(DIR / "trading_bot"), API_ID, API_HASH)
    await client.start(PHONE)
    me = await client.get_me()
    print(f"Connected as {me.phone}\n{'='*70}")

    for channel_ref in CHANNELS:
        try:
            entity = await client.get_entity(channel_ref)
            ch_id   = entity.id
            ch_name = getattr(entity, 'title', str(channel_ref))
            print(f"\n[CHANNEL] {ch_name}")
            print(f"  ID      : -{100_000_000_000 + ch_id} (or {ch_id})")
            print(f"  Username: {getattr(entity, 'username', 'N/A')}")
            print(f"  Fetching last {limit} messages...")

            messages = await client.get_messages(entity, limit=limit)
            lines = []
            for msg in reversed(messages):
                if not msg.text:
                    continue
                ts  = msg.date.strftime("%Y-%m-%d %H:%M")
                txt = msg.text.replace('\n', ' | ')
                lines.append(f"[{ts}] {txt}")

            safe_name = ch_name.replace(' ', '_').replace('/', '_')[:40]
            out_file  = OUT_DIR / f"{safe_name}.txt"
            out_file.write_text('\n'.join(lines), encoding='utf-8', errors='replace')
            print(f"  Saved {len(lines)} messages -> {out_file}")

            # Print last 10 for quick review
            print(f"\n  --- Last 10 messages ---")
            for l in lines[-10:]:
                print(f"  {l}")

        except Exception as e:
            print(f"[ERROR] {channel_ref}: {e}")

    await client.disconnect()
    print(f"\n{'='*70}")
    print(f"Done. Samples saved to: {OUT_DIR}")
    print("Share the .txt files so parsers can be built for each channel.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50,
                        help="Number of recent messages to fetch per channel (default 50)")
    args = parser.parse_args()
    asyncio.run(fetch(args.limit))
