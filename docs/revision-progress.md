# Revision Progress

本文档记录 KnowledgeBase-S 系统重构的设计决议与实施进度。

设计决议部分是本次重构的事实依据，不轻易修改。进度部分随实施持续更新。

---

## 指导原则

**规模与演化**：系统设计要能承载持续增长的内容，数据结构和算法要在大量节点下保持合理性能，图谱和检索要能在内容演化中保持有效。

**边界健壮性**：与现实世界接口处（用户操作、外部数据接入）要假设输入是脏的、不可知的。校验和容错在边界做，内部模块之间可以信任。

---

## 第一部分：设计决议

### 1. 系统定位调整

KnowledgeBase-S 专注于知识库核心层。**应用层**（每日简报、草稿生成、写作偏好）与 KB 核心解耦，后续可能整体删除。

KB 核心层包括：节点、关系、来源、搜索、MCP 工具。

应用层包括：`briefing`、`drafts`、`writing_memory`、`topics`、`briefings`（已废弃）。

---

### 2. 核心数据模型

#### 节点层

```
nodes（统一注册表）
  id VARCHAR PK              -- 类型前缀：art_ / ent_ / sum_ / idx_
  type                       -- article | entity | summary | index
  title TEXT
  abstract TEXT              -- 所有类型的 embedding 来源
  embedding vector(1536)     -- 来自 abstract
  embedding_model VARCHAR    -- 记录生成 embedding 所用模型版本（应对未来模型升级）
  tags TEXT[]
  source_id FK → sources
  published_at TIMESTAMPTZ   -- 知识时间（effective_at ?? source_published_at ?? captured_at）
  created_at / updated_at

articles（article 专属）
  node_id FK → nodes PK
  source_item_id FK → source_items
  body_ref TEXT              -- 正文存储引用（wiki 文件路径）
  source_type VARCHAR
  status                     -- active | archived

entities（entity 专属）
  node_id FK → nodes PK
  canonical_name TEXT
  aliases TEXT[]
  entity_type VARCHAR
  merged_into FK → nodes     -- 被合并时指向目标 entity

summaries（summary 专属）
  node_id FK → nodes PK
  summary_of FK → nodes      -- 被观察的 article 或 index（FK 即是关系事实源，不再需要 edges.summarizes）
  body TEXT
  body_embedding vector(1536)
  perspective_label TEXT
  perspective_embedding vector(1536)
  is_default BOOLEAN

indices（index 专属）
  node_id FK → nodes PK
  description TEXT           -- 用户可编辑
  rollup_instruction TEXT    -- 用户可编辑，指导 LLM 聚合 abstract
  abstract_stale BOOLEAN     -- children 变化后置 true
```

#### 关系层

```
edges（节点间关系事实源）
  from_id FK → nodes
  to_id FK → nodes
  type              -- mentions | similar_to
  weight FLOAT
  metadata JSONB    -- 按 type 存扩展数据，如 confidence / evidence_span
  UNIQUE(from_id, to_id, type)

  说明：
  - mentions：article → entity，ingestion 时抽取，是事实边
  - similar_to：派生/缓存边，可完全重建，不是事实源
  - summarizes 不在此表，由 summaries.summary_of FK 表达

index_children（index 结构事实源）
  index_id FK → nodes
  child_id FK → nodes
  position INT
  UNIQUE(index_id, child_id)
```

#### 来源层

```
sources
  id, name, type, config JSONB, last_fetched_at
  deleted_at TIMESTAMPTZ     -- 软删除，articles 仍保留并分组显示

source_items
  id, source_id FK, origin_ref, extracted_text_ref
  published_at, captured_at
  status                     -- pending | processing | succeeded | failed
  error TEXT
```

#### 派生/enrichment 层（非核心，可重建）

```
entity_candidates（ingestion pipeline 内部状态）
  id, canonical_name UNIQUE, aliases[], embedding
  mention_count INT          -- 不再存 JSONB mentions 数组，只存计数器
  max_salience FLOAT
  promoted_entity_id FK → nodes
  first_seen_at, updated_at

entity_facts（来源可溯的事实，timeline 工具数据来源）
  entity_id, article_id, source_item_id
  fact_text, fact_time, evidence_span, confidence

entity_pair_signals（relatedness 缓存，可重建）
  entity_a_id, entity_b_id
  co_occurrence_count, relatedness_score, explanation, ...

jobs（后台任务队列）
  id, job_type, status, payload, result, error, attempts, ...
```

#### 应用层（与 KB 解耦，后续可删）

`drafts`、`topics`、`briefings`、`writing_memory`、`user_settings`

---

### 3. 删除与简化的字段/表

| 内容 | 处理方式 |
|---|---|
| `knowledge_nodes` 里的对象专属字段 | 迁移完成后删除（summary_of / canonical_name / aliases / perspective_* / body_embedding / is_default / source_node_ids / source_type / raw_ref 等） |
| `entity_profiles` 表 | 删除。Entity 描述文字即是 `nodes.abstract`，不需要单独 profile 表 |
| `entity_candidates.mentions`（JSONB 数组） | 替换为 `mention_count + max_salience` 计数器 |
| `knowledge_edges` 里的 `summarizes` 边 | 删除，由 `summaries.summary_of` FK 替代 |
| `knowledge_edges` 里的 LLM 语义边（extends / background_of / supports / contradicts） | 已在 Phase 5 清理，确认不再写入 |
| `is_primary` 字段 | 从 `nodes` 表删除，仅保留在 `sources` 表（供应用层 briefing 使用） |
| `priority_score / last_accessed_at / access_count` | 从 `nodes` 删除（应用层概念） |

---

### 4. MCP 工具（只读，6 个）

| 工具 | 对应 API | 说明 |
|---|---|---|
| `search` | `GET /api/kb/search` | hybrid vector+keyword，按 type/tags/source/date 过滤 |
| `fetch` | `GET /api/kb/nodes/{id}` | 节点详情 + body（从文件系统），支持批量 |
| `related` | `GET /api/kb/nodes/{id}/related` | 按 relation 类型导航：mentions / mentioned_by / summarizes / summarized_by / contains / part_of / similar_to |
| `timeline` | `GET /api/kb/timeline` | 以 entity_id 或 topic 为锚点，按 published_at 排序 |
| `retrieve_context` | `POST /api/kb/retrieve` | summary-first 分层检索，返回结构化知识包 |
| `entity_context` | `GET /api/kb/entities/{id}/context` | entity profile + facts + related entities |

---

### 5. 搜索与检索算法

#### `search` 工具（轻量，无 LLM）

```
query → embed → nodes.embedding 向量搜索
       → title + abstract 关键词搜索（pg tsvector）
       → hybrid 合并排序
       → filters 过滤（type / tags / source_id / date_range）
       → 返回轻量列表（id / title / snippet / score / why_matched）
```

#### `retrieve_context` 工具（summary-first 分层检索）

```
Step 1: embed(query)，可选 HyDE（LLM 生成假想答案再 embed）
Step 2: 主路径并行搜索
  a. summary_nodes.body_embedding → top-K summaries
  b. nodes.embedding (type=entity) → top-K entities
Step 3: 展开高分节点
  a. 高分 summary → 取 summary_of 的 abstract
     若 summary_of 是 index → 取 child summaries
  b. 高分 entity → 取关联文章 abstract（via mentions edges）
Step 4: fallback
  若 summary 命中 < 阈值 → 补充直接 article 向量搜索
Step 5: 去重、排序、按 budget.max_chars 截断
```

`is_primary` 不参与检索排序（它是应用层概念）。

#### Entity 发现算法

```
ingestion 时：
1. embed 文章正文
2. 取最近 20 个 entity 节点 + mention_count 最多的 20 条 candidates → 注入 article_analysis prompt
3. Claude 返回 entities[]（含 salience / matches_existing_entity_id）
4. 有匹配 → 创建 mentions edge，结束
5. 无匹配 → upsert entity_candidates（mention_count +1，更新 max_salience）
6. 检查晋升：mention_count >= 3 OR max_salience >= 0.9 OR (mention_count >= 2 AND max_salience >= 0.7)
7. 晋升 → 创建 entity nodes，Claude 生成 abstract，回扫历史文章创建 mentions edges
```

#### Tag 收敛

Ingestion 时，将当前库中出现频率最高的 top-50 tags 传入 `article_analysis` prompt，引导 Claude 优先复用已有 tags。无需新表。

---

### 6. 用户操作 API（KB 写入）

```
# Summary
POST   /api/kb/nodes/{id}/summaries          创建（含 perspective）
POST   /api/kb/summaries/{id}/revise         按 instruction 修改
DELETE /api/kb/summaries/{id}

# Entity
PATCH  /api/kb/entities/{id}                 修改 canonical_name / aliases / entity_type
POST   /api/kb/entities/{id}/regenerate      重新生成 abstract
POST   /api/kb/entities/merge                合并两个 entity
DELETE /api/kb/entities/{id}                 硬删除（级联删 edges / entity_facts）

# Index
POST   /api/kb/indices                       创建
PATCH  /api/kb/indices/{id}
POST   /api/kb/indices/{id}/children         添加子节点
DELETE /api/kb/indices/{id}/children/{child_id}
PATCH  /api/kb/indices/{id}/children/order
POST   /api/kb/indices/{id}/rollup           触发 abstract 重生成

# 通用节点
PATCH  /api/kb/nodes/{id}/metadata           修改 title / tags / published_at
POST   /api/kb/nodes/{id}/archive            软删除（status = archived）
DELETE /api/kb/nodes/{id}                    硬删除

# 维护
POST   /api/kb/maintenance/run
POST   /api/kb/maintenance/rebuild
```

---

### 7. 图谱与 UI 规则

**图谱显示**：
- 显示：article、entity、index 节点；mentions 边；contains 边（index_children）
- 不显示：summary 节点（默认）；similar_to 边
- 选中 article/index 时：在详情面板列出其 summaries，不在图中新增节点
- 图谱以选中节点为中心展示邻域，不按 priority_score 过滤可见性（防止节点被静默隐藏）

**资源管理器**：
- Articles 按 source 分组，组内按 `published_at` 排序（可切换入库时间）
- Source 删除：软删除（`deleted_at`），articles 保留，分组标注"[已停用]"
- Source 删除时提供选项：仅停用来源 / 同时删除 N 篇文章

**搜索面板**：右侧列表默认隐藏，通过快捷键（Cmd/Ctrl+F）或工具栏按钮触发。

**Entity 管理**：提供 merge UI（选择两个 entity → 确认目标）和 delete（带确认提示）。

---

## 第二部分：实施计划

### Phase A：Schema 清理

目标：让数据模型与设计决议对齐。

- [ ] `knowledge_nodes` 中已迁移到 object tables 的字段全部删除
- [ ] `nodes` 表加 `embedding_model VARCHAR`
- [ ] `nodes` 表删除 `is_primary`、`priority_score`、`last_accessed_at`、`access_count`、`source_node_ids`
- [ ] `knowledge_edges` 加 `UNIQUE(from_node_id, to_node_id, relation_type)` 约束（先清重复边）
- [ ] `knowledge_edges` 删除所有 `summarizes` 边（改由 `summary_nodes.summary_of` 表达）
- [ ] `entity_candidates.mentions`（JSONB）替换为 `mention_count INT + max_salience FLOAT`
- [ ] 删除 `entity_profiles` 表（entity 描述由 `nodes.abstract` 承担）
- [ ] `sources` 表加 `deleted_at TIMESTAMPTZ`（软删除支持）

验证：DB 中无重复边，object tables 字段与 knowledge_nodes 不再重叠，所有读写路径通过 object tables 而非 knowledge_nodes 专属字段。

---

### Phase B：KB / 应用层代码分离

目标：API 层明确划分 KB 核心与应用层。

- [ ] 目录结构调整：
  ```
  services/api/
    kb/           ← 知识库核心（nodes, edges, sources, search, retrieval, maintenance）
    app/          ← 应用层（briefing, drafts, feedback，后续可删）
  ```
- [ ] `routers/kb.py` 保留并精简为纯 KB 操作
- [ ] `routers/briefing.py`、`routers/drafts.py` 移入 app/，与 KB 接口解耦（通过 KB API 调用，不直接操作 DB）
- [ ] `writing_memory`、`topics`、`briefings` 明确归入应用层，从 KB schema 文档中移除

验证：KB 模块内无对 drafts / briefings / writing_memory 表的直接 SQL 引用。

---

### Phase C：MCP 工具实现

目标：6 个只读工具全部可用，通过外部 MCP adapter 调用。

- [ ] `GET /api/kb/search`：hybrid 搜索，支持 filters
- [ ] `GET /api/kb/nodes/{id}`：fetch，支持批量，返回 outline（index 类型）
- [ ] `GET /api/kb/nodes/{id}/related`：按 relation_type 导航
- [ ] `GET /api/kb/timeline`：entity 或 topic 时间轴
- [ ] `POST /api/kb/retrieve`：summary-first 分层检索
- [ ] `GET /api/kb/entities/{id}/context`：entity 完整上下文

验证：用 10 个真实查询跑完整工具链，结果可用，token 在预算内。

---

### Phase D：算法更新

目标：entity 发现和检索算法与设计决议对齐。

- [ ] `entity_candidates` 改为计数器模式（去掉 JSONB mentions 数组）
- [ ] `retrieve_context` 改为 summary-first（去掉 article 直接参与主路径搜索）
- [ ] tag 生成 prompt 注入 top-50 已有 tags（收敛机制）
- [ ] Ingestion 时自动更新 `embedding_model` 字段
- [ ] Maintenance 任务加：检测 `embedding_model` 不匹配的节点并排队重算

验证：新文章入库后 tags 与已有库中 tags 重叠率提升；retrieve_context 的第一层命中以 summary 为主。

---

### Phase E：UI 更新（/knowledge、/source）

目标：UI 与设计决议对齐。

- [ ] 图谱：默认不显示 summary 节点，不显示 similar_to 边
- [ ] 图谱：选中节点时在详情面板显示 summaries（列表），不在图中新增节点
- [ ] 图谱：以邻域为单位渲染，去掉 priority_score 过滤可见性
- [ ] 资源管理器：按 source 分组，按 `published_at` 排序
- [ ] Source 删除：软删除 + 二次确认选项
- [ ] Entity 管理：merge UI + 硬删除 UI
- [ ] 搜索面板：默认隐藏，快捷键调出

---

## 第三部分：实施进度

> 随实施过程更新，每条记录格式：`[日期] Phase X - 具体内容 - 状态`

（待填入）
