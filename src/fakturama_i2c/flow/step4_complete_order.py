"""Step 4 -- finish the Order: confirm, save, verify, and open the linked Invoice.

Confirms every product line and the extracted totals (raising for review on any
mismatch), saves the Order, verifies it in Data > Documents, then creates the
follow-up Invoice from the saved Order's "Create a follow-up document" area so
the Order-Invoice relationship is preserved.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import OrderTotals
from ..ui.elements import Button, Combo, Edit, Table, format_decimal
from ..utils.errors import ManualReviewError
from ..utils.logging import get_logger
from .context import (
    FlowContext,
    candidate_doc_rows,
    doc_kind,
    doc_state_ok,
    doc_total_ok,
    read_decimal,
    read_text,
)

logger = get_logger("flow.step4")

DOCUMENTS_DIALOG_TITLE = "Documents"
INVOICE_EDITOR_TITLE = "Invoice"


def step_complete_order(ctx: FlowContext) -> None:
    """Save the Order and open the linked Invoice editor."""
    if ctx.settings.dry_run:
        logger.info(
            "DRY-RUN step4: complete order net=%s vat=%s gross=%s shipping_free=%s",
            format_decimal(ctx.extracted.totals.total_net),
            format_decimal(ctx.extracted.totals.total_vat),
            format_decimal(ctx.extracted.totals.total_gross),
            ctx.extracted.header.shipping_is_free,
        )
        ctx.mark("step4.complete_order")
        return

    order_win = ctx.window()
    ctx.mark("step4.complete_order")
    _log_order_state(ctx)
    _set_order_inputs(ctx)
    _confirm_totals(ctx)
    ctx.save()
    _open_invoice_from_documents(ctx, order_win)
    ctx.mark("step4.invoice_open")


def _log_order_state(ctx: FlowContext) -> None:
    table = Table(ctx.find("ORDER_ITEMS_TABLE"), ctx.waits)
    rows = table.rows()
    logger.info("step4: items table has %d line(s)", len(rows))
    for row in rows:
        logger.info("step4:   line: %s", " | ".join(row))
    try:
        logger.info("step4: debtor section: %s", read_text(ctx.find("DEBTOR_ADDRESS_SECTION")))
    except Exception:
        pass


def _set_order_inputs(ctx: FlowContext) -> None:
    # Spec 4.2: Overall Discount = 0% UNLESS the image supplies a value.
    discount = ctx.extracted.header.overall_discount_percent or Decimal("0")
    if discount:
        Edit(ctx.find("ORDER_DISCOUNT"), ctx.waits).fill(format_decimal(discount))
        logger.info("step4: overall discount = %s (from image)", format_decimal(discount))
    else:
        Edit(ctx.find("ORDER_DISCOUNT"), ctx.waits).fill("0")
    _set_shipping(ctx)


def _set_shipping(ctx: FlowContext) -> None:
    header = ctx.extracted.header
    ctrl = ctx.find("ORDER_SHIPPING")
    if header.shipping_is_free:
        try:
            Combo(ctrl, ctx.waits).select("Free of shipping costs")
            logger.info("step4: shipping = Free of shipping costs")
        except Exception:
            Edit(ctrl, ctx.waits).fill("0.00")
            logger.info("step4: shipping = 0.00")
    else:
        Edit(ctrl, ctx.waits).fill(format_decimal(header.shipping_amount))
        logger.info("step4: shipping = %s", format_decimal(header.shipping_amount))


def _confirm_totals(ctx: FlowContext) -> None:
    totals: OrderTotals = ctx.extracted.totals
    for role, expected in (
        ("ORDER_TOTAL_NET", totals.total_net),
        ("ORDER_TOTAL_VAT", totals.total_vat),
        ("ORDER_TOTAL", totals.total_gross),
    ):
        actual = read_decimal(ctx, role)
        if format_decimal(actual) != format_decimal(expected):
            logger.warning("step4: %s shows %s, expected %s", role, actual, expected)
            raise ManualReviewError("step4.totals", f"{role} shows {actual}, expected {expected}")
        logger.info("step4: %s confirmed %s", role, format_decimal(expected))


def _open_invoice_from_documents(ctx: FlowContext, order_win) -> None:
    _verify_documents(ctx, order_win)
    ctx.cancel_dialog()
    ctx.set_window(order_win)
    Button(ctx.find("FOLLOW_UP_INVOICE"), ctx.waits).click()
    ctx.wait_for_editor(INVOICE_EDITOR_TITLE, "INVOICE_PAYMENT_METHOD")
    logger.info("step4: linked Invoice editor open")


def _verify_documents(ctx: FlowContext, order_win) -> None:
    ctx.menu_select("MENU_DATA", "MENU_DOCUMENTS", "Documents")
    docs_win = ctx.waits.for_window(ctx.app.desktop, DOCUMENTS_DIALOG_TITLE, ctx.settings.window_timeout)
    ctx.set_window(docs_win)
    ctx.waits.stable_snapshot(ctx.find("DOCUMENTS_TABLE"))
    table = Table(ctx.find("DOCUMENTS_TABLE"), ctx.waits)
    rows = table.rows()
    ref = ctx.extracted.header.external_reference
    totals: OrderTotals = ctx.extracted.totals
    candidates = candidate_doc_rows(rows, ref, ctx.extracted.header.order_date, totals.total_gross)
    order_rows = [
        r for r in candidates
        if doc_kind(r, "order") and doc_total_ok(r, totals.total_gross) and doc_state_ok(r, "open")
    ]
    if len(order_rows) != 1:
        raise ManualReviewError(
            "step4.documents",
            f"expected exactly 1 saved open Order row, found {len(order_rows)}",
        )
    logger.info("step4: saved Order confirmed: %s", " | ".join(order_rows[0][:10]))