# GBrain Inspiration

本文记录从 `~/Code/gbrain` 项目中可借鉴到 KnowledgeBase-S 新架构的设计点。

背景参考：

- `docs/revision-progress.md` 第一部分已将 KB-S 定位为知识库核心层，应用层逐步解耦。
- KB-S 核心包括节点、关系、来源、搜索、MCP 工具。
- MCP 工具只读，由外部 `~/Code/kb-chat/` MCP adapter 封装 KB-S 的 `/api/kb/v1/` 稳定接口。

## 总体判断

GBrain 很值得借鉴，但不应整体照搬。

GBrain 更像“个人/团队 brain + agent runtime”：它把 markdown repo 当作事实源，把数据库当作可重建索引，并围绕 agent 长期记忆、写入、同步、技能包、schema pack 展开。

KB-S 的新设计更像“可审计来源驱动的知识库核心层”：它主要处理 RSS、WeChat、URL、PDF、法规、判例、文章等外部来源，Postgres 中的结构化事实表更适合作为核心事实层。

因此，KB-S 应借鉴 GBrain 的架构纪律，而不是照搬它的产品形态。

## 值得借鉴的点

### 1. 明确事实源和派生层

GBrain 最强的架构纪律是：

- markdown repo 是事实源。
- Postgres/PGLite 是可重建索引。
- 每张表都被分类为事实源、派生数据或运行态数据。

KB-S 不一定要把 markdown 变成最高事实源，但应借鉴这种分类法。

建议在 KB-S 中明确：

- 核心事实：`nodes`、`articles`、`entities`、`summaries`、`index_children`、`source_items`
- 可重建派生：`similar_to`、`entity_pair_signals`、embedding、search cache
- 运行态：jobs、logs、locks

这样以后在备份、迁移、volume 切换、灾难恢复时，就能清楚知道哪些数据不可丢，哪些数据可以重建。

### 2. Brain / Source 双轴模型

GBrain 把 `brain` 和 `source` 分清楚：

- `brain` 是数据库边界。
- `source` 是同一 brain 内的内容来源或内容仓库边界。

KB-S 现在已有 `sources`，但将来可以更明确：

- `source`：RSS、WeChat、PDF upload、single URL、book、manual note
- `collection` 或 `workspace`：更高层的数据所有权、用途或权限边界
- `source_id` 必须贯穿 search、edges、facts、summaries，避免跨源 slug/title 混淆

这个思路尤其适合未来出现多个知识库、多个用户、多个来源集合时的扩展。

### 3. 检索不是单一 vector search

GBrain 的检索栈是：

```text
intent classify
  -> optional query expansion
  -> vector search
  -> keyword/BM25 search
  -> RRF fusion
  -> source-aware rerank
  -> graph augment
  -> optional cross-encoder rerank
  -> token budget enforcement
  -> dedup
```

这和 KB-S revision 中的 `search`、`cite`、`summarize_corpus` 方向很接近。

KB-S 可直接借鉴：

- `why_matched` 不应只停留在 `keyword | vector`，后续可以扩展为 explain trace。
- keyword/vector 结果应用 RRF 合并，而不是硬编码一个全局权重。
- graph augment 对 entity/topic 问题很重要。
- 所有 LLM 工具在拼上下文前统一经过 token budget 截断。

### 4. Compiled Truth + Timeline 模式

GBrain 页面分成两层：

- compiled truth：当前综合判断，可被重写。
- timeline：不可变证据轨迹，只追加。

KB-S 可以映射为：

- `nodes.abstract`：当前综合描述，适合 entity/index。
- `entity_facts`：可溯源时间线事实。
- `summaries`：针对 article/index 的不同视角摘要。
- `source_items` / `articles`：原始证据。

这个模式特别适合 entity 页面。Entity 不应该只是“被提到的名字”，而应有一个可重写的 `abstract`，背后由 `entity_facts` 支撑。

### 5. 来源归因和冲突处理

GBrain 强调每个事实都要知道：

- 谁说的
- 何时说的
- 在什么上下文中说的
- 来源优先级是什么
- 是否与已有事实冲突

KB-S 已经设计了 `entity_facts.fact_text / fact_time / evidence_span / confidence`，方向是对的。

下一步应明确：

- 每个 fact 必须有 `article_id` 和 `source_item_id`。
- `cite` 工具必须服务端验证 quote 存在于正文。
- conflicting facts 不应在 ingestion 阶段被静默合并，而应保留并暴露给 timeline/compare/cite。

### 6. Schema Pack 的思想，而不是它的复杂度

GBrain 的 schema pack 是动态本体：

- 类型
- 路径
- link verbs
- extractable / expert_routing
- cache isolation
- agent-authorable mutations

KB-S 当前不需要完整 schema-pack 系统，但可以借鉴轻量版：

- `doc_kind.values` 已经是第一步。
- 未来可以有 `entity_type.values`。
- 未来可以有 `edge_type` registry，声明哪些关系是事实源、哪些关系可重建。
- 不要让用户随手输入自由字符串污染类型系统。

对 KB-S 而言，受控枚举比开放标签更适合承担结构化过滤和工具语义。

### 7. Ingestion Source Contract

GBrain 的 ingestion source 是 dumb emitter：

- source 只负责 emit event。
- daemon 负责监督、重试、去重、路由。
- downstream pipeline 负责写入和 enrichment。

这非常适合 KB-S。

建议 KB-S 的 RSS、WeChat、PDF、URL、future email/webhook 都收敛到统一 source item 入口：

```text
source adapter
  -> emits source_item
  -> pipeline dedup
  -> extract text
  -> classify doc_kind
  -> create article node
  -> extract entity/facts/tags
  -> embed/index
```

source adapter 不应该直接写 knowledge nodes。每个 source item 必须带上：

- `source_id`
- `origin_ref`
- `content_hash`
- `published_at`
- `captured_at`
- raw/extracted text reference
- trust/provenance metadata

### 8. Contract-first MCP / API

GBrain 的 CLI/MCP/HTTP 工具来自同一 operations contract，避免多个入口的 schema 漂移。

KB-S 的 revision 已经决定：

- `kb_public.py` 作为 MCP 稳定接口。
- MCP adapter 位于 `~/Code/kb-chat/`。
- KB-S 本身只暴露干净 REST API，不内置 MCP server。

这个方向是正确的。

进一步可借鉴的是：KB-S 的 7 个 public API 应该有一个单一工具定义源，用于生成：

- OpenAPI schema
- MCP tool schema
- adapter 文档
- prompt/tool usage 描述

这样可以避免 chat 端对 `time_basis`、`captured_at`、`published_at` 等能力产生误解。

## 不建议照搬的点

### 1. 不建议把 KB-S 改成 markdown 事实源

GBrain 的核心是“brain repo 是 system of record”。这适合个人/团队知识库和 agent memory。

KB-S 的主要对象是外部来源文章、法规、判例、RSS、WeChat、PDF。它们天然需要：

- source metadata
- ingestion state
- provenance
- captured/published/effective time
- structured entity/fact tables
- API-first 查询

因此，Postgres 事实表更适合作为 KB-S 的核心事实层。

### 2. 不建议让 MCP 具备大量写操作

GBrain 的 MCP 有大量写入能力，因为它本质上是 agent memory runtime。

KB-S 的 MCP 只读是更安全的选择，尤其是法律、合规、政策、法规知识库场景。

写操作应留在 KB Internal API 和 UI 中，由用户明确触发。

### 3. 不建议一上来做完整 schema-pack 系统

Schema pack 很强，但复杂度也很高。

KB-S 现在更需要的是：

- `doc_kind` 受控
- `entity_type` 受控
- `edge_type` 受控
- ingestion/search/tool contract 清晰

完整动态本体系统可以等到数据规模和使用模式稳定后再考虑。

### 4. 不建议照搬 every-message signal detection

GBrain 很强调每条消息都做 signal detection，因为它服务于个人 agent memory。

KB-S 当前不是聊天记忆系统。除非未来明确要做 personal brain/agent memory，否则不应把 chat 消息自动写入知识库。

## 对 KB-S 的实际优先级建议

### Priority 1: 写一份 system-of-record 文档

新增 `docs/system-of-record.md`，逐表标注：

- 是否事实源
- 是否可重建
- 是否运行态
- 灾难恢复时如何处理
- 是否需要备份

这是从 GBrain 最应该立即借鉴的纪律。

### Priority 2: Contract-first public API

让 `/api/kb/v1` 成为唯一稳定公共契约。

MCP adapter、OpenAPI 文档、工具描述都从同一份定义中生成或至少严格同步。

重点解决：

- time filters 的语义
- `captured_at` / `published_at` / `effective_at` 的暴露
- `search` / `fetch` / `related` 的边界
- 错误返回格式

### Priority 3: 可解释 search pipeline

逐步把 search 做成可解释 pipeline：

```text
query
  -> keyword
  -> vector
  -> RRF
  -> filters
  -> graph augment
  -> optional rerank
  -> explain trace
```

先实现 keyword/vector/RRF/filter/explain，再考虑 rerank 和 graph augment。

### Priority 4: Entity abstract + facts timeline

把 entity 设计成：

- `nodes.abstract` 是当前综合描述。
- `entity_facts` 是证据时间线。
- `mentions` edge 是 article -> entity 的事实边。

这样 entity 页面才会变成真正有用的知识节点。

### Priority 5: Ingestion event contract

把所有来源收敛到统一 source item/event contract。

目标是让 source adapter 只负责“发现和提取原始内容”，不负责知识建模。

## 结论

GBrain 给 KB-S 的最大启发不是某个功能，而是三个架构纪律：

1. 事实源边界清楚。
2. 派生数据可重建。
3. agent-facing 工具契约稳定。

这三点正好支撑 KB-S revision 第一部分的方向：

- KB-S 专注核心知识层。
- 应用层解耦。
- MCP 只读且外置。
- Public API 稳定。
- 来源、节点、关系、搜索成为系统核心。

因此，KB-S 应该借鉴 GBrain 的结构性纪律，而不是变成另一个 GBrain。
