"""
context_aware_parser.py
-----------------------
LLM-context parser for conversational Telegram channels (STOCK MARKET TRADING TIPS,
Market Yaatra, etc.) where a single message has no meaning without the prior context.

Architecture:
  • Keeps a rolling window of the last MAX_HISTORY messages per channel in memory.
  • On each new message, sends (history + current) to Claude to extract a structured signal.
  • Returns the same dict shape as other channel parsers so _log_and_store_signal works.

Registration in telegram_reader_production.py:
  Import ContextAwareParser and call handle_context_channel() in handle_message()
  for channels that need it (STOCK MARKET TRADING TIPS = -1003800707569).
"""

import json
import logging
import re
from collections import deque
from datetime import datetime
from pathlib import Path

import pytz

log = logging.getLogger("context_aware_parser")
IST = pytz.timezone("Asia/Kolkata")

MAX_HISTORY   = 20    # rolling window per channel
CLAUDE_MODEL  = "claude-haiku-4-5-20251001"   # fast + cheap for signal extraction

# ── Persistent history storage ────────────────────────────────────────────────
# History is saved to disk so it survives process restarts.
_HISTORY_DIR = Path(r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\data\ctx_history")
_HISTORY_DIR.mkdir(parents=True, exist_ok=True)

def _history_path(channel_id: str) -> Path:
    safe = channel_id.replace("-", "m")
    return _HISTORY_DIR / f"{safe}.json"

def _save_history(channel_id: str):
    """Persist the rolling window to disk."""
    try:
        data = list(_history.get(channel_id, []))
        _history_path(channel_id).write_text(json.dumps(data), encoding="utf-8")
    except Exception as e:
        log.debug(f"[CTX] Could not save history for {channel_id}: {e}")

def _load_history(channel_id: str):
    """Load rolling window from disk (called at startup)."""
    path = _history_path(channel_id)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _history[channel_id] = deque(
            [(ts, txt) for ts, txt in data],
            maxlen=MAX_HISTORY
        )
        log.info(f"[CTX] Loaded {len(_history[channel_id])} history messages for {channel_id}")
    except Exception as e:
        log.warning(f"[CTX] Could not load history for {channel_id}: {e}")

# ── Channels that use this parser ─────────────────────────────────────────────
CONTEXT_CHANNELS = {
    "-1003800707569",   # STOCK MARKET TRADING TIPS
}

# ── Rolling message history: channel_id → deque of (timestamp_str, text) ──────
_history: dict[str, deque] = {}

_SYSTEM_PROMPT = """You are a trading signal extractor. You will receive a sequence of
messages from a Telegram trading channel in chronological order. The LAST message is the
most recent one. Your job is to determine if the most recent message, in context of the
prior messages, constitutes an actionable BUY option signal.

Return ONLY valid JSON with these fields if it's a signal:
{
  "is_signal": true,
  "action": "BUY",
  "symbol": "<index or stock name, e.g. NIFTY>",
  "strike": <integer>,
  "option_type": "CE" or "PE",
  "expiry_date": "<YYYY-MM-DD or null>",
  "entry_price": <float or null>,
  "stop_loss": <float or null>,
  "target": <float or null>,
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "reason": "<why you think this is a signal>"
}

If it is NOT a signal (update, noise, discussion), return:
{"is_signal": false}

Rules:
- Only extract BUY signals (no short/sell signals in this system).
- If entry price is a range (e.g. 150-160), use the midpoint.
- Expiry date: nearest weekly Thursday for NIFTY/BANKNIFTY/SENSEX; nearest monthly last-Thursday for stocks.
- Be conservative — LOW confidence means skip (return is_signal: false).
"""


def _build_user_prompt(history_msgs: list[tuple[str, str]], current_text: str) -> str:
    parts = ["=== CHANNEL HISTORY (oldest first) ===\n"]
    for ts, txt in history_msgs:
        parts.append(f"[{ts}] {txt}")
    parts.append(f"\n=== NEW MESSAGE (extract signal from this) ===\n{current_text}")
    return "\n".join(parts)


def _call_claude(user_prompt: str, claude_key: str) -> dict | None:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=claude_key)
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        return json.loads(raw)
    except Exception as e:
        log.warning(f"[CTX_PARSER] Claude call failed: {e}")
        return None


def parse_with_context(channel_id: str, message_text: str, claude_key: str) -> dict | None:
    """
    Main entry point. Call this from handle_message() for context-aware channels.
    Updates rolling history and returns a parsed signal dict or None.
    """
    if channel_id not in CONTEXT_CHANNELS:
        return None

    # Update history (load from disk on first access for this channel)
    if channel_id not in _history:
        _history[channel_id] = deque(maxlen=MAX_HISTORY)
        _load_history(channel_id)
    ts_str = datetime.now(IST).strftime("%H:%M")
    hist   = list(_history[channel_id])          # snapshot before appending
    _history[channel_id].append((ts_str, message_text))
    _save_history(channel_id)                    # persist after every new message

    # Need at least a few messages for context
    if len(hist) < 2 and len(message_text) < 30:
        log.debug("[CTX_PARSER] Not enough history yet, buffering")
        return None

    user_prompt = _build_user_prompt(hist, message_text)
    result      = _call_claude(user_prompt, claude_key)

    if not result or not result.get("is_signal"):
        return None
    if result.get("confidence", "LOW") == "LOW":
        log.info(f"[CTX_PARSER] LOW confidence signal discarded")
        return None

    log.info(
        f"[CTX_PARSER] Signal: {result.get('symbol')} {result.get('strike')} "
        f"{result.get('option_type')} | conf={result.get('confidence')} | {result.get('reason','')}"
    )

    return {
        "action":          result.get("action", "BUY"),
        "symbol":          (result.get("symbol") or "").upper(),
        "strike":          result.get("strike"),
        "option_type":     (result.get("option_type") or "CE").upper(),
        "instrument_type": "OPTIONS",
        "expiry_date":     result.get("expiry_date"),
        "tradingsymbol":   None,   # order placer will resolve
        "quantity":        None,   # order placer will resolve
        "exchange":        "NFO",
        "entry_price":     result.get("entry_price"),
        "stop_loss":       result.get("stop_loss"),
        "target":          result.get("target"),
        "source":          "CONTEXT_AWARE_PARSER",
        "confidence":      result.get("confidence", "MEDIUM"),
    }


def seed_history(channel_id: str, messages: list[tuple[str, str]]):
    """
    Pre-seed the rolling history from fetched past messages.
    messages = list of (timestamp_str, text) tuples, oldest first.
    Persists to disk so future restarts pick it up automatically.
    """
    _history[channel_id] = deque(messages[-MAX_HISTORY:], maxlen=MAX_HISTORY)
    _save_history(channel_id)
    log.info(f"[CTX_PARSER] Seeded {len(_history[channel_id])} messages for channel {channel_id}")


def seed_from_db(channel_id: str, db_password: str = "Krishna@123"):
    """
    Seed rolling history from MySQL (last MAX_HISTORY messages for this channel).
    Called at telegram_reader startup to warm up context without manual history fetch.
    """
    try:
        import mysql.connector
        conn = mysql.connector.connect(
            host="127.0.0.1", port=3306, user="root",
            password=db_password, database="trading_live", autocommit=False,
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT DATE_FORMAT(timestamp, '%%H:%%i'), raw_text
            FROM   signals
            WHERE  channel_id = %s
              AND  raw_text IS NOT NULL
              AND  LENGTH(raw_text) > 10
            ORDER  BY timestamp DESC
            LIMIT  %s
        """, (channel_id, MAX_HISTORY))
        rows = cur.fetchall()
        conn.close()
        if rows:
            # Reverse so oldest is first
            msgs = [(ts, txt) for ts, txt in reversed(rows)]
            seed_history(channel_id, msgs)
        else:
            log.info(f"[CTX_PARSER] No DB history for {channel_id}")
    except Exception as e:
        log.warning(f"[CTX_PARSER] DB seed failed for {channel_id}: {e}")
