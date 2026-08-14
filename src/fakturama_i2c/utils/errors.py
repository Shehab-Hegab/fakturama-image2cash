"""Shared error types.

The AMBIGUITY POLICY of the assignment is encoded here: whenever the automation
cannot make an exact, unambiguous decision it raises ``ManualReviewError`` and
stops instead of guessing. The runner catches it, takes an annotated screenshot,
and reports the precise step + reason to the user.
"""

from __future__ import annotations


class I2CError(Exception):
    """Base class for all Image-to-Cash errors."""

    step: str = "unknown"
    detail: str = ""

    def __init__(
        self,
        *args: object,
        step: str | None = None,
        detail: str | None = None,
    ) -> None:
        if args:
            super().__init__(*args)
            if len(args) >= 2:
                if step is None:
                    step = str(args[0])
                if detail is None:
                    detail = str(args[1])
            elif detail is None:
                detail = str(args[0])
        elif detail is not None:
            super().__init__(str(detail))
        else:
            super().__init__()
        if step is not None:
            self.step = step
        if detail is not None:
            self.detail = detail


class ExtractionError(I2CError):
    """Source image could not be extracted/validated."""


class ControlNotFoundError(I2CError):
    """A UI control expected by a step could not be discovered.

    Raised only after the finder exhausted its ordered fallback strategies and
    the stability-wait budget -- this signals a real layout mismatch, not a
    transient race.
    """


class ManualReviewError(I2CError):
    """The automation reached an ambiguous/conflicting state and stops.

    Per the assignment: never guess. Exact-match-only selection; anything that
    is missing, duplicate, or conflicting halts the flow here.
    """

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"[Manual review needed @ {step}] {detail}")
        self.step = step
        self.detail = detail


class FlowTimeoutError(I2CError):
    """A wait-for-stable / wait-for-window budget was exhausted."""


class NotSavedError(I2CError):
    """A save action was issued but the persisted record could not be verified."""
