# Project log

Running record of what this project is, what has changed, and why. **Every change
made from here on gets an entry.** If you are picking this up cold — a teammate,
an evaluator, or a future session — read "Current state" first, then the newest
log entry.

Newest entries at the top.

---

## How to keep this file

One entry per change, with four things: **what changed**, **why**, **which files**,
and **how it was verified**. A change that is not verified is not finished, and an
entry that does not say why will be useless in a week.

If a change alters a number quoted anywhere (test count, record count, accuracy),
update the "Current state" block too — a log that disagrees with the code is worse
than no log.

---

## Current state

**What it is.** A deterministic settlement reconciliation engine. It recomputes
what a settlement *should* have paid from a versioned policy registry, compares
that against what the report claims, what the bank actually credited, what the
merchant's own books say, and what each seller was paid — four independent axes,
reported separately and never blended. Plus a language-model agent that explains
the results without being able to change them.

**Stack.** Python 3.12, FastAPI, PostgreSQL 16, vanilla-JS single-file SPA.
Six dependencies: `psycopg`, `fastapi`, `uvicorn`, `PyYAML`, `pydantic`, `pytest`.
No ORM, no npm, no vendor AI SDK. All money is `BIGINT` paise — never floats.

**Tests: 201, all passing.**

| file | n | covers |
|---|---:|---|
| `test_evaluation_batch.py` | 64 | the static batch: every scenario's stated delta, tier and exception, plus the CSV export |
| `test_agent.py` | 41 | agent tools, scoping, injection refusal, citation guard, storage |
| `test_golden.py` | 30 | the 19 golden scenarios |
| `test_append.py` | 19 | append mode: tiling, id sequences, late refunds, clean-append zero |
| `test_browse.py` | 14 | the Data tab: catalogue registry, injection, scoping, paging |
| `test_phase0_fixtures.py` | 12 | the 10 hand-worked M01–M10 fixtures |
| `test_calculation.py` | 9 | calculation properties |
| `test_invariants.py` | 7 | structural vs business invariants |
| `test_money.py` | 5 | integer paise primitives |

**Local environment (this machine).** Postgres on **port 5433** (5432 was taken),
interpreter **python3.12**. `Makefile` uses `PYTHON ?= python3.12`; override with
`make <target> PYTHON=python3` elsewhere.

**Two files must be created by hand** — the Claude file bridge refuses to write
them because both execute code:

```bash
mv Makefile.txt Makefile
mkdir -p .github/workflows && cp ci/github-workflow-ci.yml .github/workflows/ci.yml
```

Until the second one exists, **CI does not run on pushes** — GitHub only looks in
`.github/workflows/`.

**Commands.**

```bash
make db-up && make schema && make generate   # first-time setup
make serve                                   # http://localhost:8000
make tick                                    # +10 settlements, re-reconcile
make evaluation-batch                        # load the fixed 22-scenario batch
make evaluation-csv                          # export it to fixtures/csv/
make agent-schema                            # add the agent table to a LIVE db
make db-summary / make db-shell              # inspect the database
make test                                    # 193 tests
```

**Layout.**

```
engine/       13 modules — the deterministic reconciliation engine (unchanged this session)
generator/    seeded dataset generation + append mode (origin.py, append.py are new)
fixtures/     the static evaluation batch: authoring.py, loader.py, evaluation_batch.json
              plus export_csv.py and csv/ — the same batch as 15 flat files
agent/        the investigation agent: llm.py, tools.py, investigator.py, store.py
api/          FastAPI app + browse.py (Data tab) + the single-file SPA in static/
db/           schema.sql (destructive), indexes.sql, agent.sql (idempotent)
tests/        193 tests
```

**UI tabs.** Dashboard · Settlements · Settlement detail · Exceptions · Seller
payouts · Trace money · **Data** · **Ask the agent**

**UI copy policy.** Screens state what they are and show numbers — no
explanatory paragraphs, no commentary columns, no metric labels written as
sentences. The one deliberate exception is the `Demo policy` chip in the header:
these are not Razorpay's real terms, so that notice stays.

**Header buttons.** Generate dataset · Simulate next cycle · Evaluation batch ·
Run reconciliation

---

## 2026-09-03 — Gave the evaluation batch a real population

**The problem.** One customer behind all 35 payments, two sellers, and seller
money moving on only 2 of 22 settlements. It reconciled perfectly and looked
nothing like a book anyone kept. The single customer was a placeholder inherited
from the Phase-0 fixture loader, where the point was arithmetic, not people —
and it was hardcoded in `loader.py` rather than being data.

**What changed.**

- **26 named customers**, assigned per order, spread across the batch. The
  busiest has four payments; the retried order keeps one customer across all
  three attempts, because a person does not change between retries.
- **6 sellers spanning all three commission tiers** the policy defines —
  INDIVIDUAL at 1500 bps was previously absent entirely, so a bug in that tier
  could not have shown up here. One seller is SUSPENDED, which a real roster
  always has.
- **Orders have their own identity** (`ORD_0001…`) instead of being derived from
  the payment id.
- **25 allocations across 16 settlements**, all paid correctly. Δ₄ previously had
  two data points and both were defects; these are the controls that make EV18
  and EV19 mean something.

**Files.** `fixtures/authoring.py`, `fixtures/loader.py`,
`fixtures/evaluation_batch.json`, `fixtures/csv/*`,
`tests/test_evaluation_batch.py`.

**Verified.** All **22 scenarios still match their stated deltas, tiers and
exception types** — the added realism changed no expected outcome — and the
honesty metrics are unchanged at 100% detection, 100% diagnosis, 100%
escalation, 3/3 traps, 0 false auto-resolutions. Five new tests pin the
population so it cannot regress to a placeholder: a minimum customer count,
one customer per retried order, every commission tier represented, seller money
moving on at least half the settlements, and allocations never exceeding their
payment. **201 tests passing** (was 196).

**Record count is now 345** (was 301), still one dataset, still no randomness.

---

## 2026-09-03 — CSV export of the evaluation batch

**What.** `fixtures/csv/` — the static batch as 15 flat files, one per table,
**992 rows** covering the 345 financial records. `make evaluation-csv` or
`./run.sh evaluation-csv` regenerates them.

**Why.** So the batch can be read without a database: opened in Excel, diffed in
git, or loaded into another system by an evaluator who wants to check the engine
against their own arithmetic.

**How it is produced.** Dumped from the database *after* the batch is loaded, so
the files are the same rows the engine reconciles — not a second rendering of the
JSON that could drift from it. Loading is idempotent, so the export is safe to
run at any time.

**`scenarios.csv` is the entry point** — one row per settlement with its family,
what was done to it, and the expected `d1..d4` deltas, worst tier and exception
types. Everything else is the data those expectations are about.

**Two deliberate choices.** Money stays in **integer paise**; converting to
rupees would put a second representation of the same number in the file and
invite someone to reconcile against the rounded one. And `dataset_id` is dropped
— the same constant on every row, carrying no information in a flat file.

**Files.** New: `fixtures/export_csv.py`, `fixtures/csv/` (15 CSVs +
`README.md`). Changed: `Makefile.txt`, `run.sh`, `tests/test_evaluation_batch.py`.

**Verified.** Every file parses as well-formed CSV with uniform column counts.
Three new tests: the export matches the batch and the database row-for-row, the
constant `dataset_id` is absent, and every `*_paise` value is still an integer —
a decimal point anywhere would mean something converted to rupees on the way out.
**196 tests passing** (was 193).

---

## 2026-09-03 — Fixed: Generate left the UI on a deleted run (regression I caused)

**The bug.** Clicking **Generate dataset** created the dataset but never
reconciled and never changed the selected run. Because `dataset_id` is derived
from the seed, generating *replaces* that seed's dataset — and datasets cascade,
so every run against it is deleted with it. The UI stayed pointed at a `run_id`
that no longer existed, and every following call (`metrics`, `conversation`,
`ask`, `tables`) returned **404**.

**How I made it worse.** Sanjesh had already fixed this by hand. The fix was in
`api/static/index.html` — which is **generated** by `web/build.sh` from
`web/index.html`. My UI cleanup edited the source and rebuilt, silently
overwriting his change. Worse, it was doomed anyway: `run.sh serve` builds before
starting, so the next restart would have destroyed it regardless.

**The fix, in two parts.**

1. The auto-reconcile now lives in `web/index.html`, the source, so it survives
   every build. Generate now reconciles and moves the UI onto the new run — the
   same thing the tick and evaluation-batch buttons already did. Merged into one
   toast and refreshes the Data tab counts, for consistency with those two.
2. `web/build.sh` **refuses to overwrite a generated file that has diverged from
   its source**, printing the diff and how to proceed (`FORCE=1` to discard).
   Loud beats silent: this failure mode is now impossible to repeat without
   someone reading exactly what is about to be lost.

**Files.** `web/index.html`, `web/build.sh`, `api/static/index.html`. No engine,
API, generator, agent or fixture change.

**Verified.** Clicked Generate in Chromium and walked every tab: the run
transitions from the old one to the new, **zero HTTP 4xx**, no console errors.
The build guard was proven against the exact failure — it refuses, shows the
diff, and `FORCE=1` still works. 193 tests passing.

---

## 2026-09-03 — UI cleanup

**What.** Removed the explanatory prose from the interface. Every screen now
states what it is and shows the numbers; nothing argues its own case.

**Why.** The UI read like documentation — full-width disclaimer banner, a
paragraph under the waterfall, commentary columns in the ground-truth table,
"did the money actually arrive" as a metric label. Fine in a README, wrong in a
finance tool someone uses.

**What went.**

- header tagline "deterministic settlement reconciliation · P0"
- the full-width **Demo Merchant Policy** banner → now a small `Demo policy`
  chip in the header, full statement on hover
- the "demo policy" labels repeated under four waterfall bars, and the three
  inline policy badges in the settlements, detail and seller tables
- the waterfall footnote, the throughput headline sentence, the five commentary
  notes in the ground-truth table
- the Data tab and Ask-the-agent intro paragraphs (the suggested-question chips
  stay — they are usable, not explanatory)
- editorialising card subtitles: "worst unexplained residual in this batch",
  "a settlement can reconcile perfectly while a seller is still short",
  "one recursive walk over the lineage table", "what we planted"

**What was tightened rather than removed.** Metric labels are now nouns:
"did the money actually arrive" → "Settlement vs bank credit"; "do the merchant's
books agree" → "Double-entry integrity". The hero line is `Across N open
exceptions`. Empty states are one short sentence each.

**One judgment call.** The policy disclaimer was **kept**, in reduced form. These
are not Razorpay's real commercial terms and the screen is full of rates — that
notice is an honesty safeguard, not decoration. It is now a header chip with the
full text on hover rather than a banner across every page. Say the word if you
want it gone entirely.

**Also fixed.** `recommended_action` is engine prose sitting in a dense figures
table; it was running off the right edge. Now a single truncated line with the
full text on hover, so every row is a uniform 58px and the column edge is
straight. The full text is still in the settlement detail view.

**Files.** `web/index.html`, `api/static/index.html`. **Nothing else touched** —
no engine, API, generator, agent or fixture change.

**Verified.** Every tab rendered in Chromium with no console errors; longest line
of copy on the dashboard went from 222 characters to 43. 193 tests still passing.
The SPA is 2.9 KB smaller.

---

## 2026-09-02 — Static evaluation batch

**What.** A fixed, hand-authored batch of **22 settlements / 345 financial
records** with every expected outcome written down beside the data. Constant
`dataset_id`, so loading twice replaces rather than accumulates. Reachable from
the **Evaluation batch** header button, `POST /api/fixtures/evaluation-batch`, or
`make evaluation-batch`.

**Why.** The seeded generator is reproducible but large. An evaluator needs
something small enough to read end to end, and a fixed point to compare engine
versions against. No seed, no sampling, no clock.

**Coverage.** 3 clean controls · 5 Δ₁ · 5 Δ₂ · 3 Δ₃ · 2 Δ₄ · **4 false-positive
traps**. Includes the cases specifically asked for: a retried order with two
FAILED attempts, refunds, an unitemised chargeback, a duplicated ledger posting,
and two genuinely identical same-day payments that must **not** be flagged.

**Score against it: 100% detection, 100% diagnosis, 100% correct escalation,
3/3 traps avoided, 0 false auto-resolutions.**

**Four things the first draft got wrong** — each worth remembering:

1. `refund_outside_period` is a **trap**, not a defect. A refund dated after one
   period closes belongs to the *next* settlement, where it is correctly itemised.
   The right engine behaviour is silence on both.
2. `ALLOCATION_TRANSFER_DIVERGENCE` only resolves when a REVERSED transfer of
   exactly the missing amount exists. Without that evidence the engine is right to
   call it an unexplained `PHANTOM_PAYOUT_GAP` and refuse to resolve.
3. A bulk credit covering two settlements only resolves when their settlement
   dates fall inside `POLICY.MATCH.date_window_days` (3). Mine were 4 apart.
4. A trap is `is_resolvable=True` with `expected_exception_type="NONE"`.
   `is_resolvable=False` means something different — "undiagnosable, must escalate
   to tier C" — and confusing the two makes the honesty score measure the wrong
   thing.

**Files.** New: `fixtures/{__init__,authoring,loader}.py`,
`fixtures/evaluation_batch.json`, `tests/test_evaluation_batch.py`. Changed:
`api/main.py`, `web/index.html`, `api/static/index.html`, `run.sh`,
`Makefile.txt`, `README.md`.

**Verified.** 22/22 scenarios match their stated deltas, tiers and exception
types against a real run. 56 new tests; 193 total passing. A test pins the batch
to the policy's `config_hash`, so changing `policy.yaml` fails loudly rather than
letting the two silently disagree.

---

## 2026-09-02 — Data tab

**What.** A read-only browser over all 22 tables, scoped to the selected run,
with live row counts. Grouped into *Generated data* (the book the engine reads)
and *Engine results* (what it concluded). Money shows as rupees with a **raw
paise** toggle. Also `./run.sh db-summary` and `./run.sh db-shell`.

**Why.** Asked twice how to see the data as it is created. Counts update the
moment you hit Simulate — verified 21 → 31 settlements without a manual refresh.

**Safety.** The table registry is derived from PostgreSQL's own catalogue, not a
hand-written list, so it cannot drift when the schema changes. A table name is
checked against that registry before reaching any SQL string —
`settlements; DROP TABLE payments` returns "unknown table". Page sizes clamped
server-side. `api/browse.py` contains only `SELECT`s, asserted by a test that
greps the module source.

**Files.** New: `api/browse.py`, `tests/test_browse.py`. Changed: `api/main.py`,
`web/index.html`, `api/static/index.html`, `README.md`.

**Verified.** 14 new tests; 137 total at the time. Rendered in Chromium, no
console errors.

---

## 2026-09-02 — Fixed: `agent_transcripts` missing on existing installs

**What.** `GET /api/runs/{id}/conversation` returned **500** on any database that
predated the agent. New idempotent migration `db/agent.sql`; `agent/store.py`
applies it on demand; the endpoint degrades to an empty conversation instead of
failing. New `make agent-schema`.

**Why — this was my bug, in two parts.** The table existed only in `schema.sql`,
which begins with `DROP TABLE` on everything — so the obvious fix would have
destroyed the dataset and every run. And the SPA calls that endpoint on every run
selection, so a problem in the agent's own storage was throwing errors into a
reconciliation screen. An additive feature must never be able to do either.

**Files.** New: `db/agent.sql`. Changed: `agent/store.py`, `api/main.py`,
`db/schema.sql`, `tests/test_agent.py`, `run.sh`, `Makefile.txt`.

**Verified.** Dropped the table with 5 runs present, restarted, hit the endpoint:
200, table created, 5 runs intact, zero tracebacks. 4 new tests.

---

## 2026-09-02 — Fixed: `make generate` crashed on a repeated seed

**What.** Regenerating an existing seed from the CLI failed with a primary-key
violation. Now it replaces the dataset and says what it is replacing.

**Why.** `dataset_id` is derived from the seed. The API's Generate button always
deleted first; `main()` in the generator never did. Pre-existing bug, not from
this session's work — but it fires on the most ordinary action there is.

**Files.** `generator/generate.py`. Also added `db-shell` / `db-summary` targets.

---

## 2026-09-01 — Investigation agent

**What.** A language model with **12 bounded read-only tools** over one finished
run, answering questions in English. New **Ask the agent** tab, `POST
/api/runs/{id}/ask`, transcripts persisted to `agent_transcripts`.

**Why.** The hackathon track is called *AI Finance Controller* and its brief opens
"Build an agent" — the project had zero LLM calls. The track's own example
directions list "Settlement Q&A agent" second.

**The design decision that matters.** The engine still computes everything
deterministically; the agent only reads persisted results. It cannot change a
number, a tier, or a status, because `agent/tools.py` contains only `SELECT`s —
asserted by a test, not assumed. The pitch is stronger for it: *the numbers are
proved, not generated, and the agent explains them.*

**Provider.** Gemini or Grok, through one adapter over the OpenAI-compatible
chat-completions format, written against the standard library. **No new
dependencies.** Set `GEMINI_API_KEY` or `XAI_API_KEY`; with neither, the panel
explains what is missing and every other screen is unaffected.

**Two guardrails.** The tool-call budget is bounded, so a confused model stops
rather than walking the dataset. And every record id in an answer is checked
against ids that actually appeared in tool results — anything else renders as a
red **Unverified reference**. That is the failure mode that matters with a model
near financial data: not a wrong tone, but a confident sentence naming a record it
never read.

**Two problems found while building.** `get_settlement` returned 47 Δ₄ rows that
were all zero, burying the actual residue — now summarised. And the citation guard
produced a false positive when `get_evidence` returned nothing for a real
settlement, so tools now confirm the subject exists and echo it back.

**Files.** New: `agent/{__init__,llm,tools,investigator,store}.py`,
`tests/test_agent.py`. Changed: `api/main.py`, `web/index.html`,
`api/static/index.html`, `db/schema.sql`, `.env.example`, `README.md`.

**Verified.** 37 new tests, all passing with **no API key and no network** — the
model is stubbed, because the agent is untested exactly where it matters
otherwise. Covers scoping across two runs sharing settlement ids, limit clamping,
unknown-tool refusal, malformed arguments, budget exhaustion, and the citation
guard catching a fabricated id.

---

## 2026-09-01 — Append mode ("Simulate next cycle")

**What.** The dataset grows. `make tick`, `POST /api/datasets/{id}/tick`, and a
**Simulate next cycle** button append a new settlement cycle to the *same* dataset
and re-reconcile everything into a new immutable run.

**Why.** The system only handled a fixed snapshot. In reality payments keep
arriving, refunds turn up weeks later, and the bank credits yesterday's settlement
tomorrow.

**What continues across a tick.** The calendar (resuming the day after the last
period ended, so periods still tile with no gaps); every id sequence (re-derived
from the data's own high-water mark, not a stored counter); the seller population;
and ground truth, so detection rate is scored over the whole grown dataset.

**Two things that make a tick worth watching.** Refunds arrive **late** — against
payments settled cycles ago, netted off the current settlement. That is the Δ₂
timing case the engine always handled but never saw. They post `DR REFUNDS / CR
BANK` and deliberately never touch `RAZORPAY_CLEARING`, because that payment's
clearing balance was closed by its own settlement and crediting it again would
manufacture a Δ₃ imbalance on a settlement nobody touched. And the last
settlement of a cycle has **no bank credit yet** — it reports as an open Δ₂
exception and the *next* tick closes it. An exception that heals itself is the
clearest proof that a run is a picture of a moment, not a permanent verdict.

**Scaling.** Linear: 500 settlements / 146k records reconcile in 3.11s. Appending
costs ~0.35s per cycle, 0.24s of which is re-deriving id high-water marks from the
data — the safe choice over trusting a counter.

**Also fixed a latent bug** in the existing generator: a settlement whose refunds
swallow the net was emitting a negative `credit_paise`, violating a DDL check. It
never fired at 100 settlements but does on smaller batches.

**Files.** New: `generator/origin.py`, `generator/append.py`,
`tests/test_append.py`. Changed: `generator/generate.py`, `generator/anomalies.py`,
`api/main.py`, `web/index.html`, `run.sh`, `Makefile.txt`, `ci/…`,
`tests/test_invariants.py`, `README.md`.

**Verified.** 19 new tests. Across 130 settlements and four batches: calendar
tiling closed at every seam, zero duplicate ids on all 11 sequences, refund
headroom intact, all entry groups balanced, 101/101 detection with 100% diagnosis
and zero false auto-resolutions. CI gained a gate: two clean ticks must still
reconcile to exactly zero — twice, because one append can pass on a counter that
never advanced.

---

## Decisions and open items

**Deferred — forward cash forecaster.** Discussed and deliberately postponed. It
would derive a day-by-day cash curve from the working-day calendar and policy the
project already has: when each settlement lands, what is owed out to sellers
(169 PENDING allocations ≈ ₹26.8 L in one run), and what rolling reserve is held.
It would also upgrade Δ₂ from "missing" to **"due Thursday" vs "overdue by three
days"**, which is the distinction a controller actually cares about. This is the
strongest remaining addition — it completes the track's own subtitle, *"Run the
books and the cash position"*.

**Not planned — tax-line matcher.** The other track example. It would reconcile
GST input credit across three sources: what the settlement charged per line, what
`INPUT_GST` holds, and what the gateway's GSTR-2B tax invoice claims. The third
source does not exist in the generator, so it would mean inventing a data source
before the feature means anything — and Δ₁ already proves most of the arithmetic.

**Supabase.** Considered and declined. Nothing in the project needs it; local
Postgres keeps the benchmark honest and removes a demo-day network dependency.
If judges ever need a live URL it is a small port (~20 lines: a connection pool
and `prepare_threshold=None` for the transaction pooler).

**Standing invariants — do not break these.**

- Money is integer paise everywhere. No floats, no `Decimal` in the money path.
- The four deltas are reported separately and never blended into one number.
- Runs are immutable. A re-run mints a new `run_id`; nothing is ever mutated.
- The agent reads. It never computes and never writes.
- Clean data must reconcile to **exactly zero** — CI gates on it, before and
  after appending.
- **`api/static/index.html` is generated.** Never edit it; edit `web/index.html`
  and rebuild. `web/build.sh` now refuses to clobber a diverged copy, but check
  for local edits before rebuilding regardless — a fix living only in the
  generated file is already lost.
- Any action that **replaces a dataset must move the UI onto a new run.**
  Datasets cascade; leaving the UI on a deleted `run_id` 404s every subsequent
  call. Generate, tick and evaluation-batch all do this now.
