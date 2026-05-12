from typing import Any

import database


async def mark_index_stale(index_id: str) -> None:
    await database.database.execute(
        """
        UPDATE index_nodes
        SET abstract_stale = true, updated_at = NOW()
        WHERE node_id = :index_id
        """,
        {"index_id": index_id},
    )


async def _assert_index(index_id: str, user_id: str | None = None) -> dict[str, Any]:
    user_filter = "AND user_id = :user_id" if user_id is not None else ""
    row = await database.database.fetch_one(
        f"""
        SELECT id, user_id, title, object_type
        FROM knowledge_nodes
        WHERE id = :id AND object_type = 'index'
          {user_filter}
        """,
        {"id": index_id, "user_id": user_id},
    )
    if not row:
        raise ValueError("index not found")
    return dict(row)


async def _assert_child(child_id: str, user_id: str | None = None) -> dict[str, Any]:
    user_filter = "AND user_id = :user_id" if user_id is not None else ""
    row = await database.database.fetch_one(
        f"""
        SELECT id, user_id, title, object_type
        FROM knowledge_nodes
        WHERE id = :id AND object_type IN ('article', 'index')
          {user_filter}
        """,
        {"id": child_id, "user_id": user_id},
    )
    if not row:
        raise ValueError("child not found")
    return dict(row)


async def _would_create_cycle(index_id: str, child_id: str) -> bool:
    if index_id == child_id:
        return True
    row = await database.database.fetch_one(
        """
        WITH RECURSIVE descendants AS (
            SELECT child_id
            FROM index_children
            WHERE index_id = :child_id
          UNION
            SELECT ic.child_id
            FROM index_children ic
            JOIN descendants d ON d.child_id = ic.index_id
        )
        SELECT 1 AS found FROM descendants WHERE child_id = :index_id LIMIT 1
        """,
        {"index_id": index_id, "child_id": child_id},
    )
    return row is not None


async def add_child(
    index_id: str,
    child_id: str,
    user_id: str = "default",
    position: int | None = None,
    child_role: str = "member",
) -> dict[str, Any]:
    await _assert_index(index_id, user_id)
    await _assert_child(child_id, user_id)
    if await _would_create_cycle(index_id, child_id):
        raise ValueError("index cycle is not allowed")

    if position is None:
        row = await database.database.fetch_one(
            "SELECT COALESCE(MAX(position), -1) + 1 AS next_position FROM index_children WHERE index_id = :index_id",
            {"index_id": index_id},
        )
        position = int(row["next_position"] if row else 0)

    await database.database.execute(
        """
        INSERT INTO index_children (index_id, child_id, position, child_role)
        VALUES (:index_id, :child_id, :position, :child_role)
        ON CONFLICT (index_id, child_id) DO UPDATE SET
          position = EXCLUDED.position,
          child_role = EXCLUDED.child_role,
          updated_at = NOW()
        """,
        {
            "index_id": index_id,
            "child_id": child_id,
            "position": position,
            "child_role": child_role or "member",
        },
    )
    await mark_index_stale(index_id)
    return {"index_id": index_id, "child_id": child_id, "position": position, "child_role": child_role or "member"}


async def remove_child(index_id: str, child_id: str, user_id: str = "default") -> int:
    await _assert_index(index_id, user_id)
    result = await database.database.execute(
        "DELETE FROM index_children WHERE index_id = :index_id AND child_id = :child_id",
        {"index_id": index_id, "child_id": child_id},
    )
    await mark_index_stale(index_id)
    return int(result.split()[-1]) if isinstance(result, str) and result.split() else 0


async def reorder_children(index_id: str, child_ids: list[str], user_id: str = "default") -> None:
    await _assert_index(index_id, user_id)
    if not child_ids:
        return
    existing = await database.database.fetch_all(
        "SELECT child_id FROM index_children WHERE index_id = :index_id",
        {"index_id": index_id},
    )
    existing_ids = {r["child_id"] for r in existing}
    missing = [cid for cid in child_ids if cid not in existing_ids]
    if missing:
        raise ValueError(f"children not in index: {', '.join(missing)}")

    for pos, child_id in enumerate(child_ids):
        await database.database.execute(
            """
            UPDATE index_children
            SET position = :position, updated_at = NOW()
            WHERE index_id = :index_id AND child_id = :child_id
            """,
            {"index_id": index_id, "child_id": child_id, "position": pos},
        )
    await mark_index_stale(index_id)


async def get_children(index_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        SELECT ic.index_id, ic.child_id, ic.position, ic.child_role,
               kn.title, kn.object_type, kn.abstract, kn.created_at
        FROM index_children ic
        JOIN knowledge_nodes kn ON kn.id = ic.child_id
        WHERE ic.index_id = :index_id AND kn.user_id = :user_id
        ORDER BY ic.position ASC, ic.created_at ASC
        """,
        {"index_id": index_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


async def get_parents(object_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        SELECT ic.index_id, ic.child_id, ic.position, ic.child_role,
               kn.title, kn.object_type, kn.abstract, kn.created_at
        FROM index_children ic
        JOIN knowledge_nodes kn ON kn.id = ic.index_id
        WHERE ic.child_id = :object_id AND kn.user_id = :user_id
        ORDER BY kn.title NULLS LAST, ic.created_at ASC
        """,
        {"object_id": object_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


async def get_ancestors(object_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        WITH RECURSIVE ancestors AS (
            SELECT ic.index_id, ic.child_id, 1 AS depth
            FROM index_children ic
            WHERE ic.child_id = :object_id
          UNION
            SELECT ic.index_id, ic.child_id, a.depth + 1
            FROM index_children ic
            JOIN ancestors a ON a.index_id = ic.child_id
            WHERE a.depth < 20
        )
        SELECT a.index_id, a.child_id, a.depth,
               kn.title, kn.object_type, kn.abstract
        FROM ancestors a
        JOIN knowledge_nodes kn ON kn.id = a.index_id
        WHERE kn.user_id = :user_id
        ORDER BY a.depth ASC, kn.title NULLS LAST
        """,
        {"object_id": object_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]


async def get_descendants(index_id: str, user_id: str = "default") -> list[dict[str, Any]]:
    rows = await database.database.fetch_all(
        """
        WITH RECURSIVE descendants AS (
            SELECT ic.index_id, ic.child_id, ic.position, ic.child_role, 1 AS depth
            FROM index_children ic
            WHERE ic.index_id = :index_id
          UNION
            SELECT ic.index_id, ic.child_id, ic.position, ic.child_role, d.depth + 1
            FROM index_children ic
            JOIN descendants d ON d.child_id = ic.index_id
            WHERE d.depth < 20
        )
        SELECT d.index_id, d.child_id, d.position, d.child_role, d.depth,
               kn.title, kn.object_type, kn.abstract
        FROM descendants d
        JOIN knowledge_nodes kn ON kn.id = d.child_id
        WHERE kn.user_id = :user_id
        ORDER BY d.depth ASC, d.position ASC
        """,
        {"index_id": index_id, "user_id": user_id},
    )
    return [dict(r) for r in rows]
