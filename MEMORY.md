# KnowledgeBase-S 架构总览

## 系统定位

以**法律/合规知识**为主要领域的个人知识库后端。核心价值：多源文章自动入库 → 结构化知识图谱（article/entity/summary/index 节点 + mentions/similar_to/contains 边）→ 通过 MCP 工具向 AI Agent 提供高质量检索与推理支持。

KB 核心只负责节点/关系/来源/搜索/MCP 工具。应用层（briefing、drafts）已完全移除。

MCP adapter 实现位于外部仓库 `~/Code/kb-chat/`，封装 `/api/kb/v1/` 稳定接口。KB-S 本身不内置 MCP server。

---

## 部署架构（Docker Compose）

```
nginx
  ↓
api (FastAPI, :8000)         ← 主服务，含 job_worker 逻辑
  ↓
postgres (pgvector:pg16)     ← 数据库 + 向量索引（1536 维）

ingestion-worker             ← 内容抓取 + 入库 pipeline（只通过 HTTP 调用 api）
job-worker                   ← 后台 job 队列消费者（运行在 api 镜像内）
maintenance-worker           ← --profile maintenance，手动触发维护任务
web (Next.js)                ← 前端
```

**技术栈**：Python 3.12 + FastAPI + asyncpg / PostgreSQL 16 + pgvector / Anthropic Claude（Haiku 4.5 做分析/摘要/实体，Sonnet 4.6 做对比/引证/综述）/ OpenAI text-embedding-3-small（1536 维）

---

## 核心数据模型

### 节点层（双表设计）

所有节点统一注册在 `knowledge_nodes`，类型专属字段在 object 子表。

**knowledge_nodes**（统一注册表）
```
id VARCHAR PK            -- 前缀：art_ / ent_ / sum_ / idx_
user_id, title, abstract TEXT
embedding vector(1536)   -- 来自 abstract
embedding_model VARCHAR  -- 记录 embedding 版本（drift 检测用）
source_id FK, tags TEXT[], doc_kind VARCHAR
object_type VARCHAR(16)  -- article|entity|summary|index
ingested_at, published_at, created_at, updated_at
```

`published_at` 是通用知识时间索引，从 `effective_at ?? source_published_at ?? captured_at` 计算。

**object 子表**（均以 `node_id FK → knowledge_nodes` 为 PK）

| 子表 | 关键字段 |
|---|---|
| article_nodes | source_item_id, **document_instance_id FK**, raw_ref JSONB, source_type, 原始时间四列, status(active\|archived) |
| summary_nodes | summary_of FK（取代 summarizes 边）, perspective_*, body TEXT, body_embedding, is_default |
| entity_nodes | canonical_name, aliases TEXT[], entity_type, merged_into FK, **abstract_stale BOOLEAN** |
| index_nodes | description, rollup_instruction, abstract_stale BOOLEAN |

### 关系层

**knowledge_edges**
```
from_node_id / to_node_id FK (CASCADE), relation_type, weight, metadata JSONB
UNIQUE(from_node_id, to_node_id, relation_type)
```
- `mentions`：article → entity，ingestion 时抽取，事实边
- `similar_to`：派生/缓存边，可完全重建
- `summarizes`：已删除，由 `summary_nodes.summary_of` FK 表达

**index_children**：`(index_id, child_id)` PK，position, child_role(member|chapter)

### 来源层（legacy，ingestion pipeline 底层）

**sources**：id, name, type, config JSONB, is_primary, default_doc_kind, deleted_at（软删除）

**source_items**：每条待处理/已处理内容项，含原始时间四列、doc_kind、**document_instance_id FK**、status(pending|processing|succeeded|failed)、UNIQUE(user_id, source_id, origin_ref_type, origin_ref)

source type：rss | url | wechat | pdf | image | plaintext | word | epub

### 文件夹层（Phase B 新增，用户组织层）

**folders**：id(`fld_`), user_id, parent_id, name, kind(normal|stream), status(active|archived)

**connectors**：id(`con_`), folder_id FK, type(rss|wechat), config JSONB, status(active|inactive), last_fetched_at — stream 资料夹的外部接入，与 legacy source 一一对应（`fld_XXXX` ↔ `src_XXXX`，`con_XXXX` ↔ `src_XXXX`）

**raw_assets**：id(`ra_`), user_id, storage_key（物理路径/URL，不随 UI 移动变化）, original_filename, mime_type, size, sha256

**document_instances**：id(`di_`), folder_id FK, raw_asset_id FK, connector_id FK(nullable), display_name, origin_ref, origin_ref_type, doc_kind, status — 资料夹条目，与 source_items 一一对应（`di_XXXX` ↔ `si_XXXX`）

查询链路：资料夹 → document_instances → raw_assets → article_nodes → wiki md

ID 映射约定（同 hex 后缀，不同前缀）：
- `src_XXXX` ↔ `fld_XXXX`（legacy source ↔ folder）
- `src_XXXX` ↔ `con_XXXX`（stream source ↔ connector）
- `si_XXXX` ↔ `di_XXXX`（source_item ↔ document_instance）
- `si_XXXX` ↔ `ra_XXXX`（source_item ↔ raw_asset）

### 派生层

**entity_candidates**：`canonical_name UNIQUE`，`mention_count INT`，`max_salience FLOAT`，`source_article_ids TEXT[]`，promoted_entity_id FK（JSONB mentions 已删除，改为计数器）

**entity_facts**：entity_id + article_id 可溯事实，fact_text / fact_time / evidence_span / confidence，UNIQUE(entity_id, article_id, fact_text)

**entity_pair_signals**：co_occurrence + embedding_similarity + graph_proximity + temporal → relatedness_score

**jobs**：job_type + status + payload JSONB + idempotency_key，供 job_worker 消费

---

## doc_kind 继承链

优先级（高→低）：显式 IngestRequest.doc_kind > document_instances.doc_kind > source_items.doc_kind > sources.default_doc_kind > config.default

枚举值定义在 `config/system.yaml`：regulation / case / news / memo / contract / analysis / other。所有 doc_kind 输入点均需校验，非法值降级为 default。UI 下拉从 `GET /api/config/doc_kind` 获取。

---

## API 结构

### 双 OpenAPI 文档

- `/api/docs` — 内部全量
- `/api/kb/v1/docs` — KB Public（MCP adapter 只看这里）

`app.mount("/api/kb/v1", kb_public_app)` 实现子应用挂载。

### KB Public（MCP 稳定接口，`/api/kb/v1/`，全部只读）

| 端点 | 工具 | LLM |
|---|---|---|
| GET /search | search（hybrid vector+keyword） | 无 |
| GET /nodes/{id} | fetch 单节点 | 无 |
| POST /nodes/batch | fetch 批量 | 无 |
| GET /nodes/{id}/related | related（6 种 relation） | 无 |
| GET /timeline | timeline（entity_id 或 topic_query） | 无 |
| POST /compare | compare（2-5 节点对比） | sonnet-4-6 |
| POST /cite | cite（两阶段引证查找） | sonnet-4-6 |
| POST /summarize_corpus | summarize_corpus | sonnet-4-6 |

### KB Internal（`/api/kb/`）

**入库 pipeline**（ingestion-worker 调用，无 cookie 认证，用 INTERNAL_API_KEY）：
- `POST /kb/ingest` — 核心入库，doc_kind 级联在此处理
- `POST /kb/entity_candidates/analyze_context` — 返回 nearby_entities + top_candidates + popular_tags
- `POST /kb/entity_candidates/process` — upsert 候选，返回晋升列表
- `POST /kb/entity_candidates/{id}/mark_promoted`
- `POST /kb/entities/{id}/backfill_wikilinks`
- `POST /kb/entities/refresh_stale` — 批量 LLM 刷新 abstract_stale=true 的 entity（ingestion-worker 每轮 pipeline 结束后调用）

**节点/图谱**：`GET /kb/search`，`GET /kb/node/{id}`，`GET /kb/nodes`，`GET /kb/graph`

**管理**（需认证）：节点 CRUD / summary CRUD / entity merge+delete / index CRUD + rollup / maintenance 触发 / jobs 管理

完整端点见 `services/api/kb/` 各模块。

### Sources（`/api/sources/`，legacy）

CRUD + wechat2rss 专属接口 + source-items 状态管理 + doc_kind 覆盖（`PATCH /source-items/{id}`）

### Folders（`/api/folders/`，`/api/document-instances/`，`/api/connectors/`，Phase B 新增）

**Folders**：`GET /api/folders`（树形列表）、`POST /api/folders`（创建，同时生成 legacy source）、`PATCH/DELETE /api/folders/{id}`、`GET /api/folders/{id}/contents`（子资料夹 + document_instances）、`POST /api/folders/{id}/upload`（文件上传→raw_asset+document_instance+source_item）、`POST /api/folders/{id}/add-url`

**Document Instances**：`GET/PATCH/DELETE /api/document-instances/{id}`、`POST .../copy`、`POST .../reprocess`

**Connectors**：`GET/POST /api/connectors`、`PATCH/DELETE /api/connectors/{id}`、`POST /api/connectors/{id}/sync`（触发 ingestion-worker）

---

## 7 个 MCP 工具详解

### search
`GET /api/kb/v1/search` — Hybrid 向量+关键词
```
final_score = vector_score + (0.15 if keyword_hit else 0)
vector_score = 0.75*(1 - body_embedding <=> q) + 0.25*(1 - perspective_embedding <=> q)
               若无 summary：1 - n.embedding <=> q
why_matched: vector | keyword | hybrid
```
过滤器：type / doc_kind / tags / source_ids / date_range

### fetch
单节点和批量，返回 body（wiki 文件）+ summaries 列表 + index outline（子节点）

### related
6 种关系路由：mentions / mentioned_by / summarizes / summarized_by / contains / part_of

### timeline
entity_id → mentions 反向边；topic_query → 向量过滤（timeline_min_score=0.3）；include_facts=true 附 entity_facts

### compare
`_load_doc_context()` 优先取 default summary body，无则 fallback wiki 正文（截断 compare_body_chars=4000）→ LLM 生成 Markdown 表格 + 分析

### cite（两阶段）
```
Stage 1（无 LLM）：embed(claim) → top cite_candidate_count(20) 候选，doc_kind 过滤
Stage 2（LLM）：cite_match prompt → LLM 返回 [{article_id, candidate_quote, explanation, confidence}]
服务端验证：substring 匹配原文，幻觉 quote 丢弃
返回：服务端验证通过的精确引语（最多 cite_max_results=5）
```

### summarize_corpus
query 路径：summary_nodes.body_embedding 搜索（min_score=0.3）→ fallback article 直接搜索；
显式 node_ids 路径直接跳过搜索。
LLM 生成综述 + coverage_note（基于 N 篇文章，时间跨度）

---

## 核心算法

### HyDE 查询扩展
```
user_query → LLM(hyde_abstract, haiku-4-5, 200 tokens) → hypothetical_abstract
→ embed(hypothetical_abstract) → 用扩展向量检索（而非原始 query）
```
config 控制：`retrieval.use_hyde: true`

### Summary-first 分层检索（内部 RAG，compare/cite/summarize_corpus 共用）
```
Phase 1a: summary_nodes.body_embedding 向量搜索 top 5
Phase 1b: entity 节点向量搜索 top 10
Phase 1c: 兜底 article 直接搜索 top 8

Phase 2: summary → 取 summary_of 文章；entity → mentions 反向边文章
Phase 3: expansion，以 anchor 文章为起点，二跳找相关文章（阻尼 0.3）
Phase 4: index 展开（得分 > 0.4）
Phase 5: 兜底（分数 × 0.5 折扣）

截断：article_inline_threshold(2000 token) 控全文/abstract；context_max_tokens=100000
```

### Entity 发现算法（ingestion 时）
```
1. embed(article[:8000])
2. analyze_context → nearby_entities(top20) + top_candidates(top20) + popular_tags(top50)
3. LLM article_analysis(text, nearby_entities, top_candidates, popular_tags)
   → {abstract, tags[], entities[{canonical_name, salience, matches_existing_id}]}
4. process_entity_candidates(article_id, entities)：
   - 有 matches_existing_id → INSERT mentions edge + upsert entity_fact
                            → entity_nodes.abstract_stale = true
   - 无 → UPSERT entity_candidates(mention_count+1, max_salience=GREATEST, source_article_ids+=)
   - 检查晋升：max_salience >= 0.9
                OR (max_salience >= 0.7 AND mention_count >= 2)
                OR mention_count >= 3
5. 晋升 → ingestion-worker 生成 entity_page（LLM）→ post_ingest entity → backfill_wikilinks
```

### Entity Abstract 持续更新
新 mention → `entity_nodes.abstract_stale = true`；merge → target 置 stale。
每轮 ingestion pipeline 结束后 ingestion-worker 调 `POST /api/kb/entities/refresh_stale`，
每批最多 `entity_update_batch`（默认 10）个 entity 用 `entity_update` prompt + Claude Haiku 重写 abstract 并重算 embedding。
实现：`kb/graph.py:lm_refresh_entity_abstract()` + `refresh_stale_entity_abstracts()`。

### Tag 收敛机制
入库分析时，top-50 常用 tags 注入 `article_analysis` prompt `<<<existing_tags>>>`，引导 Claude 优先复用已有 tags，只在真正新主题时创造新词。

### Embedding Model Drift 检测
`maintenance/diagnostics.py` 检测 `embedding_model != current_model` 的节点，只报告不自动重算（重算影响检索质量和 API 成本，需人工确认）。

---

## Ingestion Worker Pipeline

独立服务，不直连 DB，全部 HTTP 调用 API。

```
main.py: 轮询 GET /api/sources + GET /api/connectors（Phase B）
         → build_source() → _dispatch_pipeline()

run_once：subscription sources + active connectors（fld_ → src_ 映射，避免重复）

run_pipeline（标准文章）：
  fetch_pending_source_items → 对每个 source_item：
    source.extract_text(item) → text
    读取 source_item.document_instance_id → 传入 ArticleIngestionInput
    process_article_like_item(ArticleIngestionInput, adapters)
  → update_last_fetched
  → refresh_stale_entities()   ← 调 POST /api/kb/entities/refresh_stale

process_article_like_item：
  embed → analyze_context → LLM article_analysis
  → post_ingest(article, document_instance_id=...) → post_ingest(summary)
  → write_wiki_article(doc_kind 写入 frontmatter)
  → process_entity_candidates → [晋升流程]

run_book_pipeline（EPUB/MOBI）：
  extract_chapters → post_ingest(index_node)
  → 对每章 process_article_like_item(use_entity_context=False)
  → refresh_stale_entities()
```

`write_wiki_article` 写 frontmatter（含 doc_kind），`restore_from_wiki` 读取重建 DB。

**article ID 生成优先级**（`_make_node_id`）：document_instance_id（Phase B 稳定键）> raw_ref.path > raw_ref.url > random

---

## 维护任务（run_maintenance）

```
1. promote_entity_candidates  → 晋升满足条件的候选，创建 entity nodes
2. backfill_wikilinks         → 回填新晋 entity 的历史文章 [[wikilink]]
3. backfill_entity_facts      → 补全 entity_facts（从 mentions edges）
4. rebuild_entity_pair_signals → 重建 co-occurrence + similarity 缓存
5. aggregate_index_abstracts  → LLM 聚合 abstract_stale=true 的 index 节点
6. detect_embedding_model_drift → 只检测报告
```

**job_worker job_type**：generate_summary / revise_summary / aggregate_index_abstract / rebuild_wiki / run_maintenance / rebuild_from_raw

**restore_from_wiki**：从 wiki 文件系统重建 DB，published_at 从时间字段级联推导。
**rebuild_from_raw**：清空派生节点 + 重置 source_items 为 pending + ingestion-worker 重新处理。

---

## 配置管理

**所有参数外置**，不允许硬编码：
- 数值/枚举 → `config/system.yaml`（分区：doc_kind / ingestion / models / embedding / entity / retrieval / maintenance / entity_insights / llm_output_tokens / kb_public）
- Prompt 字符串 → `config/prompts.md`（section 标题即 key，`## section_name`）

API 和 ingestion-worker 各有独立 `settings.py`（frozen dataclass），用 `**sub(key)` 反序列化。YAML 新增 key 时两份均需同步，否则启动报 `TypeError: unexpected keyword argument`。

---

## 已知遗留项

| 项 | 状态 |
|---|---|
| `POST /api/kb/nodes/{id}/summaries`（§7 规范路径） | 实际为 `/create_summary`，功能可用 |
| Source 删除 cascade 选项（同时删 N 篇文章） | 未实现 |
| Source 管理页显示已停用 source | 后端支持 `?include_deleted=true`，前端未接（旧 /sources 页已重建为文件管理器） |
| Phase B: raw_ref 降级（Phase 5） | ✓ 完全完成：孤立文章回填 raw_assets+document_instances，files.py 改纯 INNER JOIN，restore.py 只读 storage_key，wiki.py 写 storage_key。raw_ref 列仍存在但不再作为任何读路径的依赖 |
| Phase B: 旧 /sources 页 wechat 配置子页 | `/sources/[id]/page.tsx` 仍存在但入口已从新 UI 移除，微信 connector 暂通过旧页面管理 |
