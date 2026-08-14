"""Tests for the spec pricing formulas (see ``src/fakturama_i2c/pricing.py``)."""

from __future__ import annotations

from decimal import Decimal

from fakturama_i2c.models import ItemLine
from fakturama_i2c.pricing import (
    gross_price_from_net,
    line_gross_total,
    line_net_total,
    line_price_after_discount,
)


def test_gross_price_from_net_formula() -> None:
    assert gross_price_from_net(Decimal("10"), Decimal("19")) == Decimal("11.90")
    assert gross_price_from_net(Decimal("7.50"), Decimal("7")) == Decimal("8.03")


def test_master_price_is_never_affected_by_line_discount() -> None:
    base = ItemLine(
        sku="X-1",
        quantity=Decimal("1"),
        unit_net_price=Decimal("10"),
        vat_percent=Decimal("19"),
        discount_percent=Decimal("0"),
    )
    discounted = base.model_copy(update={"discount_percent": Decimal("50")})
    assert discounted.master_price_gross == base.master_price_gross == Decimal("11.90")
    assert base.master_price_gross == gross_price_from_net(
        base.unit_net_price, base.vat_percent
    )


def test_master_price_ignores_discount_for_all_sample_items(sample_items) -> None:
    for item in sample_items:
        assert item.master_price_gross == gross_price_from_net(
            item.unit_net_price, item.vat_percent
        )


def test_line_net_total_formula() -> None:
    assert line_net_total(Decimal("2"), Decimal("10")) == Decimal("20.00")
    assert line_net_total(Decimal("3"), Decimal("7.50")) == Decimal("22.50")


def test_line_price_after_discount_formula() -> None:
    assert line_price_after_discount(Decimal("2"), Decimal("10"), Decimal("0")) == Decimal("20.00")
    assert line_price_after_discount(Decimal("3"), Decimal("7.50"), Decimal("10")) == Decimal("20.25")
    assert line_price_after_discount(Decimal("1"), Decimal("100"), Decimal("25")) == Decimal("75.00")


def test_line_gross_total_formula() -> None:
    assert line_gross_total(Decimal("2"), Decimal("10"), Decimal("0"), Decimal("19")) == Decimal("23.80")
    assert line_gross_total(Decimal("1"), Decimal("100"), Decimal("25"), Decimal("0")) == Decimal("75.00")


def test_sample_items_computed_values(sample_items) -> None:
    assert sample_items[0].master_price_gross == Decimal("11.90")
    assert sample_items[0].line_net == Decimal("20.00")
    assert sample_items[0].line_price == Decimal("20.00")
    assert sample_items[0].line_gross == Decimal("23.80")

    assert sample_items[1].master_price_gross == Decimal("8.03")
    assert sample_items[1].line_net == Decimal("22.50")
    assert sample_items[1].line_price == Decimal("20.25")
    assert sample_items[1].line_gross == Decimal("21.67")

    assert sample_items[2].master_price_gross == Decimal("100.00")
    assert sample_items[2].line_net == Decimal("100.00")
    assert sample_items[2].line_price == Decimal("75.00")
    assert sample_items[2].line_gross == Decimal("75.00")


def test_rounding_uses_round_half_up() -> None:
    # 1.005 rounds UP to 1.01 (ROUND_HALF_UP), not DOWN to 1.00.
    assert gross_price_from_net(Decimal("1.005"), Decimal("0")) == Decimal("1.01")
    assert gross_price_from_net(Decimal("0.005"), Decimal("0")) == Decimal("0.01")
    # 5.025 rounds UP to 5.03.
    assert line_price_after_discount(Decimal("1"), Decimal("10.05"), Decimal("50")) == Decimal("5.03")
    assert line_gross_total(Decimal("1"), Decimal("10.05"), Decimal("50"), Decimal("0")) == Decimal("5.03")