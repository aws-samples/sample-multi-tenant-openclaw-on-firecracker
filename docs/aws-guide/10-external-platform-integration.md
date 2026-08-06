# 外部平台集成指南

本章面向把 ClawPool 集成进自有 SaaS、交易平台或企业门户的客户。当前推荐路径是：

- 用户身份、登录和会话由客户平台自己管理。
- 客户后端调用 ClawPool 控制面创建和管理 tenant。
- 浏览器只连接客户平台的 `/gw/ws`。
- 客户后端代持每租户 gateway token 和 Ed25519 device 私钥，连接 microVM。

早期“把客户 IdP 联邦进 ClawPool Cognito，再换 hub token”的方案已废弃。

## 1. 责任边界

| 责任 | 客户平台 | ClawPool |
| --- | --- | --- |
| 终端用户注册、登录、会话 | 负责 | 不接触用户密码 |
| 购买、配额、退款、停用 | 负责 | 接收生命周期调用 |
| 用户到 tenant 的归属 | 生成并持久化映射 | 存 `owner_id`、`tenant_user_id`、`platform_id` |
| 浏览器实时入口 | `/gw/ws` | 不提供 hub token 兑换 |
| 第二跳认证 | 代持并解密租户凭据 | gateway token + Ed25519 device 验证 |
| microVM、调度、镜像、备份 | 不负责 | 负责 |

## 2. 控制面身份

所有控制面调用都带 `x-api-key`，但 API Key 仅用于 API Gateway usage plan 客户
标识，不是独立认证。写调用还必须满足以下一种部署合同：

1. 带有效的 operator/admin Cognito Bearer token。
2. 在受信私网中显式把 `console_auth.default_no_jwt_role` 配为
   `operator` 或 `admin`，并用 IAM SigV4、private API resource policy 或 Lambda
   authorizer 约束真实身份。

生产环境优先使用 platform-scoped API key/authorizer，把调用方限制到自己的
`platform_id`。浏览器绝不能持有 `x-api-key` 或控制面 Bearer token。

## 3. 用户归属

客户平台为每个用户维护：

- `owner_id`：UUID，控制面 owner 门使用。
- `tenant_user_id`：客户平台稳定用户 id，用于用户维度查询；建议加平台前缀。
- `platform_id`：平台命名空间。

`tenant_user_id` 是查询/归因字段，不是独立授权凭据。平台后端必须从已验证的会话
推导这些值，不接受浏览器自报 owner 或 platform。

## 4. 开通流程

```text
Browser -> customer backend: activate assistant
customer backend:
  1. verify platform session
  2. run purchase/quota checks
  3. POST /tenants with control-plane credentials
  4. poll GET /tenants/{id}
  5. return readiness only
```

请求示例：

```json
{
  "name": "assistant-user-123",
  "client_token": "market:user-123",
  "owner_id": "11111111-2222-3333-4444-555555555555",
  "tenant_user_id": "market:user-123",
  "platform_id": "market"
}
```

带 `client_token` 时返回 ID 形态为 `t-<16hex>`。`creating`、`pending` 或 `queued`
都不是完成；继续轮询到 `status=running`，需要应用就绪时再检查
`app_health=up`。

## 5. 聊天流程

```text
Browser
  -> wss://<customer-platform>/gw/ws?token=<platform-session-token>
  -> customer backend
  -> /ws/{tenant_id}
  -> CloudFront -> ALB -> OpenResty -> host DNAT
  -> microVM gateway :18789
```

客户后端：

1. 验证平台会话并从服务端映射选择 tenant。
2. 调 `GET /tenants/{id}` 或 `/credentials` 获取 gateway/device 密文。
3. 在进程内用正确 KMS encryption context 解密或解开 recipient 信封。
4. 完成 `connect.challenge` 的 Ed25519 签名和 gateway token 认证。
5. 映射 `chat.send` 与 `reply_delta` / `reply` / `reply_error`。

浏览器从不接触 ClawPool 凭据。旧 `/hub/token`、`/hub/ws`、`/channel-token` 与
hub 文件接口都不存在。

## 6. 用户维度管理

- `GET /users/{tenant_user_id}/tenants`：列该用户的 tenant。
- `GET /users/{tenant_user_id}/summary`：按状态汇总。
- `POST /users/{tenant_user_id}/action`：批量 start/stop。
- `POST /external/authz`：启用外部授权模式后，HMAC 签名写 grant/revoke。

这些接口仍受 owner、platform scope 与 RBAC 约束。

## 7. 安全要求

- API Key、控制面 Bearer、gateway token、device 私钥都只存在后端。
- 平台后端不得把 `GET /tenants/{id}` 或 `/credentials` 原样返回浏览器。
- KMS 解密必须带与加密时完全一致的 `tenant_id` 或 `owner_id` context。
- `client_token` 必须稳定，防止重试双开。
- 文件功能需要平台后端自行实现 tenant 授权、MIME/size 限制与 Amazon S3 预签。
- 多 region 调用要显式配置超时、重试和幂等；不能把网络超时当失败后盲目重建。

## 8. 参考实现

`engineering/backend/` 展示平台 JWT、`/gw/ws`、控制面客户端和 device 握手。
`console/marketplace-demo/` 是历史开发测试床，其中仍可能包含已废弃的 Cognito/hub
用例，不作为当前契约。上线前以第 9 章、OpenAPI、第 13 章和目标环境回归为准。
