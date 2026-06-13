"""
channel_parsers.py
------------------
Dedicated parsers for channels. Covers 7 named parsers + 1 universal fallback.

1. ShortTermParser  — INDEX OPTIONS PRIME + COPY MY TRADES BANKNIFTY + INDEX OPTIONS PRIME (BTST)
                      Format: "SHORT TERM BUY NIFTY MARCH 22700 PE AROUND 350 TARGET 400,450,500+ STOP LOSS 280"

2. WealthWorldParser — Wealth World Trading Hub + similar channels
                       Format: "BUY NIFTY 22650Ce ABOVE 130 SL- 115 Tg- 137/148/170++"
                       Also:   "NIFTY 22600 PUT BUY ABOVE :- 42/- TARGET :- 55,60++"

3. SidharthParser   — SIDHARTH SINGH PREMIUM (conversational drip style)
                       Entry:  "Buy 23000 call near 35-40  Target - 80"

4. JPParser         — JP Paper trade (short structured format)

5. MCXPremiumParser — MCX PREMIUM (-1002770917134) — three sub-formats:
                       a) Stock options:  "#TRENT MAY 4100 CE / ABOVE 130 / TGT 150,180 / Sl 120"
                       b) Index options:  "BUY - NIFTY 23600 CE / NEAR LEVEL -- 215 / TARGET 250/300 / STOPLOSS -- 190"
                       c) MCX commodity: "COMMODITY_MCX_TRAD / BUY CRUDEOIL 9200 CE / NEAR LEVEL 265 / TARGET 300/340 / STOPLOSS 250"

6. StockOptionsPrimeParser — STOCK OPTIONS PRIME (-1001553033593)
                      Format: "SHORT TERM BUY TRENT JUNE 4100 CE AROUND 185-188 TARGET 230,280,340+ STOP LOSS 120"
                      Correctly extracts stock symbol BEFORE the expiry month token.
                      Sets position_type=LONGTERM: multi-day swing, no time_sl, no EOD exit, SL checked 4-hourly.

6. GenericParser    — Universal fallback for unknown channels.
                       Handles compact, multiline, pipe-separated, emoji-heavy formats.
                       Registered for all 5 channels whose signal format is unknown:
                         -1001478345624, -1003053351657, -1001822833953,
                         luxurywithtrading (@luxurywithtrading), -1003800707569

Integration in dhan_tg_trader.py:
    from channel_parsers import get_channel_parser
    parser_fn = get_channel_parser(chat_id)
    if parser_fn:
        parsed = parser_fn(message_text)
"""

import re
import logging
import calendar
from datetime import datetime, date
import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# ── Channel ID → parser mapping ───────────────────────────────────────────────
CHANNEL_PARSER_MAP = {
    "-1003770951544": "investingkorner",       # Investing Korner (CMP;entry | SL;sl | Tgt;t1,t2 format)
    "-1002770917134": "mcxpremium",          # MCX PREMIUM (stock options + index options + MCX commodity)
    "-1001858110716": "shortterm",          # INDEX OPTIONS PRIME
    "-1001903138387": "shortterm",          # COPY MY TRADES BANKNIFTY
    "-1001670038276": "shortterm",          # STOCK OPTIONS PRIME (second channel with same TG name)
    "-1001553033593": "stockoptionsprime",  # STOCK OPTIONS PRIME (confirmed via logs 02-Jun-2026)
    "-1001542890753": "shortterm",          # INDEX OPTIONS PRIME (was labelled BTST EQUITY CASH AND FUTURES)
    "-1001404315099": "futuresegment",      # FUTURES SEGMENT BATCH (stock futures + options)
    "-1001967914715": "commodityprime",     # COMMODITY OPTIONS PRIME (long-term positional, 6mo-1yr)
    # ── New channels (IDs confirmed 02-Jun-2026) ───────────────────────────────
    "-1003853936992": "sidharth",           # EXPIRY KING — drip format identical to Sidharth
    "-1001294857397": "generic",            # Mcx Trading King Official Group (MCX futures)
    "-1001893868490": "generic",            # MCX TRADERS (commentary, no structured signals)
    "-1003089362819": "wealthworld", # Wealth World Trading Hub
    "-1003658135032": "sidharth",    # SIDHARTH SINGH PREMIUM
    "-1003282204738": "jp",          # JP Paper trade - May-2026
    "-1003115553842": "premiumnb",   # Premium Nifty Banknifty group no 3
    "-1002670475451": "pgso",        # Premium Group Stock Option (Nifty/Sensex options)
    # ── New channels (signal format unknown — generic parser) ─────────────────
    "-1001478345624": "generic",     # VISION BY SMK
    "-1003053351657": "generic",     # STOCK MARKET TRADING TIPS
    "-1001822833953": "generic",     # COMMODITY OPTIONS PRIME (inactive since May-2026; duplicate name of -1001967914715)
    "-1003800707569": "generic",     # Momentum to Multibagger - Chikoutrader
    # luxurywithtrading is a public channel — Telethon delivers it with its
    # username as a negative int ID at runtime; also register the username string
    # so manual channel-name-based lookups work.
    "luxurywithtrading": "generic",  # @luxurywithtrading public channel
}

# ── Month map ─────────────────────────────────────────────────────────────────
MONTH_MAP = {
    "jan":1,"feb":2,"mar":3,"march":3,"apr":4,"may":5,"jun":6,
    "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12
}

INDEX_MAP = {
    "nifty":"NIFTY","banknifty":"BANKNIFTY","bank nifty":"BANKNIFTY",
    "sensex":"SENSEX","sensed":"SENSEX",   # "sensed" is a common typo in JP channel
    "finnifty":"FINNIFTY","midcpnifty":"MIDCPNIFTY",
    "bankex":"BANKEX",
}

# ── Noise patterns shared across parsers ──────────────────────────────────────
_NOISE = re.compile(
    r'(safe traders? book|book profit|target done|target hit|screenshots?|'
    r'limited quantity|go with|please share|exit around|join fast|'
    r'jackpot call coming|fees\s*=|account management|capital dubal|'
    r'tomorrow.*expiry|premium join|loss cover plan)',
    re.IGNORECASE
)

def _is_noise(text):
    return bool(_NOISE.search(text))

def _current_expiry_month():
    """Returns current or next month abbreviation for expiry guessing."""
    now = datetime.now(IST)
    return now.strftime("%b").upper()

def _parse_month(text):
    """Extract month string from text, return 3-char uppercase e.g. MAR."""
    m = re.search(
        r'\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
        r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b',
        text, re.IGNORECASE
    )
    if m:
        raw = m.group(1).lower()[:3]
        return raw.upper() if raw != "mar" else "MAR"
    return _current_expiry_month()

def _parse_targets(text):
    """Extract first numeric target from strings like '400,450,500+' or '130/160+'"""
    m = re.search(r'(?:target|tgt|tg)[:\-\s]*(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    # bare numbers after TARGET keyword already consumed
    return None

def _parse_sl(text):
    """Extract SL from various formats."""
    patterns = [
        r'stop\s*loss\s*[:\-]?\s*(\d+(?:\.\d+)?)',
        r'\bsl[\s\-:]+(\d+(?:\.\d+)?)',
        r's\.?l\.?\s*[:\-]\s*(\d+(?:\.\d+)?)',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return float(m.group(1))
    return None

def _parse_entry(text):
    """Extract entry price from AROUND / ABOVE / CMP / near patterns."""
    patterns = [
        r'(?:around|above|cmp|near|entry)\s*[:\-]?\s*(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)',
        r'buy\s*above\s*[:\-]+\s*(\d+(?:\.\d+)?)',
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            # If range like "340-345", take midpoint
            raw = m.group(1)
            range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw)
            if range_m:
                return (float(range_m.group(1)) + float(range_m.group(2))) / 2
            return float(raw.split()[0])
    return None

def _normalise_instrument(raw):
    """Map raw name to canonical instrument + type."""
    r = raw.lower().strip().replace(" ", "").replace("#", "")
    for key, val in INDEX_MAP.items():
        if key.replace(" ", "") == r:
            return val, "INDEX_OPT"
    return raw.upper().replace(" ",""), "STOCK_OPT"

def _build_result(instrument, itype, strike, opt_type, month, entry, sl, target, source):
    if sl is None and entry:
        sl = round(entry * 0.85, 2)  # 15% default buffer
    opt = opt_type.upper().replace("CALL","CE").replace("PUT","PE")

    # Resolve expiry_date via InstrumentLookup if available, else use placeholder
    expiry_date = None
    tradingsymbol = None
    quantity = None
    try:
        from instrument_lookup import InstrumentLookup
        il = InstrumentLookup()
        result_lookup = il.resolve(instrument, int(strike), opt)
        if result_lookup:
            tradingsymbol = result_lookup.get("tradingsymbol")
            expiry_date   = result_lookup.get("expiry_date")
            quantity      = result_lookup.get("lot_size")
    except Exception:
        pass

    # Fallback tradingsymbol if lookup failed
    if not tradingsymbol:
        tradingsymbol = f"{instrument}{month}{strike}{opt}"

    result = {
        # Fields expected by _log_and_store_signal and order placer
        "action":          "BUY",
        "symbol":          instrument,           # order placer uses "symbol"
        "strike":          int(strike),          # order placer uses "strike"
        "option_type":     opt,
        "instrument_type": itype,
        "expiry_date":     str(expiry_date) if expiry_date else None,
        "tradingsymbol":   tradingsymbol,
        "quantity":        quantity,
        "entry_price":     entry,
        "stop_loss":       sl,
        "target":          target,
        # Also keep alternate names for compatibility
        "instrument":      instrument,
        "strike_price":    int(strike),
        "expiry_str":      month,
        "source":          source,
        "confidence":      "HIGH" if (sl and target) else "MEDIUM",
    }
    log.info(f"[{source}] SIGNAL: {instrument} {strike} {opt} Entry={entry} SL={sl} Tgt={target}")
    return result


# ═══════════════════════════════════════════════════════════════════════
# PARSER 1 — ShortTerm (INDEX OPTIONS PRIME / COPY MY TRADES / STOCK OPTIONS PRIME)
# Format: SHORT TERM BUY NIFTY MARCH 22700 PE AROUND 350 TARGET 400,450,500+ STOP LOSS 280
# ═══════════════════════════════════════════════════════════════════════

# Matches: "SHORT TERM [BUY] NIFTY MARCH 22700 PE AROUND 350 TARGET 400 STOP LOSS 280"
_ST_SIGNAL = re.compile(
    r'(?:short\s*term\s*)?(?:buy\s+)?'
    r'(nifty|banknifty|bank\s*nifty|sensex|finnifty|midcpnifty|bankex|[A-Z]{3,12})'  # instrument
    r'\s+'
    r'(?:\d{1,2}\s+)?(?:(jan|feb|mar(?:ch)?|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+)?'  # optional day + month
    r'(\d{4,6})'               # strike
    r'\s*(ce|pe|call|put)\s*'  # option type
    r'(?:around|above|buy\s+above)?\s*[:\-]?\s*(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)'  # entry
    r'.*?target\s*[:\-]?\s*(\d+(?:\.\d+)?)'   # first target
    r'.*?stop\s*loss\s*(\d+(?:\.\d+)?)',       # stop loss
    re.IGNORECASE | re.DOTALL
)

# Also matches: "EICHERMOT MARCH 6800 CE AROUND 190 TARGET 230 STOP LOSS 150"
_ST_SIGNAL2 = re.compile(
    r'(nifty|banknifty|bank\s*nifty|sensex|finnifty|[A-Z]{3,12})\s+'
    r'(jan|feb|mar(?:ch)?|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+'
    r'(\d{4,6})\s+(ce|pe|call|put)'
    r'.*?(?:around|above)\s*[:\-]?\s*(\d+(?:\.\d+)?(?:[-/]\d+(?:\.\d+)?)?)'
    r'.*?target\s*[:\-]?\s*(\d+(?:\.\d+)?)'
    r'.*?stop\s*loss\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE | re.DOTALL
)

def parse_shortterm(text):
    if _is_noise(text):
        return None

    # Must have "SHORT TERM" or clear structured signal
    if not re.search(r'short\s*term|(?:target|tgt).*(?:stop.loss|sl)', text, re.IGNORECASE):
        return None

    for pattern in [_ST_SIGNAL, _ST_SIGNAL2]:
        m = pattern.search(text)
        if m:
            groups = m.groups()
            if len(groups) == 7:
                raw_inst, raw_month, strike, opt, raw_entry, tgt, sl = groups
            else:
                raw_inst, raw_month, strike, opt, raw_entry, tgt, sl = groups
            
            name, itype = _normalise_instrument(raw_inst)
            month = _parse_month(raw_month or "") if raw_month else _current_expiry_month()
            
            # Parse entry (handle range)
            range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_entry.strip())
            if range_m:
                entry = (float(range_m.group(1)) + float(range_m.group(2))) / 2
            else:
                entry = float(raw_entry.split()[0])
            
            return _build_result(
                name, itype, strike, opt, month,
                entry, float(sl), float(tgt), "SHORTTERM_PARSER"
            )
    return None


# ═══════════════════════════════════════════════════════════════════════
# PARSER 2 — WealthWorld
# Format A: "BUY NIFTY 22650Ce ABOVE 130 SL- 115 Tg- 137/148/170++"
# Format B: "NIFTY 22600 PUT BUY ABOVE :- 42/- TARGET :- 55,60++"
# ═══════════════════════════════════════════════════════════════════════

_WW_FORMAT_A = re.compile(
    r'buy\s+(nifty|banknifty|sensex|finnifty|[A-Z]{3,12})\s+'
    r'(\d{4,6})\s*(ce|pe|call|put|[A-Z]{2,4}(?:ce|pe))\s+'
    r'(?:above|around)?\s*(\d+(?:\.\d+)?)'   # entry
    r'.*?sl[\s\-:]+(\d+(?:\.\d+)?)'           # SL
    r'.*?tg[\s\-:]+(\d+(?:\.\d+)?)',           # first target
    re.IGNORECASE | re.DOTALL
)

_WW_FORMAT_B = re.compile(
    r'(nifty|banknifty|sensex|finnifty|[A-Z]{3,12})\s+'
    r'(\d{4,6})\s*(put|call|ce|pe)\s+'
    r'(?:buy\s+)?above\s*[:\-]+\s*(\d+(?:\.\d+)?)/?' # entry (buy optional)
    r'.*?target\s*[:\-]+\s*(\d+(?:\.\d+)?)',     # target
    re.IGNORECASE | re.DOTALL
)

# Noise specific to Wealth World
_WW_NOISE = re.compile(
    r'(\d+\s*[🔥🚀💥✅]|account management|fees\s*=|loss cover|'
    r'join fast|capital dubal|tomorrow.*expiry|premium join)',
    re.IGNORECASE
)

def parse_wealthworld(text):
    if _is_noise(text) or _WW_NOISE.search(text):
        return None

    # Format A: BUY NIFTY 22650Ce ABOVE 130 SL-115 Tg-137  (requires BUY)
    m = _WW_FORMAT_A.search(text) if re.search(r'\bbuy\b', text, re.IGNORECASE) else None
    if m:
        raw_inst, strike, raw_opt, entry, sl, tgt = m.groups()
        # Clean option type — e.g. "22650Ce" embedded
        opt_m = re.search(r'(ce|pe|call|put)', raw_opt, re.IGNORECASE)
        opt = opt_m.group(1) if opt_m else raw_opt
        name, itype = _normalise_instrument(raw_inst)
        month = _parse_month(text)
        return _build_result(
            name, itype, strike, opt, month,
            float(entry), float(sl), float(tgt), "WEALTHWORLD_PARSER"
        )

    # Format B: NIFTY 22600 PUT BUY ABOVE :- 42/- TARGET :- 55
    m = _WW_FORMAT_B.search(text)
    if m:
        raw_inst, strike, opt, entry, tgt = m.groups()
        name, itype = _normalise_instrument(raw_inst)
        month = _parse_month(text)
        sl = _parse_sl(text)
        return _build_result(
            name, itype, strike, opt, month,
            float(entry), sl, float(tgt), "WEALTHWORLD_PARSER"
        )
    return None


# ═══════════════════════════════════════════════════════════════════════
# PARSER 3 — Sidharth Singh (conversational drip)
# Entry signals:
#   "Buy 23000 call near 35-40  Target - 80"
#   "BUY 22900 PUT near 20-25  Target - 50/80/120"
#   "22900 call buy" (followed by "Target - 20" in next msg — handled via state)
#   "Buy full quantity near 25-30"  (update, skip)
# ═══════════════════════════════════════════════════════════════════════

# State: last instrument seen in Sidharth channel for multi-message drip
_sidharth_state = {
    "instrument": None,   # e.g. "NIFTY"
    "strike":     None,   # e.g. "23000"
    "opt":        None,   # CE or PE
    "entry_low":  None,
    "entry_high": None,
    "timestamp":  None,
}
_STATE_TIMEOUT_SECS = 300  # 5 min — if no follow-up, clear state

# Noise for Sidharth
_SS_NOISE = re.compile(
    r'(mark my word|market will touch|now we are above|no shorting|'
    r'add.*watchlist|scalping time|buy on deep|atleast|hold tightly|'
    r'capital double|boom|entry missed|done\s*[☑✅]|enjoy your day|'
    r'tomorrow.*expiry|fire\s*🔥|no more call|be ready|wait for level|'
    r'data is still|still.*bullish|sell on rise)',
    re.IGNORECASE
)

# Primary entry signal: "Buy 23000 call near 35-40 Target - 80"
_SS_ENTRY = re.compile(
    r'buy\s+'
    r'(?:(nifty|banknifty|sensex)\s+)?'    # optional index name
    r'(\d{4,6})\s+'                         # strike
    r'(call|put|ce|pe)\s+'                  # option type
    r'(?:near|around|above|cmp)?\s*'
    r'(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)'  # entry (range ok)
    r'(?:.*?target\s*[-:\s]+(\d+(?:\.\d+)?))?',      # optional target
    re.IGNORECASE
)

# Two-word signal: "22900 call buy" or "22900 put"
_SS_INSTRUMENT_ONLY = re.compile(
    r'(\d{4,6})\s+(call|put|ce|pe)(?:\s+buy)?',
    re.IGNORECASE
)

def parse_sidharth(text):
    global _sidharth_state

    now = datetime.now(IST)

    # Clear stale state
    if (_sidharth_state["timestamp"] and
        (now - _sidharth_state["timestamp"]).total_seconds() > _STATE_TIMEOUT_SECS):
        _sidharth_state = {k: None for k in _sidharth_state}

    if _SS_NOISE.search(text):
        return None

    # Primary: full entry signal
    m = _SS_ENTRY.search(text)
    if m:
        raw_inst = m.group(1) or "NIFTY"
        strike   = m.group(2)
        opt      = m.group(3)
        raw_entry= m.group(4)
        tgt_raw  = m.group(5)

        # Parse entry range
        range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_entry.strip())
        if range_m:
            entry_low  = float(range_m.group(1))
            entry_high = float(range_m.group(2))
            entry = (entry_low + entry_high) / 2
        else:
            entry = float(raw_entry.split()[0])
            entry_low = entry_high = entry

        tgt = float(tgt_raw) if tgt_raw else None

        # Save state for drip follow-ups
        _sidharth_state.update({
            "instrument": raw_inst.upper() if raw_inst else "NIFTY",
            "strike":     strike,
            "opt":        opt,
            "entry_low":  entry_low,
            "entry_high": entry_high,
            "timestamp":  now,
        })

        if not tgt:
            # Partial — wait for target in next message
            log.debug(f"[SIDHARTH] Partial signal buffered: {raw_inst} {strike} {opt} Entry={entry}")
            return None

        name, itype = _normalise_instrument(raw_inst or "NIFTY")
        month = _parse_month(text)
        sl = _parse_sl(text)
        return _build_result(
            name, itype, strike, opt, month,
            entry, sl, tgt, "SIDHARTH_PARSER"
        )

    # Check for standalone target following a buffered instrument
    if _sidharth_state["strike"]:
        tgt_m = re.search(r'target\s*[-:\s]+(\d+(?:\.\d+)?)', text, re.IGNORECASE)
        if tgt_m:
            s = _sidharth_state
            tgt = float(tgt_m.group(1))
            entry = (s["entry_low"] + s["entry_high"]) / 2 if s["entry_low"] else None
            name, itype = _normalise_instrument(s["instrument"] or "NIFTY")
            month = _parse_month("")
            sl = _parse_sl(text)
            result = _build_result(
                name, itype, s["strike"], s["opt"], month,
                entry, sl, tgt, "SIDHARTH_PARSER"
            )
            # Clear state after use
            for k in list(_sidharth_state.keys()):
                _sidharth_state[k] = None
            return result

    # Two-word instrument: "22900 call buy" — buffer for target
    m2 = _SS_INSTRUMENT_ONLY.search(text)
    if m2 and re.search(r'\bbuy\b', text, re.IGNORECASE):
        strike = m2.group(1)
        opt    = m2.group(2)
        _sidharth_state.update({
            "instrument": "NIFTY",
            "strike":     strike,
            "opt":        opt,
            "entry_low":  None,
            "entry_high": None,
            "timestamp":  now,
        })
        log.debug(f"[SIDHARTH] Instrument buffered: NIFTY {strike} {opt}")
        return None

    return None


# ═══════════════════════════════════════════════════════════════════════
# PARSER 4 — JP (JP Paper trade channel, -1003282204738)
#
# Ultra-compact format — NO keywords, just:
#   [SYMBOL] STRIKE CE/PE PRICE [| SL PRICE]
#
# Examples:
#   "Dixon 11600 CE 625"           — stock option
#   "56600 PE 600 | SL 570"        — BankNifty (inferred from strike range)
#   "23400 PE 180-190"             — Nifty (inferred from strike 23000-26500)
#   "Sensex 77200 PE 470"          — Sensex (labeled)
#   "ltm 4200 CE 135-140"          — LTM stock, entry range
#
# Index strike inference: SENSEX ≥ 65000, BANKNIFTY ≥ 45000, NIFTY ≥ 18000
# ═══════════════════════════════════════════════════════════════════════

_JP_NOISE = re.compile(
    r'(target done|book profit|sl hit|stop loss hit|all target|exit\s*now|'
    r'\bhigh\b|enter only|dont hold|hero zero|june series|may series|'
    r'pass\s|revised level|new level|profit booking|still trading)',
    re.IGNORECASE
)

# Signal: [SYMBOL] STRIKE CE/PE PRICE
# Symbol starts with a letter; price may be a range (135-140 or 180-190)
_JP_SIG = re.compile(
    r'(?:^|\b)(?:([a-zA-Z][a-zA-Z0-9&.]{0,19}(?:\s+[a-zA-Z][a-zA-Z0-9&.]{0,9})?)\s+)?'
    r'(\d{2,6}(?:\.\d+)?)\s+(ce|pe|call|put)\s+'
    r'(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)',
    re.IGNORECASE,
)

_JP_SL_RE = re.compile(r'\bsl\s*[:\-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)

_JP_INDEX_NAMES = {
    "NIFTY", "BANKNIFTY", "SENSEX", "SENSED", "FINNIFTY", "BANKEX", "MIDCPNIFTY"
}

def _jp_infer_index(strike_int):
    """Map strike value to index name based on typical ranges."""
    if strike_int >= 65000:  return "SENSEX",    "INDEX_OPT"
    if strike_int >= 45000:  return "BANKNIFTY",  "INDEX_OPT"
    if strike_int >= 18000:  return "NIFTY",      "INDEX_OPT"
    return None, None


def parse_jp(text):
    stripped = text.strip()
    # Reject pure-number lines (running price commentary)
    if re.match(r'^\d+(?:\.\d+)?\s*$', stripped):
        return None
    # Reject "N high" lines
    if re.match(r'^\d+(?:\.\d+)?\s+high', stripped, re.IGNORECASE):
        return None
    if _is_noise(text) or _JP_NOISE.search(text):
        return None

    m = _JP_SIG.search(text)
    if not m:
        return None

    raw_sym, strike_str, opt_str, raw_price = m.groups()
    try:
        strike_val = int(float(strike_str))
    except ValueError:
        return None

    opt = opt_str.upper().replace("CALL", "CE").replace("PUT", "PE")

    # Determine instrument name and type
    if raw_sym:
        raw_sym = raw_sym.strip()
        upper_sym = raw_sym.upper().replace(" ", "")
        if upper_sym in _JP_INDEX_NAMES:
            # Provided index label — override with strike-range inference for correctness
            inferred, itype = _jp_infer_index(strike_val)
            name  = inferred if inferred else upper_sym
            itype = itype if itype else "INDEX_OPT"
        else:
            name, itype = _normalise_instrument(raw_sym)
    else:
        # No symbol — infer index from strike range
        name, itype = _jp_infer_index(strike_val)
        if name is None:
            return None  # Can't determine instrument without symbol

    # Parse entry (handle range like "135-140")
    range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_price.strip())
    if range_m:
        entry = (float(range_m.group(1)) + float(range_m.group(2))) / 2
    else:
        entry = float(raw_price.split()[0])

    sl_m = _JP_SL_RE.search(text)
    sl   = float(sl_m.group(1)) if sl_m else None

    month = _parse_month(text)
    return _build_result(name, itype, str(strike_val), opt, month,
                         entry, sl, None, "JP_PARSER")


# ═══════════════════════════════════════════════════════════════════════
# PARSER 4b — PremiumNiftyBNF  (-1003115553842)
#
# Pipe-separated format (pipes appear as literal | or as newlines in TG):
#   "NIFTY 23550 PE | Buy above 188 | TGT 195/210/230 | SL 170"
#   "SENSEX 74300 PE | BUY ABOVE 36 | TGT 48/70/100 | SL 10"
#   "ADANIENT 3000 CE | BUY 140 | TGT 148/165/180 | SL.120"
#   "CRUDEOIL 8600 PE | BUY ABOVE 225 | TGT 233/246/265 | SL 209"
#
# Noise: standalone numbers (live price feed), trade summaries, Hindi text,
#        "Wait for trigger", "SL zero" (hero-or-zero — skip).
# ═══════════════════════════════════════════════════════════════════════

_PNB_NOISE = re.compile(
    r'(wait\s+for|trade\s+no\s+\d|profit\s+(book|done)|all\s+target|sl\s+hit|'
    r'stop\s+loss\s+hit|book\s+profit|screenshot|@Niftyhelpdesk|vyomresearch|'
    r'watchlist|add\s+in\s+watch)',
    re.IGNORECASE,
)

# Two-part regex: first the instrument block, then prices via delimiters
_PNB_INSTRUMENT = re.compile(
    r'^(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|BANKEX|CRUDEOIL|CRUDE\s*OIL|'
    r'GOLD(?:\s+MINI)?|SILVER(?:\s+MINI)?|NATURALGAS|[A-Z][A-Z0-9&]{1,14})\s+'
    r'(\d{3,6}(?:\.\d+)?)\s+(CE|PE)',
    re.IGNORECASE | re.MULTILINE,
)

_PNB_BUY    = re.compile(r'buy\s*(?:above|near|@)?\s*[:\s]*(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)', re.IGNORECASE)
_PNB_TGT    = re.compile(r'(?:tgt|target)\s*[:\s]*(\d+(?:\.\d+)?)', re.IGNORECASE)
_PNB_SL     = re.compile(r'\bsl\s*[.:\-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)

_PNB_MCX_SYMS = {
    "CRUDEOIL": "CRUDEOIL", "CRUDE OIL": "CRUDEOIL", "GOLD": "GOLD",
    "GOLD MINI": "GOLDM",   "SILVER": "SILVER",      "SILVER MINI": "SILVERM",
    "NATURALGAS": "NATURALGAS",
}


def parse_premium_nifty_bnf(text: str):
    """Dedicated parser for Premium Nifty Banknifty group no 3 (-1003115553842)."""
    stripped = text.strip()
    # Reject pure-number commentary
    if re.match(r'^\d+(?:\.\d+)?\s*$', stripped):
        return None
    if _PNB_NOISE.search(text):
        return None
    # Hero-or-zero: SL is zero or the word "zero" — skip
    if re.search(r'\bsl\s*[.:\-]?\s*(?:0\b|zero\b)', text, re.IGNORECASE) and not re.search(r'\bsl\s*[.:\-]?\s*[1-9]', text, re.IGNORECASE):
        return None

    m = _PNB_INSTRUMENT.search(text)
    if not m:
        return None

    raw_inst  = m.group(1).strip()
    strike_str = m.group(2)
    opt       = m.group(3).upper()

    # Normalise MCX symbols
    key = re.sub(r'\s+', ' ', raw_inst.upper())
    if key in _PNB_MCX_SYMS:
        name  = _PNB_MCX_SYMS[key]
        itype = "MCX_OPT"
    else:
        name, itype = _normalise_instrument(raw_inst)
        # Override index name with strike-range inference (channel sometimes mislabels)
        if itype == "INDEX_OPT":
            inferred, _ = _jp_infer_index(int(float(strike_str)))
            if inferred:
                name = inferred

    # Entry price
    bm = _PNB_BUY.search(text)
    if not bm:
        return None
    raw_entry = bm.group(1)
    rng = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_entry.strip())
    entry = (float(rng.group(1)) + float(rng.group(2))) / 2 if rng else float(raw_entry.split()[0])

    # Target and SL
    tm  = _PNB_TGT.search(text)
    tgt = float(tm.group(1)) if tm else None
    sm  = _PNB_SL.search(text)
    sl  = float(sm.group(1)) if sm else None
    if sl is not None and sl == 0:
        sl = None

    month = _parse_month(text)
    return _build_result(name, itype, strike_str, opt, month, entry, sl, tgt, "PREMIUM_NB_PARSER")


# ═══════════════════════════════════════════════════════════════════════
# PARSER 4c — Premium Group Stock Option  (-1002670475451)
#
# Format A (most common):
#   ✅. Nifty 9 Jun 23350 PE | Only above 88 | SL 70 | TARGET --90-100-110-120-130
#   ✅. sensex 73700 Pe | ONLY ABOVE 80 | SL 60 | TARGE -90-100-120-130-160
#
# Format B (no ✅ prefix):
#   SENSEX 74900 CE | ABOVE 96 | SL 80 | TARGET 115 -130 -150++
#
# "SL premium" → SL is the premium paid (full option risk); parsed as None → default 85%
# "9 Jun" optional expiry-date hint between symbol and strike
#
# Noise: price-update lines (80+++, First target done), morning commentary,
#        level-call lines (Breakdown point, Up side break out level), FII/DII data.
# ═══════════════════════════════════════════════════════════════════════

_PGSO_NOISE = re.compile(
    r'(first\s+target|second\s+target|third\s+target|my\s+all\s+target|all\s+target|'
    r'keep\s+book|good\s+morning|gift\s+nifty|market\s+is\s+open|jeckp|jackpot|'
    r'nifty\s+update|be\s+ready|support\s+le|nifty\s+behaviour|overtrading|'
    r'breakdown\s+point|breakout\s+point|break\s+out\s+level|trend\s+line\s+break|'
    r'swing\s+trade|stocks\s+for\s+next|fiis?\s+net|diis?\s+net|'
    r'bullish\s+signal|bearish\s+signal|\bsl\s+hit\b|profit\s+book|'
    r'members\s+be\s+happy|all\s+members|intraday.*(?:down|up)\s*side)',
    re.IGNORECASE,
)

_PGSO_INSTRUMENT = re.compile(
    r'(?:\*{0,2}✅\.?\*{0,2}\s*)?'
    r'(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|BANKEX)\s+'
    r'(?:\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+)?'
    r'(\d{4,6}(?:\.\d+)?)\s+'
    r'(CE|PE)',
    re.IGNORECASE,
)

_PGSO_ENTRY = re.compile(r'(?:only\s+)?above\s*[:\-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
_PGSO_SL    = re.compile(r'\bsl\s*[.:\-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)
_PGSO_TGT   = re.compile(r'(?:target|targe)\s*[-]{0,3}\s*(\d+(?:\.\d+)?)', re.IGNORECASE)


def parse_pgso(text: str):
    """Dedicated parser for Premium Group Stock Option (-1002670475451)."""
    if _PGSO_NOISE.search(text):
        return None

    m = _PGSO_INSTRUMENT.search(text)
    if not m:
        return None

    raw_sym    = m.group(1).strip()
    strike_str = m.group(2)
    opt        = m.group(3).upper()

    # Entry is required — absence means it's a price-update or commentary line
    em = _PGSO_ENTRY.search(text)
    if not em:
        return None
    entry = float(em.group(1))

    # "SL premium" → no digit match → sl=None → _build_result defaults to 0.85×entry
    sm = _PGSO_SL.search(text)
    sl = float(sm.group(1)) if sm else None
    if sl is not None and sl == 0:
        sl = None

    tm  = _PGSO_TGT.search(text)
    tgt = float(tm.group(1)) if tm else None

    name, itype = _normalise_instrument(raw_sym)
    month = _parse_month(text)
    return _build_result(name, itype, strike_str, opt, month, entry, sl, tgt, "PGSO_PARSER")


# ═══════════════════════════════════════════════════════════════════════
# PARSER 5 — Generic (universal fallback for unknown channels)
#
# Handles two instrument classes:
#
# ── INDEX OPTIONS (NSE/BSE) ──────────────────────────────────────────
#  A) Compact single-line:
#       NIFTY 22700 CE ABOVE 150 SL 120 TGT 200
#       BUY BANKNIFTY 52000 PE @ 300 SL 265 TARGET 380
#       *SENSEX 75800 PE* ABOVE 210 SL 178 TARGET 250/300++
#  B) Embedded option type:
#       NIFTY 22700CE ABOVE 180 SL 155 TARGET 230++
#  C) Pipe-separated:
#       NIFTY 22700 CE | Entry: 150 | SL: 120 | Target: 200
#  D) Multiline:
#       BUY NIFTY 22700 CE / NEAR LEVEL - 150 / TARGET - 200 / STOPLOSS - 120
#  E) Emoji/bold multiline:
#       📈 NIFTY 22700CE / ⬆️ Entry: 150 / 🛑 SL: 120 / 🎯 Target: 200
#  F) Entry range:
#       NIFTY 22700 PE BUY ABOVE 145/150 SL 120 TGT 190
#
# ── MCX COMMODITY OPTIONS ────────────────────────────────────────────
#  G) MCX multiline (MCX PREMIUM / COMMODITY OPTIONS PRIME style):
#       COMMODITY MCX TRADE
#       BUY GOLD 130000 CE
#       NEAR LEVEL - 550
#       TARGET - 600/700/800
#       STOPLOSS - 530
#  H) MCX compact with decimal strike:
#       BUY CRUDEOIL 5150. CE / NEAR LEVEL - 225 / TARGET - 245/280 / SL - 215
#  I) MCX natural gas format:
#       NATURAL GAS 395 PE / ABOVE 13.60 / TARGET 18,25 / SL 11
#
# Noise gate: requires at least 2 of {entry, SL, target} to fire.
# ═══════════════════════════════════════════════════════════════════════

import unicodedata as _ud

# Strip emoji and control chars so regex doesn't choke on them
def _strip_emoji(text: str) -> str:
    cleaned = []
    for ch in text:
        cat = _ud.category(ch)
        if cat.startswith("C") or cat == "So":
            cleaned.append(" ")
        else:
            cleaned.append(ch)
    return "".join(cleaned)

# ── Index instruments ──────────────────────────────────────────────────────────
# Handles embedded option type (NIFTY 22700CE) and separated (NIFTY 22700 CE)
_GEN_INSTRUMENT = re.compile(
    r'(NIFTY|BANKNIFTY|BANK\s*NIFTY|SENSEX|FINNIFTY|MIDCPNIFTY|BANKEX|SENSEX50)'
    r'\s*(\d{4,6})\s*(CE|PE|CALL|PUT)',
    re.IGNORECASE,
)

# ── MCX commodity instruments ──────────────────────────────────────────────────
# Symbols: GOLD, SILVER, CRUDEOIL, NATURALGAS + variants; COPPER, ZINC, NICKEL, LEAD
# Strike: 2-6 digits, optional trailing decimal (e.g. 5150.)
_MCX_INSTRUMENT = re.compile(
    r'(GOLD(?:\s+MINI|\s+GUINEA|\s+PETAL)?'
    r'|SILVER(?:\s+MINI|\s+MICRO)?'
    r'|CRUDE\s*OIL(?:\s*MINI)?|CRUDEOIL(?:MINI|M)?'
    r'|NATURAL\s*GAS(?:\s+MINI)?|NATGAS(?:\s+MINI)?|NAT\s*GAS|NATURALGAS'
    r'|COPPER|ZINC|NICKEL|LEAD|ALUMIN(?:I)?UM|TIN)'
    r'\s+(\d{2,6})(?:\.\d*)?\s*'   # strike — allow trailing decimal (5150.)
    r'(CE|PE|CALL|PUT)',
    re.IGNORECASE,
)

# Canonical MCX symbol names (match Kite instrument names)
_MCX_CANONICAL = {
    "CRUDE OIL":      "CRUDEOIL",
    "CRUDE OIL MINI": "CRUDEOILM",
    "CRUDEOILMINI":   "CRUDEOILM",
    "NATURAL GAS":    "NATURALGAS",
    "NATURAL GAS MINI": "NATGASMINI",
    "NATGAS MINI":    "NATGASMINI",
    "NAT GAS":        "NATURALGAS",
    "GOLD MINI":      "GOLDM",
    "GOLD GUINEA":    "GOLDGUINEA",
    "GOLD PETAL":     "GOLDPETAL",
    "SILVER MINI":    "SILVERM",
    "SILVER MICRO":   "SILVERMICRO",
    "ALUMINUM":       "ALUMINIUM",
}

# ── Shared price-extraction patterns ──────────────────────────────────────────
_GEN_PRICE_KW = re.compile(
    r'(?:above|around|cmp|near|entry|buy\s+above|buy\s+around|@|'
    r'near\s+level|sl|stop\s*loss|stoploss|s/l|'
    r'target|tgt|tp|tg)\s*[:\-/]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

_GEN_ENTRY = re.compile(
    r'(?:above|around|cmp|cmpp|entry|near\s*level|near|buy\s+above|'
    r'buy\s+around|buy\s+near|@)\s*[:\-]?\s*'
    r'(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)',
    re.IGNORECASE,
)

_GEN_SL = re.compile(
    r'(?:stop\s*loss|stoploss|s\.?l\.?|s/l)\s*[:\-]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

_GEN_TGT = re.compile(
    r'(?:target|tgt|tp|tg)\s*[:\-]?\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

# Noise: update/exit/promo messages
_GEN_NOISE = re.compile(
    r'(all target done|target done|target hit|book profit|book full|sl hit|'
    r'stop loss hit|exit now|exit all|trail|fees\s*=|join fast|account management|'
    r'loss cover plan|capital dubal|premium join|limited seats|webinar|'
    r'masterclass|course|batch|contact|whatsapp|register|enroll|'
    r'youtube|live stream|watch now|given at|now at|high made|'
    r'still above sl|be ready|wait for|add.*watchlist|mark my word)',
    re.IGNORECASE,
)


def _extract_prices(clean: str):
    """Shared price extraction — returns (entry, sl, tgt)."""
    entry = None
    em = _GEN_ENTRY.search(clean)
    if em:
        raw_e = em.group(1)
        range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_e.strip())
        if range_m:
            entry = (float(range_m.group(1)) + float(range_m.group(2))) / 2
        else:
            entry = float(raw_e.split()[0])

    sl = None
    sm = _GEN_SL.search(clean)
    if sm:
        v = float(sm.group(1))
        sl = v if v > 0 else None  # SL 0 = hero-or-zero, skip

    tgt = None
    tm = _GEN_TGT.search(clean)
    if tm:
        tgt = float(tm.group(1))

    return entry, sl, tgt


def parse_generic(text: str):
    """Universal signal parser — registered for unknown/new channels.
    Handles both index options (NIFTY/BANKNIFTY/SENSEX/etc.) and
    MCX commodity options (GOLD/SILVER/CRUDEOIL/NATURALGAS/etc.).
    """
    if _GEN_NOISE.search(text):
        return None

    clean = _strip_emoji(text)

    # ── Try index options ──────────────────────────────────────────────
    m = _GEN_INSTRUMENT.search(clean)
    if m:
        raw_inst = m.group(1).strip()
        strike   = m.group(2)
        raw_opt  = m.group(3)
        name, itype = _normalise_instrument(raw_inst)
        opt   = raw_opt.upper().replace("CALL", "CE").replace("PUT", "PE")
        month = _parse_month(clean)
        source = "GENERIC_PARSER"

    else:
        # ── Try MCX commodity options ──────────────────────────────────
        m = _MCX_INSTRUMENT.search(clean)
        if not m:
            return None
        raw_inst = m.group(1).strip()
        strike   = m.group(2)
        raw_opt  = m.group(3)
        name  = _MCX_CANONICAL.get(raw_inst.upper(),
                    raw_inst.upper().replace(" ", ""))
        itype = "MCX_OPT"
        opt   = raw_opt.upper().replace("CALL", "CE").replace("PUT", "PE")
        month = _parse_month(clean)
        source = "GENERIC_MCX_PARSER"

    # Must have at least one price keyword (prevents discussion-only matches)
    if not _GEN_PRICE_KW.search(clean):
        return None

    entry, sl, tgt = _extract_prices(clean)

    # Require at least 2 price points to avoid single-word false positives
    if sum(x is not None for x in [entry, sl, tgt]) < 2:
        return None

    log.info(f"[{source}] {name} {strike} {opt} Entry={entry} SL={sl} Tgt={tgt}")
    return _build_result(name, itype, strike, opt, month, entry, sl, tgt, source)


# ═══════════════════════════════════════════════════════════════════════
# PARSER 6 — FuturesSegmentBatch  (-1001404315099)
#
# Handles both stock options AND stock futures from the same channel:
#
#  Options:  "SHORT TERM  BUY TRENT JUNE 4100 CE  AROUND 185-188  TARGET 230,280,340+  STOP LOSS 120"
#  Futures:  "SHORT TERM  BUY TECHM JUNE FUTURES  AROUND 1535  TARGET 1560,1590+  STOP LOSS 1500"
#  Futures:  "BUY ADANIPORTS JUNE FUTURES  AROUND 1815  TARGET 1840,1880+  STOP LOSS 1770"
#  Updates:  "1545  TECHM JUNE FUTURES!  SAFE TRADER'S BOOK PROFITS HERE"  → noise
# ═══════════════════════════════════════════════════════════════════════

_FSB_NOISE = re.compile(
    r'(safe\s*trader|book\s*profit|almost\s*at\s*target|all\s*target|'
    r'target\s*\d+\s*done|exit\s*now|exit\s*all|sl\s*hit|stop\s*loss\s*hit|'
    r'join\s*fast|limited|premium|fees|cosmofeed|contact|whatsapp|offer|coupon)',
    re.IGNORECASE
)

_FSB_UPDATE = re.compile(r'^\s*\d+\s+[A-Z]', re.IGNORECASE)

# Futures signal: BUY [STOCK] [MONTH] FUTURES  AROUND [price]  TARGET [t]+  STOP LOSS [sl]
_FSB_FUTURES = re.compile(
    r'(?:short\s*term\s+)?buy\s+'
    r'([A-Z&]{2,15})\s+'
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+'
    r'futures?\s*'
    r'(?:around|above|near)?\s*[:\-]?\s*'
    r'(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)'   # entry
    r'.*?target\s*[:\-]?\s*(\d+(?:\.\d+)?)'            # first target
    r'.*?stop\s*loss\s*(?:at\s*)?[:\-]?\s*(\d+(?:\.\d+)?)',  # stop loss
    re.IGNORECASE | re.DOTALL
)


def parse_futures_segment_batch(text: str):
    """Dedicated parser for FUTURES SEGMENT BATCH (-1001404315099).
    Handles stock options (delegates to parse_stock_options_prime) and
    stock futures (new regex).
    """
    if _FSB_NOISE.search(text):
        return None
    if _FSB_UPDATE.match(text):
        return None

    # Try stock options path first
    if re.search(r'\b(ce|pe)\b', text, re.IGNORECASE) and not re.search(r'\bfutures?\b', text, re.IGNORECASE):
        result = parse_stock_options_prime(text)
        if result:
            result['source'] = 'FUTURES_SEGMENT_OPTIONS_PARSER'
            return result

    # Try futures path
    m = _FSB_FUTURES.search(text)
    if not m:
        return None

    raw_symbol, raw_month, raw_entry, raw_tgt, raw_sl = m.groups()
    symbol = raw_symbol.upper()

    range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_entry.strip())
    entry = (float(range_m.group(1)) + float(range_m.group(2))) / 2 if range_m else float(raw_entry.split()[0])
    sl     = float(raw_sl)
    tgt    = float(raw_tgt)

    month_num  = MONTH_MAP.get(raw_month.lower()[:3], datetime.now(IST).month)
    now        = datetime.now(IST)
    year       = now.year if month_num >= now.month else now.year + 1
    month_abbr = raw_month[:3].upper()
    # Futures expiry: last Thursday of month
    expiry_dt  = _last_thursday(year, month_num)

    result = {
        "action":          "BUY",
        "symbol":          symbol,
        "instrument_type": "FUTURES",
        "expiry_date":     expiry_dt.strftime('%Y-%m-%d'),
        "tradingsymbol":   f"{symbol}{str(year)[2:]}{month_abbr}FUT",
        "quantity":        1,
        "exchange":        "NFO",
        "entry_price":     entry,
        "stop_loss":       sl,
        "target":          tgt,
        "expiry_str":      month_abbr,
        "source":          "FUTURES_SEGMENT_PARSER",
        "confidence":      "HIGH",
    }
    log.info(f"[FSB_PARSER] {symbol} {month_abbr} FUT | Entry={entry} SL={sl} Tgt={tgt} Expiry={expiry_dt}")
    return result


# ═══════════════════════════════════════════════════════════════════════
# PARSER 6 — CommodityPrime  (-1001967914715)
#
# Long-term positional calls (6mo–1yr holding). Channel sends equity delivery
# and positional commodity signals — NOT intraday. All positions are flagged
# position_type='LONGTERM' so the SL monitor skips them at EOD cutoff and
# checks SL only every 4 hours.
#
# The channel mixes index options (same format as ShortTerm) and MCX options.
# Delegate to existing sub-parsers; inject position_type marker.
# ═══════════════════════════════════════════════════════════════════════

def parse_commodity_prime(text: str):
    """
    Dedicated parser for COMMODITY OPTIONS PRIME (-1001967914715).
    Delegates format parsing to generic/shortterm parsers, then stamps
    position_type='LONGTERM' so the SL monitor holds without EOD exit.
    """
    # Try ShortTerm format first (index options with AROUND/TARGET/STOP LOSS)
    result = parse_shortterm(text)
    if result is None:
        result = parse_generic(text)
    if result is None:
        return None
    result['position_type'] = 'LONGTERM'
    result['source']        = 'COMMODITY_PRIME_PARSER'
    return result


# ═══════════════════════════════════════════════════════════════════════
# PARSER 6 — InvestingKorner  (-1003770951544)
#
# All signals share the same price block: CMP/SL/Tgt with ; or : separator.
# Two instrument layouts (date ordinal can appear in either spot):
#
#  Layout A — strike BEFORE month:
#    "Nifty 23350 9th June CE | CMP : 203 | SL : 174 | Tgt : 224, 242, 256++"
#    "IRFC 100 jun ce | CMP ; 1.79 | SL ; 0.74 | Tgt ; 2.24, 2.54, 3.23 ++"
#    "#TCS 2320 June CE | CMP ; 75 | SL ; 39 | Tgt ; 98, 114, 138++"
#
#  Layout B — date BEFORE strike (month then strike):
#    "Sensex 14th may 75000 CE | CMP: 220 | SL : 138 | Tgt : 284, 325, 390++"
#
# Noise: update messages containing "given at", "high made", "book profit"
# ═══════════════════════════════════════════════════════════════════════

_IK_NOISE = re.compile(
    r'(given\s+at|high\s+made|book\s+(partial|full)|trail\s+sl|sl\s+hit|'
    r'target\s+done|target\s+hit|all\s+target|average\s+now|hold\s+as\s+it|'
    r'dont\s+panic|stop\s+loss\s+hit|exit\s+now|exit\s+all)',
    re.IGNORECASE,
)

_IK_MONTH_RE = (
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
)
_IK_DATE_ORD = r'(?:\d{1,2}\s*(?:st|nd|rd|th)?\s+)?'   # optional "9th ", "14th "

# Layout A: SYMBOL STRIKE [date] MONTH CE/PE
_IK_A = re.compile(
    r'(?:#)?([a-z][a-z0-9&.]{1,18})\s+'   # symbol (with optional # prefix)
    r'(\d{2,6}(?:\.\d+)?)\s+'             # strike
    + _IK_DATE_ORD +
    _IK_MONTH_RE + r'\s+'
    r'(ce|pe)',
    re.IGNORECASE,
)

# Layout B: SYMBOL [date] MONTH STRIKE CE/PE  (Sensex 14th may 75000 CE)
_IK_B = re.compile(
    r'(?:#)?([a-z][a-z0-9&.]{1,18})\s+'   # symbol
    + _IK_DATE_ORD +
    _IK_MONTH_RE + r'\s+'
    r'(\d{2,6}(?:\.\d+)?)\s+'             # strike
    r'(ce|pe)',
    re.IGNORECASE,
)

# Price block: CMP / SL / Tgt with ; or : separator
_IK_CMP = re.compile(r'(?:cmp|only\s+above|above)\s*[;:\s]+(\d+(?:\.\d+)?)', re.IGNORECASE)
_IK_SL  = re.compile(r'\b(?:sl|s\.l\.?)\s*[;:\-\s]+(\d+(?:\.\d+)?)', re.IGNORECASE)
_IK_TGT = re.compile(r'(?:tgt|tgr|target)\s*[;:\-\s]+(\d+(?:\.\d+)?)', re.IGNORECASE)


def parse_investing_korner(text: str):
    """
    Dedicated parser for Investing Korner (-1003770951544).
    Handles index options, stock options, and Sensex/Nifty/Banknifty calls.
    """
    if _IK_NOISE.search(text):
        return None

    # Try Layout A (strike before month)
    m = _IK_A.search(text)
    layout = 'A'
    if not m:
        m = _IK_B.search(text)
        layout = 'B'
    if not m:
        return None

    if layout == 'A':
        raw_sym, raw_strike, raw_month, opt = m.groups()
    else:                                   # Layout B: sym, month, strike, opt
        raw_sym, raw_month, raw_strike, opt = m.groups()

    raw_sym = raw_sym.strip().lstrip('#')
    opt     = opt.upper()

    # Validate: strike must be a plausible number
    try:
        strike = int(float(raw_strike))
    except ValueError:
        return None
    if strike <= 0:
        return None

    # Normalise symbol
    name, itype = _normalise_instrument(raw_sym)
    month = _parse_month(raw_month)

    # CMP = entry price
    mc = _IK_CMP.search(text)
    if not mc:
        return None
    entry = float(mc.group(1))

    # SL
    ms = _IK_SL.search(text)
    sl = float(ms.group(1)) if ms else None

    # Skip hero-or-zero (SL = 0) — no SL tracking possible
    if sl is not None and sl == 0:
        return None

    if sl is None:
        sl = round(entry * 0.85, 2)   # 15% fallback

    # Target
    mt = _IK_TGT.search(text)
    tgt = float(mt.group(1)) if mt else None

    return _build_result(name, itype, str(strike), opt, month, entry, sl, tgt, "INVESTING_KORNER_PARSER")


# ═══════════════════════════════════════════════════════════════════════
# PARSER 6 — MCXPremium  (MCX PREMIUM channel, -1002770917134)
#
# Three sub-formats sent by this channel:
#
#  a) Stock options  — "#TRENT MAY 4100 CE\n ABOVE 130\n TGT 150,180\n Sl 120"
#  b) Index options  — "BUY - NIFTY 23600 CE\n NEAR LEVEL -- 215\n TARGET 250/300\n STOPLOSS -- 190"
#  c) MCX commodity  — "COMMODITY_MCX_TRAD\n BUY CRUDEOIL 9200 CE\n NEAR LEVEL - 265\n TARGET 300/340\n STOPLOSS - 250"
# ═══════════════════════════════════════════════════════════════════════

# MCX commodity symbols recognised by this channel
_MCX_COMMODITY_SYMS = re.compile(
    r'\b(CRUDEOIL(?:M)?|CRUDE\s*OIL|NATURAL\s*GAS|NATURALGAS(?:M)?|NATGAS|'
    r'GOLD(?:M|PETAL|GUINEA)?|SILVER(?:M|MICRO)?|COPPER(?:M)?|ZINC(?:MINI)?|'
    r'LEAD(?:MINI)?|NICKEL|ALUMIN(?:I)?UM)\b',
    re.IGNORECASE,
)

# Update/noise patterns for this channel
_MCX_NOISE = re.compile(
    r'(book\s*profit|target\s*done|target\s*hit|all\s*target|sl\s*hit|'
    r'stop\s*loss\s*hit|exit\s*now|safe\s*trader|account\s*management|'
    r'join\s*fast|limited\s*seat|premium\s*join)',
    re.IGNORECASE,
)

# ── Sub-format a: Stock options — #STOCKNAME MONTH STRIKE CE/PE ──────────────
# Handles: "#TRENT MAY 4100 CE\nABOVE 130\nTGT 150,180\nSl 120"
# Also:    "#ADANIGREEN JUN 1460 CE\nABOVE 112\nTGT 130,160\nSl 100"
_MCX_STOCK_SIG = re.compile(
    r'#([A-Z&]{2,20})\s+'
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+'
    r'(\d{3,6})\s+(ce|pe)',
    re.IGNORECASE,
)

# ── Sub-format b: Index options — BUY - INDEX STRIKECE NEAR LEVEL ────────────
# Handles: "BUY - NIFTY 23600 CE\nNEAR LEVEL -- 215\nTARGET 250/300\nSTOPLOSS -- 190"
# Also:    "BUY - BANKNIFTY 53800CE\nNEAR LEVEL -- 415\n..."
_MCX_INDEX_SIG = re.compile(
    r'buy\s*[-–]\s*(nifty|banknifty|bank\s*nifty|sensex|finnifty|midcpnifty|bankex)\s+'
    r'(\d{4,6})\s*(ce|pe|[A-Za-z]{2,4}(?:ce|pe))',
    re.IGNORECASE,
)

# ── Sub-format c: MCX commodity — COMMODITY_MCX_TRAD / BUY SYMBOL STRIKE CE/PE
# Handles: "COMMODITY_MCX_TRAD\nBUY CRUDEOIL 9200 CE\nNEAR LEVEL - 265\n..."
# Also:    "BTST COMMODITY_MCX_TRAD\nBUY NATURAL GAS 295 PE\nNEAR LEVEL - 13\n..."
_MCX_COMMODITY_SIG = re.compile(
    r'buy\s+'
    r'(crudeoil(?:m)?|crude\s*oil|natural\s*gas(?:\s*mini)?|naturalgas(?:m)?|'
    r'gold(?:m|petal|guinea)?|silver(?:m|micro)?|copper(?:m)?|zinc(?:mini)?|'
    r'lead(?:mini)?|nickel|alumin(?:i)?um)\s+'
    r'(\d{2,7}(?:\.\d+)?)\s*(ce|pe)',
    re.IGNORECASE,
)

_MCX_CANONICAL = {
    "CRUDE OIL":   "CRUDEOIL",  "NATURAL GAS": "NATURALGAS",
    "GOLD MINI":   "GOLDM",     "SILVER MINI":  "SILVERM",
    "SILVER MICRO":"SILVERMIC", "GOLD PETAL":   "GOLDPETAL",
    "GOLD GUINEA": "GOLDGUINEA","COPPER MINI":  "COPPERM",
    "ZINC MINI":   "ZINCMINI",  "LEAD MINI":    "LEADMINI",
}

_MCX_NEAR_LEVEL = re.compile(
    r'near\s*level\s*[-–:]+\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

_MCX_TARGET_MULTI = re.compile(
    r'target\s*[-–:]\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)

_MCX_SL = re.compile(
    r'stoploss\s*[-–:]+\s*(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)


def parse_mcx_premium(text: str):
    """
    Dedicated parser for MCX PREMIUM channel (-1002770917134).
    Routes to the correct sub-parser based on message format.
    """
    if _MCX_NOISE.search(text):
        return None

    # ── Route a: stock option  (#STOCK MONTH STRIKE CE/PE) ───────────────────
    m = _MCX_STOCK_SIG.search(text)
    if m:
        stock_sym = m.group(1).upper()
        raw_month = m.group(2)
        strike    = int(m.group(3))
        opt       = m.group(4).upper()

        entry = _parse_entry(text)
        sl    = _parse_sl(text)
        tgt   = _parse_targets(text)

        if entry is None:
            return None

        if sl is None and entry:
            sl = round(entry * 0.85, 2)

        month_num  = MONTH_MAP.get(raw_month.lower()[:3], datetime.now(IST).month)
        now        = datetime.now(IST)
        year       = now.year if month_num >= now.month else now.year + 1
        expiry_dt  = _last_thursday(year, month_num)
        month_abbr = raw_month[:3].upper()

        # Resolve tradingsymbol + Dhan security_id via FastInstrumentFinder
        tradingsymbol = None
        quantity      = None
        exchange      = 'BSE_FNO'
        finder = _get_sop_finder()   # reuse the lazily-loaded finder from SOP parser
        if finder:
            try:
                inst = finder.find_instrument(stock_sym, strike, opt)
                if inst:
                    tradingsymbol = inst.get('tradingsymbol')
                    raw_qty       = inst.get('lot_size')
                    quantity      = int(raw_qty) if raw_qty is not None else None
                    exchange      = inst.get('exchange', 'BSE_FNO')
            except Exception as e:
                log.warning(f"[MCX_PREMIUM][STK] Instrument lookup failed {stock_sym} {strike} {opt}: {e}")

        if not tradingsymbol:
            tradingsymbol = f"{stock_sym}{str(year)[2:]}{month_abbr}{strike}{opt}"

        result = {
            "action":          "BUY",
            "symbol":          stock_sym,
            "strike":          strike,
            "option_type":     opt,
            "instrument_type": "STOCK_OPT",
            "expiry_date":     expiry_dt.strftime('%Y-%m-%d'),
            "tradingsymbol":   tradingsymbol,
            "quantity":        quantity,
            "exchange":        exchange,
            "entry_price":     entry,
            "stop_loss":       sl,
            "target":          tgt,
            "instrument":      stock_sym,
            "strike_price":    strike,
            "expiry_str":      month_abbr,
            "source":          "MCX_PREMIUM_STOCK_PARSER",
            "confidence":      "HIGH" if sl and tgt else "MEDIUM",
        }
        log.info(f"[MCX_PREMIUM][STK] {stock_sym} {strike} {opt} | Entry={entry} SL={sl} Tgt={tgt} | {tradingsymbol}")
        return result

    # ── Route b: index option  (BUY - INDEX STRIKECE/PE NEAR LEVEL) ──────────
    m = _MCX_INDEX_SIG.search(text)
    if m:
        raw_inst = m.group(1)
        strike   = m.group(2)
        raw_opt  = m.group(3)
        opt_m    = re.search(r'(ce|pe|call|put)', raw_opt, re.IGNORECASE)
        opt      = opt_m.group(1).upper() if opt_m else raw_opt.upper()

        name, itype = _normalise_instrument(raw_inst)

        # NEAR LEVEL -- price  (channel uses "--" not "AROUND")
        m_entry = _MCX_NEAR_LEVEL.search(text)
        entry   = float(m_entry.group(1)) if m_entry else _parse_entry(text)

        # STOPLOSS -- price  (channel uses "STOPLOSS" not "STOP LOSS")
        m_sl  = _MCX_SL.search(text)
        sl    = float(m_sl.group(1)) if m_sl else _parse_sl(text)

        # TARGET - t1/t2/...  (first number)
        m_tgt = _MCX_TARGET_MULTI.search(text)
        tgt   = float(m_tgt.group(1)) if m_tgt else _parse_targets(text)

        if entry is None:
            return None
        if sl is None and entry:
            sl = round(entry * 0.85, 2)

        month = _parse_month(text)
        return _build_result(name, itype, strike, opt, month, entry, sl, tgt, "MCX_PREMIUM_INDEX_PARSER")

    # ── Route c: MCX commodity — DISABLED (wrong strikes, unreliable P&L)
    if 'COMMODITY_MCX_TRAD' in text.upper() or _MCX_COMMODITY_SYMS.search(text):
        log.debug("[MCX_PREMIUM][MCX] Commodity option signal skipped — route c disabled")
        return None

    return None


# ═══════════════════════════════════════════════════════════════════════
# PARSER 7 — StockOptionsPrime  (STOCK OPTIONS PRIME channel, -1001553033593)  (STOCK OPTIONS PRIME channel, -1001553033593)
#
# Signal format:
#   "SHORT TERM  BUY TRENT JUNE 4100 CE  AROUND 185-188  TARGET 230,280,340+  STOP LOSS 120"
#   "SHORT TERM  BUY ADANIPORTS JUNE 1800 CE  AROUND 55-57  TARGET 80,100,120+  STOP LOSS 40"
#
# Key difference vs ShortTerm parser: stock symbol comes BEFORE the expiry month token,
# so the existing regex grabs JUNE as the symbol.  This parser handles that explicitly.
# ═══════════════════════════════════════════════════════════════════════

# Lazy singleton — loaded once per process, not on every call
_sop_fast_finder = None

def _get_sop_finder():
    global _sop_fast_finder
    if _sop_fast_finder is None:
        try:
            import sys as _sys
            _master_lib = r"C:\Users\meetm\OneDrive\Desktop\GCPPythonCode\MasterConfiguration\lib"
            if _master_lib not in _sys.path:
                _sys.path.append(_master_lib)
            from instrument_finder_FAST import FastInstrumentFinder
            from master_resource import get_parquet_path
            _sop_fast_finder = FastInstrumentFinder(get_parquet_path())
        except Exception as e:
            log.warning(f"[SOP_PARSER] FastInstrumentFinder unavailable: {e}")
    return _sop_fast_finder


def _last_thursday(year, month):
    """Last Thursday of the given month — stock option expiry day."""
    last_day = calendar.monthrange(year, month)[1]
    d = date(year, month, last_day)
    offset = (d.weekday() - 3) % 7   # 3 = Thursday
    return d.replace(day=last_day - offset)


# Noise: update/exit messages — start with a price, or contain profit/target-hit keywords
_SOP_NOISE = re.compile(
    r'(safe\s*trader|book\s*profit|almost\s*at\s*target|all\s*target|'
    r'target\s*\d+\s*done|exit\s*now|exit\s*all|sl\s*hit|stop\s*loss\s*hit|'
    r'join\s*fast|limited|premium|fees|webinar|contact|whatsapp)',
    re.IGNORECASE
)

# Updates start with: "212  TRENT JUNE 4100 CE!" — a bare price followed by contract
_SOP_UPDATE = re.compile(r'^\s*\d+\s+[A-Z]', re.IGNORECASE)

# Main entry signal
# Groups: (stock_symbol, month, strike, ce_pe, entry_range, first_target, stop_loss)
_SOP_SIGNAL = re.compile(
    r'(?:short\s*term\s+)?buy\s+'
    r'([A-Z&]{2,15})\s+'                                   # 1: stock symbol  e.g. TRENT
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+'  # 2: expiry month
    r'(\d{3,6})\s+'                                        # 3: strike        e.g. 4100
    r'(ce|pe)\s*'                                          # 4: option type
    r'(?:around|above|near)?\s*[:\-]?\s*'
    r'(\d+(?:\.\d+)?(?:\s*[-/]\s*\d+(?:\.\d+)?)?)'        # 5: entry / range
    r'.*?target\s*[:\-]?\s*(\d+(?:\.\d+)?)'               # 6: first target
    r'.*?stop\s*loss\s*[:\-]?\s*(\d+(?:\.\d+)?)',         # 7: stop loss
    re.IGNORECASE | re.DOTALL
)


def parse_stock_options_prime(text):
    """Dedicated parser for STOCK OPTIONS PRIME channel (-1001553033593)."""
    if _SOP_NOISE.search(text):
        return None
    if _SOP_UPDATE.match(text):
        return None
    # Futures signals in this channel (no CE/PE) — skip
    if re.search(r'\bfutures?\b', text, re.IGNORECASE) and not re.search(r'\b(ce|pe)\b', text, re.IGNORECASE):
        return None

    m = _SOP_SIGNAL.search(text)
    if not m:
        return None

    raw_symbol, raw_month, strike_str, opt_str, raw_entry, raw_tgt, raw_sl = m.groups()

    symbol = raw_symbol.upper()
    opt    = opt_str.upper()
    strike = int(strike_str)

    # Entry: handle range like "185-188" → midpoint
    range_m = re.match(r'(\d+(?:\.\d+)?)\s*[-/]\s*(\d+(?:\.\d+)?)', raw_entry.strip())
    entry = (float(range_m.group(1)) + float(range_m.group(2))) / 2 if range_m else float(raw_entry.split()[0])
    sl  = float(raw_sl)
    tgt = float(raw_tgt)

    # Expiry date: last Thursday of the stated month
    month_num = MONTH_MAP.get(raw_month.lower()[:3], datetime.now(IST).month)
    now = datetime.now(IST)
    year = now.year
    if month_num < now.month:
        year += 1
    expiry_dt  = _last_thursday(year, month_num)
    expiry_str = expiry_dt.strftime('%Y-%m-%d')
    month_abbr = raw_month[:3].upper()

    # Resolve tradingsymbol + lot_size from parquet
    tradingsymbol = None
    quantity  = None
    exchange  = 'NFO'
    finder = _get_sop_finder()
    if finder:
        try:
            inst = finder.find_instrument(symbol, strike, opt)
            if inst:
                tradingsymbol = inst.get('tradingsymbol')
                raw_qty = inst.get('lot_size')
                quantity  = int(raw_qty) if raw_qty is not None else None
                exchange  = inst.get('exchange', 'NFO')
        except Exception as e:
            log.warning(f"[SOP_PARSER] Instrument lookup failed for {symbol} {strike} {opt}: {e}")

    # Fallback tradingsymbol if parquet lookup missed
    if not tradingsymbol:
        tradingsymbol = f"{symbol}{str(year)[2:]}{month_abbr}{strike}{opt}"

    result = {
        "action":          "BUY",
        "symbol":          symbol,
        "strike":          strike,
        "option_type":     opt,
        "instrument_type": "STOCK_OPT",
        "expiry_date":     expiry_str,
        "tradingsymbol":   tradingsymbol,
        "quantity":        quantity,
        "exchange":        exchange,
        "entry_price":     entry,
        "stop_loss":       sl,
        "target":          tgt,
        # compatibility aliases expected by order placer
        "instrument":      symbol,
        "strike_price":    strike,
        "expiry_str":      month_abbr,
        "source":          "STOCK_OPTIONS_PRIME_PARSER",
        "confidence":      "HIGH",
        # Multi-day swing calls — skip 15-min time_sl, aggressive trail, and EOD force-exit
        "position_type":   "LONGTERM",
    }
    log.info(
        f"[SOP_PARSER] {symbol} {strike} {opt} | Entry={entry} SL={sl} Tgt={tgt} "
        f"| Expiry={expiry_str} | TS={tradingsymbol} | Qty={quantity}"
    )
    return result


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════

_PARSERS = {
    "shortterm":         parse_shortterm,
    "wealthworld":       parse_wealthworld,
    "sidharth":          parse_sidharth,
    "jp":                parse_jp,
    "premiumnb":         parse_premium_nifty_bnf,
    "pgso":              parse_pgso,
    "generic":           parse_generic,
    "futuresegment":     parse_futures_segment_batch,
    "commodityprime":    parse_commodity_prime,
    "investingkorner":   parse_investing_korner,
    "mcxpremium":        parse_mcx_premium,
    "stockoptionsprime": parse_stock_options_prime,
}

def get_channel_parser(channel_id: str):
    """
    Returns the dedicated parse function for this channel_id, or None.
    channel_id should be a string (e.g. str(event.chat_id)).
    For public channels Telethon may deliver a negative int or a username string;
    both are handled via the CHANNEL_PARSER_MAP keys.
    """
    key = CHANNEL_PARSER_MAP.get(str(channel_id))
    return _PARSERS.get(key) if key else None

