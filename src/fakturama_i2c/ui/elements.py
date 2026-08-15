"""Typed element wrappers over pywinauto controls.

These make the flow code declarative: ``Edit.fill("...")``, ``Combo.select("x")``,
``Button.click()``, ``Table.read_rows()``. All raw pywinauto exceptions are
translated into our error taxonomy. Wrappers deliberately do NOT know about
coordinates or layout.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from typing import Any, Optional, Sequence

from ..utils.errors import ControlNotFoundError, ManualReviewError
from .waits import Waits

# ---------------------------------------------------------------------------
# Value parsing helpers (Fakturama uses locale-dependent numeric formats)
# ---------------------------------------------------------------------------


def parse_decimal(text: str) -> Decimal:
    """Parse a decimal from a UI cell that may use comma or dot decimals."""
    cleaned = (text or "").strip()
    cleaned = cleaned.replace("€", "").replace("$", "").replace("EUR", "").strip()
    # thousand separators: "1.234,56" -> remove dots, keep comma as decimal
    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    return Decimal(cleaned or "0")


def parse_percent(text: str) -> Decimal:
    return parse_decimal(text.replace("%", ""))


def format_decimal(value: Decimal) -> str:
    """Format a Decimal for a Fakturama numeric field (no group separators)."""
    q = value.quantize(Decimal("0.01"))
    return f"{q:f}"


# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


class Control:
    """Thin wrapper that re-resolves the underlying control lazily."""

    def __init__(self, control, waits: Waits) -> None:
        self._ctrl = control
        self.waits = waits

    @property
    def ctrl(self) -> Any:
        if not _alive(self._ctrl):
            raise ControlNotFoundError("underlying control vanished", "")
        return self._ctrl

    def screenshot(self) -> Any:
        return self.ctrl.capture_as_image()


def _alive(control: Any) -> bool:
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


class Edit(Control):
    def value(self) -> str:
        try:
            v = self.ctrl.get_value()
            return str(v or "")
        except Exception as exc:
            raise ControlNotFoundError("edit.value", str(exc)) from exc

    def fill(self, text: Any) -> None:
        try:
            # Ensure focus before setting text — SWT Edits may silently
            # ignore set_edit_text on an unfocused control.
            try:
                self.ctrl.set_focus()
            except Exception:
                pass
            self.ctrl.set_edit_text("" if text is None else str(text))
        except Exception:
            try:
                self.ctrl.type_keys(str(text or ""))
            except Exception as exc:
                raise ControlNotFoundError("edit.fill", str(exc)) from exc

    def clear(self) -> None:
        self.fill("")

    def value_decimal(self) -> Decimal:
        return parse_decimal(self.value())


class Combo(Control):
    def items(self) -> list[str]:
        try:
            return list(self.ctrl.texts())
        except Exception:
            try:
                return list(self.ctrl.selectable_texts())
            except Exception as exc:
                raise ControlNotFoundError("combo.items", str(exc)) from exc

    def value(self) -> str:
        try:
            return str(self.ctrl.selected_text() or "")
        except Exception:
            return self.items()[0] if self.items() else ""

    def select(self, text: str) -> None:
        """Select combo item via resilient keyboard navigation (SWT-safe).

        pywinauto's UIA ``select()`` takes no positional argument on SWT
        ComboBoxes, so the direct call is a no-op at best.  The keyboard path
        (focus -> click -> type text -> ENTER) is the standard SWT-compatible
        approach; a text-scan arrow traversal is used as a fallback.
        """
        if not text:
            return

        # Path 1: keyboard type-ahead (works on SWT ComboBox / CCombo).
        try:
            self.ctrl.set_focus()
            time.sleep(0.15)
            self.ctrl.click_input()
            time.sleep(0.25)
            self.ctrl.type_keys(str(text), with_spaces=True, pause=0.02)
            time.sleep(0.2)
            self.ctrl.type_keys("{ENTER}")
            return
        except Exception:
            pass

        # Path 2: scan items and traverse with arrow keys.
        try:
            items = self.items()
            exact = [i for i in items if (i or "").strip().lower() == (text or "").strip().lower()]
            if not exact and items:
                exact = [i for i in items if (text or "").strip().lower() in (i or "").strip().lower()]
            if not exact:
                raise ManualReviewError(
                    "combo.select",
                    f"value {text!r} not in combo items {items!r}",
                )
            self.ctrl.set_focus()
            self.ctrl.click_input()
            time.sleep(0.2)
            self.ctrl.type_keys("{HOME}")
            time.sleep(0.1)
            for _ in range(len(items) if items else 10):
                current = self.ctrl.selected_text() if hasattr(self.ctrl, "selected_text") else None
                if current and text.strip().lower() in (current or "").strip().lower():
                    self.ctrl.type_keys("{ENTER}")
                    return
                self.ctrl.type_keys("{DOWN}")
                time.sleep(0.08)
            self.ctrl.type_keys("{ENTER}")
        except ManualReviewError:
            raise
        except Exception as exc:
            raise ControlNotFoundError("combo.select", f"Failed selecting '{text}': {exc}") from exc

    def select_or_clear(self, text: str) -> None:
        """Select if present, else clear -- used for payment-method codes."""
        if not text:
            try:
                self.ctrl.select(0)
            except Exception:
                pass
            return
        try:
            self.select(text)
        except ManualReviewError:
            # Code not present: spec says blank it, don't invent one.
            try:
                self.ctrl.select(0)
            except Exception as exc:
                raise ControlNotFoundError("combo.select_or_clear", str(exc)) from exc


class Button(Control):
    def click(self) -> None:
        last_exc: Optional[Exception] = None
        for method_name in ("click", "click_input", "invoke"):
            method = getattr(self.ctrl, method_name, None)
            if callable(method):
                try:
                    method()
                    return
                except Exception as exc:
                    last_exc = exc
        raise ControlNotFoundError("button.click", str(last_exc or "no clickable method found"))

    def enabled(self) -> bool:
        try:
            return bool(self.ctrl.is_enabled())
        except Exception:
            return True


class Checkbox(Control):
    def set_checked(self, checked: bool) -> None:
        try:
            if checked != bool(self.ctrl.get_toggle_state()):
                self.ctrl.click()
        except Exception as exc:
            raise ControlNotFoundError("checkbox.set_checked", str(exc)) from exc

    def is_checked(self) -> bool:
        try:
            return bool(self.ctrl.get_toggle_state())
        except Exception:
            return False


class Table(Control):
    def rows(self) -> list[list[str]]:
        try:
            rows = self.ctrl.rows()
        except Exception:
            try:
                rows = self.ctrl.children()
            except Exception as exc:
                raise ControlNotFoundError("table.rows", str(exc)) from exc
        out: list[list[str]] = []
        for row in rows[:200]:
            cells: list[str] = []
            try:
                for cell in row.children():
                    try:
                        cells.append(" | ".join(cell.texts()))
                    except Exception:
                        cells.append(str(cell.window_text()))
            except Exception:
                cells.append(str(row.window_text()))
            out.append(cells)
        return out

    def find_row(self, predicate) -> Optional[list[str]]:
        for row in self.rows():
            if predicate(row):
                return row
        return None

    def select_row(self, row_index: int) -> None:
        try:
            rows = self.ctrl.rows()
            rows[row_index].click()
        except Exception as exc:
            raise ControlNotFoundError("table.select_row", str(exc)) from exc


class List(Control):
    def item_count(self) -> int:
        try:
            return int(self.ctrl.item_count())
        except Exception as exc:
            raise ControlNotFoundError("list.item_count", str(exc)) from exc

    def items(self) -> list[str]:
        try:
            return list(self.ctrl.get_items())
        except Exception:
            try:
                return list(self.ctrl.texts())
            except Exception as exc:
                raise ControlNotFoundError("list.items", str(exc)) from exc

    def select(self, index: int) -> None:
        try:
            self.ctrl.select(index)
        except Exception as exc:
            raise ControlNotFoundError("list.select", str(exc)) from exc


class Dialog(Control):
    def wait_for(self, timeout: float = 10.0) -> "Dialog":
        try:
            self._ctrl.wait("exists visible", timeout=timeout)
        except Exception as exc:
            raise ControlNotFoundError("dialog.wait_for", str(exc)) from exc
        return self

    def close(self) -> None:
        try:
            self._ctrl.close()
        except Exception:
            pass

    @property
    def title(self) -> str:
        try:
            return str(self._ctrl.window_text())
        except Exception:
            return ""


class Menu(Control):
    def select_item(self, *items: str) -> None:
        try:
            self.ctrl.menu_select("->".join(items))
        except Exception as exc:
            raise ControlNotFoundError("menu.select_item", str(exc)) from exc