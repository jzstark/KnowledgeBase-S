# Phase 2：MCP OAuth（云端客户端接入）实施计划

> 状态：**核心上线与客户端验证已完成（2026-09-17）**。LibreChat 静态入口、Claude.ai / ChatGPT OAuth 入口及 OAuth 容器重建均已通过实际调用；未完成的负向和资源验收继续保留在清单中。
> 前置：Phase 1（静态 token MCP）已完成并上线，见 `MAP.md` / `MEMORY.md`。

## 1. 目标与边界

为托管云客户端（Claude.ai web、ChatGPT web）增加受 OAuth 保护的 MCP 接入，准入仅限 **2 个可信 Google 账号**（本人 + 1 位朋友）。

- **Phase 1 对外入口不变**：`https://swanny.laughtale.co.uk/mcp` 继续使用静态 token，服务 LibreChat / Claude Desktop / 脚本，现有客户端配置不改。
- **新增 OAuth 入口**：`https://mcp.laughtale.co.uk/mcp`，供需要 OAuth discovery + CIMD/DCR 的云端客户端使用。
- **LibreChat 暂不切 OAuth**：继续使用现有静态 token；以后如需每个 LibreChat 用户独立授权，可再指向 OAuth 入口。
- **工具只读**：沿用现有 9 个工具（8 个 `/api/kb/v1` 知识库工具 + `get_current_time`），不增加写操作。
- **准入方式**：用户点击连接器登录 Google；服务端验证 `email_verified` 并检查邮箱白名单，不接受用户自行填写一个邮箱字符串作为身份凭证。
- **基础设施保持简单**：继续使用现有单台 AWS EC2、Docker Compose、nginx 和 Cloudflare Flexible/橙云；本阶段不增加 ALB、ACM、Redis、源站 TLS 证书或其他 AWS 服务。

### 为什么云端客户端需要 OAuth

Claude.ai / ChatGPT 的 custom connector 网页 UI 主要以 URL 接入，通过 OAuth discovery 和 CIMD/DCR 完成授权，没有与现有 LibreChat 配置等价的自定义静态 header 入口。因此保留静态入口的同时，新增独立 OAuth 入口。

## 2. 已锁定的决策

| 决策 | 选择 | 理由 |
|---|---|---|
| FastMCP 版本 | **精确锁定 `fastmcp==4.0.4`** | v4 已稳定，直接面向新协议实施；避免刚迁 v3 又升级 v4 |
| 容器形态 | **同一镜像，双实例**：保留 `kb-mcp`，新增 `kb-mcp-oauth` | 一套工具代码、两个 Python 进程；鉴权配置和进程重启独立 |
| 模式选择 | `MCP_MODE=static\|oauth` | 同一镜像按环境变量构建对应 ASGI app；未知或缺失 mode 必须 fail closed |
| 分阶段启用 | `kb-mcp` 默认启动，`kb-mcp-oauth` 使用 `oauth` profile | Google 凭证未就绪时仍能运行静态服务和开发环境 |
| 工具复用 | `tools.py` 中的 `register_tools(mcp)` | 静态门和 OAuth 门不能复制两套工具定义 |
| 静态入口 | `https://swanny.laughtale.co.uk/mcp` | 保持 Phase 1 对外契约不变 |
| OAuth 入口 | **`https://mcp.laughtale.co.uk/mcp`** | 使用 FastMCP 默认、常见的 HTTP 路径；与现有路径一致但由 hostname 隔离 |
| OAuth base URL | `https://mcp.laughtale.co.uk` | OAuth operational/discovery 路由位于子域根；最终 MCP URL = base URL + `/mcp` |
| IdP | Google `GoogleProvider` / OAuth Proxy | Google 不支持 DCR；FastMCP 对 MCP 客户端提供 CIMD/DCR 并代理到预注册的 Google client |
| LibreChat | 暂时继续静态 token | 当前最简单，不强迫现有用户再次 Google 登录 |
| OAuth 存储 | 单机加密文件存储 + Docker volume | 符合当前单 EC2 和预算；不为两个用户引入 Redis |
| Cloudflare | **维持 Flexible + 橙云** | 使用 Cloudflare 边缘 HTTPS，源站仍走 HTTP；本阶段不配置源站证书 |
| AWS | **不改现有网络架构** | 不增加 ALB / ACM / ECS 等成本和运维面 |

## 3. URL 与路由模型

同一套 MCP 工具运行在两个实例中，都使用 `/mcp`，由 hostname 区分入口：

| 用途 | 对外 URL | nginx upstream | 鉴权 |
|---|---|---|---|
| Phase 1 / LibreChat | `https://swanny.laughtale.co.uk/mcp` | `kb-mcp:7878/mcp` | `X-MCP-Token` 或 Bearer 静态 token |
| Phase 2 / 云端 | `https://mcp.laughtale.co.uk/mcp` | `kb-mcp-oauth:7878/mcp` | Google OAuth + Gmail 白名单 |

OAuth 子域还需要把以下路径转发给 `kb-mcp-oauth`：

```text
/.well-known/oauth-authorization-server
/.well-known/oauth-protected-resource/...
/register
/authorize
/token
/auth/callback
/mcp
```

因此 OAuth 子域的 nginx 必须使用 `location /` 转发整个 host，不能只代理 `location /mcp`。

FastMCP 路由参数固定为：

```text
base_url = https://mcp.laughtale.co.uk
mcp_path = /mcp
public MCP URL = https://mcp.laughtale.co.uk/mcp
Google callback = https://mcp.laughtale.co.uk/auth/callback
```

`base_url` 不包含 `/mcp`，否则可能形成 `/mcp/mcp` 或把 OAuth operational 路由错误地放到 `/mcp` 下。

## 4. 架构

```text
                    services/kb-mcp/ (FastMCP 4，一份镜像)
                    ┌─────────────────────────────────────┐
   tools.py ───────►│ register_tools(mcp)  ← 9 个只读工具 │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   kb-mcp container                        kb-mcp-oauth container
   MCP_MODE=static                          MCP_MODE=oauth
   :7878/mcp                                :7878/mcp
              │                                         │
              ▼                                         ▼
   swanny.laughtale.co.uk/mcp               mcp.laughtale.co.uk/mcp
   静态 token，Phase 1 不变                  Google OAuth，Phase 2 新增

   两个容器均使用 KB_SERVICE_TOKEN
                  ↓
   http://api:8000/api/kb/v1（只读 API）
```

两个实例共享 EC2、nginx、知识库 API 和数据库，隔离范围是 MCP 进程及鉴权配置，不是整套系统的高可用。增加的是一个 Python 进程的资源占用；上线验收记录两个容器的内存和 CPU 使用，不增加 EC2 实例。时间工具直接在 MCP 进程内执行。

### 鉴权边界

- **出站 MCP → KB API**：继续使用 `KB_SERVICE_TOKEN`。
- **静态入口的入站鉴权**：保留当前 `MCP_STATIC_TOKEN` 语义，同时接受 `X-MCP-Token` 和 `Authorization: Bearer ...`；空值返回 `503`，错误值返回 `401`。
- **OAuth 入口的认证**：`GoogleProvider` 验证 Google 身份，FastMCP OAuth Proxy 向 MCP 客户端签发自己的 token。
- **OAuth 入口的授权**：全局 `AuthMiddleware(auth=require_allowed_email)` 对所有工具统一检查白名单。

白名单规则：

- `MCP_ALLOWED_EMAILS` 按逗号分隔，读取后逐项 `strip().lower()`。
- 配置为空时 fail closed，OAuth 工具全部不可见、不可调用。
- token 中的 `email_verified` 必须严格为 `true`。
- token 中的 `email` 经 `strip().lower()` 后必须属于白名单。
- 不在每个工具函数内重复鉴权。

## 5. 关键技术依据

- FastMCP 4.0.4 是实施时的当前稳定版；本项目从 `mcp.server.fastmcp.FastMCP` 直接迁到 standalone `fastmcp`，不是从 standalone FastMCP 3.4.7 升级。
- FastMCP v4 HTTP transport 默认使用 `/mcp`；`http_app(path="/mcp")` 明确固定该路径。
- `GoogleProvider` 使用 OAuth Proxy：对 Claude.ai / ChatGPT 提供 CIMD/DCR，对 Google 使用预先创建的 Web OAuth client。
- Google 默认回调路径是 `/auth/callback`，Google Console 中必须精确登记 `https://mcp.laughtale.co.uk/auth/callback`。
- FastMCP 的全局 `AuthMiddleware` 可以对工具列表和调用统一应用自定义 `AuthContext` 检查。
- 当前是单机单实例部署，OAuth client/token storage 使用加密 `FileTreeStore` 并挂持久化 volume；若以后水平扩容，再迁 Redis 等共享存储。

实施参考：

- <https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3>
- <https://gofastmcp.com/servers/auth/oauth-proxy>
- <https://gofastmcp.com/servers/authorization>
- <https://gofastmcp.com/deployment/http>
- <https://gofastmcp.com/servers/storage-backends>
- <https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp>
- [nginx 动态解析与 proxy_pass](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_pass)
- [Cloudflare DNS 记录管理](https://developers.cloudflare.com/dns/manage-dns-records/how-to/create-dns-records/)
- [Cloudflare Flexible 模式](https://developers.cloudflare.com/ssl/origin-configuration/ssl-modes/flexible/)

## 6. 实施步骤

### 6.1 先建立回归基线

- [ ] 记录当前 Phase 1 的 9 个工具名称和 input schema。
- [ ] 用现有静态 token 对 `https://swanny.laughtale.co.uk/mcp` 完成 initialize、list tools 和至少 1 次真实工具调用。
- [ ] 保存 LibreChat 当前 MCP 配置，确认升级后无需修改。
- [ ] 发布前记录旧 `kb-mcp` 镜像 digest，备份当前 Compose、nginx 配置及 VPS 环境配置（secret 备份只放受控位置）。

### 6.2 代码重构（`services/kb-mcp/`）

- [x] 新建 `tools.py`：实现 `register_tools(mcp)`，把当前 `server.py` 的 9 个工具原样迁入；HTTP 请求、参数清洗、返回结构和超时保持不变。
- [x] 新建 `app.py`：
  - `build_static_mcp()`：创建 `FastMCP` 并调用 `register_tools()`。
  - `build_oauth_mcp()`：创建带 `GoogleProvider` 和全局 `AuthMiddleware` 的 `FastMCP`，并调用同一个 `register_tools()`。
  - 两者都通过 `mcp.http_app(path="/mcp")` 暴露 transport。
- [x] 静态模式继续用现有 ASGI `TokenAuthMiddleware` 包裹 MCP app，以兼容 `X-MCP-Token` 与 Bearer 两种 header；不要在迁移时改变 Phase 1 鉴权契约。
- [x] OAuth 模式实现 `require_allowed_email(AuthContext)`，严格执行 `email_verified` 和 `MCP_ALLOWED_EMAILS`。
- [x] OAuth provider 参数：
  - `base_url="https://mcp.laughtale.co.uk"`
  - `required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"]`
  - `redirect_path="/auth/callback"`
  - 显式 `jwt_signing_key`
  - 加密的文件型 `client_storage`，目录固定为 `/data/oauth`
- [x] 保持 FastMCP 默认的授权 consent 防护；不要设置 `require_authorization_consent=False`。
- [x] `__main__.py` 按 `MCP_MODE=static|oauth` 选择 app；缺失或未知 mode 直接退出，不启动无鉴权服务。
- [x] 按模式读取配置：静态模式不读取或校验 Google 凭证、OAuth 密钥和存储；OAuth 模式缺少必要凭证、密钥格式错误或存储不可写时明确退出，不能退回静态或无鉴权模式。
- [x] `requirements.txt`：删除直接依赖 `mcp>=1.9.0`，精确锁定 `fastmcp==4.0.4`；保留代码直接使用的 `httpx`、`uvicorn`，并把 `starlette` 下限提高到 `>=1.0.1`（FastMCP 4 的服务端依赖下限）。
- [x] Docker 镜像仍通过 `python -m kb_mcp` 启动，同一镜像供两个 service 使用。

OAuth 文件存储必须：

- 使用 `FileTreeStore` 的 V1 key/collection sanitization strategy，避免 DCR client ID 中的 URL 字符变成非法或越界路径。
- 用 `FernetEncryptionWrapper` 加密，不得把上游 Google token 明文落盘。
- 使用单独的 `MCP_OAUTH_STORAGE_ENCRYPTION_KEY`；格式为 Fernet key。
- `/data/oauth` 只能挂给 `kb-mcp-oauth`，不得与静态容器或宿主其他目录混用。

### 6.3 测试

- [ ] contract test：迁移前后工具名称、数量和 schemas 一致。
- [x] static test：token 缺失配置 → `503`；未提供/错误 token → `401`；`X-MCP-Token` 和 Bearer 正确值均通过。
- [x] mode test：空值和未知 `MCP_MODE` fail closed。
- [x] OAuth authorization unit test：
  - 白名单为空 → 拒绝。
  - token 无 email / email 未验证 → 拒绝。
  - 大小写和首尾空格归一化后匹配 → 允许。
  - 白名单外账号 → 拒绝。
- [x] builder test：static 和 OAuth builder 都注册同一组工具及 schema。
- [x] 配置隔离：无任何 Google/OAuth 配置时静态模式可启动；OAuth 模式缺少必要配置时启动失败且不影响静态进程。
- [x] HTTP/MCP 授权集成测试：通过真实 app 和鉴权中间件，用受控测试身份验证工具列表过滤和直接按名称调用的拒绝；不能仅测试白名单函数。
- [ ] GoogleProvider 联调：核对其实际输出的 `email`、`email_verified` 值和类型，刷新前后均满足授权检查；不得因类型不符而直接改成普通 truthy 判断。
- [ ] token 生命周期：过期 access token 不可直接使用，客户端刷新后可继续调用；重建 OAuth 容器并保留 volume/密钥后，原客户端注册及尚有效的授权可继续使用，覆盖实际调用和刷新。
- [ ] 权限撤销：从白名单移除用户并重建 OAuth 容器后，该用户已有 token 也不能列出或调用工具；其他白名单用户仍可用。
- [x] dependency smoke test：在新镜像内成功 import FastMCP 4、创建两个 app 并启动 lifespan。
- [x] 本地 client smoke test：对静态 `/mcp` 完成 initialize、list tools 和 `get_current_time` 调用；OAuth discovery URL 与无 token `401` challenge 也已验证。

### 6.4 Google Cloud Console

- [x] 配置 OAuth consent screen；当前两账号私用阶段保持 External + Testing，扩大用户范围前再评估发布状态。
- [x] 创建 OAuth **Web application** client。
- [x] Authorized JavaScript origin：`https://mcp.laughtale.co.uk`。
- [x] Authorized redirect URI：`https://mcp.laughtale.co.uk/auth/callback`。
- [x] 获取 `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`，只写入 VPS `.env`。
- [x] 只请求 `openid` 和 Google userinfo email scope；当前白名单不需要 Gmail 邮件权限。

### 6.5 Docker Compose

保留现有 `kb-mcp` service 名和镜像仓库名，只新增 `kb-mcp-oauth`。两个 service 复用同一个构建产物及 image/build context，不复制工具代码。

`kb-mcp`（现有静态实例）：

- [x] `MCP_MODE=static`
- [x] `KB_API_BASE=http://api:8000`
- [x] `KB_PUBLIC_PREFIX=/api/kb/v1`
- [x] `KB_SERVICE_TOKEN`
- [x] `MCP_STATIC_TOKEN`
- [x] `PORT=7878`
- [x] `expose: ["7878"]`

`kb-mcp-oauth`：

- [x] `profiles: ["oauth"]`，默认不启动。
- [x] `MCP_MODE=oauth`
- [x] `KB_API_BASE=http://api:8000`
- [x] `KB_PUBLIC_PREFIX=/api/kb/v1`
- [x] `KB_SERVICE_TOKEN`
- [x] `MCP_OAUTH_BASE_URL=https://mcp.laughtale.co.uk`
- [x] `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET`
- [x] `MCP_ALLOWED_EMAILS=<本人 Gmail>,<朋友 Gmail>`
- [x] `MCP_OAUTH_JWT_SIGNING_KEY=<强随机且稳定的字符串>`
- [x] `MCP_OAUTH_STORAGE_ENCRYPTION_KEY=<Fernet key>`
- [x] named volume 挂载到 `/data/oauth`
- [x] `PORT=7878`
- [x] `expose: ["7878"]`

所有 secret 只进入 VPS `.env`，不得提交到仓库。

Compose 配套要求：

- [x] 两个 MCP service 都保留 `restart: unless-stopped` 和 `depends_on: api: condition: service_healthy`；7878 只在 Docker 网络暴露，不发布到宿主公网端口。
- [x] `nginx.depends_on` 保留原有 `kb-mcp`，不添加对可选 `kb-mcp-oauth` 的强制启动依赖。OAuth 未启用时 nginx 仍可启动。
- [x] OAuth 环境变量用空默认值传入，必填检查在 OAuth 应用启动时进行；避免 Compose 的 `${VAR:?}` 在未启用 profile 时也阻止整份配置解析。
- [x] `.env.example` 补齐模式、凭证和密钥的占位说明；默认配置及 `workers + oauth` profile 组合均通过 `docker compose config --quiet`。
- [x] `deploy.sh` 默认部署 core + `workers`；OAuth 启用后使用 `./deploy.sh --oauth`，等价启用 `workers + oauth`。脚本不使用 `--remove-orphans`，避免默认部署误删可选 OAuth 实例。

### 6.5.1 镜像发布与 Watchtower

迁移期间使用不可变 commit SHA 固定镜像，避免 Watchtower 在 Compose 环境变量尚未应用时提前升级。核心验收完成后，`main` 分支构建同时发布不可变 SHA tag 和 `stable` tag；Compose 日常使用 `stable`，需要回滚时把 `KB_MCP_IMAGE_TAG` 改为已知 SHA。两个 MCP service 始终排除在 Watchtower 之外，因此 `stable` 只有在显式 pull/up 后才会生效。

- [x] 迁移期间使用 commit SHA 固定并验收静态与 OAuth 实例。
- [x] 两个 MCP service 均已应用 Watchtower 排除标签；恢复其他服务的自动更新不会更新 MCP。
- [x] `main` 分支构建同时发布不可变 SHA tag 和 `stable`；Compose 默认使用 `stable`。
- [x] `MCP_MODE=static|oauth` 与对应 Compose 配置已在 VPS 应用并完成客户端回归。

### 6.6 nginx

保留现有 `swanny` server、`kb-mcp` upstream 名和公网 URL。两个 MCP 入口使用 Docker DNS 的请求时解析，容器重建后地址可重新解析，OAuth 服务缺席也不会导致 nginx 加载配置失败。以下示例的 `127.0.0.11` 适用于当前 Compose 自定义网络，实施时在 nginx 容器内确认 resolver：

```nginx
location /mcp {
    resolver 127.0.0.11 valid=10s ipv6=off;
    set $kb_mcp_upstream kb-mcp:7878;
    proxy_pass http://$kb_mcp_upstream;
    proxy_http_version 1.1;
    proxy_set_header Connection '';
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

新增 OAuth 子域专属 server block。必须代理整个 `/`，因为 discovery、DCR、authorize、token 和 callback 不在 `/mcp` 下：

```nginx
server {
    listen 80;
    server_name mcp.laughtale.co.uk;

    location / {
        resolver 127.0.0.11 valid=10s ipv6=off;
        set $kb_mcp_oauth_upstream kb-mcp-oauth:7878;
        proxy_pass http://$kb_mcp_oauth_upstream;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
    }
}
```

`X-Forwarded-Proto` 必须继续写死为 `https`：Cloudflare Flexible 到源站是 HTTP，但 OAuth 元数据中必须公布公网 HTTPS URL。

变量型 `proxy_pass` 保持上述无 URI 后缀写法，完整保留 `/mcp`、discovery 和回调路径及查询参数；不得额外拼接或删除 `/mcp`。OAuth 未启用时可先安装其 server block，对该子域的请求暂时返回 502，不能回退到静态 MCP 或前端。DNS 仍在正式启用时最后添加。修改配置后必须检查容器内的实际配置、执行 `nginx -t` 并 reload。

### 6.7 Cloudflare 与发布顺序

本阶段固定使用：

- Cloudflare SSL mode：Flexible。
- `mcp.laughtale.co.uk`：橙云代理。
- EC2/nginx：继续只监听 HTTP 80。
- 不申请或安装源站证书。
- 沿用现有 AWS Security Group 和网络；若部署检查发现需要额外变更，单独说明原因，不作为本计划的默认操作。

Cloudflare 操作清单（当前账户配置需由部署者核对）：

| 设置 | 值 |
|---|---|
| DNS 类型 | `A` |
| 名称 | `mcp` |
| IPv4 地址 | 当前 EC2 公网 IP，与现有源站一致 |
| 代理状态 | Proxied / 橙云 |
| TTL | Auto |

- [ ] 检查是否已有同名记录，避免重复或冲突；现有 `swanny` 记录保持不变。
- [ ] 确认边缘证书已签发且覆盖 `mcp.laughtale.co.uk`；这是 Cloudflare 边缘证书，不需要在 EC2 安装证书。
- [ ] 确认新子域实际使用 Flexible，公网 HTTP 请求在边缘重定向至 HTTPS；不在源站添加会与 Flexible 冲突的强制 HTTPS 跳转。
- [ ] 检查已有 Cache Rules、Access、WAF/浏览器挑战规则是否覆盖新子域。MCP、授权及 token 响应不可缓存；机器客户端不能被 Access 登录或浏览器挑战拦截。有冲突时只调整该子域相关规则，不全站关闭防护。
- [ ] Cloudflare 无需为 `/mcp`、`/auth/callback` 分别配置路由，全部由 nginx 和 FastMCP 处理。

发布顺序必须是：

1. 完成本地代码、测试和镜像构建；按 §6.5.1 在发布新版镜像前处理 Watchtower，并保存旧镜像和配置。
2. 应用新版 Compose，保留服务名 `kb-mcp`，设置 `MCP_MODE=static` 后重建静态实例；此时可完全没有 Google 凭证，先完成 LibreChat 回归。
3. 安装支持动态解析的 nginx 配置，验证 OAuth profile 未启用时 nginx 也能加载和启动，现有 web/API/静态 MCP 正常。
4. Google 凭证和密钥就绪后启用 `oauth` profile；从 Docker/宿主网络带正确 Host 验证 OAuth 路由和 discovery，再确认静态入口正常。
5. **最后**在 Cloudflare 添加上述 DNS 橙云记录，确认边缘 HTTPS 证书和相关规则。
6. 验证公网 discovery、Google callback 和完整授权流程，再依次接入 Claude.ai / ChatGPT；完成刷新、重建和故障隔离验收。

OAuth 已启用后的常规 VPS 发布统一使用 `./deploy.sh --oauth`；省略参数只用于尚未
启用 OAuth 的阶段。部署脚本不再自动执行 `docker image prune`，旧镜像确认不再
需要后再人工清理，避免破坏回滚点。

这样可以避免 DNS 提前生效时，新 hostname 落入当前 nginx `default_server`，意外进入现有静态 `/mcp` 路由。

### 6.8 验收

- [x] `https://swanny.laughtale.co.uk/mcp` + 原静态 token 的工具回归通过。
- [x] LibreChat 无需修改配置，仍可正常调用 MCP 工具。
- [x] OAuth discovery 返回的 issuer、authorization、token 和 resource URL 全部为 HTTPS 且 hostname/path 正确。
- [x] `https://mcp.laughtale.co.uk/mcp` 无 token 时返回 OAuth challenge，而不是静态 token 错误。
- [x] 本人 Google 登录后工具可见且可调用。
- [ ] 朋友的白名单 Google 账号可用。
- [ ] 白名单外账号即使完成 Google 身份认证，也看不到工具且无法直接调用；不要求它在 Google 登录页面就被拒绝。
- [ ] `MCP_ALLOWED_EMAILS` 为空时 fail closed。
- [x] OAuth 容器重建并保留 volume/密钥后，Claude.ai / ChatGPT 原连接可继续实际调用。
- [ ] access token 到期后客户端可刷新并继续调用。
- [ ] 移除白名单用户并重建后，其已有 token 的工具访问被拒绝。
- [ ] OAuth profile 未启用、容器停止或移除时，nginx 启动/reload 均成功，web/API/静态入口仍可用；OAuth 请求只影响该子域，不误入静态入口。
- [x] OAuth MCP 容器重建后入口自动恢复，不要求手动重启 nginx；静态 MCP 实例仍可服务。
- [ ] 记录双实例内存和 CPU 使用，确认当前 EC2 能承载。
- [x] Claude.ai 和 ChatGPT 均已连接并完成真实只读工具调用。

### 6.9 回滚

- OAuth 上线失败：停止 OAuth 服务，保留 volume 和密钥；新子域可暂时返回不可用。静态 `kb-mcp` 和 `swanny` 配置继续运行。
- 静态迁移失败：用已记录的旧 digest 重建同名 `kb-mcp`，恢复与旧镜像匹配的 Compose/环境配置；必要时恢复 nginx 配置并校验后 reload，回归 LibreChat。
- MCP 保持排除在 Watchtower 之外；回滚时将 `KB_MCP_IMAGE_TAG` 设为已验收 SHA 后显式 pull/up。不要使用 `docker compose down -v` 或删除 OAuth volume；无需停掉整个知识库栈。

## 7. 暂缓项（不阻塞本阶段）

以下不是本次 implementation 的组成部分：

- Cloudflare Flexible → Full/Strict。
- Let's Encrypt 或 Cloudflare Origin Certificate。
- 将 `mcp.laughtale.co.uk` 改为灰云直连。
- AWS ALB、ACM、ECS、Auto Scaling 或额外实例。
- Redis / DynamoDB OAuth storage。
- LibreChat 从静态 token 切换到每用户 Google OAuth。

暂缓的已知代价：

- Cloudflare 到源站的链路仍是 HTTP。
- 橙云仍受 Cloudflare 代理超时约束；nginx 的 300 秒 timeout 不能取消 Cloudflare 自身的上限。
- 因为源站没有公网可信证书，目前不能直接切灰云作为超时逃生口。

如果以后需要灰云直连，应先给源站配置公网可信的 Let's Encrypt 证书；Cloudflare Origin Certificate 不适合灰云直连，因为普通客户端不会信任它。

## 8. 安全注记

- 接入云端意味着工具返回的知识库内容可被 Anthropic/OpenAI 的服务器和模型处理；只暴露只读工具。
- Google 登录只证明身份；最终准入仍由 `MCP_ALLOWED_EMAILS` 决定。
- `GOOGLE_CLIENT_SECRET`、`MCP_STATIC_TOKEN`、`KB_SERVICE_TOKEN`、JWT signing key、storage encryption key 一律不入库。
- OAuth 存储目录包含客户端注册和上游 token，只允许 OAuth 容器和受控管理员访问。
- 不把白名单授权逻辑放到 nginx；应用层必须执行并测试。

## 9. 实施前输入

- [x] IdP：Google。
- [x] OAuth hostname：`mcp.laughtale.co.uk`。
- [x] OAuth MCP path：`/mcp`。
- [x] FastMCP：`4.0.4`。
- [x] Cloudflare：继续 Flexible + 橙云。
- [x] AWS：不新增基础设施。
- [x] LibreChat：继续使用静态 token。
- [x] 静态服务名保留 `kb-mcp`，只新增 `kb-mcp-oauth`。
- [x] Google `client_id` / `client_secret` 已配置在 VPS `.env`（值不入库）。
- [x] 准入邮箱已配置在 VPS `.env`（值不入库）。
- [x] JWT signing key 与 Fernet storage encryption key 已生成并持久化在 VPS `.env`。

## 10. 推荐实施切口

1. **静态迁移切口**：先迁 FastMCP 4、抽取 `register_tools()`、建立 `MCP_MODE`，只启动现有名称的 `kb-mcp`；无 Google 配置时也可测试。部署前执行 Watchtower 发布保护，以 LibreChat 和 contract tests 证明 Phase 1 契约保持。
2. **OAuth 本地切口**：实现 GoogleProvider、白名单和加密文件存储；使用测试/本地 URL 验证路由、discovery 和授权规则。
3. **双实例切口**：通过 profile 新增 `kb-mcp-oauth`，更新 nginx 动态解析，但先不创建公网 DNS；验证 OAuth 缺席、重建和 nginx 重启场景。
4. **公网切口**：配置 Google Web OAuth callback，最后创建 Cloudflare DNS，依次验收 Claude.ai 和 ChatGPT。

任何切口失败时，都应能按 §6.9 恢复可用的 `kb-mcp`；不得用 OAuth 上线结果作为 Phase 1 静态入口继续可用的前提。
