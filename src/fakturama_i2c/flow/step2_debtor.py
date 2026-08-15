"""Step 2 -- resolve the Debtor: select an existing contact or create one.

Select-or-create on an exact-match-only basis. When the extracted payment
method is missing from the Debtor combo it is provisioned through
Data > Terms of payment before the Debtor is saved. The Order stays open
throughout and the flow always returns to it.
"""

from __future__ import annotations

import time

from ..models import DebtorData, payment_code_for
from ..ui.elements import Button, Checkbox, Combo, Edit, List
from ..utils.errors import ControlNotFoundError, ManualReviewError
from ..utils.logging import get_logger
from .context import (
    FlowContext,
    read_text,
    row_matches_exact,
    select_combo_value,
    stabilize_active_editor,
)

logger = get_logger("flow.step2")

ADDRESS_DIALOG_TITLE = "Select the address"
CONTACT_EDITOR_TITLE = "Contact"
PAYMENT_DIALOG_TITLE = "Terms of payment"


def step_resolve_debtor(ctx: FlowContext) -> None:
    """Resolve the extracted Debtor inside the still-open Order."""
    debtor = ctx.extracted.debtor

    if ctx.settings.dry_run:
        logger.info(
            "DRY-RUN step2: debtor=%r method=%r same_delivery=%s",
            debtor.search_key,
            debtor.payment_method,
            debtor.same_delivery_address,
        )
        ctx.mark("step2.debtor")
        return

    order_win = ctx.window()
    ctx.mark("step2.debtor")
    _select_or_create(ctx, order_win, debtor)
    _confirm_addresses(ctx, order_win, debtor)


# ---------------------------------------------------------------------------
# Select-or-create
# ---------------------------------------------------------------------------


def _select_or_create(ctx: FlowContext, order_win, debtor: DebtorData) -> None:
    ctx.set_window(order_win)
    _force_render_section(ctx)
    # CRITICAL: SWT lazy rendering — the Addresses section is not in the UIA
    # tree until the editor body is clicked and the form is traversed.
    stabilize_active_editor(ctx, wait_control="Addresses", max_retries=5)
    _click_debtor_selector(ctx)
    table = ctx.open_search_dialog(ADDRESS_DIALOG_TITLE, debtor.search_key)

    if table is None:
        raise ManualReviewError(
            "step2.debtor.select",
            "address result table is not exposed by UIA; exact debtor match cannot be proven",
        )

    rows = table.rows()
    matches = [i for i, r in enumerate(rows) if row_matches_exact(r, _expected_debtor_cells(debtor))]

    if len(matches) == 1:
        ctx.choose_ok(table, matches[0])
        ctx.set_window(order_win)
        logger.info("step2: selected existing contact %r", debtor.search_key)
        return

    if len(matches) > 1:
        raise ManualReviewError(
            "step2.debtor.select",
            f"{len(matches)} contacts exactly match {debtor.search_key!r}; refusing to guess",
        )

    logger.info("step2: no exact contact match; creating a new Debtor")
    ctx.cancel_dialog()
    ctx.set_window(order_win)
    _create_debtor(ctx, order_win, debtor)
    _reopen_and_select(ctx, order_win, debtor)


def _reopen_and_select(ctx: FlowContext, order_win, debtor: DebtorData) -> None:
    ctx.set_window(order_win)
    _click_debtor_selector(ctx)
    table = ctx.open_search_dialog(ADDRESS_DIALOG_TITLE, debtor.search_key)

    if table is None:
        raise ManualReviewError(
            "step2.debtor.reverify",
            "address result table is not exposed by UIA; newly created debtor cannot be proven",
        )

    rows = table.rows()
    matches = [i for i, r in enumerate(rows) if row_matches_exact(r, _expected_debtor_cells(debtor))]
    if len(matches) != 1:
        raise ManualReviewError(
            "step2.debtor.reverify",
            f"expected exactly 1 newly saved contact, found {len(matches)}",
        )
    ctx.choose_ok(table, matches[0])
    ctx.set_window(order_win)
    logger.info("step2: re-selected newly saved contact %r", debtor.search_key)


def _click_debtor_selector(ctx: FlowContext) -> None:
    """Click the Debtor selector with a 4-attempt retry loop.

    Each attempt re-stabilizes the editor because SWT can lazily drop the
    Addresses section (and its 'Open' dropdown) from the UIA tree at any
    point between interactions.
    """
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            _click_debtor_selector_once(ctx)
            return
        except (ControlNotFoundError, ManualReviewError) as exc:
            last_exc = exc
            logger.info(
                "step2: debtor selector attempt %d/4 failed (%s); re-rendering...",
                attempt + 1,
                exc,
            )
            stabilize_active_editor(ctx, wait_control="Addresses", max_retries=3)
            time.sleep(0.5)
    assert last_exc is not None
    raise last_exc


def _click_debtor_selector_once(ctx: FlowContext) -> None:
    """Open the selector through its uniquely scoped UIA semantic role."""
    Button(ctx.find("DEBTOR_SELECTOR"), ctx.waits).click()


def _confirm_addresses(ctx: FlowContext, order_win, debtor: DebtorData) -> None:
    ctx.set_window(order_win)
    try:
        section = read_text(ctx.find("DEBTOR_ADDRESS_SECTION"))
    except Exception:
        section = ""
    logger.info(
        "step2: Invoice/Delivery addresses populated for %r (section=%r)",
        debtor.search_key,
        section,
    )


def _expected_debtor_cells(debtor: DebtorData) -> list[str]:
    addr = debtor.billing_address
    return [debtor.company, debtor.first_name, debtor.last_name, addr.zip_code, addr.city]


def _force_render_section(ctx: FlowContext) -> None:
    """Force SWT to lazily render the Addresses section.

    Eclipse SWT renders some form sections only after the editor gains focus
    and the user interacts with it.  Sending Tab keypresses through the
    header fields triggers the lazy render, making the Addresses section
    (and its selector Image icons) available in the UIA tree.
    """
    try:
        ctx.window().set_focus()
    except Exception:
        pass
    # Tab through a few header fields to trigger lazy render
    for _ in range(3):
        try:
            ctx.window().type_keys("{TAB}")
            time.sleep(0.15)
        except Exception:
            break


# ---------------------------------------------------------------------------
# Creation branch
# ---------------------------------------------------------------------------


def _create_debtor(ctx: FlowContext, order_win, debtor: DebtorData) -> None:
    ctx.set_window(order_win)
    Button(ctx.find("NEW_CONTACT_BUTTON"), ctx.waits).click()
    ctx.wait_for_editor(CONTACT_EDITOR_TITLE, "DEBTOR_COMPANY")
    _fill_debtor_fields(ctx, debtor)
    _resolve_payment_method(ctx, debtor)
    ctx.save()
    ctx.set_window(order_win)
    logger.info("step2: new Debtor saved")


def _fill_debtor_fields(ctx: FlowContext, debtor: DebtorData) -> None:
    # Proposed Customer ID is left unchanged.
    Edit(ctx.find("DEBTOR_COMPANY"), ctx.waits).fill(debtor.company)
    Edit(ctx.find("DEBTOR_FIRST_NAME"), ctx.waits).fill(debtor.first_name)
    Edit(ctx.find("DEBTOR_LAST_NAME"), ctx.waits).fill(debtor.last_name)
    if debtor.salutation and debtor.salutation != "---":
        Combo(ctx.find("DEBTOR_SALUTATION"), ctx.waits).select(debtor.salutation)
    Edit(ctx.find("DEBTOR_ALIAS"), ctx.waits).fill(debtor.alias)
    # Spec 2.9 mandates Discount = 0% on the Debtor master. If the image
    # extracted a non-zero value, surface it (never silently drop it).
    if debtor.discount_percent:
        logger.warning(
            "step2: image shows debtor discount %s%% but spec 2.9 forces 0%% on the master",
            debtor.discount_percent,
        )
    Edit(ctx.find("DEBTOR_DISCOUNT"), ctx.waits).fill("0")
    select_combo_value(ctx, "DEBTOR_PRICE_MODE", "Net", "step2.debtor.price_mode")
    _fill_main_address(ctx, debtor)


def _fill_main_address(ctx: FlowContext, debtor: DebtorData) -> None:
    addr = debtor.billing_address
    ctx.find("DEBTOR_MAIN_ADDRESS")
    for role, value in (
        ("ADDRESS_STREET", addr.street),
        ("ADDRESS_ZIP", addr.zip_code),
        ("ADDRESS_CITY", addr.city),
        ("ADDRESS_COUNTRY", addr.country),
        ("ADDRESS_EMAIL", addr.email),
        ("ADDRESS_PHONE", addr.telephone),
    ):
        if value:
            Edit(ctx.find(role), ctx.waits).fill(value)
    Checkbox(ctx.find("ADDRESS_ROLE_INVOICE"), ctx.waits).set_checked(True)
    if debtor.same_delivery_address:
        Checkbox(ctx.find("ADDRESS_ROLE_DELIVERY"), ctx.waits).set_checked(True)


def _resolve_payment_method(ctx: FlowContext, debtor: DebtorData) -> None:
    method = (debtor.payment_method or "").strip()
    if not method:
        logger.info("step2: no payment method extracted; leaving combo default")
        return
    debtor_win = ctx.window()
    combo = Combo(ctx.find("DEBTOR_PAYMENT_METHOD"), ctx.waits)
    exact = [i for i in combo.items() if (i or "").strip().lower() == method.lower()]
    if len(exact) > 1:
        raise ManualReviewError(
            "step2.payment.combo",
            f"payment method {method!r} ambiguous in combo: {exact!r}",
        )
    if len(exact) == 1:
        combo.select(exact[0])
        logger.info("step2: selected existing payment method %r", exact[0])
        return
    _provision_payment_method(ctx, debtor_win, method)


def _provision_payment_method(ctx: FlowContext, debtor_win, method: str) -> None:
    ctx.menu_select("MENU_DATA", "MENU_TERMS_OF_PAYMENT", "Terms of payment")
    list_win = ctx.waits.for_window(ctx.app.desktop, PAYMENT_DIALOG_TITLE, ctx.settings.window_timeout)
    ctx.set_window(list_win)
    list_ctrl = ctx.find("PAYMENT_METHODS_LIST")
    ctx.waits.stable_count(list_ctrl)
    items = List(list_ctrl, ctx.waits).items()
    exact = [i for i in items if (i or "").strip().lower() == method.lower()]

    if len(exact) > 1:
        raise ManualReviewError(
            "step2.payment.select",
            f"{len(exact)} payment methods match {method!r}; refusing to guess",
        )
    if len(exact) == 1:
        logger.info("step2: reusing existing payment method %r", exact[0])
        ctx.cancel_dialog()
        ctx.set_window(debtor_win)
        select_combo_value(ctx, "DEBTOR_PAYMENT_METHOD", method, "step2.payment.select")
        return

    Button(ctx.find("PAYMENT_NEW_BUTTON"), ctx.waits).click()
    ctx.wait_for_editor(PAYMENT_DIALOG_TITLE, "PAYMENT_NAME")
    _fill_payment_editor(ctx, method)
    ctx.save()
    ctx.set_window(list_win)
    ctx.cancel_dialog()
    ctx.set_window(debtor_win)
    select_combo_value(ctx, "DEBTOR_PAYMENT_METHOD", method, "step2.payment.select")
    logger.info("step2: created payment method %r", method)


def _fill_payment_editor(ctx: FlowContext, method: str) -> None:
    code = payment_code_for(method)
    Edit(ctx.find("PAYMENT_NAME"), ctx.waits).fill(method)
    Edit(ctx.find("PAYMENT_DESCRIPTION"), ctx.waits).fill(method)
    Combo(ctx.find("PAYMENT_CODE"), ctx.waits).select_or_clear(code.value)
    Edit(ctx.find("PAYMENT_CASH_DISCOUNT"), ctx.waits).fill("0")
    Edit(ctx.find("PAYMENT_DISCOUNT_DAYS"), ctx.waits).fill("0")
    Edit(ctx.find("PAYMENT_NET_DAYS"), ctx.waits).fill("0")
    # Spec 2.10.5: the 'unpaid' / 'deposit' / 'paid' text fields must stay
    # blank. New records usually default to blank; blank them explicitly so a
    # prefilled default can never sneak a payment status into the method.
    for role in ("PAYMENT_UNPAID_TEXT", "PAYMENT_DEPOSIT_TEXT", "PAYMENT_PAID_TEXT"):
        try:
            Edit(ctx.find(role), ctx.waits).clear()
        except ControlNotFoundError:
            logger.info("step2: payment status text %s not present (fine)", role)
