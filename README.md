# Fakturama Image-to-Cash

Automate the [Fakturama](https://www.fakturama.org/) desktop application
(Java/SWT): feed it **one order image**, get back a **verified Order** plus a
**linked Invoice**, fully saved inside Fakturama. No hardcoded coordinates and
no fixed layout assumptions — every control is discovered via Windows UI
Automation (UIA) property signatures, and whenever the UI cannot be grounded
unambiguously the automation **stops and flags for manual review** rather than
guessing.

---

## What it does

```
one order image
      │
      ▼
  OCR + vision-LLM extraction   → typed, validated ExtractedOrder (pydantic)
      │                            + totals reconciliation (warn, never fix)
      ▼
  UIA flow inside Fakturama      → open Order (kept open) → resolve-or-create
      │                            Debtor → resolve-or-create Products with the
      │                            exact VAT code/value → set master gross price
      │                            → fill lines → save
      ▼
  Follow-up Invoice              → created from the saved Order's "create a
      │                            follow-up document" surface (relationship
      │                            preserved) → payment status/method/date applied
      ▼
  Verification                   → Data > Documents list + Open Order form are
                                   read back and compared field-by-field
```

The full pipeline is specified in [docs/DESIGN_DOC.md](docs/DESIGN_DOC.md) —
architecture, control-discovery strategy, normalization rules, tradeoffs, and
failure modes. Written answers to the assignment questions are recorded there
as well.

## Architecture

| Module | Responsibility |
|---|---|
| `fakturama_i2c.extraction` | Image intake: two-phase OCR + vision-LLM, normalization, pydantic-validated structured output, totals reconciliation. |
| `fakturama_i2c.ui` | The only module that touches UIA/pywinauto: semantic-role element registry, ordered fallback finders, wait-for-stable helpers, typed element wrappers. Deliberately stateless about business rules. |
| `fakturama_i2c.flow` | Business orchestration: debtor resolution, product resolution, VAT pre-provisioning, pricing math, save, follow-up invoice, payment fields. The only place that raises `ManualReviewError`. |
| `fakturama_i2c.verify` | Read-back verification of the persisted Order and Invoice against the extracted spec. |
| `fakturama_i2c.models` / `pricing` | The typed data contract and the spec pricing formulas shared by every layer. |

The semantic-role registry (`ui/registry.py`) is the heart of the
"no coordinates" rule: each role (e.g. `DEBTOR_SELECTOR`, `SAVE_BUTTON`)
maps to an **ordered chain of property-based finders** tried in sequence —
keyed `automation_id`, typed `control_type`+`name`, ancestor-scoped, and
name-only as the last resort. When two candidates match equally well, the
finder raises `ManualReviewError` instead of picking arbitrarily.

## The four key design insights

1. **The follow-up document preserves the Order relationship.** The Invoice is
   created from the saved Order's *"Create a follow-up document"* surface, so
   Fakturama itself records the Order→Invoice link — the automation never has to
   invent it, and verification checks the link directly in the documents list.
2. **The ambiguity policy is STOP, not guess.** `ManualReviewError` is raised
   for any exact-match failure, duplicate, or conflicting signal (combo values,
   extra search hits, control ties). Every failure converges on a recorded
   review with screenshots and an element dump.
3. **Order-first state management.** The New Order is opened once and stays
   open as the persistent anchor; every sub-operation (debtor, products, VAT,
   save) returns to the same still-open Order instead of re-deriving it.
4. **Master price = Net × (1 + VAT%), no line discount.** The Product master
   gross price is computed from the unit net price and VAT only. The *line*
   total is computed separately as `qty × net × (1 − discount%)`, so a line
   discount never contaminates the master record.

## Setup

Requires Python 3.10+.

```powershell
# 1. Virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1

# 2. Dependencies
pip install -r requirements.txt

# 3. Fakturama (the desktop app)
#    Install Fakturama for Windows and note the path to Fakturama.exe.
#    https://www.fakturama.org/

# 4. Configuration
Copy-Item .env.example .env   # then edit .env
```

The `.env` file drives everything via `I2C_*` variables. Key ones:

| Variable | Meaning | Default |
|---|---|---|
| `I2C_FAKTURAMA_EXE` | Path to `Fakturama.exe`; empty ⇒ attach to a running instance | *(empty)* |
| `I2C_OCR_ENGINE` | `mock` (sidecar `.txt`), `tesseract`, `easyocr` | `mock` |
| `I2C_LLM_PROVIDER` | `mock` (sidecar `.json`), `openai`/`groq`/`together`/`openrouter`/`ollama`/`vllm` (OpenAI-compatible), `anthropic` (native) | `mock` |
| `I2C_LLM_API_KEY` | API key for the chosen LLM provider (`ollama`/`vllm` need none) | *(empty)* |
| `I2C_LLM_MODEL` / `I2C_LLM_BASE_URL` | Model name / OpenAI-compatible base URL (used by `openai`/`groq`/`together`/`openrouter`/`ollama`/`vllm`) | *(empty)* |

With the default `mock` engines the whole pipeline runs fully offline: OCR text
is read from `<image>.txt` and the structured result from `<image>.json` next to
the image.

## Running

```powershell
# Extract + preview only (never touches the UI)
python -m fakturama_i2c.cli --image path/to/order.png --dry-run

# Full run: drive the UI, then verify the saved Order + Invoice in Fakturama
python -m fakturama_i2c.cli --image path/to/order.png --launch

# Skip post-flow verification
python -m fakturama_i2c.cli --image path/to/order.png --launch --no-verify

# Print the UIA element tree of the main window (control-discovery audit trail)
python -m fakturama_i2c.cli --dump-tree

# Verbose (DEBUG) logging
python -m fakturama_i2c.cli --image path/to/order.png --verbose
```

`--launch` starts Fakturama from `I2C_FAKTURAMA_EXE` if it is not already
running; without it the runner attaches to a running instance by window title.
When `--image` is omitted, a `sample-order.png` under `assets/` (or the repo
root / CWD) is used if present.

The installed console script is equivalent:

```powershell
fakturama-i2c --image path/to/order.png --dry-run
```

A ready-made demo order and an **annotated screenshot** mapping every image
region to its flow step ship in the repo:

* `assets/sample_order/order.png` — demo order image (mock OCR/LLM sidecars included)
* `assets/annotated/order_annotated.png` — annotated screenshot (STEP 2 debtor / STEP 3 items / STEP 4 totals / STEP 5 payment)

Regenerate them at any time with `python scripts/make_demo_assets.py`.

## Running the tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
```

Tests are fully deterministic: no time-based flakes and no real UI automation.
The OCR/LLM steps run against mock sidecar engines, and the UI registry tests
use tiny fake pywinauto stand-ins that stub exactly the duck-typed surface the
finder uses.

## Limitations and what is skipped

- **No live Fakturama interaction in the test-suite.** The UI automation layer
  (`ui/`) and the flow/verification modules are fully implemented, but the tests
  run against fakes and mock sidecar engines. A real end-to-end run requires
  Fakturama installed and its live control dump to confirm the registry's
  default `automation_id`/name guesses (documented in `ui/registry.py`).
- **Ambiguity is surfaced, not resolved.** Reconciliation mismatches, unknown
  payment codes, and control ties produce warnings / `ManualReviewError`; a
  human must review the source image.
- **Mock engines are the offline default.** Tesseract/easyocr and the OpenAI-
  compatible provider need their binaries/keys configured.
- **Items-table cell writes are positional.** `_edit_line_cells` writes line
  cells by their position in the row (item number, name, qty, price, VAT,
  discount) rather than by column header. This survives the documented layout
  for bring-up, but if Fakturama reorders columns the mapping must be updated to
  header-driven column-role binding.
- **Verification binds the first "Invoice" window.** If an unrelated Invoice
  editor is already open, `verify_invoice_payment` may read the wrong window;
  the flow expects only the follow-up Invoice to be open.