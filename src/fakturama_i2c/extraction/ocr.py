"""OCR engines.

The extraction layer is deliberately OCR-engine-agnostic. Only one engine is
active per run (``Settings.ocr_engine``). ``mock`` reads the OCR text from a
sidecar ``.txt`` file next to the image -- the fastest way to run the whole
pipeline offline and to write deterministic tests.
"""

from __future__ import annotations

import abc
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..config import Settings
from ..utils.errors import ExtractionError
from ..utils.logging import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger("extraction.ocr")


class OcrEngine(abc.ABC):
    """Extract raw text lines from an order image."""

    @abc.abstractmethod
    def extract_text(self, image: Path) -> str:
        ...


class MockOcrEngine(OcrEngine):
    """Read OCR text from ``<image>.txt`` -- for offline/dev/tests."""

    def extract_text(self, image: Path) -> str:
        sidecar = image.with_suffix(".txt")
        if sidecar.exists():
            return sidecar.read_text(encoding="utf-8")
        raise ExtractionError(
            step="extract.ocr",
            detail=f"mock OCR: sidecar text not found: {sidecar}",
        )


class TesseractOcrEngine(OcrEngine):
    """pytesseract wrapper (German + English dictionaries)."""

    def extract_text(self, image: Path) -> str:
        try:
            import pytesseract
            from PIL import Image
        except ImportError as exc:  # pragma: no cover
            raise ExtractionError(
                step="extract.ocr",
                detail="pytesseract/Pillow not installed for the tesseract engine",
            ) from exc
        try:
            with Image.open(image) as img:
                return pytesseract.image_to_string(
                    img, lang="deu+eng", config="--psm 6"
                )
        except Exception as exc:  # pragma: no cover
            raise ExtractionError(
                step="extract.ocr", detail=f"tesseract failed: {exc}"
            ) from exc


class EasyOcrEngine(OcrEngine):
    """easyocr wrapper (auto-detect language, CPU)."""

    def extract_text(self, image: Path) -> str:
        try:
            import easyocr  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover
            raise ExtractionError(
                step="extract.ocr",
                detail="easyocr not installed for the easyocr engine",
            ) from exc
        try:
            reader = easyocr.Reader(["en", "de"], gpu=False, verbose=False)
            results = reader.readtext(str(image), detail=0, paragraph=True)
            return "\n".join(results)
        except Exception as exc:  # pragma: no cover
            raise ExtractionError(
                step="extract.ocr", detail=f"easyocr failed: {exc}"
            ) from exc


def get_ocr_engine(settings: Settings) -> OcrEngine:
    """Factory for the configured OCR engine."""
    engines: dict[str, type[OcrEngine]] = {
        "mock": MockOcrEngine,
        "tesseract": TesseractOcrEngine,
        "easyocr": EasyOcrEngine,
    }
    cls = engines.get(settings.ocr_engine)
    if cls is None:
        raise ExtractionError(
            step="extract.ocr", detail=f"unknown ocr_engine: {settings.ocr_engine}"
        )
    logger.info("ocr engine: %s", settings.ocr_engine)
    return cls()