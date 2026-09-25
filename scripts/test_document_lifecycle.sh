#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

container_id=""
if [[ -z "${TEST_DATABASE_URL:-}" ]]; then
  container_id="$(docker run --rm -d -e POSTGRES_PASSWORD=test -e POSTGRES_DB=kb_lifecycle_test -p 127.0.0.1::5432 pgvector/pgvector:pg16)"
  trap 'docker stop "$container_id" >/dev/null' EXIT
  port="$(docker port "$container_id" 5432/tcp | sed -n 's/.*://p')"
  export TEST_DATABASE_URL="postgresql://postgres:test@127.0.0.1:${port}/kb_lifecycle_test"
  for attempt in {1..30}; do
    if docker exec "$container_id" pg_isready -U postgres -d kb_lifecycle_test >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

if [[ "$TEST_DATABASE_URL" != */kb_lifecycle_test ]]; then
  echo "Refusing to run lifecycle tests outside the kb_lifecycle_test database" >&2
  exit 2
fi

export DATABASE_URL="$TEST_DATABASE_URL"
export AUTH_PASSWORD="${AUTH_PASSWORD:-test-password}"
export AUTH_SECRET="${AUTH_SECRET:-test-secret}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-test-key}"
export ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-test-key}"

(cd services/api && alembic upgrade head)
PYTHONPATH="$PWD/services/api${PYTHONPATH:+:$PYTHONPATH}" python3 -m pytest \
  services/api/tests/test_document_lifecycle_postgres.py \
  services/api/tests/test_document_intake_postgres.py -q
