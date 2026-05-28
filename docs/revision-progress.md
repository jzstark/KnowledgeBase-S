# Revision Progress

本文档记录 KnowledgeBase-S 系统重构的设计决议与实施进度。

设计决议部分是本次重构的事实依据，不轻易修改。进度部分随实施持续更新。

---

## 指导原则

**规模与演化**：系统设计要能承载持续增长的内容，数据结构和算法要在大量节点下保持合理性能，图谱和检索要能在内容演化中保持有效。

**边界健壮性**：与现实世界接口处（用户操作、外部数据接入）要假设输入是脏的、不可知的。校验和容错在边界做，内部模块之间可以信任。

**配置外置**：所有可调参数——prompt 字符串、top-k 的 K、阈值、budget 大小、模型名称、token 上限等——一律放入 `config/` 目录，不允许硬编码在代码里。当前已有 `config/system.yaml`（数值参数）和 `config/prompts.md`（prompt 字符串），所有新增参数继续沿用这两个文件，按已有分区组织。

---

## 第一部分：设计决议

### 1. 系统定位调整

KnowledgeBase-S 专注于知识库核心层。**应用层**（每日简报、草稿生成、写作偏好）与 KB 核心解耦，后续可能整体删除。

KB 核心层包括：节点、关系、来源、搜索、MCP 工具。

应用层包括：`briefing`、`drafts`、`writing_memory`、`topics`、`briefings`（已废弃）。

MCP 工具全部只读。MCP adapter 实现位于外部仓库 `~/Code/kb-chat/`，封装 KB-S 暴露的 `/api/kb/v1/` 稳定接口。KB-S 本身只负责暴露干净的 REST 接口，不内置 MCP server。

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
  doc_kind VARCHAR           -- regulation|case|news|memo|contract|analysis|other
                             -- 继承链：source.default_doc_kind → source_items.doc_kind → nodes.doc_kind
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
  default_doc_kind VARCHAR   -- 来源级默认文件类型，cascade 到下属 source_items
  deleted_at TIMESTAMPTZ     -- 软删除，articles 仍保留并分组显示

source_items
  id, source_id FK, origin_ref, extracted_text_ref
  doc_kind VARCHAR           -- 继承 source.default_doc_kind，可被 nodes.doc_kind 覆盖
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

### 4. MCP 工具（只读，7 个）

MCP 工具全部只读，由 `~/Code/kb-chat/` 中的 MCP adapter 封装，调用 KB-S 的 `/api/kb/v1/` 稳定接口。

---

#### `search` — 关键词/语义搜索

**USE WHEN**：初步探索、定位特定文章或 entity；需要快速获取相关节点列表。  
**DO NOT USE**：需要完整正文（用 `fetch`）；需要节点间关系（用 `related`）。

```
Input:
  query: str
  filters:
    type: list[article|entity|summary|index]
    tags: list[str]
    source_ids: list[str]
    doc_kind: list[regulation|case|news|memo|contract|analysis|other]
    date_range: {from: date, to: date}
  top_k: int          # default: config.search.top_k
  include_snippet: bool  # default: true

Output:
  results: [
    {
      id, type, title, doc_kind,
      snippet: str,        # abstract 摘录
      score: float,
      why_matched: str,    # keyword | vector | hybrid
      tags: list[str],
      published_at
    }
  ]
```

Token budget：无 LLM 调用。

---

#### `fetch` — 节点详情

**USE WHEN**：已有节点 ID，需要完整内容（正文、摘要列表、index 大纲）。  
**DO NOT USE**：不知道 ID 时（先用 `search`）。

```
Input:
  ids: list[str]           # 支持批量（最多 config.fetch.max_batch 个）
  include_body: bool       # default: true；article 正文从文件系统读取
  include_related_ids: bool  # default: false

Output per node:
  {
    id, type, title, doc_kind, abstract, tags,
    published_at, source_id, source_name,
    body: str,             # article 正文（include_body=true 时）
    summaries: [           # 该节点的摘要列表
      {id, perspective_label, body_snippet, is_default}
    ],
    outline: [             # index 类型时返回子节点列表
      {id, title, position, type}
    ]
  }
```

Token budget：无 LLM 调用。

---

#### `related` — 关系导航

**USE WHEN**：从已知节点出发，浏览 entity 提及的文章、index 包含的子节点等关系。  
**DO NOT USE**：宽泛搜索（用 `search`）；需要全文（用 `fetch`）。

```
Input:
  id: str
  relation: mentions | mentioned_by | summarizes | summarized_by
           | contains | part_of
  limit: int    # default: config.related.default_limit

Output:
  [
    {id, type, title, doc_kind, relation, weight: float, published_at}
  ]
```

Token budget：无 LLM 调用。

---

#### `timeline` — 时间轴

**USE WHEN**：追踪某 entity（法规、案件、机构）的历史演变；需要按时间线理解某主题的发展脉络。  
**DO NOT USE**：不以时间维度为核心的查询（用 `search` 或 `related`）。

```
Input:
  entity_id: str       # 以 entity 为锚点（与 topic_query 二选一）
  topic_query: str     # 以语义 query 为锚点
  date_range: {from: date, to: date}
  limit: int           # default: config.timeline.default_limit
  include_facts: bool  # default: false；true 时返回 entity_facts 条目

Output:
  events: [
    {
      published_at, article_id, title, doc_kind,
      source_name,
      fact_text: str,    # include_facts=true 时
      evidence_span: str
    }
  ]
```

Token budget：无 LLM 调用（fact extraction 在 ingestion 时已完成）。

---

#### `compare` — 多节点对比

**USE WHEN**：比较多个法规/判例/合同在特定维度（管辖权、责任、处罚标准等）上的异同。  
**DO NOT USE**：简单查找（用 `search`/`fetch`，无需 LLM 开销）；超过 5 个节点（结果质量下降）。

```
Input:
  node_ids: list[str]       # 2-5 个节点
  dimensions: list[str]     # 可选，指定对比维度（如 ["适用范围", "罚则", "例外情形"]）
  focus: str                # 可选，自由文本指导（如 "重点比较数据出境条款"）

Output:
  {
    comparison_table: str,  # Markdown 表格
    analysis: str,          # LLM 生成的综合分析
    sources_used: [
      {id, title, doc_kind, published_at}
    ]
  }
```

Token budget：中（1 次 LLM 调用，以节点 abstract + 部分正文为输入）。

---

#### `cite` — 引证查找

**USE WHEN**：为法律主张/论点寻找原文依据；需要可溯源的精确引语。  
**DO NOT USE**：一般性搜索（用 `search`）；不需要精确引语时（`search` + `fetch` 即可）。

```
Input:
  claim: str              # 需要支撑的法律主张或命题
  context: str            # 可选，提供背景（如所在文件类型、法域）
  doc_kinds: list[str]    # 可选，限定来源文件类型
  max_results: int        # default: config.cite.max_results

Output:
  citations: [
    {
      article_id, title, doc_kind, published_at,
      quote: str,                   # 服务端验证过的精确原文引语
      relevance_explanation: str,   # 为什么这段话支撑该主张
      confidence: float
    }
  ]
```

两阶段算法（详见「算法」部分）。Token budget：中（1 次 LLM 调用）。

---

#### `summarize_corpus` — 语料综述

**USE WHEN**：需要对一批来源进行综合概括（如"现行数据安全法规的整体要求"）；需要对检索结果提炼观点。  
**DO NOT USE**：需要精确引语（用 `cite`）；只有 1-2 个节点（直接用 `fetch`）。

```
Input:
  node_ids: list[str]    # 显式指定语料（与 query 二选一）
  query: str             # 通过搜索隐式确定语料
  max_sources: int       # default: config.summarize_corpus.max_sources
  focus: str             # 可选，综述聚焦方向
  output_format: bullet | prose | structured   # default: prose

Output:
  {
    summary: str,          # LLM 生成的综述
    sources_used: [
      {id, title, doc_kind, published_at}
    ],
    coverage_note: str     # 如 "基于 12 篇文章，时间跨度 2019-2024"
  }
```

Token budget：高（1 次 LLM 调用，输入上下文较大，由 `config.summarize_corpus.max_input_tokens` 控制）。

---

### 5. 搜索与检索算法

#### `search` 工具（轻量，无 LLM）

```
query → embed → nodes.embedding 向量搜索
       → title + abstract 关键词搜索（pg tsvector）
       → hybrid 合并排序
       → filters 过滤（type / doc_kind / tags / source_id / date_range）
       → 返回轻量列表（id / title / snippet / score / why_matched）
```

#### `cite` 两阶段算法

**Stage 1：粗筛（无 LLM）**
```
embed(claim) → top-20 候选文章（向量搜索 + doc_kind 过滤）
```

**Stage 2：精确匹配（LLM + 服务端验证）**
```
将候选文章的 abstract + body_snippet 注入 LLM
LLM 返回：[{article_id, candidate_quote, explanation, confidence}]
服务端验证：对每条 candidate_quote 在原文中做字符串匹配
  → 匹配成功 → 纳入结果
  → 不存在于原文 → 丢弃（防止 LLM 幻觉）
最终仅返回服务端验证通过的引语
```

#### 内部 summary-first 分层检索（`compare` / `cite` / `summarize_corpus` 共用）

当上述工具需要为 LLM 准备输入上下文时，内部采用以下分层策略：

```
Step 1: embed(query/claim/focus)
Step 2: 并行搜索
  a. summary_nodes.body_embedding → top-K summaries
  b. nodes.embedding (type=entity) → top-K entities（可选）
Step 3: 展开高分节点
  a. 高分 summary → 取 summary_of 的 abstract
     若 summary_of 是 index → 取 child summaries
  b. 若 summary 命中 < 阈值 → fallback 直接 article 向量搜索
Step 4: 去重、排序、按 budget 截断
```

此逻辑为内部实现细节，不作为独立工具对外暴露。

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

### 6. API 层分离

KB-S 的 API 分为两个路由文件，职责明确：

```
services/api/
  kb_public.py    ← MCP 稳定接口，前缀 /api/kb/v1/，FastAPI tag="KB Public"
  kb_internal.py  ← 内部管理接口（用户操作、维护任务），FastAPI tag="KB Internal"
```

- `kb_public.py`：对应 7 个 MCP 工具的后端端点。接口稳定，变更需前向兼容。
- `kb_internal.py`：Summary CRUD、entity 合并/删除、index 管理、maintenance 触发等。
- OpenAPI 文档分离：`/api/docs`（内部全量）vs `/api/kb/v1/docs`（仅 Public）。
- MCP adapter（`~/Code/kb-chat/`）只依赖 `kb_public.py` 的接口，不依赖 `kb_internal.py`。

---

### 7. 用户操作 API（KB Internal）

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
PATCH  /api/kb/nodes/{id}/metadata           修改 title / tags / published_at / doc_kind
POST   /api/kb/nodes/{id}/archive            软删除（status = archived）
DELETE /api/kb/nodes/{id}                    硬删除

# 维护
POST   /api/kb/maintenance/run
POST   /api/kb/maintenance/rebuild
```

---

### 8. 图谱与 UI 规则

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

**doc_kind 交互**：

doc_kind 是 config 控制的枚举（`config/system.yaml` 的 `doc_kind.values`），**不允许用户自定义字符串**。自由文本会让过滤失去意义，退化为 tags；枚举扩展由运维人员通过修改 config 完成。

| 入库方式 | doc_kind 的来源 |
|---|---|
| 手动上传 PDF / Word / image | 上传表单提供下拉选择，**必填** |
| 添加单个 URL | 添加时下拉选择（默认 `news`，可改） |
| RSS / WeChat / 批量来源 | source 配置时指定 `default_doc_kind`；可在 source_items 列表里手动覆盖个别条目 |
| 书籍 EPUB | 导入时选择（通常 `other` 或 `analysis`） |

节点入库后，doc_kind 可通过节点元数据编辑（`PATCH /api/kb/nodes/{id}/metadata`）逐条覆盖，UI 同样提供下拉而非自由输入。

---

## 第二部分：实施计划

### Phase A：Schema 清理 + 参数审计

目标：让数据模型与设计决议对齐，同时清查并外置所有硬编码参数。

- [ ] `knowledge_nodes` 中已迁移到 object tables 的字段全部删除
- [ ] `nodes` 表加 `embedding_model VARCHAR`
- [ ] `nodes` 表加 `doc_kind VARCHAR`（枚举值定义在 `config/system.yaml` 的 `doc_kind.values`）
- [ ] `nodes` 表删除 `is_primary`、`priority_score`、`last_accessed_at`、`access_count`、`source_node_ids`
- [ ] `sources` 表加 `default_doc_kind VARCHAR`
- [ ] `source_items` 表加 `doc_kind VARCHAR`
- [ ] `knowledge_edges` 加 `UNIQUE(from_node_id, to_node_id, relation_type)` 约束（先清重复边）
- [ ] `knowledge_edges` 删除所有 `summarizes` 边（改由 `summary_nodes.summary_of` 表达）
- [ ] `entity_candidates.mentions`（JSONB）替换为 `mention_count INT + max_salience FLOAT`
- [ ] 删除 `entity_profiles` 表（entity 描述由 `nodes.abstract` 承担）
- [ ] `sources` 表加 `deleted_at TIMESTAMPTZ`（软删除支持）

- [ ] 审计代码中所有硬编码参数，迁移到 `config/system.yaml` 或 `config/prompts.md`
  - 数值类（top-k、阈值、budget、token 上限、batch size 等）→ `system.yaml`
  - Prompt 字符串 → `prompts.md`
  - 模型名称 → `system.yaml`（`models` 分区）
  - doc_kind 枚举值 → `system.yaml`（`doc_kind.values`）
- [ ] `config_loader.py` / `prompt_loader.py` 确保覆盖所有新增 key，缺失 key 启动时报错而非静默使用默认值

验证：DB 中无重复边，object tables 字段与 knowledge_nodes 不再重叠，所有读写路径通过 object tables 而非 knowledge_nodes 专属字段；`grep -r "top_k\|max_k\|limit.*=.*[0-9]" services/` 无硬编码数值参数。

---

### Phase B：KB / 应用层代码分离 + API 层划分

目标：API 层明确划分 KB 核心与应用层；MCP 接口与内部管理接口分离。

- [ ] 目录结构调整：
  ```
  services/api/
    kb/           ← 知识库核心（nodes, edges, sources, search, retrieval, maintenance）
    app/          ← 应用层（briefing, drafts, feedback，后续可删）
  ```
- [ ] `kb_public.py`：提取 7 个 MCP 工具对应的端点，前缀 `/api/kb/v1/`，FastAPI tag `"KB Public"`
- [ ] `kb_internal.py`：用户操作 API（summary CRUD / entity 管理 / index 管理 / maintenance），FastAPI tag `"KB Internal"`
- [ ] 配置 FastAPI 生成两份 OpenAPI 文档：
  - `/api/docs`：内部全量文档
  - `/api/kb/v1/docs`：仅 Public 接口，供 `kb-chat` 对接
- [ ] `routers/briefing.py`、`routers/drafts.py` 移入 `app/`，与 KB 接口解耦（通过 KB API 调用，不直接操作 DB）
- [ ] `writing_memory`、`topics`、`briefings` 明确归入应用层，从 KB schema 文档中移除

验证：KB 模块内无对 drafts / briefings / writing_memory 表的直接 SQL 引用；`kb_public.py` 内无写入操作。

---

### Phase C：MCP 工具实现（7 个）

目标：7 个只读工具全部可用，接口与设计决议对齐。

- [ ] `GET /api/kb/v1/search`：hybrid 搜索，支持 type / doc_kind / tags / source / date 过滤
- [ ] `GET /api/kb/v1/nodes/{id}`（支持批量）：fetch，返回 summaries 列表 + index outline
- [ ] `GET /api/kb/v1/nodes/{id}/related`：按 relation 类型导航
- [ ] `GET /api/kb/v1/timeline`：entity 或 topic 时间轴，支持 entity_facts
- [ ] `POST /api/kb/v1/compare`：多节点对比，LLM 生成 comparison_table + analysis
- [ ] `POST /api/kb/v1/cite`：两阶段引证查找，返回服务端验证过的精确引语
- [ ] `POST /api/kb/v1/summarize_corpus`：语料综述，支持显式 node_ids 或隐式 query

验证：用 10 个真实法律场景查询跑完整工具链，结果可用，token 在 config 预算内；`cite` 输出的所有 quote 均可在原文中找到。

---

### Phase D：算法更新

目标：entity 发现和检索算法与设计决议对齐。

- [ ] `entity_candidates` 改为计数器模式（去掉 JSONB mentions 数组）
- [ ] `cite` 实现两阶段算法：向量粗筛 → LLM 精确匹配 → 服务端 quote 验证
- [ ] `compare` / `summarize_corpus` 内部使用 summary-first 分层检索准备 LLM 上下文
- [ ] tag 生成 prompt 注入 top-50 已有 tags（收敛机制）
- [ ] Ingestion 时自动更新 `embedding_model` 字段
- [ ] Maintenance 任务加：检测 `embedding_model` 不匹配的节点并排队重算
- [ ] `doc_kind` 继承逻辑：ingestion 时从 `source.default_doc_kind` → `source_items.doc_kind` → `nodes.doc_kind` 逐级继承，允许逐节点覆盖

验证：新文章入库后 tags 与已有库中 tags 重叠率提升；cite 输出 quote 服务端验证通过率 100%；summary-first 的第一层命中以 summary 为主。

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
- [ ] 上传/添加 URL 表单：加入 doc_kind 下拉（枚举来自 config），上传必填，URL 默认 `news`
- [ ] Source 配置页：加入 `default_doc_kind` 字段（下拉）
- [ ] Source items 列表：每条 item 允许手动覆盖 doc_kind（下拉，不允许自由输入）
- [ ] 节点元数据编辑面板：doc_kind 字段展示为下拉（下拉值从 config 读取），不提供自由文本输入

---

## 第三部分：实施进度

> 随实施过程更新，每条记录格式：`[日期] Phase X - 具体内容 - 状态`

### 2026-05-27

- Phase A · 加列 · ✅ `services/api/database.py` SCHEMA_SQL 新增：
  - `knowledge_nodes.embedding_model VARCHAR`
  - `knowledge_nodes.doc_kind VARCHAR`
  - `sources.default_doc_kind VARCHAR`
  - `sources.deleted_at TIMESTAMPTZ`
  - `source_items.doc_kind VARCHAR`
  - 全部 `ALTER TABLE IF EXISTS ... ADD COLUMN IF NOT EXISTS`，幂等，不影响现有数据
- Phase A · doc_kind 枚举配置 · ✅ `config/system.yaml` 新增 `doc_kind.values`（regulation/case/news/memo/contract/analysis/other）+ `doc_kind.default=other`
- Phase A · 参数审计 · ✅ 现状：代码库已大量通过 `config_loader.get(...)` 读取参数（`models.*` / `embedding.*` / `entity.*` / `retrieval.*` / `llm_output_tokens.*` 等已完整外置）
  - 本批迁出：`maintenance.py` 的 rebuild 轮询参数（`maintenance.rebuild_max_wait_seconds` / `rebuild_poll_interval_seconds`）
  - 剩余硬编码集中在三个地方，全部延后到对应 Phase 一起处理，避免在即将搬迁/重写的代码上重复改：
    - `routers/briefing.py`（BATCH_SIZE、max_tokens=8192）、`routers/drafts.py`（100 阈值）、`routers/settings.py`（briefing_hours_back=24）→ Phase B 迁入 `app/` 时统一外置
    - `kb_tools.py`（wiki body limit=4000、search limit 1-10 边界）→ Phase C 重写 MCP 工具时统一外置
    - `entity_insights.py`（refresh limit=200）→ Phase D entity 算法更新时一并处理
- Phase B · API 层分离 · ✅ 骨架完成（端点实际迁移延后到 Phase C）：
  - 新建 `services/api/routers/kb_public.py`：FastAPI router（tag=`"KB Public"`）含 7 个工具 stub（search / fetch / fetch_batch / related / timeline / compare / cite / summarize_corpus），均返回 501，Pydantic 请求/响应类型已定义
  - `routers/kb.py` tag 改为 `"KB Internal"`（文件名保留，避免破坏 5 处跨模块导入）
  - `main.py`：挂载 `kb_public_app` 子应用于 `/api/kb/v1/`，自动获得独立 OpenAPI 文档 `/api/kb/v1/docs`
  - 主应用 `/api/docs` 显示全量；MCP adapter 只看 `/api/kb/v1/docs`
  - **延后**：`briefing/drafts` 物理移入 `app/`（涉及 `drafts.py → kb._hyde_embed_query`、`maintenance.py → kb.write_wiki_node` 等跨模块导入，留给专门的批次处理）
- Phase C · 7 个 MCP 工具实现 · ✅ `routers/kb_public.py`（904 行）实现 8 个端点对应 7 个工具：
  - `GET  /api/kb/v1/search` → hybrid 向量+关键词搜索；filters：type / tags / source_ids / doc_kind / date_from / date_to；keyword_hit 触发 +0.15 分加成，why_matched 标记 vector/keyword/hybrid
  - `GET  /api/kb/v1/nodes/{id}` → 单节点 fetch，含 summaries 列表、index outline、source_name；`include_body` 控制 wiki 正文，`include_related_ids` 输出可见边
  - `POST /api/kb/v1/nodes/batch` → 批量 fetch，受 `kb_public.fetch_max_batch` 限制
  - `GET  /api/kb/v1/nodes/{id}/related` → 6 种 relation（mentions / mentioned_by / summarizes / summarized_by / contains / part_of）的固定 SQL 路由表
  - `GET  /api/kb/v1/timeline` → entity_id 锚点走 `mentions` 边；topic_query 锚点走向量过滤（`kb_public.timeline_min_score` 阈值）；`include_facts=true` 时合并 `entity_facts`
  - `POST /api/kb/v1/compare` → 2-5 节点对比，LLM（`compare_nodes` prompt）生成 Markdown 表 + 分析；服务端按最后一个 `|` 行切分表格与分析
  - `POST /api/kb/v1/cite` → 两阶段算法
    - Stage 1：embed(claim) → top-N 候选（`kb_public.cite_candidate_count`，doc_kind 过滤）
    - Stage 2：`cite_match` prompt → LLM 返回 JSON 数组 → 服务端逐字 substring 验证 → 不在原文者丢弃
    - 返回服务端验证通过的 `{article_id, quote, relevance_explanation, confidence}`
  - `POST /api/kb/v1/summarize_corpus` → 显式 `node_ids` 或 `query`（向量搜索）确定语料；LLM（`summarize_corpus` prompt）按 bullet/prose/structured 输出；附 `coverage_note`（基于 N 篇文档 + 时间跨度）
  - 共享工具函数：`_load_doc_context` / `_docs_to_prompt_text` / `_source_descriptor` / `_extract_json_array`（带 fence 与 preamble 容错）
  - 路由设计：`POST /nodes/batch` 与 `GET /nodes/{id}` 不冲突（按 method+path 匹配）
- Phase C · 配置补充 · ✅
  - `config/system.yaml`：新增 `kb_public.*` 13 个参数（top_k / batch / cite 两阶段计数 / 各 body 截断）；`models.{compare,cite,summarize_corpus}`、`llm_output_tokens.{compare,cite,summarize_corpus}` 各 3 项
  - `config/prompts.md`：新增 `compare_nodes` / `cite_match` / `summarize_corpus` 三段 prompt，cite_match 显式约束"逐字出现、禁止改写"
- Phase C · 收口修正 · ✅
  - `services/api/kb/public.py` search：keyword-only 节点不再因缺少 embedding 被过滤；vector score 缺失时降为 0，keyword 命中仍可进入结果
  - `services/api/kb/public.py` cite：Stage 2 按 `kb_public.cite_body_chars` 从候选文章全文抽取相关窗口输入 LLM；服务端 quote 验证读取全文，避免只验证前 N 字符
  - `~/Code/kb-chat/services/kb-mcp/kb_mcp/server.py`：MCP adapter 切换到 `/api/kb/v1/*`，暴露 search / fetch / batch fetch / related / timeline / compare / cite / summarize_corpus 七类 KB 工具
  - 新增轻量合同测试：`services/api/tests/test_kb_public_contract.py` 与 kb-chat MCP adapter route/tool contract test
- Phase D · entity_candidates 计数器 · ✅
  - `database.py`：新增 `entity_candidates.mention_count INT` + `max_salience FLOAT`；幂等 backfill UPDATE 从历史 JSONB 浓缩计数（仅当 mention_count=0 且 mentions 非空时执行）
  - `routers/kb.py` `process_entity_candidates`：upsert 时同步递增 `mention_count` 与 `GREATEST(max_salience, :salience)`；晋升判定从计数器列读，不再 `jsonb_array_length`
  - `routers/kb.py` `entity_analyze_context`：候选池按 `mention_count DESC, max_salience DESC` 排序，跨过 JSONB
  - JSONB `mentions` 列暂保留，用于晋升时取 `source_article_ids`；Phase A 第三批移除
- Phase D · doc_kind 继承 · ✅
  - `routers/kb.py` `IngestRequest`：新增 `doc_kind: str | None`
  - `ingest()`：cascade 实现 — 显式 > `source_items.doc_kind` > `sources.default_doc_kind` > `config.doc_kind.default`；非法枚举值降级为 default（不阻断 ingestion）
  - INSERT 同步写入 `knowledge_nodes.doc_kind`
- Phase D · embedding_model 追踪 · ✅
  - `IngestRequest` 增 `embedding_model`；`ingest()` 显式写入 `knowledge_nodes.embedding_model`（未给则取当前 `config.embedding.model`）
  - `ingestion-worker/pipeline.py` `_article_ingestion_adapters` 注入 `embedding_model`；`article_ingestion.py` 在 article / summary / entity 三种 payload 中均带上
  - `maintenance.detect_embedding_model_drift()`：检测 `embedding_model IS NULL OR != current_model` 的节点，按 model / object_type 分桶 + 20 个 sample_ids；并入 `run_maintenance` 日志输出但**不自动重算**（重算属重操作，应人工触发）
- Phase D · tag 收敛 · ✅
  - `routers/kb.py` `entity_analyze_context` 返回新增 `popular_tags`（按频次倒排 top-50；列表大小由 `ingestion.context_popular_tags` 控制）
  - `ingestion-worker/pipeline.analyze_article` 增 `popular_tags` 参数，注入 `article_analysis` prompt
  - `config/prompts.md` `article_analysis`：新增 `<<<existing_tags>>>` 占位符及收敛指令（优先复用、仅新主题才造新词）
  - `article_ingestion.py` 把 context 中的 `popular_tags` 透传给 `analyze_article`
  - 测试 fixture `tests/test_article_ingestion.py` 同步更新签名（含 `popular_tags`）+ 适配器加 `embedding_model="text-embedding-3-small"`
- Phase D · summary-first 检索（summarize_corpus）· ✅
  - `routers/kb_public.py` `summarize_corpus` 的 query 路径改为分层：
    - Stage 1：在 `summary_nodes.body_embedding` 向量搜索，命中阈值 `kb_public.summarize_summary_min_score` 的取出 `summary_of` 文章 id
    - Fallback：summary 路径未填满 `max_sources` 时，补足直接在 `knowledge_nodes(object_type='article')` 上做向量搜索，排除已选
  - 显式 `node_ids` 路径维持原行为
- Phase D · summary-first 上下文（compare）· ✅
  - `services/api/kb/public.py` `_load_doc_context()`：article 节点优先读取 `summary_nodes` 中 default summary（`is_default DESC, created_at ASC`），缺 summary 时才回退 wiki 正文
  - summary 节点直接使用自身 body/abstract，避免 compare 工具在已有 summary 可用时把整篇 article 正文塞进 LLM 上下文
- Phase D · 配置补充 · ✅
  - `ingestion.context_nearby_entities / context_top_candidates / context_popular_tags`（20 / 20 / 50）
  - `kb_public.summarize_summary_min_score`（0.3）

**Phase D 设计取舍 / 延后**
- embedding drift 当前**只检测+报告，不自动重算**。理由：re-embed 会批量改写向量、影响检索结果和 API 成本，应由显式 rebuild/re-embed job 在人工确认后触发，而不是混进普通 maintenance 自动任务。
- `entity_insights.refresh_stale_entity_profiles` 的 limit=200 仍硬编码（涉及 entity profile 流程，不在 Phase D 核心算法范畴，留给后续整理）

### Phase A 第三批（破坏性）· ✅ 已完成

**实际执行范围（设计文档中标记的 7 项里，安全可做的 4 项）**

1. **entity_candidates.mentions JSONB → 完全移除**
   - `database.py` `CREATE TABLE` 中删 `mentions JSONB DEFAULT '[]'`，新建 schema 直接使用计数器结构
   - `database.py` 新增 `source_article_ids TEXT[]` 列；`init()` 中条件迁移块（`if has_mentions:` 守卫）从 JSONB 抽取 `article_id` 数组后 `DROP COLUMN mentions`，幂等
   - 改写 4 处 mentions JSONB 读取点：
     - `kb.py process_entity_candidates`（写入+晋升判定）：read `source_article_ids` 数组，写入用 `array_append`
     - `kb.py _materialize_candidate_facts`：用 `source_article_ids` + 候选侧 `max_salience` 作回填权重；不再有 per-article salience（兜底场景可接受）
     - `kb.py list_entity_candidates`：用 `mention_count / max_salience` 列排序
     - `maintenance.py promote_entity_candidates`：用 `mention_count / max_salience / source_article_ids`
     - `maintenance.py backfill_wikilinks_for_entity`：salience_map 从 `entity_facts.confidence` 构建（per-article salience 在 ingestion 时已经通过 `upsert_fact_from_mention` 固化到 `entity_facts.confidence`）
     - `maintenance.py migrate_wikilink_edges` Step A：salience 来源切到 `entity_facts.confidence`
     - `maintenance.py rebuild_from_raw` 清空候选：用 `source_article_ids && base_ids[]`，不再 `jsonb_array_elements`

2. **knowledge_nodes 应用层遗留字段删除**
   - `database.py`：删除 `ADD COLUMN priority_score / last_accessed_at / access_count`；新增 `DROP COLUMN IF EXISTS`；删除 `idx_knowledge_nodes_priority` 索引声明，增加 `DROP INDEX IF EXISTS`
   - 全代码库 grep 确认无 Python 引用，纯 schema 清理

3. **summarizes 边删除**
   - `database.py`：`DELETE FROM knowledge_edges WHERE relation_type = 'summarizes'`（幂等）
   - 删除三处写入：
     - `kb.py generate_summary_job`：删除 INSERT summarizes 边
     - `maintenance.py backfill_summarizes_edges`：整个函数删除 + 从 `run_maintenance` 调用链移除 + 顶部 docstring 更新
     - `maintenance.py restore_from_wiki`：删除 summarizes `_add_edge` 调用
   - 读取已用 `summary_nodes.summary_of` FK 替代：
     - `drafts.py` Phase 2c：`SELECT node_id, summary_of FROM summary_nodes WHERE node_id IN (...)`
     - `kb_public.py related` 工具的 `summarizes / summarized_by` 已经走 `summary_nodes` JOIN，无需改

4. **knowledge_edges UNIQUE 约束**
   - `database.py`：去重 DELETE（`row_number() OVER (PARTITION BY ...)` CTE，保留每组最小 id，幂等）
   - `init()`：检测 `uq_edges_from_to_type` 约束是否存在，缺则 `ADD CONSTRAINT UNIQUE(from_node_id, to_node_id, relation_type)`

**本批延后的项（设计列表里剩 3 项）**

- **`knowledge_nodes.is_primary`**：briefing.py 仍按它过滤（应用层概念，正确归宿是改为 JOIN `sources.is_primary`）；kb.py 4+ 处 INSERT 和 worker 都还在写入。等 briefing/drafts 迁入 `app/` 时一起处理
- **`entity_profiles` 表**：`entity_insights.py` 4 处引用 + `kb.py:get_entity_timeline` 直接读 timeline_summary + `run_maintenance.refresh_stale_entity_profiles`。需要先把 entity 描述读路径全部切到 `nodes.abstract`，再删表。属于独立批次
- **`knowledge_nodes` 其余对象专属字段**（canonical_name / aliases / perspective_* / body_embedding / source_node_ids / raw_ref / source_type）：每个字段都有多写多读路径，object tables 与 knowledge_nodes 同时被读写。需要逐字段做"切读 → 改写 → 删列"三步，跨越 ingest endpoint / object_nodes / maintenance / kb_tools 多文件。属于独立大批次

**当前 schema 状态（Phase A 第三批后）**

- `knowledge_nodes`：去掉了 `priority_score`、`last_accessed_at`、`access_count`；其余字段保持
- `entity_candidates`：JSONB `mentions` 完全消失；保留 `mention_count / max_salience / source_article_ids` 三列
- `knowledge_edges`：UNIQUE(from, to, type) 强制约束；`summarizes` 边永久清空
- 其余表保持原状

### 延后项 1：knowledge_nodes.is_primary 删除 · ✅

- `database.py`：CREATE TABLE 去 `is_primary BOOLEAN`；`SCHEMA_SQL` 加 `DROP COLUMN IF EXISTS is_primary`。`sources.is_primary` 保留
- `briefing.py`：搜索 article 时 JOIN `sources s ON s.id = n.source_id`，过滤改为 `s.is_primary = true`；`KNOWLEDGE_TIME_SQL` 和 `node_cutoff` 改用 `n.` 前缀避免与 sources 字段歧义
- `kb.py`：删除 `IngestRequest.is_primary`；3 处 INSERT INTO knowledge_nodes 去掉 `is_primary` 列与参数
- `maintenance.py restore_from_wiki`：INSERT 去掉 `is_primary`
- `ingestion-worker`：`ArticleIngestionInput.is_primary` 删除；`pipeline.run_pipeline` 不再透传 `source_config.is_primary`；测试 fixture 同步

### 延后项 2：entity_profiles 表删除 · ✅

- `database.py`：CREATE TABLE 删除；6 行 ALTER ADD COLUMN 清除；`idx_entity_profiles_status` 索引声明删除；加 `DROP TABLE IF EXISTS entity_profiles CASCADE`
- `entity_insights.py`：
  - 删除 `mark_profile_stale`、`refresh_stale_entity_profiles`
  - `refresh_entity_profile` 改为直接 `UPDATE knowledge_nodes SET abstract = ...` —— entity 描述统一回到 `nodes.abstract`
  - `upsert_entity_fact` 不再调用 `mark_profile_stale`
- `maintenance.py`：`run_maintenance` 去掉 `refresh_stale_entity_profiles` 调用与 `entity_profiles` 输出字段；顶部 docstring 已在 Phase A 第三批同步
- `kb.py get_entity_timeline`：不再 SELECT `entity_profiles`；返回字段从 `timeline_summary / profile_status / profile_refreshed_at` 改为 `abstract / abstract_updated_at`（更贴近"description 即 nodes.abstract"的新模型）
- `/entities/{id}/regenerate` 端点：通过 `refresh_entity_profile` 重算 abstract（确定性 fact-summary，无 LLM 调用）。Phase E / 后续批次可改为 Claude `entity_page` prompt

### 延后项 3：knowledge_nodes 对象专属字段裁剪 · ✅

**删除的 9 个对象专属字段**

| 字段 | 归属 | 权威来源 |
|---|---|---|
| `summary_of` | summary | `summary_nodes.summary_of` |
| `perspective` (legacy alias) | summary | `summary_nodes.perspective_label` |
| `perspective_label` | summary | `summary_nodes.perspective_label` |
| `perspective_instruction` | summary | `summary_nodes.perspective_instruction` |
| `perspective_embedding` | summary | `summary_nodes.perspective_embedding` |
| `body_embedding` | summary | `summary_nodes.body_embedding` |
| `is_default` | summary | `summary_nodes.is_default` |
| `canonical_name` | entity | `entity_nodes.canonical_name` |
| `aliases` | entity | `entity_nodes.aliases` |

**database.py 改动**

- CREATE TABLE `knowledge_nodes` 同步去除 9 列
- 删除 19 行无用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`（这些列在 DROP 之前曾被加，现已 DROP）
- 新增 `DROP CONSTRAINT IF EXISTS fk_summary_of`
- 新增 `DROP INDEX IF EXISTS idx_knowledge_nodes_summary_of / idx_knowledge_nodes_body_embedding / idx_knowledge_nodes_perspective_embedding`
- 新增 9 行 `ALTER TABLE ... DROP COLUMN IF EXISTS`
- 删除 Phase 0 的 `INSERT INTO {summary,entity}_nodes SELECT ... FROM knowledge_nodes` 一次性 backfill 块（依赖已删字段，且 object_nodes 模块已是权威写入路径）
- `init()` 移除 `fk_summary_of` 添加块（约束随 DROP COLUMN 消失）

**写入清理（3 处 INSERT INTO knowledge_nodes）**

- `kb.py ingest()`：INSERT 列表删除 9 字段；本地 `perspective_label / perspective_instruction / is_default / body_embedding_literal / perspective_embedding_literal` 计算保留供 `upsert_object_node`（object 表权威写入）
- `kb.py generate_summary_job`：INSERT 列表删除 7 个 summary 字段（perspective/body_embedding/is_default 等）；保留 `upsert_object_node` 调用
- `maintenance.py restore_from_wiki`：INSERT 列表删除 `summary_of / canonical_name / aliases / perspective`；dict 参数同步删除

**读取清理（COALESCE 链精简）**

- `kb.py /search`：drop 三条 COALESCE 中的 `n.*` 回退（`COALESCE(s.body_embedding, n.body_embedding, n.embedding)` → `COALESCE(s.body_embedding, n.embedding)`）；perspective/is_default 三列直接读 `s.*`；filter 条件去掉 `n.body_embedding IS NOT NULL`
- `kb_tools.py search`：同样处理
- `kb_public.py search`：同样处理
- `entity_insights.py`（3 处）：`COALESCE(en.canonical_name, n.canonical_name, n.title)` → `COALESCE(en.canonical_name, n.title)`
- `kb.py get_related_entities`：`COALESCE(en.canonical_name, other.canonical_name, other.title)` → `COALESCE(en.canonical_name, other.title)`
- `drafts.py` Phase 3 child summary expansion：`COALESCE(sn.summary_of, kn.summary_of)` → `sn.summary_of`；查询条件简化为 `JOIN summary_nodes`
- `kb.py ingest()` 实体去重：`SELECT id FROM knowledge_nodes WHERE canonical_name = :name` → 改为 `JOIN entity_nodes en` 后比 `en.canonical_name`

**后续已清理的字段**（原记录曾标记为暂缓）

| 字段 | 当前状态 |
|---|---|
| `raw_ref` | 已从 `knowledge_nodes` 删除；article 权威值在 `article_nodes.raw_ref`，读取由 `object_nodes.fetch_node_with_object_fields()` 兼容派生 |
| `source_type` | 已从 `knowledge_nodes` 删除；article 权威值在 `article_nodes.source_type`，summary/entity/index 由 object type 派生 |
| `source_node_ids` | 已从 `knowledge_nodes` 删除；summary 来源列表写入 `summary_nodes.source.source_node_ids`，entity 来源由 `entity_facts` 推导 |
| `source_item_id` | 已从 `knowledge_nodes` 删除；article 权威值在 `article_nodes.source_item_id` |
| `source_published_at / source_updated_at / captured_at / effective_at` | 已从 `knowledge_nodes` 删除；原始来源时间保留在 `article_nodes/source_items`，`knowledge_nodes.published_at` 作为通用知识时间索引 |

迁移策略：启动时先把旧列回填到 object tables 与 `knowledge_nodes.published_at`，再幂等删除旧列。全库通用时间过滤改为 `COALESCE(n.published_at, n.ingested_at, n.created_at)`；需要原始发布/捕获时间的路径通过 `article_nodes` 或 `source_items` 查询。

**所有可校验项**
- `python3 -c "import ast; ..."` 通过 11 个文件
- `grep -E "n\.(canonical_name|aliases|perspective|...|summary_of)"` 返回空（无直接读取残留）
- 3 处 `INSERT INTO knowledge_nodes` 列清单全部不含被删字段

### 端到端 Docker 验证 · ✅

2026-05-27 在 `make dev` 栈上（postgres_dev 命名卷，含 292 历史节点）执行完整迁移。

**前置坑**：原 deploy 栈用 `./data/postgres` bind mount，实际数据在 `postgres_dev` 命名卷里（TODO.md 已记录的 dev/deploy 分裂）。先 `docker compose -f docker-compose.yml -f docker-compose.dev.yml --profile workers up -d` 切回 dev 才能看到真实数据。

**修复**：发现 `entity_candidates.mention_count / max_salience` 没被回填（Phase D 的 backfill UPDATE 被 Phase A 第三批的注释覆盖了）。补救：
- `database.py init()` 在 `if has_mentions:` 块里加 mention_count/max_salience UPDATE（DROP COLUMN 之前）
- 加兜底 UPDATE：若 `mention_count=0` 但 `source_article_ids` 非空，用 `cardinality(source_article_ids)` 补 count，`max_salience=0.5` 默认值

**Schema 验证（dev DB, 292 节点 / 326 候选 / 1363 边）**

| 检查项 | 结果 |
|---|---|
| 9 个对象专属字段 + is_primary + 3 个 app 字段 | DROP 0 行（全删除）|
| `knowledge_nodes` 当前列数 | 21 列（21=正确精简后规模）|
| `entity_candidates` 当前列 | mention_count / max_salience / source_article_ids 全在；mentions JSONB 消失 |
| `entity_profiles` 表 | DROP 完成（pg_tables 0 行）|
| `uq_edges_from_to_type` UNIQUE 约束 | 存在 |
| `summarizes` 边数 | 0 |
| 数据保留 | 全 292 节点 / 92 article_nodes / 94 summary_nodes / 104 entity_nodes / 326 candidates 完整 |
| candidates 计数器回填 | 326/326 with mention_count > 0；max_count=8，max_salience=0.5（fallback）|

**端点验证（dev mode, /api/kb/v1/）**

| 端点 | 结果 |
|---|---|
| `GET /openapi.json` | 8 端点全部可见 |
| `GET /search` | 中文 query 返回 entity 节点 + score + why_matched=vector |
| `GET /nodes/{id}` | 节点详情 + summaries[] + outline[] + doc_kind |
| `POST /nodes/batch` | 批量返回 |
| `GET /nodes/{id}/related` (mentioned_by) | 实际 article 列表 + weight |
| `GET /timeline?entity_id=...` | 按时间排序的文章列表 |
| `POST /api/kb/entities/{id}/regenerate` | 成功更新 `nodes.abstract`（确认 entity_profiles 删除后 regenerate 走新路径）|
| `POST /api/briefing/generate` | sources.is_primary JOIN 路径不报错 |

**未端到端验证**（需要真实 LLM 调用 + ingestion 完整链）
- `POST /compare / /cite / /summarize_corpus` —— 接口存在、参数验证通过；实际 LLM 行为留待真实使用时检验
- doc_kind 继承链 / embedding_model 标记 / tag 收敛 —— 新文章入库后才能填充，需要 ingestion-worker 重启后验证。当前 292 节点这两字段均为 NULL（预期；未做历史 backfill）

### Phase E · UI 更新 · ✅

**已完成（设计文档 §8 的可落地子集）**

| 项 | 实现 |
|---|---|
| 图谱默认隐藏 summary 节点 | `knowledge/page.tsx` visibleNodeTypes 初值改为 `{article, entity, index}` |
| 图谱默认隐藏 similar_to / summarizes 边 | visibleEdgeTypes 初值改为 `{mentions, part_of, contains}` |
| `GET /api/config/doc_kind` 端点 | `main.py` 新增，UI 下拉的枚举源 |
| Source 创建表单加 `default_doc_kind` 下拉 | `sources/page.tsx` AddForm + 后端 `SourceCreate.default_doc_kind` |
| Source 卡片显示当前默认类型 | SourceCard 底部 meta 栏新增 |
| 上传文件 dialog 加 doc_kind 下拉（必填） | UploadModal + 后端 `/upload` 接 `Form doc_kind` |
| 添加 URL dialog 加 doc_kind 下拉（默认 `news`） | AddUrlModal + 后端 `/add-url` body 接 `doc_kind` |
| 共享 `DocKindSelect` 组件 + `useDocKindConfig` hook | 在 `sources/page.tsx` 内定义，不依赖任何外部组件库 |
| 后端 `_validate_doc_kind` 校验 | 非法值 400 + 列出可选枚举 |
| 中文标签映射 | `DOC_KIND_LABELS` 把 `regulation→法规 / 规章` 等显示给用户 |

**后端配合改动**

- `routers/sources.py`：`SourceCreate / SourceUpdate / SourceItemCreate` 加 `default_doc_kind / doc_kind`；`/upload` 接 Form `doc_kind`；`/add-url` 接 body `doc_kind`；`update_source` 支持 PATCH `default_doc_kind`
- `_create_source_item` INSERT 列表加 `doc_kind`
- 顶部 `import config_loader`
- `main.py`：`GET /api/config/doc_kind` 返回 `{values, default}`

**Docker 端到端验证（dev 模式）**

| 验证 | 结果 |
|---|---|
| `GET /api/config/doc_kind` | 返回 7 枚举值 + default=other |
| `POST /api/sources` 携带 `default_doc_kind` | 写入成功 |
| `POST /api/sources` 携带 `default_doc_kind=bogus` | 400 + 列出合法值 |
| `POST /api/sources/{id}/add-url` 携带 `doc_kind` | `source_items.doc_kind` 写入 case |
| `GET /api/sources` 返回字段 | 含 `default_doc_kind` |
| TypeScript 编译 | `npx tsc --noEmit` 干净 |
| Next.js 构建 chunk | `app/sources/page.js` 含 `DocKindSelect / default_doc_kind / 默认类型`；`app/knowledge/page.js` 含新默认 `new Set(["article","entity","index"])` |

**本批延后项**（依赖未实现的后端 API；设计文档列出但需独立专批）

- **资源管理器按 source 分组**：当前 ListPanel 是平铺；按 source 分组 + 切换排序需 API 支持（`/api/kb/nodes?group_by=source`），属 medium-large
- **Source 软删除 UI**：需要后端 `deleted_at` 字段已加（Phase A 第一批）但 DELETE 端点目前仍硬删；需要新增 `POST /api/sources/{id}/deactivate`（含可选 cascade 选项）
- **Entity merge / 硬删除 UI**：设计 §7 列出 `POST /entities/merge` 与 `DELETE /entities/{id}`，目前两个后端端点均不存在
- **节点元数据编辑（doc_kind 下拉）**：需要 `PATCH /api/kb/nodes/{id}/metadata`（设计 §7 列出但未实现）
- **搜索面板默认隐藏 + Cmd/Ctrl+F 唤起**：当前右侧 ListPanel 始终显示；唤起改造需要快捷键 + 容器折叠

这 5 项每项都是独立工程量（后端 API 新建 + 前端组件改造），各自适合作为后续单独的批次。Phase E 把"无需新后端"的部分全部落地。

---

### 审计批次 2026-05-28（P1–P5 + Pyright 清零）

依据 `docs/audit-2026-05-28.md` 的发现，在 Phase A–E 基础上执行以下修复与补充。

#### P1 · 阻断性 Bug 修复 · ✅

- **Bug-1 `config_loader.get()` 返回类型**：`config_loader.py:94` 加 `-> Any` 注解，消除所有调用处的 Pyright `Unknown` 传播错误
- **Bug-2 `content[0].text` ToolUseBlock 守卫**：Claude API 返回 `TextBlock | ToolUseBlock` union，只有 `TextBlock` 有 `.text`。统一改为 `getattr(resp.content[0], "text", "")`，覆盖 8 处（`kb_tools.py`、`kb/retrieval.py`、`maintenance.py` ×2、`briefing.py`、`kb/public.py` ×3）
- **Bug-3 `canonical_name` 残留**：`kb_tools.py` `_reference()` 中 `node.get("canonical_name")` 已删除（字段已迁至 `entity_nodes`）；entity 节点标题不再静默 fallback 为 id
- **`entity_insights.py` LIMIT 参数外置**：`refresh_entity_profile` 中 `LIMIT 12` 改为 `:facts_limit` 绑定参数，值由 `config_loader.get("entity_insights.refresh_facts_limit", 12)` 读取

#### P2 · §7 后端 API 补全 + 配置外置 · ✅

**§7 缺失端点全部实现**（均位于 `kb/internal.py`）：

| 端点 | 功能 |
|---|---|
| `DELETE /summaries/{summary_id}` | 删除摘要 |
| `PATCH  /entities/{entity_id}` | 修改 canonical_name / aliases / entity_type |
| `POST   /entities/merge` | 合并两个 entity（source → target，级联重定向 edges） |
| `DELETE /entities/{entity_id}` | 硬删除 entity（级联删 edges / entity_facts） |
| `POST   /nodes/{node_id}/archive` | 软删除（status = archived） |
| `PATCH  /nodes/{node_id}/metadata` | 修改 title / tags / published_at / doc_kind |

**配置外置**：
- `app/drafts.py`：上下文组装阈值 `remaining <= 100` → `config_loader.get("drafts.min_remaining_chars", 100)`（`config/system.yaml` 新增 `drafts.min_remaining_chars: 100`）
- `entity_insights.py`：`LIMIT 12` → `config_loader.get("entity_insights.refresh_facts_limit", 12)`（`config/system.yaml` 新增 `entity_insights.refresh_facts_limit: 12`）
- `config/system.yaml` 新增 entity 晋升阈值配置：`entity.promotion_max_salience`（0.9）/ `entity.promotion_salience`（0.7）/ `entity.promotion_salience_mentions`（2）/ `entity.promotion_min_mentions`（3）

#### P3 · Phase E 延后项（4/5 落地） · ✅

| 项 | 状态 | 实现方式 |
|---|---|---|
| 资源管理器按 source 分组 | ✅ | `knowledge/page.tsx` 新增 `groupBySrc` toggle，按 `source_name` 分组渲染 |
| Entity merge UI | ✅ | WikiPanel 内 mergeOpen 状态 + 输入框，调用 `POST /api/kb/entities/merge` |
| 节点 doc_kind 内联编辑 | ✅ | 详情面板 doc_kind 行加编辑态，调用 `PATCH /api/kb/nodes/{id}/metadata` |
| 搜索面板默认隐藏 + 快捷键 | ✅ | `showList` 初值 `false`；Cmd/Ctrl+F 唤起；Esc 收起 |
| Source 软删除 UI + cascade 选项 | ⚠️ 部分 | 后端 `DELETE /api/sources/{id}` 已实现软删除（`deleted_at = NOW()`）；前端有确认对话框（"已入库文章继续保留"）；**"同时删除 N 篇文章"的 cascade 选项未实现** |

#### P4 · 代码清理 · ✅

- **`database.py` 冗余 ALTER TABLE**：移除 8 条冗余 `ALTER TABLE IF EXISTS ... ADD COLUMN IF NOT EXISTS`（`knowledge_nodes` 的 object_type / updated_at / abstract / ingested_at / published_at；`entity_candidates` 的 mention_count / max_salience / source_article_ids），这些列已在 `CREATE TABLE` 中定义。保留 6 条真正的 migration alter（embedding_model / doc_kind / sources.default_doc_kind / sources.deleted_at / source_items.doc_kind / knowledge_edges.description）
- **`entity_profiles` 注释**：`maintenance.py:~1229` 的 `# entity_profiles 表已删除` 注释是解释性上下文（说明后续调用为何是 rebuild_entity_pair_signals），保留
- **COALESCE 链一致性**：`kb_tools.py` 与 `kb/public.py` 的 `COALESCE(s.body_embedding, n.embedding)` / `COALESCE(s.perspective_embedding, s.body_embedding, n.embedding)` 链完全一致，无需修改

#### P5 · 测试补充 · ✅

新增 `services/api/tests/test_p5_coverage.py`（23 个测试，4 个测试类）：

| 测试类 | 覆盖内容 | 策略 |
|---|---|---|
| `CitationPromptBodyTests` (5) | `_citation_prompt_body` 的 excerpt 选取逻辑（边界长度、无匹配词截断、有匹配词抽段、marker 格式、context 词驱动） | AST 提取纯函数 + `exec`（绕开 prompt_loader 文件依赖） |
| `CiteQuoteVerificationTests` (3) | quote 逐字校验存在、全文用于验证（非 excerpt）、Stage 1→2→验证顺序 | 源码文本检查 |
| `EntityPromotionThresholdTests` (8) | 各 salience/mention_count 阈值边界（0.9 / 0.7+2 / 3条） | 纯逻辑镜像（固化 config 默认值） |
| `DocKindCascadeTests` (3) | explicit → source_item → source.default → config.default 优先级顺序、非法值降级 | 源码文本位置检查 |
| `SummaryFirstFallbackTests` (4) | summary_nodes 先于 wiki fallback、else 分支触发条件、非空 body 过滤、is_default 排序 | 源码文本检查 |

#### Pyright · 58 → 0 错误 · ✅

| 类型 | 受影响文件 | 修复 |
|---|---|---|
| ToolUseBlock `.text` 崩溃风险 | `briefing.py`、`kb/public.py` ×3 | `content[0].text` → `getattr(..., "text", "")` |
| `repair_json` 返回类型未收窄 | `briefing.py` | 增加 `if not isinstance(parsed, list): return []` |
| Pydantic v1 `.model_dump()` 不存在 | `settings.py`、`kb/internal.py` | `.model_dump()` → `.dict()` |
| `int(None)` 类型收窄不足 | `kb/public.py` ×3 | `int(x)` → `int(x or 0)` |
| `_embed_query(str\|None)` 类型收窄不足 | `kb/public.py` ×2 | 增加 `assert` |
| `dict(row)` 无 None 检查 | `routers/sources.py` ×3 | `assert row is not None` + `dict[str, Any]` 注解 |
| `params dict` 类型过窄 | `maintenance.py` | 显式标注 `dict[str, Any]`；补 `from typing import Any` |
| 测试文件 AST / 模块属性误报 | 两个测试文件 | `str()`、`# type: ignore[attr-defined]`、`list[Any]`、`assert not None` |

---

## 与第一、二部分设计决议的差异说明

以下记录当前实现与 §1–8 设计决议的差异，分为**已知遗留**和**需要修复**两类。

### 已知遗留（有意延后，需独立批次）

**§7 路径命名偏差（KB Internal 仅供内部使用，优先级低）**

设计决议 §7 规定的两个路径与实际实现不同：

| §7 规定路径 | 实际路径 |
|---|---|
| `POST /api/kb/nodes/{id}/summaries` | `POST /api/kb/nodes/{node_id}/create_summary` |
| `POST /api/kb/summaries/{id}/revise` | `POST /api/kb/nodes/{node_id}/revise_summary` |

这两个端点属于 KB Internal（前端直接调用），MCP adapter 不依赖。功能可用，路径命名与 §7 规范不符。后续可在不破坏现有前端调用的前提下增加路径别名或改名。

**§8 Source 删除 cascade 选项未实现**

设计决议 §8 描述："Source 删除时提供选项：仅停用来源 / 同时删除 N 篇文章"。

当前实现：
- 后端 `DELETE /api/sources/{id}` 为软删除（`deleted_at = NOW()`），articles 保留 ✅
- 前端有确认对话框，说明"已入库文章继续保留" ✅
- **缺失**："同时删除 N 篇文章"的 cascade 选项（需要后端接受 `cascade_articles: bool` 参数并级联软删或硬删 article nodes）

**§8 已停用 source 的"[已停用]"分组标注未实现**

设计决议 §8 描述："Source 删除：软删除（`deleted_at`），articles 保留，分组标注"[已停用]""。

当前 sources/page.tsx 的 `loadSources()` 调用 `GET /api/sources`，该接口加了 `AND deleted_at IS NULL` 过滤，软删除后的 source 不出现在列表中。§8 要求在资源管理器中以"[已停用]"分组继续显示，目前未实现。

### 需要修复（功能正确性问题）

**Bug-3 部分修复：entity 节点在 kb_tools.py 搜索结果中仍无 canonical_name**

`kb_tools.py` 的 `_reference()` 已删 `node.get("canonical_name")`。但 `kb_tools.py` 的主搜索 SQL 中没有 `LEFT JOIN entity_nodes`，entity 节点返回的 `title` 是 `knowledge_nodes.title`（可能为 NULL 或不同于 `entity_nodes.canonical_name`）。

影响：entity 搜索结果的展示名称可能不准确。

修复方向：在 `kb_tools.py` 的搜索 SQL 中加 `LEFT JOIN entity_nodes en ON en.node_id = n.id`，SELECT 加 `COALESCE(en.canonical_name, n.title) AS title`（或 `display_title`）。

**`kb_tools.py` 的 HyDE 路径中 content[0].text 守卫**

审计 Bug-2 修复了 `briefing.py`、`kb/public.py`、`maintenance.py` 的 `.text` 访问。`kb_tools.py` 的 HyDE embed 路径（`kb/retrieval.py` 第 ~33 行）已在 P1 修复，但如果 `kb_tools.py` 有自己调用 `claude_client.messages.create` 并读 `content[0].text` 的路径，需再次确认已全部覆盖。当前 Pyright 清零说明无遗漏，但运行时覆盖仍依赖 Claude API 总是返回 TextBlock（对话类调用通常满足此条件）。
