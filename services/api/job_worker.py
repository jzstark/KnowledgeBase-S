import asyncio
import os
import traceback

import database
import jobs


POLL_INTERVAL_SECONDS = float(os.environ.get("JOB_WORKER_POLL_INTERVAL", "2"))


async def run_job(job: dict) -> dict:
    job_type = job["job_type"]
    payload = job["payload"] or {}
    user_id = job["user_id"]

    if job_type == "generate_summary":
        from routers import kb

        return await kb.generate_summary_job(
            payload["node_id"],
            payload.get("perspective_label"),
            payload.get("perspective_instruction"),
            user_id,
        )
    if job_type == "revise_summary":
        from routers import kb

        return await kb.revise_summary_job(
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
        from routers import kb

        return await kb._do_rebuild_wiki(user_id)
    if job_type == "run_maintenance":
        from maintenance import run_maintenance

        return await run_maintenance(user_id)
    if job_type == "rebuild_from_raw":
        from maintenance import rebuild_from_raw

        return await rebuild_from_raw(user_id)

    raise ValueError(f"unknown job_type: {job_type}")


async def worker_loop() -> None:
    await database.init()
    print("[job-worker] started", flush=True)
    try:
        while True:
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
