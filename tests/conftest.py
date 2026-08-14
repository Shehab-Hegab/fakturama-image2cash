"""Shared pytest fixtures and sys.path bootstrap for the test-suite.

The package lives under ``src/`` and is not pip-installed in this repo, so the
first thing this file does is put ``src`` on ``sys.path``. Everything else is
shared fixtures used across the individual test modules.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from fakturama_i2c.config import Settings
from fakturama_i2c.models import ExtractedOrder, ItemLine


def order_payload() -> dict:
    """A valid, self-consistent order payload (single source of truth).

    Totals are chosen so ``reconcile_totals`` reports no mismatch, keeping the
    fixture usable for both the models tests and the extractor round-trip.
    """
    return {
        "header": {
            "order_date": "2026-03-15",
            "external_reference": "PO-4821",
            "price_mode": "Net",
            "vat_mode": "With VAT",
            "overall_discount_percent": 0,
            "shipping_amount": 0,
            "shipping_is_free": True,
        },
        "debtor": {
            "company": "Acme GmbH",
            "first_name": "",
            "last_name": "",
            "salutation": "---",
            "alias": "",
            "billing_address": {
                "street": "Hauptstr. 1",
                "zip_code": "10115",
                "city": "Berlin",
                "country": "DE",
                "email": "kontakt@acme.example",
                "telephone": "",
            },
            "delivery_address": {
                "street": "",
                "zip_code": "",
                "city": "",
                "country": "",
                "email": "",
                "telephone": "",
            },
            "same_delivery_address": True,
            "payment_method": "Bank Transfer",
            "price_mode": "Net",
            "discount_percent": 0,
        },
        "items": [
            {
                "sku": "A-100",
                "description": "Mechanical keyboard",
                "quantity": 2,
                "unit_net_price": 10.00,
                "vat_percent": 19,
                "discount_percent": 0,
            },
            {
                "sku": "B-200",
                "description": "USB-C cable",
                "quantity": 3,
                "unit_net_price": 7.50,
                "vat_percent": 7,
                "discount_percent": 10,
            },
        ],
        "totals": {"total_net": 40.25, "total_vat": 5.22, "total_gross": 45.47, "currency": "EUR"},
        "payment": {"paid_status": "Unpaid", "payment_date": None, "payment_method": "Bank Transfer"},
    }


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env()


@pytest.fixture
def sample_items() -> list[ItemLine]:
    """Three lines with known, hand-checked arithmetic."""
    return [
        ItemLine(
            sku="A-100",
            description="Mechanical keyboard",
            quantity=Decimal("2"),
            unit_net_price=Decimal("10.00"),
            vat_percent=Decimal("19"),
            discount_percent=Decimal("0"),
        ),
        ItemLine(
            sku="B-200",
            description="USB-C cable",
            quantity=Decimal("3"),
            unit_net_price=Decimal("7.50"),
            vat_percent=Decimal("7"),
            discount_percent=Decimal("10"),
        ),
        ItemLine(
            sku="C-300",
            description="Mousepad",
            quantity=Decimal("1"),
            unit_net_price=Decimal("100.00"),
            vat_percent=Decimal("0"),
            discount_percent=Decimal("25"),
        ),
    ]


@pytest.fixture
def sample_order() -> ExtractedOrder:
    return ExtractedOrder.model_validate(order_payload())