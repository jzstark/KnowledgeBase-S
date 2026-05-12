#!/bin/bash
set -e

DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="./backups"
mkdir -p "$BACKUP_DIR"

echo "==> Backing up user_data..."
tar -czf "${BACKUP_DIR}/user_data-${DATE}.tar.gz" ./user_data/

if [ -d ./data/wechat2rss ]; then
    echo "==> Backing up wechat2rss data..."
    tar -czf "${BACKUP_DIR}/wechat2rss-${DATE}.tar.gz" ./data/wechat2rss/
fi

echo "==> Backing up database..."
docker compose exec -T postgres pg_dump -U postgres app > "${BACKUP_DIR}/db-${DATE}.sql"

echo "==> Backup complete: ${BACKUP_DIR}/"
ls -lh "${BACKUP_DIR}/"

# 如已配置 rclone，上传到 R2
if command -v rclone &> /dev/null && rclone listremotes | grep -q "r2:"; then
    echo "==> Uploading to R2..."
    rclone copy "${BACKUP_DIR}/user_data-${DATE}.tar.gz" r2:bucket/backups/
    if [ -f "${BACKUP_DIR}/wechat2rss-${DATE}.tar.gz" ]; then
        rclone copy "${BACKUP_DIR}/wechat2rss-${DATE}.tar.gz" r2:bucket/backups/
    fi
    rclone copy "${BACKUP_DIR}/db-${DATE}.sql" r2:bucket/backups/
    rclone delete --min-age 30d r2:bucket/backups/
    echo "==> Upload complete."
fi
