# Source / Folder Revision

本文档记录 `source` 体系的下一轮重构设计。目标是把当前偏技术化的 source 管理，改成用户更容易理解的“资料夹 + 原始材料 + Wiki 知识”双视角模型。

当前结论：UI 不再把 `source` 作为核心概念暴露给用户。用户看到的是资料夹；系统内部保留或重命名 source/connector，用于表达 RSS、微信公众号等外部接入来源。

---

## 目标模型

系统分三层：

```
Raw Pool
  系统事实层。保存真实 raw material，用户不能直接操作。

资料夹视角
  用户组织层。用户在这里上传、移动、复制、重命名、归档资料。

Wiki / Knowledge 视角
  知识派生层。展示 article/entity/summary/index 以及图谱、检索结果。
```

三层不是同一件事的三个副本，而是不同维度的权威：

```
Pool 是“材料事实”的权威。
资料夹是“用户组织方式”的权威。
Wiki 是“知识派生结果”的权威。
```

Pool 决定“这份材料是什么”，资料夹决定“用户在哪里、以什么名字看见它”，Wiki 决定“这份材料被处理成了哪些知识对象”。

---

## 核心不变量

1. 用户不能直接操作 Pool。
2. 资料夹里的“文件”不是 raw asset 本身，而是一个 document instance / folder entry。
3. document instance 可以移动、重命名、复制。
4. raw asset 是真实材料，可以被多个 document instance 引用。
5. article 绑定 document instance，而不是 `raw_ref.path`。
6. Wiki md 文件绑定 article id，而不是资料夹路径。
7. 删除 raw material 和删除 article 是不同危险等级的操作。
8. `raw_ref` 不再作为权威字段存在，也不参与查重、定位、重建或 article id 生成。
9. stream/RSS/微信公众号 provenance 不随资料夹移动而改变。
10. 资料夹视角和 Wiki 视角不要同时拥有同一类修改权。

---

## 推荐数据概念

命名可以在实现时再精修，但边界应保持稳定。

```
folders
  id
  parent_id
  name
  kind              -- normal | stream
  status            -- active | inactive | archived

connectors
  id
  folder_id
  type              -- rss | wechat
  config
  status
  last_fetched_at

raw_assets
  id
  storage_key        -- 稳定物理存储引用，不随 UI 资料夹移动
  original_filename
  mime_type
  size
  sha256
  created_at

document_instances
  id
  folder_id
  raw_asset_id
  connector_id       -- nullable；用于 stream provenance
  display_name
  origin_ref
  origin_ref_type
  status
  created_at
  updated_at

article_nodes
  node_id
  document_instance_id
  raw_asset_id        -- 可冗余，便于反查
```

查询链路：

```
资料夹 -> document_instances -> raw_assets -> article_nodes -> wiki md
Wiki article -> article_nodes -> document_instances -> folders
```

同一个 raw asset 可以出现在多个资料夹：

```
raw_asset_123 = A.pdf 的真实文件

document_instance_1 = Client A / 合同 / A.pdf
document_instance_2 = Client B / 参考资料 / A.pdf
```

右键 `Copy 到...` 创建新的 document instance，指向同一个 raw asset。用户重新上传同内容文件时，系统可以提示“内容相同”，但不自动合并；按新的用户动作创建新的文档实例。

---

## 普通资料夹

普通资料夹用于手动放资料，支持 PDF、Word、图片、EPUB、网页链接等。

### 操作矩阵

#### 新建资料夹

```
Pool: 不变
Folder: 新建 folder(kind=normal)
Wiki: 不变
```

#### 重命名资料夹

```
Pool: 不变
Folder: folder.name 更新
Wiki: 不变；article 反查位置时显示新路径
```

#### 移动资料夹

```
Pool: 不变
Folder: folder.parent_id 更新
Wiki: 不变；article 反查位置时显示新路径
```

#### 归档资料夹

```
Pool: 不变
Folder: folder.status = archived
Wiki: 默认不变；检索/图谱是否隐藏 archived 资料夹下 article 由过滤规则决定
```

#### 删除空资料夹

```
Pool: 不变
Folder: 删除 folder
Wiki: 不变
```

#### 上传文件

```
Pool: 新建 raw_asset
Folder: 新建 document_instance，指向 raw_asset
Wiki: 创建或排队创建 article、summary、entities、edges
```

即使 sha256 相同，也不要自动合并。系统可以提示，但用户动作仍是新的文档实例。

#### 添加网页

```
Pool: 保存网页快照 raw_asset
Folder: 新建 document_instance
Wiki: 创建或排队创建 article
```

#### 重命名文件显示名

```
Pool: 不变
Folder: document_instance.display_name 更新
Wiki: 默认不改 article title
```

如未来需要，可提供显式动作“同步更新文章标题”，但不作为默认行为。

#### 移动文件到另一个资料夹

```
Pool: 不变
Folder: document_instance.folder_id 更新
Wiki: article 不变；反查位置显示新资料夹
```

#### Copy 文件到另一个资料夹

```
Pool: 不变，共享同一个 raw_asset
Folder: 新建 document_instance，指向同一个 raw_asset
Wiki: 默认不自动创建新 article
```

Copy 是组织操作，不是知识生成操作。不要自动制造重复 article、summary、edges。

#### 重新处理文件

```
Pool: 不变
Folder: document_instance 不变
Wiki: 重新生成 article、summary、entities、edges
```

第一版建议“重新生成当前文章”保留 article id；版本化可以后置。

#### 删除文件及派生知识

```
Pool: 若无其他 document_instance 引用，可删除/归档 raw_asset
Folder: 删除/归档 document_instance
Wiki: 删除 article、summary、相关 edges/facts；entity 默认不自动删除
```

这是危险操作，UI 必须展示影响数量。

---

## Stream 资料夹

stream 资料夹用于 RSS、微信公众号等持续订阅。它在用户体验上仍然是资料夹，但多了一个自动入口 connector。

stream 资料夹不是另一套知识模型。它和普通资料夹一样，最终产出：

```
document_instance -> raw_asset -> article
```

区别是：

```
普通资料夹：用户手动放资料。
stream 资料夹：connector 自动放资料。
```

### Stream 链路

```
stream folder
  -> connector
  -> fetched item
  -> raw_asset
  -> document_instance
  -> article
```

stream folder 可以嵌套在普通资料夹下：

```
Client A/
  新闻监控/
    监管 RSS
    某公众号
```

### Stream item 身份

stream 是自动入口，需要 connector 层去重，避免 RSS/公众号重复抓取。

推荐优先级：

```
RSS:
  feed_url + entry_guid
  fallback: canonical_url
  fallback: title + published_at

WeChat:
  feed_id/account + article_url
  fallback: message id
  fallback: title + published_at
```

这不是 raw pool 的全局去重。用户手动重新上传同内容文件，仍可作为新文档实例处理。

### Stream 操作矩阵

#### 新建订阅资料夹

```
Pool: 不变
Folder: 新建 folder(kind=stream)
Connector: 新建 rss/wechat connector
Wiki: 不变
```

#### 立即同步 / 定时抓取

```
Connector: 读取新条目
Pool: 为每条新内容保存 raw_asset 快照
Folder: 每条新内容创建 document_instance
Wiki: 每条新内容创建或排队创建 article
```

#### 暂停订阅

```
Pool: 不变
Folder: folder 仍存在
Connector: status = inactive
Wiki: 已有 article 不变
```

#### 恢复订阅

```
Connector: status = active
下次继续抓取
```

#### 归档订阅资料夹

```
Pool: 不变
Folder: folder.status = archived
Connector: status = inactive
Wiki: 已有 article 默认保留
```

#### 删除订阅资料夹

主操作应是归档或暂停。硬删除放危险区，并展示影响数量。

```
仅删除订阅:
  Connector: 删除/归档
  Folder: 归档或删除
  Pool: 不变
  Wiki: 已抓取内容保留

删除订阅及全部内容:
  Connector: 删除/归档
  Folder: 删除/归档 document_instances
  Pool: raw_asset 若无其他引用再删除/归档
  Wiki: 删除 articles、summaries、edges/facts；entity 默认不自动删除
```

#### 移动 stream 文章到普通资料夹

```
Pool: 不变
Folder: document_instance.folder_id = target folder
Connector: provenance 保留
Wiki: article 不变
```

#### Copy stream 文章到普通资料夹

```
Pool: 不变
Folder: 新建 document_instance，指向同一 raw_asset
Connector: provenance 保留
Wiki: 默认不新建 article
```

---

## Wiki 视角

Wiki 视角用于查看和管理派生知识，不应成为 raw organization 的第二个权威入口。

### 保留操作

#### 打开 article/entity/summary/index

```
Pool: 不变
Folder: 不变
Wiki: 读取对应 node / md 文件
```

#### 定位原始资料

```
Pool: 通过 article -> document_instance -> raw_asset 查询
Folder: 高亮所有当前位置
Wiki: 不变
```

如果同一个 raw asset 出现在多个资料夹，显示多个位置。

#### 删除 article

```
Pool: 不变
Folder: document_instance 保留，显示“知识文章已删除”或允许重新处理
Wiki: 删除 article、summary、edges/facts
```

这不是删除原始材料。

#### 删除 article 及原始资料

```
Pool: 若无其他引用，删除/归档 raw_asset
Folder: 删除/归档 document_instance
Wiki: 删除 article、summary、edges/facts
```

危险操作。

#### 重新生成 article

```
Pool: 不变
Folder: 不变
Wiki: 用当前 document_instance/raw_asset 重跑 ingestion
```

#### summary/entity/index 操作

```
Pool: 不变
Folder: 不变
Wiki: 只影响 summary_nodes/entity_nodes/index_nodes/edges/facts
```

entity 合并、entity 删除、summary 创建/删除等均不应联动删除 raw 或 folder entry。

### 第一版明确不提供的操作

这些操作单个看有价值，但会制造多权威同步问题。第一版应删除或放弃。

1. Wiki 里重命名 article。
2. Wiki 里移动 article 到资料夹。
3. 直接编辑 generated wiki md。
4. Copy 文件后自动生成新 article。
5. 自由创建同一 raw asset 的多个平行 article。
6. 从资料夹“仅移除文件但保留 article”的普通入口。
7. 删除 summary/entity 时联动删除 raw 或 article。
8. 手动修改 stream item 的 connector provenance。
9. 硬删除 source/stream folder 作为主操作。
10. raw pool 级别的文件管理 UI。

核心原则：

```
资料夹管 raw material。
Wiki 管派生知识。
Pool 永远不暴露。
```

---

## UI 方向

`/sources` 需要大型重改。目标不是继续做 source 管理表，而是接近真实文件系统的资料管理界面。

### 总体体验

用户进入“来源/资料”页面时，应看到一个优雅、直观、接近操作系统文件管理器的界面：

```
左侧：资料夹树
中间：当前资料夹内容
右侧或详情抽屉：选中文件/资料夹的元数据、处理状态、Wiki article 链接
```

资料夹树支持嵌套。普通资料夹和 stream 资料夹在视觉上区分，但行为尽量一致。

### 普通资料夹内容

显示：

- 文件/网页名称
- 类型图标
- 处理状态
- 对应 Wiki article 状态
- 上传/捕获时间
- 文件大小
- 所在路径

主要动作：

- 上传文件
- 添加网页
- 新建资料夹
- 重命名
- 移动
- Copy 到...
- 重新处理
- 定位 Wiki article
- 删除文件及派生知识

### Stream 资料夹内容

显示：

- 订阅名称
- connector 类型（RSS/微信公众号）
- 状态（active/inactive/archived）
- 上次同步时间
- 已抓取条目
- 最近失败信息

主要动作：

- 新建订阅资料夹
- 暂停/恢复
- 立即同步
- 归档
- 打开抓取条目
- 定位 Wiki article

### 风格要求

UI 应贴近现有网站设计风格：克制、清晰、工作流优先。不要做营销式大卡片页面。资料管理是高频操作界面，应保持密度适中、层级清楚、按钮克制。

设计上接近真实文件系统，但不要复制桌面系统全部复杂度。第一版保留最有价值的文件管理动作，避免引入多选批量复杂状态，除非实现已经足够稳定。

---

## 当前代码中的迁移风险

现有实现有三处关键耦合，需要在实现时优先拆开。

### source_id 参与 source_item 身份

当前 `source_items` 唯一约束是：

```
UNIQUE (user_id, source_id, origin_ref_type, origin_ref)
```

如果把 source 直接改成资料夹，则移动资料夹会改变 item 身份语义。目标模型应把 `connector_id` / provenance 和 `folder_id` / organization 分开。

### article id 依赖 raw_ref.path/url

当前 article node id 优先由 `raw_ref.path` 或 `raw_ref.url` 生成。移动或重命名 raw 位置会导致同一材料生成不同 article id。

目标模型中 article id 应改为基于稳定身份：

```
article_id = hash(user_id + document_instance_id)
```

或在 book/chapter 场景：

```
article_id = hash(user_id + document_instance_id + logical_part_key)
```

### 入库去重按 raw_ref.path/url

当前 ingestion 会按 `article_nodes.raw_ref->>'path'` 或 `raw_ref->>'url'` 查重。目标模型中去重应按：

```
1. document_instance_id 是否已有 article
2. connector item external key 是否已处理
3. legacy raw_ref.path/url 仅做旧数据兼容
```

不要再把资料夹位置、显示名或 `raw_ref.path` 作为身份。

---

## Raw 保留策略

律师/合规场景中 raw material 是事实证据，不应因为大小超过阈值而自动丢弃。

目标策略：

1. 默认永久保存 raw assets。
2. 移除自动 trim raw 文件的行为。
3. UI 显示 raw 存储占用。
4. 提供手动导出、归档、危险删除。
5. “不含 raw 的导出”可以保留，但不能替代 raw 保存。

---

## 实施建议

建议分阶段迁移，避免一次性破坏 ingestion、search、wiki、graph。

### Phase 1: 增量建模

新增 `folders`、`raw_assets`、`document_instances` / 兼容命名表。保留现有 `sources`、`source_items`，通过 backfill 建立映射。

### Phase 2: 身份拆分

让新入库 article 使用 `document_instance_id` 生成稳定 id。旧数据继续兼容 `raw_ref.path/url`。

### Phase 3: UI 重建

将 `/sources` 改为资料夹式文件管理器。知识库资源管理器不再显示 raw data，只显示 Wiki/KB 对象。

### Phase 4: Stream 接入迁移

把 RSS/微信公众号 source 降级为 connector，绑定到 stream folder。抓取产物统一进入 document instance。

### Phase 5: raw_ref 降级/删除

读路径全部改为 `article -> document_instance -> raw_asset` 后，`raw_ref` 仅作为 legacy 兼容字段，最终可删除。

---

## 设计结论

这次重构的关键不是把 `source` 改名为“资料夹”，而是拆开三个权威：

```
Pool: 真实材料
Folder: 用户组织
Wiki: 知识派生
```

只要稳定身份不再依赖 `source_id`、`raw_ref.path` 或 UI 路径，资料夹就可以像文件系统一样自然操作；同时 Wiki article、summary、entity、graph 也不会因为用户整理文件而意外重建或断链。
