# KnowledgeBase-S 发展路线图

> 撰写日期：2026-07-08。基于当时的代码库状态（Phase B 已完成、kb-chat 已拆分、
> `docs/CODE_REVIEW_FINDINGS_2026-07-06.md` 中的 bug 视为独立修复轨道，本文不重复）。
> 配套详细计划：`docs/phase-c-convergence-plan.md`（第一优先级实施计划）、
> `docs/search-review-and-plan.md`(搜索子系统分析与改造计划)。

---

## 0. 定位再确认：拆出 kb-chat 之后，产品已经变了

把 LibreChat 切到独立仓库（`kb-chat`，经 MCP 连接）这一步，实际上把 KB-S 变成了
**headless 知识核心**：

- 它的用户界面不再是网页，而是 **`/api/kb/v1` 契约 + MCP 工具集**。
- Next.js 前端退化为管理后台（入库、整理资料夹、看图谱）。
- 真正的"消费端"是 AI Agent（LibreChat、Claude Desktop、未来的云端客户端）。

**后续所有设计决策都应围绕这个事实展开**：契约稳定性、检索质量、写入能力、
鉴权演进，都是围绕"Agent 是一等公民"来做的。

基于这个定位，后续发展分四个层次，按优先级排列。

---

## 1. 第一优先级：完成架构收敛（还债，而不是加功能）

当前代码库最大的风险不是缺功能，而是**几套并行系统尚未合拢**：

1. **sources/source_items 与 folders/document_instances 双轨并行**，靠
   "同 hex 后缀不同前缀"（`src_XXXX ↔ fld_XXXX`、`si_XXXX ↔ di_XXXX`）的隐式
   约定绑在一起（`routers/folders.py` 头部注释即是这套约定的文档）。这种映射
   没有外键保证、没有事务保证，是 review 中 B6/B7/B8 这类孤儿数据 bug 的
   **结构性根源**——修单个 bug 治标，收敛双轨才治本。
2. **wiki 文件与 DB 的主从关系未裁决**：`restore_from_wiki`（wiki 是事实源）与
   `rebuild_from_raw`（raw 是事实源）两条重建路径并存，外加 A3 指出的双写入器
   （API `kb/wiki.py:write_wiki_node` vs worker `pipeline.py:write_wiki_article`）。
3. **raw_ref 列半退役**：Phase 5 已把读路径迁走，但列还在、7 个模块还在引用。

**目标状态（Phase C）**：

- folders/document_instances 成为唯一写入模型；source_items 降级为 pipeline
  内部处理队列，不再暴露 API。
- ID 映射从"字符串约定"变成显式外键。
- **裁决：DB + raw 层（raw_assets + 提取文本）是事实源，wiki 文件降级为派生
  产物**（可随时从 DB 重新渲染）。理由：本系统内容全部来自外部抓取而非手写
  markdown，这与 gbrain（人写 markdown 为事实源）的场景根本不同——
  `docs/gbrain-inspiration.md` 其实已经得出了这个判断，只差落地。
- 把"事实 / 派生 / 运行态"三层分类正式化为 ADR，备份只保证事实层，并用
  `rebuild_derived` 演练验证"派生层真的可以全量重建"。

详细实施步骤、顺序与验证标准见 **`docs/phase-c-convergence-plan.md`**。

这一步做完，代码库复杂度显著下降，后面所有新功能都站在干净的地基上。

---

## 2. 第二优先级：建立检索评测体系——最高杠杆的一笔投资

KB-S 的核心价值主张是"高质量检索与推理支持"，但目前检索质量完全靠感觉：
`0.75/0.25` 向量权重、`+0.15` 关键词加成、`0.3` timeline 阈值、`0.9/0.7/3`
实体晋升条件——没有任何一个数字是可验证的。在这种状态下做 RRF、reranker、
graph augment 都是盲调。

在改进检索**之前**，先做两件事：

1. **金标准评测集 + 评测脚本**。收集 50–100 个真实查询（法律/合规领域），标注
   每个查询应命中的节点，脚本输出 recall@k / MRR / nDCG。以后任何检索改动，
   先跑评测再合并。一两百行代码，但把"检索优化"从玄学变成工程。
2. **MCP 工具调用日志**。在 kb-mcp 或 public_service 层记录每次调用：query、
   返回节点、耗时。这是免费的真实使用数据——Agent 实际问了什么、search 返回
   的东西 Agent 后续有没有 fetch，都能挖出评测样本和失败案例。现在这层完全
   是黑盒。

有评测基线之后，检索栈演进方向（按预期收益排序）：

- **真正的关键词检索**：Postgres 原生 FTS（tsvector + ts_rank，中文考虑
  pg_jieba / zhparser），与向量结果做 **RRF 融合**，替代硬编码权重。
- **块级检索（chunking）**：对法律领域最关键。embedding 目前建在文章
  abstract 级，但 `cite` 要找的是**条款级**精确引语。给长文档
  （regulation/case）增加带条款锚点的段落级 chunk 层，cite Stage 1 从
  "20 篇候选文章"变成"top 候选条款"，准确率和 LLM 成本同时改善。
- **graph augment 纳入 search 工具本身**（分层检索 Phase 2-3 已有雏形），
  并把 `why_matched` 扩展成可解释 trace（向量/关键词/图扩展/哪条边）。
- cross-encoder rerank 等前面几项收益吃完再上。

搜索子系统现状的详细分析与改造计划见 **`docs/search-review-and-plan.md`**。

---

## 3. 第三优先级：让 Agent 从"读者"变成"作者"——写入面 MCP 工具

现在 MCP 纯只读，在架构收敛完成前这是正确的克制。但 headless 知识库的自然
演进方向是：**对话中产生的知识要能回流**。典型场景：

- Agent 在对话中分析了一个案例，用户说"把这个结论存进知识库"；
- 用户在聊天里贴 URL，Agent 直接调 `ingest_url` 入库到指定资料夹；
- Agent 发现两个 entity 是同一家公司，建议合并（人工确认后执行）。

对应工具：`add_note`（对话产出的 memo 入库）、`ingest_url`、`add_summary`
（带 perspective 的摘要写入）、`suggest_entity_merge`（只建议不执行）。

写入面**不能**只是把现有内部端点挂上 service token，两个前置设计：

1. **来源归因（provenance）**：每个节点/摘要要能回答"这是谁写的"——pipeline
   自动、用户手动、还是 Agent 对话产出。加一个 `provenance` 字段（枚举 +
   会话引用）成本很低，但它是日后信任和清理的基础。
2. **分层信任模型**：沿用"端点挂哪个鉴权依赖决定信任边界"的现有思路——
   追加型写入（note、summary）给 MCP token；改写/破坏型（merge、delete）
   只给建议权，执行仍走 cookie 认证的管理界面。配合 entity 已有的
   "compiled truth（abstract 可重写）+ timeline（entity_facts 只追加）"模式，
   写入面天然安全：Agent 往 timeline 追加，abstract 由 refresh_stale 机制消化。

配套的**鉴权 Phase 2**：单一 `MCP_STATIC_TOKEN` 在只读时代够用，一旦有写入
就需要 per-client token（LibreChat / Claude Desktop / 云端各一个，可单独吊销）
+ 调用审计日志；要接 Claude.ai 云端 connector 则需要 OAuth。建议顺序：先做
per-client token + 审计（约一天工作量），OAuth 等真有云端接入需求再做。

---

## 4. 第四优先级：领域纵深——法律知识库区别于通用 RAG 的地方

前三层做完，KB-S 是一个优秀的通用知识库。它标榜的领域是法律/合规，这个领域
有通用 RAG 不具备的结构，值得长期投入：

- **法规的时间有效性**：法规会修订、废止。`published_at` 只表达"发布时间"，
  法律文档需要 `effective_from / repealed_at` 有效期区间，检索支持"在某时点
  哪个版本有效"。可建在 doc_kind=regulation 之上，作为 article_nodes 扩展
  字段 + timeline 工具的新过滤器。
- **引证图谱**：判例引用法规、法规引用上位法——这是 `mentions` 之外的高价值
  边（`cites`），入库分析时让 LLM 顺带抽取，cite 工具可信度直接受益。
- **法规修订对比**：`compare` 工具 + 版本链，"新旧条文对照"是合规高频需求，
  现有 compare 基础上是低成本增量。

这些不急，但它们决定这个项目三年后是"又一个 RAG demo"还是有壁垒的领域知识
系统。

---

## 5. 明确不做的事

按 CLAUDE.md 的简单性原则，抵制以下诱惑：

| 不做 | 理由 |
|---|---|
| 多租户 | ADR 0001 已定单租户，`user_id` 只是脚手架，不为想象中的多用户加隔离逻辑 |
| 再造聊天界面 | kb-chat 用 LibreChat 是对的，前端只做管理后台 |
| 进一步微服务化 | api / ingestion-worker / job-worker / kb-mcp 的拆分已经够了（job-worker 与 api 共镜像是好设计） |
| 投机性抽象 | 通用插件系统、多向量库适配层等。pgvector 单库能撑到远超个人知识库规模的量级 |

---

## 6. 执行顺序总览

```
近期（先做）：
1. Phase C 双轨退役 + wiki 降级为派生物   → 验证：rebuild_derived 演练通过；legacy 端点删除后全部测试绿
2. 事实/派生分层 ADR + 备份对齐            → 验证：仅备份事实层可完整恢复
3. 检索评测集 + MCP 调用日志               → 验证：能输出 recall@k 基线报告

中期：
4. 搜索收敛与质量改造（FTS + RRF、chunk 级 cite） → 验证：评测指标提升
5. per-client token + 审计日志
6. 写入面 MCP 工具（note / ingest_url，带 provenance）

远期：
7. 法规时间有效性 + cites 引证边
8. graph-augmented search、OAuth（按需）
```

**核心逻辑一句话**：先把双轨系统合拢、让派生层可重建（地基），再用评测体系把
检索质量变成可度量的工程（核心竞争力），然后开放受控的写入面让知识回流
（产品闭环），最后做法律领域的纵深（壁垒）。
