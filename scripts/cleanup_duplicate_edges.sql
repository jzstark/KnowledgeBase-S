-- Remove duplicate knowledge_edges before adding a future uniqueness constraint.
--
-- Intended uniqueness key:
--   (from_node_id, to_node_id, relation_type)
--
-- Usage:
--   docker compose exec -T postgres psql -U postgres -d app -f /path/in/container
-- or paste/run this SQL in psql after reviewing the duplicate report.

BEGIN;

WITH duplicate_groups AS (
    SELECT
        from_node_id,
        to_node_id,
        relation_type,
        COUNT(*) AS duplicate_count
    FROM knowledge_edges
    GROUP BY from_node_id, to_node_id, relation_type
    HAVING COUNT(*) > 1
)
SELECT *
FROM duplicate_groups
ORDER BY duplicate_count DESC, from_node_id, to_node_id, relation_type;

WITH ranked_edges AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY from_node_id, to_node_id, relation_type
            ORDER BY id
        ) AS rn
    FROM knowledge_edges
)
DELETE FROM knowledge_edges
WHERE id IN (
    SELECT id
    FROM ranked_edges
    WHERE rn > 1
);

COMMIT;
