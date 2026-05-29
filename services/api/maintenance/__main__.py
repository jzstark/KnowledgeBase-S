import asyncio
import json
import os
import sys

# Ensure services/api/ is on the path when invoked as: python -m maintenance
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from maintenance import rebuild_from_raw, restore_from_wiki, run_maintenance


async def main():
    await database.init()
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "restore_from_wiki":
        result = await restore_from_wiki()
    elif cmd == "rebuild_from_raw":
        if "--confirm" not in sys.argv:
            print(
                "此操作将按 source_items manifest 清空可重建派生节点"
                " 并通过 ingestion-worker 重新入库。\n"
                "确认执行请加 --confirm 参数：\n"
                "  python -m maintenance rebuild_from_raw --confirm\n"
                "或通过 docker compose exec：\n"
                "  docker compose exec api python -m maintenance rebuild_from_raw --confirm"
            )
            return
        result = await rebuild_from_raw()
    else:
        result = await run_maintenance()
    print(json.dumps(result, ensure_ascii=False, indent=2))


asyncio.run(main())
