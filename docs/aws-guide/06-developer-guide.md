# 开发人员指南

本节面向需要以编程方式与该解决方案交互的开发人员，覆盖认证模型、控制面 REST API、实时聊天接入与授权模型。

> 实时聊天数据面已转型为两级路由直连，详见第 13 章。

该解决方案对外提供两个接入面：**控制面 REST API**（租户注册、生命周期、备份、注销等管理操作，经 `x-api-key` + 可选 Cognito RBAC）和**实时聊天链路**（两级路由，见第 13 章）。

> **Note**
>
> 该解决方案遵循一条贯穿全系统的设计纪律：身份、凭据、配置在租户 microVM 启动前冷注入到只读黄金镜像盘，运行后不开启从宿主机到 microVM 的热注入通道。修改某个租户的身份、技能或开关不是调用运行时 API，而是修改部署代码后重建镜像。该纪律的详细机制参见『架构详情』与『规划部署 — 安全性』。

---

## 认证模型

> 本节讲设计与信任模型，逐字段调用示例与错误码见第 9 章。

认证模型包括唯一信任根、三种 token、两个验签平面、基于角色的访问控制（RBAC，Role-Based Access Control）三级角色，以及零凭据约束。

### 信任根与验签平面

平台有两个正交的身份平面，各自独立信任根：

- **控制面平面（REST API，仍然有效）**：第一道门是 `x-api-key`；启用 RBAC 时叠加 Amazon Cognito 令牌验签。Amazon Cognito 是 AWS 面向 Web 和移动应用的身份平台，此处为控制面 console 登录签发 id_token（RS256），由控制面 Lambda 用 Cognito 的公钥（JWKS，JSON Web Key Set）校验；验签失败一律降级为最小权限（只读 viewer），不带 Bearer 的纯 `x-api-key` 请求按 `DEFAULT_NO_JWT_ROLE`（默认 `viewer`）取角色。资源属主（`owner_id`）检查独立于 RBAC 开关生效。
- **数据面平面（实时聊天，两级路由）**：第一跳用平台自签会话令牌（HS256 JWT，非 Cognito），第二跳用 OpenClaw 原生 Ed25519 device 非对称认证 + per-tenant gateway token。详见第 13 章。

### 三种 token 概览

下表汇总三种 token 的发行方、验签方、载体与生存时间（TTL，Time To Live）。

| token                   | 发行方                                                  | 验签方                                                              | 载体                                                                       | TTL                                                                |
| ----------------------- | ------------------------------------------------------- | ------------------------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| 控制面 Cognito id_token | Amazon Cognito（OAuth2 authorization-code + PKCE 换取） | 控制面 Lambda（RS256，校验 issuer，audience 配了 client id 才比对） | `Authorization: Bearer {id_token}` HTTP 头                                 | id/access token 60 分钟、refresh_token 7 天（Cognito client 配置） |
| 聊天前端 token          | `claw-hub`（HMAC 自签）                                 | `claw-hub`（重算 HMAC-SHA256、常量时间比对）                        | WebSocket 握手 query 参数 `?token=` 或 `/files/*` 的 Bearer 头 / `?token=` | 300 秒                                                             |
| 聊天通道 token          | `claw-hub`（HMAC 自签）                                 | `claw-hub`（重算 HMAC-SHA256、常量时间比对）                        | WebSocket 握手 query 参数 `?token=`                                        | 300 秒                                                             |

> **Note**
>
> 聊天前端 token 与聊天通道 token 都是 `claw-hub` 自签的 HMAC token，格式为 `base64url(claims).base64url(sig)`，使用 hub 的签名密钥做 HMAC-SHA256。两者均与 Cognito id_token 解耦，但聊天前端 token 的签发以 Cognito 验签通过且租户授权检查通过为前提。

### 控制面 Cognito id_token

调用控制面 REST API 时，在 `Authorization: Bearer {Cognito id_token}` 中携带 Cognito 登录后的 id_token。控制面用标准 JWT 库做 RS256 签名验证，通过 Cognito 的 JWKS 端点拉取公钥，issuer 校验为 `https://cognito-idp.<region>.amazonaws.com/<pool_id>`；audience 默认不强制（仅在配置了 client id 时尽力比对），缺过期时间或缺签发方视为无效。验签得到的用户唯一标识（`sub`）作为调用者身份，验签失败一律降级为 viewer。

调用者可以是原生 Cognito 用户，也可以是经外部 OIDC 联合登录进来的用户。对后者，验签后还会从令牌中抽出外部账户的稳定 id 记下来，仅用于把租户归因回外部账户，不参与验签或授权决策（授权仍以 Cognito `sub` 为准）；原生 Cognito 用户无此字段。

> **Important**
>
> 不带 Bearer token 的请求被当作可信自动化处理，此时调用者身份是内置的「密钥调用者」（api-key），等同 admin 全权限。也就是说网络层面已可信的内部自动化（无 JWT）拿到的是全权限。对外暴露该 API 时，必须保证「无 Bearer 即全权」这条路径只对可信网络开放（经 Amazon API Gateway，带 `x-api-key`）。Amazon API Gateway 是 AWS 托管的 API 网关，承载 REST/HTTP API 并对接后端；本平台用它对外暴露控制面 API。

### RBAC 三级角色

RBAC 角色等级为 viewer（只读）< operator（运维）< admin（管理）。只读白名单内的路由 viewer 可达，其余需 operator 及以上；角色检查在路由命中后、业务逻辑执行前统一运行。**属主（owner）检查与 `RBAC_ENABLED` 解耦，始终执行**：`RBAC_ENABLED` 只控制按路由的*角色*门（viewer/operator/admin），不再兼任属主检查的总开关。任何非管理员调用方对每条 per-tenant 记录都只能访问自己 `owner_id` 名下的资源，无论 `RBAC_ENABLED` 取值——关闭 RBAC 不会把 `GET/PUT/DELETE /tenants/{id}` 变成跨租户越权（IDOR）。关闭 RBAC 时每个调用方解析为 API-key 管理员身份，因而单租户控制面语义自然成立。`RBAC_ENABLED` 默认为 `true`；仅在 demo 或单租户开发环境需要开放*角色*门时才显式关闭。接入前请确认目标环境的取值。

### 聊天前端 token

前端拿到聊天前端 token 才能连 hub 的 WebSocket。发行流程为 `POST /token`，header 带 `Authorization: Bearer {Cognito id_token}`，body `{tenant_id}`。hub 先远程拉取 Cognito 的 JWKS 做 RS256 验证（校验签发方，若配了 client id 再校 audience），任何失败一律拒绝；验签成功后取用户唯一标识（`sub`），再做一次显式租户授权检查（该用户是否为该租户的 owner 或被授权人）。授权通过才签发 token，授权失败返回 403。聊天前端 token 携带四个 claim：

| claim    | 值                                    | 说明                                               |
| -------- | ------------------------------------- | -------------------------------------------------- |
| `role`   | `"frontend"`                          | 固定                                               |
| `sub`    | Cognito sub                           | 服务端验过的用户身份，不信任客户端断言             |
| `tenant` | tenant_id                             | 该 token 绑定的租户                                |
| `access` | `owner` / `<granted role>` / `shared` | 取决于授权记录（参见『租户授权模型与跨租户隔离』） |
| `exp`    | Unix 秒                               | 发行时刻 + 300s                                    |

Cognito 区域、用户池、client id 都从环境变量读取并以空值兜底。未配置用户池的环境验签直接失败，无法签发前端 token，即 Amazon Cognito 是聊天平面的前置依赖。

### 聊天通道 token

租户 microVM 内的 `claw-channel` 用聊天通道 token 向 hub 注册出站连接。发行流程为 `POST /channel-token`，body `{tenant_id, appId, timestamp, signature}`，其中 `signature = HMAC-SHA256("{appId}:{timestamp}", Buffer.from(appSecret, "hex"))`，时间窗口 ±300 秒，用常量时间比较防时序攻击。验证通过后签发通道 token。

聊天通道 token 只带三个 claim，不含 `sub`：`role:"channel"`、`tenant:tenantId`、`exp`（TTL 300s）。因为 channel 是租户 microVM 侧的单租户注册，它代表的是一个租户而非某个用户。一个 microVM 只持有一个租户的 appSecret（启动前冷注入），所以一个 channel 只能注册一个租户，跨租户 channel 需要各自的秘密。

### WebSocket 握手与 token 验证

hub 验证自签 token 的方式为重算 HMAC、常量时间比对、检查 `exp` 过期、解析 claims。WebSocket 握手时 token 走 query 参数 `GET /ws?token={hub token}`，`maxPayload` 限制为 1MB。验签失败以 WebSocket close code 1008 关闭连接。握手成功后按角色落入不同路由表：通道连接按租户登记，返回 `{type:"registered"}`；前端连接按用户登记（同一用户的多个标签页归到一起），返回 `{type:"ready"}`。

### 零凭据约束与秘密管理

租户 microVM 侧是零凭据黄金镜像：没有任何 AWS 访问密钥或 Amazon Simple Storage Service（Amazon S3）权限，只持有本租户的 appSecret（通道 HMAC 签名用）。需要读写 Amazon S3 文件时，channel 必须向 hub 申请预签名 URL（presigned URL），hub 是唯一持有 Amazon S3 实例角色的组件，把 Amazon S3 爆炸半径收在一处。

appSecret 存在两处：microVM 本地配置文件（只读数据盘）和租户表里的对应字段。注入采用控制面预生成模型：控制面在建租户时铸出每租户唯一的 32 字节随机密钥，写进租户记录，并在启动 microVM 时由部署脚本注入到本地配置；参数缺省时（兼容路径）才回退到 microVM 内本地自生成。hub 验签时从租户表读取该密钥。预生成保证租户记录在 microVM 启动前就有密钥，hub 对 channel 的第一次注册即可验签通过，避免启动竞态。整个过程无硬编码，秘密不离开 AWS 与 microVM。

同一段冷注入还处理每租户的 LiteLLM 计费虚拟 key（vkey）：有专属 vkey 就把 microVM 调 LLM 的 key 覆写成它（花费、预算、限速按租户拆分），否则保留镜像共享 key。vkey 与 appSecret 一样只落每台 microVM 的数据盘，不进只读黄金镜像，也不进浏览器。

> **Important**
>
> 秘密轮换遵守「改部署代码后重建」纪律：hub 的 token 签名密钥默认随机 32 字节、可由环境变量覆盖；appSecret 冷注入后不热更新，轮换需重新构建镜像或修改启动配置。所有凭据在本指南一律以 `[REDACTED]` 占位。

### Guest 与凭据外泄拦截

没有 Cognito 身份（无 id_token）的访客无法调用 `/token`（缺 Bearer 直接 401）。即便有合法 Cognito 登录，若该用户既不是租户 owner、又不在授权名单里，hub 一律返回 403。共享或遗留节点（没有明确属主的老记录）默认也被拒绝，只有显式打开共享访问开关才放行 shared 角色（默认关闭，失败即拒，fail-closed）。

凭据外泄拦截在工具真正执行之前生效（不依赖模型自我约束）：拦截读取云访问密钥与临时会话凭据，并拦截命令行历史、本地环境配置、SSH 目录等敏感文件读取。两道拦截都采用 fail-closed 策略，即便判断逻辑本身出错也拦死而非放行。配合零凭据约束（microVM 不持有任何 AWS 凭据），凭据外泄面被压到最小。该层属于 L2 工具执行层护栏，详见『规划部署 — 安全性』。

### 前端登录流程

前端（小程序或 Web）拿 Cognito id_token 走 OAuth2 authorization-code + PKCE，这是接入方实现登录页要遵循的流程，也是上述所有 token 的起点。

未登录时前端生成 PKCE `code_verifier`（随机），计算 S256 `code_challenge`，跳转 Cognito Hosted UI `GET /oauth2/authorize?response_type=code&scope=openid+email&code_challenge_method=S256&...`；回调带 `?code=` 返回后，用 `code` 加 `code_verifier` 调 `POST /oauth2/token`（`grant_type=authorization_code`）换回 id_token 加 refresh_token，存入 localStorage。authorization code 一次性，前端换取前先把 `?code=` 从地址栏抹掉并对换取失败做计数，防止带旧 code reload 死循环。

token 生命周期分两层，刷新机制不同。hub 自签 token（前端与通道连 WebSocket 用，TTL 300s）由三方各自留余量主动刷新：通道侧过期前 60s 清缓存、提前刷 token 再重连；前端侧 WebSocket 连接超过 270s（留 30s 余量）就强制重连换新 hub token；Media 预签名 URL 同样 TTL 300s。Cognito id_token 配成 id/access token 60 分钟、refresh_token 7 天，前端临过期时用 refresh_token 静默换新，7 天内不踢回登录页；换 hub token（`POST /token`）若收 401，前端先用 refresh_token 静默换一次 id_token 重试，换不动才清本地 token 跳登录。

> **Note**
>
> 外部 OIDC 联合接入为可选项，默认关闭。Cognito 用户池可挂一个外部 OIDC 身份提供方，让外部账号体系的用户用既有账号登录同一个池，由配置中的外部身份提供方段控制。外部提供方的 client secret 由 AWS Secrets Manager 托管（明文不进部署模板）。联合登录后 hub 仍只验 Cognito 签发的 id_token（信任根仍是 Cognito），外部账户的稳定 id 仅作归因用。接入方要打开外部登录，需在目标环境配好外部身份提供方段，并在用户池上预建好对应的自定义属性。

---

## 控制面 REST API

本节介绍控制面 REST API 的端点契约。控制面是一个 AWS Lambda 函数，提供租户注册、生命周期、备份、宿主机管理、技能与分组管理、审计日志等能力，由 Amazon DynamoDB 持久化。所有端点经 Amazon API Gateway，需 `x-api-key`，默认开启 RBAC。下面按域整理，每个端点给出方法与路径、RBAC 级别、作用、关键参数与主要状态码。

### 租户注册

#### POST /tenants

**RBAC：operator+。** 注册租户，生成 `tenant_id`（`name-xxxx` 格式）。默认同步路径返回 201：初始状态为 `pending`（无宿主机容量，返回 `{id, status:pending}`，自动触发扩容）或 `creating`（有容量，返回 `{id, host_id, guest_ip, host_port, status:creating}`）。开启创建削峰队列（`CREATE_VIA_QUEUE`／`scaler.create_via_queue`，默认关）后改走异步：入 Amazon SQS 削峰，返回 202 `{id, status:queued}`，随后轮询 `GET /tenants/{id}` 到 running。大规模建租户时推荐开队列（见问题排查的 Systems Manager 并发说明）。

关键参数：

- **必填**：`name`（DNS-label，≤32 字符，正则 `^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$`）。
- **可选**：`vcpu`（脚本默认 2）、`mem_mb`（脚本默认 4096）、`data_disk_mb`（代码默认 2048，以环境变量为准）、`config_template`、`restore_from`（对象，`{tenant_id, timestamp?}`，缺省用最新备份）、`clone_from`（id，源须 `running`、强制同宿主机）、`preferred_host_id`、`tags`（字典，≤20 项，key≤50 字、value≤100 字，key/value 禁含冒号）、`ttl_hours`、`on_expiry`、`schedule`、`skills`（列表）、`group`。`restore_from` 与 `clone_from` 互斥。
- **可选（幂等与标准字段）**：`client_token`（4–128 ASCII，幂等键；同 `client_token`＋同 `name` 派生同一 `tenant_id`，重复提交因条件写返回 409 而非重复开机；不传则每次生成新 id）、`image_id`（黄金镜像版本，DNS-label 格式，默认 `v2`）、`security`（加密配置对象，字段间有依赖，见下）。
- **`security` 对象**（可选，per-tenant 加密/证书配置，命名对齐 AWS S3 `ServerSideEncryptionConfiguration`）：`storage_encrypted`（布尔）、`encryption_type`（`none`｜`platform_managed`｜`tenant_cmk`，其中 `tenant_cmk` 为客户自带密钥 BYOK）、`kms_key_arn`／`cert_arn`／`secret_ref`（均为完整 ARN，非裸 id；`secret_ref` 存 AWS Secrets Manager 引用而非密钥内容）。校验不变量：未加密不得带 KMS key，`tenant_cmk` 必须带 `kms_key_arn`。这些均为引用/配置（非机密），存入记录并可在查询时回显。

> **Note**
>
> `vcpu`／`mem_mb` 的脚本默认值（2 vCPU／4096 MB，`deploy/userdata/launch-vm.sh:34-35`）与中心配置文件默认（`config.yml.example:36-37` `default_vcpu: 2`／`default_mem_mb: 4096`）一致。容量规划另按每 microVM 2 GB 内存口径推算稳态密度，实际以调用方传入参数为准，详见『使用解决方案 — 容量配置』。

容量分配采用 CAS（Compare-And-Swap）原子 claim vm_num 并预留 used_vcpu/used_mem_mb，冲突重试 8 次；launch 失败回滚容量。

主要状态码：

| 状态码 | 触发条件                                                             |
| ------ | -------------------------------------------------------------------- |
| 201    | 注册成功                                                             |
| 400    | 参数校验失败（错误体带稳定 `code`，如 `VALIDATION`）；clone 容量不足 |
| 409    | 同 id 已存在（幂等重放，错误体 `code:CONFLICT`）                     |
| 503    | 无容量 / CAS 竞争失败                                                |

> **Note**
>
> 错误响应体除 `error`（人读文本，可能变）外带一个稳定的机器可读 `code`（如 `VALIDATION`、`CONFLICT`），供客户端在代码里区分错误而不必解析文本。老客户端只读 `error` 仍兼容。

> **Note**
>
> 注册时控制面自动做两件身份与计费写入（接入方不必传）。其一，每租户 LiteLLM 计费 vkey：控制面向 LiteLLM `/key/generate` 申请该租户专属虚拟 key，让花费、预算、限速按租户与 Cognito sub 拆分；成功则存入 Amazon DynamoDB `litellm_vkey` 字段并在启动 microVM 时注入。该逻辑依赖控制面配了 LiteLLM master key（存 AWS Secrets Manager），未配 master key 或 `LITELLM_BASE_URL` 时跳过铸造，租户回退镜像内共享 key，非无条件生效。其二，外部账户归因：OIDC 联合登录调用者的 `tenant_user_id` 在有值时写入 Amazon DynamoDB `tenant_user_id` 字段；原生 Cognito 或 api-key 调用者无此字段。其三，标准标识字段：`uuid`（= 稳定主体标识，取自验签得到的 Cognito `sub`，即 `owner_id`；注意租户主键仍是 `id`＝`name-xxxx`，`uuid` 不做主键，故一个用户可拥有多个租户）、`created_at`、`image_id`（创建时的黄金镜像版本，默认 `v2`）随记录写入并可在查询时回显。

### 用户自助注册

#### POST /tenants/self

**RBAC：在 viewer 白名单（真正的 self-only 与上限校验在 handler 内）。** 已登录终端用户为自己开通节点（区别于 operator 级 `POST /tenants`），返回 201 或 4xx。任何验证过的 Cognito 用户可调用，但只能给自己开：`owner_id` 强制为调用者验证过的 `sub`，body 里的 `owner` 与 `owner_id` 被剥离；缺 name 时自动生成 `u-<sub前8位>`。校验后委托内部注册逻辑，宿主机调度、vkey、技能作用域与 `POST /tenants` 完全一致。

关键参数与约束：

- 必须为真实登录用户，api-key 自动化、未验证 token、`API_KEY_OWNER` 一律返回 401。
- `SELF_MAX_NODES_PER_USER`（默认 1，0 表示不限）经 owner 索引 COUNT 非 deleted 节点，达上限时返回 409；COUNT 失败时 fail-closed（当作已达上限，防 Amazon DynamoDB 抖动被刷爆）。

主要状态码：

| 状态码 | 触发条件                                                    |
| ------ | ----------------------------------------------------------- |
| 201    | 开通成功                                                    |
| 401    | api-key 自动化 / 未验证 token / `API_KEY_OWNER`             |
| 403    | `EXTERNAL_AUTHZ=true`（谁能拿节点由外部后端决定，不走自助） |
| 409    | 达到 `SELF_MAX_NODES_PER_USER` 上限                         |

> **Note**
>
> 该端点与前端「＋ 开通我的 AI 节点」入口已落地。接入前请确认目标环境的 `EXTERNAL_AUTHZ` 与 `SELF_MAX_NODES_PER_USER` 取值。

### 租户状态机与生命周期操作

租户状态机为 `pending → creating → running|stopped|paused →（各操作）→ deleted`。`pending` 表示无容量、自动触发 ASG scale-out，等后台 `process_pending`（由 Amazon EventBridge 驱动）处理；`creating` 表示宿主机已分配、microVM 启动中、健康检查轮询至 running；`running` 表示启动完成、channel_secret 已镜像到 Amazon DynamoDB、可收聊天（gateway_token 也在此时镜像，但仅供管理员 control UI，与聊天链路无关，详见能力边界）。

#### POST /tenants/{id}/{action}

**RBAC：operator+。** `action ∈ {resize | resize-disk | migrate | restart | stop | start | reset | pause | resume | backup | access}`，进入先做 IDOR（Insecure Direct Object Reference）owner 检查，非属主返回 403。`start/stop/restart/pause/resume/reset` 在配了 `LIFECYCLE_QUEUE_URL` 时入 Amazon Simple Queue Service（Amazon SQS）削峰，返回 202 queued，不配则同步执行。

三个特殊 action：

- **resize**：热加 vCPU（给 `mem_mb` 被拒，不支持在线内存 resize），仅 `running`、`new_vcpu > current`，永远同步，返回 200。
- **resize-disk**：离线扩数据盘，`new_size_mb` 必须 > 当前且 ≤1 TiB（不缩容），成功才落库。返回 200 / 400 / 502。
- **migrate**：跨宿主机快照迁移，接收 `{target_host_id}`，永远异步，返回 202，触发健康检查 sweep 轮询（状态机 `{status:migrating, migration_phase:snapshot|restore}`，成功转 running 并更新 host_id，失败回滚原状态）。`BALLOON_ENABLED=true` 时返回 409（Firecracker 不支持带 balloon 的 snapshot，改走备份加重建加恢复）。该路径属边缘运维路径，端到端真机成功与否待核。

主要状态码：

| 状态码 | 触发条件                                                   |
| ------ | ---------------------------------------------------------- |
| 200    | resize 成功 / 同步生命周期动作成功 / resize-disk 成功      |
| 202    | 配了 `LIFECYCLE_QUEUE_URL` 时的削峰入队 / migrate 异步触发 |
| 400    | resize-disk 参数非法（不大于当前或超 1 TiB）               |
| 403    | IDOR owner 检查不通过                                      |
| 409    | `BALLOON_ENABLED=true` 时的 migrate                        |
| 502    | resize-disk 后端失败                                       |

### 删除

#### DELETE /tenants/{id}

**RBAC：operator+。** `DELETE /tenants/{id}?keep_data=(true|false)&skip_backup=(true)` 删除 Application Load Balancer（ALB）规则、停止 microVM，`keep_data=false` 时清数据目录，返回 200（幂等；已 deleted 仍返回 200）。流程为 IDOR owner 校验 →（销毁前强制备份）→ 停 microVM → 清 microVM 元数据 → 移除 ALB 规则 → 撤销 DNAT → （可选）删数据目录 → 更新宿主机容量计数 → 回收 LiteLLM vkey → 软删除（status:deleted）。

> **Important**
>
> 销毁前强制备份是不可逆保护：`keep_data=false`（默认 true）且未带 `?skip_backup=true` 时，先同步调用备份 Lambda 把数据盘传到 Amazon S3；备份失败返回 502 并中止删除（不删数据）。确认无数据可留时传 `?skip_backup=true` 跳过；`keep_data=true`（软删保留磁盘）不触发备份。

vkey 回收：删除时回收该租户 per-tenant LiteLLM vkey（`POST /key/delete`，best-effort 不阻塞删除），并移除记录里的 `litellm_vkey`，防 churn 累积孤儿 key。

主要状态码：

| 状态码 | 触发条件                     |
| ------ | ---------------------------- |
| 200    | 删除成功（幂等）             |
| 403    | IDOR owner 检查不通过        |
| 502    | 销毁前强制备份失败，中止删除 |

### 备份

- **POST /tenants/{id}/backup**（RBAC operator+，归在生命周期路由下）：异步调备份 Lambda（`InvocationType:Event`），永远返回 202 `{status:started}`，不改状态。
- **GET /backups**（RBAC viewer）：列所有租户的所有备份加 `tenant_exists` 孤儿标记，返回 `[{tenant_id, tenant_name, tenant_exists, timestamp, size_bytes, last_modified}]`。
- **GET /tenants/{id}/backups**（RBAC viewer，经 owner 门控）：列单租户备份 `[{key, timestamp, size_mb}]`。

恢复时，解析后的备份 key 在启动 microVM 时传入，从 Amazon S3 下载备份覆写数据盘（而非用 data_template）。

### 查询类端点

#### GET /tenants

**RBAC：viewer。** 列租户；非管理员只看自己 `owner_id` 的租户（属主过滤始终生效，与 `RBAC_ENABLED` 解耦——管理员/API-key 看全量）。支持 `?tag=key:value` 多值过滤（AND 语义）。

响应脱敏：每条记录返回前投影掉服务端密钥 `channel_secret`（hub HMAC 注册密钥）和 `litellm_vkey`（计费 key），不下发浏览器。聊天 UI 用 Cognito Bearer 调本端点，泄露 `channel_secret` 会让登录用户伪造该节点 channel 注册，构成 IDOR 与凭据泄漏。

分页（向后兼容）：无 `?limit` 无 `?next_token` 时返回裸数组 `[{...}]`（老调用方不变）；带 `?limit=N`（1-1000，默认 100，非法回落 100）或 `?next_token` 时返回信封 `{tenants:[...], next_token, count}`。`next_token` 是 opaque base64 游标（Amazon DynamoDB `LastEvaluatedKey` 编码），原样回传取下一页，取完为 `null`，不要手工构造。

#### GET /tenants/{id}

**RBAC：viewer，handler 内做 owner 门控。** 获取单租户，返回 `effective_skills`（resolved 技能列表，或 `'*'` 表示 broadcast），同样脱敏 `channel_secret` 与 `litellm_vkey`。

#### GET /tenants/{id}/access

**RBAC：viewer，经 owner/admin 门控。** 获取显式授权列表及 `owner_id`，返回 `{id, owner_id, authorized_users}`（详见『租户授权模型与跨租户隔离』）。

### 批量与授权

#### POST /batch/tenants

**RBAC：operator+。** body `{action:(stop|start|delete|backup), 恰好一个 ids:[...] 或 filter:{tag:k:v}, async?}`（ids 与 filter 互斥）。同步与异步口径：`ids ≤ 100` 且未带 `async:true` 时同步返回 200 加 `{succeeded, failed}`；`ids > 100` 或显式 `async:true` 时转异步 job，写 job 记录后立即返回 202 加 `{job_id, status, total}`（后台 worker 分批执行）；`ids > 100000` 硬上限返回 400。异步需部署批量 jobs 表（`BATCH_JOBS_TABLE`），否则大批量返回 503。

主要状态码：

| 状态码 | 触发条件                             |
| ------ | ------------------------------------ |
| 200    | `ids ≤ 100` 且非 async，同步执行     |
| 202    | `ids > 100` 或显式 async，转异步 job |
| 400    | `ids > 100000` 超硬上限              |
| 503    | 大批量异步但未部署 jobs 表           |

#### GET /batch/jobs/{job_id}

**RBAC：viewer。** 查异步批量进度，返回 `{job_id, action, status:(queued|running|done|dispatch_failed), total, done, succeeded:[{id,action}], failed:[{id,error}], created_at, updated_at}`（不回显原始 ids 列表）；无 jobs 表返回 503。

#### POST /tenants/{id}/access

**RBAC：仅 owner/admin 可操作。** 显式授权，body `{principal, op:(grant|revoke), role?, expire_at?}`，grant 时持久化到 `authorized_users` map（详见『租户授权模型与跨租户隔离』）。

### 按外部用户管理节点舰队

这组端点供外部后端按一个外部用户（`tenant_user_id`）管理其名下所有节点，靠 tenants 表 `gsi_tenant_user` 索引反查（节点的 `tenant_user_id` 创建时写入，仅 OIDC 联合登录用户有，是数据事实而非事后拼接）。三端点共用鉴权：管理员或 api-key 可管任意用户；普通登录态只能管自己（请求 `tenant_user_id` 须等于调用者 token 的 `tenant_user_id`，否则 403）；RBAC 关闭时放行。

> **Note**
>
> `gsi_tenant_user` 是 config-gated 索引（默认不建，Amazon DynamoDB 一次 update 只能加一个 GSI，需单独部署转 ACTIVE）；缺索引时降级（query 找不到索引），不阻塞核心 CRUD。目标账号该索引是否已 ACTIVE 待核。

- **GET /users/{tenant_user_id}/tenants**（RBAC viewer）：列该用户名下所有节点（索引 query），分页 `?limit=N`（1-1000，默认 100）加 `?next_token`（opaque 游标），返回 `{tenants:[...], next_token, count}`。
- **GET /users/{tenant_user_id}/summary**（RBAC viewer）：只读汇总 `{tenant_user_id, total, by_status:{running:N,...}, truncated}`；内部分页累计，超安全上限（1000/页 × 50 页）时 `truncated:true`（不报错，提示结果可能不全）。
- **POST /users/{tenant_user_id}/action**（RBAC operator+，不在 viewer 白名单）：对该用户全部节点批量执行一个动作，body `{"action":(start|stop)}`（只这两个，不含 delete），目标集由索引 query 得到（不需传 id 列表），逐节点复用生命周期路径（同 IDOR 加审计），返回 `{tenant_user_id, action, succeeded:[{id,action}], failed:[{id,error}], truncated}`。

### 外部授权写入

#### POST /external/authz

**鉴权：不走 Cognito 与 RBAC，改用 HMAC。** 供外部后端把「用户↔租户」授权（grant/revoke）写入平台，作为映射的写权威外置入口（开启后租户归属由外部后端决定，不再由创建者隐式派生）。仅 `EXTERNAL_AUTHZ=true` 时启用（默认关，关时返回 404）。

鉴权细节：头 `x-claw-authz-timestamp` 为 Unix 秒（±`EXTERNAL_AUTHZ_TS_WINDOW`，默认 300s，防重放）、`x-claw-authz-signature` 为 `HMAC-SHA256(共享密钥, f"{timestamp}.{原始body}")` 的 hex，常量时间比对；大写 `X-Claw-Authz-*` 仅向后兼容回退；共享密钥存 AWS Secrets Manager，经 AWS CloudFormation 动态引用注入，明文不进模板。

请求体：`{tenant_id, "principal"|"tenant_user_id", op:(grant|revoke), role?, expire_at?}`，写入租户记录 `authorized_users`（hub 与控制面读同一份）。

主要状态码：

| 状态码 | 触发条件                             |
| ------ | ------------------------------------ |
| 200    | 写入成功                             |
| 401    | 缺签名 / 坏时间戳 / 超窗 / 坏签名    |
| 404    | `EXTERNAL_AUTHZ` 未启用 / 租户不存在 |
| 503    | 共享密钥未配置                       |

### 宿主机管理

- **GET /hosts**（RBAC viewer）：列非 deleted 宿主机，过滤内部记录，附 overcommit 比率。
- **POST /hosts**（RBAC operator+）：注册宿主机 `{instance_id}`，经 Amazon Elastic Compute Cloud（Amazon EC2）describe 拿规格，缺 `instance_id` 返回 400，成功返回 201。该写端点依赖真实 Amazon EC2 环境。
- **DELETE /hosts/{instance_id}**（RBAC operator+）：标 draining 加 ASG terminate，返回 200。
- **GET /hosts/rootfs-version**（RBAC viewer）：读 Amazon S3 manifest 的 version，缺失返回 unknown。
- **POST /hosts/refresh-rootfs**（RBAC operator+）：推 rootfs、data、immutable 三个镜像分片到所有 active/idle 宿主机，标 version in-flight，不等完成、不确认；无 manifest 返回 500，无宿主机返回 200 `updated:0`。

宿主机容量计数考虑 overcommit（`CPU_OVERCOMMIT_RATIO` / `MEM_OVERCOMMIT_RATIO`），可用容量 =（total × ratio）− used（ratio 默认 1.0）。guest IP 计算为 `block=(vm_num-1)*4`，`IP = SUBNET_PREFIX.{block//256}.{block%256+2}`。

### 技能与分组管理

- **GET /groups**（RBAC viewer）：列 per-group 技能集。
- **POST /groups**（RBAC operator+）：建分组 `{name(DNS-label 必填), skills?, description?}`，缺名/非法名/skills 非列表返回 400、重名 409、无 `GROUPS_TABLE` 503。
- **POST /groups/{name}/skills**（RBAC operator+）：追加技能（幂等）。
- **DELETE /groups/{name}/skills/{skill}**（RBAC operator+）：移除技能。
- **GET /skills/{name}**（RBAC viewer）：从 Amazon S3 读 `skills/{name}/SKILL.md`，非法名 400、无 bucket 503、缺失 404、成功 200。
- **PUT /skills/{name}**（RBAC operator+）：更新（校验 UTF-8、≤256KiB、至少一行顶级 `# Title`），非法/超大/无 H1 返回 400，新建 201、替换 200。
- **DELETE /skills/{name}**（RBAC operator+）：删整个前缀，前缀空 404、成功 200 `{deleted:N}`。

技能分发为合并 `tenant.skills + groups[tenant.group].skills`；返回 None 时 broadcast 所有技能。

> **Note**
>
> `GET /skills`（列表，可选 `?tenant` 过滤）是独立的 skills Lambda，不是控制面 api Lambda，不要给 `/skills` 写 PUT/DELETE。

### 系统与审计

- **GET /system/info**（RBAC viewer）：环境派生的特性快照（version、region、agentcore、metrics、multi_az、waf、cognito{rbac_enabled}、notifications、quotas、host_config），只读。
- **GET /agentcore/status**（RBAC viewer）：返回 `{enabled, gateway_url}`，config-gated 默认 disabled。
- **GET /agentcore/tools**（RBAC viewer）：返回写死的静态三工具 `{hello, system_info, timestamp}`（非实时查 Gateway；disabled 时返回 `{enabled:false, tools:[]}`）。整个 agentcore 功能 config-gated，默认关。
- **GET /audit-log**(RBAC viewer):列审计(newest-first),query 参数 `limit`(默认 50,clamp 到 `[1,500]`)、`since`(ISO-8601);无 audit_table 返回 200 `[]`。审计 best-effort 记录所有 POST/PUT/DELETE,Amazon DynamoDB 表 TTL 90 天到期即失,属可变审计流水,不做为不可篡改的合规证据;WORM 级不可篡改审计 trail 属规划(未实现,见 issue #32)。**按调用者身份隔离**:管理员与 API-key 调用方看全量;普通用户(viewer/operator)只能看到自己发起的操作(按 `actor_owner_id` 服务端过滤),未通过验签的 token 返回 403,防止越权枚举其他租户的审计轨迹。owner 过滤独立于 RBAC 总开关。

Amazon Simple Notification Service（Amazon SNS）生命周期事件经内部发布逻辑发送，无 topic 则 no-op（Amazon SNS 通知是 config-gated 默认关闭能力，参见『能力边界』）。

### 配额与执行约束

`QUOTAS_ENABLED=true`（默认 false）时才检查 `vcpu` / `mem_mb` / `data_disk_mb` 是否超单租户上限，默认不开。microVM 生命周期执行分两种：fire-and-forget（返回 CommandId，migrate 用）与同步轮询（返回布尔，launch/stop/resize 用）。DNAT 规则动态提取默认网卡避免硬编码。ALB 规则 priority 随机选空闲值、冲突重试。

---

## 实时聊天接入

> 实时聊天数据面已转型为两级路由直连，详见第 13 章。

---

## 租户授权模型与跨租户隔离

租户授权模型包括授权字段、grant/revoke 操作、hub 侧授权决策、控制面 RBAC 与 owner 检查，以及跨租户隔离机制。

### owner_id 与 authorized_users

Amazon DynamoDB 租户表的授权字段为 `owner_id`（创建者身份）和 `authorized_users`（显式授权 map）；此外注册时还会按需写入 `tenant_user_id`（归因）和 `litellm_vkey`（计费）两个字段。`owner_id` 在注册时由调用者身份取得后写入，取值可为 Cognito sub（验过 id_token 的用户）、`API_KEY_OWNER`（值为 `api-key`，无 Bearer 的密钥调用）、None（有 Bearer 但验签失败）或空字符串（遗留记录）。`authorized_users` 结构为 `{<sub>:{role, granted_at, expire_at?}, ...}`。

### grant 与 revoke

`POST /tenants/{id}/access` 提供授权写入：

- **grant**：`principal + op="grant" + role + expire_at`（可选 epoch）写入 `authorized_users[principal]={role, granted_at, expire_at}`。
- **revoke**：`principal + op="revoke"` 从 `authorized_users` 删除该 principal。
- 前置条件：仅 owner/admin 可操作；owner 身份不可被 grant/revoke 修改，尝试返回 400。

`GET /tenants/{id}/access` 返回 `{id, owner_id, authorized_users}`，同样经 owner/admin 门控。

### hub 侧授权决策

hub 侧「某用户能否访问某租户」的判定是所有授权点（`/token`、`/files`、WebSocket）统一咨询的单一真相源，决策顺序如下：

1. `owner_id === sub` → allowed，role 为 `owner`。
2. `sub ∈ authorized_users` 且未过期 → allowed，role 来自 grant。
3. `owner_id ∈ {"api-key", ""}` 且 `CLAW_HUB_SHARED_TENANT_ACCESS=true` → allowed，role 为 `shared`；默认关闭时直接 denied（go-live 默认）。
4. Amazon DynamoDB 错误 → denied（fail-closed）。
5. 其他（别人的私有租户）→ denied。

授权读取只取属主和授权名单两个字段以节省读容量，属主缺省按遗留记录处理。授权的过期判断只在 hub 侧做：没设过期时间则视为长期有效，设了则比当前时间。授权与秘密都带约 60 秒的本地缓存以减少数据库压力，代价是凭据与授权轮换最大可见性延迟约 60 秒。

### 控制面 RBAC 与 owner 检查

控制面的 owner/admin 检查：admins 和 api-key 调用者 bypass，其他需 `owner_id` 匹配，仅在 `RBAC_ENABLED=true` 时生效。`GET /tenants` 在 RBAC 时过滤，非 admin 的 Cognito 用户只看自己 `owner_id` 的租户；`GET /tenants/{id}`、`DELETE /tenants/{id}` 等 per-tenant 路由均做该检查，失败返回 403。控制面与 hub 两侧 Cognito 验签链一致，信任根唯一。

### 跨租户隔离

跨租户隔离在两个层面叠加。

聊天链路上跨租户被结构性挡住：前端 token 只带一个 tenant，channel 只注册一个 tenant，hub 路由时要求前端 WebSocket 的 tenant 和 channel 的 tenant 一致，否则路由到空 channel（agent offline）。回复单播：channel 发 `receiverId=cognitoSub` 的 reply 帧，hub 查该 sub 所有 tab 的 WebSocket 集合扇出。

网络层隔离由宿主机 iptables 强制（Firecracker 自己不过滤流量）：per-tap 三类 DROP（IMDS、跨租户东西向、管理面端口）都插到链顶优先生效。加固后跨租户 100% 丢包是加固后目标态（漏洞态为 0% 丢包，加固后带时间戳的新鲜裸金属复测待核）；IMDS DROP 表现为连接超时或无路由，不是 401（401 是 IMDSv2 无 token 的应用层响应，口径不同）。microVM 内护栏方面，越狱拦截在 LLM 与 LiteLLM 层的 Amazon Bedrock Guardrail（不在镜像内），恶意技能与凭据读取由镜像内 sentinel-guard、acl-guard 拦截。该网络与内容隔离的完整分层参见『规划部署 — 安全性』。

---

## 能力边界

接入时需要诚实标注的能力边界如下。这些能力要么是历史残留、要么是示例脚手架、要么是默认关闭的可选开关，接入方不应按生产级直接依赖。

### chatCompletions 端点

OpenClaw 原生的 `POST /vm/{tenant}/v1/chat/completions` 端点源码默认 disabled。该架构显式删除了它：启动 microVM 注入 openclaw.json 时删掉了 `gateway.http.endpoints.chatCompletions`（同一步还删了 `dangerouslyDisableDeviceAuth`、把 `allowedOrigins` 收到单一 Amazon CloudFront 源），任何租户的 microVM 都不会注入该端点。`gateway_token` 仍存在于系统中，但用途与聊天链路无关：宿主机侧从 microVM 的 openclaw.json 读出 gateway 认证 token 写入 Amazon DynamoDB 租户记录，供管理员控制台拼接可选的 `/vm/{tenant}/` control UI 链接（该 control UI 是与 C 端聊天正交的管理员旁路，不参与聊天）。控制面 REST API 自身的认证走 Amazon Cognito id_token（RS256）+ `x-api-key` + RBAC，不读 `gateway_token`。对外聊天入口则完全不经 gateway HTTP 端点。

> **Important**
>
> 接入方如需对外聊天端点，正确做法是按需做成 per-tenant 开关（只给确实需要的租户在重建时注入 `enabled:true`），不要全局打开。该端点是直通 LLM、绕过 control UI 设备认证的旁路，全局开等于给每个租户多一个对外攻击面。该 per-tenant 开关已实现：注册时传 `chat_endpoint_enabled:true`（默认 false，校验见 `deploy/lambda/api/services/tenant_service.py:824`）的租户，其 DynamoDB 记录置位（`tenant_service.py:1040`）并作为参数传给启动脚本（SSM 注入判定 `deploy/lambda/api/core/ssm_dispatch.py:48/118`），启动时才注入 `chatCompletions.enabled:true`；未置位的租户仍是显式删除该端点，默认安全。

### POST /chat/sign 孤儿路径

`POST /chat/sign`（RBAC viewer 加 owner 门控）验证 Cognito id_token 的 RS256 签名，从 claims 取服务端派生 `sub`，用该租户 `channel_secret` 给 `{sub,text}` 信封签 HMAC，返回 `{path, body, headers}`，返回头含 `x-claw-signature`、`x-claw-random`、`x-claw-timestamp`。

> **Important**
>
> 该端点没有配对的活 inbound 校验方：`claw-channel` 是 outbound-WebSocket 模型、不开入站端口，代码里无任何 `x-claw-*` 签名校验逻辑。启动配置虽配了 nginx 把 `/chat/{tenant}/` 反向代理到 microVM 内部端口，但 channel 进程在该端口无 webhook server 监听。这条「签名 → 经 nginx → microVM inbound」是历史设计残留，接入方不要当作活契约，C 端聊天投递实际全走 hub WebSocket。该孤儿路径仍有 IDOR 加 Cognito 验签门控，即便误调也只有 owner/admin 能签自己的租户，不构成新攻击面。是否还有活调用方待核。

### agentcore 与 media 占位

- `/agentcore/tools` 返回写死的静态三工具（`hello`、`system_info`、`timestamp`），是 stub，不实时查 Gateway；整个 agentcore 功能 config-gated 默认关（disabled 时返回 `{enabled:false, tools:[]}`）。接入方不应把这三个工具当真实工具清单。
- media 对象就绪检查是 stub：`/files/download-url` 的就绪判定是 HeadObject 200 即视为就绪，未接病毒或内容扫描。要扫描需自行在链路上添加。
- 量化、结算、广场发布等示例技能属样本：框架在，但端到端依赖外部条件（如 testnet key、外部结算后端、发布占位接口），非平台保证的生产能力。

### config-gated 默认关清单

下表列出 CDK 已就绪但默认关闭、需在目标环境显式开启的能力。接入方不应按「开箱即用」设计接入，接入前先确认目标环境实际是否开启。

| 开关                                                                                                                  | 影响的接入面                                                                                                                                                 | 默认                                                               | 开启方式                                                                        |
| --------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| `gsi_tenant_user` 索引                                                                                                | `GET /users/{id}/tenants`、`GET /users/{id}/summary`、`POST /users/{id}/action` 三个按外部用户管理节点舰队端点全依赖该索引；缺索引时降级，核心 CRUD 不受影响 | 默认不建                                                           | config 开启加单独部署，等 `gsi_owner` ACTIVE 后再让 `gsi_tenant_user` 转 ACTIVE |
| `chatCompletions` 端点                                                                                                | 想要对外聊天 HTTP 端点的接入方                                                                                                                               | 默认显式删除（仅注册时传 `chat_endpoint_enabled:true` 的租户注入） | per-tenant 开关已实现（`tenant_service.py:824/1040` + `core/ssm_dispatch.py:48/118`），按租户开，不要全局开 |
| `EXTERNAL_AUTHZ`                                                                                                      | 开启后 `POST /tenants/self` 自助注册返 403；`POST /external/authz` 写权威入口才启用（关时返 404）                                                            | 默认 false                                                         | env / config `external_authz` 段，需外部方提供 HMAC 共享密钥入库联调            |
| `RBAC_ENABLED`                                                                                                        | 关掉后控制面不做 per-route 角色检查与 owner 检查（全开放）                                                                                                   | 默认 true（默认强制，是本清单中默认开的例外）                      | 仅 demo/dev 想全开放才显式设 false                                              |
| `CLAW_HUB_SHARED_TENANT_ACCESS`                                                                                       | 开启后共享或遗留节点（owner=api-key 或空）放行 shared 角色；关时 fail-closed 直接拒                                                                          | 默认关                                                             | env 显式 true                                                                   |
| `SELF_MAX_NODES_PER_USER`                                                                                             | `POST /tenants/self` 每用户节点上限，达上限返 409                                                                                                            | 默认 1（0 表示不限）                                               | env                                                                             |
| AZ failover / 主机监控 / GuardDuty / Amazon SNS 通知 / 指标看板 / WAF / DNS Firewall / balloon 超卖 / Amazon SQS 削峰 | 监控告警外发、生命周期削峰、内存超卖等运维侧能力；Amazon SNS 通知关时事件 no-op，Amazon SQS 关时生命周期动作走同步而非 202 queued                            | 全部默认关                                                         | 各自 config 段 / env                                                            |

### claw-hub 部署形态

接入方连接的是 hub WebSocket（经 Amazon CloudFront `/hub/*` → ALB → hub）。当前生产实跑单进程 metal hub（systemd），跨 Pod Redis 路由（cluster-routing）代码完整且 degrade-safe，但 Amazon Elastic Kubernetes Service（Amazon EKS）多副本灰度尚未切到生产（目标态/灰度中）。这是有意的渐进切换，接入契约（`/hub/token`、`/hub/ws`、帧结构）在单进程与多副本下一致，接入方代码无需区分。默认端口 `CLAW_HUB_PORT=8790`。

> **Note**
>
> 能直接当生产契约接入的是认证模型、控制面 REST API 与授权模型（认证模型、控制面 REST API、租户授权模型与跨租户隔离三节）；本节列出的能力要么不接（孤儿路径），要么当样本起点（脚手架），要么先确认开关再依赖（config-gated）。
