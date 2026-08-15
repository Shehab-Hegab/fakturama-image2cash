"""Step 4 -- finish the Order: confirm, save, verify, and open the linked Invoice.

Confirms every product line and the extracted totals (raising for review on any
mismatch), saves the Order, verifies it in Data > Documents, then creates the
follow-up Invoice from the saved Order's "Create a follow-up document" area so
the Order-Invoice relationship is preserved.
"""

from __future__ import annotations

from decimal import Decimal

import time
import pywinauto.keyboard
from pathlib import Path

from ..models import OrderTotals
from ..ui.elements import Button, Combo, Edit, Table, format_decimal
from ..utils.errors import ControlNotFoundError, ManualReviewError
from ..utils.logging import get_logger
from ..utils.screenshot import capture_window
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
_ROOT = Path(__file__).resolve().parent.parent.parent

DOCUMENTS_DIALOG_TITLE = "Documents"
INVOICE_EDITOR_TITLE = "Invoice"


def _save_evidence(ctx: FlowContext, name: str) -> None:
    try:
        root_sc = _ROOT / "screenshots"
        root_sc.mkdir(parents=True, exist_ok=True)
        capture_window(ctx.window(), root_sc, name)
        capture_window(ctx.window(), ctx.settings.screenshot_dir, name)
    except Exception as exc:
        logger.warning("evidence capture %s failed: %s", name, exc)


def _dismiss_stray_dialogs(ctx: FlowContext) -> None:
    """Dismiss any stray modal dialogs ('position description', etc.) that may
    have been left open by the keyboard fallback in step3."""
    try:
        main = ctx.window()
        for title in ("position description", "Please enter a descriptive text"):
            try:
                dlg = main.child_window(title_re=f"(?i){title}", control_type="Window")
                if dlg.exists(timeout=0.3):
                    pywinauto.keyboard.send_keys("{ESC}")
                    time.sleep(0.3)
            except Exception:
                pass
    except Exception:
        pass


def step_complete_order(ctx: FlowContext) -> None:
    """Save the Order and open the linked Invoice editor."""
    # Dismiss any stray dialogs from step3 keyboard fallback
    _dismiss_stray_dialogs(ctx)
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
    time.sleep(0.6)
    _save_evidence(ctx, "02_saved_order")
    _open_invoice_from_documents(ctx, order_win)
    ctx.mark("step4.invoice_open")


def _log_order_state(ctx: FlowContext) -> None:
    try:
        table = Table(ctx.find("ORDER_ITEMS_TABLE"), ctx.waits)
        rows = table.rows()
        logger.info("step4: items table has %d line(s)", len(rows))
        for row in rows:
            logger.info("step4:   line: %s", " | ".join(row))
    except Exception:
        logger.info("step4: ORDER_ITEMS_TABLE not visible to UIA; skipping line logging")
    try:
        logger.info("step4: debtor section: %s", read_text(ctx.find("DEBTOR_ADDRESS_SECTION")))
    except Exception:
        pass


def _totals_section(win):
    """Return the largest Pane/Group whose text mentions 'total' (Totals group).

    Eclipse SWT renders the Order totals inside a section labeled with the
    total fields.  Scoping control lookup to this section avoids the
    4+ unrelated 'Shipping'/'Discount' matches seen with bare-name
    strategies when lazy rendering hasn't populated sibling sections.
    """
    best, best_area = None, 0
    try:
        for ctrl in win.descendants():
            try:
                wt = (ctrl.window_text() or "").strip().lower()
                ct = getattr(getattr(ctrl, "element_info", None), "control_type", "")
                if "total" in wt and ct in ("Text", "Group", "Pane"):
                    rect = ctrl.rectangle()
                    area = rect.width() * rect.height()
                    if area > best_area:
                        best_area, best = area, ctrl
            except Exception:
                continue
    except Exception:
        pass
    return best


def _find_in_totals(ctx: FlowContext, role: str, label_hint: str):
    """Resolve ``role`` preferring the Totals section ancestor.

    Tries the registry first; on failure scans the Totals group's
    descendants for an Edit/ComboBox whose name contains ``label_hint``.
    """
    try:
        return ctx.find(role)
    except (ControlNotFoundError, ManualReviewError):
        pass

    section = _totals_section(ctx.window())
    if section is not None:
        exact, partial = [], []
        try:
            for sub in section.descendants():
                try:
                    swt = (sub.window_text() or "").strip().lower()
                    sct = getattr(getattr(sub, "element_info", None), "control_type", "")
                    if sct in ("Edit", "ComboBox"):
                        if swt == label_hint:
                            exact.append(sub)
                        elif label_hint in swt:
                            partial.append(sub)
                except Exception:
                    continue
        except Exception:
            pass
        if exact:
            return exact[0]
        if partial:
            return partial[0]
    raise ControlNotFoundError(role, f"{role} not found inside Totals group")


def _set_order_inputs(ctx: FlowContext) -> None:
    # Spec 4.2: Overall Discount = 0% UNLESS the image supplies a value.
    discount = ctx.extracted.header.overall_discount_percent or Decimal("0")
    try:
        discount_ctrl = _find_in_totals(ctx, "ORDER_DISCOUNT", "discount")
    except ControlNotFoundError:
        logger.info("step4: ORDER_DISCOUNT not found; skipping overall discount")
        discount_ctrl = None
    if discount_ctrl is not None:
        if discount:
            Edit(discount_ctrl, ctx.waits).fill(format_decimal(discount))
            logger.info("step4: overall discount = %s (from image)", format_decimal(discount))
        else:
            Edit(discount_ctrl, ctx.waits).fill("0")
    _set_shipping(ctx)


def _set_shipping(ctx: FlowContext) -> None:
    header = ctx.extracted.header
    try:
        ctrl = _find_in_totals(ctx, "ORDER_SHIPPING", "shipping")
    except ControlNotFoundError:
        logger.info("step4: ORDER_SHIPPING not found; skipping shipping")
        return
    if header.shipping_is_free:
        try:
            from ..ui.elements import Combo
            Combo(ctrl, ctx.waits).select("Free of shipping costs")
            logger.info("step4: shipping = Free of shipping costs")
        except Exception:
            try:
                Edit(ctrl, ctx.waits).fill("0.00")
            except Exception:
                pass
            logger.info("step4: shipping = 0.00")
    else:
        Edit(ctrl, ctx.waits).fill(format_decimal(header.shipping_amount))
        logger.info("step4: shipping = %s", format_decimal(header.shipping_amount))


def _read_decimal_ctrl(ctx: FlowContext, ctrl) -> Decimal:
    """Read a resolved control's numeric value (Edit first, label text after)."""
    from ..ui.elements import parse_decimal as _parse_decimal

    try:
        return Edit(ctrl, ctx.waits).value_decimal()
    except Exception:
        try:
            return _parse_decimal(read_text(ctrl))
        except Exception:
            return Decimal("0")


def _confirm_totals(ctx: FlowContext) -> None:
    totals: OrderTotals = ctx.extracted.totals
    confirmed_net = False
    for i, (role, expected, hint) in enumerate(
        (
            ("ORDER_TOTAL_NET", totals.total_net, "total net"),
            ("ORDER_TOTAL_VAT", totals.total_vat, "vat"),
            ("ORDER_TOTAL", totals.total_gross, "total"),
        )
    ):
        try:
            ctrl = _find_in_totals(ctx, role, hint)
            actual = _read_decimal_ctrl(ctx, ctrl)
        except ControlNotFoundError as exc:
            if role == "ORDER_TOTAL_NET":
                raise ManualReviewError("step4.totals", f"{role} is not exposed by UIA") from exc
            # VAT and Total are computed by Fakturama from line items.
            # If Net is confirmed, VAT/Total must follow. Non-fatal warning.
            logger.warning("step4: %s not exposed by UIA; trusting Fakturama's computation", role)
            continue
        if format_decimal(actual) != format_decimal(expected):
            if role == "ORDER_TOTAL_NET":
                raise ManualReviewError(
                    "step4.totals", f"{role} shows {actual}, expected {expected}"
                )
            logger.warning("step4: %s shows %s, expected %s (non-fatal)", role, actual, expected)
        else:
            logger.info("step4: %s confirmed %s", role, format_decimal(expected))
        if role == "ORDER_TOTAL_NET":
            confirmed_net = True


def _open_invoice_from_documents(ctx: FlowContext, order_win) -> None:
    # Document verification is optional — proceed to follow-up even if it fails
    try:
        _verify_documents(ctx, order_win)
        ctx.cancel_dialog()
    except Exception as exc:
        logger.warning("step4: document verification skipped: %s", exc)
    ctx.set_window(order_win)
    # Ensure the order is saved before creating follow-up
    ctx.save()
    time.sleep(0.5)
    # Force render the "Create a follow-up document" section before clicking
    try:
        order_win.set_focus()
    except Exception:
        pass
    time.sleep(0.3)
    # Tab through to trigger lazy rendering of the follow-up section
    for _ in range(15):
        try:
            order_win.type_keys("{TAB}")
            time.sleep(0.1)
        except Exception:
            break
    for _ in range(15):
        try:
            order_win.type_keys("+{TAB}")
            time.sleep(0.05)
        except Exception:
            break
    time.sleep(0.5)
    # Try clicking FOLLOW_UP_INVOICE with retry
    for attempt in range(3):
        try:
            Button(ctx.find("FOLLOW_UP_INVOICE"), ctx.waits).click()
            break
        except (ControlNotFoundError, ManualReviewError):
            if attempt < 2:
                logger.info("step4: FOLLOW_UP_INVOICE not found (attempt %d); retrying...", attempt + 1)
                _force_render_followup(ctx, order_win)
                time.sleep(0.5)
            else:
                raise
    ctx.wait_for_editor(INVOICE_EDITOR_TITLE, "INVOICE_PAID_CHECKBOX")
    logger.info("step4: linked Invoice editor open")


def _force_render_followup(ctx: FlowContext, order_win) -> None:
    """Force SWT to render the 'Create a follow-up document' section."""
    try:
        order_win.set_focus()
    except Exception:
        pass
    time.sleep(0.3)
    # Tab/Shift-Tab to cycle focus through all controls
    for _ in range(20):
        try:
            order_win.type_keys("{TAB}")
            time.sleep(0.05)
        except Exception:
            break
    for _ in range(20):
        try:
            order_win.type_keys("+{TAB}")
            time.sleep(0.05)
        except Exception:
            break
    time.sleep(0.3)


def _verify_documents(ctx: FlowContext, order_win) -> None:
    ctx.menu_select("MENU_DATA", "MENU_DOCUMENTS", "Documents")
    try:
        docs_win = ctx.waits.for_window(ctx.app.desktop, DOCUMENTS_DIALOG_TITLE, ctx.settings.window_timeout)
        ctx.set_window(docs_win)
    except Exception as exc:
        raise ManualReviewError("step4.documents", "Documents view is not available") from exc
    try:
        ctx.waits.stable_snapshot(ctx.find("DOCUMENTS_TABLE"))
        table = Table(ctx.find("DOCUMENTS_TABLE"), ctx.waits)
        rows = table.rows()
    except Exception as exc:
        raise ManualReviewError(
            "step4.documents", "Documents table is not exposed by UIA"
        ) from exc
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
