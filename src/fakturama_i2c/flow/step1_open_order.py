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
from .context import FlowContext, read_text, stabilize_active_editor

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
    time.sleep(0.8)  # Let Fakturama settle after closing tabs
    # Clicking New Order right after closing stale tabs can fail if the window
    # lost focus (COMError). Retry until the Order editor tab actually appears.
    editor = None
    for click_attempt in range(3):
        try:
            main_win = ctx.app.main_window()
            try:
                main_win.set_focus()
            except Exception:
                pass
            time.sleep(0.3)
            btn = Button(ctx.find("ORDER_NEW_BUTTON"), ctx.waits)
            btn.click()
        except Exception:
            # Fallback: click the button by coordinates (UIA click failed)
            try:
                import pywinauto
                btn_ctrl = ctx.find("ORDER_NEW_BUTTON")
                r = btn_ctrl.rectangle()
                pywinauto.mouse.click(coords=(r.mid_point().x, r.mid_point().y))
            except Exception:
                pass
        time.sleep(1.5)  # Wait for tab to fully create + render
        try:
            editor = ctx.wait_for_editor(ORDER_EDITOR_TITLE, "ORDER_DATE", timeout=10)
            break
        except ControlNotFoundError:
            logger.info("step1: New Order tab not open after click attempt %d; retrying...", click_attempt + 1)
            time.sleep(1.0)
    if editor is None:
        raise ControlNotFoundError("step1: could not open the New Order editor after 3 click attempts")
    ctx.set_window(editor)

    # Force SWT to render the full header: focus the pane + Tab through fields.
    _force_render_header(ctx)
    # Click the editor body so SWT lays out the header fields (Date, Cust.Ref,
    # ...). Without this the ORDER_DATE control can flicker in/out of the UIA
    # tree and wait_for_editor/find fail sporadically.
    stabilize_active_editor(ctx, wait_control="Date", max_retries=4)

    # Proposed No. is left unchanged (Fakturama assigns the number on save).
    # Fill with progressive retry: SWT lazy rendering may delay Cust.Ref.
    for attempt in range(3):
        try:
            _fill_date_field(ctx, header.order_date)
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
                stabilize_active_editor(ctx, wait_control="Date", max_retries=3)
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


def _fill_date_field(ctx: FlowContext, order_date: object) -> None:
    """Fill the Order date field using the 'Date' label as anchor.

    The SWT DateTime picker has an empty UIA name and a runtime auto_id that
    changes on every render, so the registry cannot match it reliably.
    We locate the static 'Date' label and click the Edit to its right.

    The picker accepts keyboard input in its display format (e.g. "Mar 18,
    2026").  We try multiple formats with increasing aggressiveness.
    """
    import pywinauto as _pw

    main = ctx.window()
    label = None
    for c in main.descendants():
        if c.element_info.control_type == "Text" and (c.window_text() or "").strip() == "Date":
            r = c.rectangle()
            if 150 < r.top < 220:  # header band
                label = c
                break
    if label is None:
        raise ControlNotFoundError("step1: 'Date' label not found in header band")

    lr = label.rectangle()
    edit_x = lr.right + 15
    edit_y = (lr.top + lr.bottom) // 2

    # Pre-compute all acceptable date strings
    display_date = order_date.strftime("%b %d, %Y")   # "Mar 18, 2026"
    iso_date = str(order_date)                          # "2026-03-18"
    us_date = order_date.strftime("%m/%d/%Y")          # "03/18/2026"
    de_date = order_date.strftime("%d.%m.%Y")          # "18.03.2026"
    acceptable = [display_date, iso_date, us_date, de_date]

    def _read_shown() -> str:
        """Read the value of the date control near the Date label.

        The SWT DateTime picker is NOT an Edit control — it's a Custom/DateTime
        type.  We check window_text() on ANY control near the target coords.
        """
        for c in main.descendants():
            r = c.rectangle()
            if abs(r.left - edit_x) < 40 and abs(r.top - edit_y) < 20:
                try:
                    return c.window_text() or ""
                except Exception:
                    pass
        return ""

    def _try_fill(date_str: str, clear_method: str = "ctrl+a") -> bool:
        """Attempt to fill the date field with a given string."""
        _pw.mouse.click(coords=(edit_x, edit_y))
        time.sleep(0.3)

        if clear_method == "ctrl+a":
            _pw.keyboard.send_keys("^a")
            time.sleep(0.15)
        elif clear_method == "triple_click":
            _pw.mouse.click(coords=(edit_x, edit_y))
            time.sleep(0.1)
            _pw.mouse.click(coords=(edit_x, edit_y))
            time.sleep(0.1)
            _pw.mouse.click(coords=(edit_x, edit_y))
            time.sleep(0.15)
        elif clear_method == "home_shift_end":
            _pw.keyboard.send_keys("{HOME}")
            time.sleep(0.1)
            _pw.keyboard.send_keys("+{END}")
            time.sleep(0.15)
        elif clear_method == "delete":
            _pw.keyboard.send_keys("^a{DELETE}")
            time.sleep(0.15)

        _pw.keyboard.send_keys(date_str, pause=0.03)
        time.sleep(0.3)
        _pw.keyboard.send_keys("{ENTER}")
        time.sleep(0.5)

        shown = _read_shown()
        return any(fmt in shown for fmt in acceptable)

    # --- Strategy 1: display format (e.g. "Mar 18, 2026") with Ctrl+A ---
    if _try_fill(display_date, "ctrl+a"):
        logger.info("step1: date set to display format: %s", _read_shown())
        return

    # --- Strategy 2: display format with triple-click select-all ---
    if _try_fill(display_date, "triple_click"):
        logger.info("step1: date set via triple-click: %s", _read_shown())
        return

    # --- Strategy 3: ISO format with Ctrl+A ---
    if _try_fill(iso_date, "ctrl+a"):
        logger.info("step1: date set to ISO format: %s", _read_shown())
        return

    # --- Strategy 4: DE format with delete-then-type ---
    if _try_fill(de_date, "delete"):
        logger.info("step1: date set to DE format: %s", _read_shown())
        return

    # --- Strategy 5: US format with home+shift+end ---
    if _try_fill(us_date, "home_shift_end"):
        logger.info("step1: date set to US format: %s", _read_shown())
        return

    # All strategies failed — log warning but continue (date is non-fatal)
    shown = _read_shown()
    logger.warning("step1: date field shows %r after all fill attempts (expected one of %r)", shown, acceptable)


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
            if expected.strip().lower() in ("with vat", "withvat", "vat included"):
                # 'With VAT' is Fakturama's built-in default for a New Order;
                # when SWT hides the combo from UIA we trust the default and
                # let step4's totals reconciliation prove VAT was applied.
                logger.warning(
                    "step1: ORDER_VAT_MODE not exposed by UIA; trusting Fakturama's "
                    "default %r (step4 totals reconciliation still proves VAT)",
                    expected,
                )
                return
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
            # Dismiss any 'Save Parts' dialog that may appear for unsaved docs
            try:
                dlg = main.child_window(title="Save Parts", control_type="Window")
                if dlg.exists(timeout=0.5):
                    cancel = dlg.child_window(title_re="(?i)cancel", control_type="Button")
                    if cancel.exists(timeout=0.5):
                        cancel.click_input()
                        time.sleep(0.3)
            except Exception:
                pass
        except Exception:
            logger.debug("step1: could not close stale tab %r", text)
    if closed:
        logger.info("step1: closed %d stale editor tab(s)", closed)
