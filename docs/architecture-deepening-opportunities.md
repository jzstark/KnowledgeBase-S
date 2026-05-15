# Architecture Deepening Opportunities

Captured on 2026-05-15 after running `/improve-codebase-architecture`.

The repo currently has no root `CONTEXT.md` or `docs/adr/`, so these notes use
the existing project language from `docs/architecture-notes.md`: article, entity,
summary, index, wikilink, source item, and maintenance.

These are candidate refactors for future discussion. They are not approved
implementation decisions.

## 1. Ingestion pipeline module

Files:

- `services/ingestion-worker/pipeline.py`
- `services/ingestion-worker/main.py`

Problem:

Regular ingestion and book ingestion both interleave source item state,
extraction, LLM calls, embedding, knowledge object writes, wiki writes, entity
promotion, and backfill. This is a shallow Module: callers and maintainers must
understand almost the whole Implementation to change one step.

Candidate direction:

Deepen the ingestion Module around "process one source item into knowledge
effects", with source-specific Adapters only responsible for producing extracted
text or chapters.

Expected benefit:

Better Locality for source item status, article/summary creation, and entity
effects. Tests could exercise one source item flow with fake provider and
storage Adapters instead of coordinating the worker, HTTP router, filesystem,
and real LLM calls.

## 2. Wiki document module

Files:

- `services/ingestion-worker/pipeline.py`
- `services/api/routers/kb.py`
- `services/api/maintenance.py`
- `services/api/kb_tools.py`
- `services/api/routers/drafts.py`

Problem:

Wiki rendering, parsing, path selection, body stripping, and wikilink mutation
are repeated across worker, router, chat/draft helpers, and maintenance. The
deletion test shows the current modules are shallow: deleting any one wiki
writer does not delete the complexity; it reappears in the other writers.

Candidate direction:

Make wiki files a real Module that owns rendering, parsing, locating, and
wikilink patching for article/entity/summary/index.

Expected benefit:

High Leverage because every caller gets the same frontmatter and body rules.
Tests become simple golden-file tests for round trips and wikilink insertion.

## 3. Knowledge object store

Files:

- `services/api/routers/kb.py`
- `services/api/object_nodes.py`
- `services/api/database.py`

Problem:

Object data is split between `knowledge_nodes` and type-specific tables, but
the current Interface is still "pass a dict with the right magic keys". Article,
summary, entity, and index invariants leak into callers.

Candidate direction:

Deepen this into one knowledge object write/read Module that owns IDs,
deduplication, typed table writes, summary perspective defaults, and fetch
merging.

Expected benefit:

Better Locality for storage rules and a stronger test surface. Object write
tests would not need to go through the full HTTP router to prove summary/entity
/index invariants.

## 4. Entity promotion module

Files:

- `services/ingestion-worker/pipeline.py`
- `services/api/maintenance.py`
- `services/api/entity_insights.py`
- `services/api/routers/kb.py`

Problem:

Entity promotion is spread across ingestion, maintenance, candidate processing,
fact creation, and wikilink backfill. Threshold and effect rules are not local,
so promotion bugs require bouncing through several modules.

Candidate direction:

Put candidate processing, promotion decision, entity materialization, fact
creation, and backfill requests behind one entity promotion Module.

Expected benefit:

Strong Locality for entity lifecycle rules. Tests can cover salience thresholds,
duplicate mentions, matched existing entities, and fact creation through one
Interface.

## 5. Retrieval module

Files:

- `services/api/routers/drafts.py`
- `services/api/kb_tools.py`
- `services/api/routers/kb.py`

Problem:

Vector scoring, HyDE embedding, summary weighting, graph propagation, index
expansion, time filtering, and wiki body lookup are split across draft
generation, chat tools, and search. The retrieval rules are high-value but not
local.

Candidate direction:

Make retrieval a deeper Module used by draft generation, chat tools, and search
endpoints, with each caller choosing output shape rather than reimplementing
scoring.

Expected benefit:

More Leverage from the ranking logic and fewer divergent search behaviors. Tests
can assert retrieval phases and ranking effects without invoking draft
generation or chat streaming.

## 6. Background job handler module

Files:

- `services/api/job_worker.py`
- `services/api/routers/kb.py`
- `services/api/maintenance.py`

Problem:

The job worker imports router modules to run work. That makes routing code
double as job Implementation, and job payload rules leak into places that should
only enqueue work.

Candidate direction:

Move job work into use-case Modules. Routers enqueue jobs, the worker dispatches
jobs, and both call the same Implementation.

Expected benefit:

Cleaner Seam between HTTP and background execution. Job tests become direct and
do not need FastAPI route modules.

