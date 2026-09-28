#!/bin/sh
set -e

# Schema is owned by Alembic. Only the api service sets RUN_MIGRATIONS=1, so it
# is the single migrator; workers share this image but skip migrations and rely
# on depends_on(api: healthy) for an up-to-date schema.
if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] alembic upgrade head"
  alembic upgrade head

  # Retired resource-manager documents; schema.md is still a separate setting.
  user_config_dir="${USER_DATA_DIR:-/app/user_data}/default/config"
  rm -f "$user_config_dir/topics.md" "$user_config_dir/templates/公众号新闻.md"
  rmdir "$user_config_dir/templates" 2>/dev/null || true
fi

exec "$@"
