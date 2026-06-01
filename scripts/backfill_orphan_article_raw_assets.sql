-- One-time backfill: create raw_assets + document_instances for the 41 article_nodes
-- that have raw_ref->>'path' but no document_instance_id (pre-source_items era articles).
-- Run once on VPS, then the raw_ref COALESCE fallback in files.py can be removed.

BEGIN;

INSERT INTO raw_assets (id, user_id, storage_key, created_at)
SELECT
    'ra_' || substring(node_id FROM 5),
    user_id,
    raw_ref->>'path',
    NOW()
FROM article_nodes
WHERE document_instance_id IS NULL
  AND raw_ref->>'path' IS NOT NULL
ON CONFLICT (id) DO NOTHING;

INSERT INTO document_instances (id, user_id, raw_asset_id, origin_ref, origin_ref_type, status, created_at, updated_at)
SELECT
    'di_' || substring(node_id FROM 5),
    user_id,
    'ra_' || substring(node_id FROM 5),
    raw_ref->>'path',
    'file',
    'succeeded',
    NOW(),
    NOW()
FROM article_nodes
WHERE document_instance_id IS NULL
  AND raw_ref->>'path' IS NOT NULL
ON CONFLICT (id) DO NOTHING;

UPDATE article_nodes
SET document_instance_id = 'di_' || substring(node_id FROM 5)
WHERE document_instance_id IS NULL
  AND raw_ref->>'path' IS NOT NULL;

-- Verify: should return 0
SELECT COUNT(*) AS remaining_orphans
FROM article_nodes
WHERE document_instance_id IS NULL
  AND raw_ref->>'path' IS NOT NULL;

COMMIT;
