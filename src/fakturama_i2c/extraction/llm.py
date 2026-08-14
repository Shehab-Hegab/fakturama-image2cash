"""Structured vision-LLM extraction.

The heavy lifting of Step 1.1 is done by a vision LLM that receives the order
image (and optional OCR text as a hint) and must return **strict JSON** that
pydantic validates against :class:`~fakturama_i2c.models.ExtractedOrder`.

Two providers ship:

* ``mock``         -- read a sidecar ``<image>.json`` (offline/dev/tests).
* ``openai``       -- any OpenAI-compatible ``/chat/completions`` endpoint
                      (OpenAI, Groq, or a self-hosted gateway via
                      ``I2C_LLM_BASE_URL``). Vision-capable model required.

Failures validate against the schema up to ``retry_attempts``; a run that
cannot produce valid JSON raises :class:`ExtractionError` -- the flow never
proceeds with partial data.
"""

from __future__ import annotations

import abc
import base64
import json
import os
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..models import ExtractedOrder
from ..utils.errors import ExtractionError
from ..utils.logging import get_logger

logger = get_logger("extraction.llm")


SYSTEM_PROMPT = """\
You are an order-extraction engine. Read the attached order/purchase image and
output ONE strict JSON object -- no markdown, no commentary, no code fence.

The JSON must match this exact structure (every field listed must be present;
use null / "" / 0 for missing values, never invent data):

{
  "header": {
    "order_date": "YYYY-MM-DD",              // from the image, REQUIRED
    "external_reference": "string",          // order/customer reference, if any
    "price_mode": "Net" | "Gross",
    "vat_mode": "With VAT" | "Without VAT",
    "overall_discount_percent": 0,           // number, 0 if none
    "shipping_amount": 0,                    // number, 0 if free
    "shipping_is_free": true
  },
  "debtor": {
    "company": "string",
    "first_name": "string",
    "last_name": "string",
    "salutation": "string",                  // use "---" when not supplied
    "alias": "string",
    "billing_address": {
      "street": "string", "zip_code": "string", "city": "string",
      "country": "string", "email": "string", "telephone": "string"
    },
    "delivery_address": {                    // same shape as billing_address
      "street": "string", "zip_code": "string", "city": "string",
      "country": "string", "email": "string", "telephone": "string"
    },
    "same_delivery_address": false,          // true only when the image shows
                                             // ONE address used for both roles
    "payment_method": "string",              // exact wording from the image
    "price_mode": "Net" | "Gross",
    "discount_percent": 0
  },
  "items": [
    {
      "sku": "string",                       // REQUIRED, non-empty
      "description": "string",
      "quantity": 1,
      "unit_net_price": 0,                   // unit net price (before VAT)
      "vat_percent": 0,
      "discount_percent": 0
    }
  ],
  "totals": {
    "total_net": 0,
    "total_vat": 0,
    "total_gross": 0,
    "currency": "EUR"
  },
  "payment": {
    "paid_status": "Unpaid" | "Deposit" | "Paid",
    "payment_date": "YYYY-MM-DD" | null,     // only if the image shows one
    "payment_method": "string"
  }
}

RULES:
- Transcribe company names, SKUs, descriptions and payment methods VERBATIM.
- Keep item rows exactly as printed; do not merge or drop lines.
- Quantities and prices as plain numbers; do not apply any formula or rounding.
- If totals are printed on the image, copy them exactly; never compute them.
- "paid_status": "Paid" ONLY when the image explicitly marks the invoice/order
  as paid; otherwise "Unpaid".
"""


class StructuredLlm(abc.ABC):
    """Turn an order image into a validated :class:`ExtractedOrder`."""

    @abc.abstractmethod
    def extract_order(self, image: Path, ocr_hint: str = "") -> ExtractedOrder:
        ...


class MockLlmProvider(StructuredLlm):
    """Load a sidecar ``<image>.json`` -- for offline/dev/tests.

    The sidecar may optionally be a dict ``{"extracted_order": {...}}``; the
    plain object form is also accepted.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def extract_order(self, image: Path, ocr_hint: str = "") -> ExtractedOrder:
        sidecar = image.with_suffix(".json")
        if not sidecar.exists():
            raise ExtractionError(
                step="extract.llm",
                detail=f"mock LLM: sidecar JSON not found: {sidecar}",
            )
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                step="extract.llm",
                detail=f"mock LLM: sidecar {sidecar.name} is not valid JSON: {exc}",
            ) from exc
        if isinstance(payload, dict) and "extracted_order" in payload:
            payload = payload["extracted_order"]
        try:
            return ExtractedOrder.model_validate(payload)
        except Exception as exc:
            raise ExtractionError(
                step="extract.llm",
                detail=f"mock LLM: sidecar {sidecar.name} failed validation: {exc}",
            ) from exc


class OpenAiCompatibleProvider(StructuredLlm):
    """Vision LLM via an OpenAI-compatible ``/chat/completions`` endpoint.

    Sends the image as a base64 ``data:`` URL together with the strict schema
    prompt. Parses the reply as JSON and validates it with pydantic, retrying
    on schema/validation failures up to ``settings.retry_attempts``.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = (settings.llm_base_url or "https://api.openai.com/v1").rstrip("/")
        self._api_key = settings.llm_api_key or os.getenv("OPENAI_API_KEY", "")
        self._model = settings.llm_model or "gpt-4o"

    def extract_order(self, image: Path, ocr_hint: str = "") -> ExtractedOrder:
        import requests  # imported lazily; requests is a runtime dependency

        content: list[dict] = [{"type": "text", "text": SYSTEM_PROMPT}]
        if ocr_hint.strip():
            content.append(
                {
                    "type": "text",
                    "text": (
                        "OCR hint (may be noisy / optional, the image is "
                        f"authoritative):\n{ocr_hint[:4000]}"
                    ),
                }
            )
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{self._encode(image)}"},
            }
        )

        last_error: Optional[Exception] = None
        for attempt in range(1, self._settings.retry_attempts + 1):
            try:
                resp = requests.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": content},
                        ],
                        "temperature": 0,
                        "max_tokens": 4096,
                    },
                    timeout=self._settings.llm_timeout,
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"]
                order = self._parse(raw)
                logger.info("llm extraction ok on attempt %d", attempt)
                return order
            except Exception as exc:  # noqa: BLE001 - retryable in nature
                last_error = exc
                logger.warning("llm extraction attempt %d failed: %s", attempt, exc)
        raise ExtractionError(
            step="extract.llm",
            detail=f"vision LLM could not produce a valid ExtractedOrder: {last_error}",
        )

    def _parse(self, raw: str) -> ExtractedOrder:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("```", 2)[1]
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM reply is not valid JSON: {exc}") from exc
        if isinstance(payload, dict) and "extracted_order" in payload:
            payload = payload["extracted_order"]
        try:
            return ExtractedOrder.model_validate(payload)
        except Exception as exc:
            raise ValueError(f"LLM JSON failed schema validation: {exc}") from exc

    def _encode(self, image: Path) -> str:
        return base64.b64encode(image.read_bytes()).decode("ascii")


def get_llm_provider(settings: Settings) -> StructuredLlm:
    """Factory for the configured vision-LLM provider."""
    providers: dict[str, type[StructuredLlm]] = {
        "mock": MockLlmProvider,
        "openai": OpenAiCompatibleProvider,
        "groq": OpenAiCompatibleProvider,
        "anthropic": OpenAiCompatibleProvider,
    }
    cls = providers.get(settings.llm_provider)
    if cls is None:
        raise ExtractionError(
            step="extract.llm", detail=f"unknown llm_provider: {settings.llm_provider}"
        )
    logger.info("llm provider: %s", settings.llm_provider)
    return cls(settings)