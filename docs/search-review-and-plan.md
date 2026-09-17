# 搜索子系统：现状分析与改造实施计划

> 撰写日期：2026-07-08。上游文档：`docs/roadmap.md` 第 2 节。
> 范围：查询侧检索链路（search / layered_retrieval / embedding），不含
> ingestion 侧的实体发现与摘要生成。

---

## Part 1 现状分析

### 1.1 总评

**概念设计清晰、有品位，工程实现的 robust 程度跟不上设计野心。**
检索的概念架构（summary-first、图传播、HyDE、可解释 why_matched、参数全外置）
放在个人项目里属于上游水平，且 MEMORY.md 文档与实现同步。但实现层存在
三套并行入口、四处复制的打分公式、两种 SQL 纪律、静默降级——这是一次
没有收尾的重构留下的状态：**能工作，但坏了你不知道**。

### 1.2 三套并行实现（最大的结构性问题）

| 实现 | 位置 | 特性 | 消费方 |
|---|---|---|---|
| `/api/kb/v1/search` | `kb/public.py:159` | 向量 + ILIKE 关键词加成(+0.15)；doc_kind/tags/source_ids/date 过滤；**无**时间基准选择 | kb-mcp → LibreChat / Claude Desktop |
| `kb_search` 工具 | `kb_tools.py:223` | 纯向量（无关键词加成）；time_basis/lookback_hours/sort；**整段 try/except 静默降级为 ILIKE** | READ_ONLY_TOOLS 消费方 |
| `layered_retrieval` | `kb/public_service.py:111` | 五阶段图增强检索 | compare / cite / summarize_corpus 内部 RAG |

伴随的复制：

- **embedding/HyDE 逻辑两份**：`kb/retrieval.py:14-38`（重构后正宗）与
  `kb_tools.py:96-119`（一模一样的私有拷贝）；`retrieval.py:41-45` 的
  backward-compat 别名说明迁移做了一半。
- **打分公式 `0.75*body + 0.25*perspective` 至少四处**：`public.py:229`、
  `kb_tools.py:265`、`public_service.py:141`、`public_service.py:304`。

后果：调整打分公式或换 embedding 模型时几乎必然漏改一份，同一查询在 MCP
工具和内部 RAG 返回不同结果，且没有测试会发现。

### 1.3 健壮性问题清单（按严重度）

| # | 问题 | 位置 | 后果 |
|---|---|---|---|
| R1 | 字符串拼 SQL：`exclude_ids`、`user_id`、节点 ID 列表全程 f-string 拼进 SQL | `public_service.py:132-133,159,180,216,235,...`；`kb_tools.py` 部分 | 注入面（IDs 来自 API 请求体）；与 `public.py` 的参数化纪律双重标准 |
| R2 | 静默吞异常：向量检索整体 `except Exception` 降级为 ILIKE、score=NULL，无日志；HyDE `except: pass` 同样无日志 | `kb_tools.py:281`；`retrieval.py:36` | 故障伪装成"结果变差"——DB 断连/SQL bug/OpenAI 挂了都表现为检索质量下降 |
| R3 | 关键词层名不副实：`ILIKE '%整个query%'` 整句子串匹配 | `public.py:183,236` | 多词查询几乎不命中；对 `s.body` 的 `%..%` 无法走索引；+0.15 二元加成与 0.3 阈值均为拍脑袋值。实际贡献可能≈0 |
| R4 | 分数代数不自洽：entity→article **加法**、summary→article 加法、二跳 **max**、index 展开**乘法**、兜底 **×0.5** 混在同一字典排序 | `public_service.py:213,227,247,323,334` | 分数无可解释含义，调任何 damping 参数排序变化不可预测 |
| R5 | HNSW 索引实际用不上：所有 ORDER BY 都是 COALESCE/CASE/加权表达式 | 三处 search 全部 | 迁移 0003 建的索引形同虚设，全表扫描；量级到数万节点后显形 |
| R6 | 结果不去重：不限定 type 时，同一文章的 summary 节点与 article 节点同时出现 | `public.py:search` | Agent 看到重复，挤占 top_k |
| R7 | 零可观测性：整条链路无日志——query、结果、HyDE 是否生效、是否降级均不记录 | 全链路 | 无法评测、无法排障 |
| R8 | index 展开 N+1：循环内逐 index 查子节点 + 两个向量查询 | `public_service.py:284-321` | index 多时延迟放大 |
| R9 | wiki 文件耦合：读正文靠硬编码 `"## 关联节点"` 标记切分 | `kb_tools.py:138` | Phase C wiki 渲染格式一变即断 |
| R10 | 打分阈值/权重全部未验证 | `config/system.yaml` retrieval 区 | 没有评测基线，所有调参是盲调 |

### 1.4 做得好的、要保留的

- summary-first 分层 + 图传播的**概念**（five-phase）——保留思路，重写融合语义。
- HyDE 带降级——保留，补日志。
- `why_matched` 可解释性——保留并升级为 trace。
- 参数外置到 `config/system.yaml`——保留。
- `public.py` 的参数化 SQL 纪律与 CTE 复用注释——推广到全部模块。

---

## Part 2 改造实施计划

原则：**先度量（S0），再收敛（S1/S2，不改行为），最后才改质量（S3，用评测
证明提升）**。S0–S2 完成前不做任何"让搜索变好"的改动。

---

### S0. 评测与日志先行（其他一切的前提）

**S0.1 检索日志**

- 在三个入口各加结构化日志（JSON 行，落 stdout 由 docker 收集）：
  `{ts, entry, query, filters, hyde_used, degraded, top_ids, top_scores, latency_ms}`。
- HyDE 与 kb_tools 降级路径的 `except` 里必须打 `logger.warning`（保留降级
  行为，消灭静默）。
- kb-mcp 侧记录工具调用：工具名、入参、返回节点 id 列表（为挖掘"search 返回
  了但 Agent 没 fetch"的负样本）。

**S0.2 金标准评测集**

- `evals/search_golden.yaml`：50–100 条真实查询，每条标注 `relevant_node_ids`
  （分 must-hit / nice-to-hit 两级）。来源：自己的高频问题 + S0.1 日志挖掘。
- `evals/run_search_eval.py`：对每条查询打 `/api/kb/v1/search`，输出
  recall@5 / recall@10 / MRR / nDCG@10，结果落 `evals/reports/{date}.md`。
- 跑出**基线报告**并提交入库——这是后续所有改动的对照组。

验证：基线报告存在；人为制造一次 OpenAI 故障，日志中出现 degraded=true 而非
无声降级。工作量：2 天。

---

### S1. 收敛三套实现（纯还债，行为不变）

**目标**：一个检索核心，三个薄入口。

1. **`kb/retrieval.py` 升级为检索核心模块**，新增：
   - `vector_search(conn, query_vec, *, object_types, filters, top_k) -> list[Hit]`
     ——唯一一份向量检索 SQL（含 summary 的 0.75/0.25 组合打分），
     全参数化（见 S2）。
   - `SCORE_SQL_SUMMARY / SCORE_SQL_PLAIN` 常量——打分公式单点定义。
2. **`kb_tools.py`**：删除 `_embed_text/_embed_query` 私有拷贝（改 import
   `kb.retrieval`）；`search()` 改为调用核心 `vector_search` + 自己的时间过滤
   包装；删除整段 `except Exception` 降级（改为：embedding 失败才走 ILIKE
   兜底，且必须打日志——这是原兜底的真实意图）。
3. **`kb/public.py:search`**：向量部分改调核心；关键词加成逻辑暂时原样保留
   （S3 再替换）。
4. **`kb/public_service.py:layered_retrieval`**：内部 `_vec_search` 删除，
   改调核心；五阶段逻辑本身此步不动。
5. **删除 `retrieval.py:41-45` 的 backward-compat 别名**（调用方已迁完）。
6. **一致性回归**：S0 评测跑一遍，指标与基线完全一致（允许浮点噪声）；
   grep 验收：`0.75 *` 打分公式仅出现在 `kb/retrieval.py` 一处。

工作量：1–2 天。风险：低（行为不变 + 评测对照）。

---

### S2. SQL 安全 + 剩余可观测性（半天到一天）

1. `public_service.py` / `kb_tools.py` 全部 f-string SQL 值改参数化：
   ID 列表用 `= ANY($n::text[])`，`user_id`/`exclude_ids` 进参数。
   embedding 向量字面量保留（pgvector 惯例），但必须来自服务端 embed 结果、
   永不拼接用户输入。
2. grep 验收：`grep -rn "f\"'{" services/api/kb/` 为空；
   `", ".join(f"'{` 模式为空。
3. 给 `layered_retrieval` 加阶段耗时日志（phase1a/1b/1c/2/3/4/5 各自
   candidates 数与耗时），为 S3 的 N+1 修复提供依据。

验证：评测指标不变；渗透性单测——`exclude_ids=["x') OR 1=1 --"]` 之类
恶意输入返回正常空结果而非 SQL 错误。

---

### S3. 质量改造（每项独立、逐项过评测门槛）

以下每项单独分支、单独跑评测，**指标不升不合**。预期收益从高到低排序。

**S3.1 真正的关键词层：Postgres FTS 替换 ILIKE**

- Alembic revision：`knowledge_nodes` 加 `fts tsvector` 生成列
  （`to_tsvector('simple', title || ' ' || abstract)`；中文效果不足时再评估
  zhparser/pg_jieba，先用 simple + 分词后的 query 验证收益）+ GIN 索引；
  summary body 同理（在 `summary_nodes`）。
- 查询侧：query 分词（英文空格切 + 中文按字/jieba），`websearch_to_tsquery`
  / `plainto_tsquery`，`ts_rank` 得分。
- **RRF 融合替换 +0.15**：向量 top-50 与 FTS top-50 各自排名，
  `score = Σ 1/(60+rank)`；`why_matched` 由两个列表的成员关系直接推导
  （比 0.3 阈值诚实）。
- 配置：`retrieval.fusion: rrf`，保留 `legacy` 开关一个周期便于 A/B。

**S3.2 让 HNSW 生效：两段式排序**

- 第一段：裸 `embedding <=> $vec` ORDER BY（可走 HNSW）取 top-N 候选
  （N = top_k × 10，配置化）；
- 第二段：仅对候选集计算组合分（summary 0.75/0.25、RRF 融合）再排序。
- 验证：`EXPLAIN ANALYZE` 显示 Index Scan (hnsw)；评测指标不降；
  记录 P95 延迟对比。

**S3.3 结果去重（summary ↔ article 折叠）**

- search 返回前按"summary 折叠到其 summary_of 文章"去重：同组取分高者，
  被折叠者的 id 放进结果的 `also_matched` 字段（Agent 仍可见但不占名额）。

**S3.4 layered_retrieval 融合语义统一**

- 五阶段各自产出**独立的候选排名列表**（vector-summary / vector-entity /
  graph-propagation / expansion / direct），统一用 RRF 融合替换现在的
  加法/乘法/max 杂烩；damping 参数转化为各列表的 RRF 权重（可解释、可单调
  调参）。
- 顺带修 R8 N+1：index 子节点查询合并为一条 `= ANY(...)` 批量查询。
- 验证：cite / summarize_corpus 的端到端行为用评测集的引证子集衡量
  （cite 的 quote 命中率不降）。

**S3.5 chunk 级检索（法律领域的关键一步，可放到 S3 末尾单独立项）**

- 新表 `chunks(id, article_id FK, seq, anchor TEXT, text, embedding vector)`：
  对 doc_kind ∈ {regulation, case} 的长文按条款/段落切分（anchor 存"第 X 条"
  类结构锚点），ingestion 时生成。
- `cite` Stage 1 改为 chunk 向量检索（top 候选条款直接携带精确原文），
  Stage 2 的 LLM 验证与 substring 校验逻辑不变——候选粒度变细后，
  幻觉引语被丢弃的比例应显著下降。
- search 不改（文章级结果对 Agent 更友好），chunk 只服务 cite 与未来的
  条款级问答。
- 验证：评测集中引证类查询的 quote 命中率、cite 平均 token 消耗对比。

---

### S4. 收尾

1. `why_matched` 升级为 `match_trace`：`{vector_rank, keyword_rank,
   graph_path?}`，向后兼容保留 why_matched 字符串字段。
2. `kb_tools.py:_read_wiki_body` 的硬编码标记切分，随 Phase C 的 wiki 渲染
   单点化一并处理（改为 API 内部直接取正文数据，不再解析 md）。
3. `config/system.yaml` retrieval 区清理：删除不再使用的 damping 参数，
   新参数（rrf_k、candidate_multiplier、fusion 开关）补注释。
4. MEMORY.md 检索章节重写（当前描述的 +0.15 公式届时已不存在）。

---

## Part 3 排期与完成定义

| 阶段 | 内容 | 预估 | 合并门槛 |
|---|---|---|---|
| S0 | 日志 + 评测集 + 基线报告 | 2 天 | 基线报告入库 |
| S1 | 三套实现收敛 | 1–2 天 | 评测与基线一致；公式单点 grep 验收 |
| S2 | SQL 参数化 + 阶段日志 | 1 天 | 注入单测通过；评测不变 |
| S3.1 | FTS + RRF | 2 天 | 评测指标 ↑ |
| S3.2 | 两段式排序（HNSW） | 1 天 | EXPLAIN 走索引；指标不降；延迟 ↓ |
| S3.3 | 去重折叠 | 0.5 天 | 指标不降 |
| S3.4 | layered RRF 化 + N+1 | 2 天 | cite 子集指标不降 |
| S3.5 | chunk 级 cite | 3 天 | 引证命中率 ↑ |
| S4 | trace / 配置 / 文档收尾 | 1 天 | — |

**完成定义**：

1. 检索核心逻辑（打分、embed、HyDE）在 `kb/retrieval.py` 单点存在，
   三个入口均为薄封装；
2. 全链路无静默降级（每条降级路径有日志与计数）；
3. `services/api/kb/` 下无字符串拼接 SQL 值；
4. 评测报告显示 recall@5 / MRR 相对基线提升，且每次检索改动的 PR 附带
   评测对比；
5. `EXPLAIN` 确认主查询走 HNSW 索引。
