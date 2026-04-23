# 系统架构说明

## 1. 与 RAG 的异同

### 相同点

| 要素 | 标准 RAG | KnowledgeBase-S |
|------|---------|-----------------|
| Embedding 向量化 | chunks → embedding | `abstract` → embedding（text-embedding-3-small, 1536d） |
| 向量数据库 | pgvector / Chroma 等 | pgvector |
| 检索增强生成 | 检索 top-k → 喂给 LLM | 草稿生成时从 KB 检索相关节点喂给 Claude |
| LLM 生成 | GPT / Claude | Claude（Haiku / Sonnet） |

### 本质差异

**RAG 是检索机制，KnowledgeBase-S 是知识培育 + 内容生产系统，RAG 只是其中一个组件。**

**1. 知识表示不同**
- RAG：文本切块（chunks），扁平、无结构
- 本系统：Article / Entity / Summary 三类一等对象，通过 wikilink `[[entity_id|text]]` 构成图谱，Entity 有 `canonical_name`、`aliases`、`salience` 等结构化属性

**2. 知识是动态演化的，不是静态快照**
- RAG：一次性入库，不主动更新
- 本系统：Entity 通过累积 mentions/salience 按阈值"晋升"，每次新文章进来 entity page 会增量更新，还有矛盾检测机制

**3. 目的是内容生产，不是问答**
- RAG 的目标：回答用户的问题
- 本系统：每日从新增文章提炼"选题" → 用户选择角度 → 生成草稿 → 用户修改反馈 → 系统学习写作偏好 → 下次草稿更符合个人风格

**4. 有持续的个性化反馈环**
- RAG：无此机制
- 本系统：`feedback-worker` 分析草稿 diff，提炼写作偏好规则，注入后续草稿生成

**5. 检索粒度不同**
- RAG：检索"文本片段"
- 本系统：检索已经过 LLM 提炼和结构化的知识单元（article abstract、entity page、summary），信息密度更高

---

## 2. 新文章入库后的完整流程

### 步骤概览

```
新文章入库
    │
    ├─ embed 原文 → 拉最近 entity + 候选上下文
    │
    ├─ Claude 分析 → { abstract, tags, entities[] }
    │
    ├─ 对每个 entity：
    │    ├─ 匹配已有 → 追加 source_node_ids（仅打标，不重新生成页）
    │    └─ 新词 → upsert 候选池 + 立即检查晋升
    │         └─ 晋升 → 生成 entity 页 → 回灌历史 wikilink
    │
    └─ 写 wiki 文件（article + summary）

Maintenance（定期手动）
    ├─ 再次扫候选池，补漏未晋升的
    ├─ entity_update：已有 entity 页基于新来源增量更新
    ├─ 孤儿 entity 标记
    └─ 图结构修复（孤岛 / 补边 / 矛盾检测）
```

### 步骤一：分析前的上下文准备

文章文本先做一次初始 embedding，然后拿这个向量查询两份上下文列表，注入 `article_analysis` prompt：

1. **最近的 20 个已有 entity 节点**（向量距离最近，语义最相关）
2. **候选池里 mention 数最多的 20 条候选**（还没建页但反复出现的词）

目的：让 Claude 识别"这个实体其实跟已有的某个实体是同一个"，避免重复建页。

### 步骤二：Claude 分析文章（`article_analysis` prompt）

返回 JSON：

```json
{
  "abstract": "3-5句核心摘要，用于后续 embedding",
  "tags": ["标签1", "标签2"],
  "entities": [
    {
      "name": "实体规范名",
      "aliases": ["别名"],
      "salience": 0.85,
      "matches_existing_entity_id": "entity_xxx 或 null",
      "summary_hint": "一句话描述，供生成 entity 页时参考"
    }
  ],
  "contradictions": [...],
  "structural_hints": [...]
}
```

### 步骤三：处理 entity 候选

**情况 A — 匹配已有 entity（`matches_existing_entity_id` 非 null）**

只追加来源：
```sql
UPDATE knowledge_nodes
SET source_node_ids = array_append(source_node_ids, :article_id)
WHERE id = :existing_entity_id
```
不重新生成 entity 页（页面更新由 Maintenance 负责）。

**情况 B — 新词，进候选池**

Upsert `entity_candidates` 表，追加一条 mention 记录 `{article_id, salience, seen_at}`。同一篇文章只记录一次。

### 步骤四：晋升条件检查（每次 upsert 后立即检查）

| 条件 | 说明 |
|------|------|
| `max_salience >= 0.9` | 单篇文章中极度突出，立即晋升 |
| `max_salience >= 0.7` 且 `mention_count >= 2` | 中等显著度 + 出现过两次 |
| `mention_count >= 3` | 不论显著度，三篇文章都提过 |

### 步骤五：晋升 → 生成 entity 页

1. 拉取来源文章 abstract（最多 5 篇）
2. Claude `entity_page` prompt 生成维基百科风格 Markdown（200-500 字）
3. 写入 DB：`knowledge_nodes`，`object_type='entity'`
4. 写入文件：`wiki/entities/{entity_id}.md`
5. 标记候选：`entity_candidates.promoted_entity_id = entity_node_id`

### 步骤六：wikilink 回灌

新 entity 晋升后，扫描**所有历史 article wiki 文件**：

- 找到正文中 `canonical_name` 或任意 alias 的**第一次出现**
- 替换为 `[[entity_id|原文字面]]`
- 在 `knowledge_edges` 表插入 `wikilink` 类型的边
- 把该 article ID 追加到 entity 的 `source_node_ids`

### 候选池的来源

`entity_candidates` 是**被动积累**的，没有专门的生成步骤——完全来自历次文章入库时 Claude 识别但尚未晋升的 entity：

```
第1篇文章提到"数据安全法" → 插入候选池，mentions=[{article_1, salience:0.6}]
第2篇文章提到"数据安全法" → 追加，mentions=[{...}, {article_2, salience:0.5}]
第3篇文章提到"数据安全法" → 追加，mentions=[{...}, {...}, {article_3, salience:0.8}]
                              mention_count=3 → 触发晋升
```

查询时按 mention 数倒排取前 20，只看 `promoted_entity_id IS NULL` 的行。

---

## 3. 旧有文章的 entity 调整

### 有的调整

| 操作 | 触发时机 | 做什么 |
|------|---------|--------|
| 新文章提到已有 entity | 每次入库 | entity 的 `source_node_ids` 追加新文章 ID |
| 新 entity 晋升后回灌 | 每次有 entity 晋升 | 历史文章 wiki 文件注入 wikilink |
| entity 页内容更新 | Maintenance 手动触发 | 基于新来源 abstract 增量更新 entity 页，冲突处标注 `⚠️ 待核实` |
| 孤儿标记 | Maintenance 运行时 | `source_node_ids` 为空的 entity 打 `orphan` tag |

### 没有的（当前限制）

| 操作 | 当前状态 |
|------|---------|
| 自动合并重复 entity | 不存在。依赖 Claude 在分析时通过 `matches_existing_entity_id` 防止重复，但若向量距离不近仍可能产生重复，需手动合并 |
| 自动删除 entity | 不存在。只有 `orphan` 打 tag |
| 旧有文章触发 entity 页自动更新 | 不自动触发。新文章入库仅追加 `source_node_ids`，页面更新仅在 Maintenance 时执行 |
| 重新分析旧文章的 entity 归属 | 不存在。文章入库时的分析是一次性的 |

> **设计取舍**：entity 页内容更新不是实时的，而是通过 Maintenance 批量处理。原因是每篇文章入库时同步更新所有相关 entity 页代价太高（一篇文章可能匹配十几个 entity，每个都要调一次 Claude）。
