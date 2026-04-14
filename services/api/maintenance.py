"""
每周知识库维护：
  1. fix_islands        — 孤岛检测：找无边节点，用 LLM 分析并建立语义边
  2. supplement_edges   — 补边：对仅有 similar_to 边的节点对，精化为更具体的关系类型
  3. detect_contradictions — 矛盾发现：检测相似节点对中存在的观点矛盾

可以作为独立脚本运行（python maintenance.py），也可以由 API 端点触发。
"""
import asyncio
import json
import os
import sys

import anthropic

sys.path.insert(0, os.path.dirname(__file__))
import database

USER_ID = "default"
CLAUDE_MODEL = "claude-haiku-4-5-20251001"


# ── LLM 关系分析 ──────────────────────────────────────────────────────────────

async def analyze_relation(
    id_a: str,
    summary_a: str,
    id_b: str,
    summary_b: str,
    client: anthropic.AsyncAnthropic,
) -> dict:
    """
    调用 Claude Haiku 判断两个知识节点之间的关系。
    返回：{"relation": str, "confidence": float, "from_id": str, "to_id": str}
    """
    prompt = (
        "以下是两个知识节点的摘要。请分析它们之间最有意义的关系。\n\n"
        f"节点 A：\n{summary_a[:600]}\n\n"
        f"节点 B：\n{summary_b[:600]}\n\n"
        "从以下关系类型中选择最合适的一种：\n"
        "- extends：一个节点是对另一个节点观点的延伸或深化\n"
        "- background_of：一个节点为理解另一个节点提供必要背景知识\n"
        "- contradicts：两个节点持明显相反的观点\n"
        "- supports：一个节点为另一个节点提供支持性证据或案例\n"
        "- none：没有明显的有意义关系\n\n"
        "以 JSON 格式输出（不含任何其他文字）：\n"
        '{"relation":"extends|background_of|contradicts|supports|none",'
        '"direction":"a_to_b|b_to_a|symmetric","confidence":0到1之间的小数}\n'
        "direction 说明：a_to_b 表示 A→B，b_to_a 表示 B→A，"
        "symmetric 表示双向（如 contradicts）。"
    )

    try:
        response = await client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        result = json.loads(response.content[0].text.strip())
    except Exception:
        return {"relation": "none", "confidence": 0.0, "from_id": id_a, "to_id": id_b}

    relation = result.get("relation", "none")
    direction = result.get("direction", "a_to_b")
    confidence = float(result.get("confidence", 0.0))

    if direction == "b_to_a":
        from_id, to_id = id_b, id_a
    else:
        from_id, to_id = id_a, id_b

    return {"relation": relation, "confidence": confidence, "from_id": from_id, "to_id": to_id}


async def upsert_llm_edge(from_id: str, to_id: str, relation: str, confidence: float) -> bool:
    """插入 LLM 推导的边，若相同三元组已存在则跳过。返回是否实际插入。"""
    existing = await database.database.fetch_one(
        """
        SELECT id FROM knowledge_edges
        WHERE from_node_id = :from_id AND to_node_id = :to_id AND relation_type = :rel
        """,
        {"from_id": from_id, "to_id": to_id, "rel": relation},
    )
    if existing:
        return False
    await database.database.execute(
        """
        INSERT INTO knowledge_edges (from_node_id, to_node_id, relation_type, weight, created_by)
        VALUES (:from_id, :to_id, :rel, :weight, 'auto_llm')
        """,
        {"from_id": from_id, "to_id": to_id, "rel": relation, "weight": confidence},
    )
    return True


# ── 1. 孤岛检测 ───────────────────────────────────────────────────────────────

async def fix_islands(user_id: str, client: anthropic.AsyncAnthropic) -> dict:
    """找出无任何边的孤立节点，尝试用 LLM 为其建立语义边。"""
    islands = await database.database.fetch_all(
        """
        SELECT n.id, n.title, n.summary
        FROM knowledge_nodes n
        WHERE n.user_id = :user_id
          AND n.embedding IS NOT NULL
          AND n.summary IS NOT NULL
          AND NOT EXISTS (
            SELECT 1 FROM knowledge_edges e
            WHERE e.from_node_id = n.id OR e.to_node_id = n.id
          )
        LIMIT 20
        """,
        {"user_id": user_id},
    )
    if not islands:
        return {"islands_found": 0, "edges_added": 0}

    edges_added = 0
    for island in islands:
        island = dict(island)
        # 找 top-3 最相似的节点（asyncpg 原生接口支持向量运算符）
        async with database.database.connection() as conn:
            candidates = await conn.raw_connection.fetch(
                """
                SELECT id, title, summary,
                       1 - (embedding <=> (
                         SELECT embedding FROM knowledge_nodes WHERE id = $1
                       )) AS sim
                FROM knowledge_nodes
                WHERE id != $1
                  AND user_id = $2
                  AND embedding IS NOT NULL
                  AND summary IS NOT NULL
                ORDER BY embedding <=> (
                  SELECT embedding FROM knowledge_nodes WHERE id = $1
                )
                LIMIT 3
                """,
                island["id"], user_id,
            )

        for c in candidates:
            c = dict(c)
            if float(c["sim"]) < 0.55:
                continue
            result = await analyze_relation(
                island["id"], island["summary"] or "",
                c["id"], c["summary"] or "",
                client,
            )
            if result["relation"] != "none" and result["confidence"] >= 0.70:
                added = await upsert_llm_edge(
                    result["from_id"], result["to_id"],
                    result["relation"], result["confidence"],
                )
                if added:
                    edges_added += 1
                    break  # 每个孤岛建一条边即可打破孤立状态

    return {"islands_found": len(islands), "edges_added": edges_added}


# ── 2. 补边（将 similar_to 精化为更具体关系）────────────────────────────────────

async def supplement_edges(user_id: str, client: anthropic.AsyncAnthropic) -> dict:
    """
    对仅有 similar_to（auto_semantic）边的节点对，用 LLM 判断是否存在更精确的关系。
    每次最多处理 20 对，按相似度降序。
    """
    async with database.database.connection() as conn:
        pairs = await conn.raw_connection.fetch(
            """
            SELECT e.from_node_id, e.to_node_id, e.weight,
                   na.summary AS summary_a, nb.summary AS summary_b
            FROM knowledge_edges e
            JOIN knowledge_nodes na ON na.id = e.from_node_id
            JOIN knowledge_nodes nb ON nb.id = e.to_node_id
            WHERE e.relation_type = 'similar_to'
              AND e.created_by = 'auto_semantic'
              AND na.user_id = $1
              AND na.summary IS NOT NULL
              AND nb.summary IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_edges e2
                WHERE e2.created_by = 'auto_llm'
                  AND (
                    (e2.from_node_id = e.from_node_id AND e2.to_node_id = e.to_node_id)
                    OR (e2.from_node_id = e.to_node_id AND e2.to_node_id = e.from_node_id)
                  )
              )
            ORDER BY e.weight DESC
            LIMIT 20
            """,
            user_id,
        )

    if not pairs:
        return {"pairs_analyzed": 0, "edges_added": 0}

    edges_added = 0
    for p in pairs:
        p = dict(p)
        result = await analyze_relation(
            p["from_node_id"], p["summary_a"],
            p["to_node_id"], p["summary_b"],
            client,
        )
        if result["relation"] not in ("none", "similar_to") and result["confidence"] >= 0.70:
            added = await upsert_llm_edge(
                result["from_id"], result["to_id"],
                result["relation"], result["confidence"],
            )
            if added:
                edges_added += 1

    return {"pairs_analyzed": len(pairs), "edges_added": edges_added}


# ── 3. 矛盾发现 ───────────────────────────────────────────────────────────────

async def detect_contradictions(user_id: str, client: anthropic.AsyncAnthropic) -> dict:
    """
    对相似度适中（0.75~0.92）的 similar_to 节点对检测观点矛盾。
    相似度过高往往是同一事件不同报道，过低则主题不同，中间段最有可能出现"同题不同观点"。
    每次最多检查 10 对。
    """
    async with database.database.connection() as conn:
        pairs = await conn.raw_connection.fetch(
            """
            SELECT e.from_node_id, e.to_node_id, e.weight,
                   na.summary AS summary_a, nb.summary AS summary_b
            FROM knowledge_edges e
            JOIN knowledge_nodes na ON na.id = e.from_node_id
            JOIN knowledge_nodes nb ON nb.id = e.to_node_id
            WHERE e.relation_type = 'similar_to'
              AND e.created_by = 'auto_semantic'
              AND e.weight BETWEEN 0.75 AND 0.92
              AND na.user_id = $1
              AND na.summary IS NOT NULL
              AND nb.summary IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM knowledge_edges e2
                WHERE e2.relation_type = 'contradicts'
                  AND (
                    (e2.from_node_id = e.from_node_id AND e2.to_node_id = e.to_node_id)
                    OR (e2.from_node_id = e.to_node_id AND e2.to_node_id = e.from_node_id)
                  )
              )
            ORDER BY e.weight DESC
            LIMIT 10
            """,
            user_id,
        )

    if not pairs:
        return {"pairs_checked": 0, "contradictions_found": 0}

    contradictions_found = 0
    for p in pairs:
        p = dict(p)
        result = await analyze_relation(
            p["from_node_id"], p["summary_a"],
            p["to_node_id"], p["summary_b"],
            client,
        )
        if result["relation"] == "contradicts" and result["confidence"] >= 0.75:
            added = await upsert_llm_edge(
                result["from_id"], result["to_id"],
                "contradicts", result["confidence"],
            )
            if added:
                contradictions_found += 1

    return {"pairs_checked": len(pairs), "contradictions_found": contradictions_found}


# ── 主入口 ────────────────────────────────────────────────────────────────────

async def run_maintenance(user_id: str = USER_ID) -> dict:
    """
    运行全部维护任务。
    - 由 API 端点触发时：database 已由 main.py lifespan 连接，直接使用
    - 作为独立脚本运行时：__main__ 块负责调用 database.init()
    """
    claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    if not claude_api_key:
        print("[maintenance] ERROR: CLAUDE_API_KEY not set", flush=True)
        return {"error": "CLAUDE_API_KEY not set"}

    client = anthropic.AsyncAnthropic(api_key=claude_api_key)

    print(f"[maintenance] Starting for user_id={user_id}", flush=True)

    island_result = await fix_islands(user_id, client)
    print(f"[maintenance] Islands: {island_result}", flush=True)

    supplement_result = await supplement_edges(user_id, client)
    print(f"[maintenance] Supplement: {supplement_result}", flush=True)

    contradiction_result = await detect_contradictions(user_id, client)
    print(f"[maintenance] Contradictions: {contradiction_result}", flush=True)

    summary = {
        "islands": island_result,
        "supplement": supplement_result,
        "contradictions": contradiction_result,
    }
    print(f"[maintenance] Done: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    return summary


if __name__ == "__main__":
    async def main():
        await database.init()
        result = await run_maintenance()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    asyncio.run(main())
