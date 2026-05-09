# KnowledgeBase-S 重构计划

> 本文记录当前系统从“个人知识库 + 每日简报 + 写作助手”一体化实现，重构为“清晰的知识库核心 + 上层应用”的设计方案。
>
> 目标不是一次性推翻现有系统，而是通过可验证的小步骤，把领域边界、数据模型、一致性规则和后台任务模型逐步理顺。

---

## 1. 重构目标

当前系统已经可以完成资料导入、知识沉淀、每日选题、草稿生成和反馈学习，但代码和数据模型逐渐承担过多职责。主要问题包括：

- Knowledge Core 与 briefing / drafts / chat 等上层应用耦合过深。
- `knowledge_nodes` 同时承载 article、entity、summary、index 的通用字段和对象专属字段。
- source、raw 文件、URL 队列、rebuild 之间缺少统一的 ingestion manifest。
- wiki 文件和 DB 双写，但缺少明确的一致性模型。
- LLM / embedding 调用散落在 API、worker、maintenance 中，缺少统一队列、状态、重试和成本记录。
- 当前 maintenance 仍保留 LLM 语义边，与目标图谱关系体系不一致。

重构后的目标：

1. **核心知识库与上层应用分离**
   - Knowledge Core 负责 source item、article、index、summary、entity、edge、embedding、检索、rebuild、restore。
   - Briefing、drafts、chat、feedback memory 作为应用层，通过稳定接口访问 Knowledge Core。

2. **模块划分清晰**
   - 路由只做 HTTP 输入输出。
   - 业务逻辑进入 service 层。
   - SQL 和存储细节进入 repository 层。
   - LLM / embedding 调用进入 job 或 provider 层。

3. **数据一致性清晰**
   - DB 是事实源。
   - wiki 不再作为任意编辑入口。
   - 用户通过结构化 API 修改标题、标签、时间、index 结构、summary 内容等。

4. **支持海量文档**
   - 增加 `source_items` 或等价 raw manifest。
   - ingestion 可追踪、可重试、可重建。
   - 检索字段、时间字段、边关系具备明确约束和索引。

5. **后台任务可管理**
   - Claude / OpenAI 调用逐步进入 job 队列。
   - 支持限流、重试、失败恢复、进度展示和成本统计。

---

## 2. 推荐目标架构

短期不建议先拆成多个容器或微服务。当前更需要先在代码层建立边界。

建议目录结构逐步演进为：

```text
services/api/
  core/
    config.py
    db.py
    llm.py
    jobs.py

  kb/
    models.py
    repository.py
    ingestion.py
    retrieval.py
    graph.py
    wiki.py
    maintenance.py
    rebuild.py
    routers.py

  sources/
    models.py
    repository.py
    service.py
    routers.py

  apps/
    briefing/
      service.py
      routers.py
    drafts/
      service.py
      routers.py
    chat/
      service.py
      tools.py
      routers.py
    feedback_memory/
      service.py
      routers.py
```

边界原则：

- `kb/*` 不依赖 briefing、draft、chat。
- apps 可以调用 `kb.service` / `kb.retrieval` / `kb.tools`。
- routers 不直接承载复杂业务逻辑。
- worker 消费 source item 或 job，不直接拼接过多业务状态。

---

## 3. 核心领域模型

### 3.1 Source 与 Source Item

当前 source 的 `config.uploads`、`pending_urls`、`pending_items` 承担了队列和历史记录职责，不适合海量数据。

建议新增：

```text
source_items
  id
  user_id
  source_id
  source_type
  uri
  raw_path
  extracted_text_path
  content_hash
  title
  source_published_at
  source_updated_at
  captured_at
  raw_retention_policy
  status
  error
  attempts
  created_at
  updated_at
```

`source_items` 是 ingestion 和 rebuild 的统一 manifest。

所有输入渠道都应先落 item：

- RSS item
- URL 队列
- wechat2rss feed item
- 文件上传
- 图片
- PDF
- Word
- EPUB / book

可验证标准：

- 新增 URL 不再写入 `sources.config.pending_urls`，而是创建 `source_items`。
- 上传文件后可以在 `source_items` 中看到每个文件独立状态。
- ingestion-worker 可以只消费 `source_items.status = pending` 的记录。

#### Raw Data 与 Extracted Text

系统中应区分三层内容：

```text
raw binary/file/url snapshot
  -> extracted_text
    -> article node
```

其中：

- `raw` 是原始证据和可重跑 OCR / PDF cleanup / parser 的依据。
- `extracted_text` 是从 raw 得到的规范文本快照，可作为 article 正文的主要事实源。
- `article` 是进入知识图谱和检索系统的结构化对象，包含 abstract、metadata、embedding 等派生字段。

长期不建议完全删除 raw。原因：

- OCR、PDF 解析、网页抽取算法未来会改进，没有 raw 就无法高质量重跑。
- 用户可能需要追溯原始来源。
- 某些 metadata 只能从 raw 或原始 HTML/PDF 中重新提取。

但考虑海量文档和存储成本，可以引入 retention policy：

```text
keep_raw              # 保留原始文件，适合重要 PDF / image / book
keep_extracted_only   # 仅保留 extracted_text，适合低价值网页/RSS
external_archive      # raw 移到对象存储或冷存储，只保留引用
discard_after_ingest  # 入库后删除 raw，仅保留 extracted_text 和 article
```

默认建议：

- PDF / image / EPUB：默认 `keep_raw`。
- RSS / URL：默认 `keep_extracted_only` 或 `external_archive`。
- 用户可按 source 配置覆盖。

可验证标准：

- 每个 source item 都记录 raw 与 extracted_text 的路径或外部引用。
- 即使 raw 被清理，article 仍能通过 extracted_text 重建主要内容。
- 需要重新 OCR / 重新解析时，只有保留 raw 的 item 才可完整重跑。

### 3.2 Article

Article 是原始素材的结构化表示，应该是不可变的 factual unit。

```text
raw/source_item -> article
                  immutable factual unit
```

建议规则：

- article 正文不允许在 wiki 中直接编辑。
- 如需修正，应通过结构化操作完成：
  - 重新清洗 raw
  - 重新分析 article
  - 修改 title / tags / time / status
  - 标记为 ignored / archived
  - 添加用户注释
- article 的 embedding 和 entity mentions 是派生结果，应由系统生成。

原因：

- 直接编辑 article 正文会导致 abstract、embedding、summary、entity mentions、raw_ref 失真。
- 海量文档系统中，article 必须可追溯、可重建。

### 3.3 Summary

Summary 是解释层和视角层，应该允许用户编辑。

更精确地说，summary 是对某个 object 的一次“观察”。同一个 object 可以被多个 summary 观察，每个 summary 代表一个明确视角、问题意识或用途。

```text
article/index -> summary
                 editable interpretation layer
```

建议字段：

```text
summary_nodes
  node_id
  summary_of
  perspective
  instruction
  body
  source: llm | manual | edited
  edited_at
```

设计原则：

- summary 不是 article 的附属缓存，而是一等的观察对象。
- summary 必须指向一个被观察对象：article 或 index。
- 同一个 article / index 可以有多个 summary。
- summary 的 `perspective` 表达观察角度，例如“商业模式”“技术架构”“人物关系”“政策影响”。
- summary 的 `instruction` 记录它当初为何、如何被生成，便于重放和解释。
- summary 可以作为搜索的第一入口，因为它通常比 article 正文更短、更聚焦、更适合 embedding。

规则：

- summary 可以由系统生成。
- summary 可以按用户指令再生成。
- summary 不开放直接编辑正文或文件编辑。
- 用户修改 summary 必须通过 revise instruction 表达意图。
- revise 完成后必须重新计算 embedding。
- summary 的正文事实源应在 DB，不应只是 wiki 文件。

#### Summary 与检索展开

未来检索应优先把 summary 作为“可观察层”：

```text
query
  -> summary hits
    -> decide whether to expand to observed object
```

当 summary 指向 article 时，展开逻辑相对直接：

```text
summary -> article
```

但当 summary 指向 index 时，不应简单展开为 index 下所有 article。Index 是 collection，直接展开全部子节点会重新引入“大对象淹没小对象”的问题。

更合理的 progressive disclosure 流程是：

```text
summary -> index
          -> child summaries
             -> selected child article/index
```

也就是说：

- 命中 index summary 后，先确认该 index 是否整体相关。
- 如果需要深入，优先检索或选择该 index 的 child summaries。
- 只有当某个 child summary 仍然相关，才进一步展开到具体 article 或 child index。
- 是否展开、展开到哪一层，可以由检索分数、预算、规则或 LLM 判断共同决定。

这与当前计划不冲突，但会影响 retrieval 的长期设计：summary 不是简单跳板，而是“从不同视角观察对象”的中间层。检索算法应围绕 summary-first、按需展开、预算受控来设计。

典型 API：

```text
POST /api/kb/nodes/{node_id}/summaries
{
  "perspective": "商业模式",
  "instruction": "重点分析这篇文章对 AI Agent 产品定价的启发"
}
```

```text
POST /api/kb/summaries/{summary_id}/revise
{
  "instruction": "压缩到 150 字，保留关键判断"
}
```

不建议提供：

```text
PATCH /api/kb/summaries/{summary_id}
{
  "body": "用户直接覆盖后的正文"
}
```

原因：

- 直接编辑正文会让系统无法知道用户想改的是事实错误、表达风格、长度还是视角。
- revise instruction 可以留下可审计的修改意图，未来可用于学习用户偏好。
- 对 summary 的修改也可以进入 job 队列，避免长时间阻塞 UI。

### 3.4 Entity

Entity 是派生图层对象。

```text
article/summary -> entity mentions
                   derived graph layer
```

建议规则：

- `canonical_name` 和 `aliases` 可以编辑。
- 重复 entity 应提供合并操作。
- entity 正文不建议通过 wiki 自由编辑，应通过：
  - 重新生成
  - 增量补充
  - 用户注释
  - 合并 entity
  来维护。

Entity 页目前是 LLM 生成的解释性页面，长期需要区分：

- 系统聚合说明
- 用户补充说明
- 来源 article 列表

这里的含义是：

- `aliases`：用户可以维护别名，例如“OpenAI”与“开放人工智能公司”。
- `merge`：如果系统生成了两个重复 entity，用户可以把 B 合并进 A，迁移 mentions/source links 后删除或归档 B。
- `regenerate`：根据当前关联 articles 重新生成 entity 描述，而不是手工改 wiki。
- `user note`：用户可以给 entity 添加独立备注，但备注不覆盖系统生成正文。

短期决策：

- 不开放 entity 正文自由编辑。
- 开放 aliases、merge、regenerate、user note。
- Entity 的可解释正文仍由系统根据来源聚合生成。

### 3.5 Index

Index 必须单独考虑。它不是 article，也不是 summary，而是结构层对象。

Index 应拆成两部分语义：

```text
Index = 结构本体 + 派生说明
```

#### 可编辑部分

Index 的结构本体可以编辑：

- title
- description
- rollup_instruction
- children
- child order
- parent / nesting

原因：

- Index 表示书、章节集合、专题集、手动 collection。
- 用户应该能控制一个 collection 里有哪些 article / index，以及它们的顺序。

建议 API：

```text
POST   /api/kb/indices
PATCH  /api/kb/indices/{id}
POST   /api/kb/indices/{id}/children
DELETE /api/kb/indices/{id}/children/{child_id}
PATCH  /api/kb/indices/{id}/children/order
```

#### 不直接编辑部分

Index 的 `abstract` / embedding 不建议手工编辑。

它应是系统从 children 聚合出的派生字段：

```text
children 改变
  -> index abstract stale
  -> rollup job
  -> 更新 abstract
  -> 更新 embedding
```

建议字段：

```text
index_nodes
  node_id
  title
  description          # 用户可编辑
  rollup_instruction   # 用户可编辑
  abstract             # 系统生成，用于 embedding
  abstract_stale       # children 或 instruction 改变后置 true
```

Index 也可以拥有 summary：

```text
Index("AI Agent 商业化")
  ├── article(...)
  ├── article(...)
  └── summary("从投资角度看 AI Agent 商业化")
```

总结：

```text
index -> contains article/index
         editable structure, system-generated rollup
```

### 3.6 knowledge_nodes 是否保留

决策：现在就重新设计为 object-specific tables，而不是把 `knowledge_nodes` 单表作为长期主模型。

仍建议保留 `knowledge_nodes`，但它只作为轻量图节点注册表和通用检索字段表，不再承载对象专属业务字段。

目标结构：

```text
knowledge_nodes
  id
  user_id
  object_type
  title
  abstract
  embedding
  priority_score
  created_at
  updated_at

article_nodes
  node_id
  source_item_id
  raw_ref
  source_type
  source_published_at
  effective_at
  tags
  status

entity_nodes
  node_id
  canonical_name
  aliases

summary_nodes
  node_id
  summary_of
  perspective
  instruction
  body
  source

index_nodes
  node_id
  description
  rollup_instruction
  abstract_stale
```

实现策略：

1. 先新增 object-specific tables，不立即删除 `knowledge_nodes` 中的旧字段。
2. 写迁移脚本，把现有 `knowledge_nodes` 数据回填到 `article_nodes`、`summary_nodes`、`entity_nodes`、`index_nodes`。
3. 新写入路径同时写 `knowledge_nodes` 和对应 object table。
4. 读路径逐步从 object table 获取专属字段。
5. 等所有读写路径迁移后，再清理 `knowledge_nodes` 中的专属字段。

可验证标准：

- 每个 node 在 `knowledge_nodes` 中有一条注册记录。
- 每个 object_type 在对应 object table 中有一条专属记录。
- 新增 article/summary/entity/index 不再只依赖 `knowledge_nodes` 的混合字段。
- API 层不再需要根据 object_type 解释大量 nullable 字段。

---

## 4. 时间 Metadata

当前 `created_at` 更像入库时间，不应被当作素材实际时间。

建议新增：

```text
ingested_at            # 系统入库时间
source_published_at    # 原始发布/发表时间
source_updated_at      # 原始更新时间
captured_at            # 用户保存/上传/推送时间
effective_at           # 内容描述的现实生效时间，可选
```

默认时间选择：

```text
knowledge_time = effective_at
              ?? source_published_at
              ?? captured_at
              ?? ingested_at
```

应用方式：

- Briefing 默认按 `knowledge_time` 或 `source_published_at` 窗口选择 article。
- 检索支持时间过滤。
- Chat tool 支持“最近一个月”“某个时期”的知识查询。
- Rebuild 不改变原始 source time。

可验证标准：

- RSS article 使用 feed published/updated 时间。
- URL article 可以从网页 metadata 获取时间，获取不到则使用 captured_at。
- 文件上传支持用户可选输入时间。
- Briefing 不再只依赖 `knowledge_nodes.created_at`。

---

## 5. Wiki 一致性模型

建议从“wiki 可编辑副本”改为：

```text
DB 为事实源
wiki 为 read-only export / Obsidian view / debug artifact
```

规则：

- 不再允许任意编辑 wiki 文件。
- Article wiki 只读。
- Index wiki 只读，由 DB 中的结构和 rollup 生成。
- Summary 可以编辑，但通过 summary API 编辑 DB，再导出到 wiki。
- Config / templates 可以继续作为文件编辑，或逐步迁入 DB。

需要明确：

- wiki 可以被删除后重建。
- wiki 不参与实时事实写入。
- `restore_from_wiki()` 可以作为灾难恢复工具，但不是日常同步机制。

可验证标准：

- 前端 `/knowledge` 不再提供 article wiki 任意编辑。
- 修改 summary 走 `/api/kb/summaries/{id}`。
- 重建 wiki 后 article / index / summary 文件与 DB 状态一致。

---

## 6. 图谱关系体系

目标关系类型应尽量来源明确、可解释、可重建。

建议保留：

```text
index         -> article    index_children / contains
index         -> index      index_children / contains
summary       -> article    summarizes
summary       -> index      summarizes
article       -> entity     mentions
summary       -> entity     mentions
entity        -> entity     similar_to
article       -> article    similar_to
entity        -> entity     co_occurs_with
```

建议移除：

```text
extends
background_of
supports
contradicts
```

原因：

- 它们依赖 LLM 语义判断。
- 难以跨领域稳定解释。
- 难以从 raw/source item 稳定重建。
- 容易污染检索和图谱理解。

Index 结构建议：

- 决策：引入 `index_children`，不再依赖 `knowledge_edges.part_of` 表达 index 层级结构。
- `knowledge_edges.part_of` 可以保留为 legacy 或只作为从 `index_children` 派生出的图谱展示边。
- 顺序、父子关系和 collection 语义以 `index_children` 为事实源。

推荐表：

```text
index_children
  index_id
  child_id
  position
  child_role
  created_at
```

Edge 约束：

```text
UNIQUE(from_node_id, to_node_id, relation_type)
```

可验证标准：

- maintenance 不再调用 LLM 判断 article 与 article 的语义边。
- DB 中新生成边只包含目标关系类型。
- 前端图谱不再默认展示 legacy LLM 语义边。

---

## 7. Knowledge Core API

重构后的核心知识库需要一组简洁、有表达力、稳定的 API。上层应用不应直接理解底层表结构，而应通过这些接口访问知识库。

这里的 API 可以同时对应 HTTP route 和 Python service 方法。HTTP 形态用于前端和外部集成，service 形态用于 briefing / drafts / chat 等内部应用。

### 7.1 设计原则

- API 围绕领域对象，而不是数据库表。
- 写操作必须表达意图，而不是暴露“编辑 markdown 文件”这类实现细节。
- 检索 API 返回可解释结果，包括命中的 object、score、match_reason、可选扩展路径。
- 上层应用只调用 Knowledge Core API，不直接拼接 `knowledge_nodes` / `knowledge_edges` SQL。
- 长任务返回 job id，实时任务返回结果。

### 7.2 Source Item / Ingestion API

```text
POST   /api/kb/source-items
GET    /api/kb/source-items
GET    /api/kb/source-items/{id}
POST   /api/kb/source-items/{id}/ingest
POST   /api/kb/source-items/{id}/retry
PATCH  /api/kb/source-items/{id}
```

表达能力：

- URL、RSS item、文件上传、wechat2rss item 都可以统一进入 source item。
- ingestion 状态可见。
- 失败可重试。
- rebuild 可以以 source item 为依据。

### 7.3 Object API

```text
GET    /api/kb/objects/{id}
GET    /api/kb/objects
PATCH  /api/kb/objects/{id}/metadata
POST   /api/kb/objects/{id}/reprocess
POST   /api/kb/objects/{id}/archive
```

说明：

- object 包括 article、summary、entity、index。
- metadata 修改包括 title、tags、time、status 等结构化字段。
- article 正文不通过此 API 直接编辑。
- `reprocess` 用于重新清洗、重新分析、重新生成派生字段。

### 7.4 Summary API

```text
GET    /api/kb/objects/{id}/summaries
POST   /api/kb/objects/{id}/summaries
GET    /api/kb/summaries/{summary_id}
POST   /api/kb/summaries/{summary_id}/revise
DELETE /api/kb/summaries/{summary_id}
```

表达能力：

- 为 article 或 index 创建指定 perspective 的 summary。
- 按指令 revise summary。
- revise 后自动重算 embedding。
- 不提供直接覆盖 summary body 的编辑接口。

### 7.5 Index API

```text
POST   /api/kb/indices
GET    /api/kb/indices/{id}
PATCH  /api/kb/indices/{id}
GET    /api/kb/indices/{id}/children
POST   /api/kb/indices/{id}/children
DELETE /api/kb/indices/{id}/children/{child_id}
PATCH  /api/kb/indices/{id}/children/order
POST   /api/kb/indices/{id}/rollup
```

表达能力：

- 创建专题 collection。
- 修改 index title / description / rollup_instruction。
- 管理 children 和顺序。
- children 或 instruction 改变后触发 rollup。
- rollup 更新 index abstract 和 embedding。

### 7.6 Entity API

```text
GET    /api/kb/entities
GET    /api/kb/entities/{id}
PATCH  /api/kb/entities/{id}
POST   /api/kb/entities/{id}/regenerate
POST   /api/kb/entities/merge
GET    /api/kb/entity-candidates
POST   /api/kb/entity-candidates/{id}/promote
```

表达能力：

- 修改 canonical_name 和 aliases。
- 合并重复 entity。
- 晋升候选 entity。
- 重新生成 entity 描述。

### 7.7 Search / Retrieval API

```text
POST /api/kb/search
POST /api/kb/retrieve
GET  /api/kb/objects/{id}/neighbors
GET  /api/kb/objects/{id}/sources
```

`search` 偏基础搜索：

```json
{
  "query": "AI Agent 商业化",
  "object_types": ["summary", "article", "index", "entity"],
  "time": {
    "from": "2026-01-01",
    "to": "2026-05-01",
    "field": "knowledge_time"
  },
  "tags": ["AI"],
  "limit": 20
}
```

`retrieve` 偏应用上下文装配：

```json
{
  "query": "写一篇关于 AI Agent 商业化瓶颈的文章",
  "mode": "summary_first",
  "expand_policy": {
    "summary_to_article": "when_needed",
    "summary_to_index": "via_child_summaries",
    "max_depth": 3
  },
  "budget": {
    "max_chars": 8000
  }
}
```

返回结果应包含：

```text
hits
context_items
expansion_trace
citations
```

这样 briefing、drafts、chat 都可以复用同一个 retrieval 能力，而不是各自实现一套检索逻辑。

### 7.8 Graph / Wiki / Export API

```text
GET  /api/kb/graph
GET  /api/kb/graph/all
POST /api/kb/wiki/rebuild
GET  /api/kb/wiki/export
```

说明：

- graph API 面向可视化和调试。
- wiki 是 export，不是编辑入口。
- 未来可增加按 index / tag / time 范围导出。

### 7.9 Job API

```text
GET    /api/kb/jobs
GET    /api/kb/jobs/{id}
POST   /api/kb/jobs/{id}/cancel
POST   /api/kb/jobs/{id}/retry
```

说明：

- 所有后台 LLM / embedding / rebuild / rollup 状态都应能被查询。
- 前端可以显示失败原因和重试按钮。

---

## 8. LLM / OpenAI API Job 队列

需要引入命令队列，尤其用于后台 ingestion、OCR、summary、embedding、maintenance、rebuild。

短期不需要 RabbitMQ / Redis。建议先用 Postgres job table。

```text
jobs
  id
  user_id
  job_type
  provider
  model
  payload
  status              # pending | running | succeeded | failed | retrying | cancelled
  priority
  idempotency_key
  attempts
  max_attempts
  result
  error
  created_at
  started_at
  finished_at
```

典型 job 类型：

- `embed_text`
- `ocr_image`
- `clean_pdf_text`
- `analyze_article`
- `generate_summary`
- `revise_summary`
- `generate_entity_page`
- `aggregate_index_abstract`
- `generate_briefing_topics`
- `generate_draft`
- `analyze_feedback`

执行方式：

```sql
SELECT *
FROM jobs
WHERE status = 'pending'
ORDER BY priority DESC, created_at ASC
FOR UPDATE SKIP LOCKED
LIMIT 1;
```

收益：

- LLM 调用状态可见。
- 失败可重试。
- 可以限流 Claude / OpenAI。
- 可以记录 token、耗时、错误、成本。
- rebuild 可以断点恢复。
- UI 可以显示 processing / failed / retry。

边界：

- 后台 ingestion、OCR、embedding、summary、maintenance 应优先队列化。
- 实时聊天和草稿生成可以先保留同步/流式，后续再视情况队列化。

---

## 9. 微信 Source 改造

计划改用同 VPS 上部署的 `wechat2rss`。

当前理解：

- wechat2rss 是一个独立 HTTP 服务，可用 Docker / Docker Compose 部署。
- 官方文档说明 Docker 镜像为 `ttttmr/wechat2rss`，私有部署需要配置 `LIC_EMAIL`、`LIC_CODE`、`RSS_HOST` 等环境变量。
- 服务启动后会提供 Web 管理界面和 RSS/JSON Feed 地址。
- API 通过 URL 中的 `k` 参数鉴权，`k` 为 `RSS_TOKEN`。
- 订阅地址形态包括：
  - `/feed/:id.xml`
  - `/feed/:id.json`
  - `/feed/all.xml?k=xxx`
  - `/feed/all.json?k=xxx`
- 它也提供 `/api/query?k=...` 查询文章，返回标题、摘要、发布时间和全文等字段。

因此，它可以作为同一 VPS 上的本地上游服务：

```text
wechat2rss container
  -> http://wechat2rss:<port>/feed/all.xml?k=...
  -> KnowledgeBase-S rss source
```

或者如果需要更强控制，也可以直接调用 wechat2rss 的 JSON/API 查询接口并写入 `source_items`。短期建议先按 RSS source 接入，减少 KnowledgeBase-S 对 wechat2rss 内部 API 的耦合。

建议：

- `wechat2rss` 作为外部上游服务。
- KnowledgeBase-S 不再维护微信 push 特例作为核心路径。
- 微信公众号内容以 RSS source 进入系统。
- 当前 `/api/sources/wechat/ingest` 可以保留为 legacy，一段时间后移除。

目标形态：

```text
wechat2rss
  -> RSS feed
    -> KnowledgeBase-S rss source
      -> source_items
        -> ingestion
```

可验证标准：

- compose 中可以部署 `wechat2rss`。
- 新建 RSS source 指向 wechat2rss feed。
- ingestion-worker 以普通 RSS 流程处理公众号内容。
- wechat push 逻辑不再是主路径。

待确认：

- wechat2rss 的认证、持久化、订阅管理方式。
- 是否需要在 KnowledgeBase-S 前端管理 wechat2rss 订阅。
- 该服务的部署、授权和长期维护风险。

---

## 10. Chat 与 Knowledge Toolset

Chat 不应直接硬接 `layered_retrieval()`，而应通过工具集访问知识库。

建议工具：

```text
kb.search(query, filters)
kb.get_node(id)
kb.get_neighbors(id, depth)
kb.get_sources(node_id)
kb.create_summary(node_id, perspective, instruction)
kb.revise_summary(summary_id, instruction)
```

Chat 层职责：

- 维护会话。
- 调用模型。
- 暴露工具。
- 汇总工具结果。
- 当前阶段只开放只读工具，不允许 Chat 写入知识库。

Knowledge Core 职责：

- 提供工具函数。
- 保证权限和数据一致性。
- 不理解 chat UI。

可验证标准：

- Chat 可以回答“最近关于某主题的资料有哪些”。
- Chat 可以打开某个节点详情。
- Chat 的工具调用不直接访问 SQL，而是通过 kb service。
- Chat 工具在当前阶段不能创建、修改或删除 summary / index / tags / entity。

未来计划：

- 写工具可以作为单独阶段设计。
- 所有写工具默认需要用户确认。
- 初期可考虑只开放低风险写操作，例如创建 summary draft，而不是直接写入正式 summary。
- Chat 写入操作必须记录 action log，包含工具名、参数、模型理由、用户确认时间和最终结果。

---

## 11. Scheduler

当前 `scheduler.py` 是 stub，可以删除。

删除前需要明确替代策略：

- ingestion-worker 自己轮询 RSS。
- summarizer-worker 或外部 cron 触发 briefing。
- maintenance 由手动按钮或外部 cron 触发。

建议：

- 短期移除 compose 中 scheduler 服务和 `scheduler.py`。
- 长期如需统一调度，基于 `jobs` 表实现真正的 scheduler，而不是保留空壳。

可验证标准：

- 删除 scheduler 后 `docker compose up` 不受影响。
- docs / Makefile 不再引用 scheduler。
- 所有定时任务都有明确入口。

---

## 12. 分阶段实施计划

### Phase 0：基线和约束确认

目标：在重构前确保现有行为可观测。

步骤：

1. 解决 Git safe.directory 问题，确保可查看 diff。
2. 记录当前 API 路由清单。
3. 建立最小 smoke test：
   - API health
   - sources list
   - kb nodes list
   - briefing get
   - drafts list
4. 记录当前 DB schema。

验证：

- `docker compose` dev 环境可启动。
- smoke test 可重复运行。
- 有一份 schema 快照。

### Phase 1：清理已知漂移

目标：先修复明显不一致和无用壳层。

步骤：

1. 修复 `summarizer-worker` 日志读取旧 `groups` 字段的问题，改为 `topics`。
2. 删除或禁用 `scheduler`。
3. 清理前端和配置中不再使用的 legacy 提示。
4. 为 `knowledge_edges` 增加唯一约束前，先写重复边清理脚本。

验证：

- summarizer-worker 触发 briefing 后日志正确显示 topic 数量。
- compose 中无 scheduler 服务。
- 重复边清理后可添加唯一约束。

### Phase 2：Wiki Read-only 与 Article Immutable

目标：明确 DB 与 wiki 的事实源关系。

步骤：

1. 前端移除 article wiki 任意编辑入口。
2. 移除 summary 的直接正文编辑入口。
3. 新增 summary revise API，用户必须通过 instruction 修改 summary。
4. revise 后重算 embedding。
5. wiki 由 DB 导出，不再作为日常编辑源。

验证：

- article 详情只能查看，不能直接改正文。
- summary 可以通过 revise instruction 修改，并重新搜索命中。
- wiki rebuild 后内容与 DB 一致。

### Phase 3：时间字段

目标：引入知识时间，不再把入库时间当作事实时间。

步骤：

1. 给 article 增加：
   - `ingested_at`
   - `source_published_at`
   - `source_updated_at`
   - `captured_at`
   - `effective_at`
2. RSS adapter 写入 published/updated。
3. 文件上传支持 optional effective/captured time。
4. Briefing 窗口改用 `knowledge_time`。

验证：

- RSS 导入后 article 有 source_published_at。
- 手动上传可以指定 effective_at。
- briefing force 生成按知识时间窗口过滤。

### Phase 4：Source Items

目标：统一 ingestion 队列和 rebuild manifest。

步骤：

1. 新增 `source_items` 表。
2. URL `pending_urls` 改为创建 source item。
3. 文件上传改为创建 source item。
4. RSS 抓取先落 source item，再处理。
5. ingestion-worker 消费 pending source item。

验证：

- URL 批量追加每个 URL 都成为独立 item。
- 每个 item 有 pending / processing / succeeded / failed 状态。
- 失败 item 可重试。

### Phase 4.5：Object-specific Tables

目标：现在就从单表混合模型迁移到 object-specific tables。

步骤：

1. 新增 `article_nodes`、`summary_nodes`、`entity_nodes`、`index_nodes`。
2. 新增迁移脚本，把现有 `knowledge_nodes` 的对象专属字段回填到对应表。
3. 新增 repository/service 层，封装跨表读写。
4. 新写入路径同时写 `knowledge_nodes` 和 object table。
5. 读路径逐步迁移到 object table。
6. 迁移完成后，清理 `knowledge_nodes` 中的对象专属字段。

验证：

- 每类 object 都能从专属表读取完整信息。
- API 不再依赖大量 nullable 字段判断对象语义。
- 旧数据迁移后节点数量与原始 `knowledge_nodes` 一致。

### Phase 5：移除 LLM 语义边

目标：图谱关系可解释、可重建。

步骤：

1. 停用 `fix_islands`。
2. 停用 `supplement_edges`。
3. 停用 `detect_contradictions`。
4. 清理或隐藏 legacy LLM semantic edges。
5. maintenance 只保留：
   - wikilink/mentions migration
   - entity candidate promotion
   - wikilink backfill
   - orphan entity handling
   - summarizes backfill
   - index abstract aggregation
   - co_occurs_with

验证：

- 新运行 maintenance 不再生成 `extends/background_of/supports/contradicts`。
- 前端默认图谱不展示这些 legacy 边。

### Phase 6：co_occurs_with

目标：实现计划中已预留但未落地的统计共现边。

步骤：

1. 基于 `article -> mentions -> entity` 统计 entity pair。
2. 按 `co_occurs_min_articles` 过滤。
3. weight 使用：

```text
log(1 + n) / log(1 + max_n)
```

4. maintenance 幂等重建 `co_occurs_with`。

验证：

- 多篇文章共同提到的 entity 之间生成 `co_occurs_with`。
- 重复运行 maintenance 不产生重复边。
- 前端图谱可以显示/隐藏该边类型。

### Phase 7：Index 结构化

目标：把 Index 作为结构层对象，而不是普通节点正文。

步骤：

1. 新增 index update API。
2. 新增 children add/remove/reorder API。
3. 新增 `index_children` 表作为 index 结构事实源。
4. children 变化后标记 `abstract_stale`。
5. rollup job 重新生成 index abstract 和 embedding。
6. 如前端图谱需要 `part_of` 边，可从 `index_children` 派生生成展示边。

验证：

- 用户可以手动创建专题 index。
- 可以向 index 添加 article / index。
- 调整顺序后结构稳定保存。
- rollup 后 index 可被语义搜索命中。

### Phase 8：Job Queue

目标：统一后台 Claude/OpenAI 调用。

步骤：

1. 新增 `jobs` 表。
2. 新增 `job-worker`。
3. 先迁移低风险任务：
   - embedding
   - summary generation
   - index rollup
4. 再迁移 ingestion 中的：
   - article analysis
   - OCR
   - PDF cleanup
   - entity page generation
5. UI 展示 job 状态。

验证：

- job pending/running/succeeded/failed 状态正确。
- worker 重启后 pending job 可继续执行。
- 失败任务可重试。

### Phase 9：Rebuild 重做

目标：从 source_items manifest 可控重建。

步骤：

1. rebuild 不再扫描散落 raw 目录猜测来源。
2. rebuild 以 source_items 为准。
3. 支持按 source、type、时间、状态重建。
4. 支持断点恢复。
5. 支持 dry run。

验证：

- RSS / URL / EPUB / 文件都可通过 source_items 重建。
- 多次 rebuild ID 稳定。
- rebuild 后 wiki 与 DB 一致。

### Phase 10：Chat Toolset

目标：Chat 成为配置好 Knowledge Toolset 的知识助手。

步骤：

1. 抽出 `kb.tools`。
2. Chat service 支持工具调用。
3. 前端显示工具引用结果。
4. 写操作加确认。

验证：

- Chat 能搜索知识库。
- Chat 能引用节点和来源。
- Chat 不直接依赖 drafts retrieval 内部实现。

---

## 13. 当前已知必须修改的问题

1. 移除 LLM 语义边：
   - `extends`
   - `background_of`
   - `supports`
   - `contradicts`

2. 打通 URL 队列：
   - 当前 API 写入 `pending_urls`
   - worker 只读取 `config.url`
   - 应迁移到 `source_items`

3. 实现 `co_occurs_with`。

4. 重构 `rebuild_from_raw`：
   - 当前只覆盖 `pdf/plaintext/word/image/wechat`
   - 不覆盖 RSS / URL / EPUB book 全结构

5. 修复 `summarizer-worker` 旧响应格式日志：
   - 当前读取 `groups`
   - API 返回 `topics`

6. 删除 scheduler stub。

7. 明确 wiki read-only export 模型。

8. 增加时间 metadata。

---

## 14. 尚不清楚或需要进一步决策

1. wechat2rss 的实际部署配置。
   - 已确认它是可 Docker 部署的独立 HTTP 服务，提供 RSS/JSON Feed 和管理 API。
   - 仍需确认：授权购买、数据目录、端口、`RSS_HOST`、`RSS_TOKEN`、是否通过内网地址接入、是否需要在 KnowledgeBase-S 前端管理订阅。

2. Entity 正文的长期编辑模型。
   - 当前决策：暂不开放自由编辑。
   - 已确定短期支持 aliases、merge、regenerate、user note。
   - 仍需后续决定 user note 是否进入检索，以及是否参与 entity summary。

3. Raw data retention 的默认策略。
   - 已确定不应无条件丢弃 raw。
   - 仍需决定每类 source 默认策略：
     - PDF / image / EPUB 是否始终 `keep_raw`
     - RSS / URL 是否默认 `keep_extracted_only`
     - 是否支持外部对象存储归档

4. Chat 写工具的未来阶段。
   - 当前决策：Chat 只读，不允许写知识库。
   - 未来如果开放写工具，需要单独设计确认流、action log 和权限边界。

---

## 15. 设计原则总结

1. **用户可以编辑结构和解释，不直接编辑派生检索字段。**

2. **Article 是不可变 factual unit。**

3. **Summary 通过 revise instruction 修改，不开放直接正文编辑。**

4. **Summary 是对 object 的视角化观察，是 summary-first 检索的主要入口。**

5. **Summary 可以按需展开到被观察对象；当被观察对象是 Index 时，应优先经 child summaries 渐进展开。**

6. **Index 是可编辑 collection structure，abstract 是系统派生 rollup。**

7. **Index 结构以 `index_children` 为事实源，不依赖 `knowledge_edges.part_of` 表达顺序。**

8. **Wiki 是导出物，不是事实源。**

9. **核心知识库 API 应少而稳定，表达领域意图，不暴露底层表和 wiki 文件实现。**

10. **LLM 调用应可追踪、可重试、可限流。**

11. **`source_items` 管 ingestion 状态，`jobs` 管模型调用和派生任务。**

12. **Knowledge Core 不理解上层应用。**

13. **所有可重建内容都必须能追溯到 source item、extracted_text、raw 或结构化用户操作。**
