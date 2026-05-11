# API Route Baseline

Captured for refactor Phase 0 on 2026-05-11.

Source of truth at capture time: FastAPI decorators in `services/api/main.py` and
`services/api/routers/*.py`.

## Auth / Health

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/auth/login` | no | Password login, sets `token` cookie |
| POST | `/api/auth/logout` | no | Clears `token` cookie |
| GET | `/api/auth/me` | yes | Cookie verification |
| GET | `/api/health` | no | Health probe |

## Sources

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/sources/wechat/ingest` | token header | Legacy WeChat push endpoint |
| GET | `/api/sources/{source_id}` | no | Source detail and article count |
| GET | `/api/sources` | no | Source list and article counts |
| POST | `/api/sources` | yes | Create source |
| POST | `/api/sources/{source_id}/upload` | yes | Upload files, append to `sources.config.uploads` |
| POST | `/api/sources/{source_id}/add-url` | yes | Append URLs to `sources.config.pending_urls` |
| POST | `/api/sources/{source_id}/fetch` | yes | Trigger ingestion-worker |
| PUT | `/api/sources/{source_id}` | no | Source update; worker uses this for `last_fetched_at` |
| DELETE | `/api/sources/{source_id}` | yes | Delete source |

## Knowledge Base

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/kb/ingest` | no | Current canonical node write entry for workers |
| POST | `/api/kb/wiki/rebuild` | yes | Background wiki rebuild |
| GET | `/api/kb/wiki/status` | no | Wiki file counts |
| GET | `/api/kb/search` | no | Vector search over `knowledge_nodes` |
| GET | `/api/kb/node/{node_id}` | no | Node detail plus edges and wiki body |
| POST | `/api/kb/nodes/{node_id}/create_summary` | no | Generate perspective summary |
| DELETE | `/api/kb/nodes/{node_id}` | yes | Delete node and wiki file |
| GET | `/api/kb/graph` | no | BFS graph around one root |
| GET | `/api/kb/nodes` | no | Paginated node list |
| GET | `/api/kb/graph/all` | no | D3 graph payload |
| POST | `/api/kb/memory/feedback` | no | Add/update writing memory |
| GET | `/api/kb/memory` | no | List writing memory |
| DELETE | `/api/kb/memory/{memory_id}` | yes | Delete writing memory |
| POST | `/api/kb/entity_candidates/analyze_context` | no | Worker entity context lookup |
| POST | `/api/kb/entity_candidates/process` | no | Worker candidate processing |
| POST | `/api/kb/entities/{entity_id}/backfill_wikilinks` | no | Worker-triggered wikilink backfill |
| POST | `/api/kb/entity_candidates/{candidate_id}/mark_promoted` | no | Mark promoted candidate |
| GET | `/api/kb/entity_candidates` | yes | Candidate debug list |
| POST | `/api/kb/maintenance/run` | yes | Trigger maintenance |
| POST | `/api/kb/maintenance/rebuild_from_raw` | yes | Trigger legacy raw rebuild |

## Briefing

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/briefing` | no | Daily topics |
| POST | `/api/briefing/generate` | yes | Generate today's topics |
| PATCH | `/api/briefing/topics/{topic_id}` | no | Update topic status |

## Drafts

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/drafts/generate` | no | Generate draft from selected topics |
| GET | `/api/drafts` | yes | List drafts |
| POST | `/api/drafts/{draft_id}/feedback` | no | Submit final content to feedback-worker |
| GET | `/api/drafts/{draft_id}` | yes | Draft detail |

## Chat

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/chat/sessions` | yes | List chat sessions |
| POST | `/api/chat/sessions` | yes | Create chat session |
| DELETE | `/api/chat/sessions/{session_id}` | yes | Delete session |
| GET | `/api/chat/sessions/{session_id}/messages` | yes | List messages |
| POST | `/api/chat/sessions/{session_id}/messages` | yes | Send message via SSE |

## Files

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/files/tree` | yes | List raw/wiki/config files |
| GET | `/api/files/content` | yes | Read allowed file content |
| PUT | `/api/files/content` | yes | Write allowed file content |
| DELETE | `/api/files/content` | yes | Delete allowed file |

## Settings

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/api/settings` | yes | Read user settings |
| PUT | `/api/settings` | yes | Update user settings |
| GET | `/api/settings/topics` | yes | Read topics instruction file |
| PUT | `/api/settings/topics` | yes | Write topics instruction file |
| GET | `/api/settings/schema` | yes | Read output schema |
| PUT | `/api/settings/schema` | yes | Write output schema |
| GET | `/api/settings/templates` | yes | List templates |
| GET | `/api/settings/templates/{name}` | yes | Read template |
| PUT | `/api/settings/templates/{name}` | yes | Write template |
| DELETE | `/api/settings/templates/{name}` | yes | Delete template |
| GET | `/api/settings/export` | yes | Export user data |
| GET | `/api/settings/export/no-raw` | yes | Export user data without raw files |
