# CI

`github-workflow-ci.yml` belongs at `.github/workflows/ci.yml`:

```bash
mkdir -p .github/workflows
cp ci/github-workflow-ci.yml .github/workflows/ci.yml
```

It spins up PostgreSQL 16, loads the schema, runs the 63 tests, and then runs
the phase-4 gate separately: generate a dataset with **no** anomalies and assert
every delta is exactly zero. If the generator and the engine ever disagree on
data with nothing wrong in it, no accuracy number downstream can be trusted.
