#!/bin/bash
set -euo pipefail

usage() {
    echo "Usage: $0 [--oauth]"
    echo "  default: deploy core services and workers"
    echo "  --oauth: also deploy the kb-mcp-oauth profile"
}

profiles=(--profile workers)
case "${1:-}" in
    "") ;;
    --oauth) profiles+=(--profile oauth) ;;
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
