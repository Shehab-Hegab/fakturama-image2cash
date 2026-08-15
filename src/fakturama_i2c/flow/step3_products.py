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
        raise ManualReviewError(
            "step3.product.select",
            f"product result table is not exposed by UIA; exact SKU {item.sku!r} cannot be proven",
        )

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
        raise ManualReviewError(
            "step3.product.verify",
            f"new product {item.sku!r} cannot be verified because the result table is unavailable",
        )

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


def _set_line_values(ctx: FlowContext, item: ItemLine, row_index: int = 0) -> None:
    """Fill the order line values for the given item.

    When ORDER_ITEMS_TABLE is invisible to UIA (SWT lazy rendering), falls
    back to clicking into the items table area and using keyboard navigation.
    """
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
            raise ManualReviewError(
                "step3.line",
                f"items table is not exposed by UIA; cannot safely edit {item.sku!r}",
            )


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
