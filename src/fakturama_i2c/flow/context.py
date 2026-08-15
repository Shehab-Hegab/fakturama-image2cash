"""Flow context: shared state + the recurring UI patterns for the five steps.

``FlowContext`` is the single object threaded through every step. It owns the
app/waits/finder references, the extracted order, the still-open editor window
anchor (``set_window``), and a small checkpoint log (``done_steps``). The module
functions below encode the exact-match-only row policy and the document-row
heuristics so the steps stay declarative.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from ..config import Settings
from ..extraction.extractor import ExtractionReport
from ..models import ExtractedOrder
from ..ui.app import AppController
from ..ui.elements import Button, Combo, Dialog, Edit, Menu, Table, parse_decimal
from ..ui.registry import ControlFinder, matches_exact
from ..ui.waits import Waits
from ..utils.errors import ControlNotFoundError, FlowTimeoutError, ManualReviewError
from ..utils.logging import get_logger

logger = get_logger("flow.context")

# Role-name prefix -> window-title fragment that identifies the role's editor.
_ROLE_TITLE_HINTS: dict[str, str] = {
    "ORDER_": "Order",
    "DEBTOR_": "Contact",
    "ADDRESS_": "Contact",
    "PRODUCT_": "Product",
    "VAT_": "VAT",
    "PAYMENT_": "Terms of payment",
    "INVOICE_": "Invoice",
}

# State tokens seen in the Data > Documents grid (row classification helper).
DOC_STATE_TOKENS = frozenset(
    {"open", "closed", "paid", "unpaid", "deposit", "accepted", "sent", "cancelled", "draft"}
)


@dataclass
class FlowContext:
    settings: Settings
    app: AppController
    waits: Waits
    finder: ControlFinder
    extracted: ExtractedOrder
    report: ExtractionReport
    done_steps: list[str] = field(default_factory=list)
    _current_window: Any = field(default=None, repr=False)

    # -- windows -------------------------------------------------------------

    def window(self) -> Any:
        """The active editor window: last set by a step, else the main window."""
        if self._current_window is not None:
            return self._current_window
        return self.app.main_window()

    def set_window(self, window: Any) -> None:
        """Switch the active editor window and drop stale finder caches."""
        new_key = self._win_key(window)
        old_key = self._win_key(self._current_window)
        if new_key is not None and new_key == old_key:
            self._current_window = window
            return
        self.finder.clear_cache()
        self._current_window = window

    def find(self, role: str) -> Any:
        """Resolve ``role`` inside the current window.

        If the current window (an editor Pane) cannot find the role, falls
        back to the main window.  This handles SWT lazy-rendered sections
        (Addresses, Items, etc.) whose ancestor labels are only discoverable
        from the full main-window tree rather than a Pane wrapper.
        """
        try:
            return self.finder.resolve(self.window(), role)
        except (ControlNotFoundError, ManualReviewError):
            try:
                main = self.app.main_window()
            except Exception:
                raise
            if id(main) != id(self.window()):
                return self.finder.resolve(main, role)
            raise

    def resolve_window(self, role: str) -> Optional[Any]:
        """Return an open window whose title hints at ``role``'s editor, else None."""
        hint = _title_hint(role)
        if not hint:
            return None
        candidates: list[Any] = list(self.app.dialogs())
        try:
            candidates.append(self.app.main_window())
        except Exception:
            pass
        for win in candidates:
            try:
                if hint.lower() in win.window_text().lower():
                    return win
            except Exception:
                continue
        return None

    def mark(self, step: str) -> None:
        """Append a step to the checkpoint log (idempotent)."""
        if step not in self.done_steps:
            self.done_steps.append(step)
        logger.info("checkpoint: %s", step)

    # -- shared UI patterns ---------------------------------------------------

    def open_search_dialog(self, dialog_title: str, search_text: str) -> Any:
        """Wait for a select dialog, search it, and return the stabilized table.

        Fakturama's select dialogs (e.g. "Select a product", "Select the
        address") are **Eclipse SWT modal child dialogs** that may not appear
        as top-level desktop windows.  ``for_window`` only scans top-level
        windows, so when it times out we fall back to scanning the main
        window's ``descendants()`` for a control whose ``window_text()``
        matches ``dialog_title``.

        When RESULT_TABLE (SWT Table) is invisible to UIA, falls back to
        keyboard-based selection: type in SEARCH_EDIT → TAB to first row →
        ENTER to confirm.
        """
        try:
            win = self.waits.for_window(self.app.desktop, dialog_title, self.settings.window_timeout)
        except FlowTimeoutError:
            # Fallback 1: find a modal child dialog inside the main window.
            try:
                win = self._find_child_dialog(dialog_title)
            except FlowTimeoutError:
                # Fallback 2: scan all top-level windows for the dialog
                # (Eclipse SWT modal dialogs may not be desktop.window() children)
                win = self._find_pid_dialog(dialog_title)
        self.set_window(win)

        # Try standard approach: find RESULT_TABLE and SEARCH_EDIT
        try:
            self.waits.stable_snapshot(self.find("RESULT_TABLE"))
            Edit(self.find("SEARCH_EDIT"), self.waits).fill(search_text)
            self.waits.stable_snapshot(self.find("RESULT_TABLE"))
            return Table(self.find("RESULT_TABLE"), self.waits)
        except ControlNotFoundError:
            pass

        # Fallback: RESULT_TABLE invisible to UIA (SWT lazy rendering).
        # Use keyboard: type search → DOWN arrow to first row → ENTER to select.
        # NOTE: {TAB} in SWT dialogs moves focus to OK/Cancel buttons, NOT the
        # table. The {DOWN} arrow moves focus directly into the first row.
        logger.info("open_search_dialog: RESULT_TABLE invisible, using keyboard fallback ({DOWN}{ENTER})")
        # Stage 1: type the search term — a failure HERE is real and fatal.
        try:
            search_edit = self.find("SEARCH_EDIT")
            Edit(search_edit, self.waits).fill(search_text)
            time.sleep(0.5)
        except Exception as exc:
            raise ManualReviewError(
                "search_dialog.keyboard",
                f"keyboard fallback for '{dialog_title}' failed at SEARCH_EDIT stage: {exc}",
            ) from exc
        # Stage 2: SWT virtual tables do not expose rows through UIA.  The
        # documented, layout-independent traversal is search -> DOWN -> ENTER.
        # This deliberately never synthesizes a mouse coordinate.
        try:
            win.set_focus()
            time.sleep(0.2)
            win.type_keys("{DOWN}")
            time.sleep(0.3)
            win.type_keys("{ENTER}")
        except Exception as exc:
            raise ManualReviewError(
                "search_dialog.keyboard",
                f"keyboard selection for '{dialog_title}' failed: {exc}",
            ) from exc
        time.sleep(0.5)
        return None  # Caller must handle None return (table not readable)

    def _find_child_dialog(self, dialog_title: str):
        """Scan main-window descendants for a dialog whose title matches.

        Returns the **container** (Pane / Window / Dialog) that holds the
        title label — NOT the title-text element itself, because
        ``descendants(control_type="Table")`` on a Text element yields nothing.
        """
        import time as _time
        from ..utils.errors import FlowTimeoutError as _FTE
        main = self.app.main_window()
        deadline = _time.monotonic() + self.settings.window_timeout
        title_lower = dialog_title.lower()
        while _time.monotonic() < deadline:
            try:
                # Collect ALL descendants whose window_text matches.
                matches = []
                for desc in main.descendants():
                    try:
                        wt = (desc.window_text() or "").strip()
                        if wt and title_lower in wt.lower():
                            matches.append(desc)
                    except Exception:
                        continue
                # Prefer a container (has children) over a leaf text element.
                for m in matches:
                    try:
                        ct = getattr(getattr(m, "element_info", None), "control_type", "")
                    except Exception:
                        ct = ""
                    if ct in ("Pane", "Window", "Dialog", "TabItem", "Tab"):
                        return m
                # No container found — try the parent of the first leaf match.
                for m in matches:
                    try:
                        parent = m.parent()
                        if parent is not None:
                            return parent
                    except Exception:
                        pass
                # Last resort: return whatever we found (will fail later with
                        # a clear RESULT_TABLE error instead of a silent hang).
                if matches:
                    return matches[0]
            except Exception:
                pass
            _time.sleep(0.5)
        raise _FTE(
            f"window '{dialog_title}'",
            "not found as top-level window or modal child of main window",
        )

    def _find_pid_dialog(self, dialog_title: str):
        """Scan top-level desktop windows matching the Fakturama PID and title.

        Eclipse SWT/RCP modal dialogs are real top-level windows but may not
        be found by ``desktop.window(title_re=...)`` due to timing.  This
        method enumerates ALL desktop windows and checks both the title and
        the owning process ID.
        """
        import time as _time
        from ..utils.errors import FlowTimeoutError as _FTE
        try:
            target_pid = self.app.app.process  # pywinauto Application PID
        except Exception:
            target_pid = None
        deadline = _time.monotonic() + self.settings.window_timeout
        title_lower = dialog_title.lower()
        while _time.monotonic() < deadline:
            for win in self.app.desktop.windows():
                try:
                    wt = (win.window_text() or "").strip()
                    if not wt:
                        continue
                    # Title match (case-insensitive substring)
                    if title_lower not in wt.lower():
                        continue
                    # PID match (if we know it)
                    if target_pid is not None:
                        try:
                            w_pid = win.process_id()
                            if w_pid != target_pid:
                                continue
                        except Exception:
                            pass
                    return win
                except Exception:
                    continue
            _time.sleep(0.5)
        raise _FTE(
            f"window '{dialog_title}'",
            "not found as top-level window, modal child, or PID dialog",
        )

    def cancel_dialog(self) -> None:
        """Close the current dialog via Cancel, falling back to a hard close."""
        try:
            Button(self.find("CANCEL_BUTTON"), self.waits).click()
        except ControlNotFoundError:
            Dialog(self.window(), self.waits).close()

    def menu_select(self, top_role: str, item_role: str, item_text: str) -> None:
        """Pick ``item_text`` under the ``top_role`` menu (with click fallbacks).

        Uses multiple strategies because Eclipse SWT RCP MenuBar items are
        often not accessible via pywinauto's built-in ``menu_select()`` or
        ``Menu.select_item()``.
        """
        # Strategy 1: built-in menu_select on the main window (simplest path).
        try:
            self.app.main_window().menu_select(f"Data->{item_text}")
            time.sleep(0.3)
            return
        except Exception as exc:
            logger.debug("menu_select(path %r) failed: %s", item_text, exc)

        # Strategy 2: bare menu_select (item only, no path).
        try:
            self.app.main_window().menu_select(item_text)
            time.sleep(0.3)
            return
        except Exception as exc:
            logger.debug("menu_select(bare %r) failed: %s", item_text, exc)

        # Strategy 3: resolve the top-level MenuItem, click it, then click
        # the sub-item by name once the menu popup expands.
        win = self.window()
        try:
            top_ctrl = self._resolve_anywhere(win, top_role)
            Button(top_ctrl, self.waits).click()
            time.sleep(0.5)
            # After the top menu expands, resolve the sub-item from the main
            # window (the popup is a child of the main window).
            item_ctrl = self._resolve_anywhere(self.app.main_window(), item_role)
            Button(item_ctrl, self.waits).click()
            time.sleep(0.3)
            return
        except Exception as exc:
            logger.debug("menu_select(click %r) failed: %s", item_text, exc)

        raise ControlNotFoundError(
            "menu_select",
            f"could not navigate to {item_text!r} under {top_role!r} "
            "via UIA menu strategies (path, bare, click)",
        )

    def _find_nav_item(self, item_text: str):
        """Find a Text control in Fakturama's left Navigation View by text."""
        try:
            main = self.app.main_window()
            for ctrl in main.descendants(control_type="Text"):
                try:
                    wt = (ctrl.window_text() or "").strip()
                    if wt.lower() == item_text.lower():
                        rect = ctrl.rectangle()
                        # Navigation panel items are in the left 383px
                        if rect.left < 383 and rect.right < 400:
                            return ctrl
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def save(self) -> None:
        """Click the current window's Save button."""
        Button(self.find("SAVE_BUTTON"), self.waits).click()

    def choose_ok(self, table: Table, row_index: int) -> None:
        """Select ``row_index`` in a dialog table and confirm with OK."""
        table.select_row(row_index)
        Button(self.find("OK_BUTTON"), self.waits).click()

    def wait_for_editor(self, title: str, probe_role: str) -> Any:
        """Wait for an editor (dialog window OR tab pane) matching ``title``
        that also exposes ``probe_role``.

        Fakturama opens Order/Invoice/Product/Contact editors as *tabs inside
        the main window* -- they are not top-level windows. We therefore scan
        the desktop for a matching dialog first, then the main window's
        tab/pane descendants. Never binds to the first partial title match;
        the ``probe_role`` must resolve inside the candidate.
        """
        deadline = time.monotonic() + self.settings.window_timeout
        while time.monotonic() < deadline:
            # 1) top-level dialogs (e.g. 'Terms of payment', 'VATs')
            for win in self.app.desktop.windows():
                try:
                    text = win.window_text()
                except Exception:
                    continue
                if title.lower() not in (text or "").lower():
                    continue
                try:
                    self.finder.resolve(win, probe_role)
                except Exception:
                    continue
                self.set_window(win)
                return win
            # 2) editor tab/pane inside the main window
            pane = self._find_editor_pane(title, probe_role)
            if pane is not None:
                return pane
            time.sleep(0.3)
        raise ControlNotFoundError(probe_role, f"editor '{title}' not found")

    def _find_editor_pane(self, title: str, probe_role: str) -> Optional[Any]:
        """Locate an editor Tab/Pane named like ``title`` that resolves probe."""
        try:
            main = self.app.main_window()
        except Exception:
            return None
        wanted = title.lower()
        for ctype in ("Tab", "TabItem", "Pane"):
            for cand in self._pane_candidates(main, ctype, wanted):
                try:
                    self.finder.resolve(cand, probe_role)
                except Exception:
                    continue
                self.set_window(cand)
                return cand
        return None

    def _pane_candidates(self, main: Any, ctype: str, wanted: str) -> list[Any]:
        try:
            nodes = list(main.descendants(control_type=ctype))
        except Exception:
            return []
        out = []
        for node in nodes:
            try:
                text = (node.window_text() or "").strip()
            except Exception:
                continue
            if text and wanted in text.lower():
                out.append(node)
        return out

    # -- internals ------------------------------------------------------------

    def _resolve_anywhere(self, window: Any, role: str) -> Any:
        for win in (window, self.app.main_window()):
            try:
                return self.finder.resolve(win, role)
            except Exception:
                continue
        raise ControlNotFoundError(role, "role not found in current or main window")

    @staticmethod
    def _win_key(window: Any) -> Optional[int]:
        if window is None:
            return None
        try:
            return int(window.handle)
        except Exception:
            return None


def _editor_body(win: Any):
    """Return the largest editor-body Pane below the toolbar, else None.

    Eclipse SWT renders the Order editor inside a Pane that spans the area
    right of the Navigation view and below the toolbar (x > 380, y > 120).
    Clicking it activates the ScrolledComposite so lazy sections render.
    """
    best, best_area = None, 0
    try:
        for ctrl in win.descendants(control_type="Pane"):
            try:
                rect = ctrl.rectangle()
                area = rect.width() * rect.height()
                if area > best_area and rect.top > 120 and rect.left > 380:
                    best_area, best = area, ctrl
            except Exception:
                continue
    except Exception:
        pass
    return best


def _has_section(win: Any, name: str) -> bool:
    """True if any descendant's text matches ``name`` (case-insensitive)."""
    pat = re.compile(r"(?i)" + re.escape(name))
    try:
        for ctrl in win.descendants():
            try:
                if pat.search(ctrl.window_text() or ""):
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def stabilize_active_editor(
    ctx: FlowContext,
    wait_control: str = "Addresses",
    max_retries: int = 5,
) -> bool:
    """Force Eclipse SWT to instantiate lazy composites (Addresses, Items, Totals).

    SWT (ScrolledComposite / ExpandableComposite) only builds the UIA tree
    nodes for a section after the editor gains focus. Keyboard traversal
    triggers the lazy layout without relying on any screen coordinates.
    """
    main = ctx.app.main_window()
    for attempt in range(max_retries):
        try:
            # 1. Focus the active editor window/pane.
            try:
                ctx.window().set_focus()
            except Exception:
                main.set_focus()
            time.sleep(0.2)

            # Traverse the form to trigger lazy layout.
            for _ in range(8):
                main.type_keys("{TAB}", pause=0.03)
            time.sleep(0.3)

            # Section instantiated?
            if _has_section(main, wait_control):
                try:
                    main.type_keys("^{HOME}", pause=0.05)
                except Exception:
                    pass
                time.sleep(0.2)
                logger.info(
                    "stabilize_active_editor: %r rendered after attempt %d",
                    wait_control,
                    attempt + 1,
                )
                return True
        except Exception:
            time.sleep(0.5)
    logger.warning(
        "stabilize_active_editor: %r NOT rendered after %d attempts",
        wait_control,
        max_retries,
    )
    return False


def _title_hint(role: str) -> Optional[str]:
    for prefix in sorted(_ROLE_TITLE_HINTS, key=len, reverse=True):
        if role.startswith(prefix):
            return _ROLE_TITLE_HINTS[prefix]
    return None


# ---------------------------------------------------------------------------
# Control / row helpers (exact-match-only policy)
# ---------------------------------------------------------------------------


def read_text(control: Any) -> str:
    """Best-effort text of a control (label or field)."""
    try:
        return str(control.window_text() or "")
    except Exception:
        try:
            return " ".join(control.texts())
        except Exception:
            return ""


def read_decimal(ctx: FlowContext, role: str) -> Decimal:
    """Read a numeric field as a Decimal (edit pattern first, label text after)."""
    ctrl = ctx.find(role)
    try:
        return Edit(ctrl, ctx.waits).value_decimal()
    except Exception:
        return parse_decimal(read_text(ctrl))


def decimal_eq(cell: Any, value: Decimal) -> bool:
    try:
        return parse_decimal(str(cell)) == value
    except Exception:
        return False


def cells_low(row: Any) -> list[str]:
    return [(c or "").strip().lower() for c in row]


def row_has_exact(row: Any, value: str) -> bool:
    """True when any trimmed cell equals ``value`` (case-insensitive)."""
    needle = (value or "").strip().lower()
    return bool(needle) and needle in cells_low(row)


def row_matches_exact(row: Any, expected: list[str]) -> bool:
    """True when every non-empty expected value appears as an exact cell."""
    cells = cells_low(row)
    return all((v or "").strip().lower() in cells for v in expected if (v or "").strip())


def select_combo_value(ctx: FlowContext, role: str, value: str, step: str) -> str:
    """Select ``value`` in a combo; ambiguous or missing values raise for review."""
    combo = Combo(ctx.find(role), ctx.waits)
    exact = matches_exact(value, combo.items())
    if len(exact) != 1:
        raise ManualReviewError(
            step, f"{role}: value {value!r} not uniquely present (items={combo.items()!r})"
        )
    combo.select(exact[0])
    return exact[0]


# ---------------------------------------------------------------------------
# Data > Documents row heuristics (shared by step4 and step5)
# ---------------------------------------------------------------------------


def doc_kind(row: Any, kind: str) -> bool:
    """True when the row belongs to the document kind (e.g. 'order', 'invoice').

    Word-boundary match (never substring): ``order`` must not match a row that
    merely contains 'order' inside another word, and ``paid`` must not match
    ``unpaid``.
    """
    text = " ".join(str(c) for c in row).lower()
    return re.search(r"(?<![a-z])" + re.escape(kind.lower()) + r"(?![a-z])", text) is not None


def doc_total_ok(row: Any, total: Decimal) -> bool:
    return any(decimal_eq(c, total) for c in row)


def doc_state(row: Any) -> str:
    for cell in row:
        token = (cell or "").strip().lower()
        if token in DOC_STATE_TOKENS:
            return token
    return ""


def doc_state_ok(row: Any, expected: str) -> bool:
    return expected in cells_low(row)


def doc_date_ok(row: Any, order_date: date) -> bool:
    iso = order_date.isoformat()
    for cell in row:
        text = (cell or "").strip()
        if iso in text.replace(" ", ""):
            return True
        for token in re.findall(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", text):
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
                try:
                    if datetime.strptime(token, fmt).date() == order_date:
                        return True
                except ValueError:
                    continue
    return False


def candidate_doc_rows(
    rows: list[list[str]], reference: str, order_date: date, total: Decimal
) -> list[list[str]]:
    """Rows belonging to the saved document pair, keyed by Cust.Ref or date+total."""
    if reference:
        return [r for r in rows if row_has_exact(r, reference)]
    return [r for r in rows if doc_date_ok(r, order_date) and doc_total_ok(r, total)]
