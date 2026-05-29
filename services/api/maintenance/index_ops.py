import os

import anthropic
from openai import AsyncOpenAI

import database
from settings import settings
from prompts import prompts
from kb.graph import upsert_object_node


async def aggregate_index_abstracts(
    user_id: str,
    index_id: str | None = None,
    only_stale: bool = False,
) -> dict:
    """
    为每个 index 节点生成聚合 abstract（底层向上）。

    收集直接子节点（via index_children）的 abstract，调用 LLM 生成 3-5 句综合摘要，
    更新 DB 中的 abstract 和 embedding，并刷新 wiki 文件 frontmatter。
    幂等：每次运行都用最新子节点状态覆盖。
    """
    from kb.wiki import write_wiki_node

    claude_api_key = os.environ.get("CLAUDE_API_KEY") or os.environ.get("ANTHROPIC_API_KEY", "")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    if not claude_api_key:
        return {"error": "CLAUDE_API_KEY not set", "processed": 0, "skipped": 0}

    claude_client = anthropic.AsyncAnthropic(api_key=claude_api_key)
    openai_client = AsyncOpenAI(api_key=openai_api_key)
    max_children = settings.ingestion.max_index_children_abstracts

    # 1. 找所有 index 节点
    filters = ["kn.user_id = :uid", "kn.object_type = 'index'"]
    params: dict = {"uid": user_id}
    if index_id:
        filters.append("kn.id = :index_id")
        params["index_id"] = index_id
    if only_stale:
        filters.append("COALESCE(ix.abstract_stale, false) = true")
    index_rows = await database.database.fetch_all(
        f"""
        SELECT kn.id, kn.title, ix.rollup_instruction
        FROM knowledge_nodes kn
        LEFT JOIN index_nodes ix ON ix.node_id = kn.id
        WHERE {' AND '.join(filters)}
        """,
        params,
    )
    if not index_rows:
        return {"processed": 0, "skipped": 0}

    index_ids = {r["id"] for r in index_rows}

    # 2. 构建 child map: index_id → [(child_id, child_object_type)]
    child_map: dict[str, list[tuple[str, str]]] = {idx: [] for idx in index_ids}
    for idx_id in index_ids:
        rows = await database.database.fetch_all(
            """
            SELECT ic.child_id, kn.object_type AS child_type
            FROM index_children ic
            JOIN knowledge_nodes kn ON kn.id = ic.child_id
            WHERE ic.index_id = :idx_id
              AND kn.user_id = :user_id
            ORDER BY ic.position ASC, ic.created_at ASC
            """,
            {"idx_id": idx_id, "user_id": user_id},
        )
        child_map[idx_id] = [(r["child_id"], r["child_type"]) for r in rows]

    # 3. 底层向上排序：无 index 子节点的先处理（满足 book→chapters 的典型两层结构）
    def has_index_children(idx_id: str) -> bool:
        return any(ctype == "index" for _, ctype in child_map.get(idx_id, []))

    ordered = sorted(index_rows, key=lambda r: (1 if has_index_children(r["id"]) else 0))

    processed = skipped = 0

    for idx_row in ordered:
        idx_id = idx_row["id"]
        idx_title = idx_row["title"] or idx_id
        rollup_instruction = idx_row["rollup_instruction"] or ""
        children = child_map.get(idx_id, [])

        if not children:
            skipped += 1
            continue

        # 4. 收集子节点 abstract（从 DB 实时读取，确保 sub-index 已更新）
        child_abstracts: list[str] = []
        for child_id, _ in children[:max_children]:
            child = await database.database.fetch_one(
                "SELECT title, abstract FROM knowledge_nodes WHERE id = :id",
                {"id": child_id},
            )
            if child and child["abstract"]:
                label = child["title"] or child_id
                child_abstracts.append(f"- 《{label}》：{child['abstract']}")

        if not child_abstracts:
            skipped += 1
            continue

        # 5. 调用 LLM 生成聚合 abstract
        try:
            prompt = prompts.index_summary(
                index_title=idx_title,
                child_abstracts=(
                    f"Rollup instruction: {rollup_instruction}\n\n" if rollup_instruction else ""
                ) + "\n".join(child_abstracts),
            )
            resp = await claude_client.messages.create(
                model=settings.models.index_summary,
                max_tokens=settings.llm_output_tokens.index_summary,
                messages=[{"role": "user", "content": prompt}],
            )
            new_abstract = getattr(resp.content[0], "text", "").strip()
        except Exception as e:
            print(f"[maintenance] index_abstract LLM error for {idx_id}: {e}", flush=True)
            skipped += 1
            continue

        # 6. 生成 embedding
        try:
            embed_resp = await openai_client.embeddings.create(
                model=settings.embedding.model,
                input=new_abstract[:settings.embedding.max_chars],
                dimensions=settings.embedding.dimensions,
            )
            embedding = embed_resp.data[0].embedding
            emb_lit = "[" + ",".join(repr(x) for x in embedding) + "]"
        except Exception as e:
            print(f"[maintenance] index_abstract embed error for {idx_id}: {e}", flush=True)
            skipped += 1
            continue

        # 7. 更新 DB（abstract + embedding）
        await database.database.execute(
            f"""
            UPDATE knowledge_nodes
            SET abstract = :abstract,
                embedding = '{emb_lit}'::vector,
                embedding_model = :embedding_model,
                updated_at = NOW()
            WHERE id = :id
            """,
            {
                "abstract": new_abstract,
                "id": idx_id,
                "embedding_model": settings.embedding.model,
            },
        )
        await upsert_object_node(
            idx_id,
            "index",
            {"abstract_stale": False},
        )

        # 8. 刷新 wiki 文件 frontmatter（write_wiki_node 保留已有 body；
        #    首次写入时以新 abstract 作为 body）
        await write_wiki_node(idx_id, user_id)

        processed += 1
        print(f"[maintenance] index_abstract updated: {idx_id} ({idx_title})", flush=True)

    return {"processed": processed, "skipped": skipped}
