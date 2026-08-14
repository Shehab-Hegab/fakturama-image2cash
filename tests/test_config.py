"""Tests for runtime configuration loading (config.py)."""

from __future__ import annotations

import pytest

from fakturama_i2c.config import ConfigError, Settings


def test_from_env_respects_overrides(monkeypatch) -> None:
    monkeypatch.setenv("I2C_WINDOW_TIMEOUT", "42")
    monkeypatch.setenv("I2C_STABLE_POLLS", "7")
    monkeypatch.setenv("I2C_OCR_ENGINE", "tesseract")
    monkeypatch.setenv("I2C_LLM_PROVIDER", "openai")
    monkeypatch.setenv("I2C_DRY_RUN", "true")
    monkeypatch.setenv("I2C_CURRENCY", "USD")

    settings = Settings.from_env()

    assert settings.window_timeout == 42.0
    assert settings.stable_polls == 7
    assert settings.ocr_engine == "tesseract"
    assert settings.llm_provider == "openai"
    assert settings.dry_run is True
    assert settings.currency == "USD"


def test_from_env_defaults_when_unset() -> None:
    settings = Settings()
    assert settings.ocr_engine == "mock"
    assert settings.llm_provider == "mock"
    assert settings.window_timeout == 15.0


def test_unsupported_llm_provider_raises(monkeypatch) -> None:
    monkeypatch.setenv("I2C_LLM_PROVIDER", "watson")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_unsupported_ocr_engine_raises(monkeypatch) -> None:
    monkeypatch.setenv("I2C_OCR_ENGINE", "kraken")
    with pytest.raises(ConfigError):
        Settings.from_env()