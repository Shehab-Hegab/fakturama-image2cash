"""Fakturama Image-to-Cash Automation package."""

import os

__version__ = "0.1.0"


def _attach_fakturama_desktop() -> None:
    """Ensure the process/thread attaches to the desktop hosting Fakturama."""
    try:
        import win32con, win32gui, win32service
        hwinsta = win32service.OpenWindowStation("WinSta0", False, win32con.MAXIMUM_ALLOWED)
        hwinsta.SetProcessWindowStation()
        target_desk = None
        for d_name in hwinsta.EnumDesktops():
            try:
                hdesk = win32service.OpenDesktop(d_name, 0, False, win32con.MAXIMUM_ALLOWED)
                found = []
                def _cb(h, _):
                    t = (win32gui.GetWindowText(h) or "").strip()
                    c = (win32gui.GetClassName(h) or "").strip()
                    if "fakturama" in t.lower() and c.startswith("SWT_Window") and win32gui.IsWindowVisible(h):
                        found.append(h)
                try:
                    win32gui.EnumDesktopWindows(hdesk, _cb, None)
                except Exception:
                    pass
                if found:
                    target_desk = hdesk
                    break
            except Exception:
                pass
        if target_desk:
            target_desk.SetThreadDesktop()
    except Exception:
        pass


_attach_fakturama_desktop()

