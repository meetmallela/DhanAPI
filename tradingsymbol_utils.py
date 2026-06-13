"""
tradingsymbol_utils.py
Utility for generating correct Zerodha tradingsymbol format

RULES:
1. NIFTY & SENSEX have weekly expiries
2. Last week of month = Monthly format: NIFTY26FEB25000CE
3. Earlier weeks = Weekly format: NIFTY2620325000CE (YY + M + DD)
4. All other symbols = Always monthly format
5. Stock options = Always include day: TATASTEEL30JAN25180CE
"""

import calendar
from datetime import datetime


def is_last_week_of_month(expiry_date):
    """
    Check if expiry date is in the last week of the month
    
    Args:
        expiry_date: datetime object or string in format 'YYYY-MM-DD'
    
    Returns:
        bool: True if in last week of month
    """
    if isinstance(expiry_date, str):
        expiry_date = datetime.strptime(expiry_date, '%Y-%m-%d')
    
    # Get last day of month
    last_day = calendar.monthrange(expiry_date.year, expiry_date.month)[1]
    
    # Last week = last 7 days of month
    return expiry_date.day > (last_day - 7)


def get_correct_tradingsymbol(symbol, strike, option_type, expiry_date):
    """
    Generate correct tradingsymbol format based on symbol and expiry
    
    RULES:
    - NIFTY & SENSEX with weekly expiries
      - Last week of month: Monthly format NIFTY26FEB25000CE
      - Earlier weeks: Weekly format NIFTY2620325000CE (YY + M + DD)
    - All other indices (BANKNIFTY, FINNIFTY, etc.): Monthly format only
    - Stock options: Always include day TATASTEEL30JAN25180CE
    
    Args:
        symbol: str - 'NIFTY', 'SENSEX', 'BANKNIFTY', 'LT', etc.
        strike: int/float - Strike price
        option_type: str - 'CE' or 'PE'
        expiry_date: str or datetime - Expiry date
    
    Returns:
        str: Correctly formatted tradingsymbol
    
    Examples:
        >>> get_correct_tradingsymbol('NIFTY', 25000, 'CE', '2026-02-03')
        'NIFTY2620325000CE'
        
        >>> get_correct_tradingsymbol('NIFTY', 25000, 'CE', '2026-02-24')
        'NIFTY26FEB25000CE'
        
        >>> get_correct_tradingsymbol('BANKNIFTY', 59600, 'CE', '2026-02-24')
        'BANKNIFTY26FEB59600CE'
        
        >>> get_correct_tradingsymbol('LT', 3900, 'CE', '2026-02-24')
        'LT24FEB263900CE'
    """
    # Parse expiry date if string
    if isinstance(expiry_date, str):
        exp_dt = datetime.strptime(expiry_date, '%Y-%m-%d')
    else:
        exp_dt = expiry_date
    
    # Get components
    year = exp_dt.strftime('%y')  # '26'
    month_abbr = exp_dt.strftime('%b').upper()  # 'FEB'
    month_no_zero = str(exp_dt.month)  # '2' (no leading zero)
    day_with_zero = exp_dt.strftime('%d')  # '03' (with leading zero)
    day_no_zero = str(exp_dt.day)  # '3' (no leading zero)
    
    # Convert strike to string (remove decimals if whole number)
    strike_str = str(int(strike)) if float(strike) == int(strike) else str(strike)
    
    # Define symbol categories
    WEEKLY_SYMBOLS = ['NIFTY', 'SENSEX']
    INDEX_SYMBOLS = ['NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX']
    
    # Determine format based on symbol type
    if symbol in WEEKLY_SYMBOLS:
        # NIFTY & SENSEX have weekly expiries
        if is_last_week_of_month(exp_dt):
            # Last week = Monthly format: NIFTY26FEB25000CE
            tradingsymbol = f"{symbol}{year}{month_abbr}{strike_str}{option_type}"
        else:
            # Earlier weeks = Weekly format: NIFTY2620325000CE (YY + M + DD)
            tradingsymbol = f"{symbol}{year}{month_no_zero}{day_with_zero}{strike_str}{option_type}"
    
    elif symbol in INDEX_SYMBOLS:
        # Other indices always use monthly format (no day)
        # Format: BANKNIFTY26FEB59600CE
        tradingsymbol = f"{symbol}{year}{month_abbr}{strike_str}{option_type}"
    
    else:
        # Stock options use MONTHLY format (same as indices)
        # Format: PERSISTENT26FEB5800PE (no day included!)
        tradingsymbol = f"{symbol}{year}{month_abbr}{strike_str}{option_type}"
    
    return tradingsymbol


# Test function
if __name__ == '__main__':
    test_cases = [
        # (symbol, strike, option_type, expiry_date, expected_format)
        ('NIFTY', 25000, 'CE', '2026-02-03', 'NIFTY2620325000CE'),  # Week 1 - Weekly
        ('NIFTY', 25000, 'CE', '2026-02-10', 'NIFTY2621025000CE'),  # Week 2 - Weekly
        ('NIFTY', 25000, 'CE', '2026-02-17', 'NIFTY2621725000CE'),  # Week 3 - Weekly
        ('NIFTY', 25000, 'CE', '2026-02-24', 'NIFTY26FEB25000CE'),  # Week 4 - Monthly
        ('SENSEX', 80000, 'PE', '2026-02-05', 'SENSEX2620580000PE'),  # Week 1 - Weekly
        ('SENSEX', 80000, 'PE', '2026-02-26', 'SENSEX26FEB80000PE'),  # Last week - Monthly
        ('BANKNIFTY', 59600, 'CE', '2026-02-24', 'BANKNIFTY26FEB59600CE'),  # Always monthly
        ('FINNIFTY', 24000, 'PE', '2026-02-24', 'FINNIFTY26FEB24000PE'),  # Always monthly
        ('LT', 3900, 'CE', '2026-02-24', 'LT26FEB3900CE'),  # Stock - MONTHLY format (no day!)
        ('HINDZINC', 640, 'CE', '2026-02-24', 'HINDZINC26FEB640CE'),  # Stock - MONTHLY format
        ('PERSISTENT', 5800, 'PE', '2026-02-24', 'PERSISTENT26FEB5800PE'),  # Your example!
    ]
    
    print("Testing tradingsymbol generation:")
    print("="*80)
    
    all_passed = True
    for symbol, strike, opt_type, expiry, expected in test_cases:
        result = get_correct_tradingsymbol(symbol, strike, opt_type, expiry)
        status = "✅ PASS" if result == expected else "❌ FAIL"
        
        if result != expected:
            all_passed = False
            print(f"{status} | {symbol:12} | {expiry} | Expected: {expected:25} | Got: {result}")
        else:
            print(f"{status} | {symbol:12} | {expiry} | {result}")
    
    print("="*80)
    if all_passed:
        print("✅ All tests passed!")
    else:
        print("❌ Some tests failed - review logic")
