"""
草稿生成路由。

POST /api/drafts/generate   — RAG 检索 + 模板 + 偏好规则 → Claude 生成草稿
GET  /api/drafts            — 历史草稿列表（需认证）
GET  /api/drafts/{id}       — 单篇草稿详情（需认证）
"""

import asyncio
import os
import secrets
from pathlib import Path

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import config_loader
import database
from auth import require_auth
from routers.kb import _hyde_embed_query

router = APIRouter(prefix="/api/drafts", tags=["drafts"])

USER_ID = "default"
USER_DATA_DIR = Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
FEEDBACK_WORKER_URL = os.environ.get("FEEDBACK_WORKER_URL", "http://feedback-worker:8002")

claude = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_API_KEY", ""))

DEFAULT_TEMPLATE = """请写一篇适合微信公众号的文章。风格轻松有观点，适合碎片化阅读。
开头用一个有趣的现象或问题引入，中间分2-3个小节展开，每节有小标题，
结尾给读者一个值得思考的问题，不要号召性语言。长度1500字左右。"""

MAX_KNOWLEDGE_CHARS = config_loader.get("retrieval.draft_knowledge_chars", 6000)


# ── 请求/响应模型 ─────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    selected_topic_ids: list[str]
    template_name: str = "default"


class FeedbackRequest(BaseModel):
    final_content: str


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def read_template(template_name: str) -> str:
    """读取用户模板文件，不存在则返回默认模板。"""
    template_dir = USER_DATA_DIR / USER_ID / "config" / "templates"
    candidates = [
        template_dir / f"{template_name}.md",
        template_dir / f"{template_name}.txt",
    ]
    for path in candidates:
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return DEFAULT_TEMPLATE


async def fetch_node(node_id: str) -> dict | None:
    row = await database.database.fetch_one(
        "SELECT id, title, abstract AS summary, tags FROM knowledge_nodes WHERE id = :id",
        {"id": node_id},
    )
    return dict(row) if row else None


async def fetch_topic(topic_id: str) -> dict | None:
    row = await database.database.fetch_one(
        "SELECT id, title, description, source_node_ids FROM topics WHERE id = :id",
        {"id": topic_id},
    )
    return dict(row) if row else None


def read_wiki_body(node_id: str, object_type: str) -> str:
    """Return wiki file body (YAML frontmatter stripped), or empty string if unavailable."""
    subdir = {
        "article": "articles",
        "entity": "entities",
        "summary": "summaries",
        "index": "indices",
    }.get(object_type, "articles")
    p = USER_DATA_DIR / USER_ID / "wiki" / subdir / f"{node_id}.md"
    if not p.exists():
        return ""
    content = p.read_text(encoding="utf-8")
    parts = content.split("---", 2)
    body = parts[2].strip() if len(parts) >= 3 else content.strip()
    # Strip appended related-nodes section
    for marker in ("\n## 关联节点\n", "\n## 関連節点\n"):
        if marker in body:
            body = body[: body.index(marker)].strip()
    return body


def format_nodes(nodes: list[dict], label: str = "") -> str:
    lines = []
    if label:
        lines.append(f"【{label}】")
    for n in nodes:
        tags = "、".join((n.get("tags") or [])[:3])
        lines.append(f"- {n.get('title') or '（无标题）'}（{tags}）\n  {(n.get('summary') or '')[:200]}")
    return "\n".join(lines)


def truncate_to_chars(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    return text[:max_chars] + ("..." if len(text) > max_chars else "")


# ── 分层检索 ──────────────────────────────────────────────────────────────────

async def layered_retrieval(
    query: str,
    exclude_ids: list[str],
    user_id: str = USER_ID,
) -> dict[str, list[dict]]:
    """
    6-phase layered retrieval.

    Phase 0 : Query embedding via HyDE (or direct fallback)
    Phase 1 : Three parallel pgvector searches — summaries / entities / articles(fallback)
    Phase 2 : Graph propagation — entity→summary, entity→article, summary→article
    Phase 3 : One-hop article→entity→article expansion (limited anchor set)
    Phase 4 : Index node expansion (find article children, score by embedding sim)
    Phase 5 : Fallback fill from Phase 1c if result set is sparse
    Phase 6 : Final sort + exclude source nodes → return top-k articles + top entities
    """
    summary_top_k        = config_loader.get("retrieval.summary_top_k", 5)
    entity_top_k         = config_loader.get("retrieval.entity_top_k", 10)
    article_direct_top_k = config_loader.get("retrieval.article_direct_top_k", 8)
    article_top_k        = config_loader.get("retrieval.article_top_k", 8)
    entity_in_context    = config_loader.get("retrieval.entity_in_context", 5)
    damping_e2s          = config_loader.get("retrieval.damping_entity_to_summary", 0.7)
    damping_hop          = config_loader.get("retrieval.damping_hop", 0.3)
    expansion_anchor_k   = config_loader.get("retrieval.expansion_anchor_k", 5)
    expansion_min_score  = config_loader.get("retrieval.expansion_min_score", 0.3)
    index_expand_thr     = config_loader.get("retrieval.index_expand_threshold", 0.4)
    index_expand_limit   = config_loader.get("retrieval.index_expand_limit", 3)
    fallback_discount    = config_loader.get("retrieval.fallback_score_discount", 0.5)

    # ── Phase 0: query embedding ──────────────────────────────────────────────
    q_vec = await _hyde_embed_query(query)
    emb_lit = "[" + ",".join(repr(x) for x in q_vec) + "]"
    excl_set = set(exclude_ids)
    excl_clause = (
        "AND id NOT IN (" + ", ".join(f"'{i}'" for i in exclude_ids) + ")"
        if exclude_ids else ""
    )

    async def _vec_search(object_types: list[str], top_k: int) -> list[tuple[str, float]]:
        types_str = ", ".join(f"'{t}'" for t in object_types)
        async with database.database.connection() as conn:
            rows = await conn.raw_connection.fetch(
                f"""
                SELECT id, 1-(embedding<=>'{emb_lit}'::vector) AS sim
                FROM knowledge_nodes
                WHERE user_id = '{user_id}'
                  AND embedding IS NOT NULL
                  AND object_type IN ({types_str})
                  {excl_clause}
                ORDER BY embedding<=>'{emb_lit}'::vector
                LIMIT {top_k}
                """
            )
        return [(r["id"], float(r["sim"])) for r in rows]

    # ── Phase 1: parallel vector search ──────────────────────────────────────
    summary_hits, entity_hits, article_hits = await asyncio.gather(
        _vec_search(["summary"], summary_top_k),
        _vec_search(["entity"], entity_top_k),
        _vec_search(["article", "index"], article_direct_top_k),
    )

    entity_hits_map: dict[str, float]  = {eid: s for eid, s in entity_hits}
    scored_summaries: dict[str, float] = {sid: s for sid, s in summary_hits}
    scored_articles: dict[str, float]  = {}

    # ── Phase 2: graph propagation ────────────────────────────────────────────
    if entity_hits_map:
        ent_ids_str = ", ".join(f"'{e}'" for e in entity_hits_map)

        # 2a: entity → summary (mentions/wikilink from summary to entity, reversed)
        e2s = await database.database.fetch_all(
            f"""
            SELECT ke.from_node_id AS summary_id, ke.to_node_id AS entity_id, ke.weight
            FROM knowledge_edges ke
            JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
            WHERE ke.to_node_id IN ({ent_ids_str})
              AND ke.relation_type IN ('mentions', 'wikilink')
              AND kn.object_type = 'summary'
              AND kn.user_id = '{user_id}'
            """
        )
        for r in e2s:
            s_id    = r["summary_id"]
            e_score = entity_hits_map.get(r["entity_id"], 0)
            prop    = e_score * float(r["weight"] or 0.5) * damping_e2s
            if s_id not in scored_summaries or prop > scored_summaries[s_id]:
                scored_summaries[s_id] = prop

        # 2b: entity → article (reverse mentions/wikilink edges)
        e2a = await database.database.fetch_all(
            f"""
            SELECT ke.from_node_id AS article_id, ke.to_node_id AS entity_id, ke.weight
            FROM knowledge_edges ke
            JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
            WHERE ke.to_node_id IN ({ent_ids_str})
              AND ke.relation_type IN ('mentions', 'wikilink')
              AND kn.object_type IN ('article', 'index')
              AND kn.user_id = '{user_id}'
            """
        )
        for r in e2a:
            a_id    = r["article_id"]
            e_score = entity_hits_map.get(r["entity_id"], 0)
            scored_articles[a_id] = scored_articles.get(a_id, 0) + e_score * float(r["weight"] or 0.5)

    # 2c: summary → article (summarizes edges: summary→article/index)
    if scored_summaries:
        sum_ids_str = ", ".join(f"'{s}'" for s in scored_summaries)
        s2a = await database.database.fetch_all(
            f"""
            SELECT from_node_id AS summary_id, to_node_id AS target_id
            FROM knowledge_edges
            WHERE from_node_id IN ({sum_ids_str})
              AND relation_type = 'summarizes'
            """
        )
        for r in s2a:
            t_id = r["target_id"]
            scored_articles[t_id] = scored_articles.get(t_id, 0) + scored_summaries[r["summary_id"]]

    # ── Phase 3: one-hop article→entity→article expansion ────────────────────
    anchors = [
        (a, s)
        for a, s in sorted(scored_articles.items(), key=lambda x: x[1], reverse=True)[:expansion_anchor_k]
        if s >= expansion_min_score
    ]
    if anchors:
        anchor_ids_str  = ", ".join(f"'{a}'" for a, _ in anchors)
        anchor_score_map = dict(anchors)

        fwd_mentions = await database.database.fetch_all(
            f"""
            SELECT from_node_id AS article_id, to_node_id AS entity_id, weight
            FROM knowledge_edges
            WHERE from_node_id IN ({anchor_ids_str})
              AND relation_type IN ('mentions', 'wikilink')
            """
        )
        # entity_id → best (anchor_score * edge_weight) score
        exp_entities: dict[str, float] = {}
        for r in fwd_mentions:
            combined = anchor_score_map[r["article_id"]] * float(r["weight"] or 0.5)
            if r["entity_id"] not in exp_entities or combined > exp_entities[r["entity_id"]]:
                exp_entities[r["entity_id"]] = combined

        if exp_entities:
            exp_ent_str = ", ".join(f"'{e}'" for e in exp_entities)
            rev_mentions = await database.database.fetch_all(
                f"""
                SELECT ke.from_node_id AS other_article, ke.to_node_id AS entity_id, ke.weight
                FROM knowledge_edges ke
                JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
                WHERE ke.to_node_id IN ({exp_ent_str})
                  AND ke.relation_type IN ('mentions', 'wikilink')
                  AND kn.object_type IN ('article', 'index')
                  AND kn.user_id = '{user_id}'
                """
            )
            for r in rev_mentions:
                o_id = r["other_article"]
                if o_id in scored_articles:
                    continue  # already scored via a stronger path; don't overwrite
                e_score = exp_entities.get(r["entity_id"], 0)
                scored_articles[o_id] = e_score * float(r["weight"] or 0.5) * damping_hop

    # ── Phase 4: index node expansion ─────────────────────────────────────────
    candidate_indices = [
        (nid, score)
        for nid, score in sorted(scored_articles.items(), key=lambda x: x[1], reverse=True)
        if score > index_expand_thr
    ]
    if candidate_indices:
        cand_ids_str = ", ".join(f"'{n}'" for n, _ in candidate_indices)
        idx_rows = await database.database.fetch_all(
            f"SELECT id FROM knowledge_nodes WHERE id IN ({cand_ids_str}) AND object_type = 'index'"
        )
        actual_indices = {r["id"] for r in idx_rows}
        high_indices = [
            (n, s) for n, s in candidate_indices if n in actual_indices
        ][:index_expand_limit]

        for index_id, idx_score in high_indices:
            child_rows = await database.database.fetch_all(
                f"""
                SELECT ke.from_node_id AS child_id
                FROM knowledge_edges ke
                JOIN knowledge_nodes kn ON kn.id = ke.from_node_id
                WHERE ke.to_node_id = '{index_id}'
                  AND ke.relation_type = 'part_of'
                  AND kn.object_type = 'article'
                  AND kn.user_id = '{user_id}'
                """
            )
            child_ids = [r["child_id"] for r in child_rows]
            if child_ids:
                child_ids_str = ", ".join(f"'{c}'" for c in child_ids)
                async with database.database.connection() as conn:
                    child_sims = await conn.raw_connection.fetch(
                        f"""
                        SELECT id, 1-(embedding<=>'{emb_lit}'::vector) AS sim
                        FROM knowledge_nodes
                        WHERE id IN ({child_ids_str}) AND embedding IS NOT NULL
                        """
                    )
                for cs in child_sims:
                    child_score = idx_score * float(cs["sim"])
                    scored_articles[cs["id"]] = max(
                        scored_articles.get(cs["id"], 0), child_score
                    )
            del scored_articles[index_id]

    # ── Phase 5: fallback fill ─────────────────────────────────────────────────
    non_excl_count = sum(1 for nid in scored_articles if nid not in excl_set)
    if non_excl_count < article_top_k:
        for node_id, sim in article_hits:
            if node_id not in scored_articles and node_id not in excl_set:
                scored_articles[node_id] = sim * fallback_discount

    # ── Phase 6: sort, exclude, fetch node details ────────────────────────────
    final_ids = [
        nid
        for nid, _ in sorted(scored_articles.items(), key=lambda x: x[1], reverse=True)
        if nid not in excl_set
    ][:article_top_k]

    article_nodes: list[dict] = []
    if final_ids:
        ids_str = ", ".join(f"'{i}'" for i in final_ids)
        rows = await database.database.fetch_all(
            f"SELECT id, title, abstract, tags, object_type FROM knowledge_nodes WHERE id IN ({ids_str})"
        )
        node_map = {r["id"]: dict(r) for r in rows}
        for nid in final_ids:
            if nid in node_map:
                n = node_map[nid]
                n["summary"] = n.pop("abstract", "")
                n["score"]   = scored_articles[nid]
                article_nodes.append(n)

    top_entity_ids = sorted(entity_hits_map, key=entity_hits_map.__getitem__, reverse=True)[:entity_in_context]
    entity_nodes: list[dict] = []
    if top_entity_ids:
        ids_str = ", ".join(f"'{i}'" for i in top_entity_ids)
        rows = await database.database.fetch_all(
            f"SELECT id, title, canonical_name, abstract, tags FROM knowledge_nodes WHERE id IN ({ids_str})"
        )
        node_map = {r["id"]: dict(r) for r in rows}
        for eid in top_entity_ids:
            if eid in node_map:
                n = node_map[eid]
                n["summary"] = n.pop("abstract", "")
                n["title"]   = n["title"] or n.get("canonical_name") or ""
                entity_nodes.append(n)

    return {"articles": article_nodes, "entities": entity_nodes}


# ── 端点 ──────────────────────────────────────────────────────────────────────

@router.post("/generate")
async def generate_draft(body: GenerateRequest):
    """RAG 检索（分层）+ 模板 + 偏好规则 → Claude 生成草稿。"""
    if not body.selected_topic_ids:
        raise HTTPException(400, "至少选择一个选题")

    # 1. 获取已选选题
    topics = []
    for tid in body.selected_topic_ids:
        topic = await fetch_topic(tid)
        if topic:
            topics.append(topic)
    if not topics:
        raise HTTPException(404, "所选选题不存在")

    # 2. 来源原文节点（选题直接绑定的文章）
    all_source_ids: list[str] = list(dict.fromkeys(
        nid for t in topics for nid in (t.get("source_node_ids") or [])
    ))
    source_nodes = [n for nid in all_source_ids if (n := await fetch_node(nid))]

    # 3. 分层检索（排除已有来源节点）
    query = " ".join(f"{t['title']} {t.get('description', '')}" for t in topics)
    retrieval = await layered_retrieval(query, all_source_ids)

    # 4. 组装知识上下文（先文章后实体，token 预算递减）
    remaining = MAX_KNOWLEDGE_CHARS
    article_parts: list[str] = []
    for node in retrieval["articles"]:
        if remaining <= 100:
            break
        title   = node.get("title") or "（无标题）"
        tags    = "、".join((node.get("tags") or [])[:3])
        header  = f"**{title}**" + (f"（{tags}）" if tags else "")
        content = read_wiki_body(node["id"], node.get("object_type", "article"))
        if not content:
            content = node.get("summary") or ""
        content = truncate_to_chars(content, remaining - len(header) - 2)
        part    = f"{header}\n{content}" if content else header
        article_parts.append(part)
        remaining -= len(part) + 2  # +2 for separator

    entity_parts: list[str] = []
    for node in retrieval["entities"]:
        if remaining <= 100:
            break
        title   = node.get("title") or "（无名实体）"
        content = read_wiki_body(node["id"], "entity")
        if not content:
            content = node.get("summary") or ""
        content = truncate_to_chars(content, remaining - len(title) - 4)
        if content:
            entity_parts.append(f"**{title}**：{content}")
            remaining -= len(title) + len(content) + 4

    # 5. 读取偏好规则（confidence >= 0.8）
    pref_rows = await database.database.fetch_all(
        """
        SELECT rule FROM writing_memory
        WHERE user_id = :user_id
          AND (template_name = :tpl OR template_name IS NULL)
          AND confidence >= 0.8
        ORDER BY confidence DESC
        LIMIT 10
        """,
        {"user_id": USER_ID, "tpl": body.template_name},
    )
    preferences = "\n".join(f"- {r['rule']}" for r in pref_rows)

    # 6. 读取模板
    template = read_template(body.template_name)

    # 7. 组合 Prompt
    topic_lines = "\n".join(
        f"- 【{t['title']}】{t.get('description', '')}" for t in topics
    )
    prompt_parts = [template, "", "本次写作的选题角度：", topic_lines]
    if source_nodes:
        prompt_parts += ["", "相关来源原文摘要：", format_nodes(source_nodes, "来源原文")]
    if article_parts:
        prompt_parts += ["", "知识库相关文章：", "\n\n".join(article_parts)]
    if entity_parts:
        prompt_parts += ["", "相关实体：", "\n\n".join(entity_parts)]
    if preferences:
        prompt_parts += ["", "根据用户历史反馈，额外注意：", preferences]
    prompt = "\n".join(prompt_parts)

    # 8. 调用 Claude
    message = claude.messages.create(
        model=config_loader.get("models.draft_generation", "claude-sonnet-4-6"),
        max_tokens=config_loader.get("llm_output_tokens.draft_generation", 4096),
        messages=[{"role": "user", "content": prompt}],
    )
    draft_content = message.content[0].text.strip()

    # 9. 写入 drafts 表
    draft_id = f"draft_{secrets.token_hex(6)}"
    await database.database.execute(
        """
        INSERT INTO drafts (id, user_id, template_name, selected_node_ids, selected_topic_ids, draft_content)
        VALUES (:id, :user_id, :template_name, :selected_node_ids, :selected_topic_ids, :draft_content)
        """,
        {
            "id": draft_id,
            "user_id": USER_ID,
            "template_name": body.template_name,
            "selected_node_ids": all_source_ids,
            "selected_topic_ids": body.selected_topic_ids,
            "draft_content": draft_content,
        },
    )

    return {
        "id": draft_id,
        "draft_content": draft_content,
        "template_name": body.template_name,
        "selected_count": len(topics),
        "knowledge_count": len(retrieval["articles"]) + len(retrieval["entities"]),
    }


@router.get("")
async def list_drafts(_: dict = Depends(require_auth)):
    """历史草稿列表，不含正文。"""
    rows = await database.database.fetch_all(
        """
        SELECT id, template_name, selected_node_ids,
               LEFT(draft_content, 100) AS preview,
               created_at
        FROM drafts
        WHERE user_id = :user_id
        ORDER BY created_at DESC
        LIMIT 50
        """,
        {"user_id": USER_ID},
    )
    result = []
    for r in rows:
        d = dict(r)
        if d.get("created_at"):
            d["created_at"] = d["created_at"].isoformat()
        result.append(d)
    return result


@router.post("/{draft_id}/feedback")
async def submit_feedback(draft_id: str, body: FeedbackRequest):
    """用户提交定稿，调用 feedback-worker 分析并学习偏好规则。"""
    row = await database.database.fetch_one(
        "SELECT id, template_name, draft_content FROM drafts WHERE id = :id AND user_id = :user_id",
        {"id": draft_id, "user_id": USER_ID},
    )
    if not row:
        raise HTTPException(404, "草稿不存在")

    await database.database.execute(
        "UPDATE drafts SET final_content = :fc WHERE id = :id",
        {"fc": body.final_content, "id": draft_id},
    )

    rules_extracted = 0
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{FEEDBACK_WORKER_URL}/analyze",
                json={
                    "draft_id": draft_id,
                    "draft_content": row["draft_content"] or "",
                    "final_content": body.final_content,
                    "template_name": row["template_name"] or "default",
                },
                timeout=30,
            )
            if resp.status_code == 200:
                rules_extracted = resp.json().get("rules_extracted", 0)
    except Exception:
        pass

    return {"ok": True, "rules_extracted": rules_extracted}


@router.get("/{draft_id}")
async def get_draft(draft_id: str, _: dict = Depends(require_auth)):
    """单篇草稿详情。"""
    row = await database.database.fetch_one(
        "SELECT * FROM drafts WHERE id = :id AND user_id = :user_id",
        {"id": draft_id, "user_id": USER_ID},
    )
    if not row:
        raise HTTPException(404, "草稿不存在")
    d = dict(row)
    if d.get("created_at"):
        d["created_at"] = d["created_at"].isoformat()
    return d
