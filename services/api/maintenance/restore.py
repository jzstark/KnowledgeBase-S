import asyncio
import json
import os
import pathlib
import re
from datetime import datetime
from typing import Any

import httpx
import yaml
from openai import AsyncOpenAI

import database
from settings import settings
from kb.graph import add_child, upsert_object_node


def _parse_rebuild_time(value: str | None):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def restore_from_wiki(user_id: str = "default") -> dict:
    """
    从 wiki 文件重建 knowledge_nodes / index_children / knowledge_edges（用于 postgres 数据丢失时恢复）。

    流程：
      1. 扫描 wiki/{articles,summaries,entities,indices}/ 下所有 .md 文件
      2. 解析 frontmatter（id、type、title、tags、raw_ref 等）+ 提取 body 作为 abstract
      3. 用 OpenAI 生成 embedding
      4. INSERT 到 knowledge_nodes（跳过已存在的）
      5. 重建 summarizes / mentions 边，并把 legacy part_of relations 迁移为 index_children

    幂等：已存在的节点跳过，ON CONFLICT DO NOTHING 保护边。
    """
    openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    user_data_dir = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
    wiki_dir = user_data_dir / user_id / "wiki"

    if not wiki_dir.exists():
        return {"error": "wiki directory not found", "nodes_inserted": 0}

    def _parse(path: pathlib.Path) -> dict | None:
        try:
            text = path.read_text(encoding="utf-8")
            parts = text.split("---", 2)
            if len(parts) < 3:
                return None
            fm_raw = parts[1]
            body = parts[2].strip()
            # Sanitize curly/fancy quotes in frontmatter values before yaml parse
            fm_safe = re.sub(r'["""]', '"', fm_raw)
            try:
                meta = yaml.safe_load(fm_safe) or {}
            except Exception:
                # fallback: extract id and title with regex
                meta = {}
                for key in ("id", "type", "source_type", "storage_key", "summary_of", "canonical_name"):
                    m = re.search(rf'^{key}:\s*(.+)$', fm_raw, re.MULTILINE)
                    if m:
                        meta[key] = m.group(1).strip().strip('"')
                title_m = re.search(r'^title:\s*"?(.*?)"?\s*$', fm_raw, re.MULTILINE)
                if title_m:
                    meta["title"] = title_m.group(1).strip('"""')
            for marker in ["\n## 関連節点\n", "\n## 关联节点\n"]:
                if marker in body:
                    body = body[:body.index(marker)].strip()
            # strip leading "# Title\n\n"
            lines = body.split("\n", 2)
            if lines and lines[0].startswith("# "):
                body = lines[2].strip() if len(lines) >= 3 else ""
            meta["_body"] = body
            return meta
        except Exception as e:
            print(f"[restore] parse error {path.name}: {e}", flush=True)
            return None

    # ── 1. Collect ────────────────────────────────────────────────────────────
    all_metas: list[dict] = []
    for subdir in ["articles", "summaries", "entities", "indices"]:
        subpath = wiki_dir / subdir
        if subpath.exists():
            for f in sorted(subpath.glob("*.md")):
                m = _parse(f)
                if m and m.get("id"):
                    all_metas.append(m)

    print(f"[restore] found {len(all_metas)} wiki files", flush=True)

    # ── 2. Ensure placeholder sources exist ───────────────────────────────────
    VALID_TYPES = {"rss", "url", "plaintext", "pdf", "epub", "word", "image", "wechat"}
    seen_types: set[str] = set()
    for m in all_metas:
        st = (m.get("source_type") or "plaintext").lower()
        seen_types.add(st)
    for st in seen_types:
        src_id = f"restored_{st}"
        exists = await database.database.fetch_one(
            "SELECT id FROM sources WHERE id = :id", {"id": src_id}
        )
        if not exists:
            db_type = st if st in VALID_TYPES else "plaintext"
            await database.database.execute(
                """
                INSERT INTO sources (id, user_id, name, type, fetch_mode, is_primary, config)
                VALUES (:id, :uid, :name, :type, 'manual', true, '{}')
                """,
                {"id": src_id, "uid": user_id,
                 "name": f"[已恢复：{st}]", "type": db_type},
            )
            print(f"[restore] created placeholder source: {src_id}", flush=True)

    # ── 3. Insert nodes ───────────────────────────────────────────────────────
    nodes_inserted = nodes_skipped = 0

    for m in all_metas:
        node_id = m["id"]
        existing = await database.database.fetch_one(
            "SELECT id FROM knowledge_nodes WHERE id = :id", {"id": node_id}
        )
        if existing:
            nodes_skipped += 1
            continue

        object_type = str(m.get("type") or "article")
        body = m.get("_body") or ""

        if object_type == "summary":
            abstract = body
        else:
            abstract = body[:500] if body else (str(m.get("canonical_name") or m.get("title") or ""))

        # embedding
        try:
            embed_text = (abstract or str(m.get("title") or node_id))
            resp = await openai_client.embeddings.create(
                model=settings.embedding.model,
                input=embed_text[:settings.embedding.max_chars],
                dimensions=settings.embedding.dimensions,
            )
            emb = resp.data[0].embedding
            emb_lit = "[" + ",".join(repr(x) for x in emb) + "]"
        except Exception as e:
            print(f"[restore] embed failed {node_id}: {e}", flush=True)
            dim = settings.embedding.dimensions
            emb_lit = "[" + ",".join(["0.0"] * dim) + "]"

        # tags
        tags_raw = m.get("tags") or []
        if isinstance(tags_raw, str):
            tags_raw = re.findall(r'"([^"]*)"', tags_raw) or [t.strip() for t in tags_raw.split(",")]
        tags = [str(t).strip() for t in tags_raw if t]

        # source
        source_type = (m.get("source_type") or "plaintext").lower()
        source_id = f"restored_{source_type}"

        raw_ref_str = str(m.get("storage_key") or "")
        if raw_ref_str.startswith("http"):
            raw_ref_dict: dict = {"type": "url", "url": raw_ref_str}
        elif "::chapter::" in raw_ref_str:
            raw_ref_dict = {"type": "book_chapter", "path": raw_ref_str}
        elif raw_ref_str:
            raw_ref_dict = {"type": "file", "path": raw_ref_str}
        else:
            raw_ref_dict = {}

        # dates
        created_at = m.get("created_at") or m.get("updated_at")
        if isinstance(created_at, str):
            try:
                created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            except Exception:
                created_at = None
        published_at = None
        for time_key in ("published_at", "effective_at", "source_published_at", "captured_at"):
            value = m.get(time_key)
            if isinstance(value, str) and value:
                try:
                    published_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    break
                except Exception:
                    continue
        published_at = published_at or created_at

        # entity fields
        canonical_name = m.get("canonical_name") or None
        aliases_raw = m.get("aliases") or []
        if isinstance(aliases_raw, str):
            aliases_raw = [a.strip().strip('"') for a in aliases_raw.strip("[]").split(",") if a.strip()]
        aliases = [str(a) for a in aliases_raw]

        # summary fields
        summary_of = m.get("summary_of") or None
        sources_raw = m.get("sources") or []
        if isinstance(sources_raw, str):
            sources_raw = [s.strip() for s in sources_raw.strip("[]").split(",") if s.strip()]
        source_node_ids = [str(s) for s in sources_raw]

        perspective = m.get("perspective") or None

        try:
            await database.database.execute(
                f"""
                INSERT INTO knowledge_nodes
                  (id, user_id, title, abstract, embedding, source_id,
                   tags, object_type, published_at, created_at, doc_kind, embedding_model)
                VALUES
                  (:id, :uid, :title, :abstract, '{emb_lit}'::vector,
                   :source_id, :tags,
                   :object_type, :published_at, :created_at, :doc_kind, :embedding_model)
                """,
                {
                    "id": node_id, "uid": user_id,
                    "title": str(m.get("title") or node_id),
                    "abstract": abstract,
                    "source_id": source_id,
                    "tags": tags,
                    "object_type": object_type,
                    "published_at": published_at,
                    "created_at": created_at,
                    "doc_kind": m.get("doc_kind") or settings.doc_kind.default,
                    "embedding_model": settings.embedding.model,
                },
            )
            await upsert_object_node(
                node_id,
                object_type,
                {
                    "source_item_id": None,
                    "raw_ref": raw_ref_dict,
                    "source_type": source_type,
                    "tags": tags,
                    "summary_of": summary_of,
                    "perspective_label": perspective or "default",
                    "perspective_instruction": perspective or "默认摘要",
                    "body": body or abstract,
                    "is_default": not bool(perspective),
                    "source": {"source_node_ids": source_node_ids, "restored_from_wiki": True},
                    "canonical_name": canonical_name,
                    "aliases": aliases,
                    "description": abstract,
                },
            )
            nodes_inserted += 1
            print(f"[restore] {object_type}: {node_id} — {m.get('title', '')}", flush=True)
        except Exception as e:
            print(f"[restore] insert error {node_id}: {e}", flush=True)
            nodes_skipped += 1

    # ── 4. Reconstruct edges ──────────────────────────────────────────────────
    edges_inserted = 0

    async def _add_edge(from_id: str, to_id: str, rel: str, weight: float = 1.0):
        nonlocal edges_inserted
        try:
            await database.database.execute(
                """
                INSERT INTO knowledge_edges
                  (from_node_id, to_node_id, relation_type, weight, created_by)
                VALUES (:f, :t, :r, :w, 'restore_from_wiki')
                ON CONFLICT DO NOTHING
                """,
                {"f": from_id, "t": to_id, "r": rel, "w": weight},
            )
            edges_inserted += 1
        except Exception as e:
            print(f"[restore] edge error {from_id}→{to_id}: {e}", flush=True)

    # Collect all known node IDs for validation
    known_ids: set[str] = {m["id"] for m in all_metas if m.get("id")}

    for m in all_metas:
        node_id = m["id"]
        object_type = str(m.get("type") or "article")

        # summarizes 关系由 summary_nodes.summary_of FK 表达，不在此处建边

        # legacy part_of relations are restored into index_children, not knowledge_edges.
        relations = m.get("relations") or []
        if isinstance(relations, list):
            for rel in relations:
                if isinstance(rel, dict) and rel.get("type") == "part_of" and rel.get("id"):
                    try:
                        await add_child(
                            rel["id"],
                            node_id,
                            user_id=user_id,
                            child_role="member",
                        )
                    except Exception as e:
                        print(f"[restore] index child error {rel['id']}→{node_id}: {e}", flush=True)

        # mentions: article/summary → entity  (scan [[entity_id|...]] in body)
        if object_type in ("article", "summary"):
            body = m.get("_body") or ""
            for target_id in set(re.findall(r'\[\[((?:ent|nod)[_a-z0-9A-Z]+)(?:\|[^\]]+)?\]\]', body)):
                if target_id in known_ids:
                    await _add_edge(node_id, target_id, "mentions", 0.5)

    print(f"[restore] done: {nodes_inserted} nodes, {edges_inserted} edges", flush=True)
    return {
        "nodes_inserted": nodes_inserted,
        "nodes_skipped": nodes_skipped,
        "edges_inserted": edges_inserted,
    }


async def rebuild_from_raw(
    user_id: str = "default",
    *,
    source_id: str | None = None,
    source_type: str | None = None,
    status: str | None = None,
    since: str | None = None,
    until: str | None = None,
    dry_run: bool = False,
    resume: bool = False,
) -> dict:
    """
    从 source_items manifest 重建知识库（幂等）。
    执行流程：
      1. 按 source_items manifest 选择待重建 item（支持 source/type/status/time filter）
      2. 删除对应 wiki 文件
      3. 将选中 source_items 重置为 pending，触发 ingestion-worker 重新处理
      4. 轮询等待选中 source_items 完成（最长 60 分钟）
      5. 运行 run_maintenance()

    须在 api 容器中执行：
      docker compose exec api python -m maintenance rebuild_from_raw --confirm
    """
    user_data_dir = pathlib.Path(os.environ.get("USER_DATA_DIR", "/app/user_data"))
    wiki_dir = user_data_dir / user_id / "wiki"
    ingestion_url = os.environ.get("INGESTION_WORKER_URL", "http://ingestion-worker:8001")

    filters = {
        "source_id": source_id,
        "source_type": source_type,
        "status": status,
        "since": since,
        "until": until,
        "resume": resume,
        "dry_run": dry_run,
    }

    # ── Step 1: 选择 manifest items ───────────────────────────────────────────
    print(f"[rebuild] Step 1: 选择 source_items manifest... {filters}", flush=True)

    where = ["si.user_id = :uid"]
    params: dict[str, Any] = {"uid": user_id}
    if source_id:
        where.append("si.source_id = :source_id")
        params["source_id"] = source_id
    if source_type:
        where.append("si.source_type = :source_type")
        params["source_type"] = source_type
    if status:
        where.append("si.status = :status")
        params["status"] = status
    if since:
        where.append(
            "COALESCE(si.effective_at, si.source_published_at, si.captured_at, si.created_at) >= :since"
        )
        params["since"] = _parse_rebuild_time(since)
    if until:
        where.append(
            "COALESCE(si.effective_at, si.source_published_at, si.captured_at, si.created_at) <= :until"
        )
        params["until"] = _parse_rebuild_time(until)
    if resume:
        where.append("si.status <> 'succeeded'")

    item_rows = await database.database.fetch_all(
        f"""
        SELECT si.id, si.source_id, si.source_type, si.status
        FROM source_items si
        WHERE {' AND '.join(where)}
        ORDER BY si.source_id, si.created_at ASC
        """,
        params,
    )
    item_ids = [r["id"] for r in item_rows]
    source_ids = sorted({r["source_id"] for r in item_rows})
    source_types = sorted({r["source_type"] for r in item_rows})

    if not item_ids:
        result = {
            "dry_run": dry_run,
            "filters": filters,
            "source_items_selected": 0,
            "sources_selected": 0,
            "nodes_deleted": 0,
            "wiki_files_deleted": 0,
            "sources_triggered": 0,
            "sources_failed": 0,
            "maintenance": None,
        }
        print(f"[rebuild] 无匹配 source_items: {json.dumps(result, ensure_ascii=False)}", flush=True)
        return result

    node_rows = await database.database.fetch_all(
        """
        SELECT n.id, n.object_type
        FROM knowledge_nodes n
        JOIN article_nodes an ON an.node_id = n.id
        WHERE n.user_id = :uid
          AND an.source_item_id = ANY(:item_ids)
          AND n.object_type = 'article'
        """,
        {"uid": user_id, "item_ids": item_ids},
    )
    base_ids = {r["id"] for r in node_rows}
    summary_rows = await database.database.fetch_all(
        """
        SELECT n.id
        FROM knowledge_nodes n
        JOIN summary_nodes sn ON sn.node_id = n.id
        WHERE n.user_id = :uid
          AND n.object_type = 'summary'
          AND sn.summary_of = ANY(:base_ids)
        """,
        {"uid": user_id, "base_ids": list(base_ids) or ["__none__"]},
    )
    summary_ids = {r["id"] for r in summary_rows}
    entity_rows = await database.database.fetch_all(
        """
        SELECT DISTINCT n.id
        FROM knowledge_nodes n
        LEFT JOIN entity_facts ef ON ef.entity_id = n.id
        LEFT JOIN knowledge_edges ke
          ON ke.to_node_id = n.id
         AND ke.relation_type IN ('mentions', 'wikilink')
        WHERE n.user_id = :uid
          AND n.object_type = 'entity'
          AND (
            ef.article_id = ANY(:base_ids)
            OR ke.from_node_id = ANY(:base_ids)
          )
        """,
        {"uid": user_id, "base_ids": list(base_ids) or ["__none__"]},
    )
    entity_ids = {r["id"] for r in entity_rows}
    deleted_ids = base_ids | summary_ids | entity_ids

    dry_run_result = {
        "dry_run": True,
        "filters": filters,
        "source_items_selected": len(item_ids),
        "sources_selected": len(source_ids),
        "source_types_selected": source_types,
        "nodes_to_delete": len(deleted_ids),
        "article_or_index_nodes_to_delete": len(base_ids),
        "summary_nodes_to_delete": len(summary_ids),
        "entity_nodes_to_delete": len(entity_ids),
    }
    if dry_run:
        print(f"[rebuild] dry run: {json.dumps(dry_run_result, ensure_ascii=False)}", flush=True)
        return dry_run_result

    # ── Step 2: 清空可重建内容 ────────────────────────────────────────────────
    print("[rebuild] Step 2: 清空选中 manifest 对应内容...", flush=True)

    ec_before_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM entity_candidates WHERE user_id = :uid",
        {"uid": user_id},
    )
    ec_before = int(ec_before_row["n"]) if ec_before_row else 0
    await database.database.execute(
        """
        DELETE FROM entity_candidates
        WHERE user_id = :uid
          AND source_article_ids && CAST(:base_ids AS text[])
        """,
        {"uid": user_id, "base_ids": list(base_ids) or ["__none__"]},
    )
    ec_after_row = await database.database.fetch_one(
        "SELECT COUNT(*) AS n FROM entity_candidates WHERE user_id = :uid",
        {"uid": user_id},
    )
    ec_after = int(ec_after_row["n"]) if ec_after_row else 0
    ec_deleted = ec_before - ec_after

    if deleted_ids:
        await database.database.execute(
            """
            DELETE FROM knowledge_nodes
            WHERE user_id = :uid AND id = ANY(:ids)
            """,
            {"uid": user_id, "ids": list(deleted_ids)},
        )

    print(
        f"[rebuild] 已清空: nodes={len(deleted_ids)}, "
        f"base={len(base_ids)}, summaries={len(summary_ids)}, entities={len(entity_ids)}, "
        f"entity_candidates_deleted={ec_deleted}",
        flush=True,
    )

    # ── Step 2b: 清理 wiki 文件 ───────────────────────────────────────────────
    wiki_deleted = 0

    # 按 ID 删除 article/entity/index/summary 的 wiki 文件
    for subdir in ("articles", "entities", "indices", "summaries"):
        d = wiki_dir / subdir
        if not d.exists():
            continue
        for f in d.iterdir():
            if f.is_file() and f.suffix == ".md" and f.stem in deleted_ids:
                f.unlink()
                wiki_deleted += 1

    print(f"[rebuild] 已删除 wiki 文件: {wiki_deleted} 个", flush=True)

    # ── Step 3: 重置 source_items，触发 ingestion-worker ────────────────────
    print("[rebuild] Step 3: 触发 ingestion-worker...", flush=True)

    await database.database.execute(
        """
        UPDATE source_items
        SET status = 'pending', error = NULL, attempts = 0, updated_at = NOW()
        WHERE user_id = :uid AND id = ANY(:item_ids)
        """,
        {"uid": user_id, "item_ids": item_ids},
    )
    await database.database.execute(
        """
        UPDATE sources
        SET last_fetched_at = NULL
        WHERE user_id = :uid AND id = ANY(:source_ids)
        """,
        {"uid": user_id, "source_ids": source_ids},
    )

    sources = await database.database.fetch_all(
        """
        SELECT id, name
        FROM sources
        WHERE user_id = :uid AND id = ANY(:source_ids)
        ORDER BY created_at ASC
        """,
        {"uid": user_id, "source_ids": source_ids},
    )

    triggered: list[str] = []
    failed: list[str] = []
    async with httpx.AsyncClient() as http:
        for src in sources:
            try:
                resp = await http.post(f"{ingestion_url}/trigger/{src['id']}", timeout=10)
                data = resp.json()
                if resp.status_code == 200 and data.get("ok"):
                    triggered.append(src["id"])
                    print(f"[rebuild]   触发成功: {src['name']} ({src['id']})", flush=True)
                else:
                    failed.append(src["id"])
                    print(f"[rebuild]   触发失败: {src['name']} — {data.get('detail', resp.text)}", flush=True)
            except Exception as e:
                failed.append(src["id"])
                print(f"[rebuild]   触发异常: {src['name']} — {e}", flush=True)

    if not triggered:
        print(
            "[rebuild] 警告：未能触发任何 source（ingestion-worker 是否在运行？），跳过等待步骤。\n"
            "[rebuild] 可待 ingestion-worker 启动后手动触发，或重新执行 rebuild_from_raw。",
            flush=True,
        )
    else:
        # ── Step 4: 轮询等待所有 source 完成 ──────────────────────────────────
        print(
            f"[rebuild] Step 4: 等待 {len(item_ids)} 个 source_items 完成（最长 60 分钟）...",
            flush=True,
        )
        max_wait = settings.maintenance.rebuild_max_wait_seconds
        interval = settings.maintenance.rebuild_poll_interval_seconds
        elapsed = 0
        pending_count = len(item_ids)

        while elapsed < max_wait and pending_count:
            await asyncio.sleep(interval)
            elapsed += interval
            row = await database.database.fetch_one(
                """
                SELECT COUNT(*) AS n
                FROM source_items
                WHERE user_id = :uid
                  AND id = ANY(:item_ids)
                  AND status IN ('pending', 'processing')
                """,
                {"uid": user_id, "item_ids": item_ids},
            )
            still_count = int(row["n"]) if row else 0
            if still_count < pending_count:
                done = len(item_ids) - still_count
                print(
                    f"[rebuild]   进度: {done}/{len(item_ids)} 完成 ({elapsed}s 已过)",
                    flush=True,
                )
            pending_count = still_count

        if pending_count:
            print(f"[rebuild] 警告：超时，仍有 {pending_count} 个 source_items 未完成", flush=True)
        else:
            print("[rebuild] 所有选中 source_items 已完成 ingestion", flush=True)

    # ── Step 5: 运行维护任务 ──────────────────────────────────────────────────
    print("[rebuild] Step 5: 运行维护任务（entity 晋升、wikilink 回灌等）...", flush=True)
    from maintenance import run_maintenance
    maintenance_result = await run_maintenance(user_id)

    result = {
        "entity_candidates_deleted": ec_deleted,
        "entities_deleted": len(entity_ids),
        "article_or_index_nodes_deleted": len(base_ids),
        "summary_nodes_deleted": len(summary_ids),
        "source_items_selected": len(item_ids),
        "sources_selected": len(source_ids),
        "source_types_selected": source_types,
        "wiki_files_deleted": wiki_deleted,
        "sources_triggered": len(triggered),
        "sources_failed": len(failed),
        "filters": filters,
        "maintenance": maintenance_result,
    }
    print(f"[rebuild] 完成: {json.dumps(result, ensure_ascii=False)}", flush=True)
    return result
