"""Tests for the typed UI element value helpers (ui/elements.py)."""

from __future__ import annotations

from decimal import Decimal

from fakturama_i2c.ui.elements import format_decimal, parse_decimal, parse_percent


def test_parse_decimal_de_notation() -> None:
    assert parse_decimal("1.234,56") == Decimal("1234.56")


def test_parse_decimal_en_notation() -> None:
    assert parse_decimal("1,234.56") == Decimal("1234.56")


def test_parse_decimal_comma_decimal() -> None:
    assert parse_decimal("12,5") == Decimal("12.5")


def test_parse_decimal_with_currency() -> None:
    assert parse_decimal("€ 1.299,00") == Decimal("1299.00")


def test_parse_decimal_empty() -> None:
    assert parse_decimal("") == Decimal("0")


def test_parse_percent() -> None:
    assert parse_percent("19%") == Decimal("19")
    assert parse_percent("7,5 %") == Decimal("7.5")


def test_format_decimal_two_places() -> None:
    assert format_decimal(Decimal("12.50")) == "12.50"
    assert format_decimal(Decimal("3")) == "3.00"
    assert format_decimal(Decimal("19")) == "19.00"