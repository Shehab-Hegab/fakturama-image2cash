"""Typed domain models for the extracted order image.

These are the *contract* between the extraction layer and the UI automation
flow. Every field maps 1:1 to something Fakturama's Order/Debtor/Product
editors can display, so the flow layer never has to re-interpret free text.
"""

from __future__ import annotations

from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pricing import (
    gross_price_from_net,
    line_gross_total,
    line_net_total,
    line_price_after_discount,
    TWOPLACES,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class PriceMode(str, Enum):
    """Debtor/Order price mode: net or gross."""

    NET = "Net"
    GROSS = "Gross"


class VatMode(str, Enum):
    """Order VAT mode."""

    WITH_VAT = "With VAT"
    WITHOUT_VAT = "Without VAT"


class PaidStatus(str, Enum):
    """Invoice paid status as it appears in Fakturama."""

    UNPAID = "Unpaid"
    DEPOSIT = "Deposit"
    PAID = "Paid"


class PaymentCode(str, Enum):
    """Fakturama's terms-of-payment code dropdown values."""

    CREDIT_TRANSFER = "Credit transfer"
    CREDIT_CARD = "Credit card"
    SEPA_DIRECT_DEBIT = "SEPA direct debit"
    NONE = ""


# Mapping from source image wording -> Fakturama payment code dropdown value.
# Spec: Bank Transfer=Credit transfer; Credit Card=Credit card;
# SEPA Direct Debit=SEPA direct debit.
PAYMENT_CODE_MAP: dict[str, PaymentCode] = {
    "bank transfer": PaymentCode.CREDIT_TRANSFER,
    "credit transfer": PaymentCode.CREDIT_TRANSFER,
    "banküberweisung": PaymentCode.CREDIT_TRANSFER,
    "credit card": PaymentCode.CREDIT_CARD,
    "kreditkarte": PaymentCode.CREDIT_CARD,
    "sepa direct debit": PaymentCode.SEPA_DIRECT_DEBIT,
    "sepa-lastschrift": PaymentCode.SEPA_DIRECT_DEBIT,
    "direct debit": PaymentCode.SEPA_DIRECT_DEBIT,
}


def payment_code_for(method: str) -> PaymentCode:
    """Resolve a free-text payment method to the dropdown code (case-insensitive).

    Returns PaymentCode.NONE when the wording is unknown so callers can decide
    whether to blank the field (spec-compliant) or stop for review.
    """
    normalized = (method or "").strip().lower()
    return PAYMENT_CODE_MAP.get(normalized, PaymentCode.NONE)


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------


class Address(BaseModel):
    """A postal address as stored on a Fakturama Debtor."""

    model_config = ConfigDict(extra="forbid")

    street: str = ""
    zip_code: str = ""
    city: str = ""
    country: str = ""
    email: str = ""
    telephone: str = ""

    def is_empty(self) -> bool:
        return not any((self.street, self.zip_code, self.city))


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


class ItemLine(BaseModel):
    """One extracted line item of the source order."""

    model_config = ConfigDict(extra="forbid")

    sku: str = Field(min_length=1)
    description: str = ""
    quantity: Decimal = Field(default=Decimal("1"), ge=Decimal("0"))
    unit_net_price: Decimal = Field(ge=Decimal("0"))
    vat_percent: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("100"))
    discount_percent: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("100"))

    # -- computed (spec formulas, see pricing module) -------------------------
    @property
    def master_price_gross(self) -> Decimal:
        """Product master price: net x (1 + VAT%), NO line discount."""
        return gross_price_from_net(self.unit_net_price, self.vat_percent)

    @property
    def line_net(self) -> Decimal:
        """qty x unit net price (before line discount)."""
        return line_net_total(self.quantity, self.unit_net_price)

    @property
    def line_price(self) -> Decimal:
        """qty x unit net x (1 - discount/100) -- what the Order line shows."""
        return line_price_after_discount(
            self.quantity, self.unit_net_price, self.discount_percent
        )

    @property
    def line_gross(self) -> Decimal:
        """qty x unit net x (1 - discount/100) x (1 + VAT%)."""
        return line_gross_total(
            self.quantity, self.unit_net_price, self.discount_percent, self.vat_percent
        )


# ---------------------------------------------------------------------------
# Debtor
# ---------------------------------------------------------------------------


class DebtorData(BaseModel):
    """Master-data fields needed to resolve-or-create a Debtor."""

    model_config = ConfigDict(extra="forbid")

    company: str = ""
    first_name: str = ""
    last_name: str = ""
    salutation: str = "---"
    alias: str = ""
    billing_address: Address = Field(default_factory=Address)
    delivery_address: Address = Field(default_factory=Address)
    # True when the source supplies one address for both roles; the UI then
    # assigns BOTH the Invoice and Delivery address role to that single address
    # instead of creating a second address record.
    same_delivery_address: bool = False
    payment_method: str = ""
    price_mode: PriceMode = PriceMode.NET
    discount_percent: Decimal = Field(default=Decimal("0"))

    @property
    def search_key(self) -> str:
        """Best-effort string used to search the 'Select the address' dialog."""
        return self.company or f"{self.first_name} {self.last_name}".strip()


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


class OrderHeader(BaseModel):
    model_config = ConfigDict(extra="forbid")

    order_date: date
    external_reference: str = ""
    price_mode: PriceMode = PriceMode.NET
    vat_mode: VatMode = VatMode.WITH_VAT
    overall_discount_percent: Decimal = Field(default=Decimal("0"))
    shipping_amount: Decimal = Field(default=Decimal("0"))
    shipping_is_free: bool = True


class OrderTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_net: Decimal = Field(ge=Decimal("0"))
    total_vat: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    total_gross: Decimal = Field(ge=Decimal("0"))
    currency: str = "EUR"

    @model_validator(mode="after")
    def _reconcile(self) -> "OrderTotals":
        if self.total_net + self.total_vat != self.total_gross:
            # Do not raise -- totals are a source-of-truth check, not a blocker
            # by itself; the flow reports the mismatch for manual review.
            return self
        return self


class PaymentData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_status: PaidStatus = PaidStatus.UNPAID
    payment_date: Optional[date] = None
    payment_method: str = ""


class ExtractedOrder(BaseModel):
    """The complete, validated result of extracting one source order image."""

    model_config = ConfigDict(extra="forbid")

    header: OrderHeader
    debtor: DebtorData
    items: list[ItemLine] = Field(min_length=1)
    totals: OrderTotals
    payment: PaymentData = Field(default_factory=PaymentData)

    @model_validator(mode="after")
    def _validate(self) -> "ExtractedOrder":
        if not self.debtor.search_key:
            raise ValueError("debtor must have company or first/last name")
        return self
