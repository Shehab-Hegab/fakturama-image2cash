"""Wait / stabilization primitives.

Fakturama is a Java SWT app: controls appear asynchronously and tables populate
in batches. Every interaction that depends on a control or a list going through
``Waits`` below so the flow never races the UI.

Key idea -- *stabilization wait*: poll a snapshot function until it returns the
same value ``n`` consecutive times. That is how we detect "the search results
have stopped changing" without any timing guess.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional, Sequence

from ..config import Settings
from ..utils.errors import ControlNotFoundError, FlowTimeoutError

# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------


def retry(
    fn: Callable[[], Any],
    attempts: int = 3,
    delay: float = 0.4,
    logger=None,
    what: str = "operation",
) -> Any:
    """Run ``fn`` up to ``attempts`` times, ignoring transient exceptions."""
    last_exc: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            last_exc = exc
            if logger:
                logger.debug("retry %d/%d of %s failed: %s", i + 1, attempts, what, exc)
            time.sleep(delay)
    if last_exc is not None:
        raise last_exc
    raise FlowTimeoutError(what, "retry exhausted")  # pragma: no cover


# ---------------------------------------------------------------------------
# Stability waiter
# ---------------------------------------------------------------------------


class StableWaiter:
    """Poll ``snapshot()`` until it is unchanged for N consecutive reads.

    Used to wait for search-result tables to finish populating.
    """

    def __init__(self, timeout: float, polls: int, interval: float) -> None:
        self.timeout = timeout
        self.polls = max(2, polls)
        self.interval = interval

    def until_stable(self, snapshot: Callable[[], Any], what: str = "list") -> Any:
        """Return the stable snapshot value.

        Raises FlowTimeoutError when the value keeps changing beyond timeout.
        """
        deadline = time.monotonic() + self.timeout
        prev: Any = None
        equal_count = 0
        while time.monotonic() < deadline:
            try:
                cur = snapshot()
            except Exception:
                cur = None
            if cur == prev and cur is not None:
                equal_count += 1
                if equal_count >= self.polls:
                    return cur
            else:
                equal_count = 0
            prev = cur
            time.sleep(self.interval)
        raise FlowTimeoutError(what, "list did not stabilize")

    def until(self, predicate: Callable[[], bool], what: str) -> None:
        """Poll a predicate until it returns True or timeout."""
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                if predicate():
                    return
            except Exception:
                pass
            time.sleep(self.interval)
        raise FlowTimeoutError(what, "condition never became true")


# ---------------------------------------------------------------------------
# Control / window waiting (thin wrappers over pywinauto timings)
# ---------------------------------------------------------------------------


class Waits:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stable = StableWaiter(
            settings.stable_timeout,
            settings.stable_polls,
            settings.stable_poll_interval,
        )

    # -- windows -------------------------------------------------------------

    def for_window(self, desktop, title: str, timeout: Optional[float] = None) -> Any:
        """Wait until a window whose title contains ``title`` exists.

        Fakturama's select-dialogs (e.g. "Select a product", "Select the address")
        are Eclipse SWT modal child dialogs. They ARE top-level windows (with their
        own title bars), but ``desktop.window(title_re=...)`` often misses them.
        We therefore also scan ``desktop.windows()`` as a fallback.
        """
        timeout = timeout or self.settings.window_timeout
        deadline = time.monotonic() + timeout
        last_exc: Optional[Exception] = None
        title_lower = title.lower()
        while time.monotonic() < deadline:
            # Fast path: desktop.window() spec match.
            try:
                win = desktop.window(title_re=f".*{title}.*")
                if win.exists(timeout=0.3):
                    return win
            except Exception as exc:
                last_exc = exc
            # Slow path: enumerate all visible top-level windows (modal dialogs).
            try:
                for w in desktop.windows():
                    try:
                        wt = (w.window_text() or "").strip()
                        if wt and title_lower in wt.lower():
                            return w
                    except Exception:
                        continue
            except Exception as exc:
                last_exc = exc
            time.sleep(0.3)
        raise FlowTimeoutError(f"window '{title}'", str(last_exc or "window not found")) from last_exc

    def for_control(self, window_spec, control_type: Optional[str] = None,
                    auto_id: Optional[str] = None,
                    title: Optional[str] = None,
                    timeout: Optional[float] = None) -> Any:
        """Wait until a control with the given property signature exists."""
        timeout = timeout or self.settings.control_timeout
        kwargs: dict[str, Any] = {}
        if control_type:
            kwargs["control_type"] = control_type
        if auto_id:
            kwargs["auto_id"] = auto_id
        if title:
            kwargs["title"] = title
        deadline = time.monotonic() + timeout
        last_exc: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                ctrl = window_spec.child_window(**kwargs)
                if ctrl.exists(timeout=0.3):
                    return ctrl
            except Exception as exc:
                last_exc = exc
            time.sleep(0.3)
        raise FlowTimeoutError(f"control {kwargs}", str(last_exc or "control not found")) from last_exc

    # -- stable list snapshots ------------------------------------------------

    def stable_snapshot(self, table_control) -> Sequence[Any]:
        """Snapshot the row identity of a pywinauto table as a stable list."""
        def _snap() -> list[Any]:
            try:
                rows = table_control.rows()
            except Exception:
                return []
            sig = []
            for row in rows[:60]:
                try:
                    sig.append(tuple(c.texts() for c in row.children()))
                except Exception:
                    sig.append("?")
            return sig

        return self._stable.until_stable(_snap, "table contents")

    def stable_count(self, list_control) -> int:
        """Wait for the visible item count of a List control to stabilize."""

        def _snap() -> Optional[int]:
            try:
                return list_control.item_count()
            except Exception:
                return None

        return self._stable.until_stable(_snap, "list item count")

    def stable_text(self, edit_control) -> str:
        """Wait until an Edit control's text stops changing (typing/debounce)."""

        def _snap() -> Optional[str]:
            try:
                return edit_control.get_value()
            except Exception:
                return None

        return self._stable.until_stable(_snap, "edit text")

    # -- convenience ----------------------------------------------------------

    def is_visible(self, window_spec, **kwargs) -> bool:
        try:
            return window_spec.child_window(**kwargs).exists(timeout=1.5)
        except Exception:
            return False