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

## 2026-05-11 - Phase 2.5 Summary Perspective Embedding

Status: implemented.

Assumptions:

- Phase 2.5 is implemented on the current `knowledge_nodes` table rather than
  introducing `summary_nodes`; the dedicated object tables remain part of a
  later object-table migration.
- Existing `perspective` remains a compatibility field. New code writes
  `perspective_label` and `perspective_instruction`.
- Existing summary rows can be safely backfilled by copying their current
  `embedding` into `body_embedding` and using that as the fallback
  `perspective_embedding`. New and revised summaries generate real perspective
  embeddings through OpenAI.

Implemented:

- Added summary-specific fields on `knowledge_nodes`:
  - `perspective_label`
  - `perspective_instruction`
  - `perspective_embedding`
  - `body_embedding`
  - `is_default`
- Added ivfflat indexes for `body_embedding` and `perspective_embedding`.
- Backfilled existing summaries with default perspective metadata and non-null
  body/perspective embeddings.
- Extended `/api/kb/ingest` so auto-created summary nodes get default
  perspective metadata, `body_embedding`, and `perspective_embedding`.
- Extended manual summary creation to accept `perspective_label` and
  `perspective_instruction`, generate both embeddings, and keep the old
  `perspective` response field for compatibility.
- Extended summary revise to always recalculate `body_embedding`; if the request
  includes new perspective fields, it also recalculates `perspective_embedding`.
- Updated wiki export frontmatter for summaries with perspective label,
  instruction, and default flag.
- Updated `/api/kb/search` so summary results are scored by a fixed mixed score:
  `0.75 * body_similarity + 0.25 * perspective_similarity`.
- Updated draft layered retrieval summary search to use the same mixed score.
- Updated index expansion in draft retrieval to score child summaries before
  falling back to direct child article embedding.
- Updated the knowledge UI to send the new perspective fields and display
  summary perspective labels.
- Updated `MEMORY.md` with the current summary perspective model.

Verification:

- Local AST parsing passed for `database.py`, `routers/kb.py`, and
  `routers/drafts.py`.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `docker compose restart api` ran the migration successfully.
- API container `py_compile` passed for `database.py`, `routers/kb.py`, and
  `routers/drafts.py`.
- Postgres shows all five Phase 2.5 summary columns on `knowledge_nodes`.
- Postgres shows `idx_knowledge_nodes_body_embedding` and
  `idx_knowledge_nodes_perspective_embedding` exist.
- Existing summary backfill check: 94 summaries, with 0 missing labels,
  instructions, body embeddings, or perspective embeddings.
- Database-side mixed summary scoring query returned ordered summary results.
- `python scripts/refactor_smoke.py --skip-auth` passed.
- `git diff --check` passed.

Not run:

- Full live `create_summary` / `revise_summary` calls were not run because they
  invoke configured Claude and OpenAI providers. Validation covered schema,
  import/runtime compilation, backfill, API health, and the mixed-scoring SQL
  path without creating new LLM-generated summaries.

## 2026-05-11 - Phase 4 Source Items

Status: implemented.

Assumptions:

- Phase 4 should introduce `source_items` as the ingestion queue/manifest while
  keeping the current `knowledge_nodes` model until Phase 4.5.
- Existing source config shapes are kept for compatibility, but new URL,
  upload, and WeChat writes create `source_items`.
- RSS is materialized by the ingestion worker: fetched feed entries are first
  inserted as pending `source_items`, then consumed by the same worker run.
- Full successful ingestion was not forced during verification because it can
  call Claude/OpenAI; status transitions were verified with short/failing probe
  items.

Implemented:

- Added `source_items` with origin/raw/extracted refs, content hash, source
  times, retention policy, status, error, attempts, and timestamps.
- Added `knowledge_nodes.source_item_id` so newly generated article, book index,
  and chapter article nodes can trace back to a source item.
- Added source item API support:
  - `GET /api/sources/{source_id}/source-items`
  - `POST /api/sources/{source_id}/source-items`
  - `POST /api/sources/source-items/{item_id}/status`
  - `POST /api/sources/source-items/{item_id}/retry`
- Changed URL batch append to create one pending source item per URL instead of
  writing `sources.config.pending_urls`.
- Changed file upload to create one pending source item per uploaded file with
  `raw_snapshot_ref`.
- Changed WeChat push ingestion to create a source item after saving the raw
  text file.
- Changed ingestion-worker pipeline to consume pending source items, mark
  `processing` / `succeeded` / `failed`, write `extracted_text_ref`, and pass
  `source_item_id` to `/api/kb/ingest`.
- Changed book pipeline to consume source items and attach the same source item
  to the book index and chapter article nodes.
- Updated `MEMORY.md` to describe the new manifest and worker flow.

Verification:

- Local AST parsing passed for `database.py`, `routers/sources.py`,
  `routers/kb.py`, `pipeline.py`, and `sources/base.py`.
- `docker compose restart api` ran the migration successfully.
- API container `py_compile` passed for `database.py`, `routers/sources.py`,
  and `routers/kb.py`.
- Ingestion-worker container `py_compile` passed for `pipeline.py`, `main.py`,
  and `sources/base.py`.
- Postgres shows `source_items` exists with the Phase 4 columns.
- Postgres shows source item indexes and `knowledge_nodes.source_item_id`.
- Direct source item probe verified create, list, processing, failed, and retry
  transitions; probe row was removed afterwards.
- URL add probe verified `/add-url` creates a pending source item and no longer
  writes `sources.config.pending_urls`.
- After restarting ingestion-worker, a failing URL probe verified the worker
  consumes pending source items and marks failures with attempts/error; probe
  row was removed afterwards.
- Upload probe verified `/upload` creates a pending `origin_ref_type=upload`
  source item; probe row was removed afterwards.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.

Implementation fixes:

- Symptom: the first status-transition probe returned a 500 when setting a
  source item to `processing`.
- Root cause: `update_source_item_status()` passed unused bind parameters to
  `databases`/SQLAlchemy; this library rejects parameters that are not present
  in the SQL text.
- Fix: build the SQL parameter dict only with fields used by the selected
  update branch.
- Verification: repeated the status-transition probe through create, list,
  processing, failed, authenticated retry, and cleanup.

Not run:

- A full successful article/book/RSS ingestion was not run because it can invoke
  configured Claude/OpenAI providers. The live worker path was verified with a
  controlled failing URL item and a short upload item that stops before LLM
  analysis.

## 2026-05-11 - Phase 4.5 Object-specific Tables

Status: implemented.

Assumptions:

- `knowledge_nodes` remains the graph/node registry and compatibility surface
  for current API/UI code; object-specific fields are now also stored in
  dedicated tables and will be removed from `knowledge_nodes` in a later phase.
- Phase 4.5 should migrate the primary write/read paths without forcing a full
  rewrite of every maintenance query in the same change.
- `entity_nodes` only stores stable identity fields. Entity facts/profiles remain
  a later phase.

Implemented:

- Added `article_nodes`, `summary_nodes`, `entity_nodes`, and `index_nodes` to
  the API schema.
- Added idempotent startup backfill from existing `knowledge_nodes` into the
  object-specific tables.
- Added repeatable migration script:
  `scripts/backfill_object_tables.sql`.
- Added `services/api/object_nodes.py` as a small service layer for cross-table
  upserts and merged node reads.
- Changed `/api/kb/ingest` to write `knowledge_nodes` plus the matching object
  table for article, summary, entity, and index objects.
- Changed manual summary creation and summary revision to update
  `summary_nodes` including body and perspective metadata.
- Changed `restore_from_wiki()` and index abstract aggregation to sync the
  relevant object table after writing `knowledge_nodes`.
- Changed `/api/kb/node/{id}` and wiki export to merge object-specific fields
  from the dedicated table.
- Changed semantic search and draft layered retrieval for summaries to prefer
  `summary_nodes.body_embedding` and `summary_nodes.perspective_embedding`, with
  legacy `knowledge_nodes` fields as fallback.
- Updated `MEMORY.md` with the new object table model.

Verification:

- Local AST parsing passed for `object_nodes.py`, `database.py`,
  `routers/kb.py`, `routers/drafts.py`, and `maintenance.py`.
- Local `py_compile` was not usable because the existing container-owned
  `services/api/__pycache__` directory denies writes from the host user; this is
  the same environment constraint seen in earlier phases.
- `docker compose restart api` ran the schema migration successfully.
- API container `py_compile` passed for `object_nodes.py`, `database.py`,
  `routers/kb.py`, `routers/drafts.py`, and `maintenance.py`.
- Postgres shows all four object-specific tables and expected indexes.
- Object-table row counts match `knowledge_nodes` counts by object type:
  article 92, summary 94, entity 104, index 2.
- `/api/kb/node/{id}` probes for article, summary, entity, and index returned
  200. The summary probe returned `summary_of`, `perspective_label`, and wiki
  body from the merged object-table read path.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.
- `git diff --check` passed.

Not run:

- Live summary create/revise and full ingestion success paths were not forced
  because they can invoke configured Claude/OpenAI providers. The code paths are
  wired to the new `object_nodes` service and container compilation passed.

## 2026-05-11 - Phase 5 Remove LLM Semantic Edges

Status: implemented.

Assumptions:

- Phase 5 removes LLM-inferred graph relations
  `extends/background_of/supports/contradicts`; it does not remove all LLM usage
  from maintenance because entity page generation and index abstract aggregation
  remain part of later/current workflows.
- `similar_to` remains a statistical embedding edge and is not treated as an LLM
  semantic edge.
- Legacy LLM semantic edges may be deleted by maintenance because the refactor
  plan allows cleanup/hiding and those edges are no longer part of the graph
  model.

Implemented:

- Removed the active LLM semantic edge inference functions from
  `services/api/maintenance.py`:
  `fix_islands`, `supplement_edges`, and `detect_contradictions`.
- Changed `run_maintenance()` so it no longer requires a Claude key before
  running deterministic maintenance tasks and no longer calls the removed edge
  inference steps.
- Added `cleanup_legacy_llm_edges()` to delete existing
  `extends/background_of/supports/contradicts` edges during maintenance.
- Changed `/api/kb/graph/all` to exclude legacy LLM semantic edges and exclude
  them from graph degree counts.
- Changed `/api/kb/graph`, `/api/kb/node/{id}`, and wiki export relation
  collection to hide legacy LLM semantic edges.
- Updated `MEMORY.md` to describe the Phase 5 graph model and maintenance flow.

Verification:

- API container `py_compile` passed for `maintenance.py` and `routers/kb.py`.
- `rg` confirmed there are no remaining references to `fix_islands`,
  `supplement_edges`, `detect_contradictions`, `analyze_relation`,
  `upsert_llm_edge`, or `auto_llm` in `services/api`.
- Postgres currently has zero
  `extends/background_of/supports/contradicts` edges.
- Direct `cleanup_legacy_llm_edges()` probe returned
  `{'deleted': {}, 'total_deleted': 0}`.
- Restarted API successfully.
- `/api/kb/graph/all` probe returned no legacy LLM semantic edge types.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.

Implementation note:

- An initial one-line Python verification command failed because it used
  separate `asyncio.run()` calls against the same `databases` connection pool.
  The probe was rerun with a single event loop and passed; no implementation
  change was needed.

## 2026-05-12 - Phase 6 Entity Facts / Profile / Relatedness

Status: implemented.

Assumptions:

- The project still has no `jobs` table/worker for derived model tasks, so Phase
  6 uses a synchronous service layer (`entity_insights.py`) that can later be
  moved behind jobs.
- Facts are source-grounded records derived from article/entity mentions. When
  the article analysis supplies `summary_hint`, that hint becomes the fact text;
  otherwise the system records a deterministic mention fact with the article
  title and evidence span.
- Entity profiles are derived from facts with deterministic rollup text in this
  phase. This keeps the phase verifiable without forcing Claude/OpenAI calls.
- Relatedness is stored only in `entity_pair_signals`; no `co_occurs_with`
  graph edges are generated.

Implemented:

- Added `entity_facts`, `entity_profiles`, and `entity_pair_signals` tables with
  indexes.
- Added `services/api/entity_insights.py` for:
  - fact upsert from mentions
  - stale profile marking
  - profile refresh
  - facts backfill from `mentions` edges
  - relatedness rebuild from article/entity co-occurrence
- Extended entity candidate processing so matched existing entities immediately
  create/update facts and mark profiles stale.
- Extended candidate promotion marking so accumulated candidate mentions are
  materialized into facts and the entity profile is refreshed.
- Extended maintenance to backfill facts, refresh stale profiles, and rebuild
  relatedness after wikilink/mentions maintenance.
- Added API endpoints:
  - `GET /api/kb/entities/{id}/facts`
  - `GET /api/kb/entities/{id}/timeline`
  - `GET /api/kb/entities/{id}/related`
  - `POST /api/kb/entities/{id}/regenerate`
- Added an entity-only section in the knowledge detail panel that shows recent
  source facts and related entities without adding relatedness edges to the
  graph.
- Updated `MEMORY.md` with the new entity model and maintenance flow.

Verification:

- Local AST parsing passed for `entity_insights.py`, `database.py`,
  `routers/kb.py`, and `maintenance.py`.
- `docker compose restart api` ran the schema migration successfully.
- API container `py_compile` passed for `database.py`, `entity_insights.py`,
  `routers/kb.py`, and `maintenance.py`.
- Postgres shows the three Phase 6 tables and expected indexes.
- Backfilled existing mentions into 690 `entity_facts`.
- Refreshed 104 `entity_profiles`.
- Rebuilt 1,458 `entity_pair_signals`; rerunning the rebuild kept the count at
  1,458, confirming idempotent pair replacement.
- Confirmed `knowledge_edges` has zero `co_occurs_with` rows.
- Probed `GET /api/kb/entities/{id}/facts`, `/timeline`, and `/related` for an
  entity with facts; all returned data including source article ids and
  relatedness explanations.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.

Not run:

- A full new article ingestion success path was not forced because it can invoke
  configured Claude/OpenAI providers. Existing mentions were backfilled through
  the same service layer used by the new candidate-processing path.

## 2026-05-12 - Phase 7 Index Structured

Status: implemented.

Assumptions:

- Phase 8 has not introduced a jobs table/worker yet, so `POST /api/kb/indices/{id}/rollup`
  runs the existing index rollup path synchronously.
- `index_children` is the source of truth for index membership and order.
  Existing `knowledge_edges.part_of` rows are migrated into `index_children`
  and hidden from API graph/detail responses instead of being deleted.
- API graph responses project `index_children` as read-only `contains` edges
  for visualization; those are not stored in `knowledge_edges`.

Implemented:

- Added `index_children` with `(index_id, child_id)` primary key, `position`,
  `child_role`, timestamps, and indexes on `(index_id, position)` and
  `child_id`.
- Added schema backfill from legacy `knowledge_edges.part_of` into
  `index_children`.
- Added `services/api/index_structure.py` with:
  - `add_child`
  - `remove_child`
  - `reorder_children`
  - `get_children`
  - `get_parents`
  - `get_ancestors`
  - `get_descendants`
  - stale marking for parent index rollups
- Changed `/api/kb/ingest` so `parent_index_id` writes `index_children` instead
  of a real `part_of` edge.
- Added Index API endpoints:
  - `POST /api/kb/indices`
  - `GET /api/kb/indices/{id}`
  - `PATCH /api/kb/indices/{id}`
  - `GET /api/kb/indices/{id}/children`
  - `POST /api/kb/indices/{id}/children`
  - `DELETE /api/kb/indices/{id}/children/{child_id}`
  - `PATCH /api/kb/indices/{id}/children/order`
  - `POST /api/kb/indices/{id}/rollup`
- Added structure query endpoints:
  - `GET /api/kb/objects/{id}/parents`
  - `GET /api/kb/objects/{id}/ancestors`
  - `GET /api/kb/indices/{id}/descendants`
- Changed `aggregate_index_abstracts()` to read children from
  `index_children`, preserve user-editable `index_nodes.description`, and clear
  `abstract_stale` after rollup.
- Changed draft layered retrieval index expansion to read article children from
  `index_children`.
- Changed `restore_from_wiki()` so legacy `part_of` frontmatter relations are
  restored into `index_children`, not `knowledge_edges`.
- Hid legacy `part_of` from node/detail graph responses and projected
  `index_children` as `contains` edges for graph display.
- Updated `MEMORY.md` with the structured index model.

Verification:

- API schema migration completed after restart.
- Postgres shows 66 rows in `index_children` backfilled from existing legacy
  data and the expected indexes:
  `index_children_pkey`, `idx_index_children_index_position`,
  `idx_index_children_child_id`.
- Existing legacy `part_of` rows remain in `knowledge_edges` for compatibility
  but `/api/kb/graph/all` returned 66 `contains` edges and 0 `part_of` edges.
- Probed:
  - `GET /api/kb/indices/ind_32ad72a2a2b66a21/children` -> 54 children
  - `GET /api/kb/indices/ind_32ad72a2a2b66a21/descendants` -> 54 descendants
  - `GET /api/kb/objects/{child}/parents` -> parent index returned
  - `GET /api/kb/objects/{child}/ancestors` -> 1 ancestor returned
- Service-layer add/reorder smoke test inserted temporary nodes, added two
  children, reordered them, confirmed parent lookup and `abstract_stale = true`,
  then deleted the temporary nodes.
- Container AST parsing passed for `database.py`, `index_structure.py`,
  `object_nodes.py`, `routers/kb.py`, `routers/drafts.py`, and
  `maintenance.py`.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.
- `git diff --check` passed.

Implementation fixes recorded:

- First API restart failed because the backfill query referenced
  `knowledge_edges.created_at`, which does not exist. Fixed by using `NOW()`
  for migrated `index_children.created_at`.
- The first service-layer write smoke test failed because asyncpg could not
  infer the type of `:user_id` in `(:user_id IS NULL OR user_id = :user_id)`.
  Fixed by generating explicit user-filter SQL when `user_id` is present.

## 2026-05-12 - Phase 8 Job Queue

Assumptions:

- Phase 8 starts with the planned short-term Postgres queue; no Redis,
  RabbitMQ, or external scheduler is introduced.
- The first migration target is low-risk long-running work already owned by
  API/maintenance code:
  - summary generation
  - summary revision
  - index rollup
  - wiki rebuild
  - maintenance run
  - rebuild_from_raw
- Standalone embedding jobs are not exposed yet. Embeddings run inside the
  migrated summary/rollup jobs where they already exist today.

Implemented:

- Added `jobs` table with:
  - `job_type`
  - `provider`
  - `model`
  - JSON `payload`
  - `pending/running/succeeded/failed/retrying/cancelled` status
  - priority
  - idempotency key
  - attempts/max_attempts
  - JSON `result`
  - `error`
  - timestamps
- Added indexes for user/status listing, worker claim order, and idempotency
  lookup.
- Added `services/api/jobs.py` queue service:
  - enqueue
  - list/detail
  - cancel
  - retry
  - claim with `FOR UPDATE SKIP LOCKED`
  - complete/fail
- Added `services/api/job_worker.py`, reusing the API image and executing
  explicit handlers for:
  - `generate_summary`
  - `revise_summary`
  - `aggregate_index_abstract`
  - `rebuild_wiki`
  - `run_maintenance`
  - `rebuild_from_raw`
- Added `job-worker` to the docker `workers` profile.
- Added Job API:
  - `GET /api/kb/jobs`
  - `GET /api/kb/jobs/{id}`
  - `POST /api/kb/jobs/{id}/cancel`
  - `POST /api/kb/jobs/{id}/retry`
- Changed long-running endpoints to enqueue jobs instead of blocking request
  handling:
  - `POST /api/kb/nodes/{id}/create_summary`
  - `POST /api/kb/nodes/{id}/revise_summary`
  - `POST /api/kb/indices/{id}/rollup`
  - `POST /api/kb/wiki/rebuild`
  - `POST /api/kb/maintenance/run`
  - `POST /api/kb/maintenance/rebuild_from_raw`
- Added a lightweight jobs panel to the knowledge page. It polls
  `/api/kb/jobs`, displays recent job statuses, and exposes retry/cancel
  actions where applicable.
- Updated `MEMORY.md` with the Phase 8 runtime model.

Verification:

- API restart completed and schema migration created `jobs` plus indexes:
  `jobs_pkey`, `idx_jobs_claim`, `idx_jobs_user_idempotency_key`,
  `idx_jobs_user_status_created`.
- Service-layer queue smoke inserted a temporary job, claimed it, failed it,
  retried it, cancelled it, and deleted it.
- Authenticated API probe:
  - login succeeded
  - `GET /api/kb/jobs?limit=3` returned 200
  - `POST /api/kb/maintenance/run` returned a pending `job_id`
  - `GET /api/kb/jobs/{id}` returned 200
  - `POST /api/kb/jobs/{id}/cancel` returned `cancelled`
- Worker handler probe enqueued and claimed a temporary `rebuild_wiki` job,
  ran `job_worker.run_job()`, completed it as `succeeded`, and deleted the
  temporary job. It rebuilt 292 wiki nodes during the probe.
- `docker compose --profile workers config --services` includes
  `job-worker`.
- Python AST parsing passed for `database.py`, `jobs.py`, `job_worker.py`, and
  `routers/kb.py`.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `python scripts/refactor_smoke.py --skip-auth` passed.

Implementation fixes recorded:

- Local `python -m py_compile` could not write to an existing root-owned
  `__pycache__`; verification switched to AST parsing, matching previous
  phases.
- The first DB verification command had invalid inline Python quoting. Re-ran
  the check via `psql`, which confirmed `jobs` row count and indexes.

## 2026-05-12 - Phase 9 Rebuild 重做

Assumptions:

- Keep Phase 9 on the existing `rebuild_from_raw` surface instead of adding a
  second rebuild endpoint.
- Use `source_items` as the rebuild manifest. Do not scan raw directories to
  infer what should be rebuilt.
- Treat `resume` as manifest-status resume: skip already `succeeded` items and
  continue with non-succeeded selected items.

Implemented:

- Reworked `maintenance.rebuild_from_raw()` to select rebuild work from
  `source_items`.
- Added rebuild filters:
  - `source_id`
  - `source_type`
  - `status`
  - `since`
  - `until`
  - `dry_run`
  - `resume`
- Added dry-run result reporting for selected source items, selected sources,
  selected source types, and node delete counts.
- Changed rebuild deletion scope to selected manifest items:
  - article/index nodes linked by `source_item_id`
  - summaries whose `summary_of` points to selected article/index nodes
  - entity nodes whose `source_node_ids` overlap selected article/index nodes
  - wiki files for the deleted node IDs
- Rebuild now resets selected `source_items` to `pending`, clears errors and
  attempts, resets selected sources' `last_fetched_at`, triggers
  ingestion-worker by source, waits on selected item statuses, then runs
  `run_maintenance()`.
- `job_worker` now passes `rebuild_from_raw` job payload into maintenance.
- `POST /api/kb/maintenance/rebuild_from_raw` now accepts an optional JSON body
  carrying the rebuild filters and uses the payload in its idempotency key.
- URL-backed nodes now get deterministic IDs from `raw_ref.url`; URL ingest also
  deduplicates by `raw_ref.url`.
- Default summary nodes created through `/api/kb/ingest` now get deterministic
  IDs from `summary_of + perspective`.
- Updated `MEMORY.md` to describe the manifest-driven rebuild model.

Verification:

- Python AST parsing passed for `maintenance.py`, `job_worker.py`, and
  `routers/kb.py`.
- Restarted API successfully.
- Ran dry-run rebuild with no matching URL items.
- Inserted a temporary source/source_item, ran non-empty dry-run selection by
  `source_id`, and cleaned up the temporary records.
- Inserted temporary article/summary/entity nodes linked to a temporary
  source_item, ran dry-run selection, confirmed node delete counts
  `article/index=1`, `summary=1`, `entity=1`, and cleaned up the temporary
  records.
- Authenticated `POST /api/kb/maintenance/rebuild_from_raw` with
  `{"dry_run": true, "source_type": "plaintext"}` returned 200 and a queued
  job; the temporary job row was deleted.
- Worker smoke enqueued a temporary `rebuild_from_raw` dry-run job, claimed it,
  ran `job_worker.run_job()`, completed it as `succeeded`, and deleted the job.
- Verified temporary smoke source/source_item/job rows were cleaned up.
- `python scripts/refactor_smoke.py --skip-auth` passed.
- `docker compose logs api --tail 80` showed no Phase 9 errors after probes.
- `git diff --check` passed.

Implementation fixes recorded:

- Initial dry-run failed because the `databases` library rejects bind values
  that are not present in the SQL text. Fixed by only adding filter params when
  the corresponding SQL predicate is added.
- The first inline container smoke used `async def` after a semicolon, which is
  invalid Python syntax. Re-ran the smoke using `exec()` with a multi-line
  async function body.

## 2026-05-12 - Phase 10 Chat Toolset

Assumptions:

- Phase 10 only exposes read-only Chat tools.
- `kb.tools` is implemented as an API service module, not as direct Chat SQL
  embedded in the router.
- Chat tool results are surfaced over the existing SSE response and shown by
  the current sidebar; assistant messages remain stored as plain text.

Implemented:

- Added `services/api/kb_tools.py` with read-only tools:
  - `kb_search(query, filters)`
  - `kb_get_node(id)`
  - `kb_get_neighbors(id, depth)`
  - `kb_get_sources(node_id)`
- `kb_search` supports object/source/time filters and uses semantic search with
  the existing embedding model; if embedding is unavailable, it falls back to
  text search.
- `kb_get_node` returns object-specific fields via `object_nodes`, strips
  embeddings, and includes a wiki body excerpt.
- `kb_get_neighbors` returns only visible graph edges plus index `contains`
  structure.
- `kb_get_sources` returns source item/source metadata for a node and its
  source nodes.
- Updated `routers/chat.py` so Chat calls Anthropic with the read-only toolset,
  executes tool calls through `kb_tools.run_tool()`, streams tool result events
  and merged references over SSE, then stores the final assistant text.
- Updated Chat system prompt to require read-only behavior and forbid creating,
  modifying, or deleting summary/index/tags/entity content.
- Updated `ChatSidebar` to display tool-call badges and referenced nodes from
  SSE events.
- Updated `MEMORY.md` to describe the Phase 10 Chat Toolset state.

Verification:

- Read-only AST parsing passed for `services/api/kb_tools.py` and
  `services/api/routers/chat.py`.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- Restarted API successfully; logs showed startup complete and no Phase 10
  import/runtime errors.
- In-container `kb_tools` smoke confirmed:
  - registered tools are `kb_search`, `kb_get_node`, `kb_get_neighbors`,
    `kb_get_sources`
  - no tool names include create/revise/delete/update
  - `kb_get_node`, `kb_get_neighbors`, and `kb_get_sources` work against an
    existing node
  - `kb_search` returned results and references

Implementation fixes recorded:

- Local `python -m py_compile` could not write to an existing root-owned
  `services/api/__pycache__`; verification switched to a no-write AST parse.
- `next lint` entered Next.js first-run ESLint configuration, so it was not used
  as a verification signal. TypeScript verification used `tsc --noEmit`.

## 2026-05-12 - Login passphrase whitespace fix

Assumption:

- The reported false negative on the login page can be caused by copied or
  password-manager-filled passphrases containing leading/trailing whitespace.

Implemented:

- Trimmed the passphrase before the login page sends it to `/api/auth/login`.
- Trimmed the submitted passphrase in `verify_password()` before comparing with
  `AUTH_PASSWORD`.

Verification:

- Confirmed the running API accepts the configured `AUTH_PASSWORD` unchanged.
- Confirmed padded input with leading spaces and a trailing newline returns 200
  through `api:8000`, `web:3000`, and `nginx`.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- Read-only AST parsing passed for `services/api/auth.py`.

Implementation fixes recorded:

- `python -m py_compile services/api/auth.py` still cannot write to the
  existing root-owned `services/api/__pycache__`; verification used no-write AST
  parsing instead.

## 2026-05-12 - Wechat2RSS-only WeChat source

Assumption:

- 微信公众号 source 只保留 Wechat2RSS 部署路径；旧 iPhone 快捷指令 push
  source 不迁移、不兼容，用户需要从 Wechat2RSS 公众号列表重新创建 source。

Implemented:

- Added a `wechat2rss` Docker Compose service using `ttttmr/wechat2rss:latest`
  with env-based license/token configuration and `./data/wechat2rss` persistence.
- Added API support for listing Wechat2RSS subscriptions and creating one
  `wechat` source per selected feed.
- Changed `wechat` sources to `fetch_mode = subscription`; generic source
  creation no longer creates legacy token-based WeChat sources.
- Removed the legacy `/api/sources/wechat/ingest` endpoint and the
  ingestion-worker `WechatSource` push adapter.
- Changed the ingestion worker so `wechat` sources must have
  `provider = wechat2rss`; it builds the internal tokenized feed URL server-side
  and reuses `RSSSource`.
- Reworked the source UI so selecting WeChat loads the Wechat2RSS subscription
  list, creates a source from one selected account, and no longer shows
  shortcut/API-token instructions.
- Added a login-protected Nginx `/wechat2rss/` reverse proxy and included
  Wechat2RSS data in backups.

Verification:

- Read-only AST parsing passed for `services/api/routers/sources.py`,
  `services/ingestion-worker/main.py`, and `services/ingestion-worker/sources/base.py`.
- `npm exec tsc -- --noEmit` passed in `services/web`.
- `docker compose config --services` and
  `docker compose --profile workers config --services` both include
  `wechat2rss`; workers config still includes `ingestion-worker`.
- `git diff --check` passed.
- Runtime grep found no legacy `wechat/ingest`, `WechatSource`,
  `X-API-Token`, or shortcut references in active service/frontend code.

Implementation fixes recorded:

- Container smoke against Wechat2RSS was not run because local `.env` does not
  yet define `WECHAT2RSS_LIC_EMAIL`, `WECHAT2RSS_LIC_CODE`,
  `WECHAT2RSS_TOKEN`, or `WECHAT2RSS_RSS_HOST`; compose config therefore emits
  expected blank-variable warnings until those are set.

## 2026-05-12 - Web startup Google Fonts removal

Issue:

- The web container logged repeated startup failures while requesting
  `https://fonts.gstatic.com/...JetBrainsMono...woff2`.

Fix:

- Removed `next/font/google` usage from `services/web/app/layout.tsx`.
- Added local CSS fallback font variables in `services/web/app/globals.css` so
  existing `--font-inter`, `--font-cormorant`, and `--font-jetbrains`
  references continue to resolve without network access.

Verification:

- `npm exec tsc -- --noEmit` passed in `services/web`.
- Runtime grep found no `next/font/google`, `fonts.gstatic`, or Google font
  loader imports in `services/web`.
- `git diff --check` passed.

## 2026-05-12 - Wechat2RSS management assets proxy

Issue:

- Visiting `/wechat2rss/` returned the Wechat2RSS HTML, but the browser then
  requested root-relative `/assets/...` files. Nginx sent those to the Next.js
  web service, causing 404s.

Fix:

- Added login-protected Nginx proxy locations for `/assets/` and
  `/favicon.ico` to forward Wechat2RSS management-page static assets to the
  `wechat2rss` container.

Verification:

- Pending reload/restart of Nginx on the VPS.

## 2026-05-12 - Wechat2RSS management root API proxy

Issue:

- The Wechat2RSS management page posts root-relative API requests such as
  `/list?k=...`. Nginx routed those requests to the Next.js web service, so
  Wechat2RSS login/list calls returned 404 and the UI reported a wrong
  passphrase.

Fix:

- Added Nginx proxy locations for Wechat2RSS root API paths including
  `/config`, `/list`, `/login/`, `/add/`, `/addurl`, `/del/`, `/pause/`,
  `/feed/`, and `/img-proxy`.
- Management/mutation routes remain protected by the existing KnowledgeBase-S
  login `auth_request`; public feed and image-proxy routes rely on Wechat2RSS
  token/HMAC behavior.

Verification:

- `docker compose config --services` passed.
- `git diff --check` passed.
