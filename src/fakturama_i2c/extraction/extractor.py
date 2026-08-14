"""Extraction orchestrator: image -> validated :class:`ExtractedOrder`.

Pipeline (Step 1.1):

    image
      -> OCR text (optional hint)          [ocr.py]
      -> vision LLM strict JSON            [llm.py]
      -> pydantic validation               [models.py]
      -> line-total reconciliation gate    (warn-only, never silently fix)

The reconciliation step recomputes the extracted totals from the line items
using the *spec formulas* (pricing.py) and appends a warning whenever they do
not match the totals printed on the source. Per the assignment the flow does
NOT guess which is right -- the mismatch is surfaced to the user/review step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..models import ExtractedOrder, ItemLine
from ..utils.errors import ExtractionError
from ..utils.logging import get_logger
from .llm import StructuredLlm, get_llm_provider
from .ocr import OcrEngine, get_ocr_engine

logger = get_logger("extraction.extractor")


@dataclass
class ExtractionReport:
    """Result of a full extraction run."""

    order: ExtractedOrder
    ocr_text: str = ""
    warnings: list[str] = field(default_factory=list)
    reconciliation: dict[str, dict[str, str]] = field(default_factory=dict)


def _sum(items: list[ItemLine], attr: str) -> Decimal:
    from decimal import ROUND_HALF_UP

    total = Decimal("0")
    for item in items:
        total += getattr(item, attr)
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def reconcile_totals(order: ExtractedOrder) -> dict[str, dict[str, str]]:
    """Compare extracted totals against the spec formulas on the line items.

    Returns a ``{metric: {"extracted": ..., "computed": ...}}`` map. Mismatches
    are reported (never auto-corrected) so a human can review the source.
    """
    computed_net = _sum(order.items, "line_price") + order.header.shipping_amount
    computed_vat = _sum(order.items, "line_gross") - _sum(order.items, "line_price")
    computed_gross = computed_net + computed_vat

    def _fmt(value: Decimal) -> str:
        return f"{value:,.2f}"

    out = {
        "net": {
            "extracted": _fmt(order.totals.total_net),
            "computed": _fmt(computed_net),
        },
        "vat": {
            "extracted": _fmt(order.totals.total_vat),
            "computed": _fmt(computed_vat),
        },
        "gross": {
            "extracted": _fmt(order.totals.total_gross),
            "computed": _fmt(computed_gross),
        },
    }
    return out


class Extractor:
    """High-level entry point for Step 1.1."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._ocr: OcrEngine = get_ocr_engine(settings)
        self._llm: StructuredLlm = get_llm_provider(settings)

    def run(self, image: Path) -> ExtractionReport:
        if not image.exists():
            raise ExtractionError(step="extract", detail=f"image not found: {image}")

        ocr_text = ""
        if self._settings.ocr_engine != "mock":
            ocr_text = self._ocr.extract_text(image)

        order = self._llm.extract_order(image, ocr_hint=ocr_text)

        reconciliation = reconcile_totals(order)
        warnings = [
            f"{metric}: extracted {row['extracted']} != computed {row['computed']}"
            for metric, row in reconciliation.items()
            if row["extracted"] != row["computed"]
        ]
        if warnings:
            logger.warning("totals reconciliation mismatch: %s", "; ".join(warnings))

        report = ExtractionReport(
            order=order,
            ocr_text=ocr_text,
            warnings=warnings,
            reconciliation=reconciliation,
        )
        logger.info(
            "extraction ok: %s items, debtor=%r, totals=%s",
            len(order.items),
            order.debtor.search_key,
            {k: v["extracted"] for k, v in reconciliation.items()},
        )
        return report