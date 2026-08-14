"""Normalization helpers: raw OCR text -> typed values.

OCR and LLM output is never trusted verbatim. Every scalar that ends up in a
pydantic model first passes through one of these functions so that German and
English number/date conventions collapse onto the same canonical form.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..models import PaidStatus

_THOUSANDS_SEP = re.compile(r"[ .]")
_CURRENCY = re.compile(r"[\u20ac€$£]\s*|\s*(?:EUR|USD|GBP)\b", re.IGNORECASE)
_NON_DECIMAL = re.compile(r"[^\d.,+-]")


def strip_currency(text: str) -> str:
    """Remove currency symbols and codes, keep digits/separators."""
    return _CURRENCY.sub("", text or "").strip()


def normalize_decimal(text: str) -> Decimal:
    """Parse a money/number that may use either `1,234.56` or `1.234,56`.

    Rules: commas used 3-in-a-row are thousands separators, a comma with 1-2
    decimals is the decimal mark; otherwise a lone dot with 1-2 decimals is the
    decimal mark. Returns 0 for empty/'-'.
    """
    raw = strip_currency(text or "").strip()
    if not raw or raw in {"-", "—", "–"}:
        return Decimal("0")
    raw = _NON_DECIMAL.sub("", raw)

    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif "," in raw:
        parts = raw.split(",")
        if len(parts[-1]) in {1, 2} and len(parts) <= 2:
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return Decimal(raw)
    except InvalidOperation:
        return Decimal("0")


def normalize_percent(text: str) -> Decimal:
    """Parse `19%`, `19`, `19,0` into Decimal('19.0')."""
    raw = (text or "").replace("%", "").strip()
    return normalize_decimal(raw)


def normalize_quantity(text: str) -> Decimal:
    """Parse a line quantity; keeps decimals, drops trailing noise."""
    return normalize_decimal(text)


_DATE_FORMATS = (
    "%d.%m.%Y",
    "%d.%m.%y",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d %b %Y",
    "%B %d, %Y",
    "%d %B %Y",
)

# Locale-independent English month names: ``strptime("%b")`` depends on the
# process locale, so English month dates are parsed explicitly instead.
_EN_MONTHS: dict[str, int] = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}
_EN_DAY_MONTH_YEAR = re.compile(r"^(?P<d>\d{1,2})\s+(?P<m>[A-Za-z]+)[,\s]+(?P<y>\d{4})$")
_EN_MONTH_DAY_YEAR = re.compile(r"^(?P<m>[A-Za-z]+)\s+(?P<d>\d{1,2})[,\s]+(?P<y>\d{4})$")

# Trailing time component ("15.03.2026 14:30", "2026-03-15T14:30:00"). Only
# matched at end-of-string so spaced English dates are left intact.
_TIME_SUFFIX = re.compile(r"(?:[Tt]\s*|\s+)\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?$")


def _parse_english_month(raw: str) -> Optional[date]:
    for pattern in (_EN_DAY_MONTH_YEAR, _EN_MONTH_DAY_YEAR):
        match = pattern.match(raw)
        if not match:
            continue
        month = _EN_MONTHS.get(match.group("m").lower())
        if month is None:
            return None
        return date(int(match.group("y")), month, int(match.group("d")))
    return None


def normalize_date(text: str) -> date:
    """Parse a date from OCR/LLM text; raises ValueError when unparseable.

    ``None``/empty is returned as-is so callers can treat it as 'not supplied'.
    English month names are parsed explicitly and are locale-independent.
    """
    raw = (text or "").strip()
    if not raw or raw.lower() in {"n/a", "na", "-", "—", "–"}:
        return None  # type: ignore[return-value]
    raw = _TIME_SUFFIX.sub("", raw)  # drop a trailing time part
    english = _parse_english_month(raw)
    if english is not None:
        return english
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized date: {text!r}")


_PAID_KEYWORDS: list[tuple[PaidStatus, tuple[str, ...]]] = [
    # DEPOSIT first so "part paid" wins over the standalone "paid" keyword.
    (PaidStatus.DEPOSIT, ("deposit", "anzahlung", "part paid")),
    (PaidStatus.PAID, ("paid", "bezahlt", "zahlung erhalten")),
    (PaidStatus.UNPAID, ("unpaid", "offen", "open", "due")),
]


def _keyword_hit(text: str, keyword: str) -> bool:
    # Word-boundary match: "paid" must not match inside "unpaid".
    return re.search(rf"\b{re.escape(keyword)}\b", text) is not None


def normalize_paid_status(text: str) -> PaidStatus:
    """Map a free-text payment status line to :class:`PaidStatus`.

    Empty/unknown defaults to UNPAID -- the safest reading for an order image
    (an order is not paid until an invoice says so).
    """
    raw = (text or "").strip().lower()
    if not raw:
        return PaidStatus.UNPAID
    for status, keywords in _PAID_KEYWORDS:
        if any(_keyword_hit(raw, k) for k in keywords):
            return status
    return PaidStatus.UNPAID