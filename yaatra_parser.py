"""
yaatra_parser.py
----------------
Parser for "Market Yaatra Official" Telegram channel.

Signal format observed (Feb–Mar 2026):
--------------------------------------
Instruments:
  - Index options  : NIFTY / BANKNIFTY / SENSEX  <strike> <expiry> CE/PE
  - Stock options  : <Stock> <strike> <month> CE/PE
  - MCX options    : NATGAS Mini <strike> <month> CE/CALL
  - Futures hint   : "Short <stock> march future" (skip — no structured entry)

Typical entry message:
    Nifty 24300 17th march CE
    CMP : 274
    SL  : 214
    TGT : 318, 335++

Variations seen:
  - "CMP :" / "CMP:" / "Cmp ;" / "CMPP :" (typo)  — all mean entry price
  - "SL : 0"  → hero-or-zero trade, no hard SL → use 15% default buffer
  - "Tgt : 205, 220, 240++"  → first target used; rest ignored
  - "tgt : 2x - 3x"          → skip, no numeric target
  - Inline single-line: "Nifty 24300 17th march CE\nCMPP : 170\nSL : 74\ntgt : 218, 244++"
  - Expiry formats: "17th march", "17 march", "17 Mar", "2 Mar", "2nd march",
                    "24 th march", "25 march", "10th Mar", "26 th feb"
  - Strike as word prefix: "Nifty 25700 2 Mar PE", "Sensex 82000 26 th feb PE"
  - Stock options: "Reliance 1380 March PE", "TCS 2440 March CE",
                   "ICICI Bank 1260 March PE", "#Hindzinc 550 PE"
  - MCX: "Natgas mini 295 24 Marc CE", "NATGAS Mini March 310 CALL"

Update/exit messages (NOT new signals — skip these):
  - "given at 198 | Now 174 | Still above SL"
  - "ALL TGT DONE, BOOK FULL PROFITS NOW"
  - "PROFIT : 1500 / lot"
  - "High made 250"
  - "exit half now"

Integration:
    from yaatra_parser import YaatraParser
    parser = YaatraParser()
    result = parser.parse(message_text)
    if result:
        # result is a dict compatible with existing parsed_data schema
        insert_signal(channel_id, channel_name, message_id, message_text, result)
"""

import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ── Month name → number ───────────────────────────────────────────────────────
MONTH_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,  'may': 5,  'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
    # common typos seen in channel
    'marc': 3, 'march': 3, 'february': 2, 'january': 1,
}

# ── Noise patterns — skip immediately ─────────────────────────────────────────
NOISE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'\b(good morning|good evening|good night)\b',
        r'\bhacked\b',
        r'\b(book|booked)\s+(full\s+)?profit',
        r'all\s+tgt\s+done',
        r'profit\s*:\s*\d+\s*/\s*lot',
        r'high\s+made\s*[:\-]?\s*\d+',
        r'given\s+at\s+\d+.*now\s+\d+',
        r'\b(exit|exited)\s+(half|full|now)\b',
        r'multibagger\s+stock',
        r'key\s+level',
        r'stoploss\s+triggered',
        r'dear\s+(students?|members?|traders?)',
        r'subscribe|subscription',
        r'market\s+mind',
        r'twitter|instagram|youtube|x\.com',
        r'portfolio\s+stock',
        r'3\s*(yr|year|yrs)',          # long-term stock picks
    ]
]

# ── Update patterns — these are follow-ups on existing signals, not new ones ──
UPDATE_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in [
        r'given\s+at\s+\d+',
        r'now\s+(its?\s+)?trading\s+at',
        r'revise\s+stoploss',
        r'trail\s+stoploss',
        r'put\s+sl\s+as\s+zero',
        r'place\s+stoploss\s+at',
        r'conservative\s+traders\s+can\s+book',
        r'book\s+(half|partial|profits?)',
        r'exit\s+(half|full|now|today)',
        r'still\s+above\s+sl',
        r'yday\s+option',
        r'hold\s+till\s+tomorrow',
    ]
]

# ── Index name normalisation ───────────────────────────────────────────────────
INDEX_MAP = {
    'nifty':     'NIFTY',
    'banknifty': 'BANKNIFTY',
    'bank nifty':'BANKNIFTY',
    'sensex':    'SENSEX',
    'midcap':    'MIDCPNIFTY',
    'finnifty':  'FINNIFTY',
    'natgas':    'NATURALGAS',
    'naturalgas':'NATURALGAS',
    'crudeoil':  'CRUDEOIL',
    'crude oil': 'CRUDEOIL',
    'gold':      'GOLD',
    'silver':    'SILVER',
    'silverm':   'SILVERM',
}

# ── Stock ticker hints seen in channel ─────────────────────────────────────────
STOCK_HINTS = {
    'reliance':  'RELIANCE',
    'tcs':       'TCS',
    'icicibank': 'ICICIBANK',
    'icici bank':'ICICIBANK',
    'hindzinc':  'HINDZINC',
    'sail':      'SAIL',
    'ola':       'OLAELEC',
    'hdfcbank':  'HDFCBANK',
}

# ── Main regex patterns ────────────────────────────────────────────────────────

# Instrument line: "NIFTY 25700 2 Mar PE" or "Sensex 82000 26th feb PE"
# Groups: (instrument, strike, day, month_str, option_type)
INSTRUMENT_RE = re.compile(
    r'(?:#)?'
    r'(nifty|banknifty|bank\s*nifty|sensex|midcap|finnifty|'
    r'natgas\s*mini?|naturalgas|crudeoil|crude\s*oil|gold|silver[m]?|'
    r'reliance|tcs|icici\s*bank|icicibank|hindzinc|sail|ola|hdfcbank|'
    r'[A-Z][A-Z0-9]+)'           # generic stock ticker
    r'\s+'
    r'(\d{3,6})'                  # strike price
    r'\s*'
    r'('                          # expiry group (optional day)
        r'(?:\d{1,2}\s*(?:st|nd|rd|th)?\s*)?'   # optional day e.g. "17th"
        r'(?:jan|feb|mar(?:c(?:h)?)?|apr|may|jun|jul|aug|sep|oct|nov|dec|'
        r'january|february|march|april|june|july|august|september|october|november|december)'
        r'(?:\s*\d{2,4})?'       # optional year
    r')'
    r'\s*'
    r'(ce|pe|call|put)',          # option type
    re.IGNORECASE
)

# Inline format with no expiry: "#Hindzinc 550 PE"
INSTRUMENT_NO_EXPIRY_RE = re.compile(
    r'(?:#)?'
    r'(reliance|tcs|icici\s*bank|icicibank|hindzinc|sail|ola|hdfcbank|[A-Z]{3,10})'
    r'\s+(\d{3,6})\s+(ce|pe|call|put)',
    re.IGNORECASE
)

# NATGAS special: "NATGAS Mini March 310 CALL"  (strike after month)
NATGAS_RE = re.compile(
    r'(natgas\s*mini?|naturalgas)\s+'
    r'(?:(?:\d{1,2}\s*(?:st|nd|rd|th)?\s*)?'
    r'(?:jan|feb|mar(?:c(?:h)?)?|apr|may|jun|jul|aug|sep|oct|nov|dec)\s+)?'
    r'(\d{3,6})\s+(ce|pe|call|put)',
    re.IGNORECASE
)

# CMP / entry price: handles "CMP : 198", "Cmp ; 16", "CMPP : 170", "Cmp 120"
CMP_RE = re.compile(r'(?:cmpp?|cmp)\s*[:\-;]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)

# SL: "SL : 134", "Sl : 0", "SL: 244"
SL_RE  = re.compile(r'\bsl\s*[:\-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)

# TGT: "Tgt : 234, 255, 280+++" — first numeric value taken
TGT_RE = re.compile(r'(?:tgt|target)\s*[:\-]?\s*(\d+(?:\.\d+)?)', re.IGNORECASE)

# Expiry date parser from captured group
EXPIRY_RE = re.compile(
    r'(\d{1,2})?\s*(?:st|nd|rd|th)?\s*'
    r'(jan|feb|mar(?:c(?:h)?)?|apr|may|jun|jul|aug|sep|oct|nov|dec|'
    r'january|february|march|april|june|july|august|september|october|november|december)',
    re.IGNORECASE
)


class YaatraParser:
    """
    Parses Market Yaatra Official messages into structured signal dicts.
    Returns None for noise, updates, and unrecognised messages.
    """

    CHANNEL_ID   = "-1003770951544"
    CHANNEL_NAME = "Market Yaatra Official"

    def parse(self, text: str) -> dict | None:
        if not text or not text.strip():
            return None

        text = text.strip()

        # 1. Noise gate
        if self._is_noise(text):
            logger.debug(f"[YAATRA] NOISE filtered: {text[:60]!r}")
            return None

        # 2. Update gate (follow-up messages on existing signals)
        if self._is_update(text):
            logger.debug(f"[YAATRA] UPDATE filtered: {text[:60]!r}")
            return None

        # 3. Try to extract instrument
        instrument_info = self._extract_instrument(text)
        if not instrument_info:
            logger.debug(f"[YAATRA] No instrument found: {text[:60]!r}")
            return None

        instrument, strike, expiry_str, option_type, instrument_type = instrument_info

        # 4. Extract prices
        cmp_match = CMP_RE.search(text)
        sl_match  = SL_RE.search(text)
        tgt_match = TGT_RE.search(text)

        # Need at least CMP to place a trade
        if not cmp_match:
            logger.debug(f"[YAATRA] No CMP found for {instrument} {strike}: {text[:60]!r}")
            return None

        entry_price = float(cmp_match.group(1))
        stop_loss   = float(sl_match.group(1))  if sl_match  else None
        target      = float(tgt_match.group(1)) if tgt_match else None

        # SL = 0 means "hero or zero" — use 15% buffer (same as existing default)
        if stop_loss is not None and stop_loss == 0:
            stop_loss = round(entry_price * 0.85, 2)
            logger.info(f"[YAATRA] Hero-or-zero SL → using 15% buffer: {stop_loss}")

        # 5. Build tradingsymbol
        tradingsymbol = self._build_tradingsymbol(
            instrument, strike, expiry_str, option_type, instrument_type
        )

        result = {
            "action":          "BUY",
            "instrument":      instrument,
            "instrument_type": instrument_type,
            "option_type":     option_type.upper().replace("CALL", "CE").replace("PUT", "PE"),
            "strike_price":    int(strike),
            "expiry_str":      expiry_str,
            "tradingsymbol":   tradingsymbol,
            "entry_price":     entry_price,
            "stop_loss":       stop_loss,
            "target":          target,
            "source":          "YAATRA_PARSER",
            "confidence":      "HIGH" if (sl_match and tgt_match) else "MEDIUM",
        }

        logger.info(
            f"[YAATRA] SIGNAL: {instrument} {strike} {option_type.upper()} "
            f"| Entry={entry_price} SL={stop_loss} Tgt={target} "
            f"| Symbol={tradingsymbol}"
        )
        return result

    # ── Private helpers ────────────────────────────────────────────────────────

    def _is_noise(self, text: str) -> bool:
        return any(p.search(text) for p in NOISE_PATTERNS)

    def _is_update(self, text: str) -> bool:
        return any(p.search(text) for p in UPDATE_PATTERNS)

    def _extract_instrument(self, text):
        """
        Returns (instrument_name, strike, expiry_str, option_type, instrument_type)
        or None.
        """
        # Try NATGAS special format first
        m = NATGAS_RE.search(text)
        if m:
            name  = "NATURALGAS"
            strike = m.group(2)
            opt    = m.group(3)
            return name, strike, "", opt, "MCX_OPT"

        # Try standard instrument line
        m = INSTRUMENT_RE.search(text)
        if m:
            raw_name   = m.group(1).strip().lower().replace(" ", "")
            strike     = m.group(2)
            expiry_raw = m.group(3).strip() if m.group(3) else ""
            opt        = m.group(4)

            name, itype = self._normalise_instrument(raw_name)
            expiry_str  = self._parse_expiry(expiry_raw)
            return name, strike, expiry_str, opt, itype

        # Try no-expiry stock format
        m = INSTRUMENT_NO_EXPIRY_RE.search(text)
        if m:
            raw_name = m.group(1).strip().lower().replace(" ", "")
            strike   = m.group(2)
            opt      = m.group(3)
            name, itype = self._normalise_instrument(raw_name)
            return name, strike, "", opt, itype

        return None

    def _normalise_instrument(self, raw: str):
        """Map raw channel name → canonical name + instrument type."""
        raw = raw.lower().replace("#", "").replace(" ", "")

        for key, val in INDEX_MAP.items():
            if key.replace(" ", "") in raw:
                itype = "MCX_OPT" if val in ("NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "SILVERM") else "INDEX_OPT"
                return val, itype

        for key, val in STOCK_HINTS.items():
            if key.replace(" ", "") in raw:
                return val, "STOCK_OPT"

        # Fallback — treat as stock ticker, uppercase
        return raw.upper(), "STOCK_OPT"

    def _parse_expiry(self, expiry_raw: str) -> str:
        """Convert '17th march' / '2 Mar' / '26 th feb' → 'DDMMM' e.g. '17MAR'."""
        if not expiry_raw:
            return ""
        m = EXPIRY_RE.search(expiry_raw)
        if not m:
            return expiry_raw.strip().upper()

        day_str    = m.group(1) or ""
        month_str  = m.group(2).lower()[:3]   # first 3 chars
        month_str  = "MAR" if month_str in ("mar", "mac") else month_str.upper()

        if day_str:
            return f"{int(day_str):02d}{month_str}"
        return month_str   # e.g. just "MAR" for monthly expiry

    def _build_tradingsymbol(self, instrument, strike, expiry_str, opt_type, itype):
        """
        Build a best-effort tradingsymbol.
        Final validation against valid_instruments.csv happens in InstrumentLookup.
        """
        opt = opt_type.upper().replace("CALL", "CE").replace("PUT", "PE")

        if itype == "INDEX_OPT":
            # e.g. NIFTY2531724300CE  (NIFTY + YYMMDD-style from InstrumentLookup)
            # We return a placeholder; InstrumentLookup will resolve properly
            return f"{instrument}_{expiry_str}_{strike}_{opt}"

        if itype == "MCX_OPT":
            return f"{instrument}_{expiry_str}_{strike}_{opt}"

        # Stock option
        return f"{instrument}_{expiry_str}_{strike}_{opt}"


# ── Convenience function for direct use in telegram_reader_production.py ──────

_parser = YaatraParser()

def parse_yaatra_message(text: str) -> dict | None:
    """
    Drop-in function. Returns parsed signal dict or None.
    Usage in telegram_reader_production.py:
        from yaatra_parser import parse_yaatra_message
        ...
        if channel_id == "-1003770951544":
            parsed = parse_yaatra_message(message_text)
    """
    return _parser.parse(text)


# ── Patch applied after initial testing ───────────────────────────────────────
# Fix 1: BANKNIFTY with dash: "Banknifty - 60700 Mar PE"
INSTRUMENT_RE_DASH = re.compile(
    r'(banknifty|bank\s*nifty)\s*[-–]\s*(\d{4,6})\s+'
    r'((?:\d{1,2}\s*(?:st|nd|rd|th)?\s*)?'
    r'(?:jan|feb|mar(?:c(?:h)?)?|apr|may|jun|jul|aug|sep|oct|nov|dec))'
    r'\s*(ce|pe|call|put)',
    re.IGNORECASE
)

# Fix 2: Bare strike with put/call only: "25200 2nd March put"
BARE_STRIKE_RE = re.compile(
    r'^[^a-zA-Z\n]*?(\d{4,6})\s+'
    r'((?:\d{1,2}\s*(?:st|nd|rd|th)?\s*)?'
    r'(?:jan|feb|mar(?:c(?:h)?)?|apr|may|jun|jul|aug|sep|oct|nov|dec))'
    r'\s+(ce|pe|call|put)',
    re.IGNORECASE
)

# Fix 3: Nifty with strike AFTER expiry-day: "Nifty 24 th march PE. 22800"
NIFTY_REVERSE_RE = re.compile(
    r'(nifty|banknifty|sensex)\s+'
    r'((?:\d{1,2}\s*(?:st|nd|rd|th)?\s*)?'
    r'(?:jan|feb|mar(?:c(?:h)?)?|apr|may|jun|jul|aug|sep|oct|nov|dec))'
    r'\s+(ce|pe|call|put)[.\s]+(\d{4,6})',
    re.IGNORECASE
)

# Monkey-patch _extract_instrument to include new patterns
_orig_extract = YaatraParser._extract_instrument

def _patched_extract(self, text):
    # Try original first
    result = _orig_extract(self, text)
    if result:
        return result

    # Fix 1: BANKNIFTY with dash
    m = INSTRUMENT_RE_DASH.search(text)
    if m:
        name  = "BANKNIFTY"
        strike = m.group(2)
        expiry = _parser._parse_expiry(m.group(3))
        opt    = m.group(4)
        return name, strike, expiry, opt, "INDEX_OPT"

    # Fix 2: Bare strike "25200 2nd March put" — assume NIFTY
    m = BARE_STRIKE_RE.search(text)
    if m:
        name   = "NIFTY"
        strike = m.group(1)
        expiry = _parser._parse_expiry(m.group(2))
        opt    = m.group(3)
        return name, strike, expiry, opt, "INDEX_OPT"

    # Fix 3: "Nifty 24th march PE. 22800"
    m = NIFTY_REVERSE_RE.search(text)
    if m:
        raw_name = m.group(1).lower()
        name = INDEX_MAP.get(raw_name, raw_name.upper())
        expiry = _parser._parse_expiry(m.group(2))
        opt    = m.group(3)
        strike = m.group(4)
        return name, strike, expiry, opt, "INDEX_OPT"

    return None

YaatraParser._extract_instrument = _patched_extract
_parser = YaatraParser()   # re-init with patched method

def parse_yaatra_message(text: str) -> dict | None:
    return _parser.parse(text)
