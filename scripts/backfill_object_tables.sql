-- Repeatable Phase 4.5 backfill from knowledge_nodes into object-specific tables.
-- Run after the API schema migration has created article_nodes, summary_nodes,
-- entity_nodes, and index_nodes.

INSERT INTO article_nodes
  (node_id, source_item_id, raw_ref, source_type, source_published_at,
   source_updated_at, captured_at, effective_at, tags, status, created_at, updated_at)
SELECT id, source_item_id, raw_ref, source_type, source_published_at,
       source_updated_at, captured_at, effective_at, COALESCE(tags, '{}'),
       'active', created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'article'
ON CONFLICT (node_id) DO UPDATE SET
  source_item_id = EXCLUDED.source_item_id,
  raw_ref = EXCLUDED.raw_ref,
  source_type = EXCLUDED.source_type,
  source_published_at = EXCLUDED.source_published_at,
  source_updated_at = EXCLUDED.source_updated_at,
  captured_at = EXCLUDED.captured_at,
  effective_at = EXCLUDED.effective_at,
  tags = EXCLUDED.tags,
  updated_at = NOW();

INSERT INTO summary_nodes
  (node_id, summary_of, perspective_label, perspective_instruction,
   perspective_embedding, body, body_embedding, is_default, source, created_at, updated_at)
SELECT id, summary_of, perspective_label, perspective_instruction,
       perspective_embedding, abstract, COALESCE(body_embedding, embedding),
       COALESCE(is_default, false),
       jsonb_build_object('source_node_ids', COALESCE(source_node_ids, '{}'), 'legacy_perspective', perspective),
       created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'summary'
ON CONFLICT (node_id) DO UPDATE SET
  summary_of = EXCLUDED.summary_of,
  perspective_label = EXCLUDED.perspective_label,
  perspective_instruction = EXCLUDED.perspective_instruction,
  perspective_embedding = EXCLUDED.perspective_embedding,
  body = EXCLUDED.body,
  body_embedding = EXCLUDED.body_embedding,
  is_default = EXCLUDED.is_default,
  source = EXCLUDED.source,
  updated_at = NOW();

INSERT INTO entity_nodes
  (node_id, canonical_name, aliases, created_at, updated_at)
SELECT id, canonical_name, COALESCE(aliases, '{}'), created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'entity'
ON CONFLICT (node_id) DO UPDATE SET
  canonical_name = EXCLUDED.canonical_name,
  aliases = EXCLUDED.aliases,
  updated_at = NOW();

INSERT INTO index_nodes
  (node_id, description, abstract_stale, created_at, updated_at)
SELECT id, abstract, false, created_at, updated_at
FROM knowledge_nodes
WHERE object_type = 'index'
ON CONFLICT (node_id) DO UPDATE SET
  description = EXCLUDED.description,
  updated_at = NOW();
