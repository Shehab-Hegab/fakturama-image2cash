"""Application controller: launch or attach to Fakturama and expose windows.

Attach-by-title is the default (the assignment's "no fixed layout" rule
extends to not depending on a specific launch path). ``--launch`` support lets
the runner start Fakturama from ``I2C_FAKTURAMA_EXE`` when needed.
"""

from __future__ import annotations

import subprocess
import time
from typing import Optional

from pywinauto import Application, Desktop

from ..config import Settings
from ..utils.errors import ControlNotFoundError, FlowTimeoutError
from ..utils.logging import get_logger

logger = get_logger("ui.app")


class AppController:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.app: Optional[Application] = None
        self._desktop: Optional[Desktop] = None

    # -- lifecycle -----------------------------------------------------------

    def launch(self) -> None:
        """Start Fakturama from ``I2C_FAKTURAMA_EXE``."""
        exe = self.settings.fakturama_exe
        if not exe:
            raise ControlNotFoundError("launch", "I2C_FAKTURAMA_EXE not configured")
        logger.info("launching Fakturama: %s", exe)
        subprocess.Popen([exe], cwd=None)
        self.wait_for_main_window(self.settings.window_timeout)

    def _pick_main_element(self):
        """Pick the best Fakturama top-level window element.

        Multiple windows often match the title (splash / initialization dialog
        plus the real main window). Rank by visibility, real SWT windows first,
        and deprioritise splash / initialization titles.
        """
        from pywinauto import findwindows

        els = findwindows.find_elements(
            title_re=f".*{self.settings.window_title}.*",
            backend=self.settings.uia_backend,
        )
        candidates = [
            e
            for e in els
            if getattr(e, "visible", True) and (e.name or "").strip()
        ]

        def rank(e):
            name = (e.name or "").lower()
            splash = 1 if ("initialization" in name or "splash" in name) else 0
            not_swt = 0 if (e.class_name or "").startswith("SWT_Window") else 1
            return (splash, not_swt)

        candidates.sort(key=rank)
        if not candidates:
            raise ControlNotFoundError(
                "attach",
                f"no Fakturama window matching title {self.settings.window_title!r}",
            )
        return candidates[0]

    def attach(self) -> None:
        """Attach to an already running Fakturama instance by window title."""
        logger.info("attaching to Fakturama (title=%r)", self.settings.window_title)
        element = self._pick_main_element()
        self.app = Application(backend=self.settings.uia_backend).connect(
            handle=element.handle
        )
        self.wait_for_main_window(self.settings.window_timeout)

    def connect(self, launch: bool = False) -> None:
        """Connect: attach to a running instance, launching when asked/configured.

        ``launch=True`` (the CLI ``--launch`` flag) forces a launch whenever no
        running instance can be attached to. Launching without a configured
        ``I2C_FAKTURAMA_EXE`` raises ``ControlNotFoundError``.
        """
        if self.settings.fakturama_exe or launch:
            try:
                self.attach()
                return
            except Exception:
                logger.info("no running instance found; launching Fakturama")
                self.launch()
                return
        self.attach()

    def wait_for_main_window(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.app is not None:
                    win = self.app.top_window()
                    if win and win.exists():
                        return
            except Exception:
                pass
            time.sleep(0.3)
        raise FlowTimeoutError("main window", "Fakturama main window not found")

    # -- windows -------------------------------------------------------------

    @property
    def desktop(self) -> Desktop:
        if self._desktop is None:
            self._desktop = Desktop(backend=self.settings.uia_backend)
        return self._desktop

    def main_window(self):
        """The Fakturama main window (or the last active top window)."""
        if self.app is None:
            self.attach()
        win = self.app.top_window()
        if not win.exists():
            raise ControlNotFoundError("main_window", "no Fakturama top window")
        return win

    def window_by_title(self, title: str, timeout: float = 15.0):
        """Find a top-level window whose title contains ``title``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for win in self.desktop.windows():
                try:
                    if title.lower() in win.window_text().lower():
                        return win
                except Exception:
                    continue
            time.sleep(0.3)
        raise FlowTimeoutError(f"window '{title}'", "window not found on desktop")

    def dialogs(self) -> list:
        """All currently open dialogs whose title is not the main window title."""
        out = []
        main_text = ""
        try:
            main_text = self.main_window().window_text().lower()
        except Exception:
            pass
        for win in self.desktop.windows():
            try:
                text = win.window_text()
                if text and text.lower() != main_text and not text.startswith(" "):
                    out.append(win)
            except Exception:
                continue
        return out