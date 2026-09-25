# Document lifecycle 重构方案

状态：已实施；验收依据为真实 PostgreSQL 测试、受影响的 API 测试及工作流依赖检查。

## 已确认的决定

- 保持现有状态含义和用户操作流程，修复部分写入和并发一致性问题。
- document lifecycle module 完整负责永久删除，包括派生知识删除、墓碑状态、提交后的文件清理和清理警告。
- module 同时负责单篇和批量操作、删除预检与确认、逐篇结果以及按来源合并 worker 触发；每篇文档有独立事务。
- 增加真实 PostgreSQL 测试，并在 CI 发布相关镜像前自动执行。

领域词汇和范围以 [CONTEXT.md](../CONTEXT.md) 为准。原有行为依据为 [资料夹设计](revision-source-folders.md) 和 [批量操作计划](sources-ui-improvement-plan.md)。

## Module 与 interface

新增 `services/api/document_lifecycle.py`。先采用一个 Python module，其内部函数集中处理数据库和文件操作；按已存在的行为组织 implementation。

| 调用意图 | interface 承诺 | implementation 负责 |
| --- | --- | --- |
| 归档单篇或一批文档 | 返回每篇的归档、跳过或失败结果 | 文件夹范围检查、processing 保护、关联状态一起提交 |
| 请求重试或重新生成 | 返回持久化受理结果及 worker 是否收到触发 | 选择真实 source item、验证可用材料、保留 regeneration intent、按 source 合并触发 |
| 预览永久删除 | 返回影响统计和确认 token | 已有的目标资格、派生知识、共享文件统计和 fingerprint 规则 |
| 执行永久删除 | 返回每篇结果及文件清理警告 | 批量确认校验、锁内状态复核、数据库删除、墓碑、提交后清理和清理重试 |
| 接收 worker 状态报告 | 返回更新后的 source item 或既有拒绝结果 | 原子领取、旧 worker 能力检查、终止状态保护、成对更新及成功后消费 regeneration intent |
| 重试失败的 source item | 保持既有重试行为 | 校验 failed、将 source item 和已关联文档的状态一起更新 |

HTTP route adapter 保留认证、请求解析、日期序列化和既有响应映射。单篇与批量的响应差异由 adapter 保留，两者调用相同的逐篇 implementation。批量 ID 去重、数量限制、目标范围验证、部分失败处理属于 module。

预期业务拒绝通过结果或模块错误表达；数据库异常在事务退出、完成回滚后转换为逐篇失败。module 不依赖 FastAPI 的请求对象。具体函数参数沿用现有操作所需信息，不建立通用状态机框架。

这个 seam 的 depth 来自隐藏完整操作的顺序约束：调用者不需先锁表、再改两个状态、再发送触发。Leverage 是单篇、批量和 worker 调用共享这些保证；locality 是规则和验证集中在同一处。

## 事务与并发

1. 关联文档的操作采用统一锁定顺序：document instance，然后按稳定顺序锁定相关 source items。以 source item 为入口时，先读取关联，再取得锁并复核关联；兼容未关联文档的 source item。
2. source item 与 document instance 的状态更新在同一事务内完成。第二次写入失败，第一次写入也回滚。
3. worker 领取保持 `pending` 条件和旧 worker 的 regeneration capability 检查。归档、删除和领取通过相同记录上的锁串行判定，保留现有允许/拒绝规则。
4. 批量操作逐篇提交；某篇异常记入其结果，继续处理其他目标。最终结果保留输入去重后的顺序。
5. 重新生成请求先持久化，再按真实 source 合并触发。触发失败返回已排队但等待轮询的结果，不撤销排队。
6. 永久删除保持数据库提交后再删文件，重新检查共享引用，并保留失败路径记录与清理重试。

`document_types.py:set_source_item_doc_kind` 的清除类型分支目前先锁 source item，再锁 document instance，与文档操作顺序相反。仅调整相关锁定顺序和必要复核，类型传播和 Wiki 元数据行为继续由 document types module 负责。

以上保证针对这些生命周期操作之间的竞争。上传、复制、移动和 intake 的整体并发协议属于其他工作，不能据此声称所有数据库及文件竞争都已解决。

## 兼容约束

- 保留现有 HTTP 路径、响应字段、状态码以及单篇和批量的区别。
- 保留独立更新 worker 时的兼容检查；worker 与网页使用现有 HTTP adapter。
- 保留批量永久删除的确认流程，以及既有单篇删除流程。
- 保留 archive/delete 终止状态、regeneration intent 的成功消费规则和 source item 重试语义。
- 保留多篇 article 文档暂不支持单篇重新生成的限制。
- 保留无关联 source item 的文档、无关联文档的 legacy source item 等已有情况；不假定一对一关联。
- 保留复制文档的现有 `pending` 含义。无需为这次设计增加状态列或迁移已有状态。

## Test surface 与 adapter

通过 module 的 interface 验证数据库和文件结果。PostgreSQL 使用与项目一致的 `pgvector/pgvector:pg16`，从 Alembic 迁移创建一次性测试库；文件使用临时目录。

worker 触发的内部 seam 有两个具体 adapter：现有 HTTP 调用，以及测试中可控制成功、失败和调用次数的 adapter。测试不调用模型或线上 worker。数据库保持真实 PostgreSQL，以验证 SQL、事务和锁。

| 必须通过的行为 | 验证方式 |
| --- | --- |
| callback/retry 第二次写入失败 | 用测试数据库触发器制造写入失败，验证两张表均保持原值 |
| 两个 worker 同时领取 | 两个独立连接竞争同一 pending 条目，恰好一次领取成功 |
| 领取与归档/删除竞争 | 用显式同步控制两个执行顺序，验证拒绝结果、成对状态和无死锁 |
| 批量部分失败 | 成功、拒绝、数据库异常混合输入，验证成功项仍提交且后续项继续执行 |
| 触发失败 | 验证 regeneration intent 和 pending 状态仍持久化，结果提示等待轮询 |
| 多篇文档共享 source | 验证该 source 只触发一次，所有成功请求在触发前已提交 |
| 删除影响变化 | 获取 token 后改变影响数据，确认返回既有冲突结果且未执行删除 |
| 提交与文件清理顺序 | 数据库失败时文件仍在；提交后文件失败时返回警告并可重试 |
| 共享文件与墓碑 | 删除一个引用保留共享文件；重复处理不能复活墓碑状态 |
| 旧调用兼容 | route adapter 合约测试，以及现有 worker 领取/重新生成相关测试 |
| 类型修改交错 | 覆盖清除类型与生命周期操作的锁定顺序，验证无死锁 |

并发测试通过独立连接、同步点和有限超时控制执行顺序，避免依赖随机 sleep。测试库使用独立名称和一次性存储，测试命令不读取部署 `.env` 或挂载现有资料目录。

保留描述行为的现有测试。真实数据库测试覆盖同一行为后，替换依赖 SQL 字符串和调用次数的旧断言，避免同时维护两套绑定 implementation 的测试。

## 自动发布关卡

- 提供一个本地命令，创建临时 PostgreSQL、迁移、运行测试、销毁测试资源。
- CI 使用一次性 PostgreSQL 容器和同一套迁移、测试入口。
- 将生命周期测试加入 `.github/workflows/build.yml` 的依赖关系，相关 application 和 ingestion-worker 镜像发布必须等待测试成功。
- 应用代码、worker、共享配置、测试基础设施或 workflow 的相关变更都触发检查；手动发布也执行检查。
- 保留其他镜像的现有发布条件，并验证 job 的跳过逻辑不会绕过测试关卡。

## 实施顺序与完成标准

1. 建立测试数据库入口和当前 HTTP 合约基线；用真实 PostgreSQL 重现 callback/retry 部分写入。完成标准：失败场景可重复验证。
2. 落地 lifecycle module 的 callback/retry、锁定规则及归档操作。完成标准：成对回滚和领取/归档并发测试通过。
3. 迁入重新生成的单篇与批量协调。完成标准：持久化意图、部分失败、按 source 触发以及旧 worker 合约通过。
4. 迁入删除预检、确认、单篇与批量删除及清理。完成标准：影响变化、并发删除、共享文件和清理重试测试通过。
5. 接通 CI 发布依赖，并删去本次迁移产生的重复 implementation。完成标准：相关测试通过，测试失败会阻止相关镜像发布，现有调用合约得到验证。

实现期间如发现必须改变状态含义或用户行为的问题，单独列出该决策，不将它隐含在本次重构中。
