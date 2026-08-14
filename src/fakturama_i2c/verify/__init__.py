"""Verification layer: read-back checks against Fakturama's persisted state.

After the flow saves the Order and creates the linked Invoice, verification
re-opens Fakturama's ``Data > Documents`` list and the Invoice editor and
compares the persisted values field-by-field against the extracted order. Any
missing, ambiguous, or conflicting value raises ``ManualReviewError`` --
verification never guesses.
"""