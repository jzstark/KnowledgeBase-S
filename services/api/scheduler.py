"""
定时调度器 — 后续实现。
将触发 ingestion-worker 和 summarizer-worker。
"""
import time

if __name__ == "__main__":
    print("Scheduler started (stub). Will trigger workers on schedule.")
    while True:
        time.sleep(60)
