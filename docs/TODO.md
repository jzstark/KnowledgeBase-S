# TODO

## Chat Tool Budget

- Issue: Chat can return `（工具调用次数已达上限，请缩小问题范围后重试。）` when Claude keeps requesting read-only knowledge tools instead of producing a final answer.
- Likely causes: broad user questions, scattered search results, or tool-call loops across `kb_search`, `kb_get_node`, `kb_get_neighbors`, and `kb_get_sources`.
- Current mitigation: `chat.max_tool_rounds` is configured in `config/system.yaml` and the chat system prompt now tells the model to stay within a fixed tool budget.
- Future improvement: add lightweight observability for per-message tool rounds and tool names, then tune the budget/prompt based on real usage.

## Chat Citation Rendering

- Issue: Some assistant messages still render raw citation text such as `[art_5539136a191a] "Rebels in our own time"` instead of turning it into a clickable knowledge-node link.
- Known attempted mitigation: the frontend chat Markdown renderer recognizes standard Markdown links, bare `[art_*]` / `[ent_*]` / `[sum_*]` / `[idx_*]` node ids, source lines with `来源:` / `Sources:`, and Markdown tables.
- Gap: real model output can still contain citation formats that bypass the current line-level parser, especially mixed prose plus quoted node titles.
- Future improvement: replace the ad hoc renderer with a proper Markdown pipeline plus a citation transform plugin, or normalize citations server-side before streaming/saving assistant messages.



## 待修复：dev / deploy 数据卷不一致

### 现象

在 VPS 上先用 `make dev` 运行系统，知识库正常；之后切换到 `make deploy` 后，知识库图谱和列表变空。检查当前 Postgres：

```bash
docker compose exec postgres psql -U postgres -d app -c "select count(*) from knowledge_nodes;"
```

返回 `0`。

### 原因

`make dev` 使用 `docker-compose.dev.yml` 覆盖了 Postgres 数据目录，实际数据在 named volume：

```yaml
postgres_dev:/var/lib/postgresql/data
```

`make deploy` 只使用基础 `docker-compose.yml`，Postgres 数据目录变成：

```yaml
./data/postgres:/var/lib/postgresql/data
```

所以切换运行方式后，API 连接到另一套空数据库，前端显示为空。

### 临时恢复

切回 dev compose：

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers up -d --remove-orphans
```

然后重新检查：

```bash
docker compose exec postgres psql -U postgres -d app -c "select count(*) from knowledge_nodes;"
```

### 后续改法

需要统一 dev / deploy 的 Postgres 存储策略，避免同一个环境因为启动命令不同而连接到不同数据库。可选方向：

- dev 和 deploy 使用同一个明确命名的 Postgres volume。
- 或将 `postgres_dev` 数据迁移到 `./data/postgres`，并删除 dev overlay 对 Postgres volume 的覆盖。
- 在 `make deploy` 或启动脚本中加入数据卷差异检查，发现 `knowledgebase-s_postgres_dev` 有数据但 `./data/postgres` 为空时给出明确提示。