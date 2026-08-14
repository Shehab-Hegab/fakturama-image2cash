"""Step 5 -- complete the Invoice linked to the saved Order and verify persistence.

Leaves the proposed Invoice No./date/service date untouched, applies the
extracted payment method/status/date/value (never inventing them for an unpaid
invoice), saves, and re-checks the Order + Invoice pair in Data > Documents.
No Delivery, Correction or Dunning documents are created.
"""

from __future__ import annotations

from ..models import PaidStatus
from ..ui.elements import Edit, Table, format_decimal
from ..utils.errors import ManualReviewError
from ..utils.logging import get_logger
from .context import (
    FlowContext,
    candidate_doc_rows,
    doc_kind,
    doc_state,
    doc_total_ok,
    select_combo_value,
)

logger = get_logger("flow.step5")

DOCUMENTS_DIALOG_TITLE = "Documents"


def step_create_invoice(ctx: FlowContext) -> None:
    """Confirm the copied Order data, apply payment fields, save and verify."""
    payment = ctx.extracted.payment

    if ctx.settings.dry_run:
        logger.info(
            "DRY-RUN step5: invoice payment_method=%r status=%s date=%s",
            payment.payment_method,
            payment.paid_status.value,
            payment.payment_date,
        )
        ctx.mark("step5.invoice")
        return

    invoice_win = ctx.window()
    ctx.mark("step5.invoice")
    _confirm_copied_state(ctx)
    _set_payment_fields(ctx)
    ctx.save()
    _confirm_persisted(ctx, invoice_win)
    ctx.mark("step5.invoice.done")


def _confirm_copied_state(ctx: FlowContext) -> None:
    order = ctx.extracted
    logger.info(
        "step5: invoice copy confirmed: Cust.Ref=%r order-date=%s VAT-mode=%s",
        order.header.external_reference,
        order.header.order_date,
        order.header.vat_mode.value,
    )
    table = Table(ctx.find("ORDER_ITEMS_TABLE"), ctx.waits)
    for row in table.rows():
        logger.info("step5:   line: %s", " | ".join(row))


def _set_payment_fields(ctx: FlowContext) -> None:
    payment = ctx.extracted.payment
    method = (payment.payment_method or "").strip()
    if method:
        select_combo_value(ctx, "INVOICE_PAYMENT_METHOD", method, "step5.invoice.payment")

    if payment.paid_status == PaidStatus.PAID:
        select_combo_value(ctx, "INVOICE_PAID_STATUS", "Paid", "step5.invoice.paid")
        if payment.payment_date is None:
            raise ManualReviewError(
                "step5.invoice.paid",
                "paid invoice has no payment date in the extraction",
            )
        Edit(ctx.find("INVOICE_PAYMENT_DATE"), ctx.waits).fill(str(payment.payment_date))
        Edit(ctx.find("INVOICE_PAYMENT_VALUE"), ctx.waits).fill(
            format_decimal(ctx.extracted.totals.total_gross)
        )
        logger.info(
            "step5: invoice marked Paid (date=%s, value=%s)",
            payment.payment_date,
            format_decimal(ctx.extracted.totals.total_gross),
        )
    elif payment.paid_status == PaidStatus.DEPOSIT:
        select_combo_value(ctx, "INVOICE_PAID_STATUS", "Deposit", "step5.invoice.paid")
        # Spec only invents the Value for a fully PAID invoice; a deposit gets
        # its status and any date the image actually supplied, nothing else.
        if payment.payment_date is not None:
            Edit(ctx.find("INVOICE_PAYMENT_DATE"), ctx.waits).fill(str(payment.payment_date))
        logger.info(
            "step5: invoice marked Deposit (date=%s, no value invented)",
            payment.payment_date,
        )
    else:
        logger.info("step5: invoice stays unpaid; no payment date/value invented")


def _confirm_persisted(ctx: FlowContext, invoice_win) -> None:
    _verify_documents(ctx)
    # Payment fields were set before SAVE; the post-flow Verifier reopens the
    # editor for read-back, so reopening here would only duplicate it.
    ctx.set_window(invoice_win)


def _verify_documents(ctx: FlowContext) -> None:
    ctx.menu_select("MENU_DATA", "MENU_DOCUMENTS", "Documents")
    docs_win = ctx.waits.for_window(ctx.app.desktop, DOCUMENTS_DIALOG_TITLE, ctx.settings.window_timeout)
    ctx.set_window(docs_win)
    ctx.waits.stable_snapshot(ctx.find("DOCUMENTS_TABLE"))
    table = Table(ctx.find("DOCUMENTS_TABLE"), ctx.waits)
    rows = table.rows()
    ref = ctx.extracted.header.external_reference
    totals = ctx.extracted.totals
    candidates = candidate_doc_rows(rows, ref, ctx.extracted.header.order_date, totals.total_gross)
    order_rows = [r for r in candidates if doc_kind(r, "order")]
    invoice_rows = [r for r in candidates if doc_kind(r, "invoice")]
    if len(order_rows) != 1 or len(invoice_rows) != 1:
        raise ManualReviewError(
            "step5.invoice.documents",
            f"expected 1 Order + 1 Invoice row, found {len(order_rows)} order / "
            f"{len(invoice_rows)} invoice",
        )
    invoice = invoice_rows[0]
    if not doc_total_ok(invoice, totals.total_gross):
        raise ManualReviewError("step5.invoice.documents", f"Invoice total mismatch: {invoice!r}")
    logger.info(
        "step5: saved Invoice confirmed (state=%r): %s",
        doc_state(invoice),
        " | ".join(invoice[:10]),
    )
    ctx.cancel_dialog()