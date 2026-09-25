"""Entity page behavior against an isolated PostgreSQL database."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

if not os.environ.get("TEST_DATABASE_URL"):
    raise unittest.SkipTest("Run with scripts/test_document_lifecycle.sh")

from database import database
import jobs
from entity_knowledge_audit import inspect_entity, repair_entity
from kb import entity, entity_knowledge, ingest, internal, wiki


class EntityKnowledgeDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.connect()
        suffix = uuid4().hex[:12]
        self.entity_id = f"ent_{suffix}"
        self.article_id = f"art_{suffix}"
        self.storage = tempfile.TemporaryDirectory()
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '印度', '旧说明', 'entity')",
            {"id": self.entity_id},
        )
        await database.execute(
            "INSERT INTO entity_nodes (node_id, canonical_name) VALUES (:id, '印度')",
            {"id": self.entity_id},
        )
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '第二篇文章', '第二篇文章讲述某项政策', 'article')",
            {"id": self.article_id},
        )
        self.text_path = Path(self.storage.name) / "default" / "extracted" / "article.txt"
        self.text_path.parent.mkdir(parents=True)
        self.text_path.write_text("第二篇文章讲述某项政策，介绍其实施时间和影响。", encoding="utf-8")
        await database.execute(
            "INSERT INTO article_nodes (node_id, source_type, extracted_text_ref) VALUES (:id, 'url', :ref)",
            {"id": self.article_id, "ref": str(self.text_path)},
        )
        wiki = Path(self.storage.name) / "default" / "wiki" / "entities"
        wiki.mkdir(parents=True)
        (wiki / f"{self.entity_id}.md").write_text("---\nid: test\n---\n\n# 印度\n\n旧说明\n")

    async def asyncTearDown(self):
        await database.execute("DELETE FROM knowledge_nodes WHERE id = :id", {"id": self.article_id})
        await database.execute("DELETE FROM knowledge_nodes WHERE id = :id", {"id": self.entity_id})
        await database.disconnect()
        self.storage.cleanup()

    async def test_new_article_for_existing_entity_updates_visible_page(self):
        await ingest.do_process_entity_candidates(ingest.ProcessCandidatesRequest(
            article_id=self.article_id,
            entities=[ingest.EntityCandidateItem(
                name="印度", aliases=[], salience=0.9,
                matches_existing_entity_id=self.entity_id,
                summary_hint="第二篇文章讲述某项政策",
            )],
        ))
        revision = await database.fetch_val(
            "SELECT requested_revision FROM entity_nodes WHERE node_id = :id", {"id": self.entity_id}
        )
        async def fake_generator(entity, sources):
            self.assertEqual([self.article_id], [source["id"] for source in sources])
            self.assertIn("实施时间", sources[0]["text"])
            return f"第二篇文章讲述某项政策 [[{self.article_id}]]", "第二篇文章讲述某项政策"

        with patch.object(wiki, "USER_DATA_DIR", Path(self.storage.name)), patch.object(entity_knowledge, "USER_DATA_DIR", Path(self.storage.name)):
            async def fake_embedder(_):
                return None
            await entity_knowledge.run_refresh(self.entity_id, revision, generator=fake_generator, embedder=fake_embedder)
            detail = await internal.get_node(self.entity_id, _={})
        self.assertIn("第二篇文章讲述某项政策", detail["wiki_body"])

    async def test_new_revision_cannot_be_overwritten_by_older_generation(self):
        await entity_knowledge.record_contribution(self.entity_id, self.article_id)
        old_revision = await database.fetch_val(
            "SELECT requested_revision FROM entity_nodes WHERE node_id = :id", {"id": self.entity_id}
        )
        second_id = f"art_{uuid4().hex[:12]}"
        second_ref = self.text_path.with_name("third.txt")
        second_ref.write_text("第三篇文章补充后续政策执行情况。", encoding="utf-8")
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '第三篇文章', '后续执行', 'article')", {"id": second_id},
        )
        await database.execute(
            "INSERT INTO article_nodes (node_id, source_type, extracted_text_ref) VALUES (:id, 'url', :ref)",
            {"id": second_id, "ref": str(second_ref)},
        )
        try:
            await entity_knowledge.record_contribution(self.entity_id, second_id)
            async def unused(*_):
                self.fail("过时任务不应调用模型")
            stale = await entity_knowledge.run_refresh(
                self.entity_id, old_revision, generator=unused,
            )
            self.assertEqual(stale["reason"], "superseded")
            revision = old_revision + 1
            body = "政策细节" * 180 + f" [[{self.article_id}]] [[{second_id}]]"
            async def generate(_, sources):
                self.assertEqual({self.article_id, second_id}, {s["id"] for s in sources})
                return body, "两篇文章共同说明政策"
            async def no_embedding(_):
                return None
            with patch.object(wiki, "USER_DATA_DIR", Path(self.storage.name)), patch.object(entity_knowledge, "USER_DATA_DIR", Path(self.storage.name)):
                await entity_knowledge.run_refresh(
                    self.entity_id, revision, generator=generate, embedder=no_embedding,
                )
                await wiki.write_wiki_node(self.entity_id, "default")
                detail = await internal.get_node(self.entity_id, _={})
            self.assertEqual(detail["wiki_body"], body)
            self.assertEqual(detail["knowledge_status"], "published")
            self.assertIn(body, (Path(self.storage.name) / "default" / "wiki" / "entities" / f"{self.entity_id}.md").read_text())
        finally:
            await database.execute("DELETE FROM knowledge_nodes WHERE id = :id", {"id": second_id})

    async def test_article_delete_removes_source_and_requests_refresh(self):
        await entity_knowledge.record_contribution(self.entity_id, self.article_id)
        with patch.object(internal, "_wiki_file_path", return_value=self.text_path.with_name("unused.md")):
            await internal.do_delete_node(self.article_id)
        self.assertEqual(await database.fetch_val(
            "SELECT count(*) FROM entity_sources WHERE entity_id = :id", {"id": self.entity_id}
        ), 0)
        self.assertEqual(await database.fetch_val(
            "SELECT requested_revision FROM entity_nodes WHERE node_id = :id", {"id": self.entity_id}
        ), 2)

    async def test_merge_deduplicates_sources_and_invalidates_old_job(self):
        source_id = f"ent_{uuid4().hex[:12]}"
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '旧印度', '', 'entity')", {"id": source_id},
        )
        await database.execute(
            "INSERT INTO entity_nodes (node_id, canonical_name) VALUES (:id, '旧印度')",
            {"id": source_id},
        )
        try:
            await entity_knowledge.record_contribution(self.entity_id, self.article_id)
            await entity_knowledge.record_contribution(source_id, self.article_id)
            await entity.do_merge_entities(source_id, self.entity_id)
            self.assertEqual(await database.fetch_val(
                "SELECT count(*) FROM entity_sources WHERE entity_id = :id", {"id": self.entity_id}
            ), 1)
            self.assertEqual(await database.fetch_val(
                "SELECT requested_revision FROM entity_nodes WHERE node_id = :id", {"id": self.entity_id}
            ), 2)
            result = await entity_knowledge.run_refresh(source_id, 1)
            self.assertEqual(result["reason"], "entity_missing_or_merged")
        finally:
            await database.execute("DELETE FROM knowledge_nodes WHERE id = :id", {"id": source_id})

    async def test_repeated_contribution_is_idempotent_and_bad_citation_keeps_old_body(self):
        await entity_knowledge.record_contribution(self.entity_id, self.article_id)
        await entity_knowledge.record_contribution(self.entity_id, self.article_id)
        self.assertEqual(await database.fetch_val(
            "SELECT requested_revision FROM entity_nodes WHERE node_id = :id", {"id": self.entity_id}
        ), 1)
        async def invalid_page(*_):
            return "无法核对的事实 [[art_elsewhere]]", "无法核对的事实"
        with patch.object(entity_knowledge, "USER_DATA_DIR", Path(self.storage.name)):
            with self.assertRaises(entity_knowledge.EntityKnowledgeError):
                await entity_knowledge.run_refresh(self.entity_id, 1, generator=invalid_page)
        page = await entity_knowledge.read_body(self.entity_id)
        self.assertIsNone(page["body_markdown"])
        self.assertEqual(page["published_revision"], 0)

    async def test_legacy_audit_is_read_only_and_repair_imports_page(self):
        await database.execute(
            """INSERT INTO knowledge_edges
               (from_node_id, to_node_id, relation_type, weight, created_by)
               VALUES (:article, :entity, 'mentions', 0.5, 'legacy')""",
            {"article": self.article_id, "entity": self.entity_id},
        )
        with patch.object(wiki, "USER_DATA_DIR", Path(self.storage.name)):
            report = await inspect_entity(self.entity_id, "default")
            self.assertEqual(report["recoverable_source_ids"], [self.article_id])
            self.assertIsNone(report["body_length"])
            self.assertEqual(await database.fetch_val(
                "SELECT count(*) FROM entity_sources WHERE entity_id = :id", {"id": self.entity_id}
            ), 0)
            await repair_entity(report, "default")
        page = await entity_knowledge.read_body(self.entity_id)
        self.assertEqual(page["body_markdown"], "旧说明")
        self.assertEqual(await database.fetch_val(
            "SELECT count(*) FROM entity_sources WHERE entity_id = :id", {"id": self.entity_id}
        ), 1)

    async def test_reclaimed_job_old_attempt_cannot_change_new_attempt_status(self):
        job = await jobs.enqueue_job("refresh_entity", {"entity_id": self.entity_id, "revision": 1})
        await database.execute(
            "UPDATE jobs SET status = 'running', attempts = 2 WHERE id = :id", {"id": job["id"]}
        )
        await jobs.complete_job(job["id"], {"status": "published"}, attempt=1)
        await jobs.fail_job(job["id"], "late failure", attempt=1)
        self.assertEqual(await database.fetch_val(
            "SELECT status FROM jobs WHERE id = :id", {"id": job["id"]}
        ), "running")
        await jobs.complete_job(job["id"], {"status": "published"}, attempt=2)
        self.assertEqual(await database.fetch_val(
            "SELECT status FROM jobs WHERE id = :id", {"id": job["id"]}
        ), "succeeded")

    async def test_first_new_contribution_recovers_legacy_sources(self):
        await database.execute(
            """INSERT INTO knowledge_edges
               (from_node_id, to_node_id, relation_type, weight, created_by)
               VALUES (:article, :entity, 'mentions', 0.5, 'legacy')""",
            {"article": self.article_id, "entity": self.entity_id},
        )
        newer_id = f"art_{uuid4().hex[:12]}"
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '新文章', '新事实', 'article')", {"id": newer_id},
        )
        try:
            await entity_knowledge.record_contribution(self.entity_id, newer_id)
            sources = await database.fetch_all(
                "SELECT article_id FROM entity_sources WHERE entity_id = :id ORDER BY article_id",
                {"id": self.entity_id},
            )
            self.assertEqual({self.article_id, newer_id}, {row["article_id"] for row in sources})
            self.assertEqual(await database.fetch_val(
                "SELECT requested_revision FROM entity_nodes WHERE node_id = :id", {"id": self.entity_id}
            ), 1)
        finally:
            await database.execute("DELETE FROM knowledge_nodes WHERE id = :id", {"id": newer_id})

    async def test_first_new_contribution_recovers_wiki_source_metadata(self):
        legacy = Path(self.storage.name) / "default" / "wiki" / "entities" / f"{self.entity_id}.md"
        legacy.write_text(f"---\nsources:\n  - {self.article_id}\n---\n\n# 印度\n\n旧说明\n")
        newer_id = f"art_{uuid4().hex[:12]}"
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '新文章', '新事实', 'article')", {"id": newer_id},
        )
        try:
            with patch.object(wiki, "USER_DATA_DIR", Path(self.storage.name)):
                await entity_knowledge.record_contribution(self.entity_id, newer_id)
            sources = await database.fetch_all(
                "SELECT article_id FROM entity_sources WHERE entity_id = :id",
                {"id": self.entity_id},
            )
            self.assertEqual({self.article_id, newer_id}, {row["article_id"] for row in sources})
            self.assertEqual((await entity_knowledge.read_body(self.entity_id))["body_markdown"], "旧说明")
        finally:
            await database.execute("DELETE FROM knowledge_nodes WHERE id = :id", {"id": newer_id})

    async def test_promotion_links_source_without_placeholder_fact(self):
        candidate = await database.fetch_one(
            """INSERT INTO entity_candidates
               (user_id, canonical_name, source_article_ids, mention_count, max_salience)
               VALUES ('default', '印度', ARRAY[:article]::text[], 1, 0.9) RETURNING id""",
            {"article": self.article_id},
        )
        try:
            result = await ingest.mark_candidate_promoted(
                candidate["id"], {"entity_node_id": self.entity_id}, _={},
            )
            self.assertEqual(result["sources_linked"], 1)
            self.assertEqual(await database.fetch_val(
                "SELECT count(*) FROM entity_sources WHERE entity_id = :id", {"id": self.entity_id}
            ), 1)
            self.assertEqual(await database.fetch_val(
                "SELECT count(*) FROM entity_facts WHERE entity_id = :id", {"id": self.entity_id}
            ), 0)
        finally:
            await database.execute("DELETE FROM entity_candidates WHERE id = :id", {"id": candidate["id"]})
