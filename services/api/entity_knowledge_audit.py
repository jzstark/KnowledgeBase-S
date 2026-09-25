#!/usr/bin/env python3
"""Audit entity evidence and optionally restore legacy links and Wiki bodies.

Default mode only reads. --apply requires --snapshot and queues refresh jobs for
restored sources; it never calls a model in this process.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path

import yaml

from database import database
from kb.entity_knowledge import import_legacy_body, record_contribution, request_refresh
from kb.common import split_frontmatter
from kb.wiki import read_wiki_body, wiki_file_path


async def inspect_entity(entity_id: str, user_id: str) -> dict:
    row = await database.fetch_one(
        """SELECT n.id, n.title, n.abstract, en.body_markdown,
                  en.requested_revision, en.published_revision, en.body_published_at,
                  en.abstract_stale
           FROM knowledge_nodes n JOIN entity_nodes en ON en.node_id = n.id
           WHERE n.id = :id AND n.user_id = :uid AND en.merged_into IS NULL""",
        {"id": entity_id, "uid": user_id},
    )
    if not row:
        raise ValueError(f"entity 不存在：{entity_id}")
    sources = await database.fetch_all(
        """SELECT article_id FROM entity_sources WHERE entity_id = :id ORDER BY article_id""",
        {"id": entity_id},
    )
    candidates = await database.fetch_all(
        """SELECT DISTINCT article.id AS article_id
           FROM knowledge_nodes article
           WHERE article.user_id = :uid AND article.object_type = 'article' AND (
             EXISTS (SELECT 1 FROM entity_facts ef
                     WHERE ef.entity_id = :id AND ef.article_id = article.id)
             OR EXISTS (SELECT 1 FROM knowledge_edges ke
                        WHERE ke.from_node_id = article.id AND ke.to_node_id = :id
                          AND ke.relation_type IN ('mentions', 'wikilink'))
             OR EXISTS (SELECT 1 FROM entity_candidates ec
                        WHERE ec.promoted_entity_id = :id
                          AND article.id = ANY(ec.source_article_ids))
           ) ORDER BY article.id""",
        {"id": entity_id, "uid": user_id},
    )
    source_ids = [r["article_id"] for r in sources]
    candidate_ids = [r["article_id"] for r in candidates]
    wiki_sources = []
    wiki_path = wiki_file_path(user_id, entity_id, "entity")
    if wiki_path.is_file():
        try:
            metadata = yaml.safe_load(split_frontmatter(wiki_path.read_text(encoding="utf-8"))[0]) or {}
            listed = (metadata.get("sources") or []) if isinstance(metadata, dict) else []
            if isinstance(listed, str):
                listed = [part.strip() for part in listed.strip("[]").split(",") if part.strip()]
            if isinstance(listed, list):
                wiki_sources = [str(value) for value in listed]
        except (OSError, yaml.YAMLError):
            pass
    if wiki_sources:
        valid = await database.fetch_all(
            """SELECT id FROM knowledge_nodes WHERE id = ANY(:ids)
               AND user_id = :uid AND object_type = 'article'""",
            {"ids": wiki_sources, "uid": user_id},
        )
        candidate_ids.extend(row["id"] for row in valid)
    fact_rows = await database.fetch_all(
        "SELECT DISTINCT article_id FROM entity_facts WHERE entity_id = :id AND article_id IS NOT NULL",
        {"id": entity_id},
    )
    mention_rows = await database.fetch_all(
        """SELECT DISTINCT from_node_id FROM knowledge_edges
           WHERE to_node_id = :id AND relation_type = 'mentions'""",
        {"id": entity_id},
    )
    fact_ids = {r["article_id"] for r in fact_rows}
    mention_ids = {r["from_node_id"] for r in mention_rows}
    promoted = await database.fetch_val(
        "SELECT count(*) FROM entity_candidates WHERE promoted_entity_id = :id",
        {"id": entity_id},
    )
    job = await database.fetch_one(
        """SELECT status, error FROM jobs WHERE job_type = 'refresh_entity'
           AND idempotency_key = :key ORDER BY created_at DESC LIMIT 1""",
        {"key": f"entity:{entity_id}:{row['requested_revision']}"},
    )
    wiki_body = read_wiki_body(user_id, entity_id, "entity", limit=None)
    evidence = await database.fetch_all(
        """SELECT n.id, n.abstract, an.extracted_text_ref, an.raw_ref->>'type' AS raw_type,
                  si.status AS item_status, si.extracted_text_ref AS item_text_ref
           FROM entity_sources es JOIN knowledge_nodes n ON n.id = es.article_id
           LEFT JOIN article_nodes an ON an.node_id = n.id
           LEFT JOIN source_items si ON si.id = an.source_item_id
           WHERE es.entity_id = :id ORDER BY n.id""",
        {"id": entity_id},
    )
    material = {}
    material_bytes = {}
    for source in evidence:
        ref = source["extracted_text_ref"] or (
            source["item_text_ref"] if source["raw_type"] != "book_chapter" else None
        )
        material[source["id"]] = (
            "full_text" if ref and Path(ref).is_file()
            else "missing_file" if ref
            else "pending" if source["item_status"] in ("pending", "processing")
            else "missing_chapter" if source["raw_type"] == "book_chapter"
            else "abstract_only" if source["abstract"] else "missing"
        )
        material_bytes[source["id"]] = (
            Path(ref).stat().st_size if material[source["id"]] == "full_text"
            else len((source["abstract"] or "").encode()) if material[source["id"]] == "abstract_only"
            else 0
        )
    body = row["body_markdown"]
    return {
        "entity_id": entity_id, "title": row["title"],
        "source_ids": source_ids, "wiki_source_ids": wiki_sources,
        "recoverable_source_ids": sorted(set(candidate_ids) - set(source_ids)),
        "fact_source_ids": sorted(fact_ids), "mention_source_ids": sorted(mention_ids),
        "facts_without_mentions": sorted(fact_ids - mention_ids),
        "mentions_without_facts": sorted(mention_ids - fact_ids),
        "promoted_candidates": promoted,
        "refresh_job_status": job["status"] if job else None,
        "refresh_job_error": job["error"] if job else None,
        "source_material": material,
        "source_material_bytes": material_bytes,
        "requested_revision": row["requested_revision"],
        "published_revision": row["published_revision"],
        "body_published_at": row["body_published_at"].isoformat() if row["body_published_at"] else None,
        "stale": row["abstract_stale"],
        "body_length": len(body) if body is not None else None,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest() if body is not None else None,
        "abstract_length": len(row["abstract"] or ""),
        "wiki_length": len(wiki_body),
        "wiki_sha256": hashlib.sha256(wiki_body.encode()).hexdigest() if wiki_body else None,
        "body": body, "abstract": row["abstract"], "wiki_body": wiki_body,
    }


async def repair_entity(item: dict, user_id: str) -> dict:
    entity_id = item["entity_id"]
    imported = False
    async with database.transaction():
        if item["body"] is None and item["wiki_body"]:
            imported = await import_legacy_body(entity_id, item["wiki_body"], user_id=user_id)
        for article_id in item["recoverable_source_ids"]:
            await record_contribution(entity_id, article_id, user_id=user_id)
        if (item["source_ids"] and not item["recoverable_source_ids"]
                and item["requested_revision"] == item["published_revision"]
                and item["body_published_at"] is None):
            await request_refresh(entity_id, user_id=user_id)
    return {"entity_id": entity_id, "imported_wiki": imported,
            "linked_sources": len(item["recoverable_source_ids"])}


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", default="default")
    parser.add_argument("--entity", action="append", help="Repeat for selected entities")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--after", default="", help="Continue after this entity ID")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--snapshot", type=Path, help="New JSON file required before --apply")
    args = parser.parse_args()
    if args.apply and not args.snapshot:
        parser.error("--apply requires --snapshot")
    if args.limit < 1:
        parser.error("--limit must be positive")
    await database.connect()
    try:
        if args.entity:
            ids = args.entity
        else:
            rows = await database.fetch_all(
                """SELECT en.node_id FROM entity_nodes en JOIN knowledge_nodes n ON n.id = en.node_id
                   WHERE n.user_id = :uid AND en.merged_into IS NULL AND en.node_id > :after
                   ORDER BY en.node_id LIMIT :limit""",
                {"uid": args.user_id, "limit": args.limit, "after": args.after},
            )
            ids = [r["node_id"] for r in rows]
        items = [await inspect_entity(entity_id, args.user_id) for entity_id in ids]
        if args.snapshot:
            descriptor = os.open(args.snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(items, handle, ensure_ascii=False, indent=2)
        results = [await repair_entity(item, args.user_id) for item in items] if args.apply else []
        print(json.dumps({
            "mode": "apply" if args.apply else "dry_run", "inspected": len(items),
            "last_entity_id": ids[-1] if ids else None,
            "missing_body": sum(item["body"] is None for item in items),
            "recoverable_links": sum(len(item["recoverable_source_ids"]) for item in items),
            "missing_material": sum(value in ("missing", "missing_file", "missing_chapter")
                                    for item in items for value in item["source_material"].values()),
            "source_material_bytes": sum(
                sum(item["source_material_bytes"].values()) for item in items
            ),
            "results": results,
        }, ensure_ascii=False))
    finally:
        await database.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
