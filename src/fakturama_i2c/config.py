"""Runtime configuration.

Loads settings from environment variables / a local ``.env`` file. Every
variable uses the ``I2C_`` prefix. ``load_settings()`` validates the minimum
set of values the runner needs before it will touch the desktop.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from .utils.errors import I2CError

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


class ConfigError(I2CError):
    """Invalid or missing configuration."""


def _bool(value: Optional[str], default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(value: Optional[str], default: float) -> float:
    if value is None or value.strip() == "":
        return default
    return float(value)


@dataclass
class Settings:
    # -- Fakturama app -------------------------------------------------------
    fakturama_exe: str = ""                     # empty => attach to running process
    window_title: str = "Fakturama"
    uia_backend: str = "uia"                    # "uia" or "win32"

    # -- Wait budgets (seconds) ----------------------------------------------
    window_timeout: float = 15.0                # wait for a window/dialog to appear
    stable_timeout: float = 4.0                 # wait for a list to stop changing
    stable_polls: int = 5                       # consecutive equal snapshots required
    stable_poll_interval: float = 0.35
    control_timeout: float = 8.0                # wait for a single control
    retry_attempts: int = 2                     # retries around transient failures

    # -- Extraction ----------------------------------------------------------
    ocr_engine: str = "mock"                    # "mock" | "tesseract" | "easyocr"
    llm_provider: str = "mock"                  # "mock" | "openai" | "groq" | "anthropic"
    llm_base_url: str = ""                      # for OpenAI-compatible endpoints
    llm_model: str = ""
    llm_api_key: str = ""
    llm_timeout: float = 90.0

    # -- Behaviour -----------------------------------------------------------
    dry_run: bool = False                       # log decisions, do NOT touch UI
    screenshot_dir: Path = field(
        default_factory=lambda: _PROJECT_ROOT / "assets" / "screenshots"
    )
    checkpoint_dir: Path = field(
        default_factory=lambda: _PROJECT_ROOT / "assets" / "checkpoints"
    )
    log_file: Optional[Path] = field(
        default_factory=lambda: _PROJECT_ROOT / "assets" / "logs" / "i2c.log"
    )

    # -- Extraction defaults -------------------------------------------------
    currency: str = "EUR"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv(_ENV_FILE)
        s = cls(
            fakturama_exe=os.getenv("I2C_FAKTURAMA_EXE", ""),
            window_title=os.getenv("I2C_WINDOW_TITLE", "Fakturama"),
            uia_backend=os.getenv("I2C_UIA_BACKEND", "uia"),
            window_timeout=_float(os.getenv("I2C_WINDOW_TIMEOUT"), 15.0),
            stable_timeout=_float(os.getenv("I2C_STABLE_TIMEOUT"), 4.0),
            stable_polls=int(os.getenv("I2C_STABLE_POLLS", "5")),
            stable_poll_interval=_float(os.getenv("I2C_STABLE_POLL_INTERVAL"), 0.35),
            control_timeout=_float(os.getenv("I2C_CONTROL_TIMEOUT"), 8.0),
            retry_attempts=int(os.getenv("I2C_RETRY_ATTEMPTS", "2")),
            ocr_engine=os.getenv("I2C_OCR_ENGINE", "mock"),
            llm_provider=os.getenv("I2C_LLM_PROVIDER", "mock"),
            llm_base_url=os.getenv("I2C_LLM_BASE_URL", ""),
            llm_model=os.getenv("I2C_LLM_MODEL", ""),
            llm_api_key=os.getenv("I2C_LLM_API_KEY", ""),
            llm_timeout=_float(os.getenv("I2C_LLM_TIMEOUT"), 90.0),
            dry_run=_bool(os.getenv("I2C_DRY_RUN"), False),
            currency=os.getenv("I2C_CURRENCY", "EUR"),
        )
        if s.llm_provider not in {"mock", "openai", "groq", "anthropic"}:
            raise ConfigError(f"unsupported llm_provider: {s.llm_provider}")
        if s.ocr_engine not in {"mock", "tesseract", "easyocr"}:
            raise ConfigError(f"unsupported ocr_engine: {s.ocr_engine}")
        s.screenshot_dir.mkdir(parents=True, exist_ok=True)
        s.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        if s.log_file:
            s.log_file.parent.mkdir(parents=True, exist_ok=True)
        return s