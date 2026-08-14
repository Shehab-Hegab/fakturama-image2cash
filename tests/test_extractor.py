"""Tests for the extraction pipeline and the mock OCR/LLM sidecar engines."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from fakturama_i2c.config import Settings
from fakturama_i2c.extraction.extractor import Extractor, reconcile_totals
from fakturama_i2c.extraction.llm import MockLlmProvider
from fakturama_i2c.extraction.ocr import MockOcrEngine
from fakturama_i2c.utils.errors import ExtractionError


def _write_sidecar(tmp_path: Path, name: str, payload: dict) -> Path:
    image = tmp_path / name
    image.write_bytes(b"fake-png-bytes")
    image.with_suffix(".json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return image


def test_mock_ocr_reads_sidecar(tmp_path: Path) -> None:
    image = tmp_path / "doc.png"
    image.write_bytes(b"x")
    image.with_suffix(".txt").write_text("RE123\n19% VAT\n", encoding="utf-8")
    assert MockOcrEngine().extract_text(image) == "RE123\n19% VAT\n"


def test_mock_ocr_missing_sidecar_raises(tmp_path: Path) -> None:
    image = tmp_path / "doc.png"
    image.write_bytes(b"x")
    with pytest.raises(ExtractionError):
        MockOcrEngine().extract_text(image)


def test_mock_llm_round_trips(sample_order, tmp_path: Path) -> None:
    provider = MockLlmProvider(Settings())
    image = _write_sidecar(tmp_path, "doc.png", sample_order.model_dump(mode="json"))
    assert provider.extract_order(image) == sample_order


def test_mock_llm_missing_sidecar_raises(tmp_path: Path) -> None:
    image = tmp_path / "doc.png"
    image.write_bytes(b"x")
    with pytest.raises(ExtractionError):
        MockLlmProvider(Settings()).extract_order(image)


def test_extractor_round_trip_without_warnings(sample_order, settings, tmp_path: Path) -> None:
    image = _write_sidecar(tmp_path, "order.png", sample_order.model_dump(mode="json"))
    report = Extractor(settings).run(image)
    assert report.order == sample_order
    assert report.warnings == []


def test_extractor_reports_reconcile_warnings_on_mismatch(sample_order, settings, tmp_path: Path) -> None:
    payload = sample_order.model_dump(mode="json")
    payload["totals"]["total_net"] = "41.25"
    image = _write_sidecar(tmp_path, "order.png", payload)
    report = Extractor(settings).run(image)
    assert report.order.totals.total_net == 41.25
    assert any(w.startswith("net:") for w in report.warnings)
    assert any("!=" in w for w in report.warnings)


def test_extractor_missing_image_raises(settings, tmp_path: Path) -> None:
    with pytest.raises(ExtractionError):
        Extractor(settings).run(tmp_path / "does-not-exist.png")


def test_reconcile_totals_matches_when_consistent(sample_order) -> None:
    result = reconcile_totals(sample_order)
    for metric in ("net", "vat", "gross"):
        assert result[metric]["extracted"] == result[metric]["computed"]


def test_reconcile_totals_flags_mismatch(sample_order) -> None:
    totals = sample_order.model_dump()["totals"]
    totals["total_net"] = Decimal("99")
    mismatched = sample_order.model_validate({**sample_order.model_dump(), "totals": totals})
    result = reconcile_totals(mismatched)
    assert result["net"]["extracted"] != result["net"]["computed"]