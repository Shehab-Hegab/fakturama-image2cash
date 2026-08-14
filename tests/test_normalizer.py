"""Tests for the OCR/LLM normalization helpers (normalizer.py)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from fakturama_i2c.extraction.normalizer import (
    normalize_date,
    normalize_decimal,
    normalize_paid_status,
    normalize_percent,
    normalize_quantity,
    strip_currency,
)
from fakturama_i2c.models import PaidStatus


def test_normalize_decimal_de_notation() -> None:
    assert normalize_decimal("1.234,56") == Decimal("1234.56")


def test_normalize_decimal_en_notation() -> None:
    assert normalize_decimal("1,234.56") == Decimal("1234.56")


def test_normalize_decimal_plain() -> None:
    assert normalize_decimal("12.345") == Decimal("12.345")
    assert normalize_decimal("12,5") == Decimal("12.5")


def test_normalize_decimal_with_currency() -> None:
    assert normalize_decimal("€ 1.299,00") == Decimal("1299.00")
    assert normalize_decimal("1.234,56 €") == Decimal("1234.56")
    assert normalize_decimal("EUR 99,90") == Decimal("99.90")


def test_normalize_decimal_empty_and_dash() -> None:
    assert normalize_decimal("") == Decimal("0")
    assert normalize_decimal("-") == Decimal("0")
    assert normalize_decimal("—") == Decimal("0")


def test_strip_currency() -> None:
    assert strip_currency("€ 12,50") == "12,50"
    assert strip_currency("12,50 EUR") == "12,50"
    assert strip_currency("12,50") == "12,50"


def test_normalize_percent() -> None:
    assert normalize_percent("19%") == Decimal("19")
    assert normalize_percent("19,5%") == Decimal("19.5")
    assert normalize_percent("7") == Decimal("7")


def test_normalize_quantity() -> None:
    assert normalize_quantity("3") == Decimal("3")
    assert normalize_quantity("2,5") == Decimal("2.5")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15.03.2026", date(2026, 3, 15)),
        ("2026-03-15", date(2026, 3, 15)),
        ("Mar 15, 2026", date(2026, 3, 15)),
        ("15 Mar 2026", date(2026, 3, 15)),
        ("March 15, 2026", date(2026, 3, 15)),
        ("2026/03/15", date(2026, 3, 15)),
        ("15.03.2026 14:30", date(2026, 3, 15)),
        ("2026-03-15T14:30:00", date(2026, 3, 15)),
    ],
)
def test_normalize_date_formats(text, expected) -> None:
    assert normalize_date(text) == expected


def test_normalize_date_invalid_raises() -> None:
    with pytest.raises(ValueError):
        normalize_date("not-a-date")
    with pytest.raises(ValueError):
        normalize_date("32.13.2026")


def test_normalize_date_empty_returns_none() -> None:
    assert normalize_date("") is None
    assert normalize_date("n/a") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("paid", PaidStatus.PAID),
        ("bezahlt", PaidStatus.PAID),
        ("anzahlung", PaidStatus.DEPOSIT),
        ("part paid", PaidStatus.DEPOSIT),
        ("", PaidStatus.UNPAID),
        ("offen", PaidStatus.UNPAID),
        ("Open", PaidStatus.UNPAID),
        ("unpaid", PaidStatus.UNPAID),
        ("unknown wording", PaidStatus.UNPAID),
    ],
)
def test_normalize_paid_status(text, expected) -> None:
    assert normalize_paid_status(text) == expected