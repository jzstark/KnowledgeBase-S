import asyncio
import os
import time
import traceback

import database
import jobs


POLL_INTERVAL_SECONDS = float(os.environ.get("JOB_WORKER_POLL_INTERVAL", "2"))
# A job stuck in 'running' longer than this is treated as abandoned (crashed
# worker) and reclaimed. Must exceed the longest expected job duration.
STUCK_TIMEOUT_SECONDS = float(os.environ.get("JOB_STUCK_TIMEOUT_SECONDS", "900"))
RECLAIM_INTERVAL_SECONDS = float(os.environ.get("JOB_RECLAIM_INTERVAL", "60"))


async def run_job(job: dict) -> dict:
    job_type = job["job_type"]
    payload = job["payload"] or {}
    user_id = job["user_id"]

    if job_type == "generate_summary":
        from kb.summary import generate_summary_job

        return await generate_summary_job(
            payload["node_id"],
            payload.get("perspective_label"),
            payload.get("perspective_instruction"),
            user_id,
        )
    if job_type == "revise_summary":
        from kb.summary import revise_summary_job

        return await revise_summary_job(
            payload["node_id"],
            payload["instruction"],
            payload.get("perspective_label"),
            payload.get("perspective_instruction"),
            user_id,
        )
    if job_type == "aggregate_index_abstract":
        from maintenance import aggregate_index_abstracts

        return await aggregate_index_abstracts(
            user_id,
            index_id=payload["index_id"],
            only_stale=bool(payload.get("only_stale", False)),
        )
    if job_type == "rebuild_wiki":
        from kb import wiki

        return await wiki.rebuild_wiki(user_id)
    if job_type == "run_maintenance":
        from maintenance import run_maintenance

        return await run_maintenance(user_id)
    if job_type == "rebuild_from_raw":
        from maintenance import rebuild_from_raw

        return await rebuild_from_raw(user_id, **payload)

    raise ValueError(f"unknown job_type: {job_type}")


async def worker_loop() -> None:
    await database.init()
    print("[job-worker] started", flush=True)
    last_reclaim = 0.0
    try:
        while True:
            now = time.monotonic()
            if now - last_reclaim >= RECLAIM_INTERVAL_SECONDS:
                reclaimed = await jobs.reclaim_stuck_jobs(STUCK_TIMEOUT_SECONDS)
                if reclaimed:
                    print(f"[job-worker] reclaimed {reclaimed} stuck job(s)", flush=True)
                last_reclaim = now

            job = await jobs.claim_next_job()
            if not job:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            print(f"[job-worker] running {job['id']} {job['job_type']}", flush=True)
            try:
                result = await run_job(job)
                await jobs.complete_job(job["id"], result)
                print(f"[job-worker] succeeded {job['id']}", flush=True)
            except Exception as exc:
                error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                await jobs.fail_job(job["id"], error)
                print(f"[job-worker] failed {job['id']}: {error}", flush=True)
    finally:
        await database.database.disconnect()


if __name__ == "__main__":
    asyncio.run(worker_loop())
