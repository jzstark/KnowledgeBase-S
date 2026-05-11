# Refactor Progress

This file tracks implementation progress for `docs/refactor-plan.md`.

When later implementation errors are found and fixed, record them here with:

- date
- affected phase
- symptom
- root cause
- fix
- verification

## 2026-05-11 - Phase 0 Baseline and Constraints

Status: implemented.

Assumptions:

- Phase 0 should not change runtime behavior.
- Baseline artifacts should be useful before and after larger refactors.
- Smoke checks should use the existing running API and avoid new test
  dependencies.

Implemented:

- Added API route baseline: `docs/baseline/api-routes.md`.
- Added DB schema baseline: `docs/baseline/db-schema.md`.
- Added stdlib smoke test script: `scripts/refactor_smoke.py`.
- Added Make target: `make refactor-smoke`.

Verification scope:

- `GET /api/health`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `GET /api/sources`
- `GET /api/kb/nodes?limit=1`
- `GET /api/briefing`
- `GET /api/drafts`

How to run:

```sh
AUTH_PASSWORD=... make refactor-smoke
```

If auth is intentionally unavailable, the unauthenticated subset can be run
with:

```sh
python scripts/refactor_smoke.py --skip-auth
```

Notes:

- `git status --short` worked in this workspace, so no Git `safe.directory`
  fix was needed here.
- Phase 0 records that `source_items`, `jobs`, and object-specific node tables
  do not exist yet.
- The baseline records that `knowledge_edges` currently lacks a uniqueness
  constraint, so duplicate cleanup is required before adding one.
- Live verification on this workspace:
  - `python scripts/refactor_smoke.py --skip-auth` passed for health, sources,
    KB nodes, and briefing.
  - Auth-only coverage was checked inside the API container with its existing
    `AUTH_PASSWORD`; login and `GET /api/drafts` passed.
  - The API container was initially unhealthy because a reload attempted DB
    connection while Postgres was starting (`CannotConnectNowError`). Restarting
    only the API container after Postgres was healthy fixed the environment.

Implementation fixes:

- Symptom: the first local smoke attempt raised a raw `ConnectionResetError`
  instead of printing a normal failure line.
- Root cause: `scripts/refactor_smoke.py` only caught `urllib.error.URLError`,
  but socket-level reset errors can surface as `OSError`.
- Fix: catch `OSError` around the smoke run and report it as `api connection`
  failure.
- Verification: `python -m py_compile scripts/refactor_smoke.py` passes.

## 2026-05-11 - Phase 1 Known Drift Cleanup

Status: implemented.

Assumptions:

- Phase 1 should remove known dead shells and stale prompts without starting
  the larger Knowledge Core split.
- The future `knowledge_edges` uniqueness key is
  `(from_node_id, to_node_id, relation_type)`.
- The uniqueness constraint itself is not persisted in Phase 1; this phase only
  makes the database ready for it.

Implemented:

- Updated `services/summarizer-worker/main.py` to read briefing responses from
  `topics` instead of the old `groups` field when logging generated counts.
- Removed the scheduler stub from compose and source:
  - deleted `services/api/scheduler.py`
  - removed the `scheduler` service from `docker-compose.yml`
  - removed the dev override for `scheduler` from `docker-compose.dev.yml`
- Removed stale LLM semantic relation prompts from the default schema text in
  `services/api/routers/settings.py`.
- Removed legacy graph filter/color entries for no-longer-user-facing edge
  types from `services/web/app/knowledge/page.tsx`.
- Updated current architecture/deploy docs to stop presenting scheduler as a
  live service.
- Added duplicate edge cleanup SQL:
  `scripts/cleanup_duplicate_edges.sql`.

Live database cleanup:

- Before cleanup, the local database had 68 duplicate edge groups.
- Ran `scripts/cleanup_duplicate_edges.sql` against the local Postgres
  container.
- Deleted 116 duplicate `knowledge_edges`, keeping the lowest `id` per
  `(from_node_id, to_node_id, relation_type)`.
- After cleanup, duplicate edge groups were 0.
- Verified in a rolled-back transaction that this future constraint can be
  added:

```sql
ALTER TABLE knowledge_edges
ADD CONSTRAINT uq_knowledge_edges_from_to_relation
UNIQUE (from_node_id, to_node_id, relation_type);
```

Verification:

- `python -m py_compile services/summarizer-worker/main.py scripts/refactor_smoke.py`
  passed.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `docker compose config --services` lists no `scheduler` service.
- `docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers config --services`
  lists no `scheduler` service.
- `python scripts/refactor_smoke.py --skip-auth` passed after duplicate-edge
  cleanup.

Not run:

- Full summarizer-worker briefing trigger was not run because it can invoke the
  configured LLM path. The changed field handling is covered by code inspection
  and Python compilation; live API shape is covered by the existing briefing
  smoke check.
- `npm run lint` was not usable as a non-interactive check because this repo
  does not yet have ESLint configured and Next.js prompted to create a config.
