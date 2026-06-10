# KnowledgeBase-S — Bug & System Design Findings

Review date: 2026-06-09
Scope: `services/api`, `services/ingestion-worker`, `services/web`, infra (`docker-compose*.yml`, `nginx/`).

This is a single-user personal knowledge base (FastAPI + Postgres/pgvector, Next.js frontend, a polling ingestion worker, and a DB-backed job queue). Most findings below are real defects; a few are design risks that are tolerable for a single-user deployment but worth knowing. Items are ordered by severity.

---

## A. Security

### A1. `routers/files.py` has no authentication at all — unauthenticated read/write/delete of user files
**Severity: Critical** — ✅ **Resolved 2026-06-10**

> Fixed: `routers/files.py:20` router now declares `dependencies=[Depends(require_auth)]`, so all four file endpoints require the auth cookie.

`main.py:49` mounts `files.router` with no dependency, and none of the four endpoints declare `Depends(require_auth)` (`routers/files.py:57,124,138,147`). nginx proxies `/api` straight to the API (`nginx/nginx.conf`, `location /api`), so these are reachable from the internet without a cookie:

- `GET /api/files/tree` — lists all of the user's files
- `GET /api/files/content?rel_path=…` — reads any wiki/config markdown
- `PUT /api/files/content` — **writes** files
- `DELETE /api/files/content` — **deletes** files

The Next.js `middleware.ts` only guards the *web* frontend; it does nothing for direct API calls. Every other router uses `require_auth`, so this one is almost certainly an oversight. Fix: add `dependencies=[Depends(require_auth)]` to the router.

### A2. Path-area confinement in `files.py` is bypassable with `..`
**Severity: High** (compounds A1) — ✅ **Resolved 2026-06-10**

> Fixed: `_safe_path` now resolves first, rejects escapes with `Path.is_relative_to(base)`, and checks the *normalized* path against the allowed area. Verified `config/../wiki/x` and the sibling-prefix escape are both denied.

`_safe_path` (`routers/files.py:31-43`) checks the *prefix* on the **raw** `rel_path` (`rel_path.startswith("config/")`) but resolves traversal only against the user base dir. So `rel_path = "config/../wiki/articles/x.md"` passes the writable-prefix check (it starts with `config/`) yet resolves into `wiki/`, defeating the read-only intent of the wiki area. A caller can write/delete anywhere under `user_data/default/`, not just `config/`.

Additionally the escape guard `str(resolved).startswith(str(base.resolve()))` (line 41) is a string-prefix check with no separator, so a sibling directory like `user_data/default-evil/` would also pass. Use `Path.is_relative_to()` (or compare against `base / ""`) and validate the *normalized* path against the allowed area, not the raw string.

### A3. Several `sources.py` write endpoints are unauthenticated
**Severity: High** — ✅ **Resolved 2026-06-10**

> Fixed: the six endpoints are gated with `require_auth_or_service_token`; the ingestion worker now presents `KB_SERVICE_TOKEN` via an `X-KB-Service-Token` header (`main.py`/`pipeline.py` `_service_headers()`), and `docker-compose.yml` passes the token to the worker. Fail-closed — `KB_SERVICE_TOKEN` must be set for ingestion to authenticate. Note: the `/api/kb/*` worker endpoints remain unauthenticated (out of A3 scope; follow-up needed).

These handlers have no `Depends(require_auth)`:

- `GET /api/sources` — `list_sources` (`sources.py:643`)
- `GET /api/sources/{id}` — `get_source` (`sources.py:620`)
- `GET /api/sources/{id}/source-items` (`sources.py:466`)
- `POST /api/sources/{id}/source-items` — **creates** items (`sources.py:492`)
- `POST /api/sources/source-items/{id}/status` — **mutates** items + document_instances (`sources.py:510`)
- `PUT /api/sources/{id}` — **updates** a source (`sources.py:839`)

These are the endpoints the ingestion worker calls, but they share the public `/api` surface with no service-token check and no network isolation (the worker reaches them over the Docker network, but nginx also exposes them publicly). An anonymous caller can enumerate sources and inject/alter source items. Either gate them behind `require_auth_or_service_token` (like `kb/internal.py` does) and have the worker present `KB_SERVICE_TOKEN`, or bind the internal API to the Docker network only.

### A4. CORS allows credentialed requests from any origin
**Severity: Medium** — ✅ **Resolved 2026-06-10**

> Fixed: `allow_origins` is now an explicit allowlist from `CORS_ALLOW_ORIGINS` (comma-separated), falling back to `NEXTAUTH_URL`, default empty (`main.py:_cors_allow_origins`). The web UI calls the API same-origin through the Next.js `/api/*` rewrite, so it needs no entry; the wildcard had no legitimate consumer.

`main.py:32-38` sets `allow_origins=["*"]` together with `allow_credentials=True`. Starlette resolves this by *reflecting the request's Origin* and returning `Access-Control-Allow-Credentials: true`, i.e. any website may make credentialed cross-origin calls and read the responses. The auth cookie is `samesite=lax` (`main.py:75`), which blocks it from being attached on cross-site `fetch`, so practical exploitability is limited today — but the configuration is wrong and a future change to `samesite=none` would open credential theft/CSRF. Pin `allow_origins` to the known frontend origin(s).

### A5. nginx exposes an agent UI with no auth
**Severity: Medium (informational)**

`nginx/nginx.conf` proxies `/agent/` to `host.docker.internal:18789` with no authentication in front of it. If that backend is a control/agent surface, it is internet-reachable unauthenticated. Confirm intent or put it behind auth.

### A6. Login has no rate limiting and uses a non-constant-time password compare
**Severity: Low**

`verify_password` (`auth.py:15-16`) compares with `==` (timing-observable) and `POST /api/auth/login` (`main.py:66`) has no throttling, so the single shared password is brute-forceable. Use `hmac.compare_digest` (as `verify_service_token` already does) and add basic rate limiting.

---

## B. Correctness bugs

### B1. Job idempotency is racy — duplicate jobs under concurrency
**Severity: Medium** — ✅ **Resolved 2026-06-10**

> Fixed: Alembic `0002` replaces the non-unique index with a **partial unique** index `uq_jobs_active_idempotency` on `(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL AND status IN ('pending','running','retrying')` (active states only, so a key is re-usable once its job is terminal — keys like `run_maintenance:default` are reused). `enqueue_job` is now a single atomic `INSERT … ON CONFLICT … DO UPDATE (no-op) RETURNING`, eliminating the read-then-insert race. Verified against Postgres: 12 truly-concurrent same-key enqueues yield exactly one row; unkeyed jobs always insert; re-enqueue after terminal creates a fresh job.

`enqueue_job` (`jobs.py:46-60`) does a read-then-insert: it `SELECT`s for an existing non-terminal job with the same `idempotency_key`, and inserts if none found. There is **no unique constraint** backing this — `database.py:482` drops the old unique index and `database.py:485` recreates it as a plain (non-unique) `CREATE INDEX`. Two concurrent enqueues with the same key both see "none" and both insert. Make `idx_jobs_user_idempotency_key` a `UNIQUE` partial index and handle the conflict, or upsert.

### B2. No recovery for jobs stuck in `running`
**Severity: Medium** — ✅ **Resolved 2026-06-10**

> Fixed: added `jobs.reclaim_stuck_jobs(timeout)` — `running` jobs whose `started_at` is older than the timeout are requeued as `retrying` (or `failed` if attempts are exhausted). The job-worker loop calls it on a time-gated cadence (`JOB_RECLAIM_INTERVAL`, default 60s; `JOB_STUCK_TIMEOUT_SECONDS`, default 900s — must exceed the longest job). Verified: stuck→retrying, attempts-exhausted→failed, within-timeout→untouched.

`claim_next_job` (`jobs.py:157-177`) only claims `pending`/`retrying`. If a worker crashes (or the process is killed) mid-job, the row stays `status='running'` forever and is never retried — it's invisible to the claimer and to `retry_job` (which only accepts `failed`/`cancelled`). There is no lease/heartbeat/timeout. Add a "reclaim jobs running longer than N minutes" sweep, or a visibility timeout.

### B3. Worker-triggering is fire-and-forget and silently swallows failures
**Severity: Medium** — ✅ **Resolved 2026-06-10**

> Fixed both ways the finding suggested. (1) The trigger is no longer silent: `_trigger_ingestion()` logs failures and the upload/add-url responses return a `triggered` flag so the caller knows the async kick didn't fire. (2) Resilience: new `GET /api/sources/pending/source-ids` lets the worker's poll loop also process **any** source with pending items (manual uploads included), so a missed trigger is recovered on the next poll instead of leaving items `pending` forever.

After upload/add-url, the API calls `POST {INGESTION_WORKER_URL}/trigger/{id}` inside `try/except Exception: pass` (`sources.py:762-768`, `sources.py:810-816`). If the worker is down or the call fails, the user gets `{"ok": true}` but nothing is ever ingested and no error is surfaced or recorded. The worker's hourly poll only covers `subscription` sources, not `manual` uploads, so a missed trigger means the item sits `pending` indefinitely. At minimum, log the failure; better, rely on the worker polling `pending` items for all source types rather than on a best-effort HTTP ping.

### B4. Wiki frontmatter is built with unescaped f-strings — LLM output can corrupt the file
**Severity: Medium** — ✅ **Resolved 2026-06-10**

> Fixed on both sides. **Writers** (`ingestion-worker/pipeline.py` article/summary/entity and the API rebuild path `kb/wiki.write_wiki_node`) now build a dict and serialize with `yaml.safe_dump(..., allow_unicode=True, sort_keys=False)`, so titles/tags/aliases containing quotes, colons, brackets, newlines or `---` can't corrupt the frontmatter; heading newlines are collapsed. **Readers** — a value containing `---` would still break a substring split, so all four (`kb/wiki`, `kb/internal`, `kb_tools`, `maintenance/restore`) now use a shared, line-anchored `split_frontmatter()` in `kb/common.py`. `restore.py` already `yaml.safe_load`ed the block (with fancy-quote/regex hacks because the old output was frequently invalid YAML); valid output makes it reliable. Verified with adversarial round-trips (exact value preservation, body with a `---` rule, legacy-file back-compat).

`write_wiki_article` / `write_wiki_summary` / `write_wiki_entity` (`pipeline.py:455-475, 487-503, 517-534`) interpolate titles, tags, and aliases directly into YAML frontmatter:

```python
title: "{title}"
tags: [{", ".join(tags)}]
```

`title`, `tags`, and `aliases` come from the LLM and from source content. A title containing `"` or a newline, or a tag containing `,`/`]`, produces invalid YAML and a malformed document. Downstream `read_wiki_body` splits on `---` (`kb/wiki.py:27`), so a stray `---` in the body would also mis-parse. Serialize via a YAML library (or escape) instead of string interpolation.

### B5. Embedding dimension is hard-coded in DDL but configurable in settings
**Severity: Low/Medium** — ✅ **Resolved 2026-06-10**

> Fixed by making the divergence fail loudly instead of silently. The vector columns are a fixed size (the schema is the storage authority); `database.validate_embedding_dimension()` introspects the actual `knowledge_nodes.embedding` dimension (pgvector `atttypmod`) and the api lifespan checks it against `settings.embedding.dimensions` at startup, raising a clear error if they diverge. So changing the config without a matching column migration stops the app at boot rather than corrupting writes. Verified: introspection returns 1536; matching config passes, mismatched raises.

The schema fixes every vector column at `vector(1536)` (`database.py:23,42,119,…`), while the embedding dimension is a runtime setting (`settings.embedding.dimensions`, used in `pipeline.py:342` and `retrieval.py:18`). If anyone changes the configured dimension, inserts will fail or silently mismatch the column. The DDL and the config must be derived from one source of truth.

### B6. `_message_text` / `getattr(resp.content[0], "text", "")` assumes a text block exists
**Severity: Low** — ✅ **Resolved 2026-06-10**

> Fixed: added a shared `message_text()` helper (`kb/common.py` for the API, `sources/base.py` for the worker) that iterates content blocks, returns the concatenated text of all *text* blocks, and yields `""` for empty content / tool-use / refusal responses — no `content[0]` IndexError and no reading a non-text block. Replaced all 13 call sites (`kb/public`, `kb/summary`, `kb/retrieval`, `kb/graph`, `kb_tools`, `maintenance/{entity,index}_ops`, `pipeline.py`, `sources/{pdf,image}`). Verified against empty/None/non-text/multi-block inputs.

Throughout `kb/public.py` (e.g. `compare` line 734, `cite` line 863) and `pipeline.py:48`, the code reads `resp.content[0]` without checking the block type or that `content` is non-empty. A stop for `max_tokens`, a tool/refusal block, or an empty response yields an `IndexError` or an empty string silently treated as a valid answer. Guard for empty content and non-text blocks.

### B7. ivfflat indexes are created on empty tables and never retrained
**Severity: Low (recall quality)** — ✅ **Resolved 2026-06-10**

> Fixed: Alembic `0003` drops the three ivfflat vector indexes and recreates them as **HNSW** (`USING hnsw (… vector_cosine_ops)`). HNSW needs no centroid training, is built incrementally as rows are inserted, and gives better recall as the corpus grows — no empty-table problem and no periodic REINDEX. Verified: after `upgrade head` all three indexes report `amname = hnsw`.

`idx_knowledge_nodes_embedding` and the summary-vector indexes are created `WITH (lists = 100)` at first boot when the tables are empty (`database.py:448, 469-472`). ivfflat builds its centroids from existing rows, so an index built on an empty/tiny table gives poor recall and is never rebuilt as data grows. Consider building the index after initial load, periodic `REINDEX`, or HNSW.

---

## C. System / architecture design problems

### C1. The entire schema + data migrations run on every process startup
**Severity: High (operational risk)** — ✅ **Resolved 2026-06-10 (Alembic)**

> Fixed: introduced Alembic. `database.init()` now only opens the pool; the schema lives in `services/api/alembic/versions/0001_baseline.py` (final-form DDL adopted from the old `SCHEMA_SQL`, with one-time backfills and legacy ALTER/DROP/rename steps removed and the 3 ALTER-only columns folded in). Migrations run via `alembic upgrade head` from the api container entrypoint (`docker-entrypoint.sh`), gated on `RUN_MIGRATIONS=1` which is set **only on the api service** — so api is the single migrator and the api+job-worker concurrent-DDL race is gone.
>
> **Prod adoption (no manual stamp needed):** the baseline is idempotent (`CREATE … IF NOT EXISTS`, guarded constraint), so on the existing DB `alembic upgrade head` no-ops the DDL and just records revision `0001_baseline`. Verified end-to-end against ephemeral pgvector: fresh build (17 tables/58 indexes/23 FKs/constraint), double-apply idempotency, real `alembic upgrade head` stamping, and the exact cutover (pre-built schema + a data row, no `alembic_version`) → DDL no-ops, **data preserved**, version stamped.
>
> Future schema changes: `alembic revision -m "…"` → edit `upgrade()/downgrade()` → ships via the same entrypoint. The `;`-split fragility and per-boot graph re-scans are gone.

`database.init()` (`database.py:504-510`) executes a ~500-statement `SCHEMA_SQL` blob plus a long sequence of imperative migrations on **every** API and job-worker start. Concerns:

- **No migration versioning.** There's no schema-version table; idempotency is hand-maintained via `IF NOT EXISTS` / `IF EXISTS` and "this runs to zero rows on re-exec" comments. Several statements are genuinely *data-mutating* on every boot, e.g. `DELETE FROM knowledge_edges WHERE relation_type='summarizes'` (`database.py:311`), edge de-duplication (`database.py:315-322`), and multiple backfills (`database.py:739-842`). These re-scan the whole graph on each restart.
- **Naïve `;` splitting.** `init()` splits `SCHEMA_SQL` on `";"` (`database.py:507`); any future statement containing a semicolon inside a string literal or a `DO $$ … $$` block will break. The code already had to pull the conditional `DO`-style logic out into Python because of this.
- **Concurrent DDL.** Both the `api` (lifespan) and `job-worker` run `init()`. `depends_on: service_healthy` orders the first boot, but on a simultaneous restart both can run DDL/backfills concurrently, risking lock contention or duplicate backfill work.

Move to a real migration tool (Alembic) with versioned, run-once migrations, and have exactly one component own schema management.

### C2. The ingestion worker drives the pipeline through ~10 unauthenticated HTTP round-trips per item
**Severity: Medium** — 🟡 **Security hardened 2026-06-10; architecture deferred**

> The dangerous half — the internal endpoints being open — is fixed. The worker-facing `/api/kb/*` write endpoints (`ingest`, `entity_candidates/analyze_context|process|{id}/mark_promoted`, `entities/{id}/backfill_wikilinks`, `entities/refresh_stale`) now require `require_auth_or_service_token`, and the worker presents `KB_SERVICE_TOKEN` on every API call. Notably `/api/kb/ingest` was an unauthenticated **write** open to the internet (anyone could inject nodes / drive LLM cost) — now closed. (This also fixed a latent 401 on the worker's already-gated `/api/kb/node/{id}` call.)
>
> **Deferred (not done):** the chattiness / lack of cross-step transactionality is inherent to the worker being a separate service that talks HTTP. Eliminating it means a real rearchitecture (shared in-process DB/service layer or a single batched ingest call) — high-risk and out of proportion to a Medium finding on a working system. Tracked for a future dedicated effort.

For each article, `pipeline.py` calls the API over HTTP for analysis context, candidate processing, node fetch, candidate promotion, wikilink backfill, ingest, and status updates (`pipeline.py:103-435`). This is chatty (network N+1), gives no cross-step transactionality (a failure mid-way leaves partial state — article ingested, candidates half-processed), and depends on the internal endpoints being open (see A3). For a co-located worker, calling a shared service/DB layer in-process (or a single batched ingest call) would be simpler, atomic, and secure.

### C3. Hard-coded single tenant, but `user_id` is threaded everywhere
**Severity: Low (design debt)** — ✅ **Resolved 2026-06-10 (decision recorded)**

> Resolved as a documented decision rather than a rip-out. Ripping `user_id` out of every table/query (or wiring real per-user authz) is a large, risky change for low-severity debt on a live system. Instead, `docs/adr/0001-single-tenant.md` records that single-user is intentional, that `user_id` is forward-compat scaffolding **not** an isolation boundary, and that access control is the shared password + service token. This removes the "false sense of isolation" by making the non-guarantee explicit; the columns remain to make a future multi-tenant migration tractable.

`USER_ID = "default"` is hard-coded in `kb/common.py:4`, `routers/files.py:18`, `routers/sources.py:25`, `pipeline.py:38`, etc., and the JWT subject is the constant string `"user"` (`auth.py:21,27`). Meanwhile every table carries a `user_id` column and queries filter on it. The schema is shaped for multi-tenancy that the app neither provides nor enforces. Either commit to single-user and drop the ceremony, or actually derive `user_id` from the token. As-is, the `user_id` filters give a false sense of isolation (e.g. `_fetch_one` checks `node.user_id != USER_ID` against a constant).

### C4. Service-token scope is returned but never enforced
**Severity: Low** — ✅ **Resolved 2026-06-10**

> The misleading `scope: "kb:read"` was actively wrong after A3 (the same token now legitimately writes during ingest). Fixed by making the model honest: the service token is a single trusted-internal credential (`scope: "service"`), and the **real** enforcement boundary is documented in `require_auth_or_service_token` — endpoints reachable by the service use it; user-only/destructive endpoints (delete/merge/settings) use cookie-only `require_auth`, which I verified they already do. Added a "don't attach this to a destructive endpoint" note so the boundary isn't eroded later. `test_auth.py` updated.

`verify_service_token` returns `{"scope": "kb:read"}` (`auth.py:53`) but no endpoint inspects the scope. Today `require_auth_or_service_token` is only attached to read endpoints, so the invariant holds by convention; a future mutating endpoint that reuses the same dependency would silently accept a read-only service token. Enforce the scope where it matters.

### C5. Workers run only under a compose profile
**Severity: Low (footgun)** — ✅ **Resolved 2026-06-10 (documented)**

> Kept the profile (separate worker lifecycle is intentional) but made the footgun impossible to miss: a prominent header comment in `docker-compose.yml` and a ⚠️ callout in `docs/VPS.md` both state that `--profile workers` is required or ingestion/jobs silently don't run, and that `deploy.sh`/`Makefile` already pass it.

`ingestion-worker` and `job-worker` are gated behind `profiles: ["workers"]` (`docker-compose.yml`). `deploy.sh` and the `Makefile` correctly pass `--profile workers`, but a plain `docker compose up` starts only api/web/postgres/nginx — uploads never get ingested and summary/maintenance jobs never run, with no visible error. Document this prominently or make the workers part of the default stack.

### C6. `published_at` fallback logic is duplicated and inconsistent
**Severity: Low** — ✅ **Resolved 2026-06-10**

> Fixed the sort-vs-display divergence. The SQL authority everywhere is `COALESCE(published_at, ingested_at, created_at)` (`KNOWLEDGE_TIME_SQL`), but the Python `_published_at` also consulted `effective_at/source_published_at/captured_at`. Those are redundant: `ingest.py` sets `published_at = effective_at or source_published_at or captured_at` at write time, so the column already folds them in. Simplified `_published_at` to `published_at | ingested_at | created_at` so fetch/display matches search/sort, with a comment marking it the single source of truth.

"Effective publish time" is computed in at least three places with different precedence: SQL `COALESCE(n.published_at, n.ingested_at, n.created_at)` (`public.py:77`), the Python `_published_at` (`public.py:81-89`, which also consults `effective_at`/`source_published_at`/`captured_at`), and the DDL backfill (`database.py:569-575`). The same node can sort by one rule in search and display another rule in fetch. Centralize the precedence in one helper/generated column.

---

## D. Smaller issues / nits — ✅ **All resolved 2026-06-10**

- ✅ **`datetime.utcnow()` (deprecated, naïve)** — replaced every occurrence (`pipeline.py`, `sources/rss.py`, `sources/base.py` `RawItem.fetched_at` factory) with `datetime.now(timezone.utc)`. None remain in `services/`.
- ✅ **Raw-file name collisions** — `save_raw`'s fallback name is now content-addressed (`{date}-{sha256(raw_bytes)[:12]}.html`), so distinct items can't clobber each other and identical content re-saves to the same path (idempotent). The named-file/URL paths were already unique.
- ✅ **`list_sources` count query** — added `AND source_id IS NOT NULL`, dropping the unused `NULL` bucket and skipping source-less nodes (entities/summaries/indices) in the scan.
- ✅ **`update_source` PUT** — `last_fetched_at` is now normalized (`Z`→`+00:00`) and a bad value returns **400** (was a 500); the parsed value is reused for the connectors update.
- ✅ **Code-fence / JSON parsing** in `analyze_article` — replaced the "take the first ``` block" heuristic with a defensive `_extract_json_object()` (mirrors the cite-path parser): strips fences, then falls back to first-`{`…last-`}`. Verified it now recovers JSON even when the model emits an unrelated fenced block first.
- ✅ **Error-size policy** — `update_source_item_status` now caps the stored error at 4000 chars, matching `fail_job`.

---

## Suggested priority

1. ~~**A1 + A2** (unauthenticated file read/write/delete + traversal)~~ — ✅ done.
2. ~~**A3** (unauthenticated source mutations) and **A4** (CORS)~~ — ✅ done.
3. ~~**C1** (replace boot-time migrations with versioned migrations)~~ — ✅ done (Alembic).
4. ~~**B1/B2** (job-queue idempotency + stuck-job recovery), **B3** (lost ingestion triggers)~~ — ✅ done.
5. ~~**B4** (wiki frontmatter escaping)~~ — ✅ done. Then the remaining design-debt items.
