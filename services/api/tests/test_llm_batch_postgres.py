"""Batched model calls: jobs wait for batch results and resume against PostgreSQL."""

import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

if not os.environ.get("TEST_DATABASE_URL"):
    raise unittest.SkipTest("Run with scripts/test_document_lifecycle.sh")

from anthropic.types import Message

import database as database_module
from database import database
import job_worker
import jobs
import llm_batch
from kb import entity_knowledge, ingest, retrieval, wiki


def message(text: str) -> Message:
    return Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
    })


class FakeBatches:
    def __init__(self, reply):
        self.reply = reply
        self.created: list[list[dict]] = []

    async def create(self, requests):
        self.created.append(list(requests))
        return SimpleNamespace(id=f"batch_{len(self.created)}")

    async def retrieve(self, batch_id):
        return SimpleNamespace(processing_status="ended")

    async def results(self, batch_id):
        requests = self.created[int(batch_id.split("_")[1]) - 1]

        async def items():
            for request in requests:
                yield SimpleNamespace(custom_id=request["custom_id"], result=SimpleNamespace(
                    type="succeeded", message=message(self.reply(request["params"])),
                ))
        return items()


class FakeClient:
    def __init__(self, reply):
        self.messages = SimpleNamespace(batches=FakeBatches(reply))


async def claim(job_id: str) -> dict:
    row = await database.fetch_one(
        """UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = NOW()
           WHERE id = :id RETURNING *""",
        {"id": job_id},
    )
    return jobs._job(row)


class LlmBatchDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.connect()
        suffix = uuid4().hex[:12]
        self.entity_id = f"ent_{suffix}"
        self.article_id = f"art_{suffix}"
        self.storage = tempfile.TemporaryDirectory()
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '印度', '', 'entity')", {"id": self.entity_id},
        )
        await database.execute(
            "INSERT INTO entity_nodes (node_id, canonical_name) VALUES (:id, '印度')", {"id": self.entity_id},
        )
        await database.execute(
            "INSERT INTO knowledge_nodes (id, user_id, title, abstract, object_type) "
            "VALUES (:id, 'default', '政策文章', '政策', 'article')", {"id": self.article_id},
        )
        text_path = Path(self.storage.name) / "default" / "extracted" / f"{suffix}.txt"
        text_path.parent.mkdir(parents=True)
        text_path.write_text("印度宣布了一项新的财政政策。", encoding="utf-8")
        await database.execute(
            "INSERT INTO article_nodes (node_id, source_type, extracted_text_ref) VALUES (:id, 'url', :ref)",
            {"id": self.article_id, "ref": str(text_path)},
        )

    async def asyncTearDown(self):
        await database.execute("DELETE FROM llm_requests")
        await database.execute("DELETE FROM knowledge_nodes WHERE id IN (:a, :e)",
                               {"a": self.article_id, "e": self.entity_id})
        await database.disconnect()
        self.storage.cleanup()

    async def test_entity_refresh_waits_for_batch_then_publishes(self):
        def reply(params):
            article_ids = sorted(set(re.findall(r"\[\[(art_\w+)\]\]", params["messages"][0]["content"])))
            return "<page>财政政策 " + " ".join(f"[[{a}]]" for a in article_ids) + "</page><summary>财政政策简介</summary>"

        async def no_embedding(_):
            return None

        client = FakeClient(reply)
        job = await entity_knowledge.record_contribution(self.entity_id, self.article_id)
        with (
            patch.object(llm_batch, "_client", lambda: client),
            patch.object(retrieval, "embed_text", no_embedding),
            patch.object(entity_knowledge, "USER_DATA_DIR", Path(self.storage.name)),
            patch.object(wiki, "USER_DATA_DIR", Path(self.storage.name)),
        ):
            await job_worker.process_job(await claim(job["id"]))
            waiting = await jobs.get_job(job["id"])
            self.assertEqual((waiting["status"], waiting["attempts"]), ("waiting", 0))
            self.assertEqual(await database.fetch_val(
                "SELECT status FROM llm_requests WHERE owner = :owner", {"owner": f"job:{job['id']}"}
            ), "pending")

            await llm_batch.dispatch()
            self.assertEqual(len(client.messages.batches.created), 1)
            self.assertEqual((await jobs.get_job(job["id"]))["status"], "pending")

            await job_worker.process_job(await claim(job["id"]))
        self.assertEqual((await jobs.get_job(job["id"]))["status"], "succeeded")
        page = await entity_knowledge.read_body(self.entity_id)
        self.assertEqual(page["body_markdown"], f"财政政策 [[{self.article_id}]]")
        self.assertEqual(await database.fetch_val(
            "SELECT count(*) FROM llm_requests WHERE owner = :owner", {"owner": f"job:{job['id']}"}
        ), 0)

    async def test_failed_batch_request_is_resubmitted_then_reported(self):
        owner = f"job:{uuid4().hex}"
        params = {"model": "claude-haiku-4-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
        with self.assertRaises(llm_batch.Deferred):
            await llm_batch.call(owner, "k", params)
        await database.execute(
            "UPDATE llm_requests SET status = 'failed', attempts = 1, error = 'overloaded' WHERE owner = :o",
            {"o": owner},
        )
        with self.assertRaises(llm_batch.Deferred):
            await llm_batch.call(owner, "k", params)
        self.assertEqual(await database.fetch_val(
            "SELECT status FROM llm_requests WHERE owner = :o", {"o": owner}
        ), "pending")
        await database.execute(
            "UPDATE llm_requests SET status = 'failed', attempts = 3, error = 'overloaded' WHERE owner = :o",
            {"o": owner},
        )
        with self.assertRaisesRegex(llm_batch.LlmRequestError, "overloaded"):
            await llm_batch.call(owner, "k", params)

    async def test_abandoned_owner_requests_are_dropped_before_submission(self):
        suffix = uuid4().hex[:8]
        live_job = await jobs.enqueue_job("generate_summary", {}, user_id="default")
        cancelled_job = await jobs.enqueue_job("generate_summary", {}, user_id="default")
        await jobs.cancel_job(cancelled_job["id"], "default")
        items = {"pending": f"si_p_{suffix}", "succeeded": f"si_s_{suffix}", "failed": f"si_f_{suffix}"}
        for status, item_id in items.items():
            await database.execute(
                "INSERT INTO source_items (id, user_id, source_type, origin_ref, origin_ref_type, status) "
                "VALUES (:id, 'default', 'url', :origin, 'url', :status)",
                {"id": item_id, "origin": f"https://example.test/{item_id}", "status": status},
            )
        await database.execute(
            "UPDATE source_items SET updated_at = NOW() - INTERVAL '31 days' WHERE id = :id",
            {"id": items["failed"]},
        )
        owners = {
            f"job:{live_job['id']}": True,
            f"job:{cancelled_job['id']}": False,
            "job:job_missing": False,
            f"source_item:{items['pending']}": True,
            f"source_item:{items['succeeded']}": False,
            f"source_item:{items['failed']}": False,
            f"source_item:si_missing_{suffix}": False,
        }
        try:
            params = {"model": "claude-haiku-4-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
            for owner in owners:
                with self.assertRaises(llm_batch.Deferred):
                    await llm_batch.call(owner, "k", params)
            await llm_batch._drop_abandoned()
            kept = {row["owner"] for row in await database.fetch_all("SELECT owner FROM llm_requests")}
            self.assertEqual(kept, {owner for owner, keep in owners.items() if keep})
        finally:
            await database.execute(
                "DELETE FROM jobs WHERE id IN (:a, :b)", {"a": live_job["id"], "b": cancelled_job["id"]}
            )
            await database.execute(
                "DELETE FROM source_items WHERE id = ANY(CAST(:ids AS VARCHAR[]))", {"ids": list(items.values())}
            )

    async def test_ingestion_endpoint_reports_waiting_then_message(self):
        body = ingest.LlmCallRequest(
            owner=f"source_item:si_{uuid4().hex[:8]}", key="analysis",
            params={"model": "claude-haiku-4-5", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
        )
        waiting = await ingest.llm_call(body, _={})
        self.assertEqual(waiting.status_code, 202)
        await database.execute(
            "UPDATE llm_requests SET status = 'succeeded', response = :r WHERE owner = :o",
            {"o": body.owner, "r": database_module.jsonb(message("分析结果").model_dump(mode="json"))},
        )
        done = await ingest.llm_call(body, _={})
        self.assertEqual(done["message"]["content"][0]["text"], "分析结果")
