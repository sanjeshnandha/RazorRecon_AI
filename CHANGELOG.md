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

**Name.** **Razor Recon AI**. The internal module, package and database are still
`finctl` — an identifier, not the product name.

**What it is.** A deterministic settlement reconciliation engine. It recomputes
what a settlement *should* have paid from a versioned policy registry, compares
that against what the report claims, what the bank actually credited, what the
merchant's own books say, and what each seller was paid — four independent axes,
reported separately and never blended. Plus a language-model agent that explains
the results without being able to change them.

**Stack.** Python 3.12, FastAPI, PostgreSQL 16, vanilla-JS single-file SPA.
Six dependencies: `psycopg`, `fastapi`, `uvicorn`, `PyYAML`, `pydantic`, `pytest`.
No ORM, no npm, no vendor AI SDK. All money is `BIGINT` paise — never floats.

**Tests: 271, all passing.**

| file | n | covers |
|---|---:|---|
| `test_evaluation_batch.py` | 64 | the static batch: every scenario's stated delta, tier and exception, plus the CSV export |
| `test_agent.py` | 41 | agent tools, scoping, injection refusal, citation guard, storage |
| `test_forecast.py` | 34 | the cash forecaster: due dates, roll-up, provenance, line detail, the agent tool |
| `test_taxmatch.py` | 34 | input tax credit: isolation from the four deltas, the two verdicts, the five planted findings |
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
make tax-schema                              # add the GSTR-2B table to a LIVE db
make db-summary / make db-shell              # inspect the database
make test                                    # 271 tests
```

**Layout.**

```
engine/       15 modules — the deterministic reconciliation engine (+ forecast.py, taxmatch.py)
generator/    seeded dataset generation + append mode (origin.py, append.py are new)
fixtures/     the static evaluation batch: authoring.py, loader.py, evaluation_batch.json
              plus export_csv.py and csv/ — the same batch as 15 flat files
agent/        the investigation agent: llm.py, tools.py, investigator.py, store.py
api/          FastAPI app + browse.py (Data tab) + the single-file SPA in static/
db/           schema.sql (destructive), indexes.sql, agent.sql + tax.sql (idempotent)
tests/        271 tests
```

**UI tabs.** Dashboard · Settlements · Settlement detail · Exceptions ·
Cash position · **Tax credit** · Seller payouts · Trace money · Data · Ask the agent

**Dashboard cards.** Run summary · Δ waterfall · Ground-truth scoring ·
Matcher guards · Exceptions by type

**UI copy policy.** Screens state what they are and show numbers — no
explanatory paragraphs, no commentary columns, no metric labels written as
sentences. The one deliberate exception is the `Demo policy` chip in the header:
these are not Razorpay's real terms, so that notice stays.

**Header buttons.** Generate dataset · Simulate next cycle · Evaluation batch ·
Run reconciliation

---

## 2026-09-04 — Tax credit page: commentary removed

**What changed.** The page explained itself in prose. It now states what it is and
shows numbers, per the standing UI copy policy.

**Removed.**

- The two footnote paragraphs under *Charged vs booked* and *Booked vs filed* —
  three lines each of generic explanation, identical on every run.
- The *How this was matched* card: four bulleted assumptions plus a paragraph on
  why the forecast is derived. Replaced by one provenance line — registry version
  and hash, the two GSTINs, place of supply — and the synthetic-feed notice, which
  stays because it is an honesty commitment, not commentary.
- Explanatory sub-lines: *"a claim is filed per period, not per settlement"*,
  *"findings open, clean lines collapsed"*.
- The XAI panel's two-sentence footer, now `Read-only · scoped to input tax credit`.

**Shortened, in `engine/taxmatch.py`.** The findings carried advice that repeated
on every row of a kind — *"Chase the supplier before the claim deadline"*, *"The
supplier must amend"*, *"This is not an error to fix"*. They now state the finding
and stop:

| before | after |
|---|---|
| No GSTR-2B line for this settlement. ₹28.80 of GST was charged but the supplier has not filed it, so none of it can be claimed. Chase the supplier before the claim deadline. | ₹28.80 charged, nothing filed. Not claimable. |
| Charged, booked and filed all agree, under the right heads, in the right return period. Fully claimable. | Charged, booked and filed agree. Fully claimable. |

Evidence labels were a legend printed on every block, so they identify the source
rather than describing it: *"charged on the settlement report"* → `settlement
report`, *"posted to INPUT_GST in the merchant's books"* → `INPUT_GST postings`,
*"filed in 2026-03 under IGST"* → `2026-03 · IGST`.

**What was deliberately kept.** The `synthetic feed` chip, the disclaimer line,
and the short identifying subtitles (*the merchant's own posting*, *what GSTR-2B
will allow*) — four words that name a comparison are a label, not a paragraph.
The reasoning behind the rules lives in the README, which is where someone goes
to ask *why*; the screen answers *what*.

**Files.** `engine/taxmatch.py` (finding and evidence-label strings),
`web/index.html` → `api/static/index.html` rebuilt.

**How it was verified.** Rendered in Chromium: console clean, zero horizontal
overflow, page 4,059px tall against roughly 4,700 before. 271 tests passing —
the tests assert findings are non-empty, never their wording, so shortening them
is safe by construction.

---

## 2026-09-04 — Tax credit page: one settlement, one block

**The problem.** The settlement list was a table where each settlement occupied a
data row *and* a detail row. Nothing marked where one settlement ended and the
next began, so 22 of them ran together into a wall — the finding for `EV_15` sat
directly against the header row for `EV_11` with the same weight and the same
background. It was congested to the point of being unreadable.

**The fix — stop using a table.** Each settlement is now a discrete block:

- a **coloured left edge** carrying the verdict at a glance (red at risk, amber
  deferred or books-disagree, grey blocked, emerald clean);
- a **header strip** — id, date, verdict chips, then invoice / filed-in / GST
  charged / at-risk right-aligned with their own labels, so each row is
  self-describing without a table header;
- a **body on a tinted ground**, separated by a rule, with the findings in a
  label/text grid so `FILING` and `BOOKS` align rather than running on;
- the three sources as **three boxes** — Charged, Booked, Claimable — which also
  makes them align vertically *down* the page, so the same figure can be compared
  across settlements, not just within one;
- 14px between blocks. The separation is the whole point.

**Findings open, clean lines collapsed.** A settlement with nothing wrong shows
only its header strip — one scannable line. Anything with a finding opens
expanded, because that is what someone came to read. Per-block toggle plus an
expand/collapse-all in the card header; the state resets when the run changes.

**Files.** `web/index.html` (`.tx-*` styles, the block renderer replacing the
table, `S.tx.open`) → `api/static/index.html` rebuilt.

**How it was verified.** Rendered in Chromium on the evaluation batch: 22 blocks,
7 open (5 filing findings + 2 books-only), zero horizontal overflow, console
clean. 271 tests passing.

---

## 2026-09-04 — Title screen

**What changed.** The app opens on a title screen: the **Razor Recon AI** wordmark
and one button, *Open the dashboard*. Nothing else — no tagline, no feature chips,
no mark.

**Inverted from the rest of the app, deliberately.** Every other screen is emerald
on white; the opener is white on emerald, so it reads as a cover rather than as a
page that failed to load its content. Emerald radial (`--em-500` → `--em-700`),
white title, white button with emerald text.

**How it behaves.** An overlay above the header, not a route. `boot()` runs
underneath it, so the dashboard is fully loaded by the time anyone clicks through
— the screen costs nothing in time to first data. Enter or Space anywhere
dismisses it too, the button is autofocused, and focus moves to the first tab on
exit.

**State is in-memory only, on purpose.** A reload shows it again, which is what you
want when opening a demo — nothing to clear between runs, and no browser storage
to go stale. It is not a setting.

**Two small things worth noting.** The exit animation is dismissed on
`animationend` *and* a 400 ms timeout, because `prefers-reduced-motion` suppresses
the animation and the event never fires — without the fallback the overlay would
stick forever for anyone who has that set. And Chromium's default focus ring is
invisible on a white button, so `:focus-visible` draws it as a white halo against
the surrounding green instead.

**Note on the caveat.** The Demo Merchant Policy notice is not on this screen. It
is still on every screen behind it, as the header chip — the opener carries no
figures, so there is nothing there for it to qualify.

**Files.** `web/index.html` (`#splash` markup, its CSS, `enterApp()`) →
`api/static/index.html` rebuilt.

**How it was verified.** Rendered in Chromium: the screen shows, the nine tabs are
already built behind it, the button removes it, the dashboard is visible, zero
horizontal overflow, console clean. 271 tests passing.

---

## 2026-09-04 — Renamed to Razor Recon AI

**What changed.** The project is called **Razor Recon AI**. Every place the
product named itself now says so:

| file | where |
|---|---|
| `README.md` | the H1 |
| `web/index.html` | `<title>` and the header wordmark (`api/static/index.html` rebuilt) |
| `api/main.py` | the FastAPI app title, and the CSV export header row |
| `scripts/reconcile.py` | the terminal report banner |
| `db/schema.sql` | the file header comment |

**Two occurrences deliberately left alone.**

- `CHANGELOG.md` — *"The hackathon track is called AI Finance Controller"*. That
  is the name of the **track**, not of this project. Rewriting it would make the
  log say the track was named after us.
- `agent/investigator.py` — *"explain what the engine found … to a finance
  controller"*. That is the **human role** the agent is writing for, not a
  product name. Renaming it would tell the model to address the software.

The module, package and database are still `finctl` — an internal identifier,
not the product name, and changing it would rewrite import paths, the connection
string and every prior run's provenance for no user-visible gain.

**How it was verified.** 271 tests passing. Terminal banner and the rebuilt SPA
header both render the new name; a repo-wide grep for the old one returns only
the two intentional occurrences above.

---

## 2026-09-04 — Tax-line matcher, with an explainability panel

**What changed.** The track's other example direction, built. Δ₁ already proved
the merchant was *charged* the right GST. This answers the different question:
**can any of it actually be claimed back.** Input tax credit is only recoverable
if the supplier filed a tax invoice that reaches the merchant's GSTR-2B, and past
the claim deadline it is gone for good — one of the few reconciliation gaps that
is a real, irreversible cash loss.

**Three sources, two comparisons — and the second is the whole design.**

| | source | who is at fault when it disagrees |
|---|---|---|
| CHARGED | `settlement_items.tax_paise` | — |
| BOOKED | the `INPUT_GST` ledger postings | the merchant's own accountant |
| CLAIMABLE | `tax_invoices` (GSTR-2B) | the supplier who did not file |

Charged-vs-booked and booked-vs-filed are **two independent verdicts on every
line**, never added together. My first draft returned early on the books check —
and EV_15, which has *both* a duplicated INPUT_GST posting and an invoice filed
under the wrong tax heads, silently reported only the first. The filing defect
vanished. `test_a_books_problem_never_hides_a_filing_problem` pins it.

**Verdicts.** Filing: `MATCHED`, `NOT_FILED`, `AMOUNT_MISMATCH`, `SPLIT_MISMATCH`
(right amount, wrong heads — it will not offset), `PERIOD_MISMATCH`, `ITC_BLOCKED`.
Claim state, which is what a controller acts on: `CLAIMABLE`, `DEFERRED` (real
credit, later return period), `AT_RISK`, `BLOCKED` (never claimable — nothing to
chase). Keeping `BLOCKED` out of "at risk" matters: otherwise someone spends a
week chasing a supplier about money that was never theirs.

**The trap.** EV_20 is a March settlement whose invoice lands in the April
GSTR-2B. A naive "is it in this month's 2B" check calls that missing and
overstates the loss. It is credit **deferred by a month, not lost** — `DEFERRED`,
₹0 at risk, and a test asserts exactly that.

**Nothing existing was modified.** New table (`tax_invoices`), new registry
(`policy/tax.yaml`), new page, new tool. Not one existing row or column changed:

- **`policy/tax.yaml` is deliberately separate from `policy/policy.yaml`.** The
  core registry's `config_hash` is stamped on every run ever made; adding tax keys
  there would silently invalidate all of them. `test_core_config_hash_is_unchanged`
  asserts the literal value `8e2326ce0e4335ea` still holds.
- **`db/tax.sql` is idempotent**, like `db/agent.sql` — installs onto a live
  database. `schema.sql` is destructive and was not touched. Missing table ⇒ the
  endpoint and page degrade with instructions rather than 500.
- **Ground truth untouched.** The five tax findings live in
  `fixtures/authoring.py::TAX_EXPECTATIONS`, *not* in `ground_truth_anomalies` —
  that table feeds the honesty metrics and the "19 planted anomalies" the
  dashboard reports. A test asserts `planted_total == 19` and that all four
  honesty metrics are still perfect.

**The data — small and realistic.** One gateway tax invoice per settlement, which
is how a gateway actually bills. 21 invoices for the 22 settlements (one was never
filed), across two return periods. Five planted findings, one per real failure
mode: not filed (EV_07), per-invoice vs per-line rounding — 3 paise (EV_11), wrong
tax heads (EV_15), filed a period late (EV_20), portal-blocked credit (EV_03).
Records stay at **395**; the CSV export gains one file, 1,139 rows.

**A finding I did not plant.** The books leg independently rediscovers three
*existing* D1/D3 anomalies from a completely different angle — a duplicated ledger
group double-posts INPUT_GST, a missing one drops it, and the tax-rounding
scenario shows a 2-paise gap. That is corroboration from real planted data, not
defects invented for this table, and it is the best evidence the check is doing
something. On the seeded generator the same anomalies happen to miss the GST leg
(it posts one group per payment, and they land on groups with no fee), so the demo
run reconciles clean on tax — stated plainly rather than dressed up.

**The page.** A `Tax credit` tab: headline tiles, the two comparisons side by side
with the totals belonging to each, a per-return-period table (a claim is filed per
period, not per settlement), and every settlement with both verdicts, worst first,
each expanding into its findings and the three evidence rows behind it.

**The XAI panel.** A floating button at the bottom of that page opens a small chat
box over the same read-only agent, with its own transcript so it never disturbs
the Ask tab. It gets `get_tax_credit` (14 tools now), citation chips that jump to
the record, the unverified-reference warning, and the same honest degradation when
no API key is set — with the point stated in the panel: *every number on this page
is computed without it; the agent only explains what the matcher already decided.*

**Bug found and fixed while testing.** `copy_entities` is shared between a fresh
generation and an append. My first invoice numbering used a running counter, which
restarts at 1 on every tick and collided on the second one — taking the whole
append down with a unique-key violation, and 81 unrelated tests with it. Serials
are derived from the settlement id now, and existing ones are skipped.
`test_invoice_numbers_survive_an_append` covers it.

**Files.**

| file | change |
|---|---|
| `policy/tax.yaml` | **new** — separate versioned registry, own hash |
| `db/tax.sql` | **new** — idempotent `tax_invoices` + constraints and indexes |
| `engine/taxmatch.py` | **new** — the three-source match, two independent verdicts |
| `fixtures/authoring.py` | **added** `TAX_INVOICES` + `TAX_EXPECTATIONS`; nothing existing touched |
| `fixtures/loader.py` | `_load_tax_invoices`, skipped when the table is absent |
| `fixtures/export_csv.py` | exports `tax_invoices.csv` |
| `generator/generate.py` | `_copy_tax_invoices`, serials derived from the settlement id |
| `api/main.py` | `GET /api/runs/{run_id}/tax` |
| `agent/tools.py` | `get_tax_credit` handler + schema |
| `agent/investigator.py` | INPUT TAX CREDIT section in the system prompt |
| `web/index.html` | `viewTax()`, `xaiPanel()`, `askXai()`, the panel CSS, `tax` in `TABS` |
| `Makefile.txt`, `run.sh` | `make tax-schema` |
| `tests/test_taxmatch.py` | **new** — 34 tests |

**How it was verified.** **271 tests passing** (was 236). Rendered in Chromium on
the evaluation batch: console clean, zero page overflow, the panel opens and
degrades correctly. The batch still scores 22/22 scenarios, 14/14 detection,
14/14 diagnosis, 2/2 escalation, 3/3 traps, 0 false auto-resolutions, 395 records.

**Standing caveat.** The GSTR-2B feed is authored for this project. It is not real
filing data, nothing here is tax advice, and the GSTINs are placeholders
containing "DEMO" so they cannot be mistaken for real registrations. The page, the
API and the agent all say so.

---

## 2026-09-04 — Cash position moved to its own page, and itemised

**What changed.** The forecast was a card on the dashboard showing four totals
and a chart. Four totals tell you *how much*; they do not tell you *which
records*. It is now its own tab — **Cash position** — where every rupee in those
totals is a row you can read, with the record it came from and the date logic
that put it there.

**The question the page answers.** *"What have we already taken that hasn't
reached the bank yet, and when does each rupee land?"* That is the
**Captured, not yet settled** table, and it is the centrepiece:

| Payment | Method | Captured | Settles | Cash lands | In | Gross | Fee + GST | Net expected |
|---|---|---|---|---|---|---:|---:|---:|
| `PIPE_P008` | CARD_INTL | 2026-03-18 | 2026-03-20 | **2026-03-23** | 5 working days | ₹14,800.00 | −₹523.92 | **₹14,276.08** |

Three dates, deliberately not collapsed into one: when the payment was captured,
when it settles (T+2 working days), and when the cash actually lands (settlement
plus the bank lag and tolerance). **Net, not gross** — the gateway fee and GST
computed from the policy registry never reach the bank, so showing gross as
expected cash would overstate the position. UPI is 0 bps under this policy, so
those rows show no fee rather than a row of `−₹0.00`.

**Four tables, in the order a controller would ask for them.**

1. **Captured, not yet settled** — money taken, still in Razorpay's hands.
2. **Settled, awaiting bank credit** — settled, matcher found no bank line, credit
   due on a stated date. Each row links through to the settlement.
3. **Seller payouts due** — what goes out, per allocation, per seller.
4. **Already overdue** — past its due date, split into bank credits and seller
   payouts, each with how many working days late.

Then **How these dates were derived** — the assumptions rendered from the
engine's own `assumptions` list, so the page states its reasoning rather than
asking to be trusted.

**A horizon control.** 10 / 15 / 30 / 60 working days, re-querying the endpoint.
A capture that settles beyond the default 15-day window is exactly the thing you
want to widen for, and it was previously invisible.

**Structured detail on every line.** `Line` gained a `detail` dict, so the UI
builds columns from typed fields instead of parsing the `basis` sentence. Shapes:
pipeline carries `capture_date` / `settles_on` / `credit_due` / `method` /
`gross_paise` / `fee_paise` / `working_days_until_credit`; awaited carries
`settlement_date` / `due_date` / `working_days_until_due`; payouts carry
`seller_id` / `seller_name` / `capture_date` / `due_date`. All JSON-safe scalars —
a `date` object here would 500 the endpoint, so a test asserts it.

**Chart fix found while testing.** At a 60-working-day horizon the SVG was
squashing every bar into a hairline: it was `width:100%` inside an `overflow-x:auto`
wrapper, so it shrank to fit rather than overflowing. Now it carries
`min-width:${W}px` as well — fills a wide card, scrolls when the horizon is long.
Verified the page body itself still has **zero** horizontal overflow.

**Files.**

| file | change |
|---|---|
| `engine/forecast.py` | `Line.detail`, populated for all three buckets |
| `web/index.html` | `viewCash()` — the page; `reloadForecast()`, `fcTable()`, `fcStat()`, `wd()`; `viewForecastCard()` removed from the dashboard; `cash` added to `TABS`; `.btn.on` style; the chart `min-width` fix |
| `tests/test_forecast.py` | +6 tests on `detail` — date ordering, fee against the registry, the zero-rated case, JSON safety |

**How it was verified.** **236 tests passing** (was 230). Rendered in Chromium on
both the seeded demo run and the evaluation batch, at 15 and 60 working days:
console clean, no page overflow, no label collisions. On the evaluation batch the
page shows the 13 pipeline captures individually — ₹73,527.46 net expected — plus
2 settlements awaiting credit and 11 payouts due.

---

## 2026-09-04 — Forward cash forecaster (phases 1–3)

**What changed.** The project now answers the second half of its own track title
— *"run the books **and the cash position**"*. A new deterministic forecaster
derives, from the working-day calendar and the policy registry, a dated schedule
of money already owed: credits still due from Razorpay, payments captured but not
yet itemised into a settlement, and seller payouts still PENDING. It ships as an
engine module, an API endpoint, a dashboard chart, and an agent tool.

It is **derived, never predicted.** There is no model, no trend, no seasonality
and no forecast of future sales — only obligations that already exist under the
policy, dated by the same calendar the engine reconciles against.

**The one design rule that matters.** The forecaster reads the matcher's Δ₂
verdict rather than re-deriving "unmatched" with its own SQL. My first draft
joined settlements to bank lines on the UTR and reported **₹1.2 Cr in flight**.
The true figure is **₹3.8 L** — 20 of those 21 settlements had already landed and
were matched on `EXACT_AMOUNT_DATE` because their UTR was corrupted on purpose.
A forecaster that re-decides what the matcher already decided will confidently
report money that is sitting in the bank. `tests/test_forecast.py::
test_reads_the_matcher_verdict_not_a_naive_utr_join` asserts the forecast stays
strictly below the naive number, so this cannot silently regress.

**Phase 1 — due dates.** `credit_due_date()` = settlement date + `expected_lag_days`
+ `bank_tolerance_days`, counted in working days. Δ₂ exceptions in the API are now
**annotated** with `AWAITED (due 2026-05-21, 3 working days)` or
`OVERDUE (due 2026-02-26, 58 working days)`. Annotated, not reclassified: the
engine's exception taxonomy and tiers are untouched, because the tier gate is what
the accuracy numbers are measured against.

**Phase 2 — the cash curve.** `_roll_up()` buckets every line into working days
with a running balance. Anything already past its due date drops out of the window
into `overdue` / `overdue_payouts` rather than being drawn as if it were coming.

**Phase 3 — pipeline data, strictly additive.** Per the standing condition on the
evaluation batch: **no existing row was modified.** Every settled record in the
22-scenario batch is byte-for-byte what it was. What was added is a tail of
captured-but-unsettled trading after the last period closed —

- **Evaluation batch:** 13 captures over 4 trading days (2026-03-16 → 03-19),
  11 PENDING allocations, spread across the real customer and seller population
  with mixed methods and amounts. Records **345 → 395**.
- **Seeded generator:** the same idea at scale — 45 pipeline payments, 204 PENDING
  allocations on the demo dataset.

Capture-only ledger postings (DR `RAZORPAY_CLEARING` / CR `SALES`, no settlement
group) so the pipeline cannot manufacture a false Δ₃.

**Verified — the batch is unchanged.** After the pipeline rows, the batch still
scores **22/22 scenarios identical**, detection **14/14**, diagnosis **14/14**,
escalation **2/2**, traps avoided **3/3**, false auto-resolutions **0**. The demo
run is likewise undisturbed: **92.00%** match rate, **50/50** detection, **5/5**
traps, **0** false auto-resolutions.

**What it reports** (demo run, as-of 2026-05-17, next 15 working days):

| | |
|---|---:|
| Expected in | ₹3,30,030.76 |
| Expected out | ₹2,92,029.92 |
| Net over the window | ₹38,000.84 |
| Already overdue | ₹48,22,306.31 (4 credits, 165 payouts) |

The as-of date is **the book's own present** — the day after the last settlement
period closed — not wall-clock time, which is meaningless against a dataset dated
2026.

**The chart.** Grouped inflow/outflow bars plus a running-balance line, built to
the `dataviz` rules: **one axis** (never a second y-scale), nice round gridline
ticks (`₹2 L / ₹1 L / ₹0 / −₹1 L / −₹2 L`), a real zero line because the balance
goes negative, 2px surface gaps between paired bars, direct labels on the two
endpoints only, a legend, and a per-day hover target wider than the marks.
Palette validated with the skill's own script: `#059669` / `#3b6fd4` pass all six
checks (CVD ΔE **21.1**, well above the 8 target). Rendered in Chromium and
eyeballed — no label collisions, no overflow, console clean.

**The agent tool.** `get_cash_forecast` joins the read-only tool set (13 tools
now). Same three guarantees as the rest: no model-written SQL, scoped to one
`run_id`, every limit clamped server-side. Its returned line ids (`A_03334`,
`P_01288`) satisfy the existing citation guard, so a claim about the cash position
is verifiable the same way a claim about a delta is. The system prompt gained a
CASH POSITION section instructing the model to present it as a derived schedule
and never as a projection of revenue.

**Files.**

| file | change |
|---|---|
| `engine/forecast.py` | **new** — due dates, the roll-up, `build()` / `to_dict()` |
| `fixtures/authoring.py` | **added** `PIPELINE` + `pipeline_day()`; nothing existing touched |
| `fixtures/loader.py` | materialise pipeline rows with capture-only ledger postings |
| `generator/generate.py` | pipeline tail after the settlement items; `ds.pipeline_from` |
| `api/main.py` | `GET /api/runs/{run_id}/forecast`; Δ₂ exceptions annotated with credit state |
| `web/index.html` | `forecastChart()`, `fcLegend()`, `viewForecastCard()`, `niceStep()`, `axisMoney()` |
| `agent/tools.py` | `get_cash_forecast` handler + schema |
| `agent/investigator.py` | CASH POSITION section in the system prompt |
| `tests/test_forecast.py` | **new** — 28 tests |
| `fixtures/csv/` | re-exported: 1,118 rows across 15 files (was 1,043) |

**How it was verified.** `tests/test_forecast.py` — 28 tests covering due-date
arithmetic on the working-day calendar, the running balance as a cumulative sum,
totals equal to the sum of the days, buckets partitioning the window, integer
paise everywhere, every line citing a record *and* a policy rule, overdue items
never appearing inside the window, the matcher-verdict rule above, provenance of
each bucket against the database, determinism across two builds, a digest proving
the forecaster writes nothing, and the agent tool's clamping, bucket filter and
run scoping. **Full suite: 230 passing.** Chart rendered in Chromium and
inspected; palette re-validated.

**Deferred — phase 4, rolling reserve release.** Explicitly postponed at the
user's request. Razorpay holds a rolling reserve on some merchant categories and
releases it on a fixed schedule; modelling it would mean adding a reserve balance
to the policy registry, a held-back line on each settlement, and a dated release
schedule feeding the same cash curve. Everything it needs is already in place —
the calendar, the policy registry, the line/bucket shape of `Forecast` — so it is
an additive change, not a rework. Not started.

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

**Built — forward cash forecaster (phases 1–3).** See the 2026-09-04 entry.
`engine/forecast.py`, `GET /api/runs/{run_id}/forecast`, the **Cash position**
tab, and the `get_cash_forecast` agent tool. Δ₂ now reads *"due 2026-05-21"* vs
*"overdue by 58 working days"* rather than just "missing".

**Deferred — phase 4, rolling reserve release.** The one piece of the forecaster
deliberately left out. It would add a reserve balance to the policy registry, a
held-back line on each settlement, and a dated release schedule feeding the same
cash curve. Additive to the existing `Forecast` line/bucket shape — no rework.
Not started.

**Built — tax-line matcher.** See the 2026-09-04 entry. `engine/taxmatch.py`,
`policy/tax.yaml`, `db/tax.sql`, `GET /api/runs/{run_id}/tax`, the **Tax credit**
tab with its explainability panel, and the `get_tax_credit` agent tool. The third
source (GSTR-2B) is synthetic and labelled as such everywhere it surfaces — that
is the honest cost of the feature and it is stated rather than hidden.

**Supabase.** Considered and declined. Nothing in the project needs it; local
Postgres keeps the benchmark honest and removes a demo-day network dependency.
If judges ever need a live URL it is a small port (~20 lines: a connection pool
and `prepare_threshold=None` for the transaction pooler).

**Standing invariants — do not break these.**

- Money is integer paise everywhere. No floats, no `Decimal` in the money path.
- The four deltas are reported separately and never blended into one number.
- Runs are immutable. A re-run mints a new `run_id`; nothing is ever mutated.
- The agent reads. It never computes and never writes.
- **The forecaster reads the matcher's verdict.** It must never re-derive
  "unmatched" from the raw records — that mistake reported ₹1.2 Cr in flight when
  the true figure was ₹3.8 L. A test pins it.
- **The evaluation batch is append-only.** Rows already marked settled are frozen;
  new scenarios and pipeline rows are added beside them, never over them.
- **`policy/policy.yaml`'s `config_hash` is `8e2326ce0e4335ea` and must not move.**
  It is stamped on every run ever made. A new feature that needs configuration
  gets its OWN registry file, as `policy/tax.yaml` does.
- **New tables ship as idempotent SQL** (`db/agent.sql`, `db/tax.sql`), never by
  re-running the destructive `schema.sql`, and every reader degrades when the
  table is absent.
- **`ground_truth_anomalies` is for the four deltas only.** It feeds the honesty
  metrics; a new feature's expectations live beside its own data and are scored
  by its own tests.
- Clean data must reconcile to **exactly zero** — CI gates on it, before and
  after appending.
- **`api/static/index.html` is generated.** Never edit it; edit `web/index.html`
  and rebuild. `web/build.sh` now refuses to clobber a diverged copy, but check
  for local edits before rebuilding regardless — a fix living only in the
  generated file is already lost.
- Any action that **replaces a dataset must move the UI onto a new run.**
  Datasets cascade; leaving the UI on a deleted `run_id` 404s every subsequent
  call. Generate, tick and evaluation-batch all do this now.
