# KnowledgeBase-S — Code Review Findings (2026-07-06)

Review scope: `services/api`, `services/ingestion-worker`, `services/kb-mcp`, infra (`docker-compose.yml`, `nginx/`), Alembic migrations.
Previous review: `docs/CODE_REVIEW_FINDINGS.md` (2026-06-09, all items A1–D resolved). This document contains **new** findings only; none below duplicate a resolved item, though S1 was explicitly flagged as an unresolved follow-up in old-A3.

Ordered by severity within each section. Each item ends with a **Fix** note written for a follow-up agent (Opus/Sonnet) to implement.

---

## S. Security

### S1. Multiple KB read endpoints are publicly reachable with no auth — full knowledge-base dump
**Severity: Critical**

nginx proxies `/api` straight to the API (`nginx/nginx.conf` `location /api`), and these handlers have **no auth dependency at all** (several even say "No auth required" in their docstrings):

- `GET /api/kb/graph` — [internal.py:304](../services/api/kb/internal.py)
- `GET /api/kb/nodes` — [internal.py:381](../services/api/kb/internal.py) (titles, abstracts, tags, paginated — trivially dumps everything)
- `GET /api/kb/graph/all` — [internal.py:461](../services/api/kb/internal.py)
- `GET /api/kb/wiki/status` — [internal.py:555](../services/api/kb/internal.py)
- `GET /api/kb/entities/{id}/facts`, `/timeline`, `/related` — [entity.py:201-224](../services/api/kb/entity.py)
- `GET /api/kb/indices/{id}`, `/children`, `/descendants`, `GET /api/kb/objects/{id}/parents`, `/ancestors` — [index_ops.py:196-263](../services/api/kb/index_ops.py)

An anonymous internet caller can enumerate every node, edge, entity fact and abstract. This was noted as "follow-up needed" in old finding A3 but never completed.

**Fix:** Add `_: dict = Depends(require_auth_or_service_token)` to every listed handler (or `dependencies=[...]` on the routers, then explicitly relax the few endpoints the worker needs). Add a test that walks `app.routes` and asserts every route under `/api/` except `/api/health`, `/api/auth/login`, `/api/config/doc_kind` declares an auth dependency — this prevents the same regression a third time.

### S2. Unauthenticated POSTs that spend LLM money and mutate summaries
**Severity: Critical**

- `POST /api/kb/nodes/{id}/create_summary` — [summary.py:302](../services/api/kb/summary.py)
- `POST /api/kb/nodes/{id}/revise_summary` — [summary.py:331](../services/api/kb/summary.py)

No `Depends(require_auth)`. Anyone can enqueue unlimited Claude jobs (cost abuse / job-queue flooding), and `revise_summary` lets an anonymous caller **rewrite summary content** with arbitrary instructions (stored-content poisoning of the KB that MCP clients then consume).

**Fix:** Add `Depends(require_auth)` to both (they are user-facing, not worker-facing). Covered by the route-walking test from S1.

### S3. Login rate-limit keyed on the wrong IP behind Cloudflare; unbounded attempt dict
**Severity: Medium**

`_login_ip` ([main.py:88](../services/api/main.py)) prefers `X-Real-IP`, which nginx sets from `$remote_addr`. Behind Cloudflare (the deployment uses CF Flexible per nginx comments), `$remote_addr` is a Cloudflare edge IP: an attacker's requests spread across many edge IPs (throttle diluted), and legitimate users can be locked out by a stranger sharing their edge. Also `_login_attempts` ([auth.py:19](../services/api/auth.py)) never evicts keys — each unique IP string leaves an empty list entry forever (slow memory leak, and an attacker can spray junk `X-Forwarded-For` values if any path trusts it).

**Fix:** In nginx use the `ngx_http_realip_module` with `set_real_ip_from` Cloudflare ranges + `real_ip_header CF-Connecting-IP`, or read `CF-Connecting-IP` first in `_login_ip`. Evict empty lists in `login_rate_limited` (`if not attempts: _login_attempts.pop(ip, None)`).

### S4. Uploaded filename is embedded unsanitized into the storage path
**Severity: Medium**

`upload_to_folder` ([folders.py:343](../services/api/routers/folders.py)) and `upload_to_source` ([sources.py:796](../services/api/routers/sources.py)) build `safe_name = f"{date}-{hex}-{file.filename}"` and write `raw_dir / safe_name`. A filename containing `/`, `\` or `..` segments changes the target path (on Linux it usually just errors with `FileNotFoundError` → 500; on a Windows dev host traversal out of `raw_dir` is possible). It also breaks the `origin_ref = upload://{safe_name}` uniqueness assumption.

**Fix:** Use `Path(file.filename or "upload").name` before composing `safe_name`, and reject empty results.

### S5. Cloudflare Flexible = plaintext origin traffic; cookie lacks `secure`
**Severity: Medium (deployment posture, informational)**

nginx listens on port 80 only and the comment in `nginx.conf` confirms Cloudflare **Flexible** mode. The CF→origin hop carries the shared password and auth cookie in cleartext across the public internet. The login cookie ([main.py:111](../services/api/main.py)) is also set without `secure=True`.

**Fix:** Move to CF Full (strict) with an origin certificate (or at least CF Origin CA cert on nginx 443), then set `secure=True` on the cookie. This is config work, not code work, but record it.

---

## B. Correctness bugs

### B1. Two writers produce conflicting wiki article files; `rebuild_wiki` destroys full text
**Severity: High (data loss)**

The article wiki file (`wiki/articles/{id}.md`) is the **only** store of the cleaned full text (DB holds just the abstract), and it has two incompatible writers:

1. Worker: `write_wiki_article` ([pipeline.py:483](../services/ingestion-worker/pipeline.py)) — full cleaned text, frontmatter keys `raw_ref` + `doc_kind`.
2. API: `write_wiki_node` ([wiki.py:42](../services/api/kb/wiki.py)) — body = `node["abstract"]` only, frontmatter keys `storage_key`, **no `doc_kind`**.

Consequences:
- **Race on every ingest:** `POST /api/kb/ingest` schedules `build_similar_edges_and_wiki` as a background task ([ingest.py:591](../services/api/kb/ingest.py)) which calls `write_wiki_node`; the worker writes the full-text file right after `post_ingest` returns ([article_ingestion.py:126](../services/ingestion-worker/article_ingestion.py)). Both target the same path over the shared `user_data` volume. Whichever writes last wins — the API side often runs later (it does a vector query first), so articles can end up **abstract-only**, silently breaking `fetch` body, `cite` quote verification, and `compare` fallback.
- **`rebuild_wiki` job** ([wiki.py:187](../services/api/kb/wiki.py), enqueued from `/api/kb/wiki/rebuild`) iterates *all* nodes and overwrites every article file with the abstract-only rendering — one click permanently destroys all full text (recoverable only via `rebuild_from_raw` re-processing, i.e. re-paying all LLM/embedding costs).
- **Frontmatter drift** breaks `restore_from_wiki` ([restore.py:159](../services/api/maintenance/restore.py) reads `storage_key`; worker files have `raw_ref`) → restored articles lose their raw-file linkage; API-written files lack `doc_kind` → restore defaults everything to the default doc_kind.

**Fix (suggested design):** make the worker the sole writer of article *bodies*. In `write_wiki_node`, when `object_type == "article"` and the file already exists, parse the existing file, update only the frontmatter (merged to one canonical key set: include `doc_kind` and `storage_key`), and preserve the existing body; only write the abstract as body when no file exists. Align `write_wiki_article` to the same frontmatter keys, and update `restore_from_wiki` to read both `storage_key` and legacy `raw_ref`. Add a regression test: ingest → rebuild_wiki → assert article body unchanged.

### B2. `maintenance-worker` compose command points at a nonexistent file
**Severity: High (feature dead)**

`docker-compose.yml:135` runs `command: python maintenance.py`, but there is no `services/api/maintenance.py` — the package is `maintenance/` with a `__main__.py`. The container exits immediately on every `--profile maintenance` run.

**Fix:** change to `command: python -m maintenance` (and check `docker-compose.dev.yml` for the same mistake).

### B3. Maintenance-path entity promotion sends an empty embedding — always fails
**Severity: High (feature dead)**

`promote_entity_candidates` ([entity_ops.py:88](../services/api/maintenance/entity_ops.py)) POSTs to `/api/kb/ingest` with `"embedding": []`. `do_ingest` renders `'[]'::vector`, which Postgres/pgvector rejects ("vector must have at least 1 dimension") → 500. The exception handler swallows it (`ingest_resp.json().get("id")` → `None` path), so the maintenance fallback promotion silently never promotes anything. (Ingestion-time promotion in the worker embeds `canonical_name` correctly — that's why this went unnoticed.)

**Fix:** compute a real embedding (`embed(canonical_name)`, mirroring [article_ingestion.py:184](../services/ingestion-worker/article_ingestion.py)) before the POST; also check `ingest_resp.status_code` and skip the `mark_promoted` update when the ingest failed, instead of proceeding with `entity_node_id=None`.

### B4. Job reclaim timeout (900s) is shorter than the longest jobs — concurrent duplicate execution
**Severity: High**

`reclaim_stuck_jobs` requeues any job `running` longer than `JOB_STUCK_TIMEOUT_SECONDS` (default 900s, [job_worker.py:13](../services/api/job_worker.py)), with a docstring saying the timeout "must exceed the longest expected job duration". But `rebuild_from_raw` deliberately polls up to `rebuild_max_wait_seconds = 3600` ([settings.py:88](../services/api/settings.py), [restore.py:574](../services/api/maintenance/restore.py)), and `rebuild_wiki`/`run_maintenance` over a large KB can also exceed 900s. At 15 minutes the still-running job is flipped to `retrying`; with a single worker it can't be picked up until the first finishes, but a second job-worker replica (or the first finishing another job) starts a **second concurrent rebuild** — node deletion + re-ingest racing with itself.

**Fix:** either raise the default well above the longest job (e.g. 2× `rebuild_max_wait_seconds`), or better: add a heartbeat column (`jobs.heartbeat_at`, updated periodically by long-running job handlers) and reclaim on stale heartbeat instead of `started_at`. Also consider retry backoff: `fail_job` leaves the job in `retrying`, and `claim_next_job` picks it up immediately — a deterministic failure burns all attempts within seconds.

### B5. Worker's connector fetch always 401s (wrong client + cookie-only endpoint)
**Severity: Medium (dead code / silent failure)**

`fetch_connectors` ([ingestion-worker/main.py:84-93](../services/ingestion-worker/main.py)) uses a bare `httpx.AsyncClient()` **without** `_service_headers()`, and `GET /api/connectors` ([folders.py:743](../services/api/routers/folders.py)) requires the auth *cookie* (`require_auth`), so the call always returns 401 and the connectors loop in `run_once` is dead. It only "works" because `create_connector` also flips the legacy source to `fetch_mode='subscription'`, which the `/api/sources` path picks up.

**Fix:** decide which mechanism is canonical. Either delete the connectors loop from `run_once` (simplest, matches current behavior), or change `list_connectors` to `require_auth_or_service_token` and pass `_service_headers()` in the worker. Note: today an `inactive` connector keeps syncing because the legacy source stays `subscription` — if connectors are kept, `update_connector(status='inactive')` should also flip the source's `fetch_mode`.

### B6. Re-adding an existing URL to a folder orphans a document_instance and duplicates raw_assets
**Severity: Medium**

`add_url_to_folder` ([folders.py:417](../services/api/routers/folders.py)) always generates fresh `si_/ra_/di_` ids, inserts the raw_asset and document_instance unconditionally, then upserts `source_items` on `(user_id, source_id, origin_ref_type, origin_ref)`. When the URL already exists, the source_item conflict-updates `document_instance_id` to the *new* di — the **old** document_instance row remains forever (shown in folder contents, stuck `pending`/stale status), and a duplicate raw_asset is created each time. Same latent pattern in `upload_to_folder` (masked only by the random hex in `origin_ref`).

**Fix:** before inserting, look up the existing source_item by `(source_id, origin_ref_type, origin_ref)`; if present, reuse its `document_instance_id`/`raw_asset_id` (reset statuses to pending) instead of minting new rows. Wrap the three inserts per URL in a transaction (see A1 below).

### B7. `copy_document_instance` produces a copy that can never be processed, and hard-delete of the original destroys the copy's raw file
**Severity: Medium**

The copy ([folders.py:567](../services/api/routers/folders.py)) creates a `document_instances` row with `status='pending'` but **no corresponding source_item**, so the ingestion worker never sees it; `/reprocess` resets `source_items WHERE document_instance_id = :id` — zero rows — then triggers a no-op. The copy sits "pending" forever and never gets an article. Additionally the copy shares `raw_asset_id` with the original, and `_hard_delete_document_instance` → `_delete_di_files` ([folders.py:609](../services/api/routers/folders.py)) unlinks the shared `storage_key` file — hard-deleting the original silently destroys the copy's underlying file.

**Fix:** on copy, also create a source_item (new `si_` id, origin_ref suffixed to avoid the unique conflict, pointing at the same raw snapshot) — or explicitly define copy as "reference-only" and set its status to the original's status instead of `pending`. In `_delete_di_files`, skip files whose raw_asset is still referenced by another document_instance (`SELECT 1 FROM document_instances WHERE raw_asset_id = ... AND id <> ...`).

### B8. A folder whose items were hard-deleted can never be deleted
**Severity: Medium**

`delete_folder` ([folders.py:238](../services/api/routers/folders.py)) refuses when `COUNT(*) FROM document_instances WHERE folder_id = :fid` > 0 — but hard-deleted items remain as `status='deleted'` tombstone rows (intentionally, to prevent feed resurrection), so the count never reaches zero.

**Fix:** count only `status NOT IN ('deleted')` (consider whether `ignored` should block deletion too), while keeping tombstones themselves.

### B9. `PATCH /api/sources/source-items/{id}` with an empty body nulls out doc_kind
**Severity: Low**

`update_source_item` ([sources.py:621](../services/api/routers/sources.py)) unconditionally executes `SET doc_kind = :doc_kind` even when `body.doc_kind is None`, so a PATCH without the field clears the stored value (and propagates NULL to `document_instances` and `knowledge_nodes`). PATCH semantics should mean "no change".

**Fix:** return 400 when `body.doc_kind is None`, or skip the update entirely.

### B10. `restore_from_wiki` / worker frontmatter key mismatch (see B1) plus lossy fallbacks
**Severity: Low (covered by B1's fix)**

Beyond B1: `restore.py:53` regex-replaces curly quotes globally in frontmatter *values* (corrupts legitimate content), and mentions-edge recovery only scans `[[ent…]]` links, so entities whose wikilink backfill never ran (see B12) restore with no mentions.

### B11. `summarize_corpus` fallback may crash on empty exclusion list
**Severity: Low (verify)**

[public.py:965](../services/api/kb/public.py): `AND NOT (n.id = ANY(:exclude_ids))` binds a plain Python list; when `seen` is empty (the common cold-start case: no summary hits), asyncpg may fail to infer the array type of an empty list (`cannot determine type of empty array` / `$n` type error). Other call sites defensively use `CAST(:ids AS text[])` or `or ["__none__"]` ([restore.py:423](../services/api/maintenance/restore.py)) — this one doesn't.

**Fix:** use `AND NOT (n.id = ANY(CAST(:exclude_ids AS text[])))` (and verify with an integration test against a cold DB).

### B12. `backfill_wikilinks_for_entity` usually no-ops because the title lives in frontmatter
**Severity: Medium (feature mostly dead)**

[entity_ops.py:177-192](../services/api/maintenance/entity_ops.py): for each term it takes the **first** occurrence in the whole file; if that occurrence is inside frontmatter it `continue`s to the next term rather than searching after the frontmatter. Since the article title almost always contains the entity name and is in frontmatter, the body link is rarely injected — so wikilink backfill, and the mentions-edges `restore_from_wiki` derives from those links, silently underperform. Also: substring matching without word boundaries (bad for short Latin names), and `modified.find("---", 3)` matches any `---` substring, not a frontmatter delimiter line.

**Fix:** split the document with `kb.common.split_frontmatter`, search only the body, re-assemble; iterate matches (not just the first per file) or at least first-in-body; use the line-based delimiter logic.

### B13. Concurrent pipeline runs for the same source double-process items
**Severity: Medium**

Every `/trigger/{source_id}` spawns a new asyncio task ([ingestion-worker/main.py:62](../services/ingestion-worker/main.py)); nothing prevents two tasks (trigger + poll loop, or double-click in UI) from fetching the same `pending` list before either marks items `processing` — duplicate LLM analysis + embedding spend (node dedup prevents duplicate rows, but money is burned and entity mention counting relies on secondary guards).

**Fix:** per-source in-process lock (`asyncio.Lock` keyed by source_id) around `_dispatch_pipeline`, or claim items atomically server-side (`UPDATE ... SET status='processing' WHERE status='pending' RETURNING ...` endpoint) instead of list-then-update.

### B14. `trim_raw_files` silently deletes raw files still referenced by the DB
**Severity: Medium (data loss)**

[ingest.py:360](../services/api/kb/ingest.py) deletes oldest files under `raw/` beyond a hardcoded 512 MB cap (`RAW_CAP_BYTES`, [ingest.py:35](../services/api/kb/ingest.py) — also violates the "no hardcoded params" convention). `raw_assets.storage_key` / `source_items.raw_snapshot_ref` keep pointing at the deleted files, so `rebuild_from_raw` and `/reprocess` for those items fail later with no warning. Items with `raw_retention_policy='keep_raw'` (uploads — the *only* copy of the file) are trimmed just like cached HTML.

**Fix:** move the cap to `system.yaml`; exempt files whose source_item has `raw_retention_policy='keep_raw'`; when deleting, mark the affected raw_assets/source_items (e.g. `raw_snapshot_ref = NULL`) so later rebuilds know the raw is gone.

### B15. `rebuild_from_raw` deletes entities shared with out-of-scope articles
**Severity: Medium (data loss)**

[restore.py:426-443](../services/api/maintenance/restore.py) deletes every entity that has a fact/mention from any rebuilt article — even if that entity is also mentioned by articles **not** being rebuilt. `DELETE FROM knowledge_nodes` cascades its entity_facts and edges for those other articles. The entity is only re-created if the rebuilt source re-promotes it, and the other articles' facts are gone.

**Fix:** restrict entity deletion to entities whose facts/mentions come **exclusively** from `base_ids` (`NOT EXISTS` a fact/edge from an article outside the set), or don't delete entities at all and rely on `abstract_stale` refresh.

---

## A. Architecture / design pitfalls

### A1. No transactions around multi-statement invariants
**Severity: High (systemic)**

Almost every multi-step write runs as separate autocommit statements: `do_ingest` (knowledge_nodes + object sub-table + index_children — a crash leaves a node without its sub-table row, [ingest.py:264-318](../services/api/kb/ingest.py)); `create_folder` (folder + legacy source, [folders.py:135](../services/api/routers/folders.py)); `create_connector` (folder + source + connector); upload/add-url (raw_asset + document_instance + source_item); `_hard_delete_document_instance` (summaries + article + tombstones); `do_merge_entities` ([entity.py:76](../services/api/kb/entity.py) — edge transfer/delete/tombstone unprotected mid-crash). Only `do_delete_entity` uses `database.transaction()`. The ID-suffix mapping convention (`fld_↔src_`, `si_↔di_↔ra_`) makes partial failure especially painful because later code *assumes* the twin row exists (`upload_to_folder` 500s with "资料夹缺少 legacy source").

**Fix:** wrap each of the listed operations in `async with database.database.transaction():`. This is mechanical and low-risk; prioritize `do_ingest`, folder/connector creation, and the hard-delete path.

### A2. Single event loop in the ingestion worker runs blocking calls
**Severity: Medium**

The worker mixes async orchestration with synchronous blockers: the **sync** Anthropic client (`claude.messages.create`, [pipeline.py:311](../services/ingestion-worker/pipeline.py)), `trafilatura.fetch_url` ([pipeline.py:246](../services/ingestion-worker/pipeline.py)), PDF/image parsing, and file IO all block the loop, freezing the `/trigger` HTTP server and the poll loop for the duration of each LLM call (tens of seconds). This also widens the B13 race windows.

**Fix:** use `anthropic.AsyncAnthropic` (mirroring the API service), and wrap trafilatura/parsing in `asyncio.to_thread(...)`. Alternatively run the pipeline in a thread/process executor and keep the trigger server responsive.

### A3. `write_wiki_node` vs `write_wiki_article` duplication is the root cause of B1
**Severity: Medium (design)**

Two services own the same file format with different frontmatter schemas and different bodies. Long-term: extract a single wiki-serialization module (shared package or move article-file writing fully server-side, with the worker passing the full text in the ingest payload — it's already sending everything else). That would also eliminate the shared-volume coupling between worker and API for wiki writes.

### A4. Folder tree integrity is unenforced
**Severity: Medium**

`FolderCreate.parent_id` is inserted unvalidated ([folders.py:143](../services/api/routers/folders.py) — FK saves it only if the id exists; a bogus id 500s instead of 400) and `update_folder` accepts any `parent_id` with **no existence check and no cycle detection** — moving A under B under A makes the tree a cycle; every tree-rendering consumer (`GET /api/folders` builders in the UI) loops or drops the subtree. Compare: `add_child` for index nodes does proper cycle detection ([graph.py:325](../services/api/kb/graph.py)).

**Fix:** in `update_folder`, verify the target exists, is active, and is not the folder itself or any of its descendants (recursive CTE like `_would_create_cycle`).

### A5. Duplicate default summaries; summary generated from abstract, not full text
**Severity: Medium (quality)**

- `generate_summary_job` mints a random `sum_` id every run ([summary.py:99](../services/api/kb/summary.py)); the job idempotency key only blocks *concurrent* duplicates, so triggering "create summary" twice yields two `is_default=true` summaries for the same article. Search dedup and `_load_doc_context` (`ORDER BY is_default DESC ... LIMIT 1`) then pick arbitrarily. The ingest path avoids this with a deterministic id (`_make_node_id` on `summary_of`+perspective) — the job path should reuse it.
- The prompt body is `(source["abstract"] or "")[:3000]` ([summary.py:81](../services/api/kb/summary.py)) — the LLM summarizes the abstract, not the article. `read_wiki_body` is available and should feed the prompt.

### A6. Unbounded LLM/API retry surface on repeated pipeline runs
**Severity: Low**

`update_source_item_status(..., 'processing')` increments `attempts` ([sources.py:557](../services/api/routers/sources.py)) but nothing ever checks `attempts` against a max for source_items — a permanently failing item (e.g. paywalled URL) is retried on every manual trigger forever. Consider skipping items with `attempts >= N` unless explicitly retried.

### A7. `GET /api/settings/export` blocks the event loop and leaks temp files
**Severity: Low**

[app/settings.py:73-97](../services/api/app/settings.py): `tempfile.mktemp` (deprecated, race-prone), synchronous `shutil.make_archive`/`zipfile` of a potentially multi-GB `user_data` inside the async handler (blocks all requests), and the zip is never deleted after the response (container `/tmp` grows on every export).

**Fix:** `mkstemp`, `asyncio.to_thread` for the archive build, and `BackgroundTask(os.remove, path)` on the `FileResponse`.

### A8. Assorted smaller items
- **Stale comment:** [ingest.py:4](../services/api/kb/ingest.py) says "no auth required" — the routes now use `require_auth_or_service_token`. Update the docstring (it invites regressions).
- **Duplicate embedding call:** [article_ingestion.py:108](../services/ingestion-worker/article_ingestion.py) — `summary_embedding = await adapters.embed(abstract)` recomputes the exact same embedding as `embedding` when `abstract` is non-empty; reuse it.
- **N+1 queries:** `list_folders` count-per-folder ([folders.py:126](../services/api/routers/folders.py)) — one `GROUP BY` query; `get_graph` BFS does per-node queries (acceptable at depth ≤3, note only).
- **ILIKE pattern injection:** search endpoints interpolate the raw query into `%…%` patterns without escaping `%`/`_` ([public.py:183](../services/api/kb/public.py), [internal.py:400](../services/api/kb/internal.py)) — self-DoS/odd results only in single-user context; escape with `re.sub(r'([%_\\])', r'\\\1', q)`.
- **Service token can rewrite source config:** `PUT /api/sources/{id}` accepts the service token and updates `config` (the URL the worker fetches). The worker only needs `last_fetched_at`. Consider splitting a `/last-fetched` endpoint for the token and cookie-gating full updates.
- **`sync_connector` ignores connector status** ([folders.py:851](../services/api/routers/folders.py)) — syncing an `inactive` connector still triggers the fetch (see also B5's fetch_mode note).
- **`verify_password` strips the password** ([auth.py:26](../services/api/auth.py)) — a password with leading/trailing whitespace can never authenticate exactly; strip at the UI, not in the comparison.
- **`promote_entity_candidates` promotion criteria drift:** the maintenance path omits the `max_salience >= promotion_max_salience` branch that ingest-time promotion has ([entity_ops.py:38](../services/api/maintenance/entity_ops.py) vs [ingest.py:530](../services/api/kb/ingest.py)); extract one shared predicate.
- **`document_instances` status comment** in Alembic 0001 omits `deleted` (docs drift with the tombstone design).

---

## Suggested priority for the fixing agent

1. **S1 + S2** (add auth; add the route-walk test) — small diff, closes the public data leak and LLM cost hole.
2. **B2** (compose command) — one-line fix, re-enables maintenance profile.
3. **B1 / A3** (wiki write ownership) — highest data-loss risk; do the "preserve existing article body + unify frontmatter" fix first, full refactor later.
4. **B3, B4, B12** — restore the dead maintenance paths and job safety.
5. **A1** (transactions), **B6–B8** (folder/di lifecycle), **B14/B15** (data-loss edges).
6. Remaining Medium/Low items opportunistically.

When fixing, follow the repo conventions: params go to `config/system.yaml` (update **both** `settings.py` files), schema changes are new Alembic revisions, and each fix should land with a test in `services/api/tests/` (auth walk test, wiki rebuild round-trip, folder lifecycle).
