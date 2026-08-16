"""Step 3 -- resolve every product line: select-or-create by exact SKU.

VAT is provisioned first (Data > VATs) so a created Product always finds its tax
rate in the VAT dropdown. Each inserted line is verified against the spec
pricing formulas before the next item is processed.
"""

from __future__ import annotations

import time
from decimal import Decimal

from ..models import ItemLine
from ..pricing import gross_price_from_net, line_price_after_discount
from ..ui.elements import Button, Edit, Table, format_decimal
from ..utils.errors import ControlNotFoundError, FlowTimeoutError, ManualReviewError
from ..utils.logging import get_logger
from .context import (
    FlowContext,
    _dismiss_owned_dialog_titles,
    decimal_eq,
    row_has_exact,
    select_combo_value,
    stabilize_active_editor,
)

logger = get_logger("flow.step3")

PRODUCT_DIALOG_TITLE = "Select a product"
PRODUCT_EDITOR_TITLE = "Product"
VATS_TAB_TITLE = "VATs"
VAT_CODE = "S"


def step_add_products(ctx: FlowContext) -> None:
    """Add every extracted item to the still-open Order as a product line."""
    if ctx.settings.dry_run:
        for item in ctx.extracted.items:
            logger.info(
                "DRY-RUN step3: item sku=%s qty=%s net=%s vat=%s%% disc=%s%%",
                item.sku,
                item.quantity,
                item.unit_net_price,
                item.vat_percent,
                item.discount_percent,
            )
        ctx.mark("step3.products")
        return

    order_win = ctx.window()
    _force_render_items(ctx)
    ctx.mark("step3.products")
    for i, item in enumerate(ctx.extracted.items):
        _resolve_product(ctx, order_win, item)
        _set_line_values(ctx, item, row_index=i)
    logger.info("step3: %d product line(s) added", len(ctx.extracted.items))


# ---------------------------------------------------------------------------
# Select-or-create per item
# ---------------------------------------------------------------------------


def _resolve_product(ctx: FlowContext, order_win, item: ItemLine) -> None:
    if not _select_existing_product(ctx, order_win, item):
        logger.info("step3: no exact SKU match for %r; creating product", item.sku)
        _ensure_vat(ctx, order_win, item.vat_percent)
        _create_product(ctx, order_win, item)
        _select_new_product(ctx, order_win, item)


def _click_product_selector(ctx: FlowContext) -> None:
    """Click the Product selector, scoped to the Items section header.

    The Items section is lazily rendered by SWT.  We anchor the click to the
    'Items' label (anchor-relative offset) so the search button is never
    confused with the 20+ bare toolbar buttons.  Retries re-stabilize the
    editor if SWT drops the section from the UIA tree.
    """
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            # Dismiss any stray dialog before trying
            _dismiss_position_description(ctx.window())
            Button(ctx.find("PRODUCT_SELECTOR"), ctx.waits).click()
            return
        except (ControlNotFoundError, ManualReviewError) as exc:
            last_exc = exc
            logger.info(
                "step3: product selector attempt %d/4 failed (%s); re-rendering...",
                attempt + 1,
                exc,
            )
            _force_render_items(ctx)
            stabilize_active_editor(ctx, wait_control="Items", max_retries=3)
            time.sleep(0.5)
    assert last_exc is not None
    raise last_exc


def _select_existing_product(ctx: FlowContext, order_win, item: ItemLine) -> bool:
    ctx.set_window(order_win)
    # CRITICAL: SWT lazy rendering — the Items section (and its product
    # selector) is not in the UIA tree until the editor is stabilized.
    # Also dismiss any stray "position description" dialog first.  The dialog
    # can open with a DELAY after the previous cell edit commits, so sweep
    # for it repeatedly over ~3s before stabilizing.
    _activate_order_editor(ctx)
    for _ in range(6):
        _dismiss_position_description(ctx.window())
        time.sleep(0.5)
    stabilize_active_editor(ctx, wait_control="Items", max_retries=5)
    _force_render_items(ctx)
    _click_product_selector(ctx)
    try:
        table = ctx.open_search_dialog(PRODUCT_DIALOG_TITLE, item.sku)
    except (FlowTimeoutError, ControlNotFoundError) as exc:
        raise ManualReviewError(
            "step3.product.select",
            f"product selector for {item.sku!r} is unavailable; cannot prove it is missing",
        ) from exc

    if table is None:
        logger.info("step3: virtual product table selected %r via keyboard traversal", item.sku)
        ctx.set_window(order_win)
        return True

    rows = table.rows()
    matches = [i for i, r in enumerate(rows) if row_has_exact(r, item.sku)]

    if len(matches) == 1:
        ctx.choose_ok(table, matches[0])
        ctx.set_window(order_win)
        logger.info("step3: selected existing product %r", item.sku)
        return True

    if len(matches) > 1:
        raise ManualReviewError(
            "step3.product.select",
            f"{len(matches)} products match SKU {item.sku!r}; refusing to guess",
        )

    ctx.cancel_dialog()
    ctx.set_window(order_win)
    return False


def _select_new_product(ctx: FlowContext, order_win, item: ItemLine) -> None:
    """Reopen the product search dialog and select the newly created product."""
    ctx.set_window(order_win)
    time.sleep(0.5)
    _click_product_selector(ctx)
    try:
        table = ctx.open_search_dialog(PRODUCT_DIALOG_TITLE, item.sku)
    except (FlowTimeoutError, ControlNotFoundError):
        # The dialog may take longer to appear after a fresh product was just
        # created.  Give it extra time and retry once.
        time.sleep(1.0)
        _click_product_selector(ctx)
        table = ctx.open_search_dialog(PRODUCT_DIALOG_TITLE, item.sku)

    if table is None:
        logger.info("step3: virtual product table re-selected %r via keyboard traversal", item.sku)
        ctx.set_window(order_win)
        return

    rows = table.rows()
    matches = [i for i, r in enumerate(rows) if row_has_exact(r, item.sku)]
    if len(matches) != 1:
        raise ManualReviewError(
            "step3.product.verify",
            f"new product {item.sku!r} not uniquely visible after save "
            f"(found {len(matches)} matches in {len(rows)} rows)",
        )
    ctx.choose_ok(table, matches[0])
    ctx.set_window(order_win)


# ---------------------------------------------------------------------------
# VAT provisioning (before product creation)
# ---------------------------------------------------------------------------


def _ensure_vat(ctx: FlowContext, order_win, vat_percent: Decimal) -> None:
    """Ensure a VAT with the given percentage exists.

    Fakturama opens VATs as a *TabItem* inside the main window (not a
    separate desktop dialog).  After ``Data > VATs`` the VATs tab is
    active and all controls live inside the main window scope.

    Strategy:
    1. ``Data > VATs`` → wait for the VATs TabItem to become selected.
    2. Check the bottom-panel table for the VAT (skip if SWT-invisible).
    3. Click the *New* button → a Product-like editor pane opens inside
       the VATs tab → fill fields → Save.
    """
    label = _vat_label(vat_percent)
    ctx.set_window(order_win)

    # --- 1. Navigate to the VATs tab ------------------------------------
    try:
        ctx.menu_select("MENU_DATA", "MENU_VATS", "VATs")
    except (ControlNotFoundError, FlowTimeoutError) as exc:
        raise ManualReviewError(
            "step3.vat.select", f"cannot open VATs to verify {label!r}"
        ) from exc

    # Wait until the VATs TabItem is selected inside the main window.
    _wait_for_tab_selected(ctx.app.main_window(), "VATs", ctx.settings.window_timeout)
    ctx.set_window(ctx.app.main_window())
    logger.info("step3: VATs tab is now active")

    # --- 2. Check if VAT already exists ----------------------------------
    vat_found = False
    try:
        table_ctrl = ctx.find("RESULT_TABLE")
        ctx.waits.stable_snapshot(table_ctrl)
        Edit(ctx.find("SEARCH_EDIT"), ctx.waits).fill(label)
        ctx.waits.stable_snapshot(table_ctrl)
        table = Table(table_ctrl, ctx.waits)
        rows = table.rows()
        named = [i for i, r in enumerate(rows) if row_has_exact(r, label)]
        if named:
            valid = [i for i in named if _vat_row_valid(rows[i], label, vat_percent)]
            if len(valid) == 1:
                logger.info("step3: reusing existing VAT %r", label)
                vat_found = True
            else:
                raise ManualReviewError(
                    "step3.vat.select",
                    f"VAT rows for {label!r} exist but conflict (code/value mismatch)",
                )
    except ManualReviewError:
        raise
    except Exception as exc:
        raise ManualReviewError(
            "step3.vat.select", f"cannot inspect existing VAT records for {label!r}"
        ) from exc

    if vat_found:
        # Close VATs tab and return to the order
        try:
            ctx.window().type_keys("^w")  # Ctrl+W close tab
            time.sleep(0.3)
        except Exception:
            pass
        ctx.set_window(order_win)
        return

    # --- 3. Create the VAT -----------------------------------------------
    Button(ctx.find("VAT_NEW_BUTTON"), ctx.waits).click()
    # The VAT editor opens as a new TabItem inside the VATs section.
    time.sleep(1.0)
    ctx.set_window(ctx.app.main_window())
    Edit(ctx.find("VAT_EDITOR_NAME"), ctx.waits).fill(label)
    Edit(ctx.find("VAT_EDITOR_DESCRIPTION"), ctx.waits).fill(label)
    Edit(ctx.find("VAT_EDITOR_CODE"), ctx.waits).fill(VAT_CODE)
    Edit(ctx.find("VAT_EDITOR_VALUE"), ctx.waits).fill(_vat_value(vat_percent))
    ctx.save()
    time.sleep(0.5)

    # Close the VATs tab and return to the order
    try:
        ctx.set_window(ctx.app.main_window())
        ctx.window().type_keys("^w")
        time.sleep(0.3)
    except Exception:
        pass
    ctx.set_window(order_win)
    logger.info("step3: created VAT %r", label)


def _wait_for_tab_selected(win, tab_text: str, timeout: float) -> None:
    """Block until a TabItem with *tab_text* is selected inside *win*."""
    import pywinauto
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for ctrl in win.descendants(control_type="TabItem"):
                try:
                    if ctrl.window_text() == tab_text and ctrl.is_selected():
                        return
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.3)
    logger.warning("step3: TabItem %r not found/selected within %.1fs", tab_text, timeout)


def _vat_row_valid(row: list[str], label: str, vat_percent: Decimal) -> bool:
    has_name = row_has_exact(row, label)
    has_code = any((c or "").strip().lower() == VAT_CODE.lower() for c in row)
    has_value = any(decimal_eq(c, vat_percent) for c in row)
    return has_name and has_code and has_value


# ---------------------------------------------------------------------------
# Product editor
# ---------------------------------------------------------------------------


def _create_product(ctx: FlowContext, order_win, item: ItemLine) -> None:
    ctx.set_window(order_win)
    Button(ctx.find("PRODUCT_NEW_BUTTON"), ctx.waits).click()
    ctx.wait_for_editor(PRODUCT_EDITOR_TITLE, "PRODUCT_ITEM_NUMBER")

    gross = gross_price_from_net(item.unit_net_price, item.vat_percent)
    Edit(ctx.find("PRODUCT_ITEM_NUMBER"), ctx.waits).fill(item.sku)
    Edit(ctx.find("PRODUCT_NAME"), ctx.waits).fill(item.description)
    Edit(ctx.find("PRODUCT_DESCRIPTION"), ctx.waits).fill(item.description)
    Edit(ctx.find("PRODUCT_PRICE_GROSS"), ctx.waits).fill(format_decimal(gross))
    Edit(ctx.find("PRODUCT_COST_PRICE"), ctx.waits).fill("0.00")
    select_combo_value(ctx, "PRODUCT_VAT", _vat_label(item.vat_percent), "step3.product.vat")
    Edit(ctx.find("PRODUCT_STOCK"), ctx.waits).fill("0.00")
    # Category / GTIN / supplier code / allowance / picture / user field stay blank.
    ctx.save()
    ctx.set_window(order_win)
    logger.info("step3: created product %r (gross price %s)", item.sku, format_decimal(gross))


# ---------------------------------------------------------------------------
# Order line values + formula verification
# ---------------------------------------------------------------------------


def _find_position_description(main):
    """Return the 'position description' dialog Window descendant if present,
    else None.  The dialog is a [Window] 'position description' child of the
    main Fakturama window (not a top-level desktop window)."""
    try:
        for ctrl in main.descendants():
            try:
                title = (ctrl.window_text() or "").strip().lower()
                if "position description" in title and ctrl.element_info.control_type == "Window":
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def _dismiss_position_description(main) -> bool:
    """Dismiss the 'position description' modal dialog if it appeared.

    This yellow dialog opens when a double-click lands on the Description/Name
    column instead of the intended cell.  It blocks all interaction until
    dismissed.  Returns True if a dialog was found and dismissed.

    Strategy (ordered from most to least reliable):
    1. Search ALL desktop windows (Eclipse SWT modal dialogs are real top-level).
    2. Search main window descendants for any dialog with "description" in title.

    IMPORTANT: this function NEVER sends an unconditional {ESC} to the main
    window.  A blanket {ESC} kills an open cell editor (cancels the in-place
    edit) which is worse than the dialog itself.
    """
    import pywinauto as _pw
    dismissed = False
    # 0. Owned popups: invisible to UIA desktop/descendant scans.
    try:
        if _dismiss_owned_dialog_titles(("position description", "description")):
            dismissed = True
            logger.info("step3: dismissed 'position description' dialog (owned popup)")
    except Exception:
        pass
    # 1. Desktop windows
    try:
        desktop = _pw.Desktop(backend="uia")
        for win in desktop.windows():
            try:
                title = (win.window_text() or "").strip().lower()
                if "position description" in title or "description" in title:
                    # Only dismiss if it looks like a dialog (not the main window)
                    if win != main:
                        try:
                            win.set_focus()
                        except Exception:
                            pass
                        _pw.keyboard.send_keys("{ESC}")
                        time.sleep(0.4)
                        logger.info("step3: dismissed 'position description' dialog (desktop)")
                        dismissed = True
                        # Verify it's gone
                        time.sleep(0.2)
                        try:
                            for w2 in desktop.windows():
                                t2 = (w2.window_text() or "").strip().lower()
                                if "position description" in t2 and w2 != main:
                                    _pw.keyboard.send_keys("{ESC}")
                                    time.sleep(0.3)
                        except Exception:
                            pass
                        return True
            except Exception:
                continue
    except Exception:
        pass
    # 2. Main window descendants — the dialog is a [Window] 'position description'
    #    descendant of main with an Edit + OK/Cancel buttons.  ctrl.close() does
    #    NOT work on SWT modal windows; send {ESC} to that window, then click
    #    Cancel as a fallback.
    try:
        for ctrl in main.descendants():
            try:
                title = (ctrl.window_text() or "").strip().lower()
                if "position description" in title and ctrl.element_info.control_type == "Window":
                    try:
                        ctrl.set_focus()
                    except Exception:
                        pass
                    time.sleep(0.2)
                    _pw.keyboard.send_keys("{ESC}")
                    time.sleep(0.4)
                    # Verify: if still present, click Cancel button
                    try:
                        cancel = ctrl.child_window(title="Cancel", control_type="Button")
                        if cancel.exists(timeout=0.3):
                            cancel.click()
                            time.sleep(0.4)
                    except Exception:
                        pass
                    logger.info("step3: dismissed 'position description' dialog (descendant Window)")
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return dismissed


def _click_neutral_no_label(main, row_y: int = 0) -> None:
    """Click a neutral spot in the 'No.' column to exit in-place editing.

    After {ENTER} commits a cell edit, SWT focus moves to the next column
    (Description/Name).  Any further keyboard input in that column can
    trigger the 'position description' dialog.  Clicking a neutral spot
    in the 'No.' column (the row-number column) cleanly exits in-place
    editing without side effects.

    If row_y is provided (>0), clicks at (360, row_y) to stay at the same
    row level -- this preserves row context for subsequent cell edits
    (e.g. discount after qty).  If row_y is 0, clicks the 'No.' column
    header label at y≈180.
    """
    if row_y > 0:
        # Click at the same row level in the 'No.' column (x≈360) to
        # preserve row focus between cell edits on the same row.
        try:
            pywinauto.mouse.click(coords=(360, row_y))
            time.sleep(0.15)
            logger.debug("step3: clicked 'No.' column at row_y=%d to neutralize focus", row_y)
            return
        except Exception:
            pass
    try:
        no_label = main.child_window(title_re=r"(?i)^No\.$", control_type="Text")
        if no_label.exists(timeout=1.0):
            no_label.click_input()
            time.sleep(0.15)
            logger.debug("step3: clicked 'No.' label to neutralize focus")
            return
    except Exception:
        pass
    # Fallback: click a point above the Items section header (column header area)
    try:
        pywinauto.mouse.click(coords=(360, 180))
        time.sleep(0.15)
    except Exception:
        pass


def _activate_order_editor(ctx: FlowContext) -> None:
    """Activate the CURRENT run's '*New Order' editor tab.

    SWT CTabFolder renders only the ACTIVE editor's content into the UIA
    tree.  A leftover editor (e.g. the Customer editor opened while resolving
    the debtor in step2, or stale '*New Order' tabs whose closes were aborted
    by 'Save Parts' Cancel) can remain the active tab, which hides the real
    Order's Items section from ORDER_ITEMS_TABLE and the 'Items' anchor.

    Newest-first heuristic: editors are appended to the end of the CTabFolder
    tab strip, so the LAST 'New Order' tab is the one opened by step1 of the
    current run (older stale editors sit further left).  With the stale-tab
    sweep really closing tabs (Don't save), there is normally only ONE
    candidate anyway.  Each candidate is activated and verified via the
    'Items' Text anchor before acceptance.
    """
    import pywinauto

    main = ctx.app.main_window()
    try:
        tabs = main.descendants(control_type="TabItem")
    except Exception as exc:
        raise ControlNotFoundError("order_tab", f"cannot enumerate editor tabs: {exc}") from exc
    candidates = [
        t for t in tabs if "new order" in (t.window_text() or "").lower()
    ]
    if not candidates:
        raise ControlNotFoundError("order_tab", "no '*New Order' editor tab found")

    def _items_visible() -> bool:
        try:
            return any(
                c.element_info.control_type == "Text" and "Items" in (c.window_text() or "")
                for c in main.descendants()
            )
        except Exception:
            return False

    for tab in reversed(candidates):
        rect = tab.rectangle()
        pywinauto.mouse.click(
            coords=(int((rect.left + rect.right) / 2), int((rect.top + rect.bottom) / 2))
        )
        time.sleep(1.0)
        if _items_visible():
            return
    raise ControlNotFoundError("order_tab", "clicked 'New Order' tab(s) but 'Items' anchor stayed hidden")


def _set_line_values_keyboard_fallback(ctx: FlowContext, item: ItemLine, row_index: int) -> None:
    """Anchor-relative keyboard fallback for Qty and Discount on the Items table.

    Used when ORDER_ITEMS_TABLE is invisible to UIA (SWT lazy rendering).
    Cell positions are offsets from the 'Items' section label bounding box --
    never absolute screen coordinates.  U.Price is explicitly re-set to the
    extracted unit price because Fakturama fills it from the Product master
    data, which can drift from the image (e.g. SKU-1002 master 10.00 vs
    extracted 9.90).
    """
    import pywinauto

    main = ctx.window()
    _activate_order_editor(ctx)
    try:
        items_label = next(
            c for c in main.descendants()
            if c.element_info.control_type == "Text" and "Items" in (c.window_text() or "")
        )
        anchor_rect = items_label.rectangle()
    except StopIteration as exc:
        raise ControlNotFoundError(
            "items_anchor", "cannot find 'Items' anchor for keyboard fallback"
        ) from exc

    # Anchor-relative cell offsets (calibrated live by double-click probing
    # the Items table row 1 of the current order editor):
    #   Qty cell editor opens for x∈[444..650] → centre ≈ 500
    #   VAT is a combo editor at x∈[1059..1170] — never aim there
    #   U.Price editor rect x∈[1184..1308] → centre ≈ 1246
    #   Discount editor rect x∈[1309..1433] → centre ≈ 1371
    #   Price column (right of Discount) has NO editor — it is derived
    #   x=1000 opens the 'position description' dialog (Description column) —
    #   never aim there.
    # anchor.left=338 → qty offset +162, price offset +908, discount offset
    # +1033.  Row 0 data band centre y≈412; anchor.top=374 → row offset +38.
    qty_x = anchor_rect.left + 162
    price_x = anchor_rect.left + 908
    discount_x = anchor_rect.left + 1033
    row_y = anchor_rect.top + 38 + (row_index * 30)

    def _open_cell_editor(cx, cy):
        """Double-click + probe + F2 fallback to open a SWT cell editor."""
        # CRITICAL: dismiss any stray dialog BEFORE the double-click — an open
        # 'position description' (or 'Save Parts') modal would swallow the
        # click and no cell editor would open.
        _dismiss_position_description(main)
        pywinauto.mouse.double_click(coords=(cx, cy))
        time.sleep(0.5)
        # CRITICAL: double-click on the wrong column (Description/Name) triggers
        # a "position description" modal dialog that blocks all interaction.
        # Dismiss it IMMEDIATELY before checking for the cell editor.
        _dismiss_position_description(main)
        found = False
        try:
            for ctrl in main.descendants():
                if ctrl.element_info.control_type == "Edit":
                    r = ctrl.rectangle()
                    if (cx - 80) < r.left < (cx + 80) and abs(r.top - cy) < 40:
                        found = True
                        break
        except Exception:
            pass
        if not found:
            pywinauto.keyboard.send_keys("{F2}")
            time.sleep(0.4)
            # F2 might also trigger the description dialog
            _dismiss_position_description(main)

    def _type_in_cell(cx, cy, value):
        """Open cell editor, select-all, type value, commit.

        CRITICAL: ^a (Ctrl+A) does NOT work in SWT cell editors — it selects
        ALL ROWS in the table instead of all text in the cell.  Use Home then
        Shift+End to select all text, then type the replacement value.

        {ENTER} commits the edit.  If focus then moves to the Description
        column, a 'position description' dialog may open; dismiss it with the
        robust helper after committing.

        Only keystrokes are sent once the cell editor is confirmed open: a
        failed double-click would leave focus in the Description column and
        typing there triggers the 'position description' dialog.
        """
        for attempt in range(3):
            _dismiss_position_description(main)
            _open_cell_editor(cx, cy)
            if not _cell_editor_open(cx, cy):
                logger.warning(
                    "step3: cell editor for %r at (%d,%d) did not open (attempt %d); retrying...",
                    value, cx, cy, attempt + 1,
                )
                continue
            pywinauto.keyboard.send_keys("{HOME}")
            time.sleep(0.1)
            pywinauto.keyboard.send_keys("+{END}")
            time.sleep(0.1)
            pywinauto.keyboard.send_keys(str(value))
            time.sleep(0.2)
            pywinauto.keyboard.send_keys("{ENTER}")
            time.sleep(0.4)
            # CRITICAL: After {ENTER} commits the cell, SWT focus advances
            # to the next column (Description).  A bare {ESC} or any further
            # typing in that column triggers the 'position description'
            # dialog.  Instead, click the neutral 'No.' column-header label
            # to cleanly exit in-place editing mode.
            _click_neutral_no_label(main, row_y=cy)
            time.sleep(0.3)
            _dismiss_position_description(main)
            # Verify the edit cell is gone (committed) and no dialog remains.
            dlg = _find_position_description(main)
            if dlg is None:
                return
            logger.info(
                "step3: 'position description' dialog reappeared (attempt %d); retrying...",
                attempt + 1,
            )
            _dismiss_position_description(main)
            time.sleep(0.5)
        logger.warning("step3: cell edit for %r possibly blocked by dialog", value)

    def _cell_editor_open(cx, cy):
        """True when an Edit control sits near (cx, cy) — the SWT cell editor."""
        try:
            for ctrl in main.descendants():
                if ctrl.element_info.control_type == "Edit":
                    r = ctrl.rectangle()
                    if (cx - 80) < r.left < (cx + 80) and abs(r.top - cy) < 40:
                        return True
        except Exception:
            pass
        return False

    # --- Qty cell: double-click to open editor (single-click doesn't activate
    #     SWT inline editor; ^a then selects all ROWS instead of cell text) ---
    _type_in_cell(qty_x, row_y, item.quantity)

    # --- U.Price cell: enforce the extracted unit price on the line (the
    #     master-derived value can drift, e.g. SKU-1002 shows 10.00 while the
    #     image says 9.90; step4 reconciles totals against the extraction). ---
    _type_in_cell(price_x, row_y, format_decimal(item.unit_net_price))

    # --- Discount cell (same row; only when the line has a discount) ---
    discount_pct = float(getattr(item, "discount_percent", 0) or 0)
    if discount_pct > 0:
        _type_in_cell(discount_x, row_y, format_decimal(item.discount_percent))

    # --- Final cleanup: release SWT edit mode fully so the Product Selector
    #     for SKU-1002 is not invoked while a cell editor is still active. ---
    # The cell was already committed by _type_in_cell.  Click the neutral
    # 'No.' column area at row level to cleanly exit in-place editing.  Do NOT
    # send a bare {ESC} (it can cancel an edit or reopen the dialog) and
    # do NOT click the 'Items' label (ExpandableComposite toggle collapses
    # the section and hides the Product Selector button).
    _click_neutral_no_label(main, row_y=row_y)
    time.sleep(0.3)
    _dismiss_position_description(main)

    logger.info(
        "step3: anchor-relative fallback set qty=%s, discount=%s%% for %r (row %d)",
        item.quantity, item.discount_percent, item.sku, row_index,
    )


def _set_line_values(ctx: FlowContext, item: ItemLine, row_index: int = 0) -> None:
    """Fill the order line values for the given item.

    When ORDER_ITEMS_TABLE is invisible to UIA (SWT lazy rendering), falls
    back to clicking into the items table area and using keyboard navigation.
    """
    # SWT CTabFolder exposes only the ACTIVE editor's content to UIA.  A
    # leftover editor (e.g. the Customer editor opened while resolving the
    # debtor) can remain the active tab, hiding this Order's Items section.
    _activate_order_editor(ctx)
    # Allow the items table to stabilize after product selection.
    # SWT lazy rendering may need multiple stabilization passes.
    for attempt in range(3):
        time.sleep(0.8 if attempt == 0 else 1.5)
        try:
            table = Table(ctx.find("ORDER_ITEMS_TABLE"), ctx.waits)
            rows = table.rows()
            idx = _line_index(rows, item.sku)
            if idx is None:
                # Table may need re-render after product selection; retry with delay.
                time.sleep(1.0)
                rows = table.rows()
                idx = _line_index(rows, item.sku)
            if idx is None:
                if attempt < 2:
                    logger.info("step3: SKU %r not in items table (attempt %d); retrying...", item.sku, attempt + 1)
                    _force_render_items(ctx)
                    continue
                raise ManualReviewError("step3.line", f"line for SKU {item.sku!r} not in items table")
            table.select_row(idx)
            _edit_line_cells(ctx, table, idx, item)

            expected = line_price_after_discount(item.quantity, item.unit_net_price, item.discount_percent)
            refreshed = table.rows()
            ridx = _line_index(refreshed, item.sku)
            row = refreshed[ridx] if ridx is not None else None
            if row is None or not any(decimal_eq(c, expected) for c in row):
                raise ManualReviewError(
                    "step3.line.verify",
                    f"line {item.sku!r} price != {expected} (formula) in {row!r}",
                )
            logger.info("step3: line %r confirmed %s", item.sku, format_decimal(expected))
            return  # Success — exit retry loop
        except ControlNotFoundError:
            if attempt < 2:
                logger.info("step3: ORDER_ITEMS_TABLE not visible (attempt %d); retrying...", attempt + 1)
                _force_render_items(ctx)
                continue
            # SWT lazy rendering: the table is not exposed to UIA at all.
            # Use the anchor-relative keyboard fallback (offset from the
            # 'Items' section label -- never absolute screen coordinates).
            logger.warning("step3: items table invisible to UIA; using anchor-relative keyboard fallback for %r", item.sku)
            _set_line_values_keyboard_fallback(ctx, item, row_index=row_index)


def _line_index(rows: list[list[str]], sku: str):
    for i, row in enumerate(rows):
        if row_has_exact(row, sku):
            return i
    return None


def _edit_line_cells(ctx: FlowContext, table: Table, idx: int, item: ItemLine) -> None:
    row_ctrl = table.ctrl.rows()[idx]
    edits = [c for c in row_ctrl.children() if _is_edit(c)]
    # Selecting an existing Product must retain its master-derived U.Price.
    # Only transaction-specific quantity, VAT and discount may be edited.
    if len(edits) < 4:
        raise ManualReviewError(
            "step3.line",
            f"line for {item.sku!r} exposes {len(edits)} edit cells, need Qty/U.Price/VAT/Discount",
        )
    for ctrl, value in (
        (edits[0], str(item.quantity)),
        (edits[2], _vat_value(item.vat_percent)),
        (edits[3], format_decimal(item.discount_percent)),
    ):
        Edit(ctrl, ctx.waits).fill(value)


def _is_edit(control) -> bool:
    try:
        return control.element_info.control_type == "Edit"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# VAT label helpers
# ---------------------------------------------------------------------------


def _vat_value(pct: Decimal) -> str:
    if pct == pct.to_integral_value():
        return str(int(pct))
    return str(pct)


def _vat_label(pct: Decimal) -> str:
    return f"VAT {_vat_value(pct)}%"


def _force_render_items(ctx: FlowContext) -> None:
    """Force SWT to lazily render the Items section.

    Eclipse SWT renders the Items table area only after the editor gains
    focus.  Sending Tab keypresses through the header triggers lazy render,
    making the Items section (and its product-selector Image icons)
    available in the UIA tree for PRODUCT_SELECTOR resolution.
    """
    _activate_order_editor(ctx)
    try:
        ctx.window().set_focus()
    except Exception:
        pass
    time.sleep(0.2)
    # Click into the items table area to trigger its render
    try:
        tbl = ctx.find("ORDER_ITEMS_TABLE")
        from ..ui.elements import Table as _Tbl
        _Tbl(tbl, ctx.waits).ctrl.click_input()
        time.sleep(0.3)
    except Exception:
        # ORDER_ITEMS_TABLE may be invisible to UIA — skip click
        pass
    # Tab through fields to ensure Items section is fully rendered
    for _ in range(5):
        try:
            ctx.window().type_keys("{TAB}")
            time.sleep(0.15)
        except Exception:
            break
    for _ in range(5):
        try:
            ctx.window().type_keys("+{TAB}")
            time.sleep(0.1)
        except Exception:
            break
    time.sleep(0.3)
