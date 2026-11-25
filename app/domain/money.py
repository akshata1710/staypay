"""Money formatting. Amounts are always integer minor units; only display code converts them.

Each currency has its own number of decimal places (ISO 4217 "exponent"):
USD 1200.00 is stored as 120000, JPY 12000 as 12000, KWD 1.500 as 1500.
"""

from decimal import Decimal

CURRENCY_EXPONENTS: dict[str, int] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "INR": 2,
    "CAD": 2,
    "AUD": 2,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "BHD": 3,
}


def exponent(currency: str) -> int:
    try:
        return CURRENCY_EXPONENTS[currency.upper()]
    except KeyError:
        raise ValueError(f"unknown currency: {currency}") from None


def format_money(minor: int, currency: str) -> str:
    e = exponent(currency)
    amount = Decimal(minor).scaleb(-e)
    return f"{amount:,.{e}f} {currency.upper()}"
