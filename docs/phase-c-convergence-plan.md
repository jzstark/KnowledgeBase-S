# Phase C：架构收敛实施计划（双轨退役 + wiki 降级 + 数据分层）

> 撰写日期：2026-07-08。上游文档：`docs/roadmap.md` 第 1 节。
> 前序设计：`docs/revision-source-folders.md`（Phase 1–5 已完成至 Phase B / raw_ref 读路径降级）。
> 本计划只做收敛，**不加任何新功能**。每一步的行为变更应为零（除明确标注的裁决项）。

---

## 1. 背景：要还的三笔债

| 债 | 现状 | 结构性后果 |
|---|---|---|
| D1. sources ↔ folders 双轨 | 两套表并行，靠"同 hex 后缀"字符串约定映射（`routers/folders.py:69` `_source_id()`、`routers/sources.py:163` `_mapped_folder_id()` 等） | 无外键、无事务保证 → B6/B7/B8 类孤儿数据 bug 的根源；每个写路径都要记得双写 |
| D2. wiki 文件与 DB 主从未裁决 | 双写入器（API `kb/wiki.py:42 write_wiki_node` vs worker `pipeline.py:483 write_wiki_article`，格式不一致 = B1/A3）；`restore_from_wiki` 与 `rebuild_from_raw` 两条互相矛盾的重建路径并存 | 全文只存在于 wiki 文件中，`rebuild_wiki` 会毁掉全文；没人说得清哪份数据是权威 |
| D3. raw_ref 半退役 | 读路径已迁走（Phase 5），但列仍在，7 个模块仍引用（graph/ingest/internal/public_service/wiki/entity_ops/restore + kb_tools） | "看起来还在用"的僵尸依赖，阻止任何人放心删除 |

---

## 2. 前置裁决（先写 ADR，再动代码）

动手前先落两份 ADR，作为后续所有步骤的依据：

### ADR-0002：事实源裁决 —— DB + raw 层为事实源，wiki 文件为派生物

- **事实源**：Postgres 中的结构化数据（nodes / edges / facts / folders /
  document_instances / raw_assets）+ 磁盘上的 raw 层文件（原始抓取快照
  `raw_snapshot_ref`、**提取后全文** `extracted_text_ref`）。
- **派生物**：`user_data/*/wiki/**/*.md` 全部可从事实源重新渲染，允许删除重建。
- **推论 1**：`restore_from_wiki` 降级为"灾难恢复最后手段"，不再是常规路径；
  `rebuild_from_raw` 是唯一常规重建路径。
- **推论 2**：文章全文的权威存储是 `extracted_text_ref` 指向的文件（事实层），
  wiki md 里的正文只是它的渲染副本。

### ADR-0003：数据三层分类（事实 / 派生 / 运行态）

| 层 | 表/目录 | 备份策略 |
|---|---|---|
| 事实 | sources, source_items, folders, connectors, raw_assets, document_instances, knowledge_nodes + 四张 object 子表, knowledge_edges(mentions/wikilink), index_children, entity_facts；`user_data/*/raw/**`（含提取文本） | 必须备份 |
| 派生 | knowledge_edges(similar_to), entity_candidates, entity_pair_signals, 所有 embedding 列, `user_data/*/wiki/**` | 可重建，不备份 |
| 运行态 | jobs, 登录限流状态 | 不备份 |

> 注：embedding 列物理上在事实表里，分类的意义是"恢复演练允许它为空、由重建任务补齐"。

**验证**：两份 ADR 合入 `docs/adr/`，MEMORY.md 与 MAP.md 同步更新。

---

## 3. 实施轨道与顺序

依赖关系：C1（事务基线）→ C2（全文事实源 + 单一 wiki 写入器）→ C3（ID 外键化）
→ C4（API 面收敛）→ C5（raw_ref 退役）→ C6（分层落地 + 演练）。
C1/C2 与 C3 可并行，其余按序。

---

### C1. 事务基线（对应 review A1，是后续所有步骤的前提）

**问题**：多语句不变量（建 folder + 建 legacy source；上传 = raw_asset +
document_instance + source_item 三连插；硬删除的十几步操作）没有事务包裹，
中途失败即产生孤儿。

**步骤**：

1. 在 `database.py` 提供统一的事务上下文（asyncpg `transaction()` 封装），
   约定：**凡是跨表维持"↔ 映射不变量"的写操作必须在一个事务内**。
2. 逐个改造（每个一个提交）：
   - `POST /api/folders`（folder + legacy source 双写）
   - `POST /api/folders/{id}/upload`（raw_asset + document_instance + source_item）
   - `POST /api/folders/{id}/add-url`
   - `folders.py:_hard_delete_document_instance`（全部 DB 语句进一个事务；
     文件删除放在事务提交**之后**，删文件失败只记日志——DB 一致性优先）
   - connector 创建/删除（connector + stream source 双写）
3. 测试先行：对每个端点写"中途注入失败 → 断言无孤儿行"的测试
   （用 monkeypatch 让第二条 INSERT 抛错）。

**验证**：注入失败测试全绿；`refactor_smoke.py` 通过。
**工作量**：约 1–2 天。风险低，纯加固。

---

### C2. 全文事实源迁移 + 单一 wiki 写入器（裁决 D2，修复 B1/A3 的根因)

这是 Phase C 中唯一改变"数据放哪"的轨道，先做，因为 C4/C5 依赖它。

**目标**：文章全文的权威位置 = `extracted_text_ref` 文件；wiki md 一律由 API
单点渲染；worker 不再直接写 wiki 文件。

**步骤**：

1. **审计现状**：确认每条入库路径都写了 `extracted_text_ref`
   （`source_items.extracted_text_ref` 列已存在）；对存量库跑一次盘点 SQL，
   统计"有 article 节点但 extracted_text_ref 为空/文件缺失"的行数。
2. **回填**：对缺失全文的存量文章，从现有 wiki md 正文提取回填成
   extracted text 文件（一次性脚本，放 `scripts/`；这是唯一一次"从 wiki 反向
   读数据"，之后 wiki 永远只写不读）。
3. **写入器合一**：
   - 把 worker `pipeline.py:483 write_wiki_article` 的渲染逻辑（frontmatter
     字段集、正文格式、关联节点区块）合并进 API `kb/wiki.py:write_wiki_node`，
     以 worker 版格式为准（它是入库主路径的格式）；正文从
     `extracted_text_ref` 读取。
   - worker 侧删除文件写入代码，改为在 `post_ingest` 完成后调用 API 的
     rebuild-wiki 单节点端点（或直接由 `post_ingest` 内联渲染）。
   - `rebuild_wiki` job 从此安全：渲染源是 DB + extracted text，不会再毁全文。
4. **restore_from_wiki 降级**：代码保留但从常规文档/Make 目标中移除入口，
   `maintenance/restore.py` 顶部加注释声明其"灾难恢复最后手段"地位（ADR-0002）。
5. **一致性测试**：新增测试——同一节点先走 ingestion 渲染、再触发
   `rebuild_wiki`，断言两次文件内容逐字节一致（这是 B1 的回归测试）。

**验证**：
- 盘点 SQL 显示 0 篇文章缺全文；
- B1 回归测试通过；
- 删除整个 `user_data/*/wiki/` 目录后跑 `rebuild_wiki`，抽查若干文章正文完整。

**工作量**：2–3 天（回填脚本 + 双格式合并是主要成本）。
**风险**：worker 与 API 渲染格式合并时的字段遗漏 → 用步骤 5 的逐字节测试兜住。

---

### C3. ID 映射外键化（D1 第一步：把约定变成约束）

**目标**：不改变双轨并存的事实，但让映射从"字符串前缀替换"变成"外键列 + 约束"，
使后续退役可以安全进行。

**步骤**：

1. **新 Alembic revision（0004）**：
   - `folders.legacy_source_id → sources.id`（nullable, UNIQUE）
   - `connectors.legacy_source_id → sources.id`（nullable, UNIQUE）
   - `document_instances.source_item_id → source_items.id`（nullable, UNIQUE）
     （反向的 `source_items.document_instance_id` 已存在，补上正向列后二选一，
     建议保留 `source_items.document_instance_id` 为权威、另一侧只加约束不加列，
     以最小化 schema 变更——实施时定夺，原则：**一个方向的 FK + UNIQUE 即可**）
   - 迁移内做存量回填：按现行 hex 后缀约定 UPDATE 一次。
   - 回填后加 CHECK/验证查询：不存在映射悬空（folder 无 source、di 无 si）。
2. **代码替换**：删除 `routers/folders.py` 的 `_source_id/_di_id/_ra_id/_con_id`
   与 `routers/sources.py` 的 `_mapped_folder_id/_mapped_connector_id`，全部改为
   JOIN / 显式列读取。grep 验收：`grep -rn '"src_" +\|"fld_" +\|"di_" +\|"si_" +\|"con_" +\|"ra_" +' services/` 结果为空（ID 生成处除外）。
3. **新建实体不再要求同后缀**：ID 生成回归各自独立随机（同后缀约定从此只是
   历史遗留，不再是不变量）。

**验证**：迁移在开发库 + 生产快照副本上各跑一次通过；映射悬空校验 SQL 返回 0；
现有全部测试绿。
**工作量**：1–2 天。
**风险**：存量数据中已有映射悬空（B6/B7/B8 造成的孤儿）→ 迁移前先跑盘点，
孤儿行单独清理（记录到迁移 revision 的 docstring）。

---

### C4. API 面收敛（D1 第二步：sources 降级为内部实现）

**目标**：`/api/sources/*` 从公开 API 降级为 worker 专用内部接口；
folders/connectors/document-instances 成为唯一的用户/前端面。

**步骤**：

1. **消费方审计**：grep 前端（`services/web/`）与 ingestion-worker 对
   `/api/sources` 的全部调用点，列清单。
2. **前端迁移**：旧 `/sources/[id]/page.tsx`（wechat 配置子页，MEMORY 遗留项）
   的功能迁到新文件管理器 UI 的 connector 设置里；迁移完删除旧页面。
   前端从此不调用任何 `/api/sources/*`。
3. **worker 接口内部化**：worker 仍需要的端点（pending items 拉取、
   source-items 状态回写、`pending/source-ids` 兜底轮询）保留，但：
   - 鉴权从 `require_auth_or_service_token` 收紧为**仅** service token；
   - 移出公开 OpenAPI（`include_in_schema=False`），路径迁到
     `/api/internal/ingestion/*`（一次性改名，worker 同步更新）。
4. **用户面删除**：`/api/sources` 的 CRUD 中不再被前端使用的端点直接删除
   （folder/connector 的 CRUD 已覆盖其功能）。
5. **契约测试**：`test_kb_public_contract` 增加断言——公开 OpenAPI 中不出现
   `/api/sources`。

**验证**：前端 grep 无 `/api/sources` 引用；worker 全链路（RSS 拉取 → 入库 →
folder 内容出现新 document_instance）在 dev compose 跑通；契约测试绿。
**工作量**：2–3 天（前端 wechat 配置迁移是主要成本）。
**风险**：worker 与 api 部署不同步导致路径 404 → 改名步骤单独成一个发布，
旧路径保留 302/别名一个版本周期后删除。

---

### C5. raw_ref 退役（D3）

**前提**：C2 完成（全文与 URL 均有事实层来源）。

**步骤**：

1. **逐模块迁移读方**（当前引用：`kb/graph.py`、`kb/ingest.py`、
   `kb/internal.py`、`kb/public_service.py`、`kb/wiki.py`、
   `maintenance/entity_ops.py`、`maintenance/restore.py`、`kb_tools.py`）：
   - URL 用途（如 `public_service._reference_url`）→ 改读
     `document_instances.origin_ref` / `raw_assets.storage_key`；
   - 路径用途 → 改读 `raw_assets.storage_key`；
   - 每迁一个模块一个提交，跑该模块测试。
2. **写方停写**：`kb/ingest.py` 停止写入 raw_ref（保留列，写 NULL）。
3. **观察一个版本周期**后，新 Alembic revision 删除 `article_nodes.raw_ref` 列。
4. grep 验收：`grep -rn raw_ref services/ --include='*.py'` 仅剩 alembic 历史
   迁移文件。

**验证**：MCP `fetch`/`get_sources` 返回的 reference URL 与迁移前一致
（抽样对比脚本）；全部测试绿。
**工作量**：1–2 天。

---

### C6. 分层落地：备份对齐 + rebuild_derived 演练

**前提**：ADR-0003 已定，C2 完成。

**步骤**：

1. **备份脚本对齐**：`scripts/backup.sh` 明确只保证事实层（pg_dump 全库仍可，
   但文档注明恢复时派生层允许为空）；`user_data/*/raw/**` 纳入备份对象，
   `user_data/*/wiki/**` 明确排除。
2. **新增 `rebuild_derived` 维护任务**（复用现有 job 框架）：
   清空并重建 similar_to 边、entity_pair_signals、entity_candidates 计数、
   wiki 目录；embedding 缺失的节点重算（分批、可断点续跑）。
3. **恢复演练（一次性，但写成可重复的脚本）**：
   - 从"仅事实层备份"在全新环境恢复；
   - 跑 `rebuild_derived`；
   - 对比演练库与生产库：节点数、边数（按 relation_type 分组）、
     search 对 10 个固定查询的 top-5 结果重合度 ≥ 80%。
4. 演练结果记录进 `docs/`（一页纸即可），此后每次大版本前重跑。

**验证**：演练脚本端到端通过；对比指标达标。
**工作量**：2 天（演练环境搭建占大头）。

---

## 4. 总排期与里程碑

| 里程碑 | 内容 | 预估 |
|---|---|---|
| M1 | ADR-0002/0003 + C1 事务基线 | 2 天 |
| M2 | C2 全文事实源 + 单一 wiki 写入器（含 B1 回归测试） | 3 天 |
| M3 | C3 ID 外键化（Alembic 0004） | 2 天 |
| M4 | C4 API 面收敛（含前端 wechat 迁移） | 3 天 |
| M5 | C5 raw_ref 退役 | 2 天 |
| M6 | C6 备份对齐 + 恢复演练 | 2 天 |

共约 14 个工作日。每个里程碑独立可发布、可回滚（Alembic revision 均写
downgrade；API 改名保留一个周期的别名）。

## 5. 完成定义（Definition of Done）

1. `grep` 三项验收全部为空：字符串前缀映射、`/api/sources` 前端引用、
   `raw_ref`（alembic 历史除外）。
2. 删除 `user_data/*/wiki/` 后 `rebuild_wiki` 可完整重建，B1 回归测试常驻 CI。
3. "仅事实层备份 → 恢复 → rebuild_derived"演练通过并有记录。
4. MEMORY.md / MAP.md 更新：双轨描述改为单轨 + 内部队列；
   `docs/revision-progress.md` 追加 Phase C 完结记录。
