#!/bin/bash
set -e

if [ -z "$1" ] || [ -z "$2" ]; then
    echo "Usage: $0 <user_data_backup.tar.gz> <db_backup.sql>"
    exit 1
fi

USER_DATA_BACKUP="$1"
DB_BACKUP="$2"

echo "==> Restoring user_data from ${USER_DATA_BACKUP}..."
tar -xzf "$USER_DATA_BACKUP" -C ./

echo "==> Restoring database from ${DB_BACKUP}..."
docker compose exec -T postgres psql -U postgres -d app < "$DB_BACKUP"

echo "==> Restore complete."
