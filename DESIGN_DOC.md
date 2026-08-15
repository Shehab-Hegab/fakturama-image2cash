# Fakturama Image-to-Cash Automation - Design Document

## Goal

Convert one purchase-order image into a saved Fakturama Order and a linked,
paid Invoice. The workflow is Order-first: the open Order is retained while
Debtor and Product selectors determine whether master records already exist.
Missing records are created only after an exact-match search fails.

## Architecture

1. **Extraction.** OCR provides a transcript and a vision/LLM provider returns
   schema-constrained data. Pydantic normalizes dates, decimals, VAT and
   discounts; a reconciliation gate confirms that line arithmetic equals the
   extracted totals before UI work begins.
2. **UI grounding.** The UI layer is the only code that drives Fakturama. It
   resolves semantic roles through UIA `automation_id`, control type, name and
   ancestor-scoped signatures. It never targets a screen pixel or assumes a
   fixed resolution. SWT virtual tables use `search`, `DOWN`, `ENTER` keyboard
   traversal when their rows are unavailable to UIA.
3. **Business flow.** Five explicit steps open and populate the Order, resolve
   the Debtor, resolve each Product/VAT, save and check totals, then create the
   follow-up Invoice. Existing product price fields are not overwritten;
   quantity and transaction discount are applied on the Order line only.
4. **Verification.** The document view is read back after saving. The flow
   requires exactly one matching open Order and one matching Invoice, including
   the expected total and Paid state, before reporting success.

## Reliability and safety policy

Ambiguity is a stop condition. Duplicate exact matches, unavailable controls,
totals mismatches, payment-method mismatch, or a missing document row raise a
manual-review error with diagnostics rather than choosing a likely answer.
The Invoice action is scoped to the saved Order's **Create a follow-up
document** section and has the exact accessible name `Invoice`; the top toolbar
Invoice control is never used. The flow ends after final verification and has
no branch that creates Delivery, Correction or Dunning documents.

## Key trade-offs

UIA is preferred to image clicks because it survives layout, DPI and window
size changes, but Java/SWT may expose weak accessibility metadata. That cost is
handled with ordered, ancestor-scoped role finders and bounded waits. OCR plus
an LLM is more robust than either alone, but its output is treated as untrusted
until schema validation and math reconciliation succeed. The result is a
fail-closed automation: a review is preferable to an incorrect accounting
record.

The detailed rationale and failure-mode analysis are also in
[docs/DESIGN_DOC.md](docs/DESIGN_DOC.md).
