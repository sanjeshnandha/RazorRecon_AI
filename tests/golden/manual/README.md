# Phase 0 — hand-worked fixtures

Every figure in these files was computed **by hand from `/policy/policy.yaml`**,
before the generator or the engine existed. They are entered literally, not
produced by running any code in this repo.

They exist to catch the single most dangerous failure mode in a project like
this: the generator and the reconciliation engine encoding the *same*
misunderstanding of the policy (e.g. both computing GST on gross instead of on
the fee), so the demo reports "100% match rate" while both halves are wrong in
the same way. A fixture derived from the policy document with no code involved
is independent of both.

Each `derivation` string shows the arithmetic. You should be able to point at
any number in any of these files and justify it from `policy.yaml` alone.

`tests/test_phase0_fixtures.py` asserts:
  1. `bps()` reproduces every fee/tax/commission figure below, and
  2. the calculation engine, run over these fixtures loaded into Postgres,
     produces exactly the stated delta, attributions, residual and tier.

Gate: if (2) disagrees with these files, the *engine* is wrong, not the file.
