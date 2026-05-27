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
