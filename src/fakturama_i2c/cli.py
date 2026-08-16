"""Command-line entry point for the Fakturama Image-to-Cash automation.

Invoked as ``python -m fakturama_i2c.cli``. Modes:

* ``--dump-tree`` -- print the UIA element tree of the main window and exit.
* ``--dry-run``   -- extraction only; no desktop automation.
* default         -- full flow + verification against Fakturama.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path
from typing import Optional

from .config import Settings
from .utils.errors import I2CError, ManualReviewError
from .utils.logging import get_logger, setup_logging
from .utils.screenshot import capture_window, describe
from .verify.verification import Verifier

logger = get_logger("cli")

_ROOT = Path(__file__).resolve().parent.parent.parent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fakturama_i2c.cli",
        description=(
            "Fakturama Image-to-Cash: one order image becomes a saved and "
            "verified Order plus a linked Invoice."
        ),
    )
    parser.add_argument("--image", type=Path, default=None,
                        help="Path to the source order image.")
    parser.add_argument("--launch", action="store_true",
                        help="Launch Fakturama if not running, else attach.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extraction only; no UI automation.")
    parser.add_argument("--dump-tree", action="store_true",
                        help="Print the UIA element tree of the main window and exit.")
    parser.add_argument("--no-verify", action="store_true",
                        help="Skip post-flow verification.")
    parser.add_argument("--verbose", action="store_true",
                        help="Enable DEBUG logging.")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)

    settings = Settings.from_env()
    if args.dry_run:
        settings.dry_run = True
    setup_logging(settings.log_file, level=logging.DEBUG if args.verbose else logging.INFO)

    try:
        if args.dump_tree:
            return _dump_tree(settings, args.launch)

        image = args.image or _default_sample(settings)
        if image is None:
            print("error: --image is required (or place a sample order image under assets/)")
            return 2

        if args.dry_run:
            return _dry_run(settings, image)
        return _full_run(settings, image, verify=not args.no_verify, launch=args.launch)
    except ManualReviewError as exc:
        _print_failure(exc, settings)
        return 1
    except I2CError as exc:
        _print_failure(exc, settings)
        return 1
    except Exception:  # noqa: BLE001 - unexpected: full traceback, exit 2
        print("error: unexpected failure")
        traceback.print_exc()
        return 2


# -- modes -------------------------------------------------------------------


def _dump_tree(settings: Settings, launch: bool) -> int:
    app = _connect(settings, launch)
    tree_text = describe(app.main_window())
    print(tree_text)
    out = _ROOT / ".elements.txt"
    try:
        out.write_text(tree_text, encoding="utf-8")
        print(f"[+] UI tree saved to {out}")
    except OSError as exc:
        logger.debug("could not save UI tree to %s: %s", out, exc)
    return 0


def _dry_run(settings: Settings, image: Path) -> int:
    from fakturama_i2c.extraction.extractor import Extractor

    report = Extractor(settings).run(image)
    print(_summary_text(report))
    return 0


def _full_run(settings: Settings, image: Path, verify: bool, launch: bool) -> int:
    from fakturama_i2c.flow.runner import run_flow

    report = run_flow(settings, image, launch=launch)
    order = report.order
    print(f"Flow complete: order saved + invoice created for {order.debtor.search_key}.")

    if not verify:
        return 0

    app = _connect(settings, launch)
    verifier = Verifier(settings, app)
    # Check the Invoice editor first: right after the flow it is the active
    # tab of the main window (editors are tabs, not separate windows), so its
    # fields are UIA-exposed.  verify_documents switches to the Documents
    # view, which hides the editor from UIA again.
    pay_report = verifier.verify_invoice_payment(
        expected_method=order.payment.payment_method,
        expected_paid_status=order.payment.paid_status,
        expected_payment_date=order.payment.payment_date,
        expected_value=str(order.totals.total_gross),
    )
    doc_report = verifier.verify_documents(
        expected_reference=order.header.external_reference,
        expected_total=str(order.totals.total_gross),
    )

    print("Verification:")
    for label, rep in (("documents", doc_report), ("invoice_payment", pay_report)):
        status = "PASS" if rep.passed else "FAIL"
        print(f"  [{status}] {label}")
        for name in rep.checks:
            print(f"      {name}: {rep.details.get(name, '')}")
        for name in rep.skipped:
            print(f"      {name}: SKIPPED — {rep.details.get(name, '')}")

    if not (doc_report.passed and pay_report.passed):
        print("error: verification failed")
        return 1
    print("Done: order and linked invoice are saved and verified.")
    return 0


# -- helpers -----------------------------------------------------------------


def _connect(settings: Settings, launch: bool):
    from .ui.app import AppController

    app = AppController(settings)
    if launch:
        app.connect()
    else:
        app.attach()
    return app


def _default_sample(settings: Settings) -> Optional[Path]:
    for candidate in (
        _ROOT / "assets" / "sample-order.png",
        _ROOT / "sample-order.png",
        Path.cwd() / "sample-order.png",
    ):
        if candidate.exists():
            return candidate
    return None


def _summary_text(report) -> str:
    order = report.order
    lines = ["Extraction OK (dry run; no UI was touched)"]
    lines.append(f"  Debtor  : {order.debtor.search_key}")
    lines.append(f"  Items   : {len(order.items)}")
    lines.append(f"  Net     : {order.totals.total_net} {order.totals.currency}")
    lines.append(f"  VAT     : {order.totals.total_vat} {order.totals.currency}")
    lines.append(f"  Gross   : {order.totals.total_gross} {order.totals.currency}")
    if report.reconciliation:
        lines.append("  Reconciliation:")
        for metric, row in report.reconciliation.items():
            mark = "ok" if row["extracted"] == row["computed"] else "MISMATCH"
            lines.append(
                f"    {metric:<6} extracted={row['extracted']} computed={row['computed']} [{mark}]"
            )
    if report.warnings:
        lines.append("  Warnings:")
        for warning in report.warnings:
            lines.append(f"    - {warning}")
    return "\n".join(lines)


def _print_failure(exc: I2CError, settings: Settings) -> None:
    if isinstance(exc, ManualReviewError):
        print("[Manual review needed] automation stopped without guessing.")
        print(f"  step  : {exc.step}")
        print(f"  reason: {exc.detail}")
    else:
        print(f"error [{exc.step}]: {exc.detail or exc}")
    try:
        app = _connect(settings, launch=False)
        path = capture_window(app.main_window(), settings.screenshot_dir, "failure")
        print(f"  artifact: {path}")
    except Exception as capture_exc:  # noqa: BLE001 - best-effort diagnostics
        logger.debug("failure screenshot unavailable: %s", capture_exc)


if __name__ == "__main__":
    sys.exit(main())