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
| `orders.csv` | 33 | fewer than payments, because one order was retried three times |
| `payments.csv` | 35 | includes two FAILED attempts that must never settle |
| `refunds.csv` | 3 | one deducted correctly, one never deducted, one belonging to the next period |
| `seller_allocations.csv` | 25 | what each seller was owed, across 16 settlements |
| `transfers.csv` | 25 | what actually moved, including one REVERSED |
| `adjustments.csv` | 1 | a chargeback that hit the header but was never itemised |
| `settlements.csv` | 22 | the headers — one per scenario |
| `settlement_items.csv` | 35 | the lines those headers roll up from |
| `bank_transactions.csv` | 21 | 22 settlements, 21 credits: one is missing on purpose |
| `ledger_entries.csv` | 200 | double-entry postings, including one duplicated and one absent |
| `money_edges.csv` | 519 | the lineage graph behind Trace Money |
| `ground_truth_anomalies.csv` | 19 | what was planted, and what should be found |

**345 financial records** across payments, refunds, allocations, transfers,
adjustments, settlement items, bank lines and ledger postings.

## Two things to know before reading a number

**Every `*_paise` column is integer paise, not rupees.** `976400` is Rs 9,764.00.
No column is ever a float — that is deliberate, and it is why the arithmetic is
exact. Divide by 100 for display only.

**`dataset_id` has been dropped from every file.** It is the same constant on all
992 rows and carries no information here; the batch is one dataset.

## Regenerating

These files are derived. The sources of truth are `fixtures/evaluation_batch.json`
(the authored scenarios and their expected outcomes) and `fixtures/loader.py`
(how those become rows). Edit those, then re-export — an edit made directly to a
CSV is lost the next time anyone runs the export.
