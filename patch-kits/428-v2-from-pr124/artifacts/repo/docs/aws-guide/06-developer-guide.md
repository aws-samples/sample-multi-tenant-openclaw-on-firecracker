# 开发人员指南

本章面向控制面 API 和实时聊天接入开发者。字段级 REST 契约以
[`openapi.yaml`](openapi.yaml) 为准；数据面以
[`13-data-plane-redesign.md`](13-data-plane-redesign.md) 和
`engineering/backend/lib/gw-ws.mjs` 为准。

## 认证与授权

### 控制面

控制面方法当前都要求 Amazon API Gateway `x-api-key`。该值用于 usage plan
客户识别、配额和限流，不应被当作独立认证或授权机制。AWS 官方文档明确说明 API
Key 不适合承担认证/授权，usage-plan 限额也是 best effort。

启用 Amazon Cognito 后，调用方可同时发送
`Authorization: Bearer <id_token>`。Lambda 通过 JSON Web Key Set (JWKS)
验证 RS256、issuer、过期时间和配置的 client id，再从 `sub` 与
`cognito:groups` 得到调用者身份和最高角色。

角色顺序为 `viewer < operator < admin`：

- `viewer` 可调用只读路由和 `POST /tenants/self`。
- `operator` 可调用普通写路由。
- `admin` 可执行全舰队或其他显式 admin 操作。

`console_auth.default_no_jwt_role` 决定不带 Bearer 的 API-key-only 请求角色。
仓库默认是 `viewer`，因此默认只能读。受信内网自动化若要只带 `x-api-key` 写入，
部署方必须显式改为 `operator` 或 `admin`，并叠加私网、IAM 或 Lambda
authorizer 等真正的安全边界。

资源授权独立于角色门：

- 普通 Cognito 用户只能访问 `owner_id == sub` 的租户。
- admin 和未被 platform scope 收窄的受信 API-key 路径可跨 owner 操作。
- platform-scoped API key 只能访问相同 `platform_id` 的租户。
- 无效 Bearer token 没有 owner 身份，返回 `403`，不会退化为受信 key 调用。

### 数据面

浏览器只携带客户平台自己的会话 token 连接
`wss://<platform>/gw/ws?token=<platform-session-token>`。平台后端验证用户、从
服务端账本选择 tenant，再用两类租户凭据连接 OpenClaw gateway：

| 凭据 | 用途 | 存储与解密边界 |
| --- | --- | --- |
| gateway token | OpenClaw gateway bearer 认证 | KMS 密文存 `openclaw-tenant-secrets`，平台后端按 `tenant_id` encryption context 解密 |
| Ed25519 device 私钥 | 对 `connect.challenge` 签名 | KMS 密文按 `owner_id` encryption context 解密；公钥冷注入 `paired.json` |
| 平台会话 token | 浏览器到平台 `/gw/ws` | 客户平台签发和验证，不发送给 ClawPool 控制面 |

浏览器不接触 `x-api-key`、gateway token 或 device 私钥。早期的
`claw-hub`、`claw-channel`、`/hub/token`、`/hub/ws`、`/channel-token`、
`/chat/sign` 和 hub 文件预签接口均已下线，不是兼容入口。

## 控制面 REST API

### 通用约定

- 写请求使用 `Content-Type: application/json`。
- 结构化错误通常为 `{"error":"...","code":"..."}`；客户端按 `code` 分支。
- `client_token` 是 4–128 位可打印 ASCII 幂等键。同 owner 重放不会重复创建。
- 无 `limit` 的旧列表可返回裸数组；带 `limit` 或 `next_token` 时返回分页信封。
- 重操作通常返回 `202`，调用方必须轮询资源或 job，而不是把接受当完成。
- 响应必须经过服务端脱敏；接入方仍不得把控制面响应原样透传给不可信前端。

### 创建与查询租户

`POST /tenants` 需要 operator+。`name` 是唯一通用必填字段；完整的 CPU、内存、
磁盘、skills、schedule、归属、购买语义、恢复和安全字段见 OpenAPI。

ID 形态取决于幂等参数：

- 带 `client_token`：`t-<16hex>`，由 owner 与 token 稳定派生。
- 不带 `client_token`：`<name>-<4hex>`。

返回 `creating`、`pending` 或 `queued` 只表示进入流程。完成判据是
`GET /tenants/{id}` 到达 `status=running`，且需要应用可用时还要检查
`app_health=up`。

`GET /tenants` 支持 owner/platform scope、标签和游标分页，并剥离服务端凭据。
`GET /tenants/{id}` 先脱敏基础记录；当租户为 `running` 时，会受 owner/admin 门
保护后附加 gateway token 的 KMS 密文和 device id、公钥、私钥 KMS 密文、scopes，
供平台后端本地解密。`GET /tenants/{id}/credentials` 可将同一凭据重包为
recipient-key `asymmetric-v1` 信封。两类响应都不得下发浏览器。

### 生命周期与删除

`POST /tenants/{id}/{action}` 支持 OpenAPI 列出的 start、stop、restart、
pause、resume、reset、rebuild、backup、resize、resize-disk、migrate、
access 和 provision。异步动作返回后继续轮询目标状态。

`DELETE /tenants/{id}` 是幂等删除。`keep_data=false` 会进入数据清理路径；
`skip_backup=true` 会跳过删前备份，接入方必须把它当显式数据风险选择。

### 镜像生命周期

镜像接口支持 live/canary 双槽和 CAS 提升：

- `POST /create-image-snapshot` 要求合法非空 `label`。控制面不会为 `""`
  自动派生 label。
- `POST /hosts/{id}/pull-image?slot=canary` 拉取候选版本。
- `GET /hosts/{id}/pull-image-progress` 和 `GET /hosts/{id}/image-slots`
  查询 job 与宿主真实槽位。
- `POST /hosts/{id}/promote-canary` 只提升调用方已确认的候选。
- `POST /hosts/{id}/reclaim-images` 回收无人引用的版本。

完整冲突码、幂等键和 rollback 方式见
[`../api/pull-image-api.md`](../api/pull-image-api.md)。

### 外部平台与授权

`POST /external/authz` 在 `EXTERNAL_AUTHZ=true` 时接受 HMAC 签名的
grant/revoke，将外部平台授权写入 `authorized_users`。签名覆盖
`timestamp.raw_body`，并受时间窗口限制。

`GET /tenantmatch` 在 handler、表和 IAM 层已有实现，但 API Gateway 资源未接线，
当前是 documented-but-unreachable，不得作为上线依赖。

## 实时聊天接入

### 请求链

```text
Browser
  -> wss /gw/ws (platform session token)
  -> platform backend (tenant authorization)
  -> /ws/{tenant_id} (gateway token + Ed25519 device handshake)
  -> CloudFront -> ALB -> OpenResty edge -> host DNAT
  -> microVM OpenClaw gateway :18789
```

平台网关的最小职责：

1. 验证平台会话 token，绝不相信浏览器自报 tenant。
2. 从服务端 owner/platform 授权事实选择一个 running tenant。
3. 取 KMS 密文并在进程内按正确 encryption context 解密。
4. 响应 `connect.challenge`，发送带 gateway token 的 Ed25519 签名连接帧。
5. 将前端 `{text, clientMessageId, threadId}` 映射为 OpenClaw `chat.send`。
6. 将上游事件映射为 `reply_delta`、`reply` 或 `reply_error`。

参考实现先发送 `gw_status:connecting`。租户未就绪时返回
`gw_status:provisioning` 并关闭 4409；无租户关闭 4404；凭据或上游握手失败关闭
4502；上游断开关闭 4503。握手成功后发送 `gw_status:ready` 和兼容 `ready` 帧。

`threadId` 仅接受 `[A-Za-z0-9._:-]{1,80}`。`clientMessageId` 用作上游
idempotency key 和回复关联键。浏览器输入缺少非空 `text` 时返回 `gw_error`。

### 版本边界

当前 `bb` 的黄金镜像仍钉在 OpenClaw `2026.2.26`。平台网关协议版本必须与目标
镜像匹配；不要仅依赖参考后端的默认值。升级 OpenClaw 时，需要一起验证
`build-rootfs.sh` pin、template schema gate、`paired.json` 形状和
`GW_PROTOCOL_VERSION`，并用远程拓扑完成真实 device 握手。

## 能力边界

- 两级数据面由 `edge.enabled` 与 `redis.enabled` 控制。仓库默认 `edge.enabled=false`、`redis.enabled=true`；因此默认会创建路由存储，但不会创建 edge 数据面。
- `/chat/sign` 的 API Gateway 资源仍存在，但 Lambda 路由已移除；调用不是有效契约。
- 已删除 hub 的媒体预签接口没有替代兼容层。文件功能需由客户平台后端重新实现
  tenant 授权、MIME/size 限制和 Amazon S3 预签。
- `chat_endpoint_enabled` 是 per-tenant 直通 HTTP 开关，默认关闭；它不是 `/gw/ws`
  的替代认证路径。
- `agentcore`、WAF、GuardDuty、Wazuh、managed metrics、外部授权和多项运维能力
  都受配置开关约束。接入前读取 `/system/info` 并核对真实部署。

## 验证

静态契约使用 `docs/aws-guide/openapi.yaml`。控制面客户路径回归入口是
`tests/api-regress/oc-regress.sh`；它会创建、轮询、启停、重建和删除测试租户，
只能在授权的测试环境运行。数据面参考测试位于
`engineering/backend/test/gw-ws-device.test.mjs` 和
`engineering/backend/test/e2e-isolation.mjs`。
