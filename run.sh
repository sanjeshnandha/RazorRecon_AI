#!/usr/bin/env bash
# Same targets as the Makefile, for anyone who would rather type ./run.sh
#
#   ./run.sh demo          postgres + schema + seeded dataset + one run
#   ./run.sh serve         API + UI on http://localhost:8000
#   ./run.sh test          the full suite, including the 19 golden scenarios
#   ./run.sh tick          append one settlement cycle and re-reconcile
#
set -euo pipefail
cd "$(dirname "$0")"

# Load environment variables (API keys)
if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

export DATABASE_URL="${DATABASE_URL:-postgresql://finctl:finctl@localhost:5433/finctl}"
SEED="${SEED:-42}"
SETTLEMENTS="${SETTLEMENTS:-100}"
LABEL="${LABEL:-demo}"
PORT="${PORT:-8000}"

install()   { pip install -r requirements.txt; }
db_up()     { docker compose up -d
              echo "waiting for postgres…"
              until docker compose exec -T db pg_isready -U finctl -d finctl >/dev/null 2>&1
                do sleep 1; done
              echo "ready."; }
db_down()   { docker compose down; }
schema()    { psql "$DATABASE_URL" -q -f db/schema.sql
              psql "$DATABASE_URL" -q -f db/indexes.sql
              echo "schema loaded."; }
generate()  { python3.12 -m generator.generate --seed "$SEED" --settlements "$SETTLEMENTS" \
                --label "$LABEL"; }
gen_clean() { python3.12 -m generator.generate --seed "$SEED" --settlements "$SETTLEMENTS" \
                --label clean --clean; }
db_shell()  { psql "$DATABASE_URL"; }
db_summary(){ psql "$DATABASE_URL" -c "SELECT d.label, left(d.dataset_id::text,8) AS dataset, d.seed,
   (d.row_counts->>'settlements')::int AS settlements,
   (d.row_counts->>'total_financial_records')::int AS records,
   jsonb_array_length(COALESCE(d.row_counts->'batches','[]')) AS cycles,
   (SELECT count(*) FROM reconciliation_runs r WHERE r.dataset_id=d.dataset_id) AS runs
 FROM datasets d WHERE (d.row_counts->>'settlements')::int > 1 ORDER BY d.generated_at DESC;"; }
agent_schema(){ psql "$DATABASE_URL" -q -f db/agent.sql
              echo "agent_transcripts ready."; }
tax_schema(){ psql "$DATABASE_URL" -q -f db/tax.sql
              echo "tax_invoices ready."; }
reconcile() { python3.12 -m scripts.reconcile; }
evalbatch() { python3.12 -m fixtures.loader; }
evalcsv()   { python3.12 -m fixtures.export_csv; }
tick()      { python3.12 -m generator.append --settlements "${TICK:-10}" \
                ${DATASET:+--dataset "$DATASET"}; }
tests()     { python3.12 -m pytest tests/ -q; }
web()       { ./web/build.sh; }
serve()     { web; uvicorn api.main:app --host 0.0.0.0 --port "$PORT" --reload; }
demo()      { db_up; schema; generate; reconcile
              echo; echo "  now run:  ./run.sh serve   ->  http://localhost:$PORT"; }

case "${1:-help}" in
  install)        install ;;
  db-up)          db_up ;;
  db-down)        db_down ;;
  schema)         schema ;;
  generate)       generate ;;
  generate-clean) gen_clean ;;
  reconcile)      reconcile ;;
  agent-schema)   agent_schema ;;
  tax-schema)     tax_schema ;;
  db-shell)       db_shell ;;
  db-summary)     db_summary ;;
  tick)           tick ;;
  evaluation-batch) evalbatch ;;
  evaluation-csv) evalcsv ;;
  test)           tests ;;
  web)            web ;;
  serve)          serve ;;
  demo)           demo ;;
  *)  grep -E '^\s+(install|db-up|db-down|db-shell|db-summary|schema|agent-schema|tax-schema|generate|generate-clean|reconcile|tick|evaluation-batch|evaluation-csv|test|web|serve|demo)\)' "$0" \
        | sed 's/).*//' | sed 's/^/  ./run.sh /' ;;
esac
