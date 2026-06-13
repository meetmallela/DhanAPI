"""
expiry_calendar.py
------------------
2026 NSE / BSE expiry calendar with holiday adjustments.

Source: Market_Expiery_Calendar.txt (Krishna's Trading Systems)

All dates are already adjusted for the 2026 NSE/BSE holiday list:
  Republic Day, Holi, Ram Navami, Good Friday, Ambedkar Jayanti,
  Maharashtra Day, Bakri Eid, Moharram, Ganesh Chaturthi,
  Gandhi Jayanti, Dussehra, Diwali, Guru Nanak Jayanti, Christmas.

Public API
----------
is_expiry_today(index)           → bool
is_monthly_expiry(index, d=None) → bool          NEW: True if last expiry of calendar month
expiry_type(index, d=None)       → str           NEW: 'MONTHLY' | 'WEEKLY' | 'NONE'
expiry_info_today()              → dict[str,str] NEW: {index: type} for all indices
is_market_holiday(d=None)        → bool
expiry_indices_today()           → list[str]   e.g. ["NIFTY", "FINNIFTY"]
next_expiry(index, from_date)    → date | None
get_expiry_dates(index)          → list[date]
morning_summary()                → str  (one-line log for startup banner)
"""

from datetime import date, timedelta
from typing import Optional


# ── Trading holidays 2026 (NSE + BSE) ────────────────────────────────────────

TRADING_HOLIDAYS: set[date] = {
    date(2026,  1, 26),   # Republic Day
    date(2026,  3,  3),   # Holi
    date(2026,  3, 26),   # Ram Navami
    date(2026,  4,  3),   # Good Friday
    date(2026,  4, 14),   # Ambedkar Jayanti
    date(2026,  5,  1),   # Maharashtra Day
    date(2026,  5, 28),   # Bakri Eid
    date(2026,  6, 26),   # Moharram
    date(2026,  9, 14),   # Ganesh Chaturthi
    date(2026, 10,  2),   # Gandhi Jayanti
    date(2026, 10, 20),   # Dussehra
    date(2026, 11, 10),   # Diwali (Balipratipada)
    date(2026, 11, 24),   # Guru Nanak Jayanti
    date(2026, 12, 25),   # Christmas
}


# ── NIFTY 50 expiry dates (standard: Tuesday) ─────────────────────────────────
# Jan 26 = Republic Day → Jan 27 not listed; Mar 3 Holi → Mar 2 (Mon);
# Mar 31 Mahavir Jayanti → Mar 30 (Mon); Apr 14 Ambedkar → Apr 13 (Mon);
# Oct 20 Dussehra → Oct 19 (Mon); Nov 10 Diwali → Nov 9 (Mon);
# Nov 24 Guru Nanak → Nov 23 (Mon).
_NIFTY_DATES: list[date] = [
    # January
    date(2026,  1,  6), date(2026,  1, 13), date(2026,  1, 20), date(2026,  1, 27),
    # February
    date(2026,  2,  3), date(2026,  2, 10), date(2026,  2, 17), date(2026,  2, 24),
    # March  — Holi(Tue Mar 3)→Mar 2(Mon); Mahavir Jayanti(Tue Mar 31)→Mar 30(Mon)
    date(2026,  3,  2), date(2026,  3, 10), date(2026,  3, 17), date(2026,  3, 24),
    date(2026,  3, 30),
    # April  — Ambedkar Jayanti(Tue Apr 14)→Apr 13(Mon)
    date(2026,  4,  7), date(2026,  4, 13), date(2026,  4, 21), date(2026,  4, 28),
    # May
    date(2026,  5,  5), date(2026,  5, 12), date(2026,  5, 19), date(2026,  5, 26),
    # June
    date(2026,  6,  2), date(2026,  6,  9), date(2026,  6, 16), date(2026,  6, 23),
    date(2026,  6, 30),
    # July
    date(2026,  7,  7), date(2026,  7, 14), date(2026,  7, 21), date(2026,  7, 28),
    # August
    date(2026,  8,  4), date(2026,  8, 11), date(2026,  8, 18), date(2026,  8, 25),
    # September
    date(2026,  9,  1), date(2026,  9,  8), date(2026,  9, 15), date(2026,  9, 22),
    date(2026,  9, 29),
    # October — Dussehra(Tue Oct 20)→Oct 19(Mon)
    date(2026, 10,  6), date(2026, 10, 13), date(2026, 10, 19), date(2026, 10, 27),
    # November — Diwali(Tue Nov 10)→Nov 9(Mon); Guru Nanak(Tue Nov 24)→Nov 23(Mon)
    date(2026, 11,  3), date(2026, 11,  9), date(2026, 11, 17), date(2026, 11, 23),
    # December
    date(2026, 12,  1), date(2026, 12,  8), date(2026, 12, 15), date(2026, 12, 22),
    date(2026, 12, 29),
]

# FINNIFTY: monthly expiry only (last Tuesday of each month) — no weekly contract
# after SEBI 2023 rationalization. Same dates as BANKNIFTY (both last Tuesday).
_FINNIFTY_DATES: list[date] = [
    date(2026,  1, 27),   # Jan
    date(2026,  2, 24),   # Feb
    date(2026,  3, 30),   # Mar (Mahavir Jayanti → Mon)
    date(2026,  4, 28),   # Apr
    date(2026,  5, 26),   # May
    date(2026,  6, 30),   # Jun
    date(2026,  7, 28),   # Jul
    date(2026,  8, 25),   # Aug
    date(2026,  9, 29),   # Sep
    date(2026, 10, 27),   # Oct
    date(2026, 11, 23),   # Nov (Guru Nanak → Mon)
    date(2026, 12, 29),   # Dec
]


# ── BANKNIFTY expiry dates (MONTHLY: last Tuesday of each month) ──────────────
# BankNifty moved from weekly Wednesday to monthly expiry effective 2026.
# Monthly expiry aligns with NIFTY's monthly (last Tuesday), holiday-adjusted:
#   Mar 31 (Mahavir Jayanti) → Mar 30 (Mon)
#   Nov 24 (Guru Nanak Jayanti) → Nov 23 (Mon)
_BANKNIFTY_DATES: list[date] = [
    date(2026,  1, 27),   # Jan
    date(2026,  2, 24),   # Feb
    date(2026,  3, 30),   # Mar (Mahavir Jayanti → Mon)
    date(2026,  4, 28),   # Apr
    date(2026,  5, 26),   # May
    date(2026,  6, 30),   # Jun
    date(2026,  7, 28),   # Jul
    date(2026,  8, 25),   # Aug
    date(2026,  9, 29),   # Sep
    date(2026, 10, 27),   # Oct
    date(2026, 11, 23),   # Nov (Guru Nanak → Mon)
    date(2026, 12, 29),   # Dec
]


# ── SENSEX expiry dates (standard: Thursday, BSE) ─────────────────────────────
# Jan 1 = New Year (closed); Jan 15 = Municipal Polls → Jan 14(Wed);
# Mar 26 Ram Navami(Thu) → Mar 25(Wed);
# May 28 Bakri Eid(Thu)  → May 27(Wed);
# Dec 25 Christmas(Fri) — Dec 24(Thu) is clear.
_SENSEX_DATES: list[date] = [
    # January — Jan 1 holiday (no expiry); Jan 15 Municipal Polls→Jan 14(Wed)
    date(2026,  1,  8), date(2026,  1, 14), date(2026,  1, 22), date(2026,  1, 29),
    # February
    date(2026,  2,  5), date(2026,  2, 12), date(2026,  2, 19), date(2026,  2, 26),
    # March — Ram Navami(Thu Mar 26)→Mar 25(Wed)
    date(2026,  3,  5), date(2026,  3, 12), date(2026,  3, 19), date(2026,  3, 25),
    # April
    date(2026,  4,  2), date(2026,  4,  9), date(2026,  4, 16), date(2026,  4, 23),
    date(2026,  4, 30),
    # May — Bakri Eid(Thu May 28)→May 27(Wed)
    date(2026,  5,  7), date(2026,  5, 14), date(2026,  5, 21), date(2026,  5, 27),
    # June
    date(2026,  6,  4), date(2026,  6, 11), date(2026,  6, 18), date(2026,  6, 25),
    # July
    date(2026,  7,  2), date(2026,  7,  9), date(2026,  7, 16), date(2026,  7, 23),
    date(2026,  7, 30),
    # August
    date(2026,  8,  6), date(2026,  8, 13), date(2026,  8, 20), date(2026,  8, 27),
    # September — Sep 14 Ganesh Chaturthi(Mon); Thu series unaffected
    date(2026,  9,  3), date(2026,  9, 10), date(2026,  9, 17), date(2026,  9, 24),
    # October
    date(2026, 10,  1), date(2026, 10,  8), date(2026, 10, 15), date(2026, 10, 22),
    date(2026, 10, 29),
    # November
    date(2026, 11,  5), date(2026, 11, 12), date(2026, 11, 19), date(2026, 11, 26),
    # December — Christmas(Fri Dec 25); Dec 24(Thu) is the expiry
    date(2026, 12,  3), date(2026, 12, 10), date(2026, 12, 17), date(2026, 12, 24),
]


# ── MIDCPNIFTY expiry dates (standard: Monday) ────────────────────────────────
# Jan 26 Republic Day(Mon) → Jan 27(Tue);
# Sep 14 Ganesh Chaturthi(Mon) → Sep 11(Fri).
_MIDCPNIFTY_DATES: list[date] = [
    # January — Republic Day(Mon Jan 26)→Jan 27(Tue)
    date(2026,  1,  5), date(2026,  1, 12), date(2026,  1, 19), date(2026,  1, 27),
    # February
    date(2026,  2,  2), date(2026,  2,  9), date(2026,  2, 16), date(2026,  2, 23),
    # March
    date(2026,  3,  2), date(2026,  3,  9), date(2026,  3, 16), date(2026,  3, 23),
    date(2026,  3, 30),
    # April
    date(2026,  4,  6), date(2026,  4, 13), date(2026,  4, 20), date(2026,  4, 27),
    # May
    date(2026,  5,  4), date(2026,  5, 11), date(2026,  5, 18), date(2026,  5, 25),
    # June
    date(2026,  6,  1), date(2026,  6,  8), date(2026,  6, 15), date(2026,  6, 22),
    date(2026,  6, 29),
    # July
    date(2026,  7,  6), date(2026,  7, 13), date(2026,  7, 20), date(2026,  7, 27),
    # August
    date(2026,  8,  3), date(2026,  8, 10), date(2026,  8, 17), date(2026,  8, 24),
    date(2026,  8, 31),
    # September — Ganesh Chaturthi(Mon Sep 14)→Sep 11(Fri)
    date(2026,  9,  7), date(2026,  9, 11), date(2026,  9, 21), date(2026,  9, 28),
    # October
    date(2026, 10,  5), date(2026, 10, 12), date(2026, 10, 19), date(2026, 10, 26),
    # November
    date(2026, 11,  2), date(2026, 11,  9), date(2026, 11, 16), date(2026, 11, 23),
    # December
    date(2026, 12,  7), date(2026, 12, 14), date(2026, 12, 21), date(2026, 12, 28),
]


# ── Master lookup table ───────────────────────────────────────────────────────

_INDEX_EXPIRY_MAP: dict[str, list[date]] = {
    "NIFTY":      _NIFTY_DATES,
    "FINNIFTY":   _FINNIFTY_DATES,
    "BANKNIFTY":  _BANKNIFTY_DATES,
    "SENSEX":     _SENSEX_DATES,
    "MIDCPNIFTY": _MIDCPNIFTY_DATES,
}

# Pre-build a set-per-index for O(1) lookup
_INDEX_EXPIRY_SET: dict[str, set[date]] = {
    idx: set(dates) for idx, dates in _INDEX_EXPIRY_MAP.items()
}

# Monthly expiry = last expiry date of each calendar month per index.
# For indices with weekly contracts (NIFTY, SENSEX, MIDCPNIFTY) this is the
# last expiry in the month; all other dates are weekly.
# For monthly-only indices (BANKNIFTY, FINNIFTY) every date is monthly.
from collections import defaultdict as _dd
_MONTHLY_EXPIRY_SET: dict[str, set[date]] = {}
for _idx, _dlist in _INDEX_EXPIRY_MAP.items():
    _by_month: dict = _dd(list)
    for _d in _dlist:
        _by_month[(_d.year, _d.month)].append(_d)
    _MONTHLY_EXPIRY_SET[_idx] = {max(v) for v in _by_month.values()}
del _dd, _idx, _dlist, _by_month, _d


# ── Public API ────────────────────────────────────────────────────────────────

def get_expiry_dates(index: str) -> list[date]:
    """Return sorted list of all 2026 expiry dates for the given index."""
    return sorted(_INDEX_EXPIRY_MAP.get(index.upper(), []))


def is_expiry_today(index: str, today: Optional[date] = None) -> bool:
    """True if today (or the supplied date) is an expiry day for the index."""
    d = today or date.today()
    return d in _INDEX_EXPIRY_SET.get(index.upper(), set())


def is_monthly_expiry(index: str, d: Optional[date] = None) -> bool:
    """True if d is the last (monthly) expiry of its calendar month for the index.
    Always False when d is not an expiry day at all."""
    check = d or date.today()
    idx   = index.upper()
    return check in _MONTHLY_EXPIRY_SET.get(idx, set())


def expiry_type(index: str, d: Optional[date] = None) -> str:
    """Return 'MONTHLY', 'WEEKLY', or 'NONE' for the given date and index.

    MONTHLY — last expiry of the calendar month (e.g. NIFTY last Tuesday)
    WEEKLY  — any earlier expiry in the month (NIFTY, SENSEX, MIDCPNIFTY only)
    NONE    — not an expiry day for this index
    """
    check = d or date.today()
    idx   = index.upper()
    if check not in _INDEX_EXPIRY_SET.get(idx, set()):
        return "NONE"
    if check in _MONTHLY_EXPIRY_SET.get(idx, set()):
        return "MONTHLY"
    return "WEEKLY"


def expiry_info_today(today: Optional[date] = None) -> dict[str, str]:
    """Return {index: 'MONTHLY'|'WEEKLY'|'NONE'} for all tracked indices."""
    d = today or date.today()
    return {idx: expiry_type(idx, d) for idx in _INDEX_EXPIRY_MAP}


def expiry_indices_today(today: Optional[date] = None) -> list[str]:
    """Return list of index names that expire today."""
    d = today or date.today()
    return [idx for idx, s in _INDEX_EXPIRY_SET.items() if d in s]


def is_market_holiday(d: Optional[date] = None) -> bool:
    """True if the date is a declared NSE/BSE trading holiday."""
    check = d or date.today()
    return check in TRADING_HOLIDAYS


def next_expiry(index: str, from_date: Optional[date] = None) -> Optional[date]:
    """
    Return the next expiry date for the index on or after from_date.
    Returns None if no expiry found in the 2026 calendar.
    """
    base = from_date or date.today()
    for d in sorted(_INDEX_EXPIRY_MAP.get(index.upper(), [])):
        if d >= base:
            return d
    return None


def days_to_expiry(index: str, from_date: Optional[date] = None) -> Optional[int]:
    """Calendar days from from_date to the next expiry (0 = today is expiry)."""
    base = from_date or date.today()
    nxt  = next_expiry(index, base)
    return (nxt - base).days if nxt else None


def morning_summary(today: Optional[date] = None) -> str:
    """
    One-line expiry status string for the morning startup log.

    Example outputs:
      "Expiry today: NIFTY FINNIFTY  |  Next: BANKNIFTY in 2d (Wed 2026-01-07)"
      "No expiry today  |  Next: NIFTY in 3d (Tue 2026-01-06)"
    """
    d   = today or date.today()
    exp = expiry_indices_today(d)

    parts = []
    if exp:
        parts.append(f"Expiry TODAY: {' '.join(exp)}")
    else:
        parts.append("No expiry today")

    # Find the nearest upcoming expiry across all indices
    upcoming = []
    for idx in _INDEX_EXPIRY_MAP:
        nxt = next_expiry(idx, d + timedelta(days=1))  # strictly after today
        if nxt:
            upcoming.append((nxt, idx))
    if upcoming:
        upcoming.sort()
        nxt_date, nxt_idx = upcoming[0]
        gap = (nxt_date - d).days
        dow = nxt_date.strftime("%a")
        parts.append(f"Next expiry: {nxt_idx} in {gap}d ({dow} {nxt_date})")

    if is_market_holiday(d):
        parts.append("*** MARKET HOLIDAY TODAY ***")

    return "  |  ".join(parts)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    today = date.today()
    print(f"\n{'='*70}")
    print(f"  Expiry Calendar — {today}")
    print(f"{'='*70}")
    print(f"  {morning_summary(today)}")
    print()

    for idx in ["NIFTY", "FINNIFTY", "BANKNIFTY", "SENSEX", "MIDCPNIFTY"]:
        dte   = days_to_expiry(idx)
        nxt   = next_expiry(idx)
        etype = expiry_type(idx)
        flag  = f" << {etype} EXPIRY TODAY" if etype != "NONE" else ""
        print(f"  {idx:<12} next={nxt}  DTE={dte}d{flag}")

    print()
    hol = is_market_holiday()
    print(f"  Market holiday today: {'YES' if hol else 'NO'}")
    print(f"{'='*70}\n")
