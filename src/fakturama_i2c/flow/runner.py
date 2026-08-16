"""Runner: orchestrates extraction + the five UI steps for one source image.

``run_flow`` extracts first, then connects to Fakturama and executes Steps 1-5
in order, all while holding the still-open Order as the state anchor. Every
failure takes a screenshot and re-raises; ``dry_run`` logs decisions and never
touches the UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..config import Settings
from ..extraction.extractor import Extractor, ExtractionReport
from ..ui.app import AppController
from ..ui.registry import ControlFinder
from ..ui.waits import Waits
from ..utils.errors import ControlNotFoundError, FlowTimeoutError, I2CError, ManualReviewError
from ..utils.logging import get_logger
from ..utils.screenshot import capture_window
from .context import FlowContext
from .step1_open_order import step_open_new_order
from .step2_debtor import step_resolve_debtor
from .step3_products import step_add_products
from .step4_complete_order import step_complete_order
from .step5_invoice import step_create_invoice

logger = get_logger("flow.runner")

_FLOW_STEPS = (
    ("step1.open_order", step_open_new_order),
    ("step2.debtor", step_resolve_debtor),
    ("step3.products", step_add_products),
    ("step4.complete_order", step_complete_order),
    ("step5.invoice", step_create_invoice),
)


def run_extract(settings: Settings, image_path) -> ExtractionReport:
    """Extract + reconcile the source image; no UI is touched."""
    logger.info("extracting order from %s", image_path)
    report = Extractor(settings).run(Path(image_path))
    for warning in report.warnings:
        logger.warning("extraction warning: %s", warning)
    return report


def run_flow(settings: Settings, image_path, launch: bool = False) -> ExtractionReport:
    """Run extraction + the five UI steps for one order image.

    ``launch`` propagates to :meth:`AppController.connect` so the CLI's
    ``--launch`` flag also applies to the main flow, not just verification.
    """
    import shutil
    src_p = Path(image_path)
    root_dir = Path(__file__).resolve().parent.parent.parent
    root_sc = root_dir / "screenshots"
    root_sc.mkdir(parents=True, exist_ok=True)
    if src_p.exists():
        try:
            shutil.copyfile(src_p, root_sc / "01_source_order.png")
            shutil.copyfile(src_p, settings.screenshot_dir / "01_source_order.png")
        except Exception as exc:
            logger.debug("could not copy source order screenshot: %s", exc)

    report = run_extract(settings, image_path)
    ctx = FlowContext(
        settings=settings,
        app=AppController(settings),
        waits=Waits(settings),
        finder=ControlFinder(settings),
        extracted=report.order,
        report=report,
    )

    if settings.dry_run:
        for name, step in _FLOW_STEPS:
            logger.info("---- DRY-RUN %s ----", name)
            step(ctx)
        logger.info("=== dry-run complete (UI untouched) ===")
        return report

    try:
        ctx.app.connect(launch=launch)
        _dismiss_stray_dialogs(ctx)
        for name, step in _FLOW_STEPS:
            logger.info("---- %s ----", name)
            step(ctx)
        _final_screenshot(ctx)
        logger.info("=== run complete; checkpoints: %s ===", " -> ".join(ctx.done_steps))
        return report
    except I2CError as exc:
        _failure_screenshot(ctx, exc)
        logger.error("=== flow FAILED at %s: %s ===", exc.step or "unknown", exc.detail or exc)
        raise
    except Exception as exc:  # noqa: BLE001 - unexpected crash: still annotated
        _failure_screenshot(ctx, None)
        logger.error("=== flow CRASHED: %s ===", exc, exc_info=True)
        raise


# ---------------------------------------------------------------------------
# Screenshots (safety net)
# ---------------------------------------------------------------------------


def _failure_screenshot(ctx: FlowContext, exc: Optional[I2CError]) -> None:
    name = f"failure_{exc.step}" if exc is not None and getattr(exc, "step", None) else "failure_unknown"
    win = None
    try:
        win = ctx.window()
    except Exception:
        pass
    if win is None:
        try:
            win = ctx.app.main_window()
        except Exception:
            pass
    if win is not None:
        capture_window(win, ctx.settings.screenshot_dir, name)
    else:
        logger.warning("no window available for %r screenshot", name)


def _final_screenshot(ctx: FlowContext) -> None:
    try:
        capture_window(ctx.window(), ctx.settings.screenshot_dir, "flow_complete")
    except Exception as exc:
        logger.warning("final screenshot failed: %s", exc)


import time

def _dismiss_stray_dialogs(ctx: FlowContext) -> None:
    """Pre-flight: dismiss any leftover modal dialogs from previous runs.

    Eclipse SWT modal dialogs (e.g. 'position description', 'Select the
    address') are child windows of the Fakturama main window and can
    survive across automation runs if the previous run crashed.
    """
    import pywinauto
    try:
        from .context import _dismiss_owned_dialog_titles

        _dismiss_owned_dialog_titles(("position description", "select the address", "select a product", "save parts"))
    except Exception:
        pass
    try:
        main = ctx.app.main_window()
        # Strategy 1: search desktop windows for any stray dialog
        desktop = pywinauto.Desktop(backend="uia")
        for win in desktop.windows():
            try:
                title = (win.window_text() or "").strip().lower()
                if any(kw in title for kw in ("position description", "select the address", "select a product")):
                    if win != main:
                        try:
                            win.set_focus()
                        except Exception:
                            pass
                        pywinauto.keyboard.send_keys("{ESC}")
                        time.sleep(0.5)
                        logger.info("runner: dismissed stray dialog %r", win.window_text())
            except Exception:
                continue
        # Strategy 2: send {ESC} to main window multiple times to clear any modals
        try:
            main.set_focus()
        except Exception:
            pass
        for _ in range(3):
            pywinauto.keyboard.send_keys("{ESC}")
            time.sleep(0.3)
    except Exception as exc:
        logger.debug("runner: stray-dialog cleanup skipped: %s", exc)
