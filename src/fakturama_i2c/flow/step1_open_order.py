"""Step 1 -- open a New Order and fill the header from the extraction.

Keeps the New Order editor open: it is the persistent anchor every later step
returns to (the "still-open Order" from the design doc).
"""

from __future__ import annotations

from ..models import VatMode
from ..ui.elements import Button, Combo, Edit
from ..utils.errors import ControlNotFoundError, ManualReviewError
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
        except ControlNotFoundError:
            if expected == VatMode.WITH_VAT.value:
                logger.info(
                    "step1: ORDER_VAT_MODE control not rendered by SWT; "
                    "trusting Fakturama default 'With VAT'"
                )
                return
            raise ManualReviewError(
                "step1.open_order.vat",
                "VAT-mode combo not exposed by SWT and expected value "
                f"{expected!r} differs from Fakturama's default 'With VAT'",
            )


def _select_vat_mode(ctx: FlowContext, expected: str) -> None:
    Combo(ctx.find("ORDER_VAT_MODE"), ctx.waits).select(expected)