# Evaluation batch — CSV export

The static evaluation batch as flat files, one per table. Open them in Excel,
diff them in git, or load them into any other system.

Regenerate with `make evaluation-csv` (or `python -m fixtures.export_csv`). They
are dumped from the database *after* the batch is loaded, so they are the same
rows the engine actually reconciles — not a second rendering of the JSON that
could drift from it.

## Start here

**`scenarios.csv`** — the manifest. One row per settlement: what the scenario is
for, what was done to it, and what the engine is expected to conclude
(`expected_d1_paise` … `expected_worst_tier`, `expected_exception_types`).
Everything else is the data those expectations are about.

## The files

| file | rows | what it holds |
|---|---:|---|
| `scenarios.csv` | 22 | the manifest: scenario, family, expected outcome, note |
| `customers.csv` | 26 | named customers; the busiest has four payments |
| `sellers.csv` | 6 | all three commission tiers, one SUSPENDED |
| `orders.csv` | 46 | fewer than payments, because one order was retried three times |
| `payments.csv` | 48 | 35 settled, 13 captured and still in the pipeline; two FAILED attempts that must never settle |
| `refunds.csv` | 3 | one deducted correctly, one never deducted, one belonging to the next period |
| `seller_allocations.csv` | 36 | 25 settled across 16 settlements, 11 still PENDING |
| `transfers.csv` | 25 | what actually moved, including one REVERSED |
| `adjustments.csv` | 1 | a chargeback that hit the header but was never itemised |
| `settlements.csv` | 22 | the headers — one per scenario |
| `settlement_items.csv` | 35 | the lines those headers roll up from |
| `bank_transactions.csv` | 21 | 22 settlements, 21 credits: one is missing on purpose |
| `ledger_entries.csv` | 226 | double-entry postings, including one duplicated and one absent |
| `money_edges.csv` | 582 | the lineage graph behind Trace Money |
| `tax_invoices.csv` | 21 | the synthetic GSTR-2B feed: one gateway tax invoice per settlement, minus the one never filed |
| `ground_truth_anomalies.csv` | 19 | what was planted, and what should be found |

**395 financial records** across payments, refunds, allocations, transfers,
adjustments, settlement items, bank lines and ledger postings. `tax_invoices.csv`
is a separate, third-party source and is deliberately not counted among them.

## The tax feed

`tax_invoices.csv` is the merchant's **GSTR-2B** — the statement the GST portal
auto-drafts each month from what SUPPLIERS filed. It is the third answer to "how
much input tax credit is claimable", independent of both the settlement report
and the merchant's own books, and it is the only one that decides whether the
money is actually recoverable.

Five findings are planted in it, one per real failure mode: `EV_07` never filed,
`EV_11` off by 3 paise (the supplier rounds per invoice, the settlement per
line), `EV_15` filed under IGST when both parties are in the same state, `EV_20`
filed a period late, and `EV_03` matching perfectly but marked ineligible by the
portal. `EV_20` is the trap — credit *deferred* by a month, not credit *lost*.

Those five are **not** in `ground_truth_anomalies.csv`, on purpose: that file
feeds the engine's four-delta honesty metrics, and adding to it would move
numbers the whole evaluation rests on. The tax expectations live in
`fixtures/authoring.py::TAX_EXPECTATIONS` and are scored by `tests/test_taxmatch.py`.

The feed is **synthetic**, authored for this project. It is not real filing data
and nothing in it is tax advice; the GSTINs contain "DEMO" so they cannot be
mistaken for real registrations.

## The pipeline tail

The last 13 payments are **captured but not yet settled** — four trading days
(2026-03-16 → 03-19) after the final settlement period closed, with 11 allocations
still `PENDING`. They exist so the cash forecaster has a forward position to
report, and they carry capture-only ledger postings (DR `RAZORPAY_CLEARING` /
CR `SALES`) so they cannot manufacture a false Δ₃.

They are **purely additive**: no row that was already settled was modified when
they were added, and the 22 scenarios score exactly as they did before. Nothing
in `scenarios.csv` refers to them — they are not a scenario, they are the trading
that had not yet been settled when the book closed.

## Two things to know before reading a number

**Every `*_paise` column is integer paise, not rupees.** `976400` is Rs 9,764.00.
No column is ever a float — that is deliberate, and it is why the arithmetic is
exact. Divide by 100 for display only.

**`dataset_id` has been dropped from every file.** It is the same constant on all
1,139 rows and carries no information here; the batch is one dataset.

## Regenerating

These files are derived. The sources of truth are `fixtures/evaluation_batch.json`
(the authored scenarios and their expected outcomes) and `fixtures/loader.py`
(how those become rows). Edit those, then re-export — an edit made directly to a
CSV is lost the next time anyone runs the export.
