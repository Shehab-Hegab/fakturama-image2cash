"""Tests for the pydantic domain models (see ``src/fakturama_i2c/models.py``)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from fakturama_i2c.models import (
    DebtorData,
    ExtractedOrder,
    ItemLine,
    OrderTotals,
    PaidStatus,
    PaymentCode,
    payment_code_for,
)


def test_valid_order_validates(sample_order) -> None:
    assert isinstance(sample_order, ExtractedOrder)
    assert sample_order.debtor.search_key == "Acme GmbH"
    assert sample_order.header.order_date.isoformat() == "2026-03-15"


def test_extra_field_rejected_extra_forbid(sample_order) -> None:
    payload = sample_order.model_dump()
    payload["header"]["bogus"] = 1
    with pytest.raises(ValidationError):
        ExtractedOrder.model_validate(payload)

    payload = sample_order.model_dump()
    payload["items"][0]["surprise"] = "x"
    with pytest.raises(ValidationError):
        ExtractedOrder.model_validate(payload)


def test_debtor_without_company_or_name_rejected(sample_order) -> None:
    payload = sample_order.model_dump()
    payload["debtor"]["company"] = ""
    payload["debtor"]["first_name"] = ""
    payload["debtor"]["last_name"] = ""
    with pytest.raises(ValidationError):
        ExtractedOrder.model_validate(payload)


def test_itemline_empty_sku_rejected() -> None:
    with pytest.raises(ValidationError):
        ItemLine(sku="", quantity=Decimal("1"), unit_net_price=Decimal("1"))


def test_ordertotals_mismatch_does_not_raise() -> None:
    totals = OrderTotals(total_net=Decimal("10"), total_vat=Decimal("0"), total_gross=Decimal("20"))
    assert totals.total_gross == Decimal("20")
    assert totals.total_net + totals.total_vat != totals.total_gross


def test_payment_code_for_mapping() -> None:
    assert payment_code_for("Bank Transfer") == PaymentCode.CREDIT_TRANSFER
    assert payment_code_for("Credit Card") == PaymentCode.CREDIT_CARD
    assert payment_code_for("SEPA Direct Debit") == PaymentCode.SEPA_DIRECT_DEBIT


def test_payment_code_for_case_insensitive() -> None:
    assert payment_code_for("bank transfer") == PaymentCode.CREDIT_TRANSFER
    assert payment_code_for("CREDIT CARD") == PaymentCode.CREDIT_CARD
    assert payment_code_for("Sepa Direct Debit") == PaymentCode.SEPA_DIRECT_DEBIT


def test_payment_code_for_unknown_returns_none() -> None:
    assert payment_code_for("Google Pay") == PaymentCode.NONE
    assert payment_code_for("") == PaymentCode.NONE
    assert payment_code_for(None) == PaymentCode.NONE


def test_search_key_prefers_company() -> None:
    debtor = DebtorData(company="Acme GmbH", first_name="John", last_name="Doe")
    assert debtor.search_key == "Acme GmbH"


def test_search_key_falls_back_to_full_name() -> None:
    debtor = DebtorData(company="", first_name="John", last_name="Doe")
    assert debtor.search_key == "John Doe"


def test_search_key_single_name() -> None:
    debtor = DebtorData(company="", first_name="John", last_name="")
    assert debtor.search_key == "John"


def test_paid_status_defaults_to_unpaid(sample_order) -> None:
    payload = sample_order.model_dump()
    payload["payment"].pop("paid_status")
    order = ExtractedOrder.model_validate(payload)
    assert order.payment.paid_status == PaidStatus.UNPAID