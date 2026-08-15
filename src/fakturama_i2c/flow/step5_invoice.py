"""Step 5 -- complete the Invoice linked to the saved Order and verify persistence.

Leaves the proposed Invoice No./date/service date untouched, applies the
extracted payment method/status/date/value (never inventing them for an unpaid
invoice), saves, and re-checks the Order + Invoice pair in Data > Documents.
No Delivery, Correction or Dunning documents are created.
"""

from __future__ import annotations

from pathlib import Path
import time

from ..models import PaidStatus, payment_code_for, PaymentCode
from ..ui.elements import Edit, Table, format_decimal
from ..utils.errors import ManualReviewError
from ..utils.logging import get_logger
from ..utils.screenshot import capture_window
from .context import (
    FlowContext,
    candidate_doc_rows,
    doc_kind,
    doc_state,
    doc_total_ok,
    select_combo_value,
    stabilize_active_editor,
)

logger = get_logger("flow.step5")
_ROOT = Path(__file__).resolve().parent.parent.parent

DOCUMENTS_DIALOG_TITLE = "Documents"


def _save_evidence(ctx: FlowContext, name: str) -> None:
    try:
        root_sc = _ROOT / "screenshots"
        root_sc.mkdir(parents=True, exist_ok=True)
        capture_window(ctx.window(), root_sc, name)
        capture_window(ctx.window(), ctx.settings.screenshot_dir, name)
    except Exception as exc:
        logger.warning("evidence capture %s failed: %s", name, exc)


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
    time.sleep(0.6)
    _save_evidence(ctx, "03_linked_paid_invoice")
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
    try:
        table = Table(ctx.find("ORDER_ITEMS_TABLE"), ctx.waits)
        for row in table.rows():
            logger.info("step5:   line: %s", " | ".join(row))
    except Exception:
        logger.warning("step5: ORDER_ITEMS_TABLE invisible to UIA — skipping line logging")


def _set_paid_status(ctx: FlowContext) -> None:
    """Mark the invoice Paid through an exposed UIA control or stop."""
    from ..ui.elements import Checkbox as _Checkbox

    try:
        select_combo_value(ctx, "INVOICE_PAID_STATUS", "Paid", "step5.invoice.paid")
        return
    except ManualReviewError:
        pass
    try:
        _Checkbox(ctx.find("INVOICE_PAID_CHECKBOX"), ctx.waits).set_checked(True)
        logger.info("step5: invoice marked Paid via INVOICE_PAID_CHECKBOX")
    except Exception as exc:
        raise ManualReviewError(
            "step5.invoice.paid", "could not set the Invoice paid status"
        ) from exc


def _set_payment_fields(ctx: FlowContext) -> None:
    payment = ctx.extracted.payment
    stabilize_active_editor(ctx, wait_control="paid", max_retries=3)
    method = (payment.payment_method or "").strip()
    if method:
        code = payment_code_for(method)
        if code is not PaymentCode.NONE:
            mapped = code.value
        else:
            mapped = method
        selected = False
        # The payment-code mapping is exact; no keyword/default selection is safe.
        for try_val in [mapped, method]:
            if selected:
                break
            try:
                select_combo_value(ctx, "INVOICE_PAYMENT_METHOD", try_val, "step5.invoice.payment")
                selected = True
            except ManualReviewError:
                continue
        if not selected:
            raise ManualReviewError(
                "step5.invoice.payment",
                f"exact payment method {method!r} / code {mapped!r} is unavailable",
            )

    if payment.paid_status == PaidStatus.PAID:
        _set_paid_status(ctx)
        if payment.payment_date is None:
            raise ManualReviewError(
                "step5.invoice.paid",
                "paid invoice has no payment date in the extraction",
            )
        de_date = payment.payment_date.strftime("%d.%m.%Y")
        try:
            date_ctrl = ctx.find("INVOICE_PAYMENT_DATE")
            try:
                date_ctrl.set_focus()
                date_ctrl.click_input()
            except Exception:
                pass
            time.sleep(0.1)
            date_ctrl.type_keys("^a{BACKSPACE}")
            time.sleep(0.1)
            date_ctrl.type_keys(f"{de_date}{{ENTER}}", pause=0.03)
        except Exception as exc:
            raise ManualReviewError("step5.invoice.paid", "could not set payment date") from exc
        try:
            val_ctrl = ctx.find("INVOICE_PAYMENT_VALUE")
            try:
                val_ctrl.set_focus()
                val_ctrl.click_input()
            except Exception:
                pass
            time.sleep(0.1)
            val_ctrl.type_keys("^a{BACKSPACE}")
            time.sleep(0.1)
            val_ctrl.type_keys(format_decimal(ctx.extracted.totals.total_gross))
        except Exception as exc:
            raise ManualReviewError("step5.invoice.paid", "could not set payment value") from exc
        logger.info(
            "step5: invoice marked Paid (date=%s, value=%s)",
            de_date,
            format_decimal(ctx.extracted.totals.total_gross),
        )
    elif payment.paid_status == PaidStatus.DEPOSIT:
        select_combo_value(ctx, "INVOICE_PAID_STATUS", "Deposit", "step5.invoice.paid")
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
    ctx.set_window(invoice_win)


def _verify_documents(ctx: FlowContext) -> None:
    ctx.menu_select("MENU_DATA", "MENU_DOCUMENTS", "Documents")
    try:
        docs_win = ctx.waits.for_window(
            ctx.app.desktop, DOCUMENTS_DIALOG_TITLE, ctx.settings.window_timeout
        )
        ctx.set_window(docs_win)
    except Exception:
        # Dialog may be a child of main window or a TabItem
        try:
            docs_win = ctx._find_child_dialog(DOCUMENTS_DIALOG_TITLE)
            ctx.set_window(docs_win)
        except Exception as exc:
            raise ManualReviewError("step5.invoice.documents", "Documents view is not available") from exc
    try:
        try:
            ctx.waits.stable_snapshot(ctx.find("DOCUMENTS_TABLE"))
            table = Table(ctx.find("DOCUMENTS_TABLE"), ctx.waits)
            rows = table.rows()
        except Exception as exc:
            raise ManualReviewError("step5.invoice.documents", "Documents table is not exposed by UIA") from exc
        ref = ctx.extracted.header.external_reference
        totals = ctx.extracted.totals
        candidates = candidate_doc_rows(
            rows, ref, ctx.extracted.header.order_date, totals.total_gross
        )
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
            raise ManualReviewError(
                "step5.invoice.documents", f"Invoice total mismatch: {invoice!r}"
            )
        if ctx.extracted.payment.paid_status == PaidStatus.PAID and doc_state(invoice) != "paid":
            raise ManualReviewError(
                "step5.invoice.documents", f"Invoice is not persisted as paid: {invoice!r}"
            )
        logger.info(
            "step5: saved Invoice confirmed (state=%r): %s",
            doc_state(invoice),
            " | ".join(invoice[:10]),
        )
        time.sleep(0.5)
        _save_evidence(ctx, "04_documents_final_state")
    finally:
        try:
            ctx.cancel_dialog()
        except Exception:
            pass
