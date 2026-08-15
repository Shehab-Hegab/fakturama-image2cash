# E2E Verification Report

## Source-data verification

The supplied `assets/sample_order/order.png` was extracted using the offline
sidecars and validated against `order.json`.

| Check | Expected | Result |
| --- | ---: | ---: |
| Reference | ORD-2026-0142 | PASS |
| Order date | 2026-03-18 | PASS |
| Line 1 | 2 x 59.90 = 119.80 | PASS |
| Line 2 | 3 x 9.90 less 10% = 26.73 | PASS |
| Net | 119.80 + 26.73 = 146.53 EUR | PASS |
| VAT | 19% of 146.53 = 27.84 EUR | PASS |
| Gross | 146.53 + 27.84 = 174.37 EUR | PASS |
| Payment intent | Bank Transfer, Paid, 2026-03-18 | PASS |

`pytest -q` completed successfully: **79 passed**. The extraction dry-run also
completed successfully with the expected two items and all reconciliation
checks marked OK.

## Live desktop E2E run

A live run was executed against the running Fakturama process on 2026-08-15.

### Step-by-step results

| Step | Description | Result |
| --- | --- | --- |
| 1 | Open New Order, set date/ref/price-mode/VAT-mode | PASS |
| 2 | Resolve Debtor (Acme GmbH keyboard fallback) | PASS |
| 3 | Resolve Products (SKU-1001 qty=2 0%, SKU-1002 qty=3 10%) | PASS |
| 4 | Confirm totals (Net=146.53, VAT=27.84, Gross=174.37), save, open linked Invoice | PASS |
| 5 | Apply payment (Credit transfer, Paid, date=18.03.2026, value=174.37), save + verify | PASS |

### Fakturama state after run

- **Order PO000034** saved with Cust.Ref ORD-2026-0142
- **Invoice INV000010** created as follow-up from PO000034, marked Paid
  - Line items: SKU-1001 (qty=2, $59.90, 0%, $119.80), SKU-1002 (qty=3, $9.90, -10%, $26.73)
  - Total Net: $146.53, VAT: $27.84, Total: $174.37
  - Payment: Paid checkbox checked, Credit transfer, value $174.37
- **Documents panel** shows INV000010 at $174.37, paid state, linked to ORD-2026-0142

### Evidence screenshots

| File | Content |
| --- | --- |
| `assets/screenshots/01_source_order.png` | Source order image (copied at flow start) |
| `assets/screenshots/02_saved_order.png` | Order after save with line items + totals |
| `assets/screenshots/03_linked_paid_invoice.png` | Invoice with payment fields applied |
| `assets/screenshots/04_documents_final_state.png` | Documents list confirming Order + Invoice pair |

## Safety checks enforced in the implementation

- No coordinate-based mouse actions remain in `src/fakturama_i2c/flow`.
- Every workflow step is fatal on an unresolved UIA control or a mismatch.
- Totals must exactly match 146.53 EUR / 27.84 EUR / 174.37 EUR before
  follow-up invoice creation.
- Payment method is exact-match only; no default or fuzzy payment selection.
- Invoice creation uses only the Order follow-up `Invoice` role.
- Document verification requires exactly one open Order and one matching paid
  Invoice; Delivery, Correction, and Dunning creation are absent.
- Ambiguity is a stop condition: duplicate matches, unavailable controls, or
  conflicting signals raise `ManualReviewError` instead of guessing.
