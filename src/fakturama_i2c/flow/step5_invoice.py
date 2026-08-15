"""Step 5 -- complete the Invoice linked to the saved Order and verify persistence.

Leaves the proposed Invoice No./date/service date untouched, applies the
extracted payment method/status/date/value (never inventing them for an unpaid
invoice), saves, and re-checks the Order + Invoice pair in Data > Documents.
No Delivery, Correction or Dunning documents are created.
"""

from __future__ import annotations

from pathlib import Path
import time
import pywinauto
import pywinauto.mouse
import pywinauto.keyboard

from ..models import PaidStatus, payment_code_for, PaymentCode
from ..ui.elements import Combo, Edit, Table, format_decimal
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


def _find_payment_combo(ctx: FlowContext):
    """Find the payment method ComboBox, first by registry, then by paid-checkbox anchor."""
    try:
        return ctx.find("INVOICE_PAYMENT_METHOD")
    except Exception:
        pass
    # Fallback: find the 'paid' CheckBox, then locate the ComboBox to its right
    try:
        paid = ctx.find("INVOICE_PAID_CHECKBOX")
        paid_rect = paid.rectangle()
        main = ctx.app.main_window()
        combos = list(main.descendants(control_type="ComboBox"))
        # Pick the ComboBox whose left edge is near the paid checkbox's right edge
        # and whose vertical position overlaps
        for c in combos:
            try:
                cr = c.rectangle()
            except Exception:
                continue
            if cr.top >= paid_rect.top - 20 and cr.top <= paid_rect.bottom + 20:
                if cr.left >= paid_rect.right - 10 and cr.left <= paid_rect.right + 200:
                    logger.info("step5: payment combo found via paid-checkbox anchor at %s", cr)
                    return c
        # Last resort: pick the lowest ComboBox in the invoice pane (payment is at bottom)
        invoice_panes = [p for p in main.descendants(control_type="Pane")
                         if "invoice" in (p.window_text() or "").lower()]
        if invoice_panes:
            all_rects = []
            for c in combos:
                try:
                    all_rects.append((c.rectangle().top, c))
                except Exception:
                    pass
            if all_rects:
                all_rects.sort(key=lambda x: x[0])
                lowest = all_rects[-1][1]
                logger.info("step5: payment combo found as lowest ComboBox at y=%d", all_rects[-1][0])
                return lowest
    except Exception as exc:
        logger.warning("step5: paid-checkbox anchor fallback failed: %s", exc)
    raise Exception("cannot locate payment method ComboBox")


def _select_payment_method(ctx: FlowContext, method: str, mapped: str) -> bool:
    """Select payment method using direct mouse+keyboard on the non-editable SWT combo.

    The payment ComboBox is a non-editable SWT dropdown with empty name.
    UIA ``type_keys`` doesn't work (combo isn't an editor), and ``items()``
    returns empty (dropdown items not exposed to UIA).

    Strategy:
      1. Find combo via _find_payment_combo → click to open dropdown.
      2. Type-ahead: press first letter of each candidate → wait → check if
         the combo's window_text() updated to contain the candidate.
      3. Arrow navigation: HOME → loop DOWN arrows → check window_text().
      4. Return True on success, False on failure.
    """
    import pywinauto as _pw

    try:
        raw = _find_payment_combo(ctx)
    except Exception as exc:
        logger.warning("step5: cannot locate payment combo: %s", exc)
        return False

    # Candidates to try, in order
    candidates = []
    if mapped and mapped != method:
        candidates.append(mapped)
    candidates.append(method)
    # Also try common Fakturama payment method names
    for extra in ("Credit transfer", "Bank Transfer", "PayPal", "Cash"):
        if extra not in candidates:
            candidates.append(extra)

    def _shown() -> str:
        try:
            return str(raw.window_text() or "")
        except Exception:
            return ""

    def _click_open() -> None:
        raw.click_input()
        time.sleep(0.5)

    # Strategy 1: type-ahead — open combo, type first char of candidate
    for cand in candidates:
        if not cand:
            continue
        try:
            _click_open()
            time.sleep(0.1)
            # Type first letter to jump to matching item
            _pw.keyboard.send_keys(cand[0])
            time.sleep(0.3)
            _pw.keyboard.send_keys("{ENTER}")
            time.sleep(0.3)
            shown = _shown()
            if cand.lower() in shown.lower() or shown.lower() in cand.lower():
                logger.info("step5: payment method selected via type-ahead: %r (shown=%r)", cand, shown)
                return True
            # Try pressing DOWN once more then ENTER
            _click_open()
            _pw.keyboard.send_keys("{DOWN}")
            time.sleep(0.15)
            _pw.keyboard.send_keys("{ENTER}")
            time.sleep(0.3)
            shown = _shown()
            if cand.lower() in shown.lower() or shown.lower() in cand.lower():
                logger.info("step5: payment method selected via type-ahead+DOWN: %r", cand)
                return True
        except Exception as exc:
            logger.debug("step5: type-ahead %r failed: %s", cand, exc)

    # Strategy 2: arrow traversal — open combo, HOME, then DOWN arrows
    for cand in candidates:
        if not cand:
            continue
        try:
            _click_open()
            _pw.keyboard.send_keys("{HOME}")
            time.sleep(0.1)
            for arrow_i in range(20):
                _pw.keyboard.send_keys("{DOWN}")
                time.sleep(0.12)
                shown = _shown()
                if cand.lower() in shown.lower() or shown.lower() in cand.lower():
                    _pw.keyboard.send_keys("{ENTER}")
                    time.sleep(0.3)
                    logger.info("step5: payment method selected via arrows (attempt %d): %r", arrow_i + 1, cand)
                    return True
            # Didn't match — close dropdown
            _pw.keyboard.send_keys("{ESC}")
            time.sleep(0.2)
        except Exception as exc:
            logger.debug("step5: arrow traversal %r failed: %s", cand, exc)
            try:
                _pw.keyboard.send_keys("{ESC}")
                time.sleep(0.1)
            except Exception:
                pass

    logger.warning("step5: all payment method strategies failed (combo shown=%r)", _shown())
    return False


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


def _set_invoice_date_positional(
    ctx: FlowContext, de_date: str, iso_date: str, us_date: str, accepted: tuple
) -> None:
    """Set payment date using positional fallback (DateTime picker not exposed to UIA).

    Layout: paid CheckBox(390,646) → ComboBox(454,642) → date Pane(569,637) → value area.
    The date picker is inside Pane rect=[569,637,857,675], center ≈ (713,656).
    """
    import pywinauto
    main = ctx.app.main_window()
    try:
        date_ctrl = ctx.find("INVOICE_PAYMENT_DATE")
        ok = False
        for candidate in (de_date, iso_date, us_date):
            if ok:
                break
            try:
                date_ctrl.set_focus()
                date_ctrl.click_input()
            except Exception:
                pass
            time.sleep(0.15)
            date_ctrl.type_keys("^a{BACKSPACE}")
            time.sleep(0.1)
            date_ctrl.type_keys(f"{candidate}{{ENTER}}", pause=0.03)
            time.sleep(0.3)
            try:
                shown = (date_ctrl.window_text() or "").replace(" ", "")
            except Exception:
                shown = ""
            ok = any(acc.replace(" ", "") in shown for acc in accepted)
            if ok:
                logger.info("step5: payment date accepted (registry) as %r", shown)
        if ok:
            return
    except Exception as exc:
        logger.debug("step5: INVOICE_PAYMENT_DATE registry failed: %s", exc)

    paid_cb = None
    try:
        paid_cb = ctx.find("INVOICE_PAID_CHECKBOX")
    except Exception:
        pass
    if paid_cb is None:
        logger.warning("step5: cannot locate paid checkbox for date positional fallback")
        return

    cb_rect = paid_cb.rectangle()
    date_x = cb_rect.right + 280
    date_y = cb_rect.top + (cb_rect.height() // 2)

    import pywinauto.mouse
    for candidate in (de_date, iso_date, us_date):
        pywinauto.mouse.double_click(coords=(date_x, date_y))
        time.sleep(0.2)
        pywinauto.keyboard.send_keys("^a")
        time.sleep(0.1)
        pywinauto.keyboard.send_keys(candidate, pause=0.03)
        time.sleep(0.15)
        pywinauto.keyboard.send_keys("{ENTER}")
        time.sleep(0.3)
        logger.info("step5: payment date typed (positional) candidate=%r", candidate)
        return
    logger.warning("step5: payment date positional fallback could not verify (all candidates tried)")


def _set_invoice_value_positional(ctx: FlowContext, gross_str: str) -> None:
    """Set payment value using positional fallback.

    Value field is to the right of the date picker, approximately x=900, y same as date.
    """
    paid_cb = None
    try:
        val_ctrl = ctx.find("INVOICE_PAYMENT_VALUE")
        val_ctrl.set_focus()
        val_ctrl.click_input()
        time.sleep(0.1)
        val_ctrl.type_keys("^a{BACKSPACE}")
        time.sleep(0.1)
        val_ctrl.type_keys(gross_str, pause=0.03)
        logger.info("step5: payment value set (registry) to %s", gross_str)
        return
    except Exception as exc:
        logger.debug("step5: INVOICE_PAYMENT_VALUE registry failed: %s", exc)

    try:
        paid_cb = ctx.find("INVOICE_PAID_CHECKBOX")
    except Exception:
        pass
    if paid_cb is None:
        logger.warning("step5: cannot locate paid checkbox for value positional fallback")
        return

    import pywinauto.mouse
    cb_rect = paid_cb.rectangle()
    val_x = cb_rect.right + 420
    val_y = cb_rect.top + (cb_rect.height() // 2)

    pywinauto.mouse.double_click(coords=(val_x, val_y))
    time.sleep(0.2)
    pywinauto.keyboard.send_keys("^a")
    time.sleep(0.1)
    pywinauto.keyboard.send_keys(gross_str, pause=0.03)
    time.sleep(0.15)
    pywinauto.keyboard.send_keys("{ENTER}")
    time.sleep(0.2)
    logger.info("step5: payment value set (positional) to %s", gross_str)


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
        if not _select_payment_method(ctx, method, mapped):
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
        iso_date = payment.payment_date.isoformat()
        us_date = payment.payment_date.strftime("%m/%d/%Y")
        accepted = (de_date, iso_date, us_date, str(payment.payment_date))
        _set_invoice_date_positional(ctx, de_date, iso_date, us_date, accepted)
        gross = format_decimal(ctx.extracted.totals.total_gross)
        _set_invoice_value_positional(ctx, gross)
        logger.info(
            "step5: invoice marked Paid (date=%s, value=%s)",
            de_date,
            gross,
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
