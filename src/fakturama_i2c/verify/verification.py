"""Read-back verification of persisted Fakturama state.

After the flow has saved the Order and created the linked Invoice, the
:class:`Verifier` re-opens Fakturama's ``Data > Documents`` list and the Invoice
editor and compares the persisted values field-by-field against the extracted
order. The AMBIGUITY POLICY applies here too: zero or multiple matching rows, or
any persisted-value mismatch, raises ``ManualReviewError`` instead of guessing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional, Sequence

from ..config import Settings
from ..models import PaidStatus, PaymentCode, payment_code_for
from ..ui.app import AppController
from ..ui.elements import Combo, Edit, Menu, parse_decimal
from ..ui.registry import ControlFinder, matches_exact
from ..ui.waits import Waits
from ..utils.errors import (
    ControlNotFoundError,
    FlowTimeoutError,
    I2CError,
    ManualReviewError,
)
from ..utils.logging import get_logger

if TYPE_CHECKING:
    from ..flow.context import FlowContext

logger = get_logger("verify.verification")

_STATE_TOKENS = {
    "open", "offen", "accepted", "angenommen", "sent", "versendet",
    "billed", "booked", "closed", "cancelled", "storniert", "paid",
    "bezahlt", "draft", "entwurf",
}


@dataclass
class VerificationReport:
    """Result of one verification pass; JSON-friendly."""

    checks: list[str] = field(default_factory=list)
    details: dict[str, str] = field(default_factory=dict)
    _failed: set[str] = field(default_factory=set, repr=False, compare=False)

    def add_check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(name)
        self.details[name] = detail or ("ok" if ok else "FAILED")
        if not ok:
            self._failed.add(name)

    @property
    def passed(self) -> bool:
        """True when every recorded check passed."""
        return not self._failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": list(self.checks),
            "details": dict(self.details),
        }


class Verifier:
    """Reads back persisted state and compares it against expectations."""

    def __init__(self, settings: Settings, app: AppController) -> None:
        self.settings = settings
        self.app = app
        self._finder: Optional[ControlFinder] = None
        self._waits: Optional[Waits] = None

    # -- lazy helpers -------------------------------------------------------

    @property
    def finder(self) -> ControlFinder:
        if self._finder is None:
            self._finder = ControlFinder(self.settings)
        return self._finder

    @property
    def waits(self) -> Waits:
        if self._waits is None:
            self._waits = Waits(self.settings)
        return self._waits

    # -- documents verification ---------------------------------------------

    def verify_documents(
        self,
        ctx: Optional["FlowContext"] = None,
        expected_reference: str = "",
        expected_total: str = "",
    ) -> VerificationReport:
        """Verify the Order + Invoice pair in ``Data > Documents``.

        When expectations are given, exactly one Order row (state ``open``) and
        exactly one Invoice row must match both the Cust.Ref. and the Total.
        Zero or multiple matches raise ``ManualReviewError``. With empty
        expectations the rows found are reported without failing.
        """
        report = VerificationReport()
        window = self._window(ctx)
        self._open_menu(window, "Data", "Documents")

        table_ctrl = self.finder.resolve(window, "DOCUMENTS_TABLE")
        stable = self.waits.stable_snapshot(table_ctrl)
        rows = [self._flatten_row(r) for r in stable if isinstance(r, tuple)]

        orders = [r for r in rows if self._classify(r) == "Order"]
        invoices = [r for r in rows if self._classify(r) == "Invoice"]

        if expected_reference or expected_total:
            order_matches = self._match_rows(orders, expected_reference, expected_total, state="open")
            self._require_single(
                "Order", order_matches, expected_reference, expected_total, report, "verify.documents"
            )
            invoice_matches = self._match_rows(invoices, expected_reference, expected_total, state=None)
            self._require_single(
                "Invoice", invoice_matches, expected_reference, expected_total, report, "verify.documents"
            )
        else:
            report.add_check(
                "documents.list",
                True,
                f"found {len(orders)} order row(s), {len(invoices)} invoice row(s)",
            )
            for row in orders:
                report.add_check("order.row", True, " | ".join(row[:8]))
            for row in invoices:
                report.add_check("invoice.row", True, " | ".join(row[:8]))
        return report

    def verify_invoice_payment(
        self,
        ctx: Optional["FlowContext"] = None,
        expected_method: str = "",
        expected_paid_status: Optional[Any] = None,
        expected_payment_date: Optional[Any] = None,
        expected_value: Optional[Any] = None,
    ) -> VerificationReport:
        """Reopen the saved Invoice editor and compare payment fields."""
        report = VerificationReport()
        editor = self._open_invoice_editor(ctx, expected_value=expected_value)

        method_actual = self._read_field(editor, "INVOICE_PAYMENT_METHOD")
        status_actual = self._read_field(editor, "INVOICE_PAID_STATUS")
        date_actual = self._read_field(editor, "INVOICE_PAYMENT_DATE")
        value_actual = self._read_field(editor, "INVOICE_PAYMENT_VALUE")

        failures: list[str] = []
        if expected_method:
            expected = self._expected_payment_method(expected_method)
            ok = self._same(expected, method_actual)
            report.add_check(
                "invoice.payment_method", ok, f"expected {expected!r}, persisted {method_actual!r}"
            )
            if not ok:
                failures.append(f"payment method: expected {expected!r}, persisted {method_actual!r}")

        if expected_paid_status is not None:
            expected = (
                expected_paid_status.value
                if isinstance(expected_paid_status, PaidStatus)
                else str(expected_paid_status)
            )
            ok = self._same(expected, status_actual)
            report.add_check(
                "invoice.paid_status", ok, f"expected {expected!r}, persisted {status_actual!r}"
            )
            if not ok:
                failures.append(f"paid status: expected {expected!r}, persisted {status_actual!r}")

        if expected_payment_date is not None:
            expected_date = self._parse_date(expected_payment_date)
            ok = expected_date is not None and self._date_matches(date_actual, expected_date)
            report.add_check(
                "invoice.payment_date",
                ok,
                f"expected {expected_payment_date}, persisted {date_actual!r}",
            )
            if not ok:
                failures.append(
                    f"payment date: expected {expected_payment_date}, persisted {date_actual!r}"
                )

        if expected_value:
            expected_dec = self._norm_decimal(expected_value)
            actual_dec = self._norm_decimal(value_actual)
            ok = expected_dec is not None and expected_dec == actual_dec
            report.add_check(
                "invoice.payment_value",
                ok,
                f"expected {expected_value!r}, persisted {value_actual!r}",
            )
            if not ok:
                failures.append(f"payment value: expected {expected_value!r}, persisted {value_actual!r}")

        if failures:
            raise ManualReviewError("verify.invoice_payment", "; ".join(failures))
        return report

    # -- row matching (exact-match-only policy) -----------------------------

    def _require_single(
        self,
        kind: str,
        matches: Sequence[list[str]],
        reference: str,
        total: str,
        report: VerificationReport,
        step: str,
    ) -> None:
        if not matches:
            raise ManualReviewError(
                step, f"no {kind} row matches Cust.Ref={reference!r} Total={total!r}"
            )
        if len(matches) > 1:
            raise ManualReviewError(
                step,
                f"{len(matches)} {kind} rows match Cust.Ref={reference!r} Total={total!r}; "
                "ambiguous, refusing to guess",
            )
        report.add_check(f"documents.{kind.lower()}", True, " | ".join(matches[0][:8]))

    def _match_rows(
        self,
        rows: Sequence[list[str]],
        reference: str,
        total: str,
        state: Optional[str],
    ) -> list[list[str]]:
        out: list[list[str]] = []
        for row in rows:
            if not self._row_matches(row, reference, total):
                continue
            if state and not self._state_ok(row, state):
                continue
            out.append(row)
        return out

    def _row_matches(self, row: Sequence[str], reference: str, total: str) -> bool:
        if reference and not matches_exact(reference, row):
            return False
        if total:
            expected = self._norm_decimal(total)
            if expected is not None and not any(
                self._norm_decimal(c) == expected for c in row
            ):
                return False
        return True

    def _classify(self, row: Sequence[str]) -> Optional[str]:
        text = " ".join(row).lower()
        if any(t in text for t in ("invoice", "rechnung", "facture", "faktura")):
            return "Invoice"
        if any(t in text for t in ("order", "offer", "bestellung", "angebot", "commande")):
            return "Order"
        return None

    def _state_ok(self, row: Sequence[str], expected: str) -> bool:
        state = self._row_state(row)
        return state in ("", expected.strip().lower())

    def _row_state(self, row: Sequence[str]) -> str:
        for cell in row:
            token = (cell or "").strip().lower()
            if token in _STATE_TOKENS:
                return token
        return ""

    # -- invoice editor reopen ----------------------------------------------

    def _open_invoice_editor(
        self,
        ctx: Optional["FlowContext"],
        expected_value: Any,
        expected_reference: str = "",
    ) -> Any:
        try:
            return self.app.window_by_title("Invoice", timeout=self.settings.window_timeout)
        except FlowTimeoutError:
            pass
        window = self._window(ctx)
        self._open_menu(window, "Data", "Documents")
        table_ctrl = self.finder.resolve(window, "DOCUMENTS_TABLE")
        row = self._locate_invoice_row(table_ctrl, expected_reference, expected_value)
        self._double_click(row)
        return self.app.window_by_title("Invoice", timeout=self.settings.window_timeout)

    def _locate_invoice_row(
        self, table_ctrl: Any, expected_reference: str, expected_value: Any
    ) -> Any:
        stable = self.waits.stable_snapshot(table_ctrl)
        raw_rows = table_ctrl.rows()
        rows = [self._flatten_row(r) for r in stable if isinstance(r, tuple)]
        invoices = [(i, r) for i, r in enumerate(rows) if self._classify(r) == "Invoice"]
        if not invoices:
            raise ManualReviewError("verify.invoice_payment", "no Invoice row in documents list")
        matches = [
            (i, r) for i, r in invoices
            if self._row_matches(r, expected_reference, str(expected_value or ""))
        ]
        if not matches:
            raise ManualReviewError(
                "verify.invoice_payment",
                "no Invoice row matches the saved reference/total",
            )
        if len(matches) > 1:
            raise ManualReviewError(
                "verify.invoice_payment",
                f"{len(matches)} Invoice rows match; ambiguous, refusing to guess",
            )
        return raw_rows[matches[0][0]]

    def _double_click(self, control: Any) -> None:
        try:
            control.double_click_input()
        except Exception:
            try:
                control.double_click()
            except Exception as exc:
                raise ControlNotFoundError("double_click", str(exc)) from exc

    # -- field reading -------------------------------------------------------

    def _read_field(self, window: Any, role: str) -> str:
        ctrl = self.finder.resolve(window, role)
        for wrapper in (Edit, Combo):
            try:
                value = wrapper(ctrl, self.waits).value()
                if str(value or "").strip():
                    return str(value).strip()
            except Exception:
                continue
        try:
            return str(ctrl.window_text() or "").strip()
        except Exception:
            return ""

    # -- navigation -----------------------------------------------------------

    def _window(self, ctx: Optional["FlowContext"]) -> Any:
        if ctx is not None:
            return ctx.window()
        return self.app.main_window()

    def _open_menu(self, window: Any, top: str, item: str) -> None:
        try:
            menu_ctrl = self.finder.resolve(window, "MENU_DATA")
            Menu(menu_ctrl, self.waits).select_item(item)
            return
        except ControlNotFoundError:
            pass
        try:
            window.menu_select(f"{top}->{item}")
            return
        except Exception:
            pass
        raise ControlNotFoundError(f"menu {top}->{item}", "menu path unavailable")

    # -- comparison helpers ---------------------------------------------------

    @staticmethod
    def _same(a: Any, b: Any) -> bool:
        return (str(a or "").strip().lower() == str(b or "").strip().lower())

    @staticmethod
    def _norm_decimal(value: Any) -> Optional[Decimal]:
        if str(value or "").strip() == "":
            return None
        try:
            return parse_decimal(str(value)).quantize(Decimal("0.01"))
        except Exception:
            return None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError:
            return None

    @staticmethod
    def _date_matches(text: Any, expected_date: date) -> bool:
        t = str(text or "").strip()
        if not t:
            return False
        if expected_date.isoformat() in t.replace(" ", ""):
            return True
        for token in re.findall(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", t):
            for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%y"):
                try:
                    if datetime.strptime(token, fmt).date() == expected_date:
                        return True
                except ValueError:
                    continue
        return False

    @staticmethod
    def _expected_payment_method(method: str) -> str:
        code = payment_code_for(method)
        if code is PaymentCode.NONE:
            return (method or "").strip()
        return code.value

    @staticmethod
    def _flatten_row(item: Any) -> list[str]:
        cells: list[str] = []
        if isinstance(item, tuple):
            for cell in item:
                if isinstance(cell, (list, tuple)):
                    cells.extend(str(x) for x in cell)
                else:
                    cells.append(str(cell))
        else:
            cells.append(str(item))
        return [c.strip() for c in cells if c.strip()]