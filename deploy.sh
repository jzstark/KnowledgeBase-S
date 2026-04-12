#!/bin/bash
set -e

echo "==> Pulling latest images..."
docker compose pull

echo "==> Starting services..."
docker compose up -d --remove-orphans

echo "==> Cleaning up old images..."
docker image prune -f

echo "==> Done. Services running:"
docker compose ps
