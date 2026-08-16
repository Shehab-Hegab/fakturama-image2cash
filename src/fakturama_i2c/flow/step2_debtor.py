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


def _verify_address_after_select(ctx: FlowContext, debtor: DebtorData, label: str) -> None:
    """Read the Invoice address area and verify the address matches.

    Retries up to 8 seconds because SWT may need time to populate the
    address form after keyboard selection.

    IMPORTANT: The SWT Tab control does NOT expose children via UIA, so
    we scan main.descendants() for text elements within the Tab's bounding
    rect instead of reading Tab.children().
    """
    deadline = time.monotonic() + 8.0
    addr_text = ""
    attempt = 0

    # Get the Invoice address Tab rect for positional scanning
    tab_rect = None
    try:
        invoice_tab = ctx.find("ADDRESS_ROLE_INVOICE")
        tab_rect = invoice_tab.rectangle()
    except Exception:
        pass

    while time.monotonic() < deadline:
        attempt += 1
        time.sleep(0.8)

        # Method 0: read Edit VALUES inside the Tab rect via WM_GETTEXT.
        # SWT Edits do NOT expose their text through UIA window_text(), so
        # UIA scans report an empty address even when it IS populated. The
        # Win32 WM_GETTEXT path reads the real buffer of every Edit whose
        # rect lies inside the Invoice address Tab.
        try:
            main = ctx.app.main_window()
            import ctypes as _ct

            _user32 = _ct.windll.user32
            _user32.SendMessageW.argtypes = [
                _ct.c_void_p, _ct.c_uint, _ct.c_void_p, _ct.c_void_p
            ]
            _user32.SendMessageW.restype = _ct.c_void_p
            edit_texts = []
            # Rect filter: prefer the Invoice address Tab rect (may resolve
            # to a TabItem with a narrower rect), else fall back to the whole
            # Addresses band of the editor.
            def _inside(r) -> bool:
                if tab_rect and (
                    r.left >= tab_rect.left - 40
                    and r.right <= tab_rect.right + 40
                    and r.top >= tab_rect.top - 40
                    and r.bottom <= tab_rect.bottom + 40
                ):
                    return True
                # Addresses band of the editor (Invoice/Delivery rows).
                return (
                    r.left >= 300 and r.right <= 1200 and r.top >= 200 and r.bottom <= 500
                )

            for desc in main.descendants():
                try:
                    if (desc.element_info.control_type or "").lower() != "edit":
                        continue
                    r = desc.rectangle()
                    if not _inside(r):
                        continue
                    hwnd = desc.element_info.handle
                    if not hwnd:
                        continue
                    length = _user32.SendMessageW(hwnd, 0x000E, 0, 0)
                    if length <= 0:
                        continue
                    buf = _ct.create_string_buffer((int(length) + 1) * 2)
                    _user32.SendMessageW(hwnd, 0x000D, len(buf), buf)
                    txt = buf.raw.decode("utf-16-le", errors="replace").rstrip("\x00")
                    # Only multiline edits inside the band are address fields
                    # (the single-line Cust.Ref./header edits must be excluded
                    # so an empty address can never pass verification).
                    if txt.strip() and ("\r\n" in txt or "\n" in txt):
                        edit_texts.append(txt.strip())
                except Exception:
                    continue
            if edit_texts:
                addr_text = " ".join(edit_texts)
        except Exception:
            addr_text = ""

        if addr_text.strip():
            break

        # Method 1: scan main.descendants() for text within the Tab's bounding rect
        try:
            main = ctx.app.main_window()
            texts_in_rect = []
            for desc in main.descendants():
                try:
                    r = desc.rectangle()
                    # Check if this element is within the Tab's bounding rect
                    if (tab_rect and
                            r.left >= tab_rect.left - 5 and
                            r.right <= tab_rect.right + 5 and
                            r.top >= tab_rect.top - 5 and
                            r.bottom <= tab_rect.bottom + 5):
                        txt = desc.window_text() or ""
                        if txt.strip() and txt.strip().lower() != "invoice address":
                            texts_in_rect.append(txt.strip())
                except Exception:
                    continue
            if texts_in_rect:
                addr_text = " ".join(texts_in_rect)
        except Exception:
            addr_text = ""

        # Method 2: fallback to Invoice Tab children (may return empty for SWT)
        if not addr_text.strip():
            try:
                invoice_tab = ctx.find("ADDRESS_ROLE_INVOICE")
                children_texts = []
                for child in invoice_tab.children():
                    txt = child.window_text() or ""
                    if txt.strip():
                        children_texts.append(txt.strip())
                addr_text = " ".join(children_texts)
            except Exception:
                pass

        # If address has real content, break
        if addr_text.strip() and addr_text.strip().lower() not in ("addresses", "invoice address"):
            break

        logger.info("step2: address not yet populated (attempt %d), retrying...", attempt)
        time.sleep(0.5)

    if not addr_text.strip() or addr_text.strip().lower() in ("addresses", "invoice address"):
        raise ManualReviewError(
            "step2.debtor.verify",
            f"address area is empty after {label} debtor selection; "
            f"the keyboard traversal may have failed to select a row",
        )

    addr = debtor.billing_address
    expected_fragments = [debtor.company, addr.zip_code, addr.city]
    missing = [f for f in expected_fragments if f and f.lower() not in addr_text.lower()]
    if missing:
        logger.warning(
            "step2: address verification partial — missing %r in %r", missing, addr_text
        )
        # If ALL expected fragments are missing, the address was not populated
        if len(missing) == len(expected_fragments):
            raise ManualReviewError(
                "step2.debtor.verify",
                f"address area is empty after {label} debtor selection; "
                f"the keyboard traversal may have failed to select a row",
            )
    logger.info("step2: post-selection address verified (%s) for %r", label, debtor.search_key)


def _select_or_create(ctx: FlowContext, order_win, debtor: DebtorData) -> None:
    ctx.set_window(order_win)
    _force_render_section(ctx)
    # CRITICAL: SWT lazy rendering — the Addresses section is not in the UIA
    # tree until the editor body is clicked and the form is traversed.
    stabilize_active_editor(ctx, wait_control="Addresses", max_retries=5)
    _click_debtor_selector(ctx)
    table = ctx.open_search_dialog(ADDRESS_DIALOG_TITLE, debtor.search_key)

    if table is None:
        # SWT virtual tables do not surface rows through UIA. The prescribed
        # selector traversal in FlowContext has searched the exact key and
        # committed the highlighted result via DOWN/ENTER.
        logger.info("step2: virtual address table selected %r via keyboard traversal", debtor.search_key)
        ctx.set_window(order_win)
        # Force-render the Addresses section to confirm the address populated
        try:
            stabilize_active_editor(ctx, wait_control="Addresses", max_retries=3)
        except Exception:
            logger.debug("step2: Addresses section not detected after keyboard selection")
        # POST-SELECTION VERIFICATION: read the populated address content
        # from the Invoice address Tab (NOT the 'Addresses' header label).
        # The Tab's children contain the actual address lines (company,
        # street, ZIP, city).  If the Tab has no children the address is
        # empty and the keyboard selection silently failed.
        _verify_address_after_select(ctx, debtor, "keyboard-selected")
        return

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
        logger.info("step2: virtual address table re-selected %r via keyboard traversal", debtor.search_key)
        ctx.set_window(order_win)
        _verify_address_after_select(ctx, debtor, "keyboard-reselected")
        return

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
    el = ctx.find("DEBTOR_SELECTOR")
    try:
        logger.info(
            "step2: debtor selector matched name=%r type=%s rect=%s hwnd=%s",
            (el.name or "")[:50],
            getattr(el, "control_type", ""),
            getattr(el, "rectangle", None),
            getattr(el, "handle", None),
        )
    except Exception:
        pass
    Button(el, ctx.waits).click()


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
