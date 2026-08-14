"""Screenshot + UI-dump helpers used for diagnostics and the safety net.

On any failure the runner captures a screenshot of the affected window and
dumps the UIA element tree so a human can review exactly where the flow was.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pywinauto import Desktop, timings

from .logging import get_logger

logger = get_logger("utils.screenshot")


def capture_window(
    window,
    out_dir: Path,
    name: str,
    element_dump: bool = True,
) -> Path:
    """Save a PNG of ``window`` plus (optionally) a UIA element-tree dump.

    ``window`` is a pywinauto WindowSpecification or HwndWrapper.
    Returns the path of the saved screenshot.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    png = out_dir / f"{safe}.png"
    txt = out_dir / f"{safe}.elements.txt"
    try:
        window.capture_as_image().save(str(png))
        logger.info("screenshot saved: %s", png)
    except Exception as exc:  # pragma: no cover - best effort
        logger.warning("screenshot failed: %s", exc)
    if element_dump:
        try:
            with open(txt, "w", encoding="utf-8") as fh:
                fh.write(describe(window))
            logger.info("element dump saved: %s", txt)
        except Exception as exc:  # pragma: no cover
            logger.warning("element dump failed: %s", exc)
    return png


def describe(window, max_depth: int = 6) -> str:
    """Render a compact UIA tree for a window (for the element dump)."""
    lines: list[str] = []

    def _walk(control, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            info = _control_signature(control)
        except Exception:
            return
        lines.append("  " * depth + info)
        try:
            children = control.children()
        except Exception:
            return
        for child in children[:40]:
            _walk(child, depth + 1)

    try:
        lines.append(_control_signature(window))
        for child in window.children()[:40]:
            _walk(child, 1)
    except Exception as exc:  # pragma: no cover
        lines.append(f"<error dumping window: {exc}>")
    return "\n".join(lines)


def _control_signature(control) -> str:
    try:
        auto_id = control.element_info.automation_id
    except Exception:
        auto_id = None
    try:
        ctrl_type = control.element_info.control_type
    except Exception:
        ctrl_type = None
    try:
        name = control.window_text()
    except Exception:
        name = None
    cls = None
    try:
        cls = control.element_info.class_name
    except Exception:
        pass
    return (
        f"type={ctrl_type} id={auto_id!r} class={cls!r} "
        f"name={name!r} rect={_rect(control)}"
    )


def _rect(control) -> str:
    try:
        r = control.rectangle()
        return f"[{r.left},{r.top},{r.right},{r.bottom}]"
    except Exception:
        return "[?]"


def desktop_dump() -> str:
    """Best-effort dump of all top-level desktop windows (diagnostics)."""
    lines: list[str] = []
    for win in Desktop(backend="uia").windows():
        try:
            lines.append(f"{win.window_text()!r} :: {win.class_name()}")
        except Exception:
            continue
    return "\n".join(lines)