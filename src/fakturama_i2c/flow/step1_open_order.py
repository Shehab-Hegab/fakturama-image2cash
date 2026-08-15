"""Step 1 -- open a New Order and fill the header from the extraction.

Keeps the New Order editor open: it is the persistent anchor every later step
returns to (the "still-open Order" from the design doc).
"""

from __future__ import annotations

import time

from ..models import VatMode
from ..ui.elements import Button, Combo, Edit
from ..utils.errors import ControlNotFoundError, ManualReviewError
from ..utils.logging import get_logger
from .context import FlowContext, read_text

logger = get_logger("flow.step1")

ORDER_EDITOR_TITLE = "Order"


def step_open_new_order(ctx: FlowContext) -> None:
    """Open a New Order, set date/ref/price-mode/VAT-mode, and keep it open."""
    header = ctx.extracted.header

    if ctx.settings.dry_run:
        logger.info(
            "DRY-RUN step1: New Order, date=%s ref=%r mode=%s vat=%s",
            header.order_date,
            header.external_reference,
            header.price_mode.value,
            header.vat_mode.value,
        )
        ctx.mark("step1.open_order")
        return

    _close_stale_tabs(ctx)
    Button(ctx.find("ORDER_NEW_BUTTON"), ctx.waits).click()
    time.sleep(1.0)  # Wait for tab to fully create + render
    # Fakturama opens the New Order editor as a tab inside the main window,
    # not a top-level window -- wait_for_editor handles both cases.
    editor = ctx.wait_for_editor(ORDER_EDITOR_TITLE, "ORDER_DATE")
    ctx.set_window(editor)

    # Force SWT to render the full header: focus the pane + Tab through fields.
    _force_render_header(ctx)

    # Proposed No. is left unchanged (Fakturama assigns the number on save).
    # Fill with progressive retry: SWT lazy rendering may delay Cust.Ref.
    for attempt in range(3):
        try:
            date_ctrl = ctx.find("ORDER_DATE")
            de_date = header.order_date.strftime("%d.%m.%Y")        # "18.03.2026"
            try:
                date_ctrl.set_focus()
            except Exception:
                pass
            try:
                date_ctrl.click_input()
            except Exception:
                pass
            time.sleep(0.1)
            date_ctrl.type_keys("^a{BACKSPACE}")
            time.sleep(0.1)
            date_ctrl.type_keys(f"{de_date}{{ENTER}}", pause=0.03)
            time.sleep(0.2)

            ref_ctrl = ctx.find("ORDER_REFERENCE")
            try:
                ref_ctrl.set_focus()
            except Exception:
                pass
            try:
                ref_ctrl.click_input()
            except Exception:
                pass
            time.sleep(0.1)
            ref_ctrl.type_keys("^a{BACKSPACE}")
            time.sleep(0.1)
            ref_ctrl.type_keys(header.external_reference, with_spaces=True)
            time.sleep(0.2)
            break
        except (ControlNotFoundError, ManualReviewError):
            if attempt < 2:
                logger.info("step1: header fields not yet rendered (attempt %d); forcing render...", attempt + 1)
                _force_render_header(ctx)
                time.sleep(0.5)
            else:
                raise

    Combo(ctx.find("ORDER_PRICE_MODE"), ctx.waits).select(header.price_mode.value)
    _set_vat_mode(ctx, header.vat_mode.value)

    logger.info(
        "step1: New Order open (date=%s, ref=%r, %s, %s)",
        header.order_date,
        header.external_reference,
        header.price_mode.value,
        header.vat_mode.value,
    )
    ctx.mark("step1.open_order")


def _set_vat_mode(ctx: FlowContext, expected: str) -> None:
    """Set the VAT-mode combo, tolerating SWT lazy rendering.

    Eclipse SWT renders some editor controls only after the editor gains
    focus. We try once, force-render with a Tab keypress, then retry. If the
    control still is not exposed we trust Fakturama's default ("With VAT")
    only when that matches the extraction -- otherwise we stop for review.
    """
    try:
        _select_vat_mode(ctx, expected)
        return
    except ControlNotFoundError:
        logger.info("step1: ORDER_VAT_MODE not yet exposed; forcing render (Tab)...")
        try:
            ctx.window().set_focus()
        except Exception:  # noqa: BLE001 - best-effort focus for render
            pass
        try:
            Combo(ctx.find("ORDER_PRICE_MODE"), ctx.waits).ctrl.type_keys("{TAB}")
        except Exception:  # noqa: BLE001 - Tab is best-effort
            pass
        try:
            _select_vat_mode(ctx, expected)
            return
        except ControlNotFoundError as exc:
            raise ManualReviewError(
                "step1.open_order.vat",
                f"VAT-mode combo not exposed by SWT; cannot prove expected {expected!r}",
            ) from exc


def _select_vat_mode(ctx: FlowContext, expected: str) -> None:
    Combo(ctx.find("ORDER_VAT_MODE"), ctx.waits).select(expected)


def _force_render_header(ctx: FlowContext) -> None:
    """Force SWT to render the full header (Cust.Ref, VAT mode, Addresses, etc).

    Eclipse SWT lazily renders form sections until the editor gains focus and
    the user interacts with it. Click into the first editable field + Tab
    through the header to trigger lazy render of all header controls.
    """
    try:
        ctx.window().set_focus()
    except Exception:
        pass
    time.sleep(0.2)
    # Click into the Date field directly (the first named editable field)
    try:
        date_edit = Edit(ctx.find("ORDER_DATE"), ctx.waits).ctrl
        date_edit.click_input()  # Click directly into the Date field
        time.sleep(0.2)
    except Exception:
        pass
    # Tab through header fields aggressively to trigger lazy render.
    # Order: Date -> Price Mode -> Cust.Ref -> Consultant -> VAT mode ->
    # Addresses section -> Items section.
    for i in range(12):
        try:
            ctx.window().type_keys("{TAB}")
            time.sleep(0.2)
        except Exception:
            break
    # Tab back to Date field
    for i in range(12):
        try:
            ctx.window().type_keys("+{TAB}")  # Shift+Tab
            time.sleep(0.1)
        except Exception:
            break
    time.sleep(0.5)  # Extra settle time for SWT rendering


def _close_stale_tabs(ctx: FlowContext) -> None:
    """Close any existing stale editor tabs before opening a new Order.

    Fakturama opens editors as tabs inside the main window.  Repeated CLI
    runs accumulate stale tabs (New Order, Invoice, Invoice Correction, etc.).
    This scans the main window for TabItems and closes all except the main
    navigation tabs.  Failures are best-effort.
    """
    try:
        main = ctx.app.main_window()
    except Exception:
        return
    try:
        tabs = main.descendants(control_type="TabItem")
    except Exception:
        return
    # Navigation tabs to keep (left sidebar)
    _KEEP = {"orders", "contacts", "products", "documents", "vats", "fakturama"}
    closed = 0
    for tab in tabs:
        try:
            text = (tab.window_text() or "").strip().lower()
        except Exception:
            continue
        if not text:
            continue
        # Keep navigation sidebar tabs
        if any(k in text for k in _KEEP):
            continue
        # Keep the currently-being-created New Order tab (step1 will open it)
        if text.startswith("new order"):
            continue
        try:
            tab.set_focus()
            tab.type_keys("^{F4}")  # Ctrl+F4 closes the active tab in Eclipse SWT
            closed += 1
            time.sleep(0.3)
        except Exception:
            logger.debug("step1: could not close stale tab %r", text)
    if closed:
        logger.info("step1: closed %d stale editor tab(s)", closed)
