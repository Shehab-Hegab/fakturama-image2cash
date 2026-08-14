"""Structured logging for the whole package."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

_CONFIGURED = False


def setup_logging(log_file: Optional[Path] = None, level: int = logging.INFO) -> None:
    """Configure root handler once; subsequent calls are no-ops."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    _CONFIGURED = True

    root = logging.getLogger("fakturama_i2c")
    root.setLevel(level)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:  # pragma: no cover - logging must never crash the run
            pass


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"fakturama_i2c.{name}")