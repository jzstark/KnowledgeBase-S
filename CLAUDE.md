# CLAUDE.md — 内容创作辅助系统

本文档记录了该项目的完整设计决策，供 Claude Code 在开发时参考。所有架构决策均经过深思熟虑，开发时请严格遵循。

---

## 项目概述

一个面向内容创作者的个人知识管理与 AI 辅助写作系统。核心流程：

1. 自动聚合多种来源的内容（公众号、RSS、用户上传文件等）
2. 入库清洁文本、生成 summary（仅用于内部索引/向量检索，不对用户展示）、构建持续生长的个人知识图谱
3. 每日基于新增原文 + 用户写作方向，由 AI 生成若干**写作选题**（角度），用户在主界面选入选题后 AI 基于知识库生成草稿
4. 用户提交定稿反馈，系统学习写作偏好，持续改善草稿质量

**关键概念区分**：
- **知识节点**（`knowledge_nodes`）= 清洁化的原始内容，`summary` 字段仅供内部 embedding 索引，不直接展示给用户
- **选题**（`topics`）= AI 基于当日内容和用户写作方向生成的可写角度，是用户的每日决策入口；一个选题可来自多篇原文，一篇原文也可衍生多个选题（M:N 关系）

**目标用户**：普通内容创作者，技术门槛尽量低。  
**开发者**：独立开发，持续迭代，通过 GitHub Actions + Docker 部署。  
**部署目标**：香港 VPS（中国大陆可访问），Cloudflare 子域名 + HTTPS。

---

## 架构总览

### 三层架构

```
应用层（Web 界面 + 每日流程）
        ↕ KB API
知识库层（PostgreSQL + pgvector，统一数据源）
        ↓ 单向只读同步（可选）
Obsidian vault（本地只读前端）
```

### Microservice 拆分

系统由以下独立服务组成，每个服务一个 Docker image：

| 服务 | 镜像 | 职责 |
|------|------|------|
| `nginx` | `nginx:alpine` | 反向代理 + HTTPS 终止（Cloudflare 侧） |
| `web` | `ghcr.io/{owner}/web` | Next.js 前端，所有用户界面 |
| `api` | `ghcr.io/{owner}/api` | 知识库统一 API，FastAPI |
| `ingestion-worker` | `ghcr.io/{owner}/ingestion-worker` | 内容抓取 + 摘要 + embedding |
| `summarizer-worker` | `ghcr.io/{owner}/summarizer-worker` | 每日简报生成 |
| `feedback-worker` | `ghcr.io/{owner}/feedback-worker` | diff 分析 + 偏好学习 |
| `scheduler` | 复用 `api` 镜像 | 定时触发各 worker |
| `maintenance-worker` | 复用 `api` 镜像 | 每周知识库维护 |
| `postgres` | `pgvector/pgvector:pg16` | 数据库 + 向量存储 |
| `rsshub` | `diygod/rsshub` | 微信公众号转 RSS |

需要自行构建的镜像只有：`web`、`api`、`ingestion-worker`、`summarizer-worker`、`feedback-worker`。

### GitHub Actions 构建策略

按路径触发，只重建有改动的服务：

```yaml
jobs:
  build-web:
    if: contains(github.event.paths, 'services/web/')
  build-api:
    if: contains(github.event.paths, 'services/api/')
  build-ingestion:
    if: contains(github.event.paths, 'services/ingestion-worker/')
  # 以此类推
```

构建完成后推送到 `ghcr.io`。用户服务器上运行 Watchtower，每小时自动检测新镜像并重启对应服务。

---

## 项目目录结构

```
repo-root/
├── docker-compose.yml
├── docker-compose.dev.yml
├── .env.example
├── deploy.sh                    # 一键部署脚本
├── Makefile                     # make dev / make deploy / make backup
├── nginx/
│   └── nginx.conf
├── scripts/
│   ├── backup.sh
│   └── restore.sh
├── services/
│   ├── web/                     # Next.js 前端
│   │   └── Dockerfile
│   ├── api/                     # FastAPI 知识库 API
│   │   ├── Dockerfile
│   │   ├── main.py
│   │   ├── routers/
│   │   ├── scheduler.py         # 复用此镜像启动
│   │   └── maintenance.py       # 复用此镜像启动
│   ├── ingestion-worker/
│   │   ├── Dockerfile
│   │   ├── main.py
│   │   └── sources/             # 各 Source 类型实现
│   ├── summarizer-worker/
│   │   ├── Dockerfile
│   │   └── main.py
│   └── feedback-worker/
│       ├── Dockerfile
│       └── main.py
└── user_data/                   # 运行时生成，不提交 git
    └── {user_id}/
        ├── raw/
        ├── wiki/
        └── config/
```

---

## 数据设计

### 用户数据目录结构

所有用户数据存储在宿主机 `./user_data/{user_id}/`，通过 Docker Volume 挂载，迁移服务器时直接打包此目录。

```
user_data/{user_id}/
├── raw/                          # 原始数据，系统只读不写
│   ├── wechat/
│   │   └── 2026-04-11-{title}.html
│   ├── rss/
│   │   └── 2026-04-11-{guid}.html
│   ├── uploads/
│   │   ├── paper.pdf
│   │   ├── notes.md
│   │   └── screenshot.png
│   └── url/
│       └── 2026-04-11-{domain}.html
│
├── wiki/                         # 知识库，可直接作为 Obsidian vault
│   ├── index.md                  # 自动生成的知识库入口
│   ├── nodes/
│   │   └── {node-id}.md          # 每个知识节点一个文件
│   └── drafts/
│       └── 2026-04-11-{title}.md # 生成的草稿
│
└── config/
    ├── topics.md                 # 选题方向，用户可编辑（/instructions 页面）
    ├── schema.md                 # 知识库宪法，用户可编辑（/instructions 页面，谨慎修改）
    └── templates/
        ├── 公众号推文.md          # 用户自定义模板，纯自然语言描述
        └── 周报.md
```

### wiki/nodes/{node-id}.md 格式

完全兼容 Obsidian，支持双链和图谱视图：

```markdown
---
id: node-abc123
source_type: rss
source_name: 晚点LatePost
raw_ref: ../../raw/rss/2026-04-11-item.html
tags: [AI, 推理模型]
created_at: 2026-04-11T08:03:00Z
relations:
  - id: node-xyz456
    type: extends
  - id: node-def789
    type: background_of
---

# 文章标题

[AI 生成的摘要内容]

## 关联节点
- [[node-xyz456]] · extends
- [[node-def789]] · background_of
```

### config/settings.json 结构

```json
{
  "schedule": {
    "briefing_time": "08:00",
    "briefing_hours_back": 24,
    "maintenance_frequency": "weekly"
  },
  "topics": "我关注AI行业动态、创业融资、产品设计",
  "claude_api_key": "sk-ant-...",
  "sources": [
    {
      "id": "src_abc123",
      "name": "科技早报",
      "type": "wechat",
      "is_primary": true,
      "api_token": "tok_..."
    }
  ]
}
```

### PostgreSQL 数据库 Schema

数据库存储可重新生成的结构化数据（embedding、关系、偏好规则）。如数据库损坏，可基于 `user_data/` 目录重建。

```sql
-- 知识节点
CREATE TABLE knowledge_nodes (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    title TEXT,
    summary TEXT,
    embedding vector(1536),
    source_type VARCHAR,         -- 'wechat'|'rss'|'pdf'|'image'|'plaintext'|'word'|'url'
    source_id VARCHAR,
    raw_ref JSONB,               -- {type: 'file', path: '...'} | {type: 'url', url: '...'}
    tags TEXT[],
    is_primary BOOLEAN,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 知识节点关系
CREATE TABLE knowledge_edges (
    id SERIAL PRIMARY KEY,
    from_node_id VARCHAR REFERENCES knowledge_nodes(id),
    to_node_id VARCHAR REFERENCES knowledge_nodes(id),
    relation_type VARCHAR,       -- 'similar_to'|'supports'|'contradicts'|'extends'|'example_of'|'background_of'
    weight FLOAT,                -- 0~1
    created_by VARCHAR           -- 'auto_semantic'|'auto_llm'|'user'
);

-- 写作偏好记忆
CREATE TABLE writing_memory (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    template_name VARCHAR,
    rule TEXT,
    rule_type VARCHAR,           -- 'style'|'structure'|'content'|'tone'
    confidence FLOAT DEFAULT 0.5,
    count INTEGER DEFAULT 1,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 订阅源
CREATE TABLE sources (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    name VARCHAR NOT NULL,
    type VARCHAR NOT NULL,       -- 'wechat'|'rss'|'url'|'pdf'|'image'|'plaintext'|'word'
    fetch_mode VARCHAR,          -- 'subscription'|'one_shot'|'push'
    is_primary BOOLEAN DEFAULT true,
    config JSONB,                -- 各类型自己的配置字段
    api_token VARCHAR,           -- wechat push 类型专用
    last_fetched_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 每日选题（M:N 关联原文节点）
CREATE TABLE topics (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    date DATE NOT NULL DEFAULT CURRENT_DATE,
    title TEXT NOT NULL,             -- 选题标题，10字以内
    description TEXT,                -- 一句话说明此角度为何值得写
    source_node_ids TEXT[],          -- 来源知识节点 ID 列表（M:N）
    status VARCHAR DEFAULT 'pending', -- 'pending'|'selected'|'skipped'
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 草稿记录
CREATE TABLE drafts (
    id VARCHAR PRIMARY KEY,
    user_id VARCHAR NOT NULL,
    template_name VARCHAR,
    selected_topic_ids TEXT[],       -- 用户选入的选题 ID（新字段）
    selected_node_ids TEXT[],        -- 展开后的来源节点 ID（冗余存储，便于 feedback）
    draft_content TEXT,
    final_content TEXT,              -- 用户提交的定稿，用于 diff 学习
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 启用 pgvector
CREATE EXTENSION IF NOT EXISTS vector;
CREATE INDEX ON knowledge_nodes USING ivfflat (embedding vector_cosine_ops);
```

---

## Source 的主要 / 非主要属性

每个 source 都有 `is_primary` 布尔字段，这是整个工作流中最重要的过滤器之一：

| | 主要 source（`is_primary = true`） | 非主要 source（`is_primary = false`） |
|--|--|--|
| **典型用途** | 你主动关注、希望消化的内容（精选 RSS、微信、自己上传的论文/笔记） | 背景参考资料（词典、手册、百科类文档，你不需要每天读摘要） |
| **今日简报** | ✅ 出现在首页卡片，参与选题 | ❌ 不出现，不打扰日常流程 |
| **草稿 RAG** | ✅ 参与语义检索（既是素材也是背景知识） | ✅ 参与语义检索（仅作背景知识） |
| **知识图谱** | ✅ 参与建边 | ✅ 参与建边 |

**实现要点**：
- `is_primary` 从 source 继承到 `knowledge_nodes.is_primary`（ingestion pipeline 负责传递）
- `briefing.py` 查询节点时强制 `AND is_primary = true`，其他查询（search、graph、RAG）不过滤
- source 创建时默认 `is_primary = true`，可在 source 卡片上随时切换
- 切换 source 的 `is_primary` 不会追溯修改已入库节点（节点保留入库时的属性）

---

## Source 抽象层

所有 Source 类型实现统一接口。接口以下各类型提取逻辑不同，接口以上（摘要、embedding、入库）完全一致。

### BaseSource 接口

```python
class BaseSource:
    fetch_mode: Literal['subscription', 'manual', 'push']
    
    def fetch_new_items(self) -> list[RawItem]:
        """拉取新内容。subscription 型过滤时间；manual/push 型由外部传入待处理项。"""
        raise NotImplementedError
    
    def extract_text(self, raw: RawItem) -> str:
        """从 RawItem 中提取纯文本。"""
        raise NotImplementedError

class RawItem:
    source_id: str
    title: str | None
    raw_ref: dict                # {'type': 'file', 'path': '...'} | {'type': 'url', 'url': '...'}
    content_type: str
    raw_bytes: bytes | None
    fetched_at: datetime
```

### Source 分类

所有 source 分为两类，区别仅在于内容如何到达：

**自动抓取型**（系统主动获取）：
| 类型 | fetch_mode | 触发方式 | extract 逻辑 |
|------|-----------|---------|-------------|
| `rss` | `subscription` | 定时轮询，过滤 pub_date > last_fetched | trafilatura HTML 正文提取 |
| `wechat` | `push` | 接收 iPhone 快捷指令推送 | 直接使用推送文本 |

**手动管理型**（用户主动添加内容）：
| 类型 | fetch_mode | 触发方式 | extract 逻辑 |
|------|-----------|---------|-------------|
| `url` | `manual` | 用户添加 URL，可随时追加 | trafilatura 提取正文 |
| `pdf` | `manual` | 用户上传文件，支持批量、可随时追加 | PyMuPDF 提取文本 |
| `image` | `manual` | 用户上传文件，支持批量、可随时追加 | Claude Vision → 文本描述 |
| `plaintext` | `manual` | 用户上传文件，支持批量、可随时追加 | 直接读取 |
| `word` | `manual` | 用户上传文件，支持批量、可随时追加 | python-docx 提取 |

**关键设计原则**：source 是持久的**内容渠道**，不是一次性触发。例如，创建一个叫"有趣的 Paper"的 PDF source 后，用户每次看到好论文都可以上传到这个 source，每次上传可以包含多个文件；系统对每个文件独立处理，生成各自的知识节点。去重逻辑（按文件哈希）在后续步骤实现。

### 微信公众号（push 型）专用端点

```
POST /api/sources/wechat/ingest
Headers: X-API-Token: {source.api_token}

{
  "source_id": "src_abc123",
  "title": "文章标题",
  "content": "正文全文...",
  "url": "https://mp.weixin.qq.com/..."
}
```

用户在 Web 界面创建微信 source 后，系统生成专属 `api_token`，用户将其填入 iPhone 快捷指令模板即可使用。

### Ingestion 流水线（所有类型共用）

```
Source.fetch_new_items()
        ↓ RawItem 列表
Source.extract_text()        ← 各类型自己实现
        ↓ 纯文本
保存原始文件到 raw/                    ← 永久存档
        ↓
Claude API → 清洁文本 + 摘要 + 标签   ← 统一逻辑；摘要仅供内部索引，不对用户展示
        ↓
Embedding API（基于 summary）         ← 统一逻辑
        ↓
POST /api/kb/ingest                   ← 写入数据库 + 生成 wiki/nodes/{id}.md
        ↓ 异步
计算与现有节点的语义相似度 → 建 similar_to 边（阈值 > 0.75）
```

---

## 知识库统一 API

所有上层服务（Web、ingestion-worker、summarizer-worker、feedback-worker、Obsidian 同步）都通过此 API 访问知识库，不直接操作数据库。

### 端点列表

```
# 内容入库（唯一写入入口）
POST   /api/kb/ingest

# 语义搜索（RAG 核心调用）
GET    /api/kb/search?q=...&limit=10&tags=AI,产品

# 获取单个节点（含所有边）
GET    /api/kb/node/:id

# 图谱查询（Obsidian 同步 + Wiki 可视化用）
GET    /api/kb/graph?root=:id&depth=2

# 偏好规则读写
POST   /api/kb/memory/feedback
GET    /api/kb/memory?template_name=...

# 维护任务触发
POST   /api/kb/maintenance/run

# Source 管理
GET    /api/sources                        # 列表（含每个 source 的文章数）
POST   /api/sources                        # 创建 source（不含文件，建渠道）
PUT    /api/sources/:id
DELETE /api/sources/:id
POST   /api/sources/:id/fetch              # 触发 ingestion-worker 抓取（自动型）
POST   /api/sources/:id/upload             # 上传文件到已有 source，支持多文件（手动型）
POST   /api/sources/:id/add-url            # 添加 URL 到已有 source（手动型）
POST   /api/sources/wechat/ingest          # 微信 push 专用（Step 7）

# 今日选题（简报）
GET    /api/briefing                       # 获取今日（或指定日期）选题列表
POST   /api/briefing/generate             # 立即生成选题
PATCH  /api/briefing/topics/:id           # 更新选题状态（selected/skipped/pending）

# 草稿
POST   /api/drafts/generate               # 接受 selected_topic_ids，通过选题查原文节点再 RAG
POST   /api/drafts/:id/feedback           # 提交定稿
GET    /api/drafts
```

---

## 草稿生成

### RAG 流程

输入为用户选入的**选题 ID 列表**，而非直接的节点 ID。

```python
def generate_draft(selected_topic_ids, template_name, user_id):
    # 1. 获取选题（title + description + source_node_ids）
    topics = [get_topic(id) for id in selected_topic_ids]

    # 2. 通过 source_node_ids 获取来源原文节点
    source_node_ids = dedupe([id for t in topics for id in t.source_node_ids])
    source_nodes = [get_node(id) for id in source_node_ids]

    # 3. 以选题标题+说明为 query，语义检索更多相关知识
    query = ' '.join([f"{t.title} {t.description}" for t in topics])
    related = vector_search(query, exclude=source_node_ids, limit=8)

    # 4. 沿边扩展一跳（获取背景知识）
    extended = graph_expand(related, relation_types=['background_of', 'extends'], depth=1)

    # 5. 截断到字符上限
    knowledge_context = truncate_to_chars(related + extended, max_chars=6000)

    # 6. 获取偏好规则（confidence >= 0.8）
    preferences = get_high_confidence_preferences(user_id, template_name, min_confidence=0.8)

    # 7. 读取模板（纯自然语言描述）
    template = read_template(user_id, template_name)

    # 8. 组合 Prompt：选题角度在前，来源原文和背景知识在后
    prompt = f"""
{template}

本次写作的选题角度：
{format_topics(topics)}

相关来源原文摘要：
{format_nodes(source_nodes)}

知识库背景知识：
{format_knowledge(knowledge_context)}

根据用户历史反馈，额外注意：
{format_preferences(preferences)}
"""
    return claude_api(prompt)
```

### 写作模板

模板是纯自然语言描述，用户直接写"我想要什么样的文章"，存为 `config/templates/{名称}.md`：

```markdown
我想要一篇适合微信公众号的文章。风格轻松有观点，
适合碎片化阅读。开头用一个有趣的现象或问题引入，
中间分2-3个小节展开，每节有小标题，结尾给读者
一个值得思考的问题，不要号召性语言。长度2000字左右。
```

---

## 轻量 RLHF：从用户修改中学习

### 流程

```
用户在 Web 界面粘贴定稿（可选操作）
        ↓
POST /api/drafts/:id/feedback
        ↓
Feedback Worker：difflib 对比草稿 v1 和定稿
        ↓
Claude API 分析 diff，提炼偏好规则（JSON 输出）
        ↓
更新 WritingMemory，提升置信度
        ↓
置信度 > 0.8 的规则自动写入 schema.md 的写作偏好区块
```

### 偏好规则存储

偏好按 `(user_id, template_name)` 存储，同一用户不同模板的偏好独立学习：

```python
# 每次定稿提交后，同一条规则出现 3 次以上置信度显著提升
match['confidence'] = min(1.0, match['confidence'] + 0.15)
```

---

## Web 界面

### 路由结构

```
/                    今日简报（首页，核心交互）
/sources             Source 管理
/knowledge           知识库浏览（列表视图 + 图谱视图）
/drafts              草稿历史
/instructions        指令设置（写作方向、模板、schema）
/settings            系统设置（节奏、偏好规则）
/login               登录页
```

所有页面顶部有持久导航栏（`app/components/Nav.tsx`），`/login` 除外。

### 各页面核心内容

**`/`（今日简报）**

三栏布局：
- 左栏（今日选题）：AI 基于当日新增原文和用户写作方向生成的写作角度列表，平铺展示，每张卡片显示选题标题、说明、来源篇数，支持"选入"和"跳过"。**注意：这里展示的是选题角度，不是文章摘要。**
- 中栏（已选选题）：用户选入的选题，可拖拽排序（顺序即叙事权重），可移除
- 右栏（生成草稿）：选择模板 + "生成草稿"按钮；生成后变为草稿预览 + 复制按钮 + 可选的定稿反馈入口

顶部状态栏显示上次生成时间，提供"立即生成选题"手动触发按钮。

**`/sources`**

两个 Tab：自动抓取型（RSS/微信）和手动管理型（URL/PDF/图片/文本/Word）。

**Source 是持久渠道，不是一次性触发。** 同一个 source 可以在任意时间追加新内容。

添加 Source 流程：选类型 → 填名称 → 创建（不含文件）：
- 微信公众号：创建后展示快捷指令配置（接收地址 + API Token）
- RSS：填写 Feed URL
- URL/文件类型：先创建渠道，后续通过 source 卡片上传

Source 卡片操作：
- 所有类型：显示 `is_primary` 状态徽章（"主要"/"参考"）+ 切换按钮；显示文章数
- 自动型（RSS）：显示最后抓取时间 + "立即抓取"按钮
- 手动型（URL）：显示文章数 + "添加 URL"按钮（可一次添加多条）
- 手动型（文件）：显示文章数 + "上传文件"按钮（支持多文件批量上传）
- 所有类型：删除按钮

**`/knowledge`**

列表视图（默认）+ 图谱视图切换。图谱视图用 D3.js 渲染，节点大小表示关联数量，边颜色表示 relation_type。顶部有"立即运行维护"按钮。

**`/settings`**

四个区块：
- 流程节奏：简报时间、覆盖小时数、维护频率
- 选题方向：自然语言编辑框 + "立即重新分类今日简报"按钮
- 模板管理：名称 + 大文本框（纯自然语言）+ "立即测试"按钮
- Schema.md：代码编辑器 + "用新 schema 重新处理最近一篇文章"按钮
- 偏好规则：显示系统学到的规则和置信度，支持手动删除

### 前端设计原则

1. **所有自动流程都有"立即执行"按钮**，用户不需要等定时任务
2. **所有配置改动后都有即时验证路径**，改完可以立刻看到效果
3. **所有异步操作显示流式进度**，不用 loading spinner，用户能看到系统在做什么：
   ```
   [立即生成简报] → ⏳ 正在抓取 (3/5 个源)... → ⏳ Claude 正在分类... → ✅ 完成，共 12 条
   ```
4. **草稿定稿反馈是可选的**，用正向激励而非流程强制，提交后显示"学到了 X 条偏好规则"

---

## Authentication

单用户模式，密码存在 `.env` 里，不需要数据库用户表：

```env
AUTH_PASSWORD=your_password_here
AUTH_SECRET=random_secret_for_jwt_signing
```

登录逻辑：用户输入密码 → 与 `AUTH_PASSWORD` 比对 → 签发 JWT 存 cookie → 所有页面和 API 请求验证 cookie。

在 Next.js 中间件里拦截所有路由，未登录重定向到 `/login`。

---

## Obsidian 同步（可选，只读）

知识库层到 Obsidian 的同步是**单向的**，系统只写不读，用户在 Obsidian 里的任何修改不会回流。

每当有新节点入库或节点更新时，异步生成/更新对应的 `wiki/nodes/{id}.md` 文件。`wiki/` 目录通过 iCloud 或 Dropbox 同步到用户本地，用户将此目录作为 Obsidian vault 打开即可使用双链和图谱视图。

---

## 基础设施

### docker-compose.yml 关键部分

```yaml
services:
  postgres:
    image: pgvector/pgvector:pg16
    volumes:
      - ./data/postgres:/var/lib/postgresql/data
    environment:
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: app

  api:
    image: ghcr.io/{owner}/api:latest
    volumes:
      - ./user_data:/app/user_data
    environment:
      DATABASE_URL: ${DATABASE_URL}
      CLAUDE_API_KEY: ${CLAUDE_API_KEY}
      AUTH_SECRET: ${AUTH_SECRET}
    depends_on: [postgres]

  ingestion-worker:
    image: ghcr.io/{owner}/ingestion-worker:latest
    volumes:
      - ./user_data:/app/user_data
    depends_on: [api, rsshub]

  maintenance-worker:
    image: ghcr.io/{owner}/api:latest
    command: python maintenance.py
    profiles: ["maintenance"]    # 不默认启动，手动或定时触发

  scheduler:
    image: ghcr.io/{owner}/api:latest
    command: python scheduler.py
    depends_on: [api]

  rsshub:
    image: diygod/rsshub:latest

  watchtower:
    image: containrrr/watchtower
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    command: --interval 3600

  nginx:
    image: nginx:alpine
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf
    ports:
      - "80:80"
    depends_on: [web, api]
```

### Nginx 配置

Cloudflare 侧终止 HTTPS，Nginx 只监听 80 端口：

```nginx
server {
    listen 80;
    server_name kb.yourdomain.com;

    location / {
        proxy_pass http://web:3000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    location /api {
        proxy_pass http://api:8000;
        proxy_set_header Host $host;
    }
}
```

### Cloudflare 配置

DNS 添加 A 记录指向 VPS IP，代理状态开启（橙色云朵）。SSL/TLS 加密模式设为"完全"。

### .env.example

```env
# 数据库
DB_PASSWORD=change_me
DATABASE_URL=postgresql://postgres:change_me@postgres:5432/app

# Claude API
CLAUDE_API_KEY=sk-ant-...

# 认证
AUTH_PASSWORD=change_me
AUTH_SECRET=random_32_char_string

# 应用
NEXTAUTH_URL=https://kb.yourdomain.com
```

### 备份

```bash
# scripts/backup.sh（每天 cron 触发）
DATE=$(date +%Y%m%d)
tar -czf backup-${DATE}.tar.gz ./user_data/
docker exec postgres pg_dump -U postgres app > db-${DATE}.sql
rclone copy backup-${DATE}.tar.gz r2:bucket/backups/
rclone copy db-${DATE}.sql r2:bucket/backups/
rclone delete --min-age 30d r2:bucket/backups/
```

---

## 数据可移植性

用户数据完全属于用户。Web 界面提供"导出我的数据"功能，打包 `user_data/{user_id}/` 整个目录供下载。

解压后：
- `wiki/` 目录直接作为 Obsidian vault 打开
- `raw/` 是所有原始文章和文件
- `config/` 是所有配置，可在新服务器导入恢复

数据库（embedding、关系图）从 `user_data/` 派生，损坏后可重建，不是唯一真相来源。

---

## Claude API 调用清单

系统中所有 Claude API 调用均为单次无状态调用，无多轮对话，无工具调用链，context 完全可控：

| 调用 | 触发时机 | 输入上限 | 输出格式 |
|------|---------|---------|---------|
| 摘要 + 打标 | 每篇文章入库时 | 原文（截断至 4000 tokens） | 摘要文本 + JSON 标签 |
| 分类 | 每日简报生成 | 摘要列表 + 选题方向描述 | JSON 分类结果 |
| 建边分析 | 每周维护 | 两个节点摘要 | JSON relation_type |
| 草稿生成 | 用户触发 | 模板 + 选题 + 知识库（≤2000 tokens） + 偏好规则 | 文章正文 |
| diff 分析 | 用户提交定稿 | 草稿 v1 + 定稿 | JSON 偏好规则列表 |
| 健康检查 | 每周维护，分批 | 每批 ≤20 个节点摘要 | JSON 问题报告 |
| 图片描述 | 图片类型 source 入库 | 图片 base64 | 文本描述 |

---

## 开发优先级

按以下顺序实现，每步完成后可独立验证：

1. ✅ **基础骨架**：docker-compose + PostgreSQL + pgvector + 登录页
2. ✅ **Ingestion Worker**：RSS 抓取 + Claude 摘要 + embedding 入库
3. ✅ **KB API**：search、node、graph、memory、ingest 端点
4. ✅ **今日简报**：summarizer-worker + 首页三栏布局
5. ✅ **草稿生成**：RAG 检索 + 模板 + 生成端点
6. ✅ **Source 管理**：Source 是持久渠道；自动型（RSS/微信）+ 手动管理型（URL/文件，支持随时追加 + 批量上传）
7. ✅ **微信快捷指令**：push 端点 + 快捷指令模板生成
8. ✅ **反馈学习**：feedback-worker + 偏好规则 + settings 页展示
9. ✅ **知识库浏览**：列表视图 + D3 图谱视图
10. ✅ **Obsidian 同步**：单向 md 文件生成
11. ✅ **指令设置页 + 数据导出**：/instructions 页面（选题方向/模板/Schema）+ 导出 zip
12. ✅ **Maintenance Worker**：孤岛检测 + 矛盾发现 + 补边

---

## 当前项目状态（2026-04-15）

### 已完成：第一步 ~ 第十二步

- **第一步**：`make dev` → 登录页 → pg + pgvector 就绪 ✅
- **第二步**：RSS 抓取 → Claude 摘要 → OpenAI embedding → 入库 → wiki md 生成 ✅
- **第三步**：KB API（search/node/graph/memory/ingest）全部通过 ✅
- **第四步**：简报生成 → 首页三栏布局（选入/跳过/拖拽排序）✅
- **第五步**：草稿生成（RAG + 模板 + Claude）→ 草稿历史页 ✅
- **第六步**：Source 管理完整实现 ✅
  - Source 是持久渠道；手动型支持随时批量追加（`/{id}/upload` 多文件、`/{id}/add-url`）
  - `is_primary` 概念明确：主要型出现在简报，参考型仅参与 RAG；卡片上可切换
  - ingestion-worker 新增 HTTP trigger server（端口 8001）+ URLSource
  - fetch_mode: `subscription` / `manual` / `push`（原 `one_shot` 已废弃）
  - 文件型 source 处理 ✅：image/pdf/plaintext/word Source 类已实现，上传后 worker 可正常处理
- **第七步**：微信快捷指令 ✅
  - `POST /api/sources/wechat/ingest`：`X-API-Token` 鉴权，保存正文到 `raw/wechat/`，追加到 `config.pending_items`，触发 worker
  - WechatSource：从 `pending_items` 读取推送条目，按 `pushed_at` 精确过滤，`extract_text` 直接解码纯文本
  - 微信 source 卡片新增"查看配置"入口 → `/sources/[id]` 详情页（连接配置 + 快捷指令指南 + 扩展占位）
  - `GET /api/sources/{id}` 单条查询端点
  - **待改进（最后处理）**：快捷指令的分发体验还有很大改进空间——例如生成可一键导入的 `.shortcut` 文件、展示 QR Code 供扫码、提供分步骤截图安装说明等。当前仅提供文字配置指南，功能可用但不够友好。
- **第八步**：反馈学习 ✅
  - `services/feedback-worker/`：独立 FastAPI 服务（端口 8002），`POST /analyze` 接收 draft diff，difflib 计算差异，Claude Haiku 提炼偏好规则（JSON），逐条写入 `writing_memory`
  - `POST /api/drafts/:id/feedback`：保存 `final_content`，同步调用 feedback-worker，返回 `{rules_extracted: N}`
  - `POST /api/kb/memory/feedback` 移除 `require_auth`（内部 worker 调用无 cookie）
  - `/settings` 页完整实现：流程节奏 / 选题方向 / 模板管理（GET/PUT/DELETE `/api/settings/templates/:name`）/ 偏好规则展示（按置信度排序 + 进度条 + 删除）
  - 草稿历史页新增"提交定稿"折叠区：粘贴定稿 → 提交 → 显示"已学习 N 条偏好规则"
  - **⚠️ 已知疑点**：当用户提交的定稿与草稿差异极大（几乎全文替换）时，`rules_extracted` 可能为 0。两种可能原因：① feedback-worker 未运行（连接失败被 `except Exception: pass` 静默吞掉，API 仍返回 `{ok:true, rules_extracted:0}`，无法区分）；② diff 全为增删行、Claude 无法归纳出具体可复用的偏好规则，返回 `[]`。**待改进方向**：当 diff 超过阈值（如 >70% 不同）时切换为"直接风格分析"模式，让 Claude 分析定稿本身的风格特征而非 diff；同时在 API 层区分"worker 不可达"与"worker 返回 0 条"，给前端不同的提示。
- **第九步**：知识库浏览 ✅
  - `GET /api/kb/nodes`：分页列表（LIMIT/OFFSET），支持文本搜索（ILIKE）和标签过滤（`tags && ARRAY[...]::text[]`），返回 `{nodes, total}`
  - `GET /api/kb/graph/all`：全量节点 + 边，节点含 `degree`（关联边数），用于 D3 力导向图
  - `/knowledge` 页：列表视图（2列卡片网格，搜索/标签过滤，分页）+ 图谱视图（D3 force-directed，节点大小=degree，边颜色=relation_type，支持拖拽+缩放）+ 点击节点展示右侧详情侧边栏
  - 首页 header 新增"知识库/草稿/设置"导航链接
  - d3 + @types/d3 已加入 `services/web/package.json`
  - "立即运行维护"按钮调用 `POST /api/kb/maintenance/run`，后台触发三项维护任务
- **第十步**：Obsidian 同步 ✅
  - `write_wiki_node(node_id, user_id)`：每次节点入库后，在 `build_similar_edges` 完成后异步写入 `user_data/{user_id}/wiki/nodes/{node_id}.md`（Obsidian 兼容 frontmatter + 双链格式）
  - `write_wiki_index(user_id)`：重建 `wiki/index.md`，按日期分组列出所有节点
  - `POST /api/kb/wiki/rebuild`（需认证）：后台重建全量 wiki 文件
  - `GET /api/kb/wiki/status`（无需认证）：返回 `{synced_count, index_exists}`
  - `/settings` 页新增 "Obsidian 同步" Section：显示同步状态 + "全量重建"按钮
  - wiki 文件路径：`/app/user_data/{user_id}/wiki/nodes/{node_id}.md`（容器内，宿主机同路径通过 volume 共享）
  - 用户可将 `user_data/default/wiki/` 目录设为 Obsidian vault 直接使用
- **第十一步**：指令设置页 + 数据导出 ✅
  - 新建 `/instructions` 页面，集中管理三类内容文档：选题方向 / 写作模板 / 知识库宪法（Schema）
  - **选题方向**：改为文件存储 `config/topics.md`；`get_settings_dict()` 优先读文件，向后兼容 DB；`GET/PUT /api/settings/topics`
  - **Schema.md**：全新实现，`GET/PUT /api/settings/schema`；默认内容包含分类体系/摘要规范/关系规则/准入标准/词汇表；文件不存在时返回默认内容（系统不中断）
  - **数据导出**：`GET /api/settings/export`（需认证）→ `shutil.make_archive` 打包 `user_data/default/` → 返回 `knowledgebase-export.zip`
  - `/settings` 页重构为"系统设置"：只保留流程节奏/偏好规则/Obsidian 同步/数据导出；选题方向和模板已迁出
  - 首页导航新增"指令设置"链接
  - 三类文档统一落在 `user_data/default/config/`，打包导出时一并包含：
    ```
    config/
      topics.md       ← 选题方向
      schema.md       ← 知识库宪法
      templates/      ← 写作模板
    ```
- **第十二步**：Maintenance Worker ✅
  - `services/api/maintenance.py` 实现三项维护任务：
    - **孤岛检测** `fix_islands()`：查找无任何边的节点（最多 20 个），找 top-3 相似节点（similarity > 0.55），调用 Claude Haiku 分析关系，confidence ≥ 0.70 则建边（created_by = 'auto_llm'）
    - **补边** `supplement_edges()`：找仅有 similar_to 边且无 auto_llm 边的节点对（最多 20 对，按 weight DESC），LLM 分析是否存在更精确关系（excludes none/similar_to），confidence ≥ 0.70 则新增边
    - **矛盾发现** `detect_contradictions()`：找 similar_to 边 weight 0.75~0.92 的节点对（最多 10 对），LLM 判断是否矛盾，confidence ≥ 0.75 则建 contradicts 边
  - `analyze_relation()`：Claude Haiku 单次调用，返回 `{relation, direction, confidence}`，方向 a_to_b/b_to_a/symmetric
  - `upsert_llm_edge()`：插入前检查 (from, to, type) 是否已存在，避免重复
  - `run_maintenance(user_id)`：顺序执行三任务，返回汇总 dict；不调用 `database.init()`（API 模式已连接）
  - `__main__` 入口：`database.init()` + `asyncio.run(run_maintenance())`，支持 Docker 独立运行
  - `POST /api/kb/maintenance/run`（需认证）：从 stub 改为实际触发，`background_tasks.add_task(run_maintenance, USER_ID)`
  - `/knowledge` 页"立即运行维护"按钮：从 stub 改为调用 API，显示"维护中…" → "维护已触发，后台运行中"
  - LLM 模型：`claude-haiku-4-5-20251001`（低成本，每次维护最多调用 ~50 次）

### 现有目录结构

```
KnowledgeBase-S/
├── docker-compose.yml          # API 含 INGESTION_WORKER_URL + FEEDBACK_WORKER_URL; workers expose 8001/8002
├── docker-compose.dev.yml      # dev 覆盖
├── .env.example                # 含 OPENAI_API_KEY
├── Makefile / deploy.sh
├── nginx/nginx.conf
├── scripts/backup.sh, restore.sh
└── services/
    ├── api/                    # FastAPI + Python 3.12
    │   ├── requirements.txt    # fastapi, uvicorn, asyncpg, databases, python-jose,
    │   │                       # httpx, openai, anthropic, python-multipart
    │   ├── main.py             # auth 端点 + 注册所有 routers
    │   ├── auth.py             # JWT（python-jose），单用户密码比对
    │   ├── database.py         # 建表（7张）+ jsonb() 辅助
    │   ├── scheduler.py        # 空壳
    │   ├── maintenance.py      # 维护任务：fix_islands / supplement_edges / detect_contradictions
    │   │                       # run_maintenance(user_id)；standalone: python maintenance.py
    │   └── routers/
    │       ├── sources.py      # CRUD + GET /{id} + /wechat/ingest(push) + /{id}/fetch
    │       │                   # /{id}/upload + /{id}/add-url；is_primary 可 PUT 切换
    │       ├── kb.py           # /api/kb/ingest, search, node, nodes, graph, graph/all, memory
    │       │                   # wiki/rebuild, wiki/status（Obsidian 同步）
    │       │                   # /maintenance/run（触发 run_maintenance 后台任务）
    │       ├── briefing.py     # GET /api/briefing, POST /api/briefing/generate（仅 is_primary 节点）
    │       ├── settings.py     # GET/PUT /api/settings（流程节奏）
    │       │                   # GET/PUT /api/settings/topics（选题方向，文件存储）
    │       │                   # GET/PUT /api/settings/schema（知识库宪法，文件存储）
    │       │                   # GET/PUT/DELETE /api/settings/templates/:name
    │       │                   # GET /api/settings/export（打包下载 user_data zip）
    │       └── drafts.py       # POST /api/drafts/generate, GET /api/drafts, GET /api/drafts/{id}
    │                           # POST /api/drafts/:id/feedback（定稿提交 → feedback-worker）
    ├── feedback-worker/        # FastAPI + uvicorn（端口 8002）
    │   ├── Dockerfile
    │   ├── requirements.txt    # anthropic, httpx, fastapi, uvicorn
    │   └── main.py             # POST /analyze：difflib diff + Claude Haiku → 规则提炼 → writing_memory
    ├── ingestion-worker/       # fastapi + uvicorn（端口 8001）用于 HTTP trigger server
    │   ├── requirements.txt    # 新增 fastapi, uvicorn[standard]
    │   ├── main.py             # 循环模式（subscription only）+ HTTP trigger server + --once
    │   ├── pipeline.py         # extract→save_raw→summarize→embed→ingest→wiki
    │   └── sources/
    │       ├── base.py         # BaseSource + RawItem
    │       ├── rss.py          # RSSSource（subscription）✅
    │       ├── url.py          # URLSource（manual，trafilatura）✅
    │       ├── file_base.py    # FileSourceMixin（文件型共用 fetch 逻辑）✅
    │       ├── plaintext.py    # PlaintextSource（直接读取 UTF-8）✅
    │       ├── pdf.py          # PDFSource（PyMuPDF）✅
    │       ├── image.py        # ImageSource（Claude Vision）✅
    │       ├── word.py         # WordSource（python-docx）✅
    │       └── wechat.py       # WechatSource（push 型，读 pending_items）✅
    ├── summarizer-worker/
    │   └── main.py             # 调用 POST /api/briefing/generate，定时或 --once
    └── web/                    # Next.js 14 + Tailwind + dnd-kit + d3
        ├── middleware.ts       # cookie 鉴权
        └── app/
            ├── login/page.tsx  # 登录页
            ├── page.tsx        # 首页三栏：文章列表/已选选题(可拖拽)/草稿生成面板
            ├── drafts/page.tsx # 草稿历史列表 + 点击查看/编辑/复制 + 提交定稿反馈
            ├── sources/page.tsx      # Source 管理（自动抓取/手动管理 Tab，is_primary 切换）
            ├── sources/[id]/page.tsx # Source 详情页（微信：连接配置 + 快捷指令指南）
            ├── knowledge/page.tsx    # 列表视图（搜索/过滤/分页）+ D3 图谱视图 + 详情侧边栏
            ├── instructions/page.tsx # 指令设置：选题方向 + 写作模板卡片列表 + Schema 编辑（含警告）
            └── settings/page.tsx     # 系统设置：流程节奏 + 偏好规则 + Obsidian 同步 + 数据导出
```

### 数据库表（7张）

| 表 | 用途 |
|----|------|
| `knowledge_nodes` | 知识节点 + 1536维向量 |
| `knowledge_edges` | 节点关系图 |
| `writing_memory` | 写作偏好规则 |
| `sources` | 订阅源配置 |
| `drafts` | 草稿记录 |
| `briefings` | 每日简报（按 user_id+date 唯一） |
| `user_settings` | 用户设置（briefing_hours_back, briefing_time 等；topics 已迁到 config/topics.md） |

### 关键约定

- **databases 库的类型转换问题**：所有含 `::type`（`::vector`、`::date`、`::timestamptz`）的参数都用 f-string 内联，不走 `:param` 绑定；`<=>` 向量运算符用 asyncpg 原生接口（`conn.raw_connection.fetch`）。
- **数据库密码**：`.env` 中 `DB_PASSWORD` 与 `DATABASE_URL` 里密码必须一致；重置时删除 `./data/postgres/`。
- **nginx**：挂载到 `/etc/nginx/conf.d/default.conf`。
- **Auth**：HttpOnly cookie `token`，JWT 7天，`AUTH_SECRET` 签名。
- **Embedding**：OpenAI text-embedding-3-small，1536 维，对摘要做 embedding。
- **USER_ID**：固定 `"default"`，单用户。
- **手动触发方式**：
  ```bash
  # ingestion
  docker compose -f docker-compose.yml -f docker-compose.dev.yml \
    run --rm ingestion-worker python main.py --once
  # 简报生成
  curl -X POST http://localhost/api/briefing/generate -b /tmp/kb_cookies.txt
  ```
- **web 新增 npm 包后**：
  1. 在宿主机 `services/web/` 下运行 `npm install --package-lock-only` 更新 `package-lock.json`，并提交到 git
  2. 重建容器时必须加 `--no-cache`，否则 Docker 会复用旧的 npm install 层：
     `sudo docker compose -f docker-compose.yml -f docker-compose.dev.yml build --no-cache web && sudo docker compose ... up -d web`
  3. 直接在运行中容器内安装也可：`sudo docker compose ... exec web npm install`