# Codebase Map

## 服务

| 路径 | 说明 |
|---|---|
| `services/api/` | FastAPI 主服务：入库、知识图谱、搜索、MCP 接口、job worker |
| `services/api/routers/folders.py` | **Phase B 新增**：文件夹 / 文档实例 / Connector API（三个 sub-router） |
| `services/web/` | Next.js 前端：知识图谱可视化、资料夹文件管理器 |
| `services/web/app/sources/page.tsx` | **Phase B 新建**：三栏文件管理器 UI（资料夹树 + 内容列表 + 详情抽屉） |
| `services/ingestion-worker/` | 内容抓取与入库 pipeline：RSS/URL/WeChat/PDF/图片/Word/EPUB；Phase B 后传递 document_instance_id |

## 配置

| 路径 | 说明 |
|---|---|
| `config/system.yaml` | 所有可调参数：模型名、token 上限、阈值、枚举值等 |
| `config/prompts.md` | 所有 LLM prompt 字符串，按 `## section_name` 分区 |
| `config/image_processing.toml` | 图片处理专项配置 |

## 文档

| 路径 | 说明 |
|---|---|
| `docs/revision-progress.md` | 重构设计决议 + 各阶段实施进度（事实依据文档） |
| `docs/revision-source-folders.md` | Source/文件夹重构设计文档（三层架构：Pool / 资料夹 / Wiki） |
| `docs/agents/` | Agent skill 配置：issue tracker、triage labels、domain docs |
| `docs/baseline/` | 重构前基线快照 |
| `MEMORY.md` | 系统架构总览：数据模型、API、算法、MCP 工具 |
| `README.md` | 项目简介与快速启动 |

## 基础设施

| 路径 | 说明 |
|---|---|
| `docker-compose.yml` | 生产部署：api / web / ingestion-worker / postgres / watchtower |
| `docker-compose.dev.yml` | 开发覆盖：本地挂载、热重载、workers profile |
| `nginx/nginx.conf` | 反向代理配置 |
| `Makefile` | 常用开发命令（`make dev`、`make logs` 等） |
| `deploy.sh` | VPS 部署脚本 |
| `pyrightconfig.json` | Pyright 静态类型检查配置 |

## 运维脚本

| 路径 | 说明 |
|---|---|
| `scripts/backup.sh` | 数据库备份脚本 |
| `scripts/restore.sh` | 数据库恢复脚本 |
| `scripts/backfill_object_tables.sql` | 历史数据迁移：knowledge_nodes → object 子表 |
| `scripts/cleanup_duplicate_edges.sql` | 清理重复 knowledge_edges |
| `scripts/refactor_smoke.py` | 重构后冒烟测试脚本 |

## 数据目录

| 路径 | 说明 |
|---|---|
| `user_data/` | 运行时数据：wiki 文章文件、原始抓取缓存（gitignore） |
| `data/` | 本地开发数据库持久化目录（gitignore） |
