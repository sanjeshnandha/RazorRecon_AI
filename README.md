# AI Finance Controller — P0

Deterministic settlement reconciliation for a marketplace. Four independent
deltas over ~28,000 financial records, a measured accuracy number with a
ground-truth table behind it, and an exception list that says in rupees exactly
what it could not explain.

**Zero model calls.** The fee, tax and refund arithmetic is computed in plain
code from a versioned policy registry. The system's job is *verification*, not
generation — so there is nothing here for a model to be confidently wrong about.

> ### Demo Merchant Policy — not Razorpay's actual terms
>
> Every numeric rule in `policy/policy.yaml` (MDR rates, the T+2 cycle,
> GST-on-fee treatment, refund window, commission rates, bank-credit lag) is a
> **synthetic demo-merchant policy we authored**. It is not a claim about how
> Razorpay actually operates. Real MDR, settlement timing and fee treatment vary
> by merchant category, risk grade and commercial agreement, and are not public
> in the form this project encodes. Every screen that shows a computed fee, tax
> or settlement-cycle figure carries this label.

---

> **Change history and full context:** [`CHANGELOG.md`](CHANGELOG.md) — what was
> built, why, and what is deliberately deferred. Updated with every change.

## Run it

```bash
pip install -r requirements.txt
make demo          # postgres + schema + seeded dataset + one reconciliation run
make serve         # http://localhost:8000
```

`./run.sh demo` and `./run.sh serve` do the same thing without `make`.

`make demo` prints the full report to the terminal. `make serve` puts the same
numbers behind a UI. To reproduce the exact dataset in this README:
`make generate SEED=42`.

Individual steps:

| command | what it does |
|---|---|
| `make db-up` | PostgreSQL 16 via docker compose (the only container) |
| `make schema` | loads `db/schema.sql` + `db/indexes.sql` — destructive |
| `make generate` | seeded dataset, 100 settlements, anomalies planted |
| `make generate-clean` | same generator, **no** anomalies — the phase-4 gate |
| `make reconcile` | runs the engine, prints the report |
| `make tick` | appends the next settlement cycle, then re-reconciles |
| `make evaluation-batch` | loads the fixed, hand-authored evaluation batch |
| `make test` | 193 tests, including the 19 golden scenarios |
| `make serve` | FastAPI + the SPA, one process |

Every target also exists as `./run.sh <target>`. If `Makefile` did not survive
the copy into this folder, `mv Makefile.txt Makefile` restores it — and the CI
workflow lives at `ci/github-workflow-ci.yml`, to be copied to
`.github/workflows/ci.yml`.

---

## What it produces

From `seed=42`, 100 settlements, 28,397 financial records:

```
THROUGHPUT
  100 settlements backed by 28,397 financial records across payments, refunds,
  seller allocations, transfers, bank credits and accounting ledger entries — of
  which 13,812 are individual ledger postings, not standalone transactions
  batch completed in 0.77s (36,952 records/s); p50 0.237ms, p95 0.423ms per settlement

MEASURED ACCURACY — reported separately, never blended
  settlement match rate (all four deltas)     92.00%   92/100
  monetary reconciliation rate                96.19%   of Rs 5,98,65,539.77 settled value
  seller payout reconciliation (D4)           99.94%
    D1 compute  97.00%   D2 bank  96.00%   D3 ledger 100.00%   D4 payout  99.94%

AMOUNT AT RISK, RIGHT NOW
  Rs 22,86,062.18   across 12 open exceptions

HONESTY CHECKS (scored against ground truth planted at generation time)
  resolvable anomalies detected       50/50   100.00%
  diagnosis accuracy                  50/50   100.00%
  undiagnosable cases escalated         7/7   100.00%
  false-positive traps avoided          5/5
  FALSE AUTO-RESOLUTIONS                  0        (must be 0)
    guard [held] SET_0089: UTR_SUFFIX uniqueness  -> resolved by EXACT_AMOUNT_DATE
    guard [held] SET_0039: amount+date ambiguity  -> resolved by nothing (tier C)
    guard [held] SET_0015: amount+date ambiguity  -> resolved by nothing (tier C)
```

The budget is 10 seconds. Wall time varies with machine load; it has not
exceeded 0.8s on any run here.

Those last five lines are the ones worth arguing about. Everything above them
can be gamed by an engine that flags nothing, or by one that flags everything.

---

## Why the accuracy number means something

Three metrics have to be true at once, and each one closes a loophole the
other two leave open:

- **False auto-resolution count = 0.** No tier-A verdict contradicts what we
  planted. An engine that guesses well is still guessing.
- **Resolvable-anomaly detection rate = 100%.** Every planted, diagnosable
  defect was actually flagged. A "flag nothing" engine scores a perfect zero
  false auto-resolutions *and* 0% here — which is the point. Being conservative
  is only virtuous when paired with actually finding the problems that exist.
- **Correct-escalation rate = 7/7.** We planted seven discrepancies that are
  genuinely undiagnosable. The system refused to diagnose all seven, and
  reported each one's exact rupee figure as unexplained.

Plus two guards that are easy to fail silently:

- **False-positive traps (5/5).** Evidence sitting nearby that must *not* be
  used — a refund dated after `period_end`, colliding UTR suffixes. Reaching for
  it produces a plausible-looking explanation that is fabricated.
- **Matcher guards.** `UTR_SUFFIX` refused to select when the suffix was shared;
  `EXACT_AMOUNT_DATE` refused when a sibling settlement had the same net on the
  same date. Both are recorded in `match_candidates` with `is_ambiguous=true`,
  so the refusal is auditable rather than invisible.

---

## The four deltas

Δ₁–Δ₃ answer questions about the platform's own money. Δ₄ answers a different
question entirely, which is why it is reported on its own axis.

**Δ₁ COMPUTE** — *did the gateway settle this the way policy says it should?*

```
expected_net = gross − in-period refunds − policy fee − policy GST + itemised adjustments
Δ1           = expected_net − settlements.net_settlement_amount_paise
```

The fee is computed from `POLICY.MDR.*`, never read off `settlement_items.fee_paise`.
Reading their number and comparing it to their number proves nothing.

Refunds are **source-derived**; adjustments are **item-derived**. That asymmetry
is deliberate. A refund's settlement is derivable from policy — a `PROCESSED`
refund dated inside `[period_start, period_end]` *must* be deducted from that
settlement, so its absence from `settlement_items` is itself the defect. An
adjustment has no policy-derivable settlement assignment: `adjustments.settlement_id`
is a *claim*, and the claim is what we are auditing, so it cannot also be an
input to the expectation. An adjustment present in the source but missing from
items becomes discoverable evidence instead.

**Δ₂ BANK** — *did the money actually arrive?* Seven matcher passes, first one
yielding exactly one candidate wins. See below.

**Δ₃ LEDGER** — *do the merchant's books agree?* A fully settled payment leaves
`RAZORPAY_CLEARING` at exactly zero, so any non-zero clearing balance is a
duplicate, missing or misdirected posting. Where clearing *is* zero we still
check account integrity: a fee posted to `SALES` instead of `GATEWAY_FEES`
leaves a balanced group and a zero clearing balance while overstating revenue
and understating input GST.

**Δ₄ SELLER PAYOUT** — *was each seller actually paid what they were owed?*

```
Δ4[allocation] = allocation.net_seller_paise − Σ PROCESSED transfers for (payment, seller)
```

Δ₁–Δ₃ can all reconcile perfectly for a settlement while a specific seller is
still short, because none of those three look inside the merchant's obligation
to its sellers. For a marketplace that is arguably the more consequential
failure: the platform's own books close cleanly while a seller is quietly
underpaid. Hand-worked fixture `M09` is exactly that case — all three platform
deltas zero, seller `M09_S2` short ₹3,520.

---

## The matcher (Δ₂)

Passes run in order. The first pass yielding **exactly one** candidate wins.
Two or more equally-scoring candidates does not "partially succeed" — the pass
fails and control moves on with both candidates still in play. If nothing later
disambiguates them, tier C. **Ambiguity always beats confidence**, and there is
no numeric confidence threshold anywhere in P0.

| # | pass | rule | tier |
|---|---|---|---|
| 0 | `EXACT_UTR` | bank line carries the settlement UTR verbatim | A |
| 1 | `UTR_IN_NARRATION` | full UTR appears in the normalised description | A |
| 2 | `UTR_SUFFIX` | last 8 chars appear in the description **and** that suffix is unique across every other settlement UTR in the date window | A |
| 3 | `EXACT_AMOUNT_DATE` | exact net, inside bank tolerance, **and** no competing settlement with the same figure in the window | A |
| 4 | `SUBSET_SUM` | merged and split credits, bounded enumeration | A |
| 5 | `AMOUNT_WIDE_WINDOW` | exact net inside the wide window, no UTR evidence | **B** |
| 6 | `FUZZY_REFERENCE` | similarity ≥ threshold **and** exact amount **and** date proximity **and** exactly one candidate clears | **B** |
| — | none | no candidate, or ≥2 tied | **C** |

Passes 2, 3 and 6 each carry a guard that a naive implementation omits:

- an 8-character suffix is **not** globally unique, so uniqueness is checked
  against the whole window before the pass may select;
- two settlements can legitimately share a net amount on a date, so pass 3
  explicitly looks for a sibling and refuses if one exists;
- similarity alone is not identity, so pass 6 also requires exact amount, date
  proximity, and a single candidate over the threshold — and never promotes
  above tier B however high the score.

**Subset-sum is bounded and exhaustive**, not heuristic: worst case C(10,4) = 210
subsets, every one enumerated, and *exactly one* must hit the target. Two
subsets that both sum correctly is ambiguity, not a tiebreak. This converts a
case most teams hand to a model into a provable match.

Every candidate examined at every pass — selected or not, ambiguous or not — is
written to `match_candidates`. That table is the matcher's audit trail, and it
is the artifact to show anyone who asks "how do I know you're not just getting
lucky on this batch."

---

## Attribution and the tier gate

For every delta with `delta_paise ≠ 0` the engine proposes attributions from a
fixed rule set, then:

```
Σ(attributions.signed_amount_paise) + residual_paise == delta_paise
```

`residual_paise` is **computed**, never asserted — an attribution layer can
never grade its own homework. The assertion runs on every delta in
`engine/runner.py`, and `tests/test_golden.py` re-checks it in SQL across the
whole batch. Every attribution carries an `evidence_record_id` that must exist
in a source table; orphans are dropped before persistence and a test proves
none survive.

| tier | condition | status |
|---|---|---|
| **A** | residual = 0, all attributions `DETERMINISTIC`, no ambiguity flag | `AUTO_RESOLVED` |
| **B** | residual = 0, ≥1 `FUZZY` attribution | `NEEDS_REVIEW` |
| **C** | residual ≠ 0, **or** ≥2 equally-scoring candidates | `UNRESOLVED` |

### The two double-counting guards

These are the easiest bugs to ship in a system like this, so both have a
dedicated golden test.

1. **Refund period gate** (test 14). A refund is deductible from settlement `S`
   only if `refund_date ∈ [S.period_start, S.period_end]`. A refund processed
   *after* `period_end` but *before* `settlement_date` belongs to a later
   settlement, however close it sits in time. Attributing it here is a
   fabricated explanation. Settlement periods tile the calendar with no gaps and
   no overlaps, so every refund lands in exactly one period — `test_calculation.py`
   proves the tiling.

2. **Fee already corrected by an adjustment** (test 15). When a `FEE_CORRECTION`
   adjustment references the same payment, only the *residual* fee error
   (`charged − corrected − policy`) may be attributed to `ATTR.FEE_RATE`, and the
   generic unitemised-adjustment rule skips that adjustment entirely. The same
   rupees are never presented twice under two evidence types. The
   `UNIQUE (run_id, delta_id, evidence_type, evidence_record_id)` constraint
   catches the crude version of this bug; the rule catches the subtle one.

And a third, structural: **refunds is a Σ, not a lookup**. A payment with two
partial refunds in period contributes both. `D1_REFUND_PARTIAL_MULTI` is planted
specifically so an engine that stops after one attribution leaves a residual and
correctly drops to tier C rather than claiming a partial win.

---

## Ground truth

`ground_truth_anomalies` is the most valuable table in the project. Every
planting function writes its row **at plant time**, computed from the values it
just wrote — never reconstructed afterwards by re-deriving from the mutated
data, which would risk the generator and the evaluator sharing the same bug.
Rows carry the before/after mutation fields, so you can show exactly what
*should* have been caught rather than asserting a percentage.

62 anomalies at `seed=42`: 50 resolvable, 7 deliberately undiagnosable, 5
false-positive traps.

<details>
<summary>Full catalogue</summary>

**Δ₁ compute** — `D1_FEE_RATE_DRIFT` ×4 · `D1_TAX_AGGREGATE_ROUNDING` ×3 ·
`D1_REFUND_NOT_DEDUCTED` ×2 · `D1_REFUND_PARTIAL_MULTI` ×1 ·
`D1_REFUND_OUTSIDE_PERIOD` ×1 (trap, both ends) · `D1_HEADER_ROLLUP_MISMATCH` ×2 ·
`D1_ADJUSTMENT_APPLIED` ×3 · `D1_FEE_CORRECTED_BY_ADJUSTMENT` ×1

**Δ₂ bank** — `D2_TIMING_NEXT_DAY` ×5 · `D2_NARRATION_NO_UTR` ×5 (half tier A,
half tier B by construction) · `D2_MERGED_CREDIT` ×3 · `D2_SPLIT_CREDIT` ×2 ·
`D2_SETTLEMENT_ON_HOLD` ×2 · `D2_SUFFIX_COLLISION` ×1 (guard) ·
`D2_SAME_AMOUNT_SAME_DAY` ×1 (guard)

**Δ₃ ledger** — `D3_DUPLICATE_LEDGER` ×3 · `D3_MISSING_LEDGER` ×2 ·
`D3_WRONG_ACCOUNT` ×2

**Δ₄ payout** — `D4_ALLOC_EXCEEDS_PAYMENT` ×2 ·
`D4_ALLOC_TRANSFER_DIVERGENCE` ×3 · `D4_TRANSFER_MISSING` ×2

**Undiagnosable by design (must reach tier C)** —
`UNRESOLVABLE_PHANTOM_DEBIT` ×3 · `UNRESOLVABLE_AMBIGUOUS_CREDIT` ×2 ·
`UNRESOLVABLE_PHANTOM_PAYOUT_GAP` ×2

</details>

The divergence/phantom distinction in Δ₄ is evidentiary, not cosmetic:
`D4_ALLOC_TRANSFER_DIVERGENCE` has a `REVERSED` transfer of exactly the missing
amount, so the gap is fully explainable and lands tier A.
`UNRESOLVABLE_PHANTOM_PAYOUT_GAP` has nothing — no reversed transfer, no
adjustment, no second transfer — so it lands tier C with the seller named and
the rupees stated. Same shape, different evidence, different answer.

---

## Phase 0: the hand-worked fixtures

`tests/golden/manual/*.json` — ten settlements whose every rupee was computed
**by hand from `policy.yaml`** before the generator or the engine existed. Each
figure carries a `derivation` string showing the arithmetic. They are entered
literally, not produced by running any code in this repo.

They exist to catch the failure mode that makes a hackathon demo worthless: the
generator and the reconciliation engine encoding the *same* misunderstanding of
the policy — both computing GST on the gross instead of on the fee, say — so
the demo reports "100% match rate" while both halves are wrong in the same way.
A fixture derived from the policy document with no code involved is independent
of both.

`tests/test_phase0_fixtures.py` loads each fixture into Postgres and runs the
**real** engine against it. If the fixture and the engine disagree, the engine
is wrong.

Two of the ten are worth reading on their own:

- **M05** — a refund dated after `period_end` but before `settlement_date`. The
  expected output is *zero attributions* and a clean tier A. A nearby refund is
  not evidence.
- **M08** — a ₹473.00 shortfall with no linked record anywhere. The expected
  output is *zero attributions*, residual ₹473.00, tier C. Reporting what you
  cannot explain beats inventing a cause.

---

## Verification you can run

```bash
make test                          # 63 tests: 19 golden scenarios + fixtures + properties
make generate-clean && make reconcile   # phase-4 gate: every delta exactly zero
```

The clean-data gate is the second-most-important check in the project. If the
generator and the engine disagree on data with nothing wrong in it, something in
the policy interpretation is inconsistent and no accuracy number downstream can
be trusted. On clean data the engine currently reports **all four deltas at
exactly zero, zero exceptions, 100% tier A**.

Determinism is checked too: the same seed produces a byte-identical dataset, a
different seed produces a different one.

---

## Layout

```
policy/policy.yaml       the registry — read by BOTH the generator and the engine
generator/               generate.py, anomalies.py, calendar.py
engine/                  money.py, policy.py, loader.py, invariants.py,
                         calculation.py, matcher.py, subset_sum.py,
                         attribution.py, exceptions.py, lineage.py,
                         metrics.py, runner.py, db.py
api/                     main.py, schemas.py, static/ (the built SPA)
web/                     index.html + build.sh
db/                      schema.sql, indexes.sql
tests/                   golden/manual/ (phase 0), test_golden.py (19 scenarios),
                         test_phase0_fixtures.py, test_invariants.py,
                         test_calculation.py, test_money.py
scripts/reconcile.py     the terminal report
```

### Non-negotiables encoded in the code

1. **All money is `BIGINT` paise.** Never float, never `Decimal`, never `numeric`.
   Column names end in `_paise` so it is hard to forget, `engine/money.py::bps()`
   raises `TypeError` on a float, and `test_invariants.py` fails if any
   `*_paise` or `*_bps` column is ever changed away from an integer type.
2. **All rates are integer basis points.** 2% MDR is `200`, 18% GST is `1800`.
3. **`bps()` is the only rounding function on the money path.** Python's builtin
   `round()` is banker's rounding and would silently manufacture off-by-one-paise
   drift that then gets diagnosed as a fake anomaly.
4. **No LLM anywhere in P0.** If you find yourself wanting one, the deterministic
   rule is missing. Write the rule.
5. **`settlement_items` is the source of truth.** The `settlements` header is a
   materialised rollup; header ≠ Σ items is an *exception* (INV-B6), not an
   assumption.
6. **Runs are immutable.** Every derived row carries `run_id`; re-running never
   mutates a prior run's output.
7. **Precision over recall.** A false auto-match is worse than an unresolved
   exception. Ambiguity always blocks auto-resolution.
8. **Everything is seeded.** Given a seed, the generator produces byte-identical
   data.

### Structural vs business invariants

Structural failures mean the record cannot be reasoned about at all (a reference
pointing nowhere): that record is excluded, logged, and can never reach tier A —
but the run continues. Business invariants are **expected to fail sometimes**;
several planted anomalies exist precisely to violate one. A violation is
recorded as an exception input and reconciliation continues for that settlement.

Getting this backwards is what causes a demo to silently reject the interesting
half of the dataset, so `test_invariants.py` asserts that all 100 settlements
still get all three settlement-level deltas computed on the dirty batch.

---

## API

Read-only over persisted results, except the two POSTs. No model call on any
page render.

```
GET  /api/health                                    engine + policy version
GET  /api/policy                                    the whole registry, verbatim
GET  /api/datasets            POST /api/datasets    list / generate
GET  /api/runs                POST /api/runs        list / reconcile
POST /api/datasets/{id}/tick                        append one cycle + re-reconcile
GET  /api/datasets/{id}/batches                     the batch log, and what is in flight
GET  /api/runs/{id}/metrics                         the full metrics block
GET  /api/runs/{id}/settlements                     table, with all four delta chips
GET  /api/runs/{id}/settlements/{sid}               waterfall, Δ arithmetic, Explain,
                                                    payments/refunds/payouts/adjustments/
                                                    bank/ledger/matcher-trail/audit
GET  /api/runs/{id}/exceptions?status=&severity=…   the honest exception list
GET  /api/runs/{id}/sellers                         per-seller payout reconciliation
GET  /api/runs/{id}/trace?node_type=&node_id=       Trace Money (recursive CTE)
GET  /api/runs/{id}/audit                           every engine decision
GET  /api/runs/{id}/export.csv | export.json        downloadable report

GET  /api/fixtures/evaluation-batch                 what the static batch contains
POST /api/fixtures/evaluation-batch                 load it and reconcile it

GET  /api/runs/{id}/tables                          live row counts, every table
GET  /api/runs/{id}/tables/{table}                  one page of real rows

GET  /api/agent/status                              is a provider key configured
POST /api/runs/{id}/ask                             ask the investigation agent
GET  /api/runs/{id}/conversation                    the persisted transcript
```

Money crosses the API as integer paise. The only place a rupee string appears is
alongside it, for display.

---

## UI

Four screens plus Trace Money, white and emerald, served by the same FastAPI
process. The SPA is one self-contained HTML file with **no build step, no npm
dependencies and no CDN calls**, so the demo runs offline next to the API —
`web/build.sh` is a copy. If you later want a bundler, replace that script and
nothing else changes.

- **Dashboard** — ₹ amount at risk in large type, the financial waterfall for the
  worst settlement in the batch, the three reconciliation rates side by side
  (deliberately not blended), the ground-truth scorecard, and throughput.
- **Settlements** — every settlement with Δ₁/Δ₂/Δ₃/Δ₄ chips, expected vs actual
  vs bank, filters on tier and status.
- **Settlement detail** — the waterfall for that settlement, the Δ₁ arithmetic
  shown step by step against policy, the **Explain** panel rendering the
  attribution ledger as line items ending in the residual, plus tabs for
  payments, refunds, seller payouts, adjustments, bank, ledger, the matcher
  trail and the audit log. Where an anomaly was planted, the ground-truth note
  is shown alongside so you can check the diagnosis against the truth.
- **Exceptions** — what is wrong, the amount affected, what was proven, what
  remains unexplained, and a recommended action.
- **Seller payouts** — per-seller owed vs paid, with the unexplained figure.
- **Trace Money** — the `money_edges` recursive CTE as an expandable timeline.
  A table, never a node-link diagram: a graph picture looks impressive and tells
  a finance person nothing.

The **Demo Merchant Policy** banner is on every screen, and individual
policy-derived figures carry their own badge.

---

## A book that keeps being written

A settlement file is not a snapshot. Payments keep arriving, refunds turn up
weeks after the sale, and the bank credits yesterday's settlement tomorrow. So
the dataset grows:

```bash
make tick                       # +10 settlements onto the newest dataset, then re-reconcile
make tick TICK=50               # a bigger cycle
DATASET=<uuid> make tick        # a specific dataset
```

or **Simulate next cycle** in the UI, or `POST /api/datasets/{id}/tick`.

A tick runs the *same* generation pipeline over a slice that continues the
existing dataset — one pipeline, not a second "append generator" that would
drift away from the real one and quietly stop being a fair test. What continues:

- **the calendar**, resuming at the day after the last period ended, so periods
  still tile with no gaps and no overlaps *across the seam* — the property that
  makes the refund period gate unambiguous;
- **every id sequence**, re-derived from the data's own high-water mark rather
  than a counter someone could get wrong;
- **the population** — the same sellers keep trading, so Δ₄ payout history means
  something instead of restarting each cycle;
- **ground truth**, so detection rate and diagnosis accuracy are scored over the
  whole grown dataset, not just the newest slice.

Two things make an appended cycle worth watching rather than just bigger:

**Refunds arrive late.** A tick refunds payments that settled cycles ago. Those
are netted off the *current* settlement — a Δ₂ timing difference the engine
always handled but, on a fixed dataset, never actually saw. The refund posts
`DR REFUNDS / CR BANK`, deliberately **not** touching `RAZORPAY_CLEARING`: that
payment's clearing balance was closed by its own settlement, and crediting it
again would manufacture a Δ₃ imbalance on a settlement nobody touched.

**Bank credits are still in flight.** The last settlement of a cycle has not been
credited when the cycle closes — T+2 lands after the cutoff. It is reported as an
open Δ₂ exception, and the *next* tick lands the credit and closes it. An
exception that heals itself is the clearest available proof that a run is a
picture of a moment, not a permanent verdict.

Every tick re-reconciles the **whole** dataset and mints a new immutable
`run_id`. That is deliberate: a run is a complete, auditable picture of the book
at one instant, and `loader.load()` issues a fixed 10 queries no matter how big
the dataset is. Measured, single container, Postgres on localhost:

| settlements | records | reconcile | records/s |
|---:|---:|---:|---:|
| 100 | 28,249 | 0.53s | 53,239 |
| 200 | 57,819 | 1.05s | 55,270 |
| 350 | 102,121 | 1.60s | 63,654 |
| 500 | 146,448 | 3.11s | 47,067 |

Linear, not quadratic. Appending itself costs ~0.35s per cycle, of which 0.24s
is re-deriving those id high-water marks from the data — the safe choice over
trusting a stored counter, and the one part that grows with dataset size.

`make tick TICK=8` with `--clean` appends data with nothing wrong in it, which
must still reconcile to **exactly zero**. CI runs precisely that, twice in a row,
because a single append can pass on a counter that never advanced.

---

## The evaluation batch

`make evaluation-batch`, or the **Evaluation batch** button in the header.

The seeded generator is reproducible but large. This is the other thing: a
**fixed, hand-authored batch of 22 settlements and 345 financial records**, small
enough to read end to end, with every expected outcome written down in
`fixtures/evaluation_batch.json` beside the data that produces it. No seed, no
sampling, no clock — the same rows every time, under a constant `dataset_id`, so
loading it twice replaces it rather than piling up copies.

It exists so an evaluator can check the engine without trusting the generator,
and so a change to the engine can be compared against a fixed point.

| family | scenarios | what they cover |
|---|---:|---|
| clean | 3 | three payment methods; a retried order with two FAILED attempts; a refund correctly deducted |
| Δ₁ compute | 5 | fee charged at 250 bps against a policy of 200; GST taken on the aggregate instead of per item; a processed refund never deducted; header gross below the sum of its items; a chargeback that hit the header but was never itemised |
| Δ₂ bank | 5 | no credit at all; one settlement paid as two unlabelled credits; two settlements paid by one bulk credit; narration carrying only the UTR suffix |
| Δ₃ ledger | 3 | the settlement group posted twice; posted not at all; the gateway fee posted to SALES |
| Δ₄ payout | 2 | a seller short-paid with a REVERSED transfer accounting for the gap; a SETTLED allocation with no transfer at all |
| traps | 4 | a refund after period close that belongs to the *next* settlement; two genuinely identical payments on the same day; two settlements and two credits that nothing distinguishes |

The traps matter as much as the defects. A batch containing only broken things
cannot tell a thorough engine from a trigger-happy one, and in production a false
positive means a controller chasing money that was never missing.

Against this batch the engine scores **100% detection, 100% diagnosis, 100%
correct escalation, 3/3 traps avoided and zero false auto-resolutions** — and
`tests/test_evaluation_batch.py` asserts every one of those numbers, plus each
scenario's stated delta, tier and exception type, one test per scenario. The file
cannot quietly drift into describing something the engine no longer does.

`make evaluation-csv` writes the whole batch to `fixtures/csv/` as 15 flat files
— 992 rows, one per table, with `scenarios.csv` as the manifest. Useful for
reading it in Excel or checking the arithmetic outside the system entirely.

The batch is authored by `fixtures/authoring.py`, which contains no randomness
and derives every fee and tax from `policy.yaml` through the same `bps()` the
engine uses. Change the policy and a test fails on the config hash, telling you
to re-author rather than letting the two silently disagree.

---

## Seeing the data

The **Data** tab is a read-only browser over every table in the schema, scoped to
the run you are looking at. Row counts are live, so it is where you confirm that
a Generate or a Simulate actually landed — hit **Simulate next cycle** with the
tab open and the counts move without a refresh.

Tables are split into two groups, because they are two different things: the
**generated data** is the book the engine reads, and the **engine results** —
deltas, attributions, match candidates, exceptions, the audit log — are what it
concluded, every row tagged with a `run_id`.

The registry comes from PostgreSQL's own catalogue rather than a hand-written
list, so adding a table to `schema.sql` surfaces it with no code change. A table
name is the one value that cannot be parameterised, so it is checked against that
registry before it reaches any SQL string; page sizes are clamped server-side.
`api/browse.py` contains only `SELECT`s, and a test asserts it.

From the terminal instead:

```bash
./run.sh db-summary     # every dataset: settlements, records, cycles, runs
./run.sh db-shell       # psql, for anything else
```

Every money column is `BIGINT` paise. The tab formats them as rupees, with a
**raw paise** toggle for when you want the stored integer.

---

## The investigation agent

The engine proves the numbers. The agent explains them. Those are different
jobs, and keeping them apart is what makes either one worth having.

```bash
export GEMINI_API_KEY=...      # or XAI_API_KEY for Grok
make serve                     # the "Ask the agent" tab
```

Ask it *"what is the single largest unexplained amount, and why could the engine
not resolve it?"* and it calls tools, reads the persisted evidence, and answers
with the settlement id, the delta id, the rupee figure and the rule that applied.

**It cannot change anything.** `agent/tools.py` contains twelve functions and
only `SELECT` statements — a test asserts that, rather than trusting it. Every
query is scoped to the one run the session was opened on and parameterised here,
so no SQL the model wrote ever reaches the database; the model picks a tool name
and a few typed arguments, nothing more. Row limits are clamped in the tool, not
taken on trust from the arguments.

The tools it has:

| tool | what it reads |
|---|---|
| `run_overview` | the run's four match rates, tiers, throughput, ground-truth scoring |
| `list_settlements` | settlements ranked by unexplained residual |
| `get_settlement` | header, the settlement-level deltas, a Δ₄ summary |
| `get_evidence` | the attribution ledger — every explained rupee and the rule that authorised it |
| `list_exceptions` | open items by status, severity, delta |
| `get_matcher_trail` | every bank-matching pass tried, and why each failed |
| `get_payments` | payment lines with the fee and tax actually charged |
| `get_ledger` | double-entry postings and any non-zero clearing balance |
| `get_seller_payouts` | Δ₄ per allocation |
| `trace_money` | the lineage graph, up and down |
| `get_policy` | the policy registry the engine computed against |
| `get_audit` | the engine's own decision log |

**Two guardrails run after every answer.** The tool-call budget is bounded, so a
confused model stops rather than walking the dataset. And every record id the
answer mentions is checked against the ids that actually appeared in tool
results — anything else is reported in the UI as an *unverified reference*
rather than quietly shipped. That is the failure mode that matters with a
language model near financial data: not a wrong tone, but a confident sentence
naming a record it never read.

Every answer is persisted to `agent_transcripts` beside the engine's own
`audit_log`, with the tools it called and whether its citations held up. A
reviewer can hold the deterministic decision trail and the narrated one side by
side.

Both providers are reached through one adapter over the OpenAI-compatible
chat-completions format, written against the standard library — the project
still has six dependencies and no vendor SDK. With no key configured the panel
explains what is missing and every other screen is unaffected; the agent is
strictly additive to a system that already works without it.

The agent is tested against a stubbed model, so all of this is covered in CI with
no API key and no network: scoping across two runs that share settlement ids,
limit clamping, refusal of unknown tools, malformed arguments, budget
exhaustion, and the citation guard catching a fabricated record id.

---

## What P0 deliberately leaves out

P1+ adds an LLM investigation agent and a conversational Settlement Q&A panel
*on top of* this engine. P0 is the deterministic core underneath — the
calculation engine, matcher, exception taxonomy, audit trail and UI — and it
clears the bar on its own with zero model calls. That is a sequencing decision:
ship something that unambiguously works first, then layer reasoning on top of a
foundation whose answers you can check.

---

## Three lines for the pitch

> Exact-key matching is deterministic code. We never ask a model whether two
> numbers are equal.

> We planted seven discrepancies that are genuinely undiagnosable. The system
> escalated all seven. It reports what it cannot explain, in rupees.

> A settlement can reconcile perfectly while a seller is still underpaid — those
> are different questions, so we answer them separately instead of folding
> seller payouts into one blended score.
