#!/bin/bash
set -euo pipefail

usage() {
    echo "Usage: $0 [--oauth]"
    echo "  default: deploy core services, workers, and OAuth MCP"
    echo "  --oauth: accepted for compatibility; same as default"
}

profiles=(--profile workers --profile oauth)
case "${1:-}" in
    ""|--oauth) ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
esac

echo "==> Pulling configured images..."
docker compose "${profiles[@]}" pull

echo "==> Starting services..."
# Do not use --remove-orphans here: services behind an inactive optional
# profile (notably kb-mcp-oauth) must survive a default deployment.
docker compose "${profiles[@]}" up -d

echo "==> Done. Services running:"
docker compose ps
