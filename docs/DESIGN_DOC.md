# Fakturama Image-to-Cash Automation — Design Document

## 1. Purpose and Scope

This document specifies the design of an automation that converts a single purchase-order image into a fully saved, verified Order plus a linked Invoice inside Fakturama (Java/SWT desktop app). The automation drives the desktop UI programmatically and must work across Fakturama versions, display DPI settings, and screen resolutions. The governing constraint is therefore:

> **No hardcoded coordinates and no fixed layout assumptions.** Every control is discovered via Microsoft UI Automation (UIA) property signatures — `automation_id`, `control_type`, `name`, `class` — through ordered fallback finder chains. When the UI cannot be grounded unambiguously, the automation stops and flags for manual review rather than guessing.

The runtime flow that this design must support end-to-end is:

1. Extract and normalize source data from the image (two-phase OCR + Vision LLM, validated structured JSON).
2. Open a New Order and keep it open as the persistent state anchor.
3. Resolve the Debtor: search the existing contact registry, treat the Order's Debtor selector as an existence check, create a new Debtor only on an exact-match failure, create a missing payment method if required, and always return to the same still-open Order.
4. Resolve every product line: select-or-create each Product by exact SKU, guarantee the exact VAT code/value exists before product creation, set master gross price from unit net price and VAT (no line discount), compute the line total from quantity, unit net price, and discount, and return to the same still-open Order.
5. Save the Order; create the linked Invoice from the saved Order's "Create a follow-up document" surface (preserving the Order–Invoice relationship), apply extracted payment status/method/date, and verify both records via the Data > Documents list.

## 2. System Architecture

The system is divided into four modules with a single orchestration entry point, plus a cross-cutting safety net.

### 2.1 Extraction module

Owns image intake, the two-phase OCR + Vision LLM pipeline, pydantic-validated structured output, normalization (dates, decimal separators, VAT %, discount %), and a reconciliation check that itemized line totals sum to the stated source total. Produces a typed `OrderSpec` that is the only data contract consumed downstream.

### 2.2 UI automation module

Owns all Fakturama interaction. Provides the element registry (semantic-role → ordered finder chains), window matching by title, wait-for-stable helpers, and a narrow set of typed primitives (select, type, click, open-dialog, confirm-save, handle-confirmation-dialog). It is the only module that touches UIA/pywinauto and is deliberately stateless regarding business semantics.

### 2.3 Flow / orchestration module

Owns the business steps above (debtor resolution, product resolution, VAT pre-provisioning, pricing math, save, follow-up invoice, payment fields, verification). It calls the UI module via semantic roles, not coordinates, and is the only module that raises `ManualReviewError`. It also maintains the persistent "still-open Order" anchor and returns to it after every sub-operation.

### 2.4 Verification module

Reads back the persisted state (Data > Documents list, and the Open Order form) and compares it field-by-field against the `OrderSpec` and the created Invoice record. Any mismatch is treated as a failure, not a warning.

### 2.5 Safety net

- **Checkpointing:** each of the five flow phases logs a checkpoint; on restart the automation resumes from the last checkpoint rather than replaying the whole flow.
- **Screenshots on failure:** every `ManualReviewError` and unexpected exception captures a full-screen and active-window screenshot plus a UIA element dump into a run artifacts folder.
- **Idempotent save:** save operations re-read the form before writing; re-running a phase against already-written data produces no duplicate records.
- **Controlled timeouts:** all waits bound by explicit timeouts so a hung dialog fails fast into the manual-review path.

## 3. Control-Discovery and Grounding Strategy

### 3.1 Backend selection: UIA over win32

Fakturama is a Java/SWT application; its widgets are exposed to Windows through the Java Access Bridge, which surfaces rich UIA properties. The automation therefore selects the **UIA backend** for discovery and interaction, with **win32** as a fallback for a small set of operations UIA handles poorly (raw keystroke delivery to dialogs whose edit fields lack UIA edit patterns). The UIA backend is preferred because it exposes `automation_id`-style stable identifiers and control types (`Edit`, `ComboBox`, `Button`, `DataItem`) rather than positional geometry, which is the basis for the no-coordinates constraint. Backend selection is a startup decision, made per window and cached, not per call.

### 3.2 Logical element registry

The core discovery primitive is a registry mapping **semantic roles** to **ordered candidate finder chains**. A semantic role is a stable label meaningful to the flow, for example `NEW_ORDER_BUTTON`, `DEBTOR_SELECTOR`, `VAT_COMBO`, `PRODUCT_SKU_FIELD`, `SAVE_BUTTON`, `FOLLOW_UP_AREA`, `PAYMENT_METHOD_COMBO`. Each role resolves to a list of candidate finders, tried in order:

1. **Keyed signature:** exact `automation_id` (and/or `control_type` + `name`) match. Highest confidence, tried first.
2. **Typed signature:** `control_type` + `name` pattern (e.g. an `Edit` whose label element matches "SKU"), tolerant of automation_id drift across versions.
3. **Ancestry-scoped signature:** the control found within a known ancestor role's subtree (e.g. the VAT combo inside the Product tab's form container), which disambiguates visually similar controls without using coordinates.
4. **Name-only candidate set:** controls sharing a name or class, resolved by the disambiguation rules below. Lowest confidence; last resort before manual review.

Every finder result is captured as a **control signature** (role, resolved properties, ancestor chain) and cached so a role is grounded once and reused; if a later lookup disagrees with the cached signature, the automation flags for review instead of silently proceeding.

### 3.3 Distinguishing visually similar controls

Several Fakturama screens contain near-identical controls (e.g. the upper contact-selection icon in the address dialog versus the lower green "+" control that creates a new contact). These are never told apart by position. Instead, the automation builds a **property signature** per candidate: `control_type`, `name`/tooltip text, `automation_id`, and the property set of its ancestor subtree (the enclosing tab, form container, or grid). The disambiguator selects the candidate whose signature matches the role's expected profile (for example, the "new contact" role expects a small green image control living inside the contacts grid's command strip, while the "select address" role expects a toolbar button whose tooltip contains "Select"). When two candidates match equally well, the automation raises `ManualReviewError` rather than picking arbitrarily.

### 3.4 Dialog matching by title

Dialogs are matched by **title**, with a normalized matching rule (case-insensitive substring, trimmed, ignoring trailing window-count suffixes such as " - Fakturama"). An active dialog is pinned before interaction; the automation verifies it received focus and matches the expected title before reading or writing anything. The "Select the address" dialog, the Data > terms of payment dialog, Data > VATs, and the New Order window are each matched this way.

### 3.5 Wait-for-stable-list technique

Data grids (contacts, products, documents) are read with a **stability poll**: the automation samples the visible item count (and, where available, a content signature) repeatedly until the count is unchanged across consecutive samples, indicating the list has finished repainting. Combined with a `wait_idle` on the backend, this avoids acting on half-populated grids. List membership is determined by cell values bound to known column roles, never by screen position.

## 4. Image-Extraction Strategy

### 4.1 Two-phase pipeline

The image is processed in two sequential passes:

1. **OCR pass:** raw text extraction (with confidence scores) to capture fields the vision model might miss or garbled numbers.
2. **Vision-LLM pass:** the model receives the image (and, optionally, the OCR transcript) and is prompted to return a **strict, schema-constrained JSON** object conforming to a pydantic model. The model output is validated; on schema failure the LLM is re-prompted once, then the failure escalates to manual review.

### 4.2 Normalization rules

- **Dates:** single canonical ISO form; ambiguous day/month ordering flagged for review rather than guessed.
- **Decimal numbers:** comma or dot decimal separators normalized to a canonical numeric type; thousand separators stripped explicitly.
- **VAT %:** a percentage field in the order maps to an exact VAT code (e.g. "VAT 19%" → VAT code `S`, value 19) via a fixed mapping table.
- **Discount %:** clamped to a sane range; out-of-range values raise manual review.

### 4.3 Reconciliation

The extraction module verifies that `sum(line_total)` equals the extracted order total within a defined tolerance. A mismatch invalidates the extraction and routes to manual review, because downstream pricing math is derived from line data and must not be applied on un-reconciled input.

## 5. Tradeoffs and Rationale

| Decision | Chosen | Tradeoffs |
|---|---|---|
| UIA vs image recognition | UIA property-based discovery | Robust to layout/DPI changes, no pixel heuristics; cost is that some SWT widgets expose weak properties, hence the fallback chains. Image recognition is the fallback of last resort only, because it reintroduces the coordinate fragility the design bans. |
| pywinauto vs WinAppDriver vs FlaUI | pywinauto (UIA + win32) | Pure Python, fits the automation's language, both backends in one library, well-supported wait helpers. WinAppDriver adds a server dependency and JSON-RPC overhead; FlaUI is .NET-bound and would split the codebase across runtimes. |
| OCR-only vs LLM extraction | Two-phase OCR + Vision LLM | LLM alone can hallucinate structured fields; OCR alone cannot infer semantics (which number is the total, which is the VAT). The combination with pydantic validation and reconciliation is the strongest correctness signal. Cost: one extra API call and model-output validation. |
| State machine navigation vs sequential script | Orchestration module with a persistent state anchor | The still-open Order is the single source of truth between phases; the orchestrator returns to it after every sub-operation rather than re-deriving it. This is more robust than a flat script (which drifts when a dialog steals focus) and less rigid than a full state machine (which is over-engineering for five phases). |
| Cost / robustness / maintainability | Fallback chains + manual-review boundary | Slightly more code than a "best single finder" approach, and occasional human intervention, in exchange for surviving version and locale drift. The semantic-role registry keeps the finders data-driven, so new versions typically require a registry update, not code changes. |

### 5.1 Failure modes

The principal failure modes are: (a) a control the registry cannot ground — handled by the manual-review boundary; (b) a dialog that never stabilizes — handled by bounded waits into the review path; (c) an unexpected exception mid-phase — handled by the safety net (checkpoint + screenshots + element dump); and (d) ambiguous data extraction — handled by reconciliation and review. Every failure path converges on a recorded `ManualReviewError` with artifacts, so no state is ever guessed into.

## 6. Verification

Verification is not an afterthought; it is the closing phase of the flow. After the Invoice is created, the module reads the Data > Documents list and the Open Order form and asserts: the Order exists with its debtor, lines, prices, and VAT; the Invoice exists, is linked to the Order, and carries the extracted payment status, method, and date. Only a fully verified pair is reported as success; anything less routes to the manual-review fallback with artifacts.

## 7. Written answer — "If you had 3 more hours, what would you do?"

With three more hours I would spend them on the two weakest links in this
deliverable: **grounding the registry against the live application** and
**raising the automated confidence in the flow itself**.

1. **Live element-dump tuning (highest value).** The registry's default
   `automation_id`/`name` guesses are documented assumptions. Against a real
   Fakturama I would run `--dump-tree`, diff the UIA tree against every registry
   role, and correct the finder chains so that the highest-signal strategy for
   each control is the one that actually matches. This converts the top three
   real-instance risks (finder semantics, selector disambiguation, unvalidated
   labels) into verified data instead of plausible defaults.
2. **Flow-level unit tests with fake windows.** Today 79 tests cover formulas,
   models, normalizer, config, and the finder, but not `FlowContext`, the five
   step modules, `Verifier`, or the CLI. I would drive the full step 2–5
   decision tree (exact match → create → conflict → payment provision → deposit
   → paid) against scripted fake windows, proving the ambiguity policy and the
   order-first anchor in code rather than by inspection.
3. **Checkpoint/resume for partial flows.** A crash mid-step currently re-raises
   with artifacts but restarts from zero on rerun. I would persist the step
   index + window anchors so a resumed run continues from the last saved state —
   important because the "5 hours" timebox strongly implies interrupted runs.
4. **Idempotent re-run protection.** Re-running the same image should detect an
   already-saved Order (by number/ref/total) and skip re-creation rather than
   duplicate master data.
5. **Real-OCR sweep.** With Tesseract/EasyOCR wired, I would run the German
   sample orders through both engines, tune `deu+eng` configs, and measure
   extraction accuracy against the reconciliation gate.
6. **Column-role binding for the items table.** Replace the positional
   child-order writes in `_edit_line_cells` with header-driven column mapping so
   line editing survives column reordering.
7. **Packaging + CI.** A one-click launcher, and a GitHub Actions job that runs
   the deterministic suite on every push so regressions surface immediately.

Ordered by expected value-per-minute: 1 → 2 → 3, with 4–7 as stretch items.