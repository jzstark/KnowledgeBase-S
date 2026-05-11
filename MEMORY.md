# KnowledgeBase-S 系统说明（按当前代码更新）

本文档描述 **当前仓库已经实现的系统**，不是纯规划稿。  
如果本文与代码冲突，以 `services/`、`config/`、`docker-compose*.yml` 为准。  
`multi-layer-plan.md` 记录的是目标架构，其中一部分已经落地，另一部分仍在过渡。

---

## 1. 系统定位

KnowledgeBase-S 是一个 **单用户** 的个人知识库与 AI 辅助写作系统，目标不是通用问答，而是把“采集资料 → 结构化沉淀 → 生成选题 → 写草稿 → 从修改中学习偏好”做成一个闭环。

当前主流程：

1. 用户创建 source，系统从 RSS / 微信 / URL / 文件 / 电子书导入内容。
2. ingestion-worker 清洗文本、提取 abstract / tags / entity candidates，并写入知识库。
3. 系统把知识内容同步为本地 Markdown wiki，供前端查看，也可作为 Obsidian vault 使用。
4. `/api/briefing/generate` 基于近期新增的 **主要来源文章** 生成当天选题。
5. 用户选择一个或多个选题，系统通过分层检索从知识库拼装上下文，调用 Claude 生成草稿。
6. 用户提交定稿后，feedback-worker 从 diff 中提炼写作偏好，写回 `writing_memory`。

另外，前端带有一个持久化聊天侧栏，但它目前是 **普通 Claude 会话**，并没有接入知识图谱检索。

---

## 2. 当前架构

### 2.1 服务拆分

| 服务 | 作用 | 备注 |
| --- | --- | --- |
| `web` | Next.js 14 前端 | 主 UI，含知识库、来源、选题、草稿、设置、聊天侧栏 |
| `api` | FastAPI 主后端 | 认证、sources、kb、briefing、drafts、chat、settings、files |
| `ingestion-worker` | 内容抓取与入库 | 支持 HTTP trigger 和轮询模式 |
| `feedback-worker` | 草稿 diff 分析 | 提炼写作偏好规则 |
| `summarizer-worker` | 定时触发简报生成 | 只负责调用 API，不承担摘要逻辑 |
| `maintenance-worker` | 周期维护 | 复用 `api` 镜像执行 `maintenance.py` |
| `postgres` | 主数据库 | `pgvector/pgvector:pg16` |
| `rsshub` | RSSHub | 供微信等订阅源使用 |
| `nginx` | 反向代理 | 代理前端、API，并转发 `/agent/` 到宿主机 18789 |
| `watchtower` | 镜像自动更新 | 生产用 |

### 2.2 部署形态

- 生产入口是 `docker-compose.yml`
- 开发叠加 `docker-compose.dev.yml`
- `workers` profile 包含 `ingestion-worker`、`summarizer-worker`、`feedback-worker`
- `maintenance` profile 包含 `maintenance-worker`
- `watchtower` 在 dev compose 中被禁用

### 2.3 认证方式

- 单用户模式，固定 `USER_ID = "default"`
- 登录密码来自 `AUTH_PASSWORD`
- 登录成功后写入 HttpOnly cookie `token`
- JWT 签名密钥来自 `AUTH_SECRET`
- Token 有效期 7 天
- Next.js middleware 会保护除 `/`、`/login`、`/api/auth/login` 外的大部分页面

### 2.4 仓库分层

从代码组织上看，这个仓库可以分成 6 层：

| 层 | 目录 | 职责 |
| --- | --- | --- |
| 入口层 | `docker-compose*.yml`、`nginx/`、`Makefile`、`deploy.sh` | 进程编排、反向代理、开发/部署入口 |
| 共享配置层 | `config/` | 系统参数、Prompt、图片 OCR 参数 |
| API 层 | `services/api/` | DB 初始化、认证、业务路由、维护脚本 |
| Worker 层 | `services/ingestion-worker/`、`services/feedback-worker/`、`services/summarizer-worker/` | 异步/离线任务 |
| Web 层 | `services/web/` | Next.js 页面、组件、知识库工作台、聊天 UI |
| 持久化层 | `user_data/`、Postgres | 原始文件、wiki 文件、用户配置、数据库记录 |

真正决定系统行为的核心代码主要集中在：

- `services/api/routers/`
- `services/ingestion-worker/pipeline.py`
- `services/api/maintenance.py`
- `services/web/app/`

### 2.5 服务启动入口

各服务的代码入口和职责边界如下：

| 服务 | 代码入口 | 启动行为 |
| --- | --- | --- |
| `api` | `services/api/main.py` | 组装 FastAPI，lifespan 中执行 `database.init()` |
| `web` | Next.js App Router | 由 `services/web/app/layout.tsx` 作为全局壳层 |
| `ingestion-worker` | `services/ingestion-worker/main.py` | 同时提供 trigger server 和轮询循环 |
| `feedback-worker` | `services/feedback-worker/main.py` | 暴露 `/analyze`，负责 diff -> 偏好规则 |
| `summarizer-worker` | `services/summarizer-worker/main.py` | 登录 API 后触发 `/api/briefing/generate` |
| `maintenance-worker` | `services/api/maintenance.py` | 复用 API 镜像执行维护脚本 |

这意味着当前系统不是“每个服务都完全自治”的架构，而是：

- `api` 是主系统入口
- 其他 worker 多数通过 HTTP 调用 `api`
- `maintenance.py` 虽然位于 API 目录，但在运行形态上更像后台脚本

### 2.6 运行时调用关系

当前运行时通信方式主要是 **HTTP + 共享数据库 + 共享文件目录**，而不是消息队列。

关键调用链可以概括为：

```text
Browser
  -> nginx
    -> web
      -> api
        -> postgres
        -> user_data/

Browser / iPhone Shortcut
  -> api /sources/*
    -> ingestion-worker /trigger/*
      -> api /kb/*
        -> postgres
        -> user_data/

Browser
  -> api /drafts/{id}/feedback
    -> feedback-worker /analyze
      -> api /kb/memory/feedback
        -> postgres

summarizer-worker
  -> api /auth/login
  -> api /briefing/generate
```

3 种核心协作介质：

1. HTTP API  
2. Postgres  
3. `user_data/` 共享目录

### 2.7 API 内部模块结构

`services/api/` 可以再分成 5 类模块：

| 模块 | 文件 | 职责 |
| --- | --- | --- |
| 应用入口 | `main.py` | FastAPI app、router 注册、auth/health |
| 基础设施 | `database.py`、`auth.py` | schema/init、JWT 认证 |
| 配置加载 | `config_loader.py`、`prompt_loader.py` | 读取 `config/` 挂载内容 |
| 业务路由 | `routers/*.py` | sources、kb、briefing、drafts、files、settings、chat |
| 脚本模块 | `maintenance.py` | 图谱维护、恢复/重建 |

当前 router 的业务分工是：

| Router | 负责内容 |
| --- | --- |
| `sources.py` | source CRUD、文件上传、URL 入队、微信 push、抓取触发 |
| `kb.py` | 节点入库、wiki 同步、图谱/节点查询、summary 创建、memory、entity candidates |
| `briefing.py` | 今日选题生成与状态更新 |
| `drafts.py` | 分层检索、草稿生成、草稿历史、反馈提交 |
| `settings.py` | 用户设置、topics/schema、模板、导出 |
| `files.py` | `user_data/wiki` 与 `user_data/config` 文件读写 |
| `chat.py` | 聊天会话和 SSE 消息流 |

两个需要特别记住的事实：

- `kb.py` 是 **正式的知识节点写入口**，ingestion-worker 最终写库走 `/api/kb/ingest`
- `drafts.py` 不只是 CRUD，它还承载了当前分层 retrieval 的主实现

### 2.8 Worker 内部模块结构

#### ingestion-worker

`services/ingestion-worker/` 由 4 部分构成：

| 模块 | 作用 |
| --- | --- |
| `main.py` | trigger server + 轮询控制 |
| `pipeline.py` | 统一入库流程 |
| `sources/*.py` | 各 source 类型的抓取与文本抽取 |
| `config_loader.py` / `prompt_loader.py` | 读取共享配置 |

它的内部设计是“source 适配层”和“知识生成层”分离：

- `sources/*.py` 负责 `fetch_new_items()` / `extract_text()`
- `pipeline.py` 负责 abstract、embedding、entity、wiki、backfill、post_ingest

#### feedback-worker

`feedback-worker` 很薄，只做 4 件事：

1. 接收草稿和定稿
2. 本地做 unified diff
3. 调 Claude 提炼规则
4. 回写 `api/kb/memory/feedback`

它不直接操作数据库。

#### summarizer-worker

`summarizer-worker` 当前只是触发器：

- 先登录拿 cookie
- 再调用 `/api/briefing/generate`
- 选题生成逻辑仍在 API 内部

### 2.9 前端代码结构

前端使用 Next.js App Router，主要代码位于 `services/web/app/`。

全局框架层：

| 模块 | 作用 |
| --- | --- |
| `layout.tsx` | 全局 layout、主题、导航、聊天侧栏、GA |
| `middleware.ts` | 页面保护，拿 cookie 去 API 验证 |
| `components/Nav.tsx` | 顶部导航、主题切换、聊天开关 |
| `components/ChatContext.tsx` | 聊天侧栏状态 |
| `components/ChatSidebar.tsx` | 会话列表、消息流式渲染 |

页面层大致分 3 组：

| 组 | 页面 |
| --- | --- |
| 内容生产 | `/briefing`、`/drafts`、`/instructions` |
| 知识库管理 | `/sources`、`/sources/[id]`、`/knowledge` |
| 系统壳层 | `/`、`/login`、`/settings` |

其中 `/knowledge/page.tsx` 是当前前端最重的聚合页面，它同时承载：

- 资源树浏览
- wiki/detail 查看
- 文件编辑
- D3 图谱渲染
- 节点列表过滤
- maintenance 手动触发
- 手动创建 summary

这页本质上是一个小型 IDE，而不是普通详情页。

---

## 3. 目录与持久化数据

### 3.1 用户数据目录

所有持久化用户数据都落在：

```text
user_data/default/
```

核心结构：

```text
user_data/default/
├─ raw/
│  ├─ wechat/
│  ├─ rss/
│  ├─ url/
│  ├─ pdf/
│  ├─ image/
│  ├─ plaintext/
│  ├─ word/
│  └─ epub/
├─ wiki/
│  ├─ articles/
│  ├─ entities/
│  ├─ summaries/
│  ├─ indices/
│  └─ index.md
└─ config/
   ├─ topics.md
   ├─ schema.md
   └─ templates/
```

### 3.2 raw 文件保留策略

- `api/routers/kb.py` 中对 `raw/` 设置了 **512 MB** 上限
- 新内容入库后会触发 `trim_raw_files`
- 超限时从最旧文件开始删除

### 3.3 wiki 的当前语义

wiki 是 **系统生成的只读 Markdown 导出**：

- 节点入库后，API 会写入 `wiki/articles|entities|summaries|indices/`
- 前端知识库页只能查看 `wiki/`，不能直接编辑 wiki 正文
- `write_wiki_node()` 从 DB 重新导出正文、frontmatter 和关联节点区块
- article/index/wiki 导出不再作为日常编辑源
- summary 需要通过 revise instruction API 修改 DB 后再导出
- 如果数据库丢失，`restore_from_wiki()` 仍可作为灾难恢复工具，但不是日常同步机制

---

## 4. 知识模型

### 4.1 一等对象

当前系统中已经落地的 `object_type` 有 4 种：

| object_type | 含义 | 典型来源 |
| --- | --- | --- |
| `article` | 原始内容对应的知识条目 | RSS、微信、URL、PDF、图片、文本、Word、书籍章节 |
| `entity` | 被多篇文章反复提及的重要概念/人物/组织/事件 | 从文章分析结果晋升得到 |
| `summary` | 对 article 或 index 的摘要节点 | 摄入时自动生成，或手动按视角生成 |
| `index` | 层级容器节点 | 主要用于书籍/章节结构 |

### 4.2 `knowledge_nodes`

核心表是 `knowledge_nodes`，重要字段如下：

- `id`
- `user_id`
- `title`
- `abstract`
- `embedding`
- `source_type`
- `source_id`
- `raw_ref`
- `tags`
- `is_primary`
- `object_type`
- `source_node_ids`
- `summary_of`
- `canonical_name`
- `aliases`
- `perspective`
- `priority_score`
- `last_accessed_at`
- `access_count`
- `created_at`
- `updated_at`

注意：

- 系统已经把旧字段 `summary` 迁移为 `abstract`
- `abstract` 是检索主字段，绝大多数向量都基于它生成
- `summary_of` 只对 `summary` 节点有意义
- `canonical_name` / `aliases` 只对 `entity` 节点有意义
- `perspective` 已经加到 schema，但当前主要用于手动多视角摘要

### 4.3 当前边类型

代码里真正使用到的边类型包括：

| relation_type | 来源 |
| --- | --- |
| `similar_to` | 入库后基于 embedding 自动建立 |
| `mentions` | entity 回灌 / wikilink 迁移 / restore_from_wiki |
| `summarizes` | summary 指向 article 或 index |
| `part_of` | 书籍章节挂到 index |
| `extends` | maintenance 的 LLM 补边 |
| `background_of` | maintenance 的 LLM 补边 |
| `supports` | maintenance 的 LLM 补边 |
| `contradicts` | maintenance 的 LLM 补边 |

重要说明：

- `multi-layer-plan.md` 里“移除 LLM 语义边”的目标 **尚未完全落实**
- `maintenance.py` 仍然会生成 `extends/background_of/supports/contradicts`
- `co_occurs_with` 尚未实现；Phase 1 已移除前端旧过滤提示
- 历史 `wikilink` 边会被 `migrate_wikilink_edges()` 迁移成 `mentions`

### 4.4 其他表

当前 schema 里还包含：

| 表 | 用途 |
| --- | --- |
| `entity_candidates` | entity 候选池，累计 mentions / salience |
| `knowledge_edges` | 节点关系 |
| `writing_memory` | 从草稿修改中学到的写作偏好 |
| `sources` | 渠道配置 |
| `topics` | 每日选题 |
| `drafts` | 生成草稿与用户定稿 |
| `user_settings` | 简报窗口、简报时间等设置 |
| `briefings` | 已建表，但 **当前基本未被业务使用** |
| `chat_sessions` | 聊天会话 |
| `chat_messages` | 聊天消息 |

---

## 5. Source 体系

### 5.1 已支持的 source 类型

| 类型 | fetch_mode | 当前实现 |
| --- | --- | --- |
| `rss` | `subscription` | `feedparser` 拉 feed，`trafilatura` 抽正文 |
| `wechat` | `push` | iPhone 快捷指令 POST 到 `/api/sources/wechat/ingest` |
| `url` | `manual` | 当前实际抓取 `config.url` 指向的单个 URL |
| `pdf` | `manual` | 上传文件，PyMuPDF 提取文本后再用 Claude 清洗 |
| `image` | `manual` | 上传图片，Claude Vision OCR + 清洗 |
| `plaintext` | `manual` | 上传 `.txt/.md` |
| `word` | `manual` | 上传 `.doc/.docx`，用 `python-docx` 提取 |
| `epub` | `manual` | 上传 `.epub/.mobi/.azw3`，走 BookSource |

### 5.2 `is_primary` 的当前语义

`is_primary` 很重要，但当前只在部分流程中生效：

- **简报/选题生成**：只看 `is_primary = true` 的 `article`
- **知识入库**：source 的 `is_primary` 会继承到 node
- **草稿检索**：当前 `layered_retrieval()` **不会过滤** `is_primary`

因此现状是：

- 主要来源会进入“今日选题”
- 参考来源不会生成今日选题
- 但两者都可能参与后续 RAG 写作检索

### 5.3 已知实现偏差

- 前端和 API 提供了 “给 URL source 批量追加 URL” 的能力（`pending_urls`）
- 但 `ingestion-worker/sources/url.py` 当前只读取 `config.url`
- 也就是说，**URL 队列接口目前未完整打通**

---

## 6. 摄入与知识生成

### 6.1 普通 source 的入库流水线

`ingestion-worker/pipeline.py::run_pipeline()` 当前流程：

1. `fetch_new_items()`
2. `extract_text()`
3. 对文件型内容必要时从正文推断标题
4. 保存 raw 文件
5. 先对正文做一次 embedding，用于拿 entity 分析上下文
6. 调 API `/api/kb/entity_candidates/analyze_context`
7. 用 `article_analysis` prompt 生成：
   - `abstract`
   - `tags`
   - `entities`
   - `contradictions`
   - `structural_hints`
8. 对 `abstract` 生成 embedding
9. 入库 `article`
10. 再入库一个初始 `summary`
11. 把 entity candidates 发给 API 处理
12. 对新晋升的 entity 生成 entity page，并入库 `entity`
13. 回灌历史 wikilinks / mentions
14. 更新 source 的 `last_fetched_at`

### 6.2 entity 候选与晋升

候选逻辑已经落地，阈值来自 `config/system.yaml`：

- `max_salience >= 0.9`
- 或 `salience >= 0.7` 且 `mentions >= 2`
- 或 `mentions >= 3`

晋升后的动作：

1. 拉来源文章的 abstract
2. 调 `entity_page` prompt 生成实体页正文
3. 入库 `entity`
4. 写 `wiki/entities/{id}.md`
5. 标记 `entity_candidates.promoted_entity_id`
6. 回扫所有 article，给首次出现的实体名注入 `[[entity_id|term]]`
7. 为 article → entity 建 `mentions` 边

### 6.3 当前 summary 的两种来源

代码里实际上存在两类 summary：

1. **摄入时自动生成的 summary 节点**
   - 本质上只是把 article 的 `abstract` 再存成一个 `summary`
   - 主要是为了后续分层检索

2. **手动创建的多视角 summary**
   - API：`POST /api/kb/nodes/{id}/create_summary`
   - 支持 `perspective`
   - 可对 `article` 或 `index` 生成新的摘要节点

### 6.4 书籍入库（index + chapter articles）

`BookSource` 与 `run_book_pipeline()` 已落地：

- 支持 `.epub`
- `.mobi/.azw3` 为 best-effort
- 每本书先创建一个 `index`
- 每个有效章节创建一个 `article`
- article 通过 `part_of` 边挂到 index
- 每章也会生成一个 `summary`
- 书籍 index 的 `abstract` 初始为空，后续由 maintenance 聚合补齐

当前 book pipeline 的取舍：

- 为了速度，章节分析时 **不做 entity 上下文查询**
- 章节依然会走 entity candidate 流程

---

## 7. 检索与草稿生成

### 7.1 分层检索已实现

`services/api/routers/drafts.py::layered_retrieval()` 已经实现了多层检索，而不只是简单 top-k 向量搜索。

当前阶段：

1. Query embedding：支持 HyDE
2. 三路并行向量检索：
   - `summary`
   - `entity`
   - `article/index`
3. 图上传播：
   - entity → summary
   - entity → article/index
   - summary → article/index
4. 单跳扩展：
   - article → entity → article
5. index 展开：
   - 高分 index 展开其 article 子节点
6. fallback：
   - 若结果太少，回退到 article/index 直接向量命中

### 7.2 HyDE

当 `retrieval.use_hyde = true` 时：

- 先用 `hyde_abstract` prompt 让 Claude 生成一段假想摘要
- 再 embed 这段摘要而不是直接 embed 用户选题文本

### 7.3 上下文装配

草稿生成时：

- 会排除选题直接绑定的 source articles，避免重复
- 优先加入相关文章的 wiki 正文
- 长度受 `retrieval.draft_knowledge_chars` 控制，当前默认 6000 字符
- 再追加 top entities
- 再追加 `writing_memory` 中 `confidence >= 0.8` 的规则
- 最后把用户模板、选题、知识上下文、偏好规则一起发给 Claude Sonnet

---

## 8. 今日选题与写作闭环

### 8.1 简报/选题生成

当前负责“每日选题”的不是独立 summarizer 逻辑，而是：

- API：`/api/briefing/generate`
- Worker：`summarizer-worker` 只负责触发这个 API

生成策略：

- 默认模式：增量生成，只处理上次生成后新增的 primary articles
- `force=true`：清空今日选题并按时间窗口重算
- 时间窗口由 `briefing_hours_back` 控制
- prompt 为 `briefing_topics`
- 分批调用 Claude，遇到 `max_tokens` 会自动拆批重试

### 8.2 草稿与反馈

- 草稿保存在 `drafts`
- 用户提交定稿后，feedback-worker 用 `difflib.unified_diff`
- Claude 读取 diff，生成 `style/structure/content/tone` 规则
- 规则写入 `writing_memory`
- 相同规则重复出现时会提高 `confidence`

---

## 9. 前端页面

当前前端不是单一首页，而是一整套工作台：

| 页面 | 作用 |
| --- | --- |
| `/` | 视觉化 landing page |
| `/login` | 单密码登录 |
| `/briefing` | 今日选题、拖拽排序、直接生成草稿 |
| `/drafts` | 草稿历史、查看、复制、提交定稿反馈 |
| `/sources` | source 管理、主要/参考切换、上传文件、添加 URL |
| `/sources/[id]` | 单个 source 详情，尤其是微信推送配置 |
| `/knowledge` | 四面板知识库 IDE：资源树、wiki/detail、图谱、列表 |
| `/instructions` | 选题方向、模板、schema 文本编辑 |
| `/settings` | 系统节奏、偏好规则、wiki 重建、数据导出 |

全局还有一个 `ChatSidebar`：

- 会话持久化到 `chat_sessions` / `chat_messages`
- SSE 流式返回 Claude
- 目前 **不做知识库 RAG**

---

## 10. 维护、恢复与重建

### 10.1 `run_maintenance()`

当前维护任务包括：

1. `migrate_wikilink_edges()`
2. `fix_islands()`
3. `supplement_edges()`
4. `detect_contradictions()`
5. `promote_entity_candidates()`
6. `backfill_wikilinks_for_entity()` for all entities
7. `cleanup_orphan_entities()`
8. `backfill_summarizes_edges()`
9. `aggregate_index_abstracts()`

这说明当前系统仍然保留了较强的 LLM 图谱维护逻辑，不是纯统计图。

### 10.2 `restore_from_wiki()`

已实现从 `wiki/` 重建数据库：

- 扫描 `articles/entities/summaries/indices`
- 解析 frontmatter
- 生成 embedding
- 补建 `knowledge_nodes`
- 尝试恢复 `summarizes` / `part_of` / `mentions`

可恢复的主要是结构和可见正文，无法完整恢复所有历史推导边。

### 10.3 `rebuild_from_raw()`

此命令已经存在，但 **当前是部分重建，不是全源全量重建**。

目前它主要重建：

- `pdf`
- `plaintext`
- `word`
- `image`
- `wechat`

它不会完整覆盖：

- RSS
- URL
- 书籍 index/chapters

因此它比 `multi-layer-plan.md` 里的“全 raw material 重建”目标更窄。

### 10.4 调度现状

Phase 1 已移除旧 `scheduler` 空壳；当前没有独立 scheduler 服务。
现阶段的自动化主要依赖：

- ingestion-worker 自己的轮询
- 手动 trigger
- 部署环境自行安排

---

## 11. 配置文件

### 11.1 `config/system.yaml`

这里是当前系统的数值配置中心，已经被 API / ingestion-worker 使用。  
主要分区：

- `ingestion`
- `models`
- `embedding`
- `entity`
- `retrieval`
- `maintenance`
- `briefing`
- `llm_output_tokens`

### 11.2 `config/prompts.md`

当前所有主要 prompt 都集中在这里，按 `## key` 组织。  
已被代码实际使用的 section 包括：

- `image_ocr`
- `image_cleanup`
- `pdf_cleanup`
- `article_analysis`
- `entity_page`
- `entity_update`
- `summary_gen`
- `feedback_analysis`
- `briefing_topics`
- `hyde_abstract`
- `index_summary`

### 11.3 `config/image_processing.toml`

图片 OCR 专用配置：

- `max_dim`
- `tile_h`
- `overlap`
- `tile_scale`

---

## 12. 与 `multi-layer-plan.md` 的对照

### 已落地

- `article / entity / summary / index` 四类对象
- 书籍 `index + article` 结构
- `perspective` 字段
- HyDE 检索
- 分层 retrieval
- index abstract 聚合
- 确定性 ID（文件型节点和 entity）
- `restore_from_wiki()`
- `rebuild_from_raw()` 的部分版本

### 只部分落地

- 多视角 summary：API 已有，但不是所有流程都自动使用
- rebuild：存在，但未覆盖所有 source 类型
- wiki 作为长期可编辑知识库：正文可编辑，但 DB 与文件不是双向实时同步

### 尚未完成或与计划不一致

- `co_occurs_with` 没有真正实现
- “移除 LLM 语义边” 尚未完成，maintenance 仍会生成 `extends/background_of/supports/contradicts`
- 独立 scheduler 空壳已在 Phase 1 移除
- URL 批量队列接口与 worker 实现未完全对齐
- `briefings` 表已建但目前未承担核心业务
- chat 还没有接入知识库检索

---

## 13. 当前最重要的事实

1. 这是一个 **单用户** 系统，不是多租户 SaaS。
2. 当前“知识库”的事实来源是 **Postgres + pgvector**，wiki 是可编辑副本与导出形态。
3. 多层记忆架构已经实现了相当一部分，但仍处于 **计划与旧实现并存** 的状态。
4. 若要继续演进，应优先区分：
   - 已经是线上行为的代码
   - 仅存在于 `multi-layer-plan.md` 的目标设计
5. 后续修改 `MEMORY.md` 时，应优先核对：
   - `services/api/routers/*.py`
   - `services/ingestion-worker/pipeline.py`
   - `services/api/maintenance.py`
   - `config/system.yaml`
   - `docker-compose*.yml`
