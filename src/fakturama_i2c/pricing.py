"""Pricing rules from the assignment spec.

The spec distinguishes THREE prices on purpose:

1. Product master price (gross)  = unit_net_price * (1 + VAT/100)
   -> stored on the Product master record. Round to 2 decimals.
   -> the line discount is NEVER applied to the master price.

2. Order line net total            = quantity * unit_net_price

3. Order line price (after discount) = quantity * unit_net_price
       * (1 - discount/100)
   -> what the user sees on the Order line.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

TWOPLACES = Decimal("0.01")
HUNDRED = Decimal("100")


def _round(value: Decimal) -> Decimal:
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def gross_price_from_net(unit_net_price: Decimal, vat_percent: Decimal) -> Decimal:
    """Master price: net x (1 + VAT%). No discount applied."""
    return _round(unit_net_price * (Decimal(1) + vat_percent / HUNDRED))


def line_net_total(quantity: Decimal, unit_net_price: Decimal) -> Decimal:
    """qty x unit net price, before discount."""
    return _round(quantity * unit_net_price)


def line_price_after_discount(
    quantity: Decimal, unit_net_price: Decimal, discount_percent: Decimal
) -> Decimal:
    """qty x unit net x (1 - discount/100)."""
    net = line_net_total(quantity, unit_net_price)
    return _round(net * (Decimal(1) - discount_percent / HUNDRED))


def line_gross_total(
    quantity: Decimal,
    unit_net_price: Decimal,
    discount_percent: Decimal,
    vat_percent: Decimal,
) -> Decimal:
    """line price x (1 + VAT%)."""
    price = line_price_after_discount(quantity, unit_net_price, discount_percent)
    return _round(price * (Decimal(1) + vat_percent / HUNDRED))
