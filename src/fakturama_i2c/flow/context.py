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
from ..utils.errors import ControlNotFoundError, ManualReviewError
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
        """Resolve ``role`` inside the current window."""
        return self.finder.resolve(self.window(), role)

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

    def open_search_dialog(self, dialog_title: str, search_text: str) -> Table:
        """Wait for a select dialog, search it, and return the stabilized table."""
        win = self.waits.for_window(self.app.desktop, dialog_title, self.settings.window_timeout)
        self.set_window(win)
        self.waits.stable_snapshot(self.find("RESULT_TABLE"))
        Edit(self.find("SEARCH_EDIT"), self.waits).fill(search_text)
        self.waits.stable_snapshot(self.find("RESULT_TABLE"))
        return Table(self.find("RESULT_TABLE"), self.waits)

    def cancel_dialog(self) -> None:
        """Close the current dialog via Cancel, falling back to a hard close."""
        try:
            Button(self.find("CANCEL_BUTTON"), self.waits).click()
        except ControlNotFoundError:
            Dialog(self.window(), self.waits).close()

    def menu_select(self, top_role: str, item_role: str, item_text: str) -> None:
        """Pick ``item_text`` under the ``top_role`` menu (with click fallbacks)."""
        win = self.window()
        try:
            Menu(self._resolve_anywhere(win, top_role), self.waits).select_item(item_text)
            return
        except Exception as exc:  # SWT menus rarely expose menu_select
            logger.debug("menu_select(%r) failed: %s", item_text, exc)
        try:
            self.app.main_window().menu_select(f"Data->{item_text}")
            return
        except Exception as exc:
            logger.debug("window.menu_select(%r) failed: %s", item_text, exc)
        Button(self._resolve_anywhere(win, top_role), self.waits).click()
        Button(self._resolve_anywhere(win, item_role), self.waits).click()

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