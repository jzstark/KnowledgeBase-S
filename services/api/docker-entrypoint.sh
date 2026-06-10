#!/bin/sh
set -e

# Schema is owned by Alembic. Only the api service sets RUN_MIGRATIONS=1, so it
# is the single migrator; workers share this image but skip migrations and rely
# on depends_on(api: healthy) for an up-to-date schema.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head
fi

exec "$@"
