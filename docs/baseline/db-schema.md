# Database Schema Baseline

Captured for refactor Phase 0 on 2026-05-11.

Source of truth at capture time: `services/api/database.py`.

The API initializes schema from `SCHEMA_SQL` at startup, then runs a small
idempotent migration for `summary -> abstract` and the `fk_summary_of`
self-reference.

## Extension

- `vector`

## Tables

### `knowledge_nodes`

Current mixed object table for article, summary, entity, and index nodes.

Columns:

- `id VARCHAR PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `title TEXT`
- `abstract TEXT`
- `embedding vector(1536)`
- `source_type VARCHAR`
- `source_id VARCHAR`
- `raw_ref JSONB`
- `tags TEXT[]`
- `is_primary BOOLEAN DEFAULT true`
- `object_type VARCHAR(16) NOT NULL DEFAULT 'article'`
- `source_node_ids TEXT[] DEFAULT '{}'`
- `summary_of VARCHAR`
- `canonical_name TEXT`
- `aliases TEXT[] DEFAULT '{}'`
- `created_at TIMESTAMPTZ DEFAULT NOW()`
- `updated_at TIMESTAMPTZ DEFAULT NOW()`
- `perspective TEXT`
- `priority_score FLOAT DEFAULT 1.0`
- `last_accessed_at TIMESTAMPTZ`
- `access_count INT DEFAULT 0`

Constraints and indexes:

- `fk_summary_of`: `summary_of` references `knowledge_nodes(id)` with cascade delete.
- `idx_knowledge_nodes_user_id`
- `idx_knowledge_nodes_embedding` using `ivfflat` / `vector_cosine_ops`
- `idx_knowledge_nodes_object_type`
- `idx_knowledge_nodes_summary_of`
- `idx_knowledge_nodes_priority`

### `entity_candidates`

Candidate entity pool used during ingestion and promotion.

Columns:

- `id SERIAL PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `canonical_name TEXT NOT NULL`
- `aliases TEXT[] DEFAULT '{}'`
- `embedding vector(1536)`
- `mentions JSONB DEFAULT '[]'`
- `promoted_entity_id VARCHAR REFERENCES knowledge_nodes(id)`
- `created_at TIMESTAMPTZ DEFAULT NOW()`
- `updated_at TIMESTAMPTZ DEFAULT NOW()`

Constraints and indexes:

- `UNIQUE (user_id, canonical_name)`
- `idx_entity_candidates_user`

### `knowledge_edges`

Current mixed graph edge table for canonical, compatibility, and legacy semantic
relations.

Columns:

- `id SERIAL PRIMARY KEY`
- `from_node_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE`
- `to_node_id VARCHAR REFERENCES knowledge_nodes(id) ON DELETE CASCADE`
- `relation_type VARCHAR`
- `weight FLOAT`
- `created_by VARCHAR`
- `description TEXT`

Current schema does not enforce edge uniqueness.

### `writing_memory`

Learned writing preference rules.

Columns:

- `id SERIAL PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `template_name VARCHAR`
- `rule TEXT`
- `rule_type VARCHAR`
- `confidence FLOAT DEFAULT 0.5`
- `count INTEGER DEFAULT 1`
- `created_at TIMESTAMPTZ DEFAULT NOW()`
- `updated_at TIMESTAMPTZ DEFAULT NOW()`

### `sources`

Source definitions. Current queues and upload history are stored inside
`config`.

Columns:

- `id VARCHAR PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `name VARCHAR NOT NULL`
- `type VARCHAR NOT NULL`
- `fetch_mode VARCHAR`
- `is_primary BOOLEAN DEFAULT true`
- `config JSONB`
- `api_token VARCHAR`
- `last_fetched_at TIMESTAMPTZ`
- `created_at TIMESTAMPTZ DEFAULT NOW()`

Indexes:

- `idx_sources_user_id`

### `drafts`

Draft history and selected source/topic references.

Columns:

- `id VARCHAR PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `template_name VARCHAR`
- `selected_node_ids TEXT[]`
- `selected_topic_ids TEXT[]`
- `draft_content TEXT`
- `final_content TEXT`
- `created_at TIMESTAMPTZ DEFAULT NOW()`

Indexes:

- `idx_drafts_user_id`

### `briefings`

Legacy daily briefing group storage.

Columns:

- `id SERIAL PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `date DATE NOT NULL DEFAULT CURRENT_DATE`
- `groups JSONB NOT NULL DEFAULT '[]'`
- `created_at TIMESTAMPTZ DEFAULT NOW()`

Constraints and indexes:

- `UNIQUE(user_id, date)`
- `idx_briefings_user_date`

### `topics`

Current daily briefing topic storage.

Columns:

- `id VARCHAR PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `date DATE NOT NULL DEFAULT CURRENT_DATE`
- `title TEXT NOT NULL`
- `description TEXT`
- `source_node_ids TEXT[] DEFAULT '{}'`
- `status VARCHAR DEFAULT 'pending'`
- `created_at TIMESTAMPTZ DEFAULT NOW()`

Indexes:

- `idx_topics_user_date`

### `user_settings`

JSON settings for the single default user.

Columns:

- `user_id VARCHAR PRIMARY KEY`
- `settings JSONB NOT NULL DEFAULT '{}'`

### `chat_sessions`

Chat session headers.

Columns:

- `id VARCHAR PRIMARY KEY`
- `user_id VARCHAR NOT NULL`
- `title TEXT`
- `created_at TIMESTAMPTZ DEFAULT NOW()`
- `updated_at TIMESTAMPTZ DEFAULT NOW()`

Indexes:

- `idx_chat_sessions_user`

### `chat_messages`

Chat messages.

Columns:

- `id SERIAL PRIMARY KEY`
- `session_id VARCHAR REFERENCES chat_sessions(id) ON DELETE CASCADE`
- `role VARCHAR NOT NULL`
- `content TEXT NOT NULL`
- `created_at TIMESTAMPTZ DEFAULT NOW()`

Indexes:

- `idx_chat_messages_session`

## Phase 0 Observations

- There is no `source_items` table yet.
- There is no `jobs` table yet.
- Object-specific tables (`article_nodes`, `summary_nodes`, `entity_nodes`,
  `index_nodes`) do not exist yet.
- `knowledge_nodes` still carries object-specific fields such as
  `summary_of`, `canonical_name`, `aliases`, and `perspective`.
- `knowledge_edges` has no uniqueness constraint, so duplicate edge cleanup is
  required before adding one.
