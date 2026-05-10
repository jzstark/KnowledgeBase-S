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

  origin_ref
  origin_ref_type
  raw_snapshot_ref
  extracted_text_ref

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

字段含义：

- `origin_ref`：来源引用，回答“这个 item 原本从哪里来”。可以是 URL、上传文件逻辑名、RSS entry guid、wechat2rss article id 等。
- `origin_ref_type`：来源引用类型，例如 `url`、`upload`、`feed_entry`、`external`。
- `raw_snapshot_ref`：可选的原始快照引用。可以是本地 path、对象存储 key，或 `null`。
- `extracted_text_ref`：规范文本引用。进入 article 生成前必须存在，是 article 正文的主要事实源。

这四个字段取代早期草案中的 `uri`、`raw_path`、`extracted_text_path`。原因是 URL 和 raw file path 不应该混在同一个“来源字段”里。一个 source item 可以同时有原始 URL、系统保存的 HTML 快照、抽取后的正文文本；它们分别表达不同层次的事实。

不同来源的典型落表方式：

| 来源 | `origin_ref` | `origin_ref_type` | `raw_snapshot_ref` | `extracted_text_ref` |
| --- | --- | --- | --- | --- |
| 上传 PDF / Word / image / EPUB | `upload://xxx.pdf` 或原文件名 | `upload` | `raw/pdf/xxx.pdf` | `extracted/pdf/item.txt` |
| 单个 URL | `https://example.com/a` | `url` | 可选：`raw/url/item.html` | `extracted/url/item.txt` |
| RSS / wechat2rss | entry link 或 guid | `feed_entry` | 可选：`raw/rss/item.html` | `extracted/rss/item.txt` |

约束：

- `origin_ref` 必须存在，用于追溯、去重和展示来源。
- `extracted_text_ref` 在进入 article 生成前必须存在。
- `raw_snapshot_ref` 可为空，由 `raw_retention_policy` 决定是否保留。
- `content_hash` 可以基于 raw snapshot 或 extracted text 生成，但必须明确 hash 的输入来源。

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
keep_raw              # 保留原始文件，适合重要 PDF / Word / image / book
keep_extracted_only   # 仅保留 extracted_text，适合低价值网页/RSS
external_archive      # raw 移到对象存储或冷存储，只保留引用
discard_after_ingest  # 入库后删除 raw，仅保留 extracted_text 和 article
```

默认建议：

- PDF / Word / image / EPUB：默认 `keep_raw`。
- RSS / URL：默认 `keep_extracted_only`。
- 用户可按 source 配置覆盖。

`external_archive` 的含义是：raw 不放在 KnowledgeBase-S 本机热存储目录，而是移动到外部对象存储或冷存储，例如 S3、MinIO、Backblaze B2、NAS archive bucket 等；DB 只保存可找回的引用。它不是当前必须实现的能力，只是为未来海量数据和低成本归档预留的 retention policy。短期可以不做，先使用本机文件系统的 `keep_raw` / `keep_extracted_only`。

可验证标准：

- 每个 source item 都记录 raw 与 extracted_text 的路径或外部引用。
- 即使 raw 被清理，article 仍能通过 `extracted_text_ref` 重建主要内容。
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

Summary 是解释层和视角层，允许用户通过 revise instruction 调整，但不开放直接正文覆盖或 wiki 文件编辑。

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
  perspective_label
  perspective_instruction
  perspective_embedding
  body
  body_embedding
  is_default
  source: llm | manual | edited
  revision_history
  edited_at
```

设计原则：

- summary 不是 article 的附属缓存，而是一等的观察对象。
- summary 必须指向一个被观察对象：article 或 index。
- 同一个 article / index 可以有多个 summary。
- summary 的 `perspective_label` 表达用户可读的观察角度，例如“商业模式”“反方论证”“对我写作有用的点”。
- summary 的 `perspective_instruction` 记录它当初为何、如何被生成，便于重放和解释。
- summary 的 `perspective_embedding` 表示“视角本身”的向量。
- summary 的 `body_embedding` 表示“summary 正文内容”的向量。
- summary 可以作为搜索的第一入口，因为它通常比 article 正文更短、更聚焦、更适合 embedding。
- 不提前规定固定 `perspective_key` 类别。固定类别会过早限制知识库的个性化表达；如需聚类或筛选，可未来作为派生结果生成，而不是核心 schema。

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
- child summaries 不应全部展开，而应按 query 与 parent summary 的视角向量进行选择。
- 只有当某个 child summary 仍然相关，才进一步展开到具体 article 或 child index。
- 是否展开、展开到哪一层，可以由检索分数、预算、规则或 LLM 判断共同决定。

这与当前计划不冲突，但会影响 retrieval 的长期设计：summary 不是简单跳板，而是“从不同视角观察对象”的中间层。检索算法应围绕 summary-first、按需展开、预算受控来设计。

当一篇 article 有多个 summary 时，index 展开不能机械取出全部 summary。每个 summary 都是一个候选观察视角，系统应根据当前 query 和父级 summary 的 perspective 选择最相关的少数 summary。

建议评分思路：

```text
child_summary_score =
  content_weight     * sim(query_embedding, child_summary.body_embedding)
+ perspective_weight * sim(parent_summary.perspective_embedding, child_summary.perspective_embedding)
+ query_view_weight  * sim(query_embedding, child_summary.perspective_embedding)
+ priority_weight    * child_priority
```

当用户 query 没有明显视角，或父级没有 perspective 时，降低 perspective 权重，优先使用正文相似度和 default summary。

默认 summary：

```text
is_default = true
perspective_label = "综合摘要"
perspective_instruction = "从整体理解角度总结该对象。"
```

default summary 是没有明确视角匹配时的 fallback。

典型 API：

```text
POST /api/kb/nodes/{node_id}/summaries
{
  "perspective_label": "商业模式",
  "perspective_instruction": "重点分析这篇文章对 AI Agent 产品定价的启发"
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

Entity 是稳定身份锚点，不只是名字，也不是一次性生成的 wiki 页面。它通过 `canonical_name`、`aliases`、`entity_type`、merge history 等字段维持 identity；关于这个 identity 的解释性表述应进入 `entity_profiles`，关于这个 identity 的来源事实应进入 `entity_facts`。

```text
article -> entity mentions
           canonical extracted fact edge
```

规则：

- `canonical_name` 和 `aliases` 可以编辑。
- 重复 entity 应提供合并操作。
- entity 正文不通过 wiki 自由编辑，应通过：
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
- `summary -> entity` 不作为主要事实源；如需保留，只表示 summary 文本中的局部 `text_mentions`。
- `index -> entity` 不作为普通事实边；应由下属 children 的 article mentions 聚合得到。

#### Entity 的持续演化模型

Entity 不应是“一次生成后静态保存”的 wiki 页。它应该随着知识库扩充持续吸收新的 article mentions、facts、时间线和 relatedness 信号。

决策：Entity 拆成三层：

```text
entity_nodes
  stable identity

entity_facts
  source-grounded, time-aware facts

entity_profile
  generated rollup / current explanation
```

`entity_nodes` 与 `entity_profiles` 长期保持分离，不做物理或语义合并。`entity_nodes` 只负责身份稳定性；`entity_profiles` 是可失效、可重生成的派生表述。`entity_profiles` 可以用于快速展示和检索入口，但不反过来作为 `entity_facts` 的事实源。

表结构：

```text
entity_facts
  id
  entity_id
  article_id
  source_item_id
  fact_text
  fact_type
  confidence
  fact_time
  source_published_at
  extracted_at
  evidence_span
  created_at

entity_profiles
  entity_id
  summary
  timeline_summary
  stale
  regenerated_at
```

规则：

- 新 article 入库并抽取 entity mentions 后，可以进一步抽取与该 entity 相关的 source-grounded facts。
- fact 必须能回溯到 article/source item，不能只是 LLM 自由发挥。
- `fact_time` 优先使用 article 的 `effective_at` / `source_published_at`，用于构建时间线。
- Entity profile 是从 facts 和相关 articles 聚合生成的派生说明，facts 变化后标记 `stale`。
- `regenerate` 不是重新发明 entity，而是基于当前 facts/articles 重新生成 profile。
- user note 与 facts 分开保存；它可以参与检索，但不应覆盖 source-grounded facts。

这样可以支持：

- 查询某个 entity 的所有相关文章。
- 查询某个 entity 的时间线 facts。
- 查询与某个 topic/query 相关的 entities。
- 查询某个 index/topic 范围内最重要的 entities。
- 随着新文章入库，entity 自动变得更完整。

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
  entity_type
  merged_into

summary_nodes
  node_id
  summary_of
  perspective_label
  perspective_instruction
  perspective_embedding
  body
  body_embedding
  is_default
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

目标关系类型应尽量来源明确、可解释、可重建。核心原则是：不要把结构事实、文本抽取事实、排序信号和前端展示边混在同一层。

### 6.1 关系分层

```text
Canonical facts:
  summary -> article/index     summarizes
  article -> entity            mentions
  index   -> child object      index_children

Local text signals:
  summary -> entity            text_mentions, optional

Materialized / derived:
  index   -> entity            index_entity_stats
  entity  -> entity            entity_pair_signals / relatedness
  article -> article           article_relatedness, optional

Virtual traversal only:
  index   -> child             contains
  child   -> index             parent
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

### 6.2 Canonical Facts

应长期作为事实源写入的关系只有少数几类：

```text
summary -> article/index     summarizes
article -> entity            mentions
index   -> child             index_children
```

其中：

- `summarizes` 表示 summary 观察的是哪一个 object。
- `article -> entity mentions` 是实体抽取的主要事实源。
- `index_children` 是 index 结构的唯一事实源。

`summary -> entity` 不应承担完整 entity 覆盖语义。它最多表示 summary 正文显式提到的实体，用于 UI 高亮、局部解释或 summary 文本检索。查询 summary 的实体上下文时，应先回到 `summary_of` 的 article/index，再查询该 object 的 entity context。

`index -> entity` 也不应作为普通写入边。Index 的 entity context 应从 children 聚合得到：

```text
index_entity_stats
  index_id
  entity_id
  mention_count
  child_count
  weight
  last_recomputed_at
```

该表是 materialized view / cache，可以重建、截断、排序，不是人工维护的事实边。

### 6.3 Relatedness，而不是 Similarity

用户真正需要的是“相关实体”，不一定是“相似实体”。共现可以说明两个 entity 有关联，但不能直接说明它们相似。

例如：

- `Claude` 与 `ChatGPT` 可能是相似/竞品关系。
- `OpenAI` 与 `Microsoft` 更像强关联关系。
- `美国` 与 `中国` 经常共现，但不能简单说二者相似。

因此不建议把 `entity -> entity co_occurs_with` 和 `entity -> entity similar_to` 作为两种平行图谱边。更合理的设计是把共现作为内部信号，参与计算 entity relatedness。

推荐表：

```text
entity_pair_signals
  entity_a_id
  entity_b_id
  co_occurrence_count
  co_occurrence_score
  embedding_similarity
  graph_proximity_score
  temporal_score
  relatedness_score
  explanation
  updated_at
```

对外 API 暴露：

```text
GET /api/kb/entities/{id}/related
```

返回值可以包含 `relatedness_score` 和解释信息，例如“共同出现于 12 篇 article”“embedding similarity 高”“同属某个 index”。如果未来需要更细分类别，可以在 relatedness 的解释层增加 `similar`、`associated`、`competing` 等标签，但不应把 `co_occurs_with` 作为用户图谱中的一等边。

### 6.4 Index 结构与双向查询

- 决策：引入 `index_children`，不再依赖 `knowledge_edges.part_of` 表达 index 层级结构。
- `knowledge_edges.part_of` 应移除为真实写入边；如 legacy 数据存在，应迁移或只读兼容。
- 顺序、父子关系和 collection 语义以 `index_children` 为事实源。
- 一个 article 或 index 允许属于多个 index。

推荐表：

```text
index_children
  index_id
  child_id
  position
  child_role
  created_at
```

单向存储事实源不等于单向查询能力。反向查询通过索引和 repository API 实现：

```sql
CREATE INDEX idx_index_children_child_id
ON index_children(child_id);
```

需要提供的结构查询能力：

```text
get_children(index_id)
get_parents(object_id)
get_ancestors(object_id)
get_descendants(index_id)
```

如果未来 index 层级很深、递归查询成为瓶颈，可以增加派生表：

```text
index_closure
  ancestor_index_id
  descendant_id
  depth
```

`index_closure` 也只是优化用 materialized view，事实源仍然是 `index_children`。

Edge 约束：

```text
UNIQUE(from_node_id, to_node_id, relation_type)
```

该约束只适用于仍保存在 `knowledge_edges` 的 canonical / compatibility edges。`index_children`、`entity_pair_signals`、`index_entity_stats` 应使用各自表的唯一约束。

可验证标准：

- maintenance 不再调用 LLM 判断 article 与 article 的语义边。
- DB 中新生成 `knowledge_edges` 只包含少量 canonical / compatibility 关系。
- `co_occurs_with` 不再作为用户图谱边生成，改为更新 `entity_pair_signals`。
- `part_of` 不再作为真实写入边生成。
- article 可以属于多个 index，且可以通过 `get_parents(article_id)` 反查父 index。
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

- 为 article 或 index 创建指定 perspective label / instruction 的 summary。
- 按指令 revise summary。
- revise 后自动重算 embedding。
- 创建或 revise 后自动重算 `perspective_embedding` 和 `body_embedding`。
- 不提供直接覆盖 summary body 的编辑接口。

### 7.5 Index API

```text
POST   /api/kb/indices
GET    /api/kb/indices/{id}
PATCH  /api/kb/indices/{id}
GET    /api/kb/indices/{id}/children
GET    /api/kb/objects/{id}/parents
GET    /api/kb/objects/{id}/ancestors
GET    /api/kb/indices/{id}/descendants
POST   /api/kb/indices/{id}/children
DELETE /api/kb/indices/{id}/children/{child_id}
PATCH  /api/kb/indices/{id}/children/order
POST   /api/kb/indices/{id}/rollup
```

表达能力：

- 创建专题 collection。
- 修改 index title / description / rollup_instruction。
- 管理 children 和顺序。
- 支持一个 article/index 属于多个 index。
- 通过 `index_children.child_id` 索引支持反向查询 parent / ancestor。
- children 或 instruction 改变后触发 rollup。
- rollup 更新 index abstract 和 embedding。

### 7.6 Entity API

```text
GET    /api/kb/entities
GET    /api/kb/entities/{id}
PATCH  /api/kb/entities/{id}
GET    /api/kb/entities/{id}/related
GET    /api/kb/entities/{id}/articles
GET    /api/kb/entities/{id}/facts
GET    /api/kb/entities/{id}/timeline
POST   /api/kb/entities/search
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
- 查询 related entities；结果来自 `entity_pair_signals`，而不是用户图谱中的 `similar_to/co_occurs_with` 边。
- 查询某个 entity 关联的所有 articles。
- 查询某个 entity 的 source-grounded facts 和按时间排序的 timeline。
- 按 query/topic/index/time 搜索相关 entities。

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

`summary_first` 模式的核心约束：

- 先全局搜索 summary。
- 命中 article summary 时，按预算决定是否展开 article。
- 命中 index summary 时，先进入该 index 的 children 范围。
- 对 child summaries 进行二次检索，排序同时考虑 `body_embedding` 和 `perspective_embedding`。
- 每个 child 默认最多选一个最相关 summary，除非预算允许且检索分数足够高。
- 只对被选中的 child summary 继续展开到 article 或 child index。

### 7.8 Graph / Wiki / Export API

```text
GET  /api/kb/graph
GET  /api/kb/graph/all
GET  /api/kb/objects/{id}/graph-context
POST /api/kb/wiki/rebuild
GET  /api/kb/wiki/export
```

说明：

- graph API 面向可视化和调试。
- graph API 的展示边可以从 `index_children`、`index_entity_stats`、`entity_pair_signals` 派生，但这些派生边不应反写为事实边。
- `part_of` 不作为真实写入边；父级查询通过 `index_children` 反查。
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
- `extract_entity_facts`
- `refresh_entity_profile`
- `refresh_entity_relatedness`
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

或者如果需要更强控制，也可以直接调用 wechat2rss 的 JSON/API 查询接口并写入 `source_items`。

决策：

- `wechat2rss` 作为外部上游服务。
- 用户单独购买并维护 wechat2rss 授权。
- KnowledgeBase-S 不再维护微信 push 特例作为核心路径。
- 微信公众号内容以 RSS source 进入系统。
- KnowledgeBase-S 前端的微信 source 页面维护一个公众号列表。
- 用户在微信 source 页面勾选公众号，即表示选择该公众号对应的 RSS feed 作为 source。
- 公众号 RSS 只在 VPS 内网或服务端侧使用，不对外公开暴露。
- 当前 `/api/sources/wechat/ingest` 可以保留为 legacy，一段时间后移除。

目标形态：

```text
wechat2rss
  -> per-account RSS feed
    -> KnowledgeBase-S wechat source selection
      -> source_items
        -> ingestion
```

微信 source 页面建议能力：

- 展示可订阅公众号列表，包括 name、wechat_id、feed_id、last_seen_at、enabled。
- 用户只看到公众号维度，不需要看到带 token 的 RSS URL。
- 勾选公众号后，KnowledgeBase-S 在服务端保存对应 feed 引用和 source 配置。
- ingestion-worker 按普通 RSS 流程拉取已启用公众号。
- RSS token、wechat2rss internal URL、授权信息只保存在服务端配置中。

可验证标准：

- compose 中可以部署 `wechat2rss`。
- 微信 source 页面可以列出公众号并启用/停用订阅。
- ingestion-worker 以普通 RSS 流程处理公众号内容。
- 前端和外部用户看不到完整 tokenized RSS URL。
- wechat push 逻辑不再是主路径。

待确认：

- wechat2rss 的数据目录、端口、容器网络名和备份方式。
- 公众号列表是从 wechat2rss API 同步，还是在 KnowledgeBase-S 中手工登记 feed id。
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
kb.create_summary(node_id, perspective_label, perspective_instruction)
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

### Phase 2.5：Summary Perspective Embedding

目标：把 summary 的“视角”本身纳入向量化模型，支持 summary-first 和 index 渐进展开。

步骤：

1. 将 `summary_nodes.perspective` / `instruction` 设计调整为：
   - `perspective_label`
   - `perspective_instruction`
   - `perspective_embedding`
   - `body_embedding`
   - `is_default`
2. 创建 summary 时同时生成 perspective embedding 和 body embedding。
3. revise summary 时重算 body embedding；如果 revise 改变观察视角，也重算 perspective embedding。
4. 为已有 summary 回填 default perspective。
5. 检索时支持按 body similarity 与 perspective similarity 混合评分。

验证：

- 同一 article 可以拥有多个不同 perspective 的 summary。
- 搜索命中 index summary 后，只选择与 query / parent perspective 最相关的 child summaries。
- 不需要固定 `perspective_key` 枚举即可完成视角对齐。

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
   - entity_pair_signals / relatedness refresh

验证：

- 新运行 maintenance 不再生成 `extends/background_of/supports/contradicts`。
- 前端默认图谱不展示这些 legacy 边。

### Phase 6：Entity Facts / Profile / Relatedness

目标：把 entity 从一次性生成页面改为稳定身份、来源事实、派生 profile 和 relatedness 信号的组合模型。

步骤：

1. 新增 `entity_facts` 和 `entity_profiles` 表；`entity_nodes` 只保留 identity 字段，例如 `canonical_name`、`aliases`、`entity_type`、merge history。
2. article analysis 抽取 `article -> entity mentions` 后，为相关 entity 创建 `extract_entity_facts` job。
3. `entity_facts` 必须保存 `article_id`、`source_item_id`、`fact_text`、`fact_time`、`source_published_at`、`evidence_span`、`confidence`。
4. 新增 `refresh_entity_profile` job，根据当前 facts/articles 生成或刷新 `entity_profiles`；facts 变化后将 profile 标记为 `stale`。
5. `regenerate entity` 只刷新 profile，不重新定义 entity identity，也不凭空生成 facts。
6. 基于 `article -> mentions -> entity` 和 entity facts 统计 entity pair。
7. 新增 `entity_pair_signals`，保存共现、embedding similarity、graph proximity、temporal 等信号。
8. 按最小共现文章数和 score 阈值过滤低价值 pair。
9. 共现分数可使用：

```text
log(1 + n) / log(1 + max_n)
```

10. 计算 `relatedness_score`，并生成简短 explanation。
11. 新增 `GET /api/kb/entities/{id}/facts`、`GET /api/kb/entities/{id}/timeline`、`GET /api/kb/entities/{id}/related`。
12. maintenance 幂等重建或增量刷新 `entity_profiles` 和 `entity_pair_signals`。

验证：

- 新文章入库后，相关 entity 可以新增 source-grounded facts。
- fact 可回溯到 article/source item 和 evidence span。
- facts 变化后 profile 标记 stale，并可由 job 重新生成。
- 多篇文章共同提到的 entity 会影响 relatedness 排序，但不会生成 `co_occurs_with` 图谱边。
- `GET /api/kb/entities/{id}/related` 返回分数、解释和来源统计。
- 重复运行 maintenance 不产生重复 pair。
- 前端可展示 related entities，但不把它们误标为 similarity。

### Phase 7：Index 结构化

目标：把 Index 作为结构层对象，而不是普通节点正文。

步骤：

1. 新增 index update API。
2. 新增 children add/remove/reorder API。
3. 新增 `index_children` 表作为 index 结构事实源。
4. children 变化后标记 `abstract_stale`。
5. rollup job 重新生成 index abstract 和 embedding。
6. 为 `index_children.child_id` 增加索引，支持反向查询 parent。
7. 新增 parents / ancestors / descendants 查询 API。
8. 迁移或隐藏 legacy `part_of`，不再写入真实 `part_of` edge。

验证：

- 用户可以手动创建专题 index。
- 可以向 index 添加 article / index。
- 一个 article 可以属于多个 index。
- article 可以通过 parents API 反查所有父 index。
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
   - entity fact extraction
   - entity profile refresh
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
2. Chat service 支持只读工具调用。
3. 前端显示工具引用结果。
4. 当前阶段不开放 Chat 写入工具；写操作确认流、action log 和权限边界作为未来阶段单独设计。

验证：

- Chat 能搜索知识库。
- Chat 能引用节点和来源。
- Chat 不直接依赖 drafts retrieval 内部实现。
- Chat 不能创建、修改或删除 summary / index / tags / entity。

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

3. 实现 entity facts / profile / relatedness：
   - 新增 `entity_facts`，保存来自 article/source item 的 source-grounded facts
   - 新增 `entity_profiles`，保存可失效、可重生成的派生说明
   - `entity_nodes` 只承载稳定身份字段，不与 profile 合并
   - 不再生成 `co_occurs_with` 用户图谱边
   - 基于共现、embedding similarity、结构邻近度等信号维护 `entity_pair_signals`
   - 对外提供 related entities 查询

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

## 14. 仍需确认的实现细节

1. wechat2rss 的实际部署配置。
   - 已确认它是可 Docker 部署的独立 HTTP 服务，提供 RSS/JSON Feed 和管理 API。
   - 已决策：用户单独购买授权；KnowledgeBase-S 微信 source 页面维护公众号列表，用户勾选公众号即启用对应 RSS source；RSS URL/token 不对外暴露。
   - 仍需确认：数据目录、端口、`RSS_HOST`、`RSS_TOKEN`、容器网络、备份方式，以及公众号列表从 wechat2rss API 同步还是手工登记。

2. Entity profile / facts / user note 的实现细节。
   - 当前决策：暂不开放自由编辑。
   - 已确定短期支持 aliases、merge、regenerate、user note。
   - 已确定长期方向：entity 随 article mentions 和 source-grounded facts 持续演化，profile 是派生 rollup。
   - 已确定 `entity_nodes` 与 `entity_profiles` 长期分离，不做合并。
   - 仍需后续细化：fact_type 枚举、fact 抽取粒度、user note 是否进入检索，以及是否参与 entity profile。

3. Summary 视角聚类是否需要 UI 层派生能力。
   - 当前决策：核心 schema 不使用固定 `perspective_key`。
   - 未来可以基于 `perspective_embedding` 自动聚类常见视角，用于 UI 筛选或推荐。
   - 该聚类结果不应成为核心数据模型的约束。

4. Raw data retention 的扩展能力。
   - 已确定不应无条件丢弃 raw。
   - 已决策：PDF / Word / image / EPUB 默认 `keep_raw`；RSS / URL 默认 `keep_extracted_only`。
   - `external_archive` 指把 raw 移到 S3 / MinIO / NAS / 冷存储等外部对象存储，只在 DB 保存引用；这不是短期必须实现的能力。
   - 仍需后续决定：是否以及何时支持外部对象存储归档。

5. Chat 写工具的未来阶段。
   - 当前决策：Chat 只读，不允许写知识库。
   - 未来如果开放写工具，需要单独设计确认流、action log 和权限边界。

---

## 15. 设计原则总结

1. **用户可以编辑结构和解释，不直接编辑派生检索字段。**

2. **Article 是不可变 factual unit。**

3. **Summary 通过 revise instruction 修改，不开放直接正文编辑。**

4. **Summary 是对 object 的视角化观察，是 summary-first 检索的主要入口。**

5. **视角本身需要向量化：使用 `perspective_embedding`，不使用固定 `perspective_key` 枚举约束核心模型。**

6. **Summary 可以按需展开到被观察对象；当被观察对象是 Index 时，应优先经 child summaries 渐进展开。**

7. **Index 是可编辑 collection structure，abstract 是系统派生 rollup。**

8. **Index 结构以 `index_children` 为事实源，不依赖 `knowledge_edges.part_of` 表达父子关系或顺序。**

9. **单向事实源不限制双向查询；parent / ancestor / descendant 通过 repository API 和索引从 `index_children` 查询。**

10. **Article 可以属于多个 index；index 是组织视角，不是单一文件夹路径。**

11. **只有 `article -> entity mentions` 是 entity mention 的主要事实边；summary/index 的 entity context 是局部信号或派生聚合。**

12. **Entity 之间暴露 relatedness，不把 `co_occurs_with` 或 `similar_to` 当作一等用户图谱边。**

13. **Entity 应持续演化：mentions 和 source-grounded facts 是事实基础，profile 是可重生成的派生说明。**

14. **Wiki 是导出物，不是事实源。**

15. **核心知识库 API 应少而稳定，表达领域意图，不暴露底层表和 wiki 文件实现。**

16. **LLM 调用应可追踪、可重试、可限流。**

17. **`source_items` 管 ingestion 状态，`jobs` 管模型调用和派生任务。**

18. **Knowledge Core 不理解上层应用。**

19. **所有可重建内容都必须能追溯到 source item、extracted_text、raw 或结构化用户操作。**
