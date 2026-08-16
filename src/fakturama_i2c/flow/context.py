"""Flow context: shared state + the recurring UI patterns for the five steps.

``FlowContext`` is the single object threaded through every step. It owns the
app/waits/finder references, the extracted order, the still-open editor window
anchor (``set_window``), and a small checkpoint log (``done_steps``). The module
functions below encode the exact-match-only row policy and the document-row
heuristics so the steps stay declarative.
"""

from __future__ import annotations

import os
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

# Win32 messages used for SWT controls (SWT buttons respond to BM_CLICK).
WM_CLOSE = 0x0010
BM_CLICK = 0x00F5


def dismiss_save_parts_dialogs(max_dialogs: int = 32) -> int:
    """Close every open 'Save Parts' dialog by clicking its 'Don't save' button.

    'Save Parts' dialogs are OWNED popup windows of the Fakturama main window
    (not WS_CHILD children), so neither child_window() nor desktop.windows()
    reliably finds them. We enumerate via Win32 EnumWindows + EnumChildWindows
    and click through BM_CLICK, which SWT buttons honour even when focus is
    disturbed. Returns the number of dialogs dismissed.

    Prefers 'Don't save' over 'Cancel': this helper is used by the stale-tab
    sweep, where the intent is to REALLY close the dirty editor (Cancel would
    abort the close and leave the tab open, which lets stale editors pile up
    and makes the active editor ambiguous for later steps).
    """
    import ctypes as _ctypes
    from ctypes import wintypes as _wt

    _user32 = _ctypes.windll.user32
    _WNDENUMPROC = _ctypes.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)

    dialogs: list[int] = []

    @_WNDENUMPROC
    def _cb(hwnd, _lp):
        buf = _ctypes.create_unicode_buffer(256)
        _user32.GetWindowTextW(hwnd, buf, 256)
        if "save parts" in buf.value.lower():
            dialogs.append(hwnd)
        return True

    _user32.EnumWindows(_cb, 0)
    dismissed = 0
    for hwnd in dialogs[:max_dialogs]:
        kids: list[tuple[int, str]] = []

        @_WNDENUMPROC
        def _kcb(ch, _lp):
            buf = _ctypes.create_unicode_buffer(128)
            _user32.GetWindowTextW(ch, buf, 128)
            kids.append((ch, buf.value))
            return True

        _user32.EnumChildWindows(hwnd, _kcb, 0)

        def _btn(*names: str) -> int | None:
            lowered = [n.strip().lower() for n in names]
            return next(
                (ch for ch, t in kids if t.strip().lower().replace("\u2019", "'") in lowered),
                None,
            )

        dont_save = _btn("don't save", "don’t save", "dont save")
        cancel = _btn("cancel")
        try:
            if dont_save:
                _user32.SendMessageW(dont_save, BM_CLICK, 0, 0)
            elif cancel:
                _user32.SendMessageW(cancel, BM_CLICK, 0, 0)
            else:
                _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            dismissed += 1
        except Exception:
            logger.debug("dismiss_save_parts: failed on hwnd %d", hwnd)
        time.sleep(0.15)
    if dismissed:
        logger.info("dismiss_save_parts_dialogs: dismissed %d dialog(s)", dismissed)
    return dismissed

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
        # CRITICAL: pywinauto control wrapper's type_keys() goes through
        # UIA which does NOT work for SWT custom search boxes.  We must use
        # pywinauto.keyboard.send_keys() for OS-level keystroke injection.
        import pywinauto.keyboard as _kb

        logger.info("open_search_dialog: RESULT_TABLE invisible, using keyboard fallback")
        # Stage 1: enter search text into the search edit.
        # Strategy: WM_SETTEXT (most reliable, bypasses keyboard entirely),
        # then clipboard paste, then char-by-char typing as fallbacks.
        import ctypes as _ctypes

        WM_SETTEXT = 0x000C
        WM_GETTEXT = 0x000D
        WM_GETTEXTLENGTH = 0x000E

        def _wm_settext(hwnd: int, text: str) -> bool:
            """Set text of a Win32 control via WM_SETTEXT message."""
            try:
                _ctypes.windll.user32.SendMessageW.argtypes = [
                    _ctypes.c_void_p, _ctypes.c_uint, _ctypes.c_void_p, _ctypes.c_void_p
                ]
                _ctypes.windll.user32.SendMessageW.restype = _ctypes.c_void_p
                buf = text.encode("utf-16-le") + b"\x00\x00"
                result = _ctypes.windll.user32.SendMessageW(
                    hwnd, WM_SETTEXT, 0, _ctypes.c_char_p(buf)
                )
                return result != 0
            except Exception as exc:
                logger.debug("_wm_settext failed: %s", exc)
                return False

        def _wm_gettext(hwnd: int) -> str:
            """Read text from a Win32 control via WM_GETTEXTLENGTH + WM_GETTEXT."""
            try:
                _ctypes.windll.user32.SendMessageW.argtypes = [
                    _ctypes.c_void_p, _ctypes.c_uint, _ctypes.c_void_p, _ctypes.c_void_p
                ]
                _ctypes.windll.user32.SendMessageW.restype = _ctypes.c_void_p
                length = _ctypes.windll.user32.SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
                if length <= 0:
                    return ""
                buf = _ctypes.create_string_buffer((int(length) + 1) * 2)
                _ctypes.windll.user32.SendMessageW(hwnd, WM_GETTEXT, len(buf), buf)
                return buf.raw.decode("utf-16-le", errors="replace").rstrip("\x00")
            except Exception:
                return ""

        def _clipboard_paste(text: str) -> None:
            CF_UNICODETEXT = 13
            GMEM_MOVEABLE = 0x0002
            user32 = _ctypes.windll.user32
            kernel32 = _ctypes.windll.kernel32
            user32.OpenClipboard.argtypes = [_ctypes.c_void_p]
            user32.EmptyClipboard.argtypes = []
            user32.SetClipboardData.argtypes = [_ctypes.c_uint, _ctypes.c_void_p]
            user32.SetClipboardData.restype = _ctypes.c_void_p
            user32.CloseClipboard.argtypes = []
            kernel32.GlobalAlloc.argtypes = [_ctypes.c_uint, _ctypes.c_size_t]
            kernel32.GlobalAlloc.restype = _ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [_ctypes.c_void_p]
            kernel32.GlobalLock.restype = _ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [_ctypes.c_void_p]
            kernel32.GlobalFree.argtypes = [_ctypes.c_void_p]
            user32.OpenClipboard(None)
            user32.EmptyClipboard()
            raw = text.encode("utf-16-le") + b"\x00\x00"
            h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(raw))
            if not h:
                user32.CloseClipboard()
                raise MemoryError("GlobalAlloc failed")
            p = kernel32.GlobalLock(h)
            if not p:
                kernel32.GlobalFree(h)
                user32.CloseClipboard()
                raise MemoryError("GlobalLock failed")
            _ctypes.memmove(p, raw, len(raw))
            kernel32.GlobalUnlock(h)
            user32.SetClipboardData(CF_UNICODETEXT, h)
            user32.CloseClipboard()

        try:
            search_edit = self.find("SEARCH_EDIT")
        except Exception as exc:
            raise ManualReviewError(
                "search_dialog.keyboard",
                f"keyboard fallback for '{dialog_title}' failed at SEARCH_EDIT stage: {exc}",
            ) from exc

        # Method 1: char-by-char typing — triggers SWT modify event so table filters.
        # This is the ONLY reliable method because WM_SETTEXT doesn't trigger
        # the SWT modify event, and clipboard paste doesn't work for all SWT edits.
        # NOTE: texts() returns '' for some SWT dialogs (especially Fakturama address
        # dialog) even when text WAS entered. We trust that real keystrokes trigger
        # the modify event and filter the table correctly.
        text_entered = False
        logger.info("open_search_dialog: typing search text char-by-char")
        try:
            search_edit.click_input()
            time.sleep(0.2)
            _kb.send_keys("^a", pause=0.02)
            time.sleep(0.1)
            _kb.send_keys("{DELETE}", pause=0.02)
            time.sleep(0.1)
            for ch in search_text:
                if ch == " ":
                    _kb.send_keys("{SPACE}", pause=0.05)
                else:
                    _kb.send_keys(ch, pause=0.05)
                time.sleep(0.05)
            time.sleep(1.5)
            # Trust that keystrokes triggered modify event — texts() is unreliable
            # for some SWT virtual table dialogs (always returns '').
            text_entered = True
            logger.info("open_search_dialog: char-by-char completed, assuming text entered")
        except Exception as exc:
            logger.debug("open_search_dialog: char-by-char failed: %s", exc)

        # Note: WM_SETTEXT and clipboard paste fallbacks removed — char-by-char is
        # the ONLY reliable method. WM_SETTEXT doesn't trigger SWT modify event.
        # Clipboard paste doesn't work for this SWT dialog (texts() always returns '').
        # The char-by-char keystrokes DO trigger the modify event and filter the table.

        # Wait for SWT virtual table to filter results
        time.sleep(1.5)

        # DIAGNOSTIC: save screenshot of the open dialog for debugging
        try:
            import pywinauto as _pwa
            _shots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets", "screenshots")
            os.makedirs(_shots_dir, exist_ok=True)
            _shot_path = os.path.join(_shots_dir, "debug_dialog_open.png")
            win.capture_as_image().save(_shot_path)
            logger.info("open_search_dialog: diagnostic screenshot saved to %s", _shot_path)
        except Exception as _exc:
            logger.debug("open_search_dialog: diagnostic screenshot failed: %s", _exc)

        # DIAGNOSTIC: dump dialog descendants for debugging
        try:
            _shots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets", "screenshots")
            os.makedirs(_shots_dir, exist_ok=True)
            _dfile = os.path.join(_shots_dir, "debug_dialog_descendants.txt")
            with open(_dfile, "w", encoding="utf-8") as _df:
                _df.write(f"dialog_title={dialog_title!r}\n")
                _df.write(f"win={win!r}\n")
                try:
                    _df.write(f"win.class_name()={win.class_name()!r}\n")
                except Exception:
                    pass
                try:
                    _df.write(f"win.rect()={win.rect()!r}\n")
                except Exception:
                    pass
                try:
                    _df.write(f"search_edit.handle={search_edit.handle!r}\n")
                    _df.write(f"search_edit.rectangle()={search_edit.rectangle()!r}\n")
                    _df.write(f"search_edit.texts()={search_edit.texts()!r}\n")
                    _df.write(f"search_edit.window_text()={search_edit.window_text()!r}\n")
                except Exception:
                    pass
                _df.write(f"\n--- win.descendants() ---\n")
                try:
                    for i, desc in enumerate(win.descendants()):
                        try:
                            ct = getattr(getattr(desc, "element_info", None), "control_type", "?")
                            wt = desc.window_text() or ""
                            r = desc.rectangle()
                            _df.write(f"  [{i}] {ct} text={wt!r} rect={r!r}\n")
                        except Exception:
                            _df.write(f"  [{i}] <error reading desc>\n")
                except Exception as exc:
                    _df.write(f"  ERROR listing descendants: {exc}\n")
        except Exception:
            pass

        # Stage 2: select the first row in the table and confirm.
        # The table should now be filtered (char-by-char typing triggers modify event).
        import pywinauto.mouse as _mouse

        def _check_dialog_closed() -> bool:
            """Check if dialog has been dismissed.

            Uses ctypes EnumWindows as the SOLE authority: UIA desktop scans
            miss owned popups and stale wrappers can ghost the result (a dead
            SWT wrapper's exists()/is_visible() may keep returning True after
            the dialog closed). When EnumWindows succeeds, its verdict wins.
            """
            try:
                import ctypes as _ct
                from ctypes import wintypes as _wt

                _user32 = _ct.windll.user32
                _WNDENUMPROC = _ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
                still_open = []

                @_WNDENUMPROC
                def _cb(hwnd, _lp):
                    buf = _ct.create_unicode_buffer(256)
                    _user32.GetWindowTextW(hwnd, buf, 256)
                    if dialog_title.lower() in buf.value.lower():
                        still_open.append(hwnd)
                    return True

                _user32.EnumWindows(_cb, 0)
                for hwnd in still_open:
                    try:
                        if _user32.IsWindowVisible(hwnd):
                            logger.info(
                                "open_search_dialog: dialog still open (%d window(s) match)",
                                len(still_open),
                            )
                            return False
                    except Exception:
                        pass
                # EnumWindows succeeded: no visible matching window -> closed.
                return True
            except Exception:
                pass
            # EnumWindows itself failed (rare): fall back to UIA scans.
            # Check desktop windows for any remaining dialog with our title
            try:
                for dw in self.app.desktop.windows():
                    wt = (dw.window_text() or "").lower()
                    if dialog_title.lower() in wt:
                        try:
                            if dw.is_visible():
                                return False
                        except Exception:
                            pass
            except Exception:
                pass
            # Also check via win handle
            try:
                if win.is_visible():
                    return False
            except Exception:
                pass
            try:
                if win.exists():
                    return False
            except Exception:
                pass
            return True

        def _click_ok_in_dialog() -> bool:
            """Find and click the OK button in the dialog."""
            # Method 0: BM_CLICK via SendMessageW on the OK button HWND.
            # PROVEN most reliable: SWT buttons respond to BM_CLICK even when
            # focus/mouse routing is disturbed by stacked modal dialogs.
            # The OK button is resolved with pure ctypes (EnumWindows +
            # EnumChildWindows) because UIA can hand back ghost wrappers
            # whose HWNDs no longer belong to the dialog.
            try:
                import ctypes as _ct
                from ctypes import wintypes as _wt

                _user32 = _ct.windll.user32
                _WNDENUMPROC = _ct.WINFUNCTYPE(_wt.BOOL, _wt.HWND, _wt.LPARAM)
                dialogs: list[int] = []

                @_WNDENUMPROC
                def _dcb(hwnd, _lp):
                    buf = _ct.create_unicode_buffer(256)
                    _user32.GetWindowTextW(hwnd, buf, 256)
                    if dialog_title.lower() in buf.value.lower():
                        dialogs.append(hwnd)
                    return True

                _user32.EnumWindows(_dcb, 0)
                ok_hwnd = None
                for hwnd in dialogs:
                    kids: list[tuple[int, str]] = []

                    @_WNDENUMPROC
                    def _kcb(ch, _lp):
                        buf = _ct.create_unicode_buffer(128)
                        _user32.GetWindowTextW(ch, buf, 128)
                        kids.append((ch, buf.value))
                        return True

                    _user32.EnumChildWindows(hwnd, _kcb, 0)
                    for ch, txt in kids:
                        if txt.strip().lower() == "ok":
                            ok_hwnd = ch
                            break
                    if ok_hwnd:
                        break
                if ok_hwnd:
                    _ct.windll.user32.SendMessageW.argtypes = [
                        _ct.c_void_p, _ct.c_uint, _ct.c_void_p, _ct.c_void_p
                    ]
                    _ct.windll.user32.SendMessageW.restype = _ct.c_void_p
                    logger.info(
                        "open_search_dialog: BM_CLICK OK hwnd=%d (dialog hwnds=%d)",
                        ok_hwnd,
                        len(dialogs),
                    )
                    _ct.windll.user32.SendMessageW(ok_hwnd, BM_CLICK, 0, 0)
                    time.sleep(0.5)
                    return True
            except Exception:
                pass
            # Method 1: scan desktop windows for the dialog's OK button
            try:
                for dw in self.app.desktop.windows():
                    wt = (dw.window_text() or "").lower()
                    if dialog_title.lower() in wt:
                        for child in dw.descendants():
                            ct = (child.element_info.control_type or "").lower()
                            txt = (child.window_text() or "").lower()
                            if ct == "button" and txt == "ok":
                                child.click_input()
                                time.sleep(0.5)
                                return True
            except Exception:
                pass
            # Method 2: scan win descendants
            try:
                for child in win.descendants():
                    ct = (child.element_info.control_type or "").lower()
                    txt = (child.window_text() or "").lower()
                    if ct == "button" and txt == "ok":
                        child.click_input()
                        time.sleep(0.5)
                        return True
            except Exception:
                pass
            # Method 3: click OK by verified absolute coordinates.
            # Diagnostic dump confirmed OK button rect=(1645,1023)-(1767,1053),
            # center (1706,1038). These are absolute screen coords (fullscreen dialog).
            try:
                _mouse.click(coords=(1706, 1038))
                time.sleep(0.5)
                return True
            except Exception:
                pass
            # Method 4: ENTER (for SWT, ENTER on selected row confirms)
            try:
                _kb.send_keys("{ENTER}", pause=0.05)
                time.sleep(0.5)
                return True
            except Exception:
                pass
            return False

        # Get dialog rect for coordinates
        dlg_rect = None
        try:
            dlg_rect = win.rect()
        except Exception:
            pass

        # Table geometry from the diagnostic dump (absolute screen coords):
        # Table pane: rect=(9,79)-(1903,1005), header row ~y79-105,
        # first data row ~y109-135 (center ~y115).
        # OK button: (1645,1023)-(1767,1053), center (1706,1038).
        # These are absolute coordinates for the fullscreen #32770 dialog.
        table_click_x = 500
        table_click_y = 115
        if dlg_rect is not None:
            # Fullscreen dialog starts at (0,0); add dialog origin for safety.
            table_click_x = dlg_rect.left + 500
            table_click_y = dlg_rect.top + 115

        selected = False

        # Strategy A: click first table row + OK button
        logger.info(
            "open_search_dialog: Stage 2 — clicking first table row + OK at (%d, %d) (dlg_rect=%s)",
            table_click_x,
            table_click_y,
            dlg_rect,
        )
        try:
            _mouse.click(coords=(table_click_x, table_click_y))
            time.sleep(0.5)
            if _check_dialog_closed():
                selected = True
                logger.info("open_search_dialog: dialog closed via row click")
        except Exception as exc:
            logger.debug("row click failed: %s", exc)

        if not selected:
            try:
                if _click_ok_in_dialog():
                    time.sleep(0.5)
                    if _check_dialog_closed():
                        selected = True
                        logger.info("open_search_dialog: dialog closed via row+OK")
            except Exception as exc:
                logger.debug("row+OK failed: %s", exc)

        # Strategy B: double-click first row (SWT: double-click = select + confirm)
        if not selected:
            logger.info("open_search_dialog: Stage 2 — trying double-click first row")
            try:
                _mouse.double_click(coords=(table_click_x, table_click_y))
                time.sleep(1.0)
                if _check_dialog_closed():
                    selected = True
                    logger.info("open_search_dialog: dialog closed via double-click")
            except Exception as exc:
                logger.debug("double-click failed: %s", exc)

        # Strategy C: TAB → table → DOWN → ENTER
        if not selected:
            logger.info("open_search_dialog: Stage 2 — trying TAB+DOWN+ENTER")
            try:
                search_edit.click_input()
                time.sleep(0.1)
            except Exception:
                pass
            _kb.send_keys("{TAB}", pause=0.05)
            time.sleep(0.3)
            _kb.send_keys("{DOWN}", pause=0.05)
            time.sleep(0.3)
            _kb.send_keys("{ENTER}", pause=0.05)
            time.sleep(0.8)
            if _check_dialog_closed():
                selected = True
                logger.info("open_search_dialog: dialog closed via TAB+DOWN+ENTER")

        # Strategy D: F6 → DOWN → OK
        if not selected:
            logger.info("open_search_dialog: Stage 2 — trying F6+DOWN+OK")
            try:
                _kb.send_keys("{F6}", pause=0.05)
                time.sleep(0.3)
                _kb.send_keys("{DOWN}", pause=0.05)
                time.sleep(0.3)
                if _click_ok_in_dialog():
                    time.sleep(0.5)
                    if _check_dialog_closed():
                        selected = True
                        logger.info("open_search_dialog: dialog closed via F6+DOWN+OK")
            except Exception:
                pass

        # Strategy E: direct OK button coordinates (verified from diagnostic dump)
        # OK button is at (1645,1023)-(1767,1053) = center (1706,1038)
        if not selected:
            logger.info("open_search_dialog: Stage 2 — trying direct OK coords")
            try:
                ok_x = 1706  # (1645+1767)//2
                ok_y = 1038  # (1023+1053)//2
                if dlg_rect is not None:
                    ok_x = dlg_rect.left + 1706
                    ok_y = dlg_rect.top + 1038
                _mouse.click(coords=(ok_x, ok_y))
                time.sleep(0.5)
                if _check_dialog_closed():
                    selected = True
                    logger.info("open_search_dialog: dialog closed via direct OK")
            except Exception:
                pass

        # Final check
        if _check_dialog_closed():
            logger.info("open_search_dialog: dialog dismissed successfully")
        else:
            logger.warning("open_search_dialog: dialog still open — ESC fallback")
            try:
                _kb.send_keys("{ESC}", pause=0.05)
                time.sleep(0.5)
            except Exception:
                pass

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

    def wait_for_editor(self, title: str, probe_role: str, timeout: float | None = None) -> Any:
        """Wait for an editor (dialog window OR tab pane) matching ``title``
        that also exposes ``probe_role``.

        Fakturama opens Order/Invoice/Product/Contact editors as *tabs inside
        the main window* -- they are not top-level windows. We therefore scan
        the desktop for a matching dialog first, then the main window's
        tab/pane descendants. Never binds to the first partial title match;
        the ``probe_role`` must resolve inside the candidate.
        """
        deadline = time.monotonic() + (timeout or self.settings.window_timeout)
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


def _dismiss_owned_dialog_titles(titles: tuple[str, ...], max_rounds: int = 3) -> bool:
    """Dismiss visible top-level windows (INCLUDING owned popups) by title.

    SWT modal dialogs ('position description', 'Select a product', ...) are
    OWNED windows of the main window -- they are NOT reported by
    ``Desktop(backend='uia').windows()`` nor by the main window's UIA
    descendants.  EnumWindows finds them regardless.  Dismissal: restore +
    foreground the dialog, send {ESC} (SWT treats ESC as Cancel), then
    BM_CLICK its Cancel button child as a fallback.
    """
    import ctypes

    user32 = ctypes.windll.user32
    _ENUM_CB = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _matching() -> list[int]:
        found: list[int] = []

        def _cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                text = (buf.value or "").strip().lower()
                if text and any(fragment in text for fragment in titles):
                    found.append(hwnd)
            return True

        try:
            user32.EnumWindows(_ENUM_CB(_cb), 0)
        except Exception:
            return []
        return found

    for _ in range(max_rounds):
        targets = _matching()
        if not targets:
            return True
        for hwnd in targets:
            # BM_CLICK the Cancel button child first: ESC is unreliable because
            # SetForegroundWindow can be blocked by Windows foreground-lock
            # rules, sending the keystroke to the wrong window.
            try:
                cancels: list[int] = []

                def _cb2(child, _):
                    buf = ctypes.create_unicode_buffer(64)
                    user32.GetWindowTextW(child, buf, 64)
                    if (buf.value or "").strip().lower() == "cancel":
                        cancels.append(child)
                    return True

                user32.EnumChildWindows(hwnd, _ENUM_CB(_cb2), 0)
                for child in cancels:
                    user32.SendMessageW(child, 0x00F5, 0, 0)  # BM_CLICK
                    logger.info("dismiss_owned_dialog: BM_CLICK Cancel on hwnd %d", hwnd)
                    time.sleep(0.4)
            except Exception:
                pass
            if user32.IsWindowVisible(hwnd):
                try:
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.4)
                    pywinauto.keyboard.send_keys("{ESC}")
                    time.sleep(0.5)
                except Exception:
                    continue
        time.sleep(0.5)
    return not _matching()


def _dismiss_stray_dialog(main: Any) -> None:
    """Dismiss a stray 'position description' modal if one is present.

    SWT modals are real top-level windows AND can also appear as descendants
    of the main window. A blanket {ESC} to the main window would kill a legit
    cell editor, so we ONLY target windows whose title actually carries the
    dialog name. Safe to call repeatedly.
    """
    import pywinauto

    # A stray 'Save Parts' modal can pop up mid-flow (e.g. an editor close
    # triggered by an ESC or F4 that landed on the wrong window).  Dismiss it
    # with Cancel: the tab stays open, but the modal stops blocking UIA.
    try:
        dismiss_save_parts_dialogs()
    except Exception:
        pass

    # Owned popups first: invisible to UIA desktop/descendant scans.
    try:
        _dismiss_owned_dialog_titles(("position description", "description"))
    except Exception:
        pass

    try:
        for w in pywinauto.Desktop(backend="uia").windows():
            try:
                title = (w.window_text() or "").lower()
                if "position description" in title or title == "description":
                    if w.is_visible():
                        w.set_focus()
                        pywinauto.keyboard.send_keys("{ESC}")
                        time.sleep(0.3)
            except Exception:
                continue
    except Exception:
        pass
    try:
        for c in main.descendants():
            try:
                if (
                    c.element_info.control_type == "Window"
                    and "position description" in (c.window_text() or "").lower()
                ):
                    c.set_focus()
                    pywinauto.keyboard.send_keys("{ESC}")
                    time.sleep(0.3)
            except Exception:
                continue
    except Exception:
        pass


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
            # 0. Any modal dialog must be dismissed first: it would swallow the
            #    set_focus/click/type_keys calls below and can open mid-stabilize
            #    right after a cell edit commits.
            _dismiss_stray_dialog(main)

            # 1. Focus the active editor window/pane.
            try:
                ctx.window().set_focus()
            except Exception:
                main.set_focus()
            time.sleep(0.2)

            # 2. Click the editor body composite to trigger SWT lazy layout.
            clicked = False
            try:
                body = _editor_body(main)
                if body is not None:
                    rect = body.rectangle()
                    main.click_input(
                        coords=(
                            rect.left + int(rect.width() * 0.3),
                            rect.top + min(120, int(rect.height() * 0.3)),
                        )
                    )
                    clicked = True
            except Exception:
                pass
            if not clicked:
                # Fallback 1: click just below the header row (the wide Edit
                # that is reliably rendered at the top of a fresh editor).
                try:
                    for c in main.descendants(control_type="Edit"):
                        r = c.rectangle()
                        if r.top < 200 and r.left > 500 and r.width() > 300:
                            main.click_input(coords=(r.left + 150, r.bottom + 25))
                            clicked = True
                            break
                except Exception:
                    pass
            if not clicked:
                # Fallback 2: generic point inside the editor area.
                try:
                    main.click_input(coords=(600, 250))
                except Exception:
                    pass
            time.sleep(0.25)

            # 3. Traverse the form to trigger lazy layout.
            for _ in range(8):
                main.type_keys("{TAB}", pause=0.03)
            time.sleep(0.3)

            # 3b. Tab traversal can land focus in the Description column and
            #     reopen the dialog; dismiss again before checking the section.
            _dismiss_stray_dialog(main)

            # 4. Section instantiated?
            if _has_section(main, wait_control):
                # NOTE: do NOT send ^{HOME} here — Ctrl+Home in the SWT
                # CTabFolder activates the FIRST tab (the 'Fa' Fakturama start
                # page), flipping the active editor away and hiding its Items
                # section from UIA for the rest of the flow.
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
