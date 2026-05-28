# Revision Audit 2026-05-28

本报告按 `docs/revision-audit-plan-2026-05-28.md` 执行。规范来源只采用 `docs/revision-progress.md` 的第一、二部分；第三部分进度记录和“差异说明”只作为待验证声明。

## Executive Summary

当前 revision 的主干方向基本落地：`knowledge_nodes` 已裁掉对象专属旧列，KB Public `/api/kb/v1/` 7 个 MCP 工具端点存在且只读，`kb-chat` adapter 已改为调用 `/api/kb/v1/`。本地 Docker DB 的关键 schema 不变量也基本通过。

但仍不能认为 Phase A-E 已完全实现。主要差距集中在：

- `knowledge_edges` 仍保留 `part_of` 边，违反“edges 只保留 mentions / similar_to，index_children 才是结构事实源”的设计。
- 新建 index、生成 summary、维护任务重算 embedding 时没有写 `embedding_model`，部分路径也没有写 `doc_kind`。
- Source / source item 的 UI 与 API 未达到 §8：停用 source 不显示为 `[已停用]` 分组；source item 不能手动覆盖 doc_kind；现有 source 配置页也不能编辑 `default_doc_kind`。
- 旧 chat 表仍在 DB 中；旧 `kb_tools.py` 仍有 entity canonical name 展示问题。
- worker 侧还有 `content[0].text` 直接访问和若干硬编码模型/token，和边界健壮性、配置外置原则不一致。

## Verification Performed

### Static / Contract

- `python -m py_compile` 通过：
  - `services/api/database.py`
  - `services/api/kb/public.py`
  - `services/api/kb/internal.py`
  - `services/api/kb_tools.py`
  - `services/api/routers/sources.py`
  - `services/api/maintenance.py`
  - `services/api/entity_insights.py`
- KB Public route AST 检查确认 8 个端点存在，对应 7 个工具。
- KB Public 源码检查确认无 `INSERT / UPDATE / DELETE / CREATE / DROP / ALTER` SQL 字符串。
- `~/Code/kb-chat/services/kb-mcp` contract tests 通过：`Ran 2 tests OK`。
- `services/web` 执行 `npx tsc --noEmit` 通过。
- `npm --prefix services/web run typecheck` 不存在脚本；项目当前只有 `dev / build / lint`。
- `python -m unittest discover -s services/api/tests` 失败 1 个测试：`test_kb_tools_time_filters` import `prompt_loader` 时找不到 `/app/shared_config/prompts.md`。这是本地测试 harness 问题，不是运行时 Docker 已验证问题，但说明测试不可离线运行。

### Docker DB Invariants

本地 Docker 栈正在运行，查询当前 DB 得到：

- `knowledge_nodes` 当前列：`id, user_id, title, abstract, embedding, source_id, tags, object_type, created_at, updated_at, ingested_at, embedding_model, doc_kind, published_at`。
- 已删除旧列检查：`summary_of / canonical_name / aliases / perspective_* / body_embedding / is_default / source_node_ids / source_type / raw_ref / source_item_id / source_published_at / source_updated_at / captured_at / effective_at / is_primary / priority_score / last_accessed_at / access_count` 均不存在于 `knowledge_nodes`。
- `uq_edges_from_to_type` UNIQUE 约束存在。
- duplicate edges 为 0。
- object table orphan 检查为 0。
- `article_nodes.source_item_id` orphan 为 0。
- 节点数量：292 nodes / 92 articles / 94 summaries / 104 entities / 2 indices。

异常或需解释：

- `knowledge_edges` 当前仍有 `part_of = 78`，同时 `index_children = 66`。
- DB 仍有 `chat_sessions` 和 `chat_messages` 表。
- 当前 292 个历史节点的 `embedding_model` 与 `doc_kind` 全部为 NULL。历史 backfill 未做可接受，但新写入路径必须补齐。

## Findings

### P1-1. `knowledge_edges` 仍保留 `part_of` 结构边

Evidence：

- DB 当前 `knowledge_edges` relation types：`mentions=826, part_of=78, similar_to=459`。
- `index_children` 同时已有 66 行。
- 设计决议 §2/§3 明确：`edges` 只保留 `mentions | similar_to`；index 结构事实源为 `index_children`。
- `services/api/kb/internal.py:1451` 的 graph/all 查询显式排除了 `part_of`，说明 UI 已不把它当事实源。

Impact：

- Schema 与设计不一致；维护任务、图谱统计或未来查询可能重复理解 index 结构。
- `part_of` 已经被迁移到 `index_children` 后，继续留在 edges 里会制造两个事实源。

Fix direction：

- 在 migration 中确认所有 `part_of` 已迁移到 `index_children` 后删除 `knowledge_edges.relation_type='part_of'`。
- 防止后续写入 `part_of` edge；index 结构写入只能走 `index_children`。
- 增加 DB invariant test：`SELECT COUNT(*) FROM knowledge_edges WHERE relation_type NOT IN ('mentions','similar_to') = 0`。

### P1-2. `embedding_model` / `doc_kind` 不是所有新节点写入路径都会填

Evidence：

- `services/api/kb/internal.py:219` ingest 路径写入 `doc_kind, embedding_model`，这是正确的。
- `services/api/kb/internal.py:668` create index 路径只插入 `id, user_id, title, abstract, embedding, source_id, tags, object_type`。
- `services/api/kb/internal.py:876` create summary 路径也没有插入 `doc_kind, embedding_model`。
- `services/api/maintenance.py:548` index abstract 聚合更新 `abstract, embedding`，但不更新 `embedding_model`。

Impact：

- Phase D “Ingestion 时自动更新 `embedding_model`”只覆盖 article ingestion，不覆盖系统内部生成的 index/summary embedding。
- drift 检测会把这些新节点当成 drift 或 NULL；`doc_kind` 过滤也会漏掉 summary/index。

Fix direction：

- create index：写入 `embedding_model=config embedding.model`，`doc_kind` 默认 `other` 或请求体显式值。
- create summary：继承被 summary 的节点 `doc_kind`，写入当前 `embedding_model`。
- revise summary / rollup index / maintenance aggregate：更新 embedding 时同步更新 `embedding_model`。
- 增加测试：所有写入或重算 embedding 的路径必须同时写 `embedding_model`。

### P1-3. Source item 级 doc_kind 覆盖没有实现为用户能力

Evidence：

- 设计 §8：`Source items 列表：每条 item 允许手动覆盖 doc_kind（下拉，不允许自由输入）`。
- API 有 `GET /api/sources/{source_id}/source-items` 和 `POST /api/sources/source-items/{item_id}/status`，但 status update body 只处理 status/raw refs/title，不处理 doc_kind：`services/api/routers/sources.py:394`。
- `services/web/app/sources/[id]/page.tsx` 没有 source item 列表，也没有 doc_kind 下拉。

Impact：

- 用户不能修正 RSS / WeChat / 批量来源中单条 item 的类型。
- 设计中的 doc_kind 继承链少了手动覆盖入口。

Fix direction：

- 增加 `PATCH /api/sources/source-items/{item_id}`，支持 `doc_kind` 且使用 config 枚举校验。
- Source detail 页展示 source items 列表，提供 doc_kind 下拉。
- 如果 item 已入库为 node，明确是否同步更新对应 `knowledge_nodes.doc_kind`；建议同步，避免 source item 与 node 不一致。

### P1-4. 停用 source 的 `[已停用]` 分组未实现

Evidence：

- `GET /api/sources` 过滤 `deleted_at IS NULL`：`services/api/routers/sources.py:479`。
- `GET /api/sources/{id}` 也过滤 `deleted_at IS NULL`：`services/api/routers/sources.py:456`。
- Knowledge list 分组只按 `source_name` 分组，不返回 `deleted_at`，也不会标注 `[已停用]`：`services/web/app/knowledge/page.tsx:344`。

Impact：

- Source 被软删除后，source 管理界面完全看不到。
- 知识库资源管理器不会按设计标注来源已停用，用户无法区分 active source 和 archived source 的文章。

Fix direction：

- `list_nodes` / source 分组查询返回 `source_deleted_at`。
- Knowledge UI 分组标题追加 `[已停用]`。
- Source 管理页增加 `include_deleted` 或独立停用列表。

### P2-1. Source 配置页不能编辑 `default_doc_kind`

Evidence：

- `services/web/app/sources/page.tsx` 的创建表单支持 `default_doc_kind`。
- `services/web/app/sources/[id]/page.tsx` 的 `Source` interface 没有 `default_doc_kind` 字段，页面只提供 WeChat feed_id 配置，不提供默认类型下拉。
- 后端 `PUT /api/sources/{id}` 已支持 `default_doc_kind`：`services/api/routers/sources.py:689`。

Impact：

- 新建时可设默认类型，但后续无法在配置页调整。
- 设计 §8 “Source 配置页加入 default_doc_kind 字段”未完成。

Fix direction：

- 在 source detail 页加入 `default_doc_kind` 字段和 `DocKindSelect`。
- 保存时调用现有 `PUT /api/sources/{id}`。

### P2-2. 资源管理器分组排序使用 `created_at`，不是 `published_at`

Evidence：

- 设计 §8：Articles 按 source 分组，组内按 `published_at` 排序。
- `list_nodes` SQL 使用 `ORDER BY n.created_at DESC`：`services/api/kb/internal.py:1392`。
- UI 分组只是对返回结果 reduce，不做组内 published_at 排序。

Impact：

- “最新文章”视图实际按节点创建时间，而不是知识时间 / 发布时间。
- 对历史补录或 delayed ingestion 的来源会误排序。

Fix direction：

- `list_nodes` 返回 `published_at`，排序默认 `COALESCE(n.published_at, n.ingested_at, n.created_at) DESC`。
- 分组模式下保持后端排序，或在 UI 组内按 `published_at` 排序。

### P2-3. `kb_tools.py` 仍未按 canonical_name 展示 entity

Evidence：

- 最后差异说明已经记录该问题。
- `services/api/kb_tools.py:259` 和 `:298` 都直接 SELECT `n.title`，没有 `LEFT JOIN entity_nodes en`。
- Public search 也同样只选 `n.title`：`services/api/kb/public.py:220`，虽然多数写入路径会同步 title，但不如 object table 权威。

Impact：

- 旧 chat 工具或兼容路径中，entity 搜索结果可能显示空 title、旧 title 或非 canonical 名称。

Fix direction：

- `kb_tools.py` 两条搜索 SQL 都 join `entity_nodes`，返回 `COALESCE(en.canonical_name, n.title) AS title`。
- KB Public search 也建议同样调整，减少对 title 同步的隐式依赖。

### P2-4. Worker 侧仍有 `content[0].text` 直接访问

Evidence：

- `services/ingestion-worker/pipeline.py:281`、`:330`
- `services/ingestion-worker/sources/image.py:79`、`:92`
- `services/ingestion-worker/sources/pdf.py:27`
- `services/feedback-worker/main.py:66`

Impact：

- 之前 API 侧已修过 ToolUseBlock / TextBlock union 风险，但 worker 边界仍有同类崩溃点。
- 特别是 ingestion-worker 属于外部输入边界，失败会阻断入库。

Fix direction：

- 统一改为 `getattr(message.content[0], "text", "")` 并处理空字符串。
- 给 PDF/image/article analysis 各加一条静态或单元测试，防止回归。

### P2-5. Prompt loader 没有启动期 required prompt 校验

Evidence：

- 设计 Phase A 要求 `config_loader.py / prompt_loader.py` 覆盖所有新增 key，缺失 key 启动时报错。
- `services/api/config_loader.py` 有 `REQUIRED_KEYS` 和 `validate_required_keys()`。
- `services/api/prompt_loader.py:19` 只加载 prompts，不校验所需 section 是否齐全。
- 本地 API tests 因 `/app/shared_config/prompts.md` 不存在导致 import 失败，说明 prompt loading 对测试环境也很脆弱。

Impact：

- 缺少 prompt section 只有在具体功能调用时才炸，不符合启动 fail-fast。
- 本地 unit tests 无法完整运行。

Fix direction：

- 为 prompt_loader 增加 `REQUIRED_PROMPTS` 和 `validate_required_prompts()`。
- main lifespan 启动时调用。
- 支持通过环境变量覆盖 prompt 文件路径，测试可指向 repo `config/prompts.md`。

### P2-6. 文件/OCR worker 仍有硬编码模型和 token

Evidence：

- `services/ingestion-worker/sources/image.py:69` / `:70` hard-code `claude-sonnet-4-6`, `4096`。
- `services/ingestion-worker/sources/image.py:85` / `:86` 同上。
- `services/ingestion-worker/sources/pdf.py:20` / `:21` hard-code `claude-haiku-4-5-20251001`, `4096`。
- `services/feedback-worker/main.py:62` / `:63` hard-code model/token。

Impact：

- 违反“所有模型名称、token 上限放入 config/”原则。
- 将来模型切换时容易遗漏 worker 路径。

Fix direction：

- 在 `config/system.yaml` 增加 `models.image_ocr`, `models.image_cleanup`, `models.pdf_cleanup`, `models.feedback_analysis` 和对应 `llm_output_tokens.*`。
- worker config_loader required keys 同步补齐。

### P2-7. `knowledge_edges` schema 未实现设计中的 `metadata JSONB`

Evidence：

- 设计 §2 关系层写明 `metadata JSONB`。
- 当前 DB `knowledge_edges` 列为 `id, from_node_id, to_node_id, relation_type, weight, created_by, description`。
- `services/api/database.py:256` 仍添加 `description TEXT`，没有 `metadata JSONB`。

Impact：

- mentions 的 `confidence / evidence_span` 等扩展数据没有设计承载位，只能散落到 `entity_facts` 或丢失。
- schema 与文档不一致。

Fix direction：

- 判断是否仍需要 edge metadata。如果需要，加 `metadata JSONB DEFAULT '{}'` 并迁移/废弃 `description`。
- 如果不需要，反向修改设计决议，明确 edge 扩展事实全部进入 `entity_facts`。

### P3-1. `knowledge_nodes` CREATE 与目标模型仍不完全一致

Evidence：

- `knowledge_nodes` CREATE 语句不直接包含 `embedding_model` / `doc_kind`，而是靠 `ALTER TABLE` 添加：`services/api/database.py:18` 与 `:251`。
- `sources` CREATE 也不直接包含 `default_doc_kind` / `deleted_at`，靠 `ALTER TABLE` 添加：`services/api/database.py:69` 与 `:253`。

Impact：

- 运行结果是正确的，但 schema definition 可读性差，继续增加迁移时容易误判“目标 schema 到底是什么”。

Fix direction：

- 把目标字段并入 CREATE TABLE。
- 保留 ALTER 作为 migration 兼容旧库，但注释为 migration-only。

### P3-2. 旧 chat DB 表仍存在

Evidence：

- DB 当前仍有 `chat_sessions` 和 `chat_messages`。
- 当前代码已无 `/api/chat` 路由命中。

Impact：

- 不影响当前运行，但属于明确 legacy schema。

Fix direction：

- 如确认无需保存旧 chat，增加一次性 migration：`DROP TABLE IF EXISTS chat_messages; DROP TABLE IF EXISTS chat_sessions;`。
- 若担心误删，先写入独立 cleanup 脚本或文档化手动 SQL。

## Phase Status

| Phase | Audit status | Notes |
|---|---|---|
| Phase A | Partially implemented | 主 schema 清理通过；edge metadata、part_of 边、prompt required 校验、部分 hard-coded worker 参数未完成。 |
| Phase B | Mostly implemented | API 分层与 app/kb 目录成立；app 层仍通过 `kb.public_service` 直接读 DB，属于可接受架构债但不是“通过 KB API 调用”。 |
| Phase C | Mostly implemented | KB Public 7 工具端点存在且只读；kb-chat adapter 已切 `/api/kb/v1/`。仍建议补 entity canonical title。 |
| Phase D | Partially implemented | cite / compare / summarize 主路径已实现；embedding_model 写入覆盖不全。 |
| Phase E | Partially implemented | 图谱默认、搜索面板、doc_kind 创建/上传路径已做；source item 覆盖、停用 source 分组、published_at 排序、source 配置页 default_doc_kind 编辑未完成。 |

## Recommended Fix Order

1. P1-1：清理 `part_of` edges，并加 invariant test。
2. P1-2：补全所有 embedding 写入路径的 `embedding_model`，并为 summary/index doc_kind 定义继承策略。
3. P1-3 / P1-4：完成 source item doc_kind 覆盖、停用 source 分组。
4. P2-3：修正 `kb_tools.py` 和 KB Public search 的 entity canonical title。
5. P2-4 / P2-6：worker `.text` 守卫与模型/token 配置外置。
6. P2-5：prompt_loader fail-fast 与测试路径修复。
7. P2-7：决定并实现 `knowledge_edges.metadata`，或修订设计。
8. P3：schema CREATE 可读性和旧 chat 表清理。

## Fix Pass 2026-05-28

已按 recommended fix order 完成本轮修复：

- `knowledge_edges` schema 加 `metadata JSONB DEFAULT '{}'`，删除 legacy `description`，启动迁移会在 `index_children` 回填后删除 `part_of` 边。
- 启动迁移删除旧 `chat_messages / chat_sessions` 表。
- create index / create summary / revise summary / index rollup / restore from wiki 的 embedding 写入路径补齐 `embedding_model`；summary `doc_kind` 继承被 summary 节点。
- 新增 `PATCH /api/sources/source-items/{item_id}`，允许用 config 枚举覆盖 source item `doc_kind`，并同步已入库 article node。
- Knowledge 资源列表返回 `published_at / source_deleted_at`，按知识时间排序，source 分组标题显示 `[已停用]`。
- Source 详情页支持编辑 `default_doc_kind`，并展示 source items 的 `doc_kind` 下拉。
- KB Public search 与 legacy `kb_tools.py` 搜索均使用 `entity_nodes.canonical_name` 作为 entity 展示标题。
- ingestion / image / pdf / feedback worker 全部移除 `content[0].text` 直接访问；PDF/OCR/feedback 模型与 token 参数已迁入 `config/system.yaml`。
- API / ingestion-worker / feedback-worker 的 prompt loader 支持本地 `config/prompts.md` fallback，并提供启动期 required prompt 校验。

验证：

- `python -m py_compile` 覆盖 API、ingestion-worker、feedback-worker 相关文件，通过。
- `PYTHONPATH=services/api python -m unittest discover -s services/api/tests`：42 tests OK。
- `npx tsc --noEmit`（`services/web`）：通过。
