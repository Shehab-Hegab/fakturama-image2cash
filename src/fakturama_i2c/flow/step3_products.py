"""Step 3 -- resolve every product line: select-or-create by exact SKU.

VAT is provisioned first (Data > VATs) so a created Product always finds its tax
rate in the VAT dropdown. Each inserted line is verified against the spec
pricing formulas before the next item is processed.
"""

from __future__ import annotations

from decimal import Decimal

from ..models import ItemLine
from ..pricing import gross_price_from_net, line_price_after_discount
from ..ui.elements import Button, Edit, Table, format_decimal
from ..utils.errors import ManualReviewError
from ..utils.logging import get_logger
from .context import (
    FlowContext,
    decimal_eq,
    row_has_exact,
    select_combo_value,
)

logger = get_logger("flow.step3")

PRODUCT_DIALOG_TITLE = "Select a product"
PRODUCT_EDITOR_TITLE = "Product"
VATS_DIALOG_TITLE = "VAT"
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
    ctx.mark("step3.products")
    for item in ctx.extracted.items:
        _resolve_product(ctx, order_win, item)
        _set_line_values(ctx, item)
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


def _select_existing_product(ctx: FlowContext, order_win, item: ItemLine) -> bool:
    ctx.set_window(order_win)
    Button(ctx.find("PRODUCT_SELECTOR"), ctx.waits).click()
    table = ctx.open_search_dialog(PRODUCT_DIALOG_TITLE, item.sku)
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
    ctx.set_window(order_win)
    Button(ctx.find("PRODUCT_SELECTOR"), ctx.waits).click()
    table = ctx.open_search_dialog(PRODUCT_DIALOG_TITLE, item.sku)
    rows = table.rows()
    matches = [i for i, r in enumerate(rows) if row_has_exact(r, item.sku)]
    if len(matches) != 1:
        raise ManualReviewError(
            "step3.product.verify",
            f"new product {item.sku!r} not uniquely visible after save",
        )
    ctx.choose_ok(table, matches[0])
    ctx.set_window(order_win)


# ---------------------------------------------------------------------------
# VAT provisioning (before product creation)
# ---------------------------------------------------------------------------


def _ensure_vat(ctx: FlowContext, order_win, vat_percent: Decimal) -> None:
    label = _vat_label(vat_percent)
    ctx.set_window(order_win)
    ctx.menu_select("MENU_DATA", "MENU_VATS", "VATs")
    vats_win = ctx.waits.for_window(ctx.app.desktop, VATS_DIALOG_TITLE, ctx.settings.window_timeout)
    ctx.set_window(vats_win)
    ctx.waits.stable_snapshot(ctx.find("RESULT_TABLE"))
    Edit(ctx.find("SEARCH_EDIT"), ctx.waits).fill(label)
    ctx.waits.stable_snapshot(ctx.find("RESULT_TABLE"))
    table = Table(ctx.find("RESULT_TABLE"), ctx.waits)
    rows = table.rows()
    named = [i for i, r in enumerate(rows) if row_has_exact(r, label)]

    if named:
        valid = [i for i in named if _vat_row_valid(rows[i], label, vat_percent)]
        if len(valid) == 1:
            logger.info("step3: reusing existing VAT %r", label)
            ctx.cancel_dialog()
            ctx.set_window(order_win)
            return
        raise ManualReviewError(
            "step3.vat.select",
            f"VAT rows for {label!r} exist but conflict (code/value mismatch)",
        )

    Button(ctx.find("VAT_NEW_BUTTON"), ctx.waits).click()
    ctx.wait_for_editor(VATS_DIALOG_TITLE, "VAT_EDITOR_NAME")
    Edit(ctx.find("VAT_EDITOR_NAME"), ctx.waits).fill(label)
    Edit(ctx.find("VAT_EDITOR_DESCRIPTION"), ctx.waits).fill(label)
    Edit(ctx.find("VAT_EDITOR_CODE"), ctx.waits).fill(VAT_CODE)
    Edit(ctx.find("VAT_EDITOR_VALUE"), ctx.waits).fill(_vat_value(vat_percent))
    ctx.save()
    ctx.set_window(vats_win)
    ctx.cancel_dialog()
    ctx.set_window(order_win)
    logger.info("step3: created VAT %r", label)


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


def _set_line_values(ctx: FlowContext, item: ItemLine) -> None:
    table = Table(ctx.find("ORDER_ITEMS_TABLE"), ctx.waits)
    rows = table.rows()
    idx = _line_index(rows, item.sku)
    if idx is None:
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


def _line_index(rows: list[list[str]], sku: str):
    for i, row in enumerate(rows):
        if row_has_exact(row, sku):
            return i
    return None


def _edit_line_cells(ctx: FlowContext, table: Table, idx: int, item: ItemLine) -> None:
    row_ctrl = table.ctrl.rows()[idx]
    edits = [c for c in row_ctrl.children() if _is_edit(c)]
    values = [
        str(item.quantity),
        format_decimal(item.unit_net_price),
        _vat_value(item.vat_percent),
        format_decimal(item.discount_percent),
    ]
    if len(edits) < len(values):
        raise ManualReviewError(
            "step3.line",
            f"line for {item.sku!r} exposes {len(edits)} edit cells, need {len(values)}",
        )
    for ctrl, value in zip(edits, values):
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