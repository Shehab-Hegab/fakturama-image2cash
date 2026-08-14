"""Step 1 -- open a New Order and fill the header from the extraction.

Keeps the New Order editor open: it is the persistent anchor every later step
returns to (the "still-open Order" from the design doc).
"""

from __future__ import annotations

from ..ui.elements import Button, Combo, Edit
from ..utils.logging import get_logger
from .context import FlowContext

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

    Button(ctx.find("ORDER_NEW_BUTTON"), ctx.waits).click()
    # Fakturama opens the New Order editor as a tab inside the main window,
    # not a top-level window -- wait_for_editor handles both cases.
    editor = ctx.wait_for_editor(ORDER_EDITOR_TITLE, "ORDER_DATE")
    ctx.set_window(editor)

    # Proposed No. is left unchanged (Fakturama assigns the number on save).
    Edit(ctx.find("ORDER_DATE"), ctx.waits).fill(str(header.order_date))
    Edit(ctx.find("ORDER_REFERENCE"), ctx.waits).fill(header.external_reference)
    Combo(ctx.find("ORDER_PRICE_MODE"), ctx.waits).select(header.price_mode.value)
    Combo(ctx.find("ORDER_VAT_MODE"), ctx.waits).select(header.vat_mode.value)

    logger.info(
        "step1: New Order open (date=%s, ref=%r, %s, %s)",
        header.order_date,
        header.external_reference,
        header.price_mode.value,
        header.vat_mode.value,
    )
    ctx.mark("step1.open_order")