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

## 2026-05-11 - Phase 2 Wiki Read-only and Article Immutable

Status: implemented.

Assumptions:

- Phase 2 should enforce the new source-of-truth model without introducing the
  future object-specific tables yet.
- In the current schema, summary body remains in `knowledge_nodes.abstract`
  until Phase 2.5/3 introduces a dedicated summary table/body field.
- Full summary revise behavior calls the configured Claude and OpenAI embedding
  providers, so live verification covers validation and routing without
  invoking LLMs.

Implemented:

- Made `wiki/` read-only through `services/api/routers/files.py`:
  - `GET /api/files/content` can still read `wiki/` and `config/`.
  - `PUT /api/files/content` only accepts `config/`.
  - `DELETE /api/files/content` only accepts `config/`; wiki node deletion
    remains through `/api/kb/nodes/{id}`.
- Changed `write_wiki_node()` to export body from DB instead of preserving old
  wiki file body text.
- Changed summary creation to use DB source text instead of reading wiki body
  as generation context.
- Added `POST /api/kb/nodes/{id}/revise_summary`:
  - accepts an instruction instead of direct body replacement
  - only allows `object_type = 'summary'`
  - rewrites the summary through the LLM path
  - recalculates embedding
  - refreshes wiki export and `similar_to` edges
- Updated `/knowledge` so wiki files open read-only and summary nodes expose a
  revise-instruction control instead of direct wiki/body editing.
- Updated settings and `MEMORY.md` to describe wiki as a read-only export.

Verification:

- `python -c "import ast,pathlib; ..."` parsed the changed API files.
- `docker compose exec -T api python -m py_compile /app/routers/files.py /app/routers/kb.py`
  passed inside the runtime container.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.
- `PUT /api/files/content` for `wiki/articles/probe.md` returns 403.
- `DELETE /api/files/content` for `wiki/articles/probe.md` returns 403.
- `PUT` then `DELETE` for a temporary `config/templates/*.md` file still
  returns `{"ok": true}`.
- `POST /api/kb/nodes/{id}/revise_summary` with an empty instruction returns
  400 without invoking LLMs.
- `git diff --check` passed.

Implementation fixes:

- Symptom: local `python -m py_compile services/api/routers/files.py services/api/routers/kb.py`
  failed with `Permission denied` writing into `services/api/routers/__pycache__`.
- Root cause: existing `__pycache__` permissions in the workspace do not allow
  this user to create the temporary pyc file.
- Fix: used AST parsing locally and ran `py_compile` inside the API container,
  where runtime permissions match the deployed app.
- Verification: both AST parsing and container `py_compile` passed.

## 2026-05-11 - Phase 3 Time Fields

Status: implemented.

Assumptions:

- Phase 3 stores time metadata on `knowledge_nodes` until Phase 4 introduces
  `source_items` and later object-specific tables.
- `knowledge_time` is an expression, not a stored column:
  `COALESCE(effective_at, source_published_at, captured_at, ingested_at)`.
- Non-force briefing increments still use DB-created topic time to find newly
  ingested articles; force and first daily generation use `knowledge_time`.

Implemented:

- Added `ingested_at`, `source_published_at`, `source_updated_at`,
  `captured_at`, and `effective_at` to `knowledge_nodes`.
- Backfilled existing rows so `ingested_at` and `captured_at` default to the
  existing DB creation time.
- Added an expression index for `knowledge_time`.
- Extended `/api/kb/ingest` to accept the new time fields and return them from
  node detail responses.
- Exported the time metadata into generated wiki frontmatter.
- Extended ingestion `RawItem` with time metadata.
- RSS ingestion now maps feed `published_parsed` and `updated_parsed` to
  `source_published_at` and `source_updated_at`.
- URL ingestion now attempts to use page metadata date as `source_published_at`
  and otherwise falls back to captured time.
- File uploads accept optional `captured_at` and `effective_at`, store them in
  source upload batches, and pass them through file ingestion.
- WeChat push ingestion maps `pushed_at` to `captured_at`.
- Briefing force/first generation now filters and orders articles by
  `knowledge_time`.
- Updated source upload UI to expose optional save/content time fields.
- Updated `MEMORY.md` with the new time-field semantics.

Verification:

- Local AST parsing passed for changed API and ingestion Python files.
- `docker compose restart api` ran schema migration successfully.
- API container `py_compile` passed for `database.py`, `routers/kb.py`,
  `routers/sources.py`, and `routers/briefing.py`.
- Ingestion-worker container `py_compile` passed for `pipeline.py` and changed
  source adapters.
- Postgres shows all five new timestamp columns on `knowledge_nodes`.
- Postgres shows `idx_knowledge_nodes_knowledge_time` exists.
- Existing nodes have no missing `ingested_at` or `captured_at` defaults.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.
- A temporary `/api/kb/ingest` probe with non-zero embedding wrote and read
  `source_published_at`, `source_updated_at`, `captured_at`, and `effective_at`.
- Simulated RSS entry in the ingestion-worker container produced populated
  `source_published_at` and `source_updated_at`.
- Simulated file upload batch in the ingestion-worker container produced
  populated `captured_at` and `effective_at`.
- Temporary probe nodes were deleted after verification.
- `git diff --check` passed.

Implementation fixes:

- Symptom: the first temporary `/api/kb/ingest` probe used an all-zero embedding;
  background similarity generation could produce `NaN`, and node detail JSON
  serialization returned 500.
- Root cause: cosine similarity is undefined for a zero vector, so an all-zero
  test embedding is invalid for this pgvector path.
- Fix: deleted the invalid probe node and reran the ingest verification with a
  non-zero 1536-dimension vector.
- Verification: the second probe returned the expected time fields and was then
  deleted.
