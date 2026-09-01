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
reconcile() { python3.12 -m scripts.reconcile; }
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
  tick)           tick ;;
  test)           tests ;;
  web)            web ;;
  serve)          serve ;;
  demo)           demo ;;
  *)  grep -E '^\s+(install|db-up|db-down|schema|generate|generate-clean|reconcile|tick|test|web|serve|demo)\)' "$0" \
        | sed 's/).*//' | sed 's/^/  ./run.sh /' ;;
esac
