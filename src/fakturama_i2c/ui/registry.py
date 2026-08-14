"""Control-discovery registry: semantic roles -> ordered finder strategies.

THE CORE IDEA (assignment's "no hardcoded coordinates / no fixed layout"):
The automation never targets pixels or fixed offsets. Instead, every UI
element the flow needs is identified by a *semantic role* (e.g.
``DEBTOR_SELECTOR``, ``NEW_ORDER_BUTTON``). Each role has an ordered list of
candidate finder strategies. The finder tries them in order and returns the
first control that matches; it raises ``ControlNotFoundError`` only after all
strategies fail.

Strategy kinds (all property-based, never positional):
  * ``auto_id``      -> UIA automation_id match (Fakturama/SWT exposes few,
                        but when present it is the strongest signal)
  * ``control_type`` -> UIA control_type match, optionally combined with name
  * ``name``         -> window text / accessible name match (regex optional)
  * ``class``        -> SWT class name (e.g. "SWT.Button"), last resort

Disambiguation of visually-similar controls (upper "select address" icon vs.
lower green "+" new-contact button) is handled by ``scope`` + ``in_ancestor``:
the strategy is only searched inside a named ancestor control, never by
position.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from ..config import Settings
from ..utils.errors import ControlNotFoundError, ManualReviewError

# ---------------------------------------------------------------------------
# Strategy model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Strategy:
    kind: str  # "auto_id" | "control_type" | "name" | "class"
    value: str
    regex: bool = False
    # Restrict search to a subtree identified by this ancestor (name/auto_id).
    in_ancestor: Optional[str] = None
    ancestor_kind: str = "name"
    # When >1 control matches this strategy, treat as ambiguity => manual review
    require_unique: bool = True
    # Extra exact (case-insensitive) name filter, combined with kind. Used to
    # disambiguate same-type controls (e.g. the VAT-mode ComboBox named 'VAT'
    # vs the totals VAT Edit / Shipping ComboBox) without a full name strategy.
    name_filter: Optional[str] = None

    def to_child_window_kwargs(self) -> dict[str, Any]:
        if self.kind == "auto_id":
            return {"auto_id": self.value}
        if self.kind == "control_type":
            kwargs = {"control_type": self.value}
            if self.name_filter:
                kwargs["name"] = self.name_filter
            return kwargs
        if self.kind == "name":
            if self.regex:
                return {"title_re": self.value}
            return {"title": self.value}
        if self.kind == "class":
            return {"class_name": self.value}
        raise ValueError(f"unknown strategy kind: {self.kind}")


@dataclass
class Role:
    """A semantic role plus its ordered candidate strategies."""

    name: str
    strategies: list[Strategy] = field(default_factory=list)

    def add(self, strategy: Strategy) -> "Role":
        self.strategies.append(strategy)
        return self


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

# Known roles (documented in docs/DESIGN_DOC.md). Values are *default* guesses
# for Fakturama (Eclipse SWT RCP). Because SWT exposes sparse automation_ids,
# the realistic matches come from control_type + name/class scoped by ancestor.
# The registry is intentionally data, so it can be tuned from an element dump
# (see `python -m fakturama_i2c.ui.registry --dump`) without touching code.
#
# NOTE: real Fakturama automation_id values must be confirmed from a live dump
# during bring-up; the entries below are the *fallback chain* and the audit
# trail the finder follows.

def default_registry() -> dict[str, Role]:
    return {
        # -- top toolbar ---------------------------------------------------
        # Real Fakturama tooltips: 'Create: New Order', 'Create: New Invoice',
        # 'Create a new product', 'Create a new contact'. The toolbar buttons
        # expose no automation_id, so the tooltip name is the reliable signal.
        "ORDER_NEW_BUTTON": Role("ORDER_NEW_BUTTON").add(
            Strategy("auto_id", "order.new"),
        ).add(Strategy("name", r"(?i)create.*new order", regex=True)),
        "INVOICE_NEW_BUTTON": Role("INVOICE_NEW_BUTTON").add(
            Strategy("auto_id", "invoice.new"),
        ).add(Strategy("name", r"(?i)create.*new invoice", regex=True)),

        # -- Order editor ---------------------------------------------------
        # auto_ids are per-instance SWT ids (strongest when present); name
        # strategies remain the cross-session fallback chain.
        "ORDER_DATE": Role("ORDER_DATE").add(
            Strategy("auto_id", "5046524"),
        ).add(Strategy("name", "Date", regex=True)),
        "ORDER_REFERENCE": Role("ORDER_REFERENCE").add(
            Strategy("auto_id", "133892"),
        ).add(Strategy("name", "Cust.Ref.", regex=True)).add(Strategy("name", "Ref.", regex=True)),
        # Price-mode ComboBox has NO name; it is the first ComboBox in the
        # New Order editor (before the VAT-mode ComboBox and the Shipping
        # ComboBox). require_unique=False picks matches[0] = the price-mode box.
        "ORDER_PRICE_MODE": Role("ORDER_PRICE_MODE").add(
            Strategy("auto_id", "133914"),
        ).add(Strategy("control_type", "ComboBox", require_unique=False)).add(
            Strategy("name", "Price", regex=True)
        ),
        # VAT-mode ComboBox is the only ComboBox named 'VAT' (the totals VAT
        # field is an Edit, the Shipping box is named 'Shipping'). NOTE: do NOT
        # add a bare name 'VAT' fallback -- it matches the totals-VAT Edit and
        # Combo.select would then write into a text field. ComboBox-scoped
        # strategies only; if the box is not exposed (lazy SWT rendering) the
        # role raises ControlNotFoundError, which the flow surfaces honestly.
        "ORDER_VAT_MODE": Role("ORDER_VAT_MODE").add(
            Strategy("control_type", "ComboBox", name_filter="VAT"),
        ).add(Strategy("auto_id", "134134")),
        # The UPPER icon (existing contact) beside the Addresses section.
        # Distinct from the LOWER green "+" (new debtor) via ancestor scope
        # and name (tooltip). Order matters: the strongest signal first.
        # The bare control_type fallback is deliberate: if the flow reaches it
        # the finder finds MANY buttons -> ManualReviewError (never a guess).
        "DEBTOR_SELECTOR": Role("DEBTOR_SELECTOR").add(
            Strategy("name", r"(?i)(select|choose|address).*(contact|customer|address)", regex=True),
        ).add(
            Strategy("control_type", "Image", in_ancestor="Addresses", ancestor_kind="name", require_unique=False),
        ).add(
            Strategy("control_type", "Button", in_ancestor="Addresses", ancestor_kind="name"),
        ).add(
            Strategy("control_type", "Button"),
        ).add(Strategy("class", "SWT.Button")),
        "DEBTOR_ADDRESS_SECTION": Role("DEBTOR_ADDRESS_SECTION").add(
            Strategy("name", "Addresses", regex=True),
        ),

        # -- New Debtor editor ----------------------------------------------
        "DEBTOR_COMPANY": Role("DEBTOR_COMPANY").add(
            Strategy("name", "Company", regex=True),
        ),
        "DEBTOR_FIRST_NAME": Role("DEBTOR_FIRST_NAME").add(
            Strategy("name", "First Name", regex=True),
        ),
        "DEBTOR_LAST_NAME": Role("DEBTOR_LAST_NAME").add(
            Strategy("name", "Name", regex=True),
        ),
        "DEBTOR_SALUTATION": Role("DEBTOR_SALUTATION").add(
            Strategy("name", "Salutation", regex=True),
        ),
        "DEBTOR_MAIN_ADDRESS": Role("DEBTOR_MAIN_ADDRESS").add(
            Strategy("name", "Main address", regex=True),
        ),
        "ADDRESS_STREET": Role("ADDRESS_STREET").add(
            Strategy("name", "Street", regex=True),
        ),
        "ADDRESS_ZIP": Role("ADDRESS_ZIP").add(
            Strategy("name", "ZIP", regex=True),
        ),
        "ADDRESS_CITY": Role("ADDRESS_CITY").add(
            Strategy("name", "City", regex=True),
        ),
        "ADDRESS_COUNTRY": Role("ADDRESS_COUNTRY").add(
            Strategy("name", "Country", regex=True),
        ),
        "ADDRESS_EMAIL": Role("ADDRESS_EMAIL").add(
            Strategy("name", "E-Mail", regex=True),
        ),
        "ADDRESS_PHONE": Role("ADDRESS_PHONE").add(
            Strategy("name", "Telephone", regex=True),
        ),
        "ADDRESS_ROLE_INVOICE": Role("ADDRESS_ROLE_INVOICE").add(
            Strategy("name", "Invoice address", regex=True),
        ),
        "ADDRESS_ROLE_DELIVERY": Role("ADDRESS_ROLE_DELIVERY").add(
            Strategy("name", "Delivery address", regex=True),
        ),
        "DEBTOR_ALIAS": Role("DEBTOR_ALIAS").add(
            Strategy("name", "Alias", regex=True),
        ),
        "DEBTOR_DISCOUNT": Role("DEBTOR_DISCOUNT").add(
            Strategy("name", "Discount", regex=True),
        ),
        "DEBTOR_PRICE_MODE": Role("DEBTOR_PRICE_MODE").add(
            Strategy("name", "Net", regex=True),
        ).add(Strategy("name", "Gross", regex=True)),
        "DEBTOR_PAYMENT_METHOD": Role("DEBTOR_PAYMENT_METHOD").add(
            Strategy("name", "Payment", regex=True),
        ),
        "NEW_CONTACT_BUTTON": Role("NEW_CONTACT_BUTTON").add(
            Strategy("name", r"(?i)create.*contact", regex=True),
        ).add(Strategy("auto_id", "contact.new")),

        # -- terms of payment (Data > terms of payment) --------------------
        "PAYMENT_METHODS_DIALOG": Role("PAYMENT_METHODS_DIALOG").add(
            Strategy("name", "Terms of payment", regex=True),
        ),
        "PAYMENT_METHODS_LIST": Role("PAYMENT_METHODS_LIST").add(
            Strategy("control_type", "List"),
        ).add(Strategy("class", "SWT.List")),
        "PAYMENT_NEW_BUTTON": Role("PAYMENT_NEW_BUTTON").add(
            Strategy("name", "New", regex=True),
        ),
        "PAYMENT_NAME": Role("PAYMENT_NAME").add(
            Strategy("name", "Name", regex=True),
        ),
        "PAYMENT_DESCRIPTION": Role("PAYMENT_DESCRIPTION").add(
            Strategy("name", "Description", regex=True),
        ),
        "PAYMENT_CODE": Role("PAYMENT_CODE").add(
            Strategy("name", "Code", regex=True),
        ),
        "PAYMENT_CASH_DISCOUNT": Role("PAYMENT_CASH_DISCOUNT").add(
            Strategy("name", "Cash discount", regex=True),
        ),
        "PAYMENT_DISCOUNT_DAYS": Role("PAYMENT_DISCOUNT_DAYS").add(
            Strategy("name", "Discount Days", regex=True),
        ),
        "PAYMENT_NET_DAYS": Role("PAYMENT_NET_DAYS").add(
            Strategy("name", "Net Days", regex=True),
        ),
        "PAYMENT_UNPAID_TEXT": Role("PAYMENT_UNPAID_TEXT").add(
            Strategy("name", "Unpaid", regex=True),
        ),
        "PAYMENT_DEPOSIT_TEXT": Role("PAYMENT_DEPOSIT_TEXT").add(
            Strategy("name", "Deposit", regex=True),
        ),
        "PAYMENT_PAID_TEXT": Role("PAYMENT_PAID_TEXT").add(
            Strategy("name", "Paid", regex=True),
        ),
        "PAYMENT_SET_STANDARD": Role("PAYMENT_SET_STANDARD").add(
            Strategy("name", "Set as standard", regex=True),
        ),

        # -- Product selection / editor --------------------------------------
        # The UPPER product-selection icon beside the Items table. Distinct
        # from the green "+" (new product) via ancestor scope + name.
        "PRODUCT_SELECTOR": Role("PRODUCT_SELECTOR").add(
            Strategy("name", r"(?i)(select|search).*(product|item)", regex=True),
        ).add(
            Strategy("control_type", "Image", in_ancestor="Items", ancestor_kind="name", require_unique=False),
        ).add(
            Strategy("control_type", "Button", in_ancestor="Items", ancestor_kind="name"),
        ).add(
            Strategy("control_type", "Button"),
        ).add(Strategy("class", "SWT.Button")),
        "PRODUCT_NEW_BUTTON": Role("PRODUCT_NEW_BUTTON").add(
            Strategy("name", r"(?i)create.*product", regex=True),
        ).add(Strategy("auto_id", "product.new")),
        "PRODUCT_ITEM_NUMBER": Role("PRODUCT_ITEM_NUMBER").add(
            Strategy("name", "Item Number", regex=True),
        ).add(Strategy("name", "No.", regex=True)),
        "PRODUCT_NAME": Role("PRODUCT_NAME").add(
            Strategy("name", "Name", regex=True),
        ),
        "PRODUCT_DESCRIPTION": Role("PRODUCT_DESCRIPTION").add(
            Strategy("name", "Description", regex=True),
        ),
        "PRODUCT_PRICE_GROSS": Role("PRODUCT_PRICE_GROSS").add(
            Strategy("name", "Price (gross)", regex=True),
        ).add(Strategy("name", "Price", regex=True)),
        "PRODUCT_COST_PRICE": Role("PRODUCT_COST_PRICE").add(
            Strategy("name", "cost price", regex=True),
        ),
        "PRODUCT_VAT": Role("PRODUCT_VAT").add(
            Strategy("name", "VAT", regex=True),
        ),
        "PRODUCT_STOCK": Role("PRODUCT_STOCK").add(
            Strategy("name", "Stock", regex=True),
        ),

        # -- VAT editor -------------------------------------------------------
        "VAT_EDITOR_NAME": Role("VAT_EDITOR_NAME").add(
            Strategy("name", "Name", regex=True),
        ),
        "VAT_EDITOR_DESCRIPTION": Role("VAT_EDITOR_DESCRIPTION").add(
            Strategy("name", "Description", regex=True),
        ),
        "VAT_EDITOR_CODE": Role("VAT_EDITOR_CODE").add(
            Strategy("name", "VAT code (E-Invoice)", regex=True),
        ).add(Strategy("name", "E-Invoice", regex=True)),
        "VAT_EDITOR_VALUE": Role("VAT_EDITOR_VALUE").add(
            Strategy("name", "Value", regex=True),
        ),
        "VAT_EDITOR_STANDARD": Role("VAT_EDITOR_STANDARD").add(
            Strategy("name", "Standard VAT", regex=True),
        ),
        "VAT_NEW_BUTTON": Role("VAT_NEW_BUTTON").add(
            Strategy("control_type", "Button"),
        ).add(Strategy("name", "New", regex=True)),

        # -- Order body -------------------------------------------------------
        "ORDER_ITEMS_TABLE": Role("ORDER_ITEMS_TABLE").add(
            Strategy("control_type", "Table"),
        ).add(Strategy("class", "SWT.Table")),
        "ORDER_DISCOUNT": Role("ORDER_DISCOUNT").add(
            Strategy("auto_id", "3149378"),
        ).add(Strategy("name", "Discount", regex=True)),
        "ORDER_SHIPPING": Role("ORDER_SHIPPING").add(
            Strategy("auto_id", "2753640"),
        ).add(Strategy("name", "Shipping", regex=True)),
        "ORDER_TOTAL_NET": Role("ORDER_TOTAL_NET").add(
            Strategy("auto_id", "4065694"),
        ).add(Strategy("name", "Total Gross", regex=True)).add(Strategy("name", "Total Net", regex=True)),
        # Totals VAT is the only Edit named 'VAT' (the VAT-mode ComboBox is a
        # ComboBox, excluded by the Edit control_type filter).
        "ORDER_TOTAL_VAT": Role("ORDER_TOTAL_VAT").add(
            Strategy("control_type", "Edit", name_filter="VAT"),
        ).add(Strategy("auto_id", "462516")).add(
            Strategy("name", "VAT", regex=True)
        ).add(Strategy("name", "Total VAT", regex=True)),
        "ORDER_TOTAL": Role("ORDER_TOTAL").add(
            Strategy("control_type", "Edit", name_filter="Total"),
        ).add(Strategy("auto_id", "2688134")).add(Strategy("name", "Total", regex=True)),

        # -- Invoice editor ----------------------------------------------------
        "INVOICE_PAYMENT_METHOD": Role("INVOICE_PAYMENT_METHOD").add(
            Strategy("name", "Payment", regex=True),
        ),
        "INVOICE_PAID_STATUS": Role("INVOICE_PAID_STATUS").add(
            Strategy("name", "Paid", regex=True),
        ),
        "INVOICE_PAYMENT_DATE": Role("INVOICE_PAYMENT_DATE").add(
            Strategy("name", "Payment date", regex=True),
        ),
        "INVOICE_PAYMENT_VALUE": Role("INVOICE_PAYMENT_VALUE").add(
            Strategy("name", "Value", regex=True),
        ),

        # -- common ------------------------------------------------------------
        "SAVE_BUTTON": Role("SAVE_BUTTON").add(
            Strategy("name", "Save", regex=True),
        ).add(Strategy("auto_id", "save")),
        "OK_BUTTON": Role("OK_BUTTON").add(
            Strategy("name", "OK", regex=True),
        ),
        "CANCEL_BUTTON": Role("CANCEL_BUTTON").add(
            Strategy("name", "Cancel", regex=True),
        ),
        "SEARCH_EDIT": Role("SEARCH_EDIT").add(
            Strategy("control_type", "Edit"),
        ).add(Strategy("class", "SWT.Text")),
        "RESULT_TABLE": Role("RESULT_TABLE").add(
            Strategy("control_type", "Table"),
        ).add(Strategy("class", "SWT.Table")),
        "FOLLOW_UP_INVOICE": Role("FOLLOW_UP_INVOICE").add(
            Strategy(
                "name",
                r"(?i)invoice",
                regex=True,
                in_ancestor="Create a follow-up document",
                ancestor_kind="name",
            ),
        ).add(Strategy(
            "control_type",
            "Button",
            in_ancestor="Create a follow-up document",
            ancestor_kind="name",
        )).add(Strategy("name", r"(?i)follow.?up.*invoice", regex=True)),
        "MENU_DATA": Role("MENU_DATA").add(
            Strategy("name", "Data", regex=True),
        ),
        "MENU_TERMS_OF_PAYMENT": Role("MENU_TERMS_OF_PAYMENT").add(
            Strategy("control_type", "MenuItem", in_ancestor="Data", ancestor_kind="name"),
        ).add(Strategy("name", "Terms of payment", regex=True)),
        "MENU_VATS": Role("MENU_VATS").add(
            Strategy("control_type", "MenuItem", in_ancestor="Data", ancestor_kind="name"),
        ).add(Strategy("name", "VATs", regex=True)),
        "MENU_DOCUMENTS": Role("MENU_DOCUMENTS").add(
            Strategy("control_type", "MenuItem", in_ancestor="Data", ancestor_kind="name"),
        ).add(Strategy("name", "Documents", regex=True)),
        "DOCUMENTS_TABLE": Role("DOCUMENTS_TABLE").add(
            Strategy("control_type", "Table"),
        ).add(Strategy("class", "SWT.Table")),
    }


# ---------------------------------------------------------------------------
# Finder
# ---------------------------------------------------------------------------


def _control_alive(control: Any) -> bool:
    """Backend-agnostic liveness check (UIA wrappers have no ``exists()``)."""
    check = getattr(control, "exists", None)
    if callable(check):
        try:
            return bool(check(timeout=0.5))
        except Exception:
            return True
    try:
        return bool(control.is_visible())
    except Exception:
        return True


class ControlFinder:
    """Resolves semantic roles to live pywinauto controls via the registry."""

    def __init__(self, settings: Settings, registry: Optional[dict[str, Role]] = None) -> None:
        self.settings = settings
        self.registry = registry or default_registry()
        # (window identity, role) -> cached resolved control.
        # Keyed by window object identity so two editor tabs inside the same
        # HWND (Fakturama opens editors as tabs, all sharing the main HWND)
        # never leak cached controls across panes.
        self._cache: dict[tuple[int, str], Any] = {}

    def resolve(self, window, role: str) -> Any:
        """Return the pywinauto control for ``role`` inside ``window``.

        Uses the cached control if present, otherwise walks the ordered
        strategy list. Raises ControlNotFoundError if every strategy fails or
        the ancestor scope is missing.
        """
        key = (id(window), role)
        if key in self._cache:
            cached = self._cache[key]
            if _control_alive(cached):
                return cached
            del self._cache[key]

        role_def = self.registry.get(role)
        if role_def is None:
            raise ControlNotFoundError(f"unknown role: {role}")

        for strategy in role_def.strategies:
            matches = self._match(window, strategy)
            if not matches:
                continue
            if strategy.require_unique and len(matches) > 1:
                # Tie => ambiguity. Per spec we must not guess. But when a
                # label Static and its value Edit share a name (SWT pattern),
                # prefer the interactive field (Edit/Combo) if exactly one
                # matches -- a property-based, never positional, tie-break.
                interactive = self._prefer_interactive(matches)
                if len(interactive) != 1:
                    raise ManualReviewError(
                        role,
                        f"strategy {strategy} matched {len(matches)} controls; "
                        "refine the registry (ancestor scope) before continuing",
                    )
                matches = interactive
            control = matches[0]
            self._cache[key] = control
            return control

        raise ControlNotFoundError(
            f"role '{role}' not found; tried {len(role_def.strategies)} strategies"
        )

    def find_all(self, window, role: str) -> list[Any]:
        role_def = self.registry.get(role)
        if role_def is None:
            raise ControlNotFoundError(f"unknown role: {role}")
        for strategy in role_def.strategies:
            matches = self._match(window, strategy)
            if matches:
                return matches
        return []

    def clear_cache(self) -> None:
        self._cache.clear()

    def _match(self, window, strategy: Strategy) -> list[Any]:
        """Enumerate controls under ``window`` matching ``strategy``.

        Uses ``descendants(**criteria)`` on the scope wrapper/spec -- the
        pywinauto API that returns the matching controls themselves (a list of
        wrapper objects). This is deliberately NOT ``child_window(...).children()``
        which resolves the *last* match and returns *its* children.
        """
        scope = window
        if strategy.in_ancestor:
            ancestor = self._find_ancestor(window, strategy)
            if ancestor is None:
                return []
            scope = ancestor
        kwargs = strategy.to_child_window_kwargs()
        try:
            results = list(scope.descendants(**kwargs))
        except TypeError:
            # Some wrappers expose descendants() without criteria kwargs.
            results = list(scope.descendants())
            results = [r for r in results if self._criteria_match(r, kwargs)]
        except Exception:
            return []
        results = [r for r in results if self._visible(r)]
        return results

    def _find_ancestor(self, window, strategy: Strategy) -> Optional[Any]:
        kwargs = (
            {"title": strategy.in_ancestor}
            if strategy.ancestor_kind == "name"
            else {"auto_id": strategy.in_ancestor}
        )
        try:
            spec = window.child_window(**kwargs)
            if spec.exists(timeout=0.5):
                return spec
        except Exception:
            pass
        return None

    @staticmethod
    def _prefer_interactive(matches: list[Any]) -> list[Any]:
        """Keep only Edit/Combo controls when a label/value name-tie occurs."""
        interactive = []
        for ctrl in matches:
            ctype = ""
            try:
                ctype = (ctrl.element_info.control_type or "").lower()
            except Exception:
                pass
            if ctype in ("edit", "combo", "combobox") or ctype.startswith("combo"):
                interactive.append(ctrl)
        return interactive or matches

    @staticmethod
    def _criteria_match(control, kwargs: dict[str, Any]) -> bool:
        """Duck-typed criteria check used by the ``descendants()`` fallback."""
        import re

        for key, value in kwargs.items():
            if key == "title_re":
                title = ""
                try:
                    title = control.window_text()
                except Exception:
                    pass
                if not title:
                    info = getattr(control, "element_info", None)
                    if info is not None:
                        title = getattr(info, "name", "") or ""
                if not re.search(value, title, re.IGNORECASE):
                    return False
            elif key == "name":
                title = ""
                try:
                    title = control.window_text()
                except Exception:
                    pass
                if not title:
                    info = getattr(control, "element_info", None)
                    if info is not None:
                        title = getattr(info, "name", "") or ""
                if (title or "").strip().lower() != str(value).strip().lower():
                    return False
            elif getattr(control, key, None) != value:
                return False
        return True

    @staticmethod
    def _visible(control) -> bool:
        try:
            if not control.is_visible():
                return False
            r = control.rectangle()
            return r.width() > 0 and r.height() > 0
        except Exception:
            return True


def matches_exact(name: str, candidates: Iterable[str]) -> list[str]:
    """Exact (case-insensitive, trimmed) matching used by select dialogs."""
    needle = (name or "").strip().lower()
    return [c for c in candidates if (c or "").strip().lower() == needle]