# Razor Recon AI

**A settlement reconciliation engine that shows its working.**

Razorpay Hackathon — Track 04, *AI Finance Controller: run the books and the cash position*.

---

## Contents

- [What problem this solves](#what-problem-this-solves)
- [The five-minute quick start](#the-five-minute-quick-start)
- [Tech stack](#tech-stack)
- [The big idea: four deltas, never blended](#the-big-idea-four-deltas-never-blended)
- [How money is stored (and why it matters)](#how-money-is-stored-and-why-it-matters)
- [The policy registry](#the-policy-registry)
- [The working-day calendar](#the-working-day-calendar)
- [Running it — every command](#running-it--every-command)
- [Docker: what runs in a container and what doesn't](#docker-what-runs-in-a-container-and-what-doesnt)
- [Environment variables and API keys](#environment-variables-and-api-keys)
- [The three kinds of dataset](#the-three-kinds-of-dataset)
- [The database schema, table by table](#the-database-schema-table-by-table)
- [What happens during a run](#what-happens-during-a-run)
- [The bank matcher, pass by pass](#the-bank-matcher-pass-by-pass)
- [Attribution and the tier gate](#attribution-and-the-tier-gate)
- [Exceptions: the honest list](#exceptions-the-honest-list)
- [Ground truth and the honesty metrics](#ground-truth-and-the-honesty-metrics)
- [The interface, screen by screen](#the-interface-screen-by-screen)
- [Cash position](#cash-position)
- [Tax credit](#tax-credit)
- [The investigation agent](#the-investigation-agent)
- [Append mode](#append-mode-simulate-next-cycle)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Full API reference](#full-api-reference)
- [Troubleshooting](#troubleshooting)
- [What this deliberately does not do](#what-this-deliberately-does-not-do)

---

## What problem this solves

Imagine you run a marketplace. A customer pays ₹10,000 for something. A few days
later Razorpay sends you a settlement: ₹9,764, with a note saying they took ₹200
as their fee and ₹36 as GST on that fee. Money lands in your bank. Your
accountant posts it. Your seller gets their cut.

Four separate things just happened, and **any of them can be wrong**:

1. Did Razorpay calculate that ₹236 correctly, according to your contract?
2. Did the ₹9,764 they promised actually arrive in your bank account?
3. Did your own accounting books record it correctly?
4. Did your seller actually get paid what they were owed?

Now multiply that by ten thousand payments a month. Somewhere in there, a fee was
charged at 2.5% instead of 2%. A bank credit never arrived. A ledger entry got
posted twice. A seller is quietly ₹47,000 short. Nobody notices, because every
individual number looks plausible.

**Razor Recon AI checks all four, separately, and tells you in rupees exactly what
it could not explain.**

The important word there is *separately*. Most reconciliation tools give you one
number — "97% reconciled" — which sounds great and tells you nothing. A settlement
can be flawless on the gateway's arithmetic and still leave a seller underpaid.
Those are different questions with different answers and different people
responsible for fixing them, so we never fold them into one score.

### What makes this different from "we asked an LLM"

Every rupee in this system is computed by deterministic code. No model decides
whether two numbers are equal. Given the same data, you get byte-identical results
every single time.

There *is* an AI agent — but it can only **read** finished results and explain
them in English. It cannot change a number, a status, or a verdict. That
separation is deliberate: a number you can prove and an explanation you can read
are two different products, and blending them costs you the first one.

---

## The five-minute quick start

You need **Docker** (for the database), **Python 3.12**, and **psql**.

```bash
# 1. Install the Python dependencies
make install

# 2. Start PostgreSQL in Docker and wait for it to be ready
make db-up

# 3. Create the database tables
make schema

# 4. Generate a realistic dataset — 100 settlements, ~28,500 records
make generate

# 5. Reconcile it and print a report to your terminal
make reconcile

# 6. Start the web interface
make serve
```

Open **http://localhost:8000**. You will land on a title screen; click
*Open the dashboard*.

Want to try it without Make?

```bash
pip install -r requirements.txt
docker compose up -d
export DATABASE_URL="postgresql://finctl:finctl@localhost:5433/finctl"
psql "$DATABASE_URL" -f db/schema.sql
psql "$DATABASE_URL" -f db/indexes.sql
python3.12 -m generator.generate --seed 42 --settlements 100 --label demo
python3.12 -m scripts.reconcile
./run.sh serve
```

Or do the first five steps in one shot:

```bash
make demo      # db-up + schema + generate + reconcile
make serve
```

### Two optional extras

These add features to a **live** database without destroying anything:

```bash
make agent-schema   # adds the table that stores AI agent conversations
make tax-schema     # adds the GSTR-2B table for the tax credit feature
```

Run them once. They are safe to re-run — they use `CREATE TABLE IF NOT EXISTS`.

---

## Tech stack

Deliberately small. Six dependencies, no ORM, no npm, no build step, no vendor AI
SDK.

| Layer | What we used | Why |
|---|---|---|
| **Language** | Python 3.12 | Type hints, `dataclasses`, `match`. Nothing exotic. |
| **Database** | PostgreSQL 16 | Recursive CTEs for money tracing, `BIGINT` for exact money, `JSONB` for metrics. |
| **DB driver** | `psycopg` 3 | Modern, fast, no ORM in between us and the SQL. |
| **API** | FastAPI + Uvicorn | Async, automatic OpenAPI docs at `/docs`, tiny. |
| **Validation** | Pydantic 2 | Request/response models. |
| **Config** | PyYAML | The policy registry is a YAML file, versioned and hashed. |
| **Frontend** | Vanilla JavaScript | One HTML file. No React, no bundler, no CDN — it runs offline. |
| **Charts** | Hand-rolled SVG | ~200 lines. No charting library. |
| **Tests** | pytest | 271 of them. |
| **Container** | Docker Compose | Only for PostgreSQL. Nothing else is containerised. |
| **CI** | GitHub Actions | Spins up Postgres, runs the suite, gates on clean-data-nets-to-zero. |
| **AI (optional)** | Gemini or Grok, via `urllib` | OpenAI-compatible wire format. No SDK — 167 lines of stdlib. |

**Why no ORM?** Reconciliation is join-heavy and the queries are the interesting
part. An ORM would hide exactly what we want visible.

**Why vanilla JS?** The whole UI is one 112KB HTML file with zero network
dependencies. It works on a laptop with no internet, which is the situation you
are in five minutes before a demo.

---

## The big idea: four deltas, never blended

A delta is a difference between what *should* have happened and what *did*. We
compute four of them for every settlement, from four independent sources.

### Δ1 — COMPUTE: did the gateway bill you correctly?

**Question:** Recomputing the fee, tax and refunds from your policy contract, does
the settlement's stated net match?

**Example.** A customer pays ₹10,000 by card.

```
Gross                          ₹10,000.00
MDR at 200 bps (2%)            −  ₹200.00      POLICY.MDR.CARD@1.0.0
GST at 1800 bps (18%) on fee   −   ₹36.00      POLICY.TAX.GST_ON_FEE@1.0.0
                               ───────────
Expected net                    ₹9,764.00
Settlement report claims        ₹9,764.00
Δ1                                    ₹0.00  ✓
```

Now here is the same payment where somebody misconfigured a rate:

```
Settlement report claims        ₹9,705.00
Δ1                                 −₹59.00  ✗   FEE_RATE_MISMATCH
```

The engine can prove that ₹59 is exactly the difference between 200 bps and
250 bps on ₹10,000 plus GST — so it names the cause rather than shrugging.

### Δ2 — BANK: did the money actually arrive?

**Question:** Does a real bank credit exist for that ₹9,764, and can we prove it
belongs to *this* settlement?

This is harder than it sounds. Bank statements are messy. The UTR gets truncated.
Two settlements land on the same day for the same amount. One credit covers three
settlements at once. The [matcher section](#the-bank-matcher-pass-by-pass) covers
how we handle each case.

**Example.** Settlement `SET_0042` expects ₹9,764. The bank statement has a credit
for ₹9,764 on the right day, but the UTR field is blank and the narration reads
`NEFT CR-HDFC-RAZORPAY-XXXX7781`. We match it on the UTR suffix `7781`, but only
after checking that no *other* settlement in the window ends in `7781`. If two
did, we refuse to match and escalate.

### Δ3 — LEDGER: do your own books agree?

**Question:** Ignoring everyone else — does your double-entry accounting reflect
this correctly?

A settlement posts four entries:

```
DR  BANK                ₹9,764.00
DR  GATEWAY_FEES          ₹200.00
DR  INPUT_GST              ₹36.00
CR  RAZORPAY_CLEARING  ₹10,000.00
                       ───────────
                        balanced ✓
```

**Example failure.** Somebody enters that group twice. Each group still balances
internally, so a naive balance check passes. But `RAZORPAY_CLEARING` is now
over-credited by ₹10,000 — money shown as settled that never was. Only an
account-level check finds it. That is `DUPLICATE_LEDGER_ENTRY`.

### Δ4 — PAYOUT: did the seller get paid?

**Question:** Each seller was allocated a share of the payment. Did that money
actually move?

**Example.** A ₹10,000 payment splits between two sellers:

```
Seller A (SMB, 1200 bps)     allocated ₹6,000  commission ₹720  owed ₹5,280
Seller B (SMB, 1200 bps)     allocated ₹4,000  commission ₹480  owed ₹3,520
Transfers that actually moved:          ₹5,280 to A,   ₹0 to B
Δ4                                                  −₹3,520  ✗   TRANSFER_MISSING
```

Seller B is owed ₹3,520 that never moved. Δ1, Δ2 and Δ3 are all perfect on this
settlement. **This is the case a single blended score hides.**

### Why they stay separate

Every screen reports these four independently:

```
D1 compute 97.00%   D2 bank 96.00%   D3 ledger 100.00%   D4 payout 99.94%
```

Different causes, different owners, different fixes. Δ1 is a conversation with
your gateway. Δ2 is a conversation with your bank. Δ3 is your own accountant.
Δ4 is your payouts team. One number would tell all four of them nothing.

---

## How money is stored (and why it matters)

**Every rupee amount in this system is an integer number of paise, in a `BIGINT`
column.** ₹9,764.00 is stored as `976400`.

There are no floats anywhere in the money path. Not one. There is no `Decimal`
either.

**Why this is not paranoia.** In floating point:

```python
>>> 0.1 + 0.2
0.30000000000000004
```

Do that ten thousand times across a settlement batch and you accumulate drift.
Then your reconciliation engine reports a ₹0.03 discrepancy that does not exist,
somebody investigates it for two hours, and your accuracy number becomes noise.

Every percentage in the policy is stored as **basis points** — an integer
hundredth of a percent. 2% is `200`. 18% is `1800`.

All rounding goes through exactly one function:

```python
def bps(amount_paise: int, rate_bps: int) -> int:
    """Round-half-up. The ONLY rounding function on the money path."""
```

Python's built-in `round()` is *banker's rounding* — it rounds 0.5 to the nearest
even number. Using it would silently manufacture off-by-one-paise drift, which
would then be diagnosed as a fake anomaly. `bps()` raises `TypeError` if you hand
it a float.

Column names end in `_paise` so it is hard to forget, and `test_invariants.py`
fails the build if any `*_paise` or `*_bps` column is ever changed away from an
integer type.

---

## The policy registry

Every rate, window and rule lives in **`policy/policy.yaml`** — never hardcoded.
Both the data generator and the reconciliation engine read the same file, so
neither can drift from the other.

```yaml
version: "1.0.0"
currency: "INR"

rounding:
  mode: "HALF_UP"
  tax_computation: "PER_ITEM"     # tax per line, then summed

mdr_bps:                          # gateway fee, in basis points
  UPI: 0
  CARD: 200
  CARD_INTL: 300
  NETBANKING: 175
  WALLET: 200

gst_on_fee_bps: 1800              # 18% GST on the fee

refunds:
  window_days: 180
  mdr_refunded: false             # the fee is NOT returned on a refund

settlement:
  cycle_working_days: 2           # T+2
  exclude_sundays: true
  exclude_second_fourth_saturday: true
  holidays: ["2026-01-26", "2026-03-04", "2026-03-21", "2026-04-14",
             "2026-05-01", "2026-08-15", "2026-10-02", "2026-10-20",
             "2026-12-25"]

bank_credit:
  expected_lag_days: 0            # credit lands on the settlement date
  tolerance_days: 1               # but may slip by one

commission_bps_by_seller_type:    # marketplace commission
  INDIVIDUAL: 1500
  SMB: 1200
  ENTERPRISE: 800

matching:
  amount_tolerance_paise: 0
  date_window_days: 3
  subset_sum_max_candidates: 10
  subset_sum_max_subset_size: 4
  fuzzy_reference_min_score_bps: 8500
```

### Rule IDs and the config hash

Every rule has an ID: `POLICY.MDR.CARD@1.0.0`. When the engine explains a
number, it cites the rule that produced it. You can trace any rupee back to the
exact line of policy that authorised it.

The whole file is SHA-256 hashed into a 16-character **`config_hash`**
(currently `8e2326ce0e4335ea`), stamped on every run. Change one digit of one rate
and the hash changes, so you can always tell which rules produced which results.

> ⚠️ **These are not Razorpay's real terms.** Every figure is a synthetic "Demo
> Merchant Policy" written for this project. Real MDR and settlement timing vary
> by merchant category, risk grade and commercial agreement. The UI carries a
> **Demo policy** chip on every screen for exactly this reason.

---

## The working-day calendar

Settlement dates are not "+2 days". They are **+2 working days**, and getting
that wrong makes an on-time credit look late.

A day is *not* a working day if it is:

- a **Sunday**;
- the **2nd or 4th Saturday** of the month (Indian banking convention);
- one of the **9 bank holidays** listed in the policy.

**Example.** A payment captured on Friday 2026-03-13:

```
Fri 13 Mar  capture
Sat 14 Mar  2nd Saturday — not a working day
Sun 15 Mar  Sunday       — not a working day
Mon 16 Mar  working day 1
Tue 17 Mar  working day 2  ← settles here
```

Four calendar days, two working days. A naive "+2 days" calculation would say
Sunday 15 March and flag a perfectly normal settlement as two days late.

This one calendar drives settlement dates, bank-credit due dates, the cash
forecast, and the closed-tiling of settlement periods.

---

## Running it — every command

There are two identical entry points. Use whichever you prefer:

- **`make <target>`** — needs the `Makefile`
- **`./run.sh <target>`** — plain bash, no Make required

Both read `DATABASE_URL`, defaulting to `postgresql://finctl:finctl@localhost:5433/finctl`.

### Setup

| Command | What it does |
|---|---|
| `make install` | `pip install -r requirements.txt` |
| `make db-up` | `docker compose up -d`, then polls `pg_isready` until Postgres answers |
| `make db-down` | `docker compose down` (your data survives — it's in a named volume) |
| `make schema` | Runs `db/schema.sql` then `db/indexes.sql`. **⚠️ DESTRUCTIVE** — drops and recreates every table |
| `make agent-schema` | Adds `agent_transcripts` to a **live** database. Idempotent, safe |
| `make tax-schema` | Adds `tax_invoices` to a **live** database. Idempotent, safe |

### Making data

| Command | What it does |
|---|---|
| `make generate` | 100 settlements, ~28,500 records, seed 42, with anomalies planted |
| `make generate SEED=7 SETTLEMENTS=250 LABEL=big` | Same, your parameters |
| `make generate-clean` | Same generator, **zero anomalies**. Must reconcile to exactly ₹0 |
| `make evaluation-batch` | Loads the fixed 22-scenario hand-authored batch |
| `make evaluation-csv` | Exports that batch to 16 CSV files in `fixtures/csv/` |
| `make tick` | Appends 10 more settlements to the newest dataset, then re-reconciles |
| `make tick TICK=25` | Appends 25 |
| `make tick DATASET=<uuid>` | Appends to a specific dataset |

### Running and looking

| Command | What it does |
|---|---|
| `make reconcile` | Runs the engine over the newest dataset, prints a full report |
| `make serve` | Builds the SPA and starts the API on http://localhost:8000 |
| `make serve PORT=9000` | Same, different port |
| `make web` | Just copies `web/index.html` → `api/static/index.html` |
| `make db-shell` | Opens `psql` against the project database |
| `make db-summary` | One row per dataset: settlements, records, cycles, runs |
| `make test` | The full pytest suite (needs a database) |
| `make demo` | `db-up` + `schema` + `generate` + `reconcile` in one go |
| `make help` | Lists every target with its description |

### The underlying Python commands

Everything above is a thin wrapper. If you would rather call the modules
directly:

```bash
# Generate a dataset
python3.12 -m generator.generate --seed 42 --settlements 100 --label demo
python3.12 -m generator.generate --seed 42 --settlements 100 --label clean --clean

# Append a settlement cycle to an existing dataset
python3.12 -m generator.append --settlements 10
python3.12 -m generator.append --settlements 10 --dataset <uuid>

# Reconcile and print the terminal report
python3.12 -m scripts.reconcile

# Load the static evaluation batch
python3.12 -m fixtures.loader

# Export the evaluation batch to CSV
python3.12 -m fixtures.export_csv

# Re-author the evaluation batch JSON from fixtures/authoring.py
python3.12 -m fixtures.authoring

# Serve the API
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# Run the tests
python3.12 -m pytest tests/ -q
python3.12 -m pytest tests/test_taxmatch.py -v        # one file, verbose
python3.12 -m pytest tests/ -q -k "forecast"          # by name
```

### Handy psql queries

```bash
psql "$DATABASE_URL"                          # interactive shell
psql "$DATABASE_URL" -c "\dt"                 # list all tables
psql "$DATABASE_URL" -c "\d settlements"      # describe one table

# Every dataset, with its record counts
psql "$DATABASE_URL" -c "SELECT label, seed, row_counts->>'settlements' AS n,
  row_counts->>'total_financial_records' AS records FROM datasets ORDER BY generated_at DESC;"

# The newest run's headline metrics
psql "$DATABASE_URL" -c "SELECT run_id, status, metrics->'accuracy'
  FROM reconciliation_runs ORDER BY started_at DESC LIMIT 1;"

# What is still unexplained, worst first
psql "$DATABASE_URL" -c "SELECT settlement_id, exception_type, unexplained_paise, tier
  FROM exceptions WHERE run_id = (SELECT run_id FROM reconciliation_runs
  ORDER BY started_at DESC LIMIT 1) ORDER BY unexplained_paise DESC LIMIT 10;"
```

### If your Postgres is on a different port

The Docker service publishes **5433** (not 5432) so it cannot collide with a
Postgres you already have installed. If yours is elsewhere:

```bash
export DATABASE_URL="postgresql://finctl:finctl@localhost:5432/finctl"
```

Put it in your shell profile, or copy `.env.example` to `.env`.

### If `python3.12` is not your Python

```bash
make test PYTHON=python3
make generate PYTHON=/usr/local/bin/python3.12
```

---

## Docker: what runs in a container and what doesn't

**Only PostgreSQL runs in Docker.** The engine, generator, API and UI are plain
Python and plain files. There is no application image, no Dockerfile, no
multi-container orchestration.

That is a deliberate choice: the interesting part of this project is the
reconciliation logic, and putting it behind a container build would add a slow
step between you and every code change.

`docker-compose.yml` in full:

```yaml
services:
  db:
    image: postgres:16-alpine
    container_name: finctl-db
    environment:
      POSTGRES_USER: finctl
      POSTGRES_PASSWORD: finctl
      POSTGRES_DB: finctl
    ports:
      - "5433:5432"          # host 5433 → container 5432
    volumes:
      - finctl-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U finctl -d finctl"]
      interval: 3s
      timeout: 3s
      retries: 20

volumes:
  finctl-pgdata:
```

Useful Docker commands:

```bash
docker compose up -d              # start
docker compose down               # stop (data survives)
docker compose down -v            # stop AND delete all data
docker compose logs -f db         # tail the Postgres log
docker compose exec db psql -U finctl -d finctl   # psql inside the container
docker compose ps                 # is it running?
```

**Don't want Docker at all?** You don't need it. Point `DATABASE_URL` at any
PostgreSQL 16 instance, run `make schema`, and everything else works identically.

---

## Environment variables and API keys

**The reconciliation needs no API key.** Every number, every accuracy percentage
and every exception is computed locally with zero network calls. You can run this
whole project offline.

API keys are only for the **optional** investigation agent, which explains
finished results in English.

Copy `.env.example` to `.env`, or just export these:

| Variable | Required? | Default | What it does |
|---|---|---|---|
| `DATABASE_URL` | **Yes** | `postgresql://finctl:finctl@localhost:5433/finctl` | Postgres connection string |
| `POLICY_PATH` | No | `./policy/policy.yaml` | Point at a different policy registry |
| `GEMINI_API_KEY` | No | — | Turns on the agent, using Google Gemini |
| `GOOGLE_API_KEY` | No | — | Alias for the above |
| `XAI_API_KEY` | No | — | Turns on the agent, using xAI Grok |
| `GROK_API_KEY` | No | — | Alias for the above |
| `FINCTL_LLM_PROVIDER` | No | whichever key is set | Force `gemini` or `grok` |
| `FINCTL_LLM_MODEL` | No | `gemini-2.5-flash` / `grok-4-fast` | Use a different model |
| `FINCTL_LLM_BASE_URL` | No | provider default | Point at a proxy or a local model |

### Turning the agent on

```bash
export GEMINI_API_KEY="your-key-here"
make serve
```

That's it. Get a Gemini key from Google AI Studio, or a Grok key from the xAI
console. Whichever key is present picks the provider — no other configuration.

To use Grok instead:

```bash
export XAI_API_KEY="your-key-here"
make serve
```

To force a specific model:

```bash
export GEMINI_API_KEY="..."
export FINCTL_LLM_MODEL="gemini-2.5-pro"
make serve
```

### With no key set

Everything works except the two AI panels, which say what is missing and how to
fix it. No crashes, no degraded numbers. `GET /api/agent/status` reports which
providers are configured, and the UI reads that.

**Why no OpenAI SDK?** Both Gemini and Grok expose an OpenAI-compatible
`/chat/completions` endpoint. `agent/llm.py` is 167 lines of `urllib` that talks
to either one. That's one fewer dependency and one fewer thing to keep updated.

---

## The three kinds of dataset

### 1. The seeded generator — realistic scale

```bash
make generate                                    # 100 settlements
make generate SEED=7 SETTLEMENTS=500 LABEL=big   # bigger
```

Produces a full marketplace's worth of data from a seed. **Same seed, byte-identical
data, every time** — so a bug is always reproducible.

A 100-settlement run creates:

| Table | Rows |
|---|---:|
| customers | 800 |
| sellers | 40 |
| orders | 3,045 |
| payments | 3,686 |
| refunds | 276 |
| seller_allocations | 3,472 |
| transfers | 3,207 |
| adjustments | 75 |
| settlements | 100 |
| settlement_items | 3,810 |
| bank_transactions | 139 |
| ledger_entries | 13,902 |
| money_edges | 46,419 |
| ground_truth_anomalies | 62 |
| **total financial records** | **28,567** |

Then an anomaly pass deliberately breaks **62** of them and writes down exactly
what it broke in `ground_truth_anomalies`. That table is the answer key — the
engine never reads it during reconciliation, only the scorer does afterwards.

The planted anomalies span all four deltas:

- **Δ1:** fee rate drift, refund not deducted, refund outside period, tax
  aggregate rounding, header rollup mismatch, unexplained adjustment
- **Δ2:** missing credit, split credit, merged credit, timing next-day, UTR
  suffix collision, narration with no UTR, settlement on hold
- **Δ3:** duplicate ledger group, missing ledger group, wrong account
- **Δ4:** allocation exceeds payment, transfer missing, allocation/transfer
  divergence, phantom payout gap

Plus **deliberately undiagnosable** cases (an unexplained shortfall with no source
record; an ambiguous credit where two bank lines fit equally well) that the engine
is *supposed* to escalate rather than guess at — and **traps**, which look wrong
but are actually fine, where guessing would be a false positive.

### 2. The evaluation batch — small, fixed, hand-authored

```bash
make evaluation-batch     # or the "Evaluation batch" button in the header
```

22 settlements, 395 financial records, **the same rows every single time**. No
seed, no sampling, no clock. It exists so an evaluator can check the engine
without trusting the generator, and so a change to the engine can be compared
against a fixed point.

Every scenario states its own expected outcome in
`fixtures/evaluation_batch.json` — the delta on each of the four axes, the worst
tier, the exception types. `tests/test_evaluation_batch.py` asserts all 22 against
a real run, so the file cannot quietly become a description of something the
system no longer does.

The batch covers refunds (deducted, not deducted, and belonging to the next
period), adjustments, multiple payments on one order, duplicates, failed payments
that must never settle, a suspended seller, and a reversed transfer. It has 26
named customers and 6 sellers across all three commission tiers.

It also carries a **pipeline tail**: 13 payments captured after the last
settlement period closed, with 11 allocations still pending. Those exist so the
cash forecast has a forward position to report.

Export it to spreadsheets:

```bash
make evaluation-csv       # → fixtures/csv/, 16 files, 1,139 rows
```

### 3. Appended cycles — the system running over time

```bash
make tick                 # or "Simulate next cycle" in the header
```

Adds the next settlement cycle to an existing dataset and re-reconciles
everything, so you can watch the books evolve. Covered in detail
[further down](#append-mode-simulate-next-cycle).

---

## The database schema, table by table

23 tables in three groups. Everything is scoped by `dataset_id` (source data) or
`run_id` (engine output), and both cascade on delete — remove a dataset and its
entire tree goes with it.

### Source data — what a real system would give you

These are scoped by `dataset_id`.

**`datasets`** — one row per generated batch.
`dataset_id, seed, policy_version, generated_at, row_counts (JSONB), label`
The `row_counts` blob holds every table's count plus the appended-cycle log.

**`customers`** — `dataset_id, customer_id, name, email, created_at`

**`sellers`** — `dataset_id, seller_id, seller_name, seller_type, commission_bps, status`
`seller_type` is `INDIVIDUAL` / `SMB` / `ENTERPRISE`, which sets the commission.
`status` can be `SUSPENDED`, which is a legitimate reason for a payout not to move.

**`orders`** — `dataset_id, order_id, customer_id, order_amount_paise, currency, order_date, order_status`

**`payments`** — `dataset_id, payment_id, order_id, customer_id, amount_paise, currency, payment_status, payment_method, created_at, captured_at, failure_reason`
`payment_status` is `CREATED` / `AUTHORIZED` / `CAPTURED` / `REFUNDED` / `FAILED`.
Two database constraints keep it honest: `captured_at` must be set if and only if
the status is `CAPTURED` or `REFUNDED`, and `failure_reason` must be set if and
only if the status is `FAILED`.

**`refunds`** — `dataset_id, refund_id, payment_id, refund_amount_paise, refund_status, refund_date, refund_reason`

**`seller_allocations`** — `dataset_id, allocation_id, payment_id, seller_id, gross_allocated_paise, commission_paise, net_seller_paise, allocation_status, allocation_date`
What each seller was *owed*. `allocation_status` is `PENDING` / `SETTLED` /
`REVERSED`. A constraint enforces `net = gross − commission`.

**`transfers`** — `dataset_id, transfer_id, payment_id, seller_id, amount_paise, transfer_status, transfer_date, transfer_reference`
What actually *moved*. Allocation minus transfer is Δ4.

**`adjustments`** — `dataset_id, adjustment_id, settlement_id, adjustment_type, amount_paise, reason, created_at, status, ref_payment_id`
Chargebacks, reserve holds, corrections.

**`settlements`** — `dataset_id, settlement_id, settlement_date, settlement_period_start, settlement_period_end, gross_amount_paise, refund_amount_paise, fee_amount_paise, tax_amount_paise, adjustment_amount_paise, net_settlement_amount_paise, settlement_status, settlement_utr`
The header. **This is a rollup, not the source of truth** — `settlement_items` is.
Header ≠ Σ items is an *exception* (`HEADER_ROLLUP_MISMATCH`), not an assumption.
Periods form a **closed tiling**: no gaps, no overlaps, so "which period does this
refund belong to" always has exactly one answer.

**`settlement_items`** — `dataset_id, settlement_item_id, settlement_id, transaction_type, payment_id, refund_id, adjustment_id, transfer_id, amount_paise, fee_paise, tax_paise, transaction_date`
The individual lines. `transaction_type` is `PAYMENT` / `REFUND` / `ADJUSTMENT` /
`TRANSFER`. Exactly one of the four foreign keys is set per row.

**`bank_transactions`** — `dataset_id, bank_transaction_id, transaction_date, description, credit_paise, debit_paise, currency, bank_reference, settlement_utr`
The bank statement. Deliberately messy: `settlement_utr` is often null, and the
`description` is narration text you have to parse.

**`ledger_entries`** — `dataset_id, ledger_entry_id, entry_group_id, account, direction, amount_paise, order_id, payment_id, refund_id, settlement_id, seller_id, ledger_date, description`
Double-entry postings. `entry_group_id` groups the legs that must balance.
Accounts: `BANK`, `SALES`, `REFUNDS`, `GATEWAY_FEES`, `INPUT_GST`,
`RAZORPAY_CLEARING`.

**`money_edges`** — `dataset_id, src_type, src_id, dst_type, dst_id, edge_kind, amount_paise`
The lineage graph. A denormalised index of every relationship, in one uniform
shape, so "where did this rupee come from" is one recursive query instead of
eight different joins. Edge kinds: `PLACED`, `PAID_BY`, `REFUNDED_BY`,
`ALLOCATED_TO`, `TRANSFERRED_BY`, `PAID_OUT_AS`, `SETTLED_AS`, `PART_OF`,
`CREDITED_AS`, `POSTED_AS`.
⚠️ **Never `SUM` the `amount_paise` column** — it is the amount of the destination
record, not a conserved flow. A ₹10,000 payment has six ledger edges carrying
different amounts because it posts as a balanced *group*.

**`ground_truth_anomalies`** — `dataset_id, anomaly_id, anomaly_type, subject_type, subject_id, settlement_id, expected_delta_kind, expected_exception_type, original_field, original_value_paise, mutated_value_paise, planted_amount_paise, is_resolvable, notes`
The answer key. What was broken, where, and whether it is even diagnosable. The
engine never reads this during reconciliation — only the scorer touches it,
afterwards.

**`tax_invoices`** *(from `db/tax.sql`)* — `dataset_id, invoice_no, invoice_date, return_period, supplier_gstin, document_type, settlement_id, taxable_value_paise, cgst_paise, sgst_paise, igst_paise, itc_eligible, ineligible_reason, filed_at`
The synthetic GSTR-2B feed. `return_period` is the month the line *appeared in*,
which is different from `invoice_date` — a late filing puts an early invoice in a
later period, and that gap is what the timing check looks for. A constraint
enforces that a supply is either CGST+SGST or IGST, never both.

### Engine output — immutable, scoped by `run_id`

**`reconciliation_runs`** — `run_id, dataset_id, policy_version, engine_version, config_hash, started_at, finished_at, status, metrics (JSONB)`
One row per run. **Runs are immutable** — reconciling again mints a new `run_id`;
nothing is ever mutated. The `metrics` blob holds the full accuracy report.

**`reconciliation_deltas`** — `run_id, delta_id, settlement_id, delta_kind, subject_id, expected_paise, actual_paise, delta_paise, explained_paise, residual_paise, tier, status`
One row per settlement per delta kind. `residual = delta − explained` — the money
still unaccounted for after attribution.

**`attributions`** — `run_id, attribution_id, delta_id, evidence_type, evidence_record_id, signed_amount_paise, derivation, rule_ids, rationale`
The line items that explain a delta. Each cites the record it used, the policy
rules it applied, and the arithmetic. This is what the **Explain** panel renders.

**`exceptions`** — `run_id, exception_id, settlement_id, subject_id, delta_kind, exception_type, severity, amount_paise, explained_paise, unexplained_paise, tier, status, recommended_action, created_at`
What is still wrong after attribution, with a plain-English next step.

**`match_candidates`** — `run_id, settlement_id, bank_transaction_id, pass_name, score_bps, is_selected, is_ambiguous`
Every bank line the matcher considered, which pass found it, its score, and
whether it was chosen. This is the **matcher trail** — you can see what it tried
and why each attempt failed.

**`audit_log`** — `run_id, audit_id, ts, actor, action, subject_type, subject_id, inputs, rule_ids, outputs, decision, tier`
The engine's own decision log. Every conclusion, the rule behind it, and the
inputs it used.

**`agent_transcripts`** *(from `db/agent.sql`)* — `run_id, turn_id, asked_at, question, answer, provider, model, tool_calls, tool_call_count, citations, unsupported_references, grounded, stop_reason, elapsed_seconds`
Every AI answer, the tools it called, and whether its citations held up. A
reviewer can put the deterministic trail and the narrated one side by side.

### Why three SQL files

| File | Destructive? | When to run |
|---|---|---|
| `db/schema.sql` | **Yes** — drops everything | Once, at setup |
| `db/indexes.sql` | No | With the schema |
| `db/agent.sql` | No — `IF NOT EXISTS` | Any time, on a live database |
| `db/tax.sql` | No — `IF NOT EXISTS` | Any time, on a live database |

Features added after the first setup ship as idempotent SQL so you never have to
wipe your data to get them.

---

## What happens during a run

`make reconcile` (or the **Run reconciliation** button) does this, in order:

1. **Load the policy** from `policy/policy.yaml`, hash it, mint a new `run_id`.
2. **Check structural invariants.** A settlement item pointing at a payment that
   does not exist cannot be reasoned about at all — that record is excluded,
   logged, and can never reach tier A. The run continues.
3. **Δ1 — recompute from policy.** For every settlement, recompute fee, tax and
   refunds from the registry and compare against the header.
4. **Δ2 — match the bank.** Run the matcher passes (next section).
5. **Δ3 — check the ledger.** Group balance, account-level integrity, and whether
   `RAZORPAY_CLEARING` nets to zero.
6. **Δ4 — check payouts.** Allocations versus transfers, per seller.
7. **Attribute.** For every non-zero delta, try to prove where the money went.
8. **Raise exceptions** for whatever is left unexplained, and assign a tier.
9. **Score against ground truth** — detection, diagnosis, escalation, traps, false
   auto-resolutions.
10. **Write everything** to `reconciliation_deltas`, `attributions`, `exceptions`,
    `match_candidates`, `audit_log` and the run's `metrics` blob.

On the 100-settlement dataset that whole pipeline takes **0.44 seconds** — about
65,000 records a second, p95 0.24ms per settlement.

### What a run prints

```
==============================================================================
  RAZOR RECON AI -- reconciliation report
  run 2aa7282c-…   policy 1.0.0   config 8e2326ce0e4335ea
==============================================================================

THROUGHPUT
  100 settlements backed by 28,567 financial records …
  batch completed in 0.438s (65,278 records/s); p50 0.172ms, p95 0.24ms

MEASURED ACCURACY -- reported separately, never blended
  settlement match rate (all four deltas)    92.00%   92/100
  monetary reconciliation rate               96.19%   of Rs 5,98,65,539.77
  seller payout reconciliation (D4)          99.94%
    D1 compute 97.00%   D2 bank 96.00%   D3 ledger 100.00%   D4 payout 99.94%

AMOUNT AT RISK, RIGHT NOW
  Rs 22,86,062.18   across 12 open exceptions

HONESTY CHECKS (scored against ground truth planted at generation time)
  resolvable anomalies detected       50/50   100.00%
  diagnosis accuracy                  50/50   100.00%
  undiagnosable cases escalated        7/7    100.00%
  false-positive traps avoided         5/5
  FALSE AUTO-RESOLUTIONS               0      (must be 0)

TIERS
  A auto-resolved  3496 (99.66%)   B needs review 3 (0.09%)   C unresolved 9 (0.26%)
```

---

## The bank matcher, pass by pass

Matching a settlement to a bank credit is the hardest part, because bank data is
genuinely messy. The matcher tries eight passes in order of how much they prove.

| Pass | Tier | What it does |
|---|:---:|---|
| `EXACT_UTR` | A | The bank line carries the settlement UTR verbatim |
| `UTR_IN_NARRATION` | A | The full UTR appears inside the narration text |
| `UTR_SUFFIX` | A | Last N digits match — **only if unique in the window** |
| `EXACT_AMOUNT_DATE` | A | Exact net, inside the bank tolerance window |
| `SUBSET_SUM_MERGED` | A | One credit covers several settlements; proven by subset-sum |
| `SUBSET_SUM_SPLIT` | A | One settlement arrived in parts; proven by subset-sum |
| `AMOUNT_WIDE_WINDOW` | B | Right amount, outside the tolerance window — needs review |
| `FUZZY_REFERENCE` | B | Reference similarity ≥ 8500 bps — needs review |

### The subset-sum matcher

When one bank credit covers several settlements, or one settlement arrives in
pieces, we solve it exactly — a **bounded subset-sum**, up to 10 candidates and
subsets of at most 4. Not a heuristic, not a model: either a subset sums to the
exact amount or it does not.

**Example.** Three settlements of ₹1,20,000, ₹80,000 and ₹45,000 are pending. The
bank shows one credit of ₹2,00,000. Subset-sum finds `{120000, 80000}` sums
exactly, so those two are matched and `MERGED_BANK_CREDIT` is raised with the
proof attached. The ₹45,000 stays open.

### The guard that matters most

`UTR_SUFFIX` is the dangerous pass. If two settlements in the window end in the
same digits, matching on suffix would confidently pair the wrong ones.

So before selecting, the matcher checks uniqueness. If the suffix is shared, it
**refuses to match on that pass** and falls through. The generator deliberately
plants suffix collisions to test this, and the run report shows the guard holding:

```
guard [held] SET_0089: UTR_SUFFIX uniqueness -> resolved by EXACT_AMOUNT_DATE
guard [held] SET_0039: amount+date ambiguity -> resolved by nothing (tier C)
```

The first one was rescued by a later pass. The second was not, and stayed tier C —
unresolved. **That is the correct outcome.** A wrong auto-match is worse than an
honest "I don't know".

---

## Attribution and the tier gate

A delta of −₹3,247 is not useful on its own. Attribution asks: *can we prove where
that money went?*

For each non-zero delta, the engine tries to build line items from real records —
this refund, that adjustment, this fee at that policy rate — until either the
delta is fully accounted for or nothing is left to try.

**Example.** Settlement `SET_0031` is ₹4,150 short.

```
Δ1 total                                      −₹4,150.00
  refund R_00847, ₹3,500 gross                −₹3,500.00   POLICY.REFUND.window_days@1.0.0
  MDR not returned on that refund (2%)        +   ₹70.00   POLICY.REFUND.mdr_refunded@1.0.0
  GST on the retained fee                     +   ₹12.60   POLICY.TAX.GST_ON_FEE@1.0.0
  chargeback ADJ_0012                         −  ₹732.60   POLICY.ADJUSTMENT@1.0.0
                                              ───────────
  Residual                                          ₹0.00  ✓ fully explained
```

Every line names its record and its rule. The **Explain** panel in the settlement
detail screen renders exactly this.

### The tier gate

Each delta ends in one of three tiers:

| Tier | Meaning | What a human does |
|---|---|---|
| **A** | Auto-resolved on deterministic evidence | Nothing. It's proven. |
| **B** | Needs review | A person confirms before closing |
| **C** | **Unresolved** — the engine refused to guess | Investigate |

**Tier C is a feature, not a failure.** When two bank credits fit a settlement
equally well, the honest answer is "I cannot tell which". Guessing would produce a
number that looks confident and is wrong.

The single most important metric in this project is **false auto-resolutions: 0**.
Not accuracy — that. Precision over recall, always. A wrong auto-match poisons
every other number on the screen.

---

## Exceptions: the honest list

When attribution cannot explain a delta, an exception is raised — with the amount
still unexplained and a plain-English next step. There are **26 exception types**.
A sample of what they say:

| Type | Recommended action |
|---|---|
| `FEE_RATE_MISMATCH` | Raise a fee dispute with the gateway citing the payment id and the policy rate |
| `REFUND_NOT_DEDUCTED` | Confirm the refund reached the customer, then expect it netted next cycle |
| `MISSING_BANK_CREDIT` | Confirm the settlement status; if not ON_HOLD, chase the credit |
| `AMBIGUOUS_BANK_MATCH` | **ESCALATE.** Two or more credits fit equally well. Ask the bank for the UTR |
| `MERGED_BANK_CREDIT` | One credit covers several settlements; the split is proven by subset-sum |
| `DUPLICATE_LEDGER_ENTRY` | Reverse the duplicate posting group; clearing is over-credited |
| `MISSING_LEDGER_ENTRY` | Post the missing settlement entry; money is stranded in clearing |
| `MISPOSTED_ACCOUNT` | Reclassify from SALES to GATEWAY_FEES; revenue is overstated |
| `TRANSFER_MISSING` | Seller is owed money that never moved. Queue the payout |
| `PHANTOM_PAYOUT_GAP` | **ESCALATE.** Seller underpaid with nothing explaining it |
| `UNEXPLAINED_SHORTFALL` | **ESCALATE.** No source record accounts for this |
| `FAILED_PAYMENT_IN_SETTLEMENT` | A FAILED payment was settled. Reverse it and investigate |
| `TIMING_DIFFERENCE` | **No action** — arrived outside the expected day but inside tolerance |

Note that last one. Some exceptions exist to say *"this looked wrong and isn't"* —
which is as useful as flagging a real problem, because it stops someone chasing it.

---

## Ground truth and the honesty metrics

Any tool can claim 97% accuracy. This one can prove it, because the generator
writes down every anomaly it plants before the engine ever sees the data.

Five metrics, all scored against that answer key:

| Metric | What it asks | Demo run |
|---|---|---|
| **Detection** | Of the anomalies that *can* be found, how many did we find? | 50/50 — 100% |
| **Diagnosis** | Did we name the *right* exception type? | 50/50 — 100% |
| **Escalation** | Of the ones that genuinely cannot be diagnosed, how many did we correctly refuse to guess at? | 7/7 — 100% |
| **Traps avoided** | Things that look wrong but are fine — did we resist? | 5/5 — 100% |
| **False auto-resolutions** | Did we ever confidently close something that was actually wrong? | **0** |

Detection alone is easy to game — flag everything and you score 100%. The traps
and false-auto-resolution counters are what make the number mean something.

**Example of a trap.** A refund dated three days after the settlement period
closed. It looks like `REFUND_NOT_DEDUCTED` — money missing from the settlement.
It is not: the refund belongs to the *next* period and will be deducted there. The
closed-tiling period design makes this unambiguous, and the engine has to *not*
flag it. That is `false_positive_traps_avoided`.

### The clean-data gate

```bash
make generate-clean
make reconcile
```

The same generator with the anomaly pass switched off. It must reconcile to
**exactly ₹0** on all four deltas. Not "close to zero" — zero. CI gates on this,
before and after appending a cycle. If the arithmetic drifts by a single paise,
the build fails.

---

## The interface, screen by screen

One HTML file, no build step, no npm, no CDN calls. It runs offline next to the
API. `web/build.sh` copies `web/index.html` → `api/static/index.html`.

> ⚠️ **`api/static/index.html` is generated. Never edit it.** Edit
> `web/index.html` and rebuild. `web/build.sh` refuses to overwrite a diverged
> copy, but check for local edits before rebuilding anyway — a fix living only in
> the generated file is already lost.

### The title screen

The wordmark and one button, white on emerald. Click **Open the dashboard**, or
press Enter or Space.

It's an overlay, not a route — the app boots underneath it, so the dashboard is
already loaded by the time you click through. A reload shows it again, which is
what you want when opening a demo.

### The header

Present on every screen.

| Control | What it does |
|---|---|
| **Demo policy** chip | Hover it: every figure comes from a synthetic policy file, not Razorpay's real terms |
| **Run** dropdown | Every reconciliation run ever made, newest first. Switching reloads every screen |
| **Generate dataset** | Makes a fresh seeded dataset and reconciles it. Replaces the previous seed-42 dataset |
| **Simulate next cycle** | Appends 10 more settlements to the current dataset and re-reconciles everything |
| **Evaluation batch** | Loads the fixed 22-scenario batch and reconciles it |
| **Run reconciliation** | Re-runs the engine on the current dataset. Mints a **new** run — the old one is untouched |

Every one of those buttons has a matching command line and a matching API
endpoint. Nothing in the UI can do something the CLI cannot.

### Dashboard

Opens with **₹ amount at risk** in large type — the single number a finance
controller cares about.

Then the three reconciliation rates side by side, deliberately not blended, plus
the four delta rates underneath. Then a **financial waterfall** for the worst
settlement in the batch:

```
Gross captured  →  Refunds  →  Gateway fees  →  GST on fees  →  Adjustments
   →  Expected net  →  Actual settled  →  Bank credit  →  Unexplained residual
```

Below that, the ground-truth scorecard, the matcher guards, throughput numbers,
exceptions by type, and CSV/JSON export links.

**Scenario.** You open the dashboard and see *₹22,86,062.18 at risk across 12 open
exceptions*, with D3 ledger at 100% but D2 bank at 96%. You now know the problem
is bank credits, not your books, before clicking anything.

### Settlements

Every settlement as a row, with Δ1/Δ2/Δ3/Δ4 chips coloured by tier. Filter by tier
and status.

**Scenario.** Filter to tier C. Three settlements. Those are the ones the engine
refused to guess at — your actual work queue.

### Settlement detail

Click any settlement. You get:

- The **waterfall** for that settlement specifically
- The **Δ1 arithmetic step by step** against policy — every rate cited
- The **Explain** panel: the attribution ledger as line items, ending in the
  residual
- Tabs for payments, refunds, seller payouts, adjustments, bank lines, ledger
  entries, the **matcher trail**, and the audit log

Where an anomaly was planted, the ground-truth note is shown alongside — so you
can check the diagnosis against the truth without leaving the screen.

**Scenario.** `SET_0089` shows Δ2 resolved at tier A. Open the matcher trail:
`UTR_SUFFIX` was tried and *rejected* because the suffix was shared with another
settlement, then `EXACT_AMOUNT_DATE` matched it. You just watched the guard work.

### Exceptions

What is wrong, the amount affected, what was proven, what remains unexplained, and
a recommended action. Filter by status, severity and delta.

### Cash position

Covered in its [own section below](#cash-position).

### Tax credit

Covered in its [own section below](#tax-credit).

### Seller payouts

Per seller: owed versus paid, with the unexplained figure. This is Δ4 at the
seller level rather than the settlement level.

**Scenario.** Every settlement reconciles perfectly, but this screen shows
*Kesar Studio: owed ₹54,397.20, paid ₹0.00*. That's the failure a blended score
would have buried.

### Trace money

Pick any record — a customer, payment, refund, allocation, transfer, settlement,
bank line, ledger entry — and follow the money up and down through the
`money_edges` graph.

It renders as an **expandable timeline, never a node-link diagram**. A graph
picture looks impressive and tells a finance person nothing.

**Scenario.** Trace payment `P_01288`: order it came from, the settlement item it
became, the settlement that rolled up, the four ledger postings, the two seller
allocations, and the transfers that paid them. One screen, whole life of the money.

### Data

Every table in the schema, scoped to the run you are looking at, with live row
counts and pagination.

It's read-only by construction: there is no write path from this tab, and the
table name is validated against the PostgreSQL catalogue server-side before it
ever reaches a query — so a crafted table name cannot become SQL injection.

**Scenario.** Click *Simulate next cycle*, then open Data → `settlements`. The row
count went from 100 to 110 and ten new rows appeared. You are watching the
database change as the system runs.

### Ask the agent

The conversational panel over the run. Covered
[below](#the-investigation-agent).

---

## Cash position

The second half of the track's title — *run the books **and the cash position***.

This page answers one question: **what money is owed, and when does it land?**

It is **derived, not predicted.** There is no model, no trend, no seasonality and
no forecast of future sales — only obligations that already exist under the
policy, dated by the same working-day calendar the engine reconciles against.

### Three sources of incoming and outgoing money

| Bucket | What it is |
|---|---|
| `settlement_awaited` | Credits due from Razorpay on settlements the matcher could not match |
| `pipeline` | Payments captured but not yet itemised into any settlement |
| `seller_payout` | Marketplace allocations still `PENDING` |

### The centrepiece table: "Captured, not yet settled"

Every captured payment, with **three separate dates** — deliberately not collapsed
into one:

| Payment | Method | Captured | Settles | Cash lands | In | Gross | Fee + GST | Net expected |
|---|---|---|---|---|---|---:|---:|---:|
| `PIPE_P008` | CARD_INTL | 2026-03-18 | 2026-03-20 | **2026-03-23** | 5 working days | ₹14,800.00 | −₹523.92 | **₹14,276.08** |

Captured → settles at T+2 working days → cash lands after the bank lag and
tolerance.

**The amount is net, not gross.** The gateway fee and GST never reach your bank,
so showing ₹14,800 as expected cash would overstate your position by ₹524. UPI is
0 bps under this policy, so those rows show no fee at all.

### The other three tables

- **Settled, awaiting bank credit** — with the due date and the rule that set it
- **Seller payouts due** — per allocation, per seller
- **Already overdue** — past its due date, split into bank credits and seller
  payouts, each with how many *working days* late

### Δ2 exceptions read better because of this

An exception that used to say *missing bank credit* now says
**`OVERDUE — due 2026-02-26, 58 working days`** or
**`AWAITED — due 2026-05-21, 3 working days`**. That's the distinction a
controller actually acts on.

It's an *annotation*: the exception taxonomy and tier gate are untouched, because
those are what the accuracy numbers are measured against.

### The mistake this design exists to avoid

The first draft joined settlements to bank lines on the UTR and asked which had no
match. It reported **₹1.2 Cr in flight**. The true figure is **₹3.8 L**.

Twenty of those twenty-one settlements had already landed — their UTRs were
corrupted on purpose, and the matcher had resolved them on `EXACT_AMOUNT_DATE`
instead. **A forecaster that re-derives "unmatched" from the raw records will
confidently report money that is sitting in your bank.**

So the forecaster **reads the matcher's Δ2 verdict** rather than re-deciding
anything.
`tests/test_forecast.py::test_reads_the_matcher_verdict_not_a_naive_utr_join`
asserts the forecast stays strictly below the naive number, so this cannot come
back quietly.

### The chart and the horizon

Grouped inflow/outflow bars plus a running-balance line. **One axis** — never a
second y-scale. A real zero line, because the balance goes negative. Direct labels
on the endpoints only.

The horizon is selectable: **10 / 15 / 30 / 60 working days**. A capture that
settles beyond the default 15-day window is exactly what you widen for.

### Not built: rolling reserve release

Razorpay holds a rolling reserve on some merchant categories and releases it on a
schedule. Modelling it means a reserve balance in the policy registry, a held-back
line per settlement, and a dated release schedule feeding the same curve.
Everything it needs already exists, so it is additive rather than a rework.
Deliberately deferred, not overlooked.

---

## Tax credit

Δ1 proves you were **charged** the right GST. That is not the same as proving you
can **get it back**.

Input tax credit on gateway fees is only recoverable if the supplier filed a tax
invoice that reaches your **GSTR-2B** — the statement the GST portal auto-drafts
each month from what your suppliers themselves filed. A settlement can be flawless
on all four deltas while the credit on its fees is unclaimable, and past the claim
deadline that money is **gone for good**. It is one of the few reconciliation gaps
that is a real, irreversible cash loss.

### Three sources, two comparisons

| | Source | Who is at fault when it disagrees |
|---|---|---|
| **CHARGED** | `settlement_items.tax_paise` | — |
| **BOOKED** | the `INPUT_GST` ledger postings | your own accountant |
| **CLAIMABLE** | `tax_invoices` (GSTR-2B) | the supplier who did not file |

**Two comparisons, not one.** Charged-vs-booked and booked-vs-filed are separate
verdicts on every line and are never added together — different culprits, different
remedies, and one settlement can carry both.

A first draft returned early on the books check, and a settlement with a
*duplicated* `INPUT_GST` posting **and** an invoice filed under the wrong tax heads
reported only the first. The filing defect vanished.
`test_a_books_problem_never_hides_a_filing_problem` pins it now.

### The verdicts

**Filing:** `MATCHED`, `NOT_FILED`, `AMOUNT_MISMATCH`, `SPLIT_MISMATCH` (right
amount, wrong tax heads), `PERIOD_MISMATCH`, `ITC_BLOCKED`.

**Claim state** — what you actually act on:

| State | Meaning |
|---|---|
| `CLAIMABLE` | Nothing to do |
| `DEFERRED` | Real credit, arriving in a later return period |
| `AT_RISK` | Not claimable as things stand — someone must act |
| `BLOCKED` | The portal says it was never creditable. Nothing to chase |

Keeping `BLOCKED` out of "at risk" matters, or someone spends a week chasing money
that was never theirs.

### The five planted findings in the evaluation batch

| Settlement | Finding | What it means |
|---|---|---|
| `EV_07` | **Not filed** | ₹28.80 charged, supplier never filed it. Not claimable |
| `EV_11` | **Amount mismatch** | 3 paise — the supplier rounds per invoice, the settlement per line |
| `EV_15` | **Wrong tax heads** | Filed as IGST when both parties are in state 29. Right amount, will not offset |
| `EV_20` | **Filed late** | March settlement, April GSTR-2B. Deferred by a month, **not lost** |
| `EV_03` | **Not creditable** | Matches on every rupee; the portal marks it ineligible |

**`EV_20` is the trap.** A naive "is it in this month's 2B" check calls it missing
and overstates the loss. It is credit deferred, and a test asserts ₹0 at risk.

### A finding nobody planted

The books leg independently rediscovers three *existing* Δ1/Δ3 anomalies from a
completely different angle: a duplicated ledger group double-posts `INPUT_GST`, a
missing one drops it, and the tax-rounding scenario shows a 2-paise gap. That is
corroboration from real planted data rather than defects invented for this table.

On the seeded generator the same anomalies happen to miss the GST leg (it posts one
group per payment, and they land on groups with no fee), so the demo run
reconciles clean on tax. Stated plainly rather than dressed up.

### How the page is laid out

One block per settlement, with a **coloured left edge** carrying the verdict at a
glance — red at risk, amber deferred, grey blocked, emerald clean. Clean
settlements collapse to a single line; anything with a finding opens expanded.

The three sources appear as three boxes — Charged, Booked, Claimable — which also
aligns them vertically down the page, so you can compare the same figure *across*
settlements, not just within one.

### The explainability (XAI) panel

A floating button at the bottom of the page opens a small chat box over the same
read-only agent, scoped to this page and with its own transcript so it never
disturbs the Ask tab. Citations are chips that jump to the record.

With no API key it says so, and makes the point worth making: **every number on
the page is computed without it — the agent only explains what the matcher already
decided.**

### What this feature cost, stated plainly

The third source does not exist in the world. It is a **synthetic GSTR-2B feed
authored for this project**. That is a weaker claim than the rest of the system
makes, where every record comes from the generator and is accounted for in ground
truth.

So it is labelled synthetic on the page, in the API response, in the agent's
answers, and in the GSTINs themselves — which contain "DEMO" and cannot be mistaken
for real registrations. **Nothing here is tax advice.**

It is also strictly additive: `policy/tax.yaml` is its own registry because
`policy/policy.yaml`'s `config_hash` is stamped on every run ever made and must not
move. `db/tax.sql` is idempotent. The five findings live beside their own data
rather than in `ground_truth_anomalies`, so the four deltas' honesty metrics are
byte-for-byte what they were.

---

## The investigation agent

An optional LLM that reads finished results and explains them in English.

**What it is not:** part of the reconciliation. Every rupee, tier and match rate
was computed deterministically before this module is even imported.

### Why it cannot lie to you about the data

`agent/tools.py` contains **fourteen functions and only `SELECT`s**. There is no
code path from the agent to a write. Three rules hold for every one of them:

1. **No SQL the model wrote reaches the database.** The model picks a tool name
   and a few typed arguments; the SQL is fixed in the file.
2. **Every query is scoped to one `run_id`.** A question about one run cannot read
   another.
3. **Every limit is clamped in the tool**, not trusted from the arguments. Ask for
   a million rows and you get 60.

The fourteen tools:

| Tool | What it reads |
|---|---|
| `run_overview` | The run's four match rates, tiers, throughput, ground-truth scoring |
| `list_settlements` | Settlements ranked by unexplained residual |
| `get_settlement` | Header, settlement-level deltas, a Δ4 summary |
| `get_evidence` | The attribution ledger — every explained rupee and its rule |
| `list_exceptions` | Open items by status, severity, delta |
| `get_matcher_trail` | Every bank-matching pass tried, and why each failed |
| `get_payments` | Payment lines with the fee and tax actually charged |
| `get_ledger` | Double-entry postings and any non-zero clearing balance |
| `get_seller_payouts` | Δ4 per allocation |
| `trace_money` | The lineage graph, up and down |
| `get_policy` | The policy registry the engine computed against |
| `get_audit` | The engine's own decision log |
| `get_cash_forecast` | The forward cash position |
| `get_tax_credit` | Input tax credit: charged, booked and claimable |

### Two guardrails run after every answer

**The tool-call budget is bounded** — 8 assistant turns, 20 tool calls max — so a
confused model stops rather than walking the whole dataset.

**Every record id the answer mentions is checked** against the ids that actually
appeared in tool results. Anything else is shown in the UI as an *unverified
reference* rather than quietly shipped.

That's the failure mode that matters with a language model near financial data:
not a wrong tone, but a confident sentence naming a record it never read.

### Everything is persisted

Every answer goes to `agent_transcripts` beside the engine's own `audit_log`, with
the tools it called and whether its citations held up. A reviewer can hold the
deterministic decision trail and the narrated one side by side.

**Scenario.** You ask *"why is SET_0039 unresolved?"*. The agent calls
`get_settlement`, then `get_matcher_trail`, then `get_evidence`, and answers:
two bank credits of exactly ₹1,20,450 landed on the same day, neither carries a
UTR, and the engine refused to pick — citing `SET_0039:D2` and both bank line ids.
Click a citation chip and you jump straight to that record.

---

## Append mode ("Simulate next cycle")

Real books do not sit still. `make tick` adds the next settlement cycle to an
existing dataset and re-reconciles everything.

```bash
make tick            # 10 more settlements
make tick TICK=25    # 25 more
```

This is harder than "insert more rows", and three things make it correct:

**1. The calendar stays a closed tiling.** The new cycle's period starts exactly
where the last one ended — no gaps, no overlaps. Otherwise "which period does this
refund belong to" would stop having a single answer.

**2. IDs continue, never restart.** The appender reads the high-water mark for
every id sequence. A counter that restarts collides on the second tick — which is
exactly the bug that took down 81 tests when the tax feature first added invoice
numbering, and why serials are now derived from the settlement id.

**3. Late-arriving events cross batch boundaries.** A refund for a payment from
two cycles ago posts as `DR REFUNDS / CR BANK` — deliberately *not* touching
`RAZORPAY_CLEARING`, which would manufacture a false Δ3 against a settlement that
was already correct.

There are also **in-flight bank credits**: a settlement whose credit has not landed
yet is genuinely unmatched this tick and self-heals on the next one. That is what
real books look like.

**Scenario.** Tick once: 100 → 110 settlements, and the newest two show
`MISSING_BANK_CREDIT`. Tick again: those two now match, and the *new* newest two
are unmatched. The system is modelling settlement lag, not producing errors.

The clean-data gate applies here too — `make generate-clean` then two ticks must
still reconcile to exactly ₹0.

---

## Testing

```bash
make test                                    # all 271
python3.12 -m pytest tests/ -q               # same thing
python3.12 -m pytest tests/test_taxmatch.py -v
python3.12 -m pytest tests/ -k "forecast"
python3.12 -m pytest tests/ -q --durations=10
```

**271 tests, all passing.** They need a database — they build real datasets and
run the real engine.

| File | Tests | Covers |
|---|---:|---|
| `test_evaluation_batch.py` | 64 | Every scenario's stated delta, tier and exception, plus the CSV export |
| `test_agent.py` | 43 | Agent tools, scoping, injection refusal, citation guard, storage |
| `test_forecast.py` | 34 | Due dates, roll-up, provenance, line detail, the agent tool |
| `test_taxmatch.py` | 34 | Isolation from the four deltas, both verdicts, the five planted findings |
| `test_golden.py` | 30 | The 19 golden scenarios |
| `test_append.py` | 19 | Tiling, id sequences, late refunds, clean-append zero |
| `test_browse.py` | 14 | The Data tab: catalogue registry, injection, scoping, paging |
| `test_phase0_fixtures.py` | 12 | The 10 hand-worked M01–M10 fixtures |
| `test_calculation.py` | 9 | Calculation properties |
| `test_invariants.py` | 7 | Structural versus business invariants |
| `test_money.py` | 5 | Integer paise primitives |

### The hand-worked fixtures

`tests/golden/manual/` holds ten scenarios (M01–M10) where a human worked out the
expected answer **by hand, with the arithmetic written down in the JSON**:

```json
"derivation": "(123457*200+5000)//10000 = 24696400//10000 = 2469;
               (2469*1800+5000)//10000 = 4449200//10000 = 444"
```

If the engine and the hand-worked answer disagree, one of them is wrong and you
have both to compare.

### CI

`.github/workflows/ci.yml` spins up PostgreSQL, loads the schema, generates both a
dirty and a clean dataset, runs the full suite, and **fails the build if clean data
does not reconcile to exactly zero**.

---

## Project layout

```
policy/policy.yaml       the registry — read by BOTH the generator and the engine
policy/tax.yaml          a SEPARATE registry for the tax matcher, with its own hash

generator/               seeded data generation
  generate.py            builds a whole marketplace from a seed
  anomalies.py           deliberately breaks things, and writes down what it broke
  calendar.py            working days, weekends, 2nd/4th Saturdays, bank holidays
  append.py              adds the next cycle to an existing dataset
  origin.py              id high-water marks so appends never collide

engine/                  15 modules, the deterministic core
  money.py               integer paise, bps(), Indian digit grouping
  policy.py              loads and hashes the registry
  loader.py              pulls a dataset out of Postgres
  invariants.py          structural vs business invariant checks
  calculation.py         Δ1 — recompute from policy
  matcher.py             Δ2 — the eight bank-matching passes
  subset_sum.py          bounded exact subset-sum for merged/split credits
  attribution.py         proving where a delta went
  exceptions.py          the 27 exception types and their recommended actions
  lineage.py             Trace Money — one recursive CTE
  forecast.py            the forward cash position
  taxmatch.py            input tax credit across three sources
  metrics.py             accuracy and the honesty scoring
  runner.py              orchestrates a run
  db.py                  psycopg helpers

fixtures/                the static evaluation batch
  authoring.py           the scenarios, as readable Python data
  loader.py              materialises them into Postgres
  evaluation_batch.json  the authored scenarios + their expected outcomes
  export_csv.py          dumps the batch to flat files
  csv/                   16 CSVs, 1,139 rows

agent/                   the investigation agent
  llm.py                 Gemini/Grok adapter, 167 lines of urllib
  tools.py               14 bounded read-only tools
  investigator.py        the tool-use loop and the citation guard
  store.py               transcript persistence

api/
  main.py                FastAPI app, 29 endpoints
  browse.py              the Data tab's catalogue-derived table browser
  schemas.py             Pydantic models
  static/index.html      the BUILT SPA — generated, never edit

web/
  index.html             the SPA source — edit this one
  build.sh               copies it into api/static, refuses to clobber a diverged copy

db/
  schema.sql             DESTRUCTIVE — drops and recreates everything
  indexes.sql            the indexes
  agent.sql              idempotent — agent_transcripts
  tax.sql                idempotent — tax_invoices

tests/                   271 tests
scripts/reconcile.py     the terminal report
ci/github-workflow-ci.yml
docker-compose.yml       PostgreSQL 16, and nothing else
Makefile / run.sh        two identical entry points
CHANGELOG.md             every change, why it was made, and how it was verified
```

---

## Full API reference

Read-only over persisted results, except the four POSTs. **No model call on any
page render.** Interactive docs at http://localhost:8000/docs.

```
GET  /api/health                                    engine + policy version
GET  /api/policy                                    the whole registry, verbatim

GET  /api/datasets                                  list every dataset
POST /api/datasets                                  generate a new one
GET  /api/datasets/{id}/batches                     the append log, and what is in flight
POST /api/datasets/{id}/tick                        append one cycle + re-reconcile

GET  /api/runs                                      list every run
POST /api/runs                                      reconcile (mints a new run_id)
GET  /api/runs/{id}/metrics                         the full metrics block
GET  /api/runs/{id}/settlements                     table, all four delta chips
GET  /api/runs/{id}/settlements/{sid}               waterfall, Δ arithmetic, Explain,
                                                    payments/refunds/payouts/adjustments/
                                                    bank/ledger/matcher-trail/audit
GET  /api/runs/{id}/exceptions?status=&severity=…   the honest exception list
GET  /api/runs/{id}/exceptions/{eid}/evidence       the attribution behind one exception
GET  /api/runs/{id}/sellers                         per-seller payout reconciliation
GET  /api/runs/{id}/trace?node_type=&node_id=       Trace Money (recursive CTE)
GET  /api/runs/{id}/forecast?horizon=&as_of=        the forward cash position, itemised
GET  /api/runs/{id}/tax                             input tax credit, two verdicts per line
GET  /api/runs/{id}/audit                           every engine decision
GET  /api/runs/{id}/export.csv | export.json        downloadable report

GET  /api/fixtures/evaluation-batch                 what the static batch contains
POST /api/fixtures/evaluation-batch                 load it and reconcile it

GET  /api/runs/{id}/tables                          live row counts, every table
GET  /api/runs/{id}/tables/{table}?limit=&offset=   one page of real rows

GET  /api/agent/status                              is a provider key configured
POST /api/runs/{id}/ask                             ask the investigation agent
GET  /api/runs/{id}/conversation                    the persisted transcript
```

Money crosses the API as **integer paise**. The only place a rupee string appears
is alongside it, for display.

```bash
# Try it
curl -s localhost:8000/api/health | python3 -m json.tool
curl -s localhost:8000/api/runs | python3 -m json.tool | head -20
RUN=$(curl -s localhost:8000/api/runs | python3 -c "import json,sys; print(json.load(sys.stdin)[0]['run_id'])")
curl -s "localhost:8000/api/runs/$RUN/metrics" | python3 -m json.tool
curl -s "localhost:8000/api/runs/$RUN/forecast?horizon=30" | python3 -m json.tool
curl -s "localhost:8000/api/runs/$RUN/tax" | python3 -m json.tool
```

---

## Troubleshooting

**`connection refused` on port 5433**
Postgres isn't up. `make db-up`, or `docker compose ps` to check.

**`relation "agent_transcripts" does not exist"`**
Run `make agent-schema`. It ships separately from the destructive schema.

**`relation "tax_invoices" does not exist"`, or the Tax credit page says it isn't installed**
Run `make tax-schema`, then `make evaluation-batch` to load the feed.

**The Cash position page is empty**
The pipeline rows only exist in datasets built after that feature was added.
Re-run `make generate` or `make evaluation-batch`.

**`make generate` says it's replacing an existing dataset**
That's correct. The `dataset_id` is derived from the seed, so seed 42 always
produces the same id. It deletes the old tree first, and the cascade takes its
runs with it. Use a different `SEED=` to keep both.

**`python3.12: command not found`**
`make test PYTHON=python3`, or point it wherever your 3.12 lives.

**The UI shows an old version after I edited it**
You edited `api/static/index.html`, which is generated. Edit `web/index.html` and
run `make web`.

**The agent panel says "not configured"**
No API key set. `export GEMINI_API_KEY=…` and restart. Everything else works
without it.

**A page 404s after generating a new dataset**
The UI was still on a deleted run. It should move you automatically — if it
doesn't, pick a run from the dropdown. Datasets cascade, so a stale `run_id` 404s
every subsequent call.

---

## What this deliberately does not do

**No LLM in the reconciliation path.** If you find yourself wanting one, a
deterministic rule is missing. Write the rule.

**No guessing.** Ambiguity always blocks auto-resolution. Tier C is a real
outcome.

**No blended accuracy number.** Four deltas, four rates, reported separately
forever.

**No node-link graph for Trace Money.** It looks impressive and tells a finance
person nothing. A timeline table is more useful.

**No rolling reserve modelling.** Deferred, documented, additive when wanted.

**No real tax filing data.** The GSTR-2B feed is synthetic and says so everywhere
it surfaces.

### Standing invariants — do not break these

- Money is integer paise everywhere. No floats, no `Decimal` in the money path.
- The four deltas are reported separately and never blended.
- Runs are immutable. A re-run mints a new `run_id`; nothing is ever mutated.
- The agent reads. It never computes and never writes.
- Clean data must reconcile to **exactly zero** — CI gates on it, before and after
  appending.
- The forecaster reads the matcher's verdict. It must never re-derive "unmatched".
- The evaluation batch is append-only. Settled rows are frozen.
- `policy/policy.yaml`'s `config_hash` is `8e2326ce0e4335ea` and must not move. A
  new feature that needs configuration gets its **own** registry file.
- New tables ship as idempotent SQL, never by re-running `schema.sql`.
- `ground_truth_anomalies` is for the four deltas only.
- **`api/static/index.html` is generated.** Edit `web/index.html` and rebuild.

---

## Three lines for the pitch

> Exact-key matching is deterministic code. We never ask a model whether two
> numbers are equal.

> We planted seven discrepancies that are genuinely undiagnosable. The system
> escalated all seven. It reports what it cannot explain, in rupees.

> A settlement can reconcile perfectly while a seller is still underpaid — those
> are different questions, so we answer them separately instead of folding seller
> payouts into one blended score.

---

*Every fee, tax, settlement-cycle, bank-lag and GST figure in this project comes
from a synthetic Demo Merchant Policy authored for this hackathon. It is not a
claim about how Razorpay actually operates, and nothing here is tax or financial
advice.*
