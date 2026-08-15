# If I had three more hours

I would use the time to finish live UIA grounding and recovery hardening.

First, I would capture and review a UIA tree for every Fakturama editor and
selector, then replace any provisional registry signatures with the exact
properties exposed by this installed version. This is the highest-value work:
it turns a plausible desktop automation into a version-verified one without
using coordinates.

Second, I would add flow-level tests using scripted fake UIA controls for the
exact-match, missing-master-data, duplicate-match, and paid-invoice branches.
Those tests would assert the important safety behaviour: retain an existing
product's price, stop on ambiguity, create the Invoice only from the Order's
follow-up action, and never create Delivery, Correction, or Dunning documents.

Third, I would add resumable checkpoints and a narrowly scoped transaction
recovery strategy. A restart should recognize a previously saved Order by its
reference and total, continue from the safe next phase, and never duplicate
master data. Finally, I would run a small synthetic image corpus through OCR
and reconciliation to measure extraction confidence before UI automation starts.
