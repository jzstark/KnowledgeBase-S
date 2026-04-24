# KnowledgeBase-S 多层记忆架构设计

> 本文档记录了 2026-04-24 对话中形成的架构设计决策，作为后续开发的参考依据。

---

## 一、设计动机

现有系统的核心痛点：所有内容平铺存储，一本书（raw data）的体量远大于其他所有文章之和，导致知识库检索时被单一来源淹没。更根本的问题是缺乏抽象层次——"先看摘要、相关再深入"的分层回忆机制从未被实现。

设计目标：
- 建立分层抽象（summary → article/index → entity），检索按层次递进
- 消除平铺导致的"大文件统治"问题
- 关系体系简洁、可解释、不依赖 LLM 语义判断
- 所有参数外部化，修改配置后可从 raw material 完整重建知识库

---

## 二、对象类型（Objects）

系统中共 **4 种第一公民对象**，均存于 `knowledge_nodes` 表，以 `object_type` 字段区分：

| object_type | 定义 | 来源 | wiki 路径 |
|-------------|------|------|-----------|
| `article` | 自成体系的文字内容单元：博客、新闻、论文、书的某一章节 | 用户导入 | `wiki/articles/{id}.md` |
| `index` | 层级容器节点，可包含若干 article 或 index（递归嵌套）。代表"书"、"专题集"等集合 | 用户导入（书）或手动创建 | `wiki/indices/{id}.md` |
| `entity` | 跨文章反复出现的概念名词：人物、地点、组织、事件、思想概念 | 系统从 article 中提取并晋升 | `wiki/entities/{id}.md` |
| `summary` | 对某一 article 或 index 的提炼性文字，可带视角标注（perspective） | 系统生成或用户手动触发 | `wiki/summaries/{id}.md` |

**关键决策**：
- **废弃 `chunk` 类型**：书的章节统一用 `article` 表示，通过 `part_of` 边与父 `index` 关联。"是否为章节"由边关系决定，不靠独立 object_type。
- **Index 节点的 abstract**：由系统从子节点 abstract 自动聚合生成（LLM rollup），用于 embedding 索引。这与 `summary` 是两回事——abstract 是短摘要（3-5 句），始终存在；summary 是更详细的视角性提炼，按需生成。
- **Summary 一对一**：一个 summary 只对应一个 article 或 index（`summarizes` 边），不嵌套。但一个 article/index 可以有多个 summary（不同 perspective）。

### Index 示例结构

```
Index("战争与和平")
  ├── article("第一章：彼得堡沙龙")
  ├── article("第二章：鲍尔孔斯基家族")
  │     └── summary("人物关系分析", perspective="人物")
  ├── Index("第三部：1812年战役")         ← 子 index，递归嵌套
  │     ├── article("第十五章：博罗季诺")
  │     └── article("第十六章：撤退")
  └── summary("全书综合概述", perspective=null)
```

---

## 三、关系体系（Edges）

格式：`来源 ──[关系]──→ 目标 ｜ weight 含义 ｜ 生成时机`

### 结构性关系（weight = 1.0，不参与检索评分，仅用于导航）

```
article  ──[part_of]────→ index      │ 1.0  │ ingestion 时建立（章节属于书）
index    ──[part_of]────→ index      │ 1.0  │ ingestion 时建立（子集属于父集）
summary  ──[summarizes]─→ article   │ 1.0  │ summary 创建时
summary  ──[summarizes]─→ index     │ 1.0  │ summary 创建时
```

### 内容引用关系（weight = 实体显著度 salience）

```
article  ──[mentions]───→ entity    │ salience ∈ [0,1]  │ ingestion 时 article_analysis 写入
summary  ──[mentions]───→ entity    │ salience ∈ [0,1]  │ summary 生成时同步写入
```

`mentions` 边的 weight 来自 `article_analysis` prompt 返回的 `salience` 字段。  
**已知 bug**：`backfill_wikilinks` 写入时 weight 硬编码为 1.0，需改为从 `entity_candidates.mentions` 读取真实 salience。

### 统计相似关系（weight = 归一化得分）

```
entity   ──[similar_to]────→ entity  │ cosine ∈ [0.75,1]     │ entity 入库时 build_similar_edges
entity   ──[co_occurs_with]─→ entity │ log(1+n)/log(1+max)   │ maintenance 定期计算
article  ──[similar_to]────→ article │ cosine ∈ [0.75,1]     │ article 入库时 build_similar_edges
```

`co_occurs_with` 的 weight 公式：`log(1 + n) / log(1 + max_n)`，其中 n 是两个 entity 共同出现的 article 数，max_n 是库内最大共现数，归一化到 [0,1]。

### 明确不做的关系

- ~~entity -- part_of / instance_of / related_to -- entity~~（LLM 语义关系，跨库类型稳定性差）
- ~~article -- contradicts / supports / extends / background_of -- article~~（LLM 语义关系，限制库的适用范围）
- 跨类型 similar_to（article↔entity 等）：无语义意义

**合计 8 种关系类型**，全部来源明确、语义无歧义。

---

## 四、边权重详解

边的 `weight` 字段是检索评分时的核心乘数，含义因 relation_type 而异：

| relation_type | weight 范围 | 语义 | 在检索中的作用 |
|---------------|------------|------|--------------|
| `mentions` | 0.0 ~ 1.0 | entity 在该 article 中的显著度（salience） | entity→article 路径评分的乘数：salience 越高，article 在"找与此 entity 相关的文章"时排名越靠前 |
| `similar_to`（entity） | 0.75 ~ 1.0 | embedding 余弦相似度 | 图谱聚类；当前检索算法未直接用它传播分数 |
| `similar_to`（article） | 0.75 ~ 1.0 | embedding 余弦相似度 | 图谱聚类；当前检索算法未直接用它传播分数 |
| `co_occurs_with` | 0.0 ~ 1.0 | 归一化共现频率 | 即使 embedding 不相似（"战争"和"拿破仑"），共现关系也能将两个 entity 联系起来 |
| `part_of` / `summarizes` | 1.0（固定） | 结构性（二元关系） | 仅用于图导航（展开 index、找 summary 对应的 article），不参与排序评分 |

---

## 五、检索算法

> 以"用户选定选题，系统生成草稿"为场景。

### 5.1 Query Embedding：HyDE 提升精度

直接把选题文字压缩成单向量会损失精度，因为选题文字（"分析拿破仑战争对欧洲民族主义的影响"）与库内 article abstract（"本文从政治与文化两个维度分析..."）在语言风格上有差距。

**HyDE（Hypothetical Document Embeddings）**：先用 LLM 生成一段假设性的"相关文章摘要"，再 embed 这段文字。生成的 ~100 字 hypothetical abstract 在词汇和风格上更接近 article abstract 的 embedding 空间，显著提升召回质量。成本：额外一次廉价 Haiku 调用。

通过 `system.yaml` 中 `retrieval.use_hyde: true/false` 控制。

### 5.2 Index 节点的可见性

Index 节点没有 `mentions` 边（entity 仅从 article 正文提取），因此无法通过 entity→article 路径发现。但这不是缺陷：Index 的 abstract 是从所有子节点 abstract 聚合生成的，包含书中所有章节涉及的核心词汇，可以通过 **直接 embedding 检索** 命中。

命中后通过 **Phase 4 Index 展开** 将其 article 子节点引入候选池（Index 本身不进 context，它只是目录）。

### 5.3 完整算法

**输入**：选题文字 T  
**输出**：按相关性排序的 article 列表，用于 context 装配

```
Phase 0: 查询嵌入
  if cfg.retrieval.use_hyde:
    hypo = LLM(hyde_abstract_prompt, T)    # ~100字假设性摘要，一次 Haiku 调用
    q_vec = embed(hypo)
  else:
    q_vec = embed(T)

Phase 1: 三路并行向量检索（纯 embedding，不走图）
  1a. summary_hits = pgvector_search(object_type='summary', q_vec, top=cfg.summary_top_k)
      → {summary_id: sim_score}
  1b. entity_hits  = pgvector_search(object_type='entity',  q_vec, top=cfg.entity_top_k)
      → {entity_id:  sim_score}
  1c. article_hits = pgvector_search(object_type IN ('article','index'), q_vec, top=cfg.article_direct_top_k)
      → {node_id: sim_score}   ← 兜底路径，Phase 5 使用

Phase 2: 沿图传播分数（3 条路径）
  scored_summaries = {**summary_hits}    # 初始化
  scored_articles  = {}

  2a. entity → summary（发现间接相关的 summary）
      for entity_id, s_e in entity_hits:
        for summary_id, w in reverse_mentions(entity_id, from_type='summary'):
          scored_summaries[summary_id] = max(
              scored_summaries.get(summary_id, 0),
              s_e * w * cfg.damping_entity_to_summary    # 默认 0.7
          )

  2b. entity → article（直接找到提到这些 entity 的 article）
      for entity_id, s_e in entity_hits:
        for article_id, w in reverse_mentions(entity_id, from_type='article'):
          scored_articles[article_id] = scored_articles.get(article_id, 0) + s_e * w
          # 加法：同一 article 被多个相关 entity 提到时累积加分

  2c. summary → article（summary 命中意味着其对应 article 也相关）
      for summary_id, s_s in scored_summaries.items():
        target_id = get_summarizes_target(summary_id)
        if target_id:
          scored_articles[target_id] = scored_articles.get(target_id, 0) + s_s

Phase 3: 一跳 article→entity→article 扩展（发现间接相关文章）
  top_j = top(scored_articles, cfg.expansion_anchor_k)    # 默认 5 篇作为锚点
  for article_id, s_a in top_j:
    if s_a < cfg.expansion_min_score: break                # 默认 0.3，低分不值得扩展
    for entity_id, w_a in mentions_of(article_id):
      for other_article_id, w_b in reverse_mentions(entity_id, from_type='article'):
        if other_article_id not in scored_articles:        # 只添加新发现的，不重复
          scored_articles[other_article_id] = s_a * w_a * w_b * cfg.damping_hop  # 默认 0.3

Phase 4: Index 展开（用子节点替换 index 本身）
  high_score_indices = [
      (id, score) for id, score in scored_articles.items()
      if node_type(id) == 'index' and score > cfg.index_expand_threshold  # 默认 0.4
  ]
  for index_id, s_idx in high_score_indices[:cfg.index_expand_limit]:     # 默认最多展开 3 个
    for child in children_of(index_id, object_type='article'):
      child_score = s_idx * embed_sim(q_vec, child.abstract_embedding)
      scored_articles[child.id] = max(scored_articles.get(child.id, 0), child_score)
    del scored_articles[index_id]    # index 本身不进 context

Phase 5: 兜底填充（冷启动 / 无摘要新库）
  if len(scored_articles) < cfg.article_top_k:
    for node_id, sim in article_hits:    # Phase 1c 的结果
      if node_id not in scored_articles:
        scored_articles[node_id] = sim * cfg.fallback_score_discount  # 默认 0.5

Phase 6: 最终排序
  return sorted(scored_articles, by score, desc)[:cfg.article_top_k]
```

### 5.4 算法正确性保证

| 属性 | 保证机制 |
|------|---------|
| **无环路** | 各 Phase 单向传播：entity→article（2b）、article→entity→article（3，仅一跳）、index→children（4）。没有任何路径回头 |
| **不重复入队** | Phase 3 的 `not in scored_articles` 检查；Phase 4 删除 index 节点本身 |
| **有界执行时间** | 所有循环大小受 config 参数严格限制（`expansion_anchor_k`、`index_expand_limit` 等），与图总规模无关 |
| **无评分爆炸** | Phase 3 乘以 `damping_hop=0.3`；间接路径得分天然低于直接路径 |
| **冷启动覆盖** | Phase 5 兜底确保即使 entity/summary 层完全没有命中，仍可通过 abstract 直接匹配 |

### 5.5 Context 装配

```
按 token 预算，从高分到低分依次决定每篇 article 的呈现方式：

  if article.wiki_body_tokens < cfg.article_inline_threshold (默认 2000):
    插入 wiki 全文
  else:
    插入 abstract
    + 若有 summary：插入最相关 summary 正文

同时插入：
  top cfg.entity_in_context 个 entity wiki 正文（按 Phase 1b entity_hits 排名，默认 5 个）

总 token 不超过 cfg.context_max_tokens（默认 100000）
```

---

## 六、Summary 多视角设计

`knowledge_nodes` 表新增 `perspective TEXT`（nullable）字段：
- `null`：默认综合摘要，入库时自动生成
- 非 null：用户命名的视角（如 "人物关系"、"技术背景"），用户手动触发

新增 API：`POST /api/kb/nodes/{id}/create_summary`，body：`{"perspective": "人物关系"}`（perspective 可选）

Index 的 `abstract` 字段（短摘要，用于 embedding）与 Summary 节点是两回事：
- `abstract`：系统自动维护，从子节点 abstract 通过 LLM 聚合生成，入库时即有
- `summary` 节点：视角性的详细提炼，按需生成

Index abstract 的聚合逻辑（维护时触发，自底向上）：
```
LLM(index_summary_prompt, children_abstracts) → index.abstract
```
叶 article 先更新，再向上逐层聚合父 index 的 abstract。

---

## 七、Config 外部化

### 现状

- `config/prompts.md`：已存在，挂载到所有容器为 `/app/shared_config/prompts.md`
- 所有数值参数（阈值、top_k、token 上限等）分散在 Python 源码中（`pipeline.py`、`maintenance.py`、`kb.py`、`drafts.py`）

### 新增：`config/system.yaml`

挂载到所有容器为 `/app/shared_config/system.yaml`。修改参数后重启容器生效；若要让参数影响历史入库内容，需执行 `rebuild_from_raw`。

```yaml
# ================================================================
# KnowledgeBase-S 系统参数配置
# ================================================================

ingestion:
  max_text_chars: 12000        # 送给 article_analysis 的最大字符数（约 4000 token）
  chunk_trigger_words: 5000    # 超过此词数 + 有章节结构时，触发 index+article 切分
  chunk_target_words: 1500     # 目标每章节词数（无明确结构时按此切分）

models:
  article_analysis:  "claude-haiku-4-5-20251001"
  entity_page:       "claude-haiku-4-5-20251001"
  entity_update:     "claude-haiku-4-5-20251001"
  summary_gen:       "claude-haiku-4-5-20251001"
  index_summary:     "claude-haiku-4-5-20251001"  # 聚合子节点 abstract 生成 index abstract
  hyde_abstract:     "claude-haiku-4-5-20251001"  # HyDE 查询扩展
  briefing_topics:   "claude-sonnet-4-6"
  draft_generation:  "claude-sonnet-4-6"

embedding:
  model:      "text-embedding-3-small"
  dimensions: 1536
  max_chars:  8000             # embed() 截断长度

entity:
  promotion_max_salience:       0.9   # 单篇 max_salience >= 此值则晋升
  promotion_salience:           0.7   # salience >= 此值 AND mentions >= below 则晋升
  promotion_salience_mentions:  2
  promotion_min_mentions:       3     # mentions >= 此值则晋升（不论 salience）

retrieval:
  use_hyde: true
  similar_to_threshold:         0.75  # cosine 阈值（article 和 entity 共用）
  similar_to_limit:             20    # build_similar_edges 每节点最多建多少条边
  co_occurs_min_articles:       3     # 共现 >= 此数才建 co_occurs_with 边
  summary_top_k:                5     # Phase 1a 检索数
  entity_top_k:                 10    # Phase 1b 检索数
  article_direct_top_k:         8     # Phase 1c 检索数（兜底）
  article_top_k:                8     # 最终进入 context 的文章数
  entity_in_context:            5     # 最终进入 context 的 entity 数
  article_inline_threshold:     2000  # token 数低于此值时插入全文，否则只插 abstract
  context_max_tokens:           100000
  damping_entity_to_summary:    0.7   # Phase 2a entity→summary 路径衰减
  damping_hop:                  0.3   # Phase 3 article→entity→article 二跳衰减
  expansion_anchor_k:           5     # Phase 3 最多以几篇 article 作为扩展锚点
  expansion_min_score:          0.3   # 低于此得分的 article 不做扩展
  index_expand_threshold:       0.4   # index 得分高于此才展开子节点
  index_expand_limit:           3     # 最多展开几个 index
  fallback_score_discount:      0.5   # Phase 5 兜底路径的分数折扣

maintenance:
  entity_update_batch:          10    # entity_update 每批处理数量

briefing:
  topics_count:                 5

llm_output_tokens:
  article_analysis:             2048
  entity_page:                  2048
  entity_update:                2048
  summary_gen:                  1024
  index_summary:                512
  hyde_abstract:                200
```

### `config/prompts.md` 新增 section

需新增以下 prompt section：

- `## index_summary`：输入若干子节点 abstract → 输出 index 的聚合 abstract（3-5句）
- `## summary_gen`：输入 article 正文 + perspective（可选）→ 输出指定视角的 summary 正文
- `## hyde_abstract`：输入选题文字 → 输出 2-3 句假设性文章摘要（供 HyDE 使用）

已有 prompt 中硬编码的数字（"3-5句"、"3-5个标签"等）改为 `<<<count>>>` 占位符，值来自 `system.yaml`。

### 代码改造：`config_loader.py`

新增共享模块（各服务各自一份，或通过 shared_config 目录提供）：

```python
# config_loader.py
import yaml
from pathlib import Path

_cfg = yaml.safe_load(Path("/app/shared_config/system.yaml").read_text())

def get(path: str, default=None):
    """点分路径读取配置，如 get('retrieval.entity_top_k', 10)"""
    keys = path.split(".")
    v = _cfg
    for k in keys:
        if not isinstance(v, dict):
            return default
        v = v.get(k)
        if v is None:
            return default
    return v
```

---

## 八、从 raw material 重建知识库

### 设计目标

修改 `config/system.yaml` 或 `config/prompts.md` 后，能用已有的 raw 文件重新构建完整知识库（DB + wiki 文件），**无需重新手动上传任何文件**。

### 前提：确定性 ID

当前 article ID 使用 `secrets.token_hex(6)` 随机生成，每次重建产生不同 ID，导致外部引用断裂。

改造方案：文件型 article 的 ID 改为基于 raw 文件路径的 deterministic hash。

```python
import hashlib

def make_article_id(raw_path: str) -> str:
    h = hashlib.sha256(raw_path.encode()).hexdigest()[:16]
    return f"art_{h}"

def make_entity_id(user_id: str, canonical_name: str) -> str:
    h = hashlib.sha256(f"{user_id}:{canonical_name}".encode()).hexdigest()[:16]
    return f"ent_{h}"

def make_index_id(raw_path: str) -> str:
    h = hashlib.sha256(f"idx:{raw_path}".encode()).hexdigest()[:16]
    return f"idx_{h}"
```

用户手动创建的节点（无 raw_ref，`source_type='manual'`）保留随机 ID，rebuild 不触碰它们。

### Rebuild 流程

触发：`docker compose exec api python maintenance.py rebuild_from_raw`

```
Step 1: 清空所有自动生成内容
  DELETE FROM knowledge_nodes   WHERE source_type != 'manual'
  DELETE FROM entity_candidates (全部清空)
  DELETE FROM knowledge_edges   WHERE created_by != 'manual'
  DELETE wiki/articles/*, wiki/indices/*, wiki/summaries/*, wiki/entities/*
  （保留 wiki/config/ 及 source_type='manual' 节点的对应 wiki 文件）

Step 2: 扫描 raw/ 目录，收集所有待处理文件
  raw/pdf/*, raw/plaintext/*, raw/word/*, raw/image/*
  以及 sources 表中 fetch_mode='subscription' 的源的已缓存文件

Step 3: 逐文件重跑 ingestion pipeline
  - 提取文本（按文件类型调用对应 source 模块）
  - 检测是否触发 index+article 切分（词数 > chunk_trigger_words 且有章节结构）
    是：建 index 节点 + 多个 article 子节点（epub/mobi/pdf 按 TOC；其他按 ## 标题）
    否：建单个 article 节点
  - 调用 analyze_article（使用当前 prompts.md 中的 prompt）
  - embed abstract，写入 DB + wiki 文件

Step 4: 实体候选积累 → 批量晋升 → 生成 entity 页 → backfill wikilinks
  （与普通入库后的 process_entity_candidates 流程相同）

Step 5: 生成 default summary（perspective=null）
  对所有 article 和 index，按当前 prompts.md 中的 summary_gen prompt 生成

Step 6: 聚合 index abstract（自底向上）
  先处理叶节点 article，再向上逐层聚合 index 的 abstract
  调用 index_summary prompt

Step 7: 计算统计边
  build_similar_edges for all articles and entities（按 similar_to_threshold）
  build_co_occurs_with for all entity pairs（按 co_occurs_min_articles）

Step 8: 输出报告
  处理文件数、生成 article/entity/summary/index 数、耗时、API 调用次数
```

**Rebuild 的幂等性**：Step 1 确保干净状态；确定性 ID 确保多次重建结果一致。

---

## 九、EPUB / MOBI 格式支持

书籍类格式需要解析内部章节结构，触发 index+article 切分。

### 依赖（`ingestion-worker/requirements.txt` 新增）

```
ebooklib>=0.18    # EPUB 解析
mobi>=0.3.3       # MOBI 解析（标准 MOBIPOCKET 格式）
```

### 解析逻辑（新增 `sources/book.py`）

**EPUB**：
```python
import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup

book = epub.read_epub(file_path)
# 按 spine 顺序提取章节
chapters = []
for item in book.spine:
    doc = book.get_item_with_id(item[0])
    if doc and doc.get_type() == ebooklib.ITEM_DOCUMENT:
        soup = BeautifulSoup(doc.get_content(), 'html.parser')
        chapters.append({
            "title": soup.find('h1') or soup.find('h2') or "章节",
            "text": soup.get_text()
        })
# 书级元数据
title = book.get_metadata('DC', 'title')[0][0]
author = book.get_metadata('DC', 'creator')[0][0]
```

**MOBI**：
```python
import mobi
tempdir, filepath = mobi.extract(file_path)
# 解压后为 HTML，用 BeautifulSoup 解析章节
# 复杂 KFX 格式降级：整体作为单一 article
```

**切块决策**：
```python
total_words = sum(len(ch["text"].split()) for ch in chapters)
if total_words > cfg.ingestion.chunk_trigger_words and len(chapters) > 1:
    # 建 index 节点 + 每章建 article
else:
    # 整体建单一 article
```

`source_type` 统一为 `'book'`，`sources` 表增加 `type='book'` 支持。

### 书级元数据写入 index 节点 wiki 文件

```markdown
---
title: 战争与和平
object_type: index
source_type: book
author: 列夫·托尔斯泰
year: 1869
tags: [俄国文学, 拿破仑战争, 历史小说]
children: [art_abc123, art_def456, idx_ghi789]
abstract: 托尔斯泰的长篇小说，通过五个贵族家庭在1812年拿破仑入侵期间的故事，...
---

## 章节目录

- [[art_abc123|第一章：彼得堡沙龙]]
- [[art_def456|第二章：鲍尔孔斯基家族]]
- [[idx_ghi789|第三部：1812年战役]]
```

---

## 十、数据库 Schema 变更

相比现有 schema，新增以下变更：

```sql
-- 1. knowledge_nodes 新增字段
ALTER TABLE knowledge_nodes
  ADD COLUMN IF NOT EXISTS perspective TEXT,           -- summary 的视角标注
  ADD COLUMN IF NOT EXISTS priority_score FLOAT DEFAULT 1.0,  -- 检索优先级（未来遗忘机制用）
  ADD COLUMN IF NOT EXISTS last_accessed_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS access_count INT DEFAULT 0;

-- 2. knowledge_edges 新增字段
ALTER TABLE knowledge_edges
  ADD COLUMN IF NOT EXISTS description TEXT;  -- relation 的自然语言描述（预留）

-- 3. 新增 wiki 目录
-- wiki/indices/ 对应 object_type='index' 节点

-- 4. object_type 允许值扩展为：'article' | 'index' | 'entity' | 'summary'
-- （移除 'chunk'，新增 'index'）
```

---

## 十一、界面变更（D3 图谱）

| 变更 | 说明 |
|------|------|
| 新节点类型 `index`：颜色 `#8b5cf6`（紫色） | 区别于 article（蓝 #3b82f6）、entity（绿 #10b981）、summary（黄 #f59e0b） |
| `part_of` 边：粗实线，颜色同目标 index 节点 | 强调层级归属关系 |
| `summarizes` 边：黄色虚线 | 体现"提炼"语义 |
| `similar_to` 边：细灰虚线 | 背景关系，不突出 |
| `co_occurs_with` 边：细绿点线 | 统计关系 |
| 新增边类型过滤复选框 | 可隐藏指定 relation_type 的边 |
| ExplorerPanel 新增 `indices/` 段 | 显示 index 节点，可展开子节点 |
| summary tooltip 显示 `perspective` 字段 | 便于区分多视角 summary |

---

## 十二、实施顺序

| 步骤 | 内容 | 是否需要 rebuild |
|------|------|-----------------|
| 1 | 新增 `config/system.yaml`；代码改用 `config_loader.py` 替代硬编码 | 否 |
| 2 | `mentions` 边 weight 改为真实 salience（修复 backfill 硬编码 1.0） | 否（maintenance backfill 可补算） |
| 3 | 新增 `index` object_type；schema migration；wiki/indices/ 目录；ExplorerPanel 更新 | 否（增量添加） |
| 4 | `perspective` 字段 + 多视角 summary API | 否 |
| 5 | 确定性 ID（article/entity/index ID 改为 hash-based） | **需要 rebuild** |
| 6 | `rebuild_from_raw` 命令实现（依赖步骤 5） | — |
| 7 | EPUB/MOBI 解析；book 入库创建 index + article 子节点 | 对已有书文件：rebuild |
| 8 | HyDE 集成；分层检索算法（替换 `drafts.py` 中的 `semantic_search_related`） | 否 |
| 9 | Index abstract 聚合逻辑（底层向上 rollup） | 否（maintenance 任务） |
| 10 | D3 图谱边着色 + index 节点 + filter 面板 | 否 |

**步骤 5（确定性 ID）是 rebuild 功能的关键前置**，建议在步骤 3 之后尽快完成，避免已建立的 index 节点在 rebuild 后 ID 失效。
