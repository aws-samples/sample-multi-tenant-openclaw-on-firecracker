# 外部平台集成指南(客户视角)

> **2026-07-08 数据面转型说明**:本章描述的实时聊天接入序列(`{HUB}/hub/token` 换令牌、`wss {HUB}/hub/ws` 建连的 hub 出站拨号模型)已被 [第 13 章 · 数据面两级路由](13-data-plane-redesign.md) 代替(客户 OIDC 不支持无浏览器登录,故弃用 Cognito 与出站拨号中枢,改用 OpenClaw 原生 gateway 认证)。当前实时聊天接入以第 13 章为准;控制面 API 集成部分仍然有效。

本节面向**要把 ClawPool 集成进自有平台的客户**(如交易平台、二手电商、任意 SaaS)。读完你能让平台的每个自由用户拥有一个专属的 AI 助手(独立 openclaw),全程用你自己的账号体系,用户不接触任何底层控制台。

> 定位区分:上一节《控制面 API 对接文档》是**逐接口参考**;本节是**集成方案与步骤**——你的平台该怎么接、认证怎么串、用户怎么用。术语「本平台/ClawPool」指被集成方(提供 openclaw pool),「你的平台」指集成方(客户自有平台)。

## 1. 集成后长什么样

- 你的用户在**你自己的平台**用**你自己的账号**登录(不新注册 ClawPool 账号)。
- 用户在你平台内「开通 AI 助手」→ **你的后端**为该用户开一个专属 openclaw microVM。
- 用户进入 AI 助手界面(嵌在你的域名下)→ 实时对话。
- 每个用户的 openclaw 相互隔离,一个用户看不到另一个用户的会话/数据。

## 2. 三个集成决策(先定这三件)

**① 身份怎么接(联邦优先)**
把你平台的 IdP(OIDC 或 SAML)注册为 ClawPool 单个 Cognito User Pool 的 upstream IdP。你的用户登录时经你的 IdP 认证,Cognito 联邦签发 ClawPool 信任的 JWT。**你不需要把用户导入 ClawPool,也不需要 ClawPool 账号体系**——信任根仍是 ClawPool 的 Cognito,但用户身份来自你。若你平台没有独立 IdP,也可直接用 ClawPool 为你分配的 Cognito app client 做账号密码登录(最简)。

**② 租户谁来开(你的后端代开,不是用户自助)**
用户点「开通 AI 助手」→ 你的前端带用户的 JWT 调**你的后端** → 你的后端持 ClawPool 发给你的 `x-api-key` 调 `POST /tenants`。这样你能在开通前插入自己的**购买/计费/配额**逻辑,用户不直接建节点。ClawPool 提供了自助注册端点 `POST /tenants/self`,但**推荐走后端代开**以保留你的商业门控。

> **Note** 租户归属有两条路,按你的调用方式二选一,**不可混用**。**路 1 · 纯 api-key 代开(推荐,最简)**:请求只带 `x-api-key`(不带用户 Bearer),body 里直接传 `owner_id`(用户在 ClawPool Cognito 的 sub,UUID)与 `tenant_user_id`(你平台的用户稳定 id),控制面校验后落库。**路 2 · 转发用户 `id_token`**:请求带 `x-api-key` + 用户的 Bearer `id_token`(联邦签发),归属自动取自 token(sub → `owner_id`、`custom:tenant_user_id` claim → `tenant_user_id`);**此路下 body 不得再传这两个字段,带了返回 403**(owner 只能来自验证过的 token,防止把节点开到他人名下)。`platform_id` 两条路都可在 body 传(可选,正则 `^[a-zA-Z0-9._-]{1,128}$`,外部平台代开时标记归属平台;见 `tenant_service.py:252`/`core/utils.py:110`)。
>
> **`tenant_user_id` 是数据归因标签,不是授权凭据**:它不参与聊天/生命周期的授权判定(授权走 `owner_id`/`authorized_users`),但你给某租户落了 `tenant_user_id`,持相同 `custom:tenant_user_id` claim 的联邦登录用户就能经 `GET /users/{tenant_user_id}/tenants` 列出该租户的元数据(不含凭据)——落这个字段即表示允许该用户看到这份列表。请用**全局唯一**的用户 id(建议带你的平台前缀,如 `yourplatform:12345`),不同平台用相同裸数字 id 会互相看到对方同名用户的节点列表。

**③ AI 助手界面放哪(嵌你的域)**
AI 助手前端(chat UI)嵌在你的域名下,用户从你的登录态无缝进入(不跳转到陌生域名)。前端用联邦签发的 JWT 换取实时对话令牌。ClawPool 提供参考实现(Hosted UI + authorization_code + PKCE + refresh,已联邦就绪)。

## 3. 集成步骤

**Step 1 · 注册你的平台**
联系 ClawPool 运营,提供:你的 IdP 元数据(OIDC issuer + JWKS URL,或 SAML metadata)、你的 `platform_id`、回调 URL。ClawPool 侧:把你的 IdP 注册为 Cognito upstream provider + 配 Pre-Token-Generation 注入 `custom:tenant_user_id`/`custom:platform_id` + 发给你一个 `x-api-key`(后端代开租户用,server-side 保管,勿入前端)。

**Step 2 · 前端接联邦登录**
你的前端发起 `authorization_code + PKCE` 登录到 Cognito Hosted UI,带 `identity_provider=<你的platform_id>` 直接跳你的 IdP(跳过选择器)。用户登录后拿到含 `custom:tenant_user_id`/`custom:platform_id` 的 `id_token`。

**Step 3 · 后端代开租户**

```
用户点开通 → 你的前端 POST 你的后端(Bearer <用户 id_token>)
你的后端:① 校验 id_token(联邦签发,JWKS 验签) ② 跑你的购买/配额门
         ③ 持 x-api-key 调 POST {CTRL_API}/tenants(不带用户 Bearer)
            body: {name, client_token:<幂等键>,
                   owner_id:<用户的 Cognito sub>, tenant_user_id:<你平台的用户id>,
                   platform_id:<你的平台标识>}
         ④ 轮询 GET {CTRL_API}/tenants/{id} 到 status:running
         ⑤ 回前端 {tenant_id, status}
```

`client_token` 用 `<platform_id>:<用户id>` 保证同用户重复开通幂等(不双开)。

> **Important** 要让「按用户管理 fleet」(Step 5)与「用户在 chat UI 看到自己的节点」生效,创建时必须让归属字段落库:纯 api-key 代开就**在 body 里显式传 `owner_id` + `tenant_user_id`**(上例);转发用户 id_token 则归属自动取自 token。两者都不给,租户会落在系统名下——用户 `GET /tenants` 列不出它、`GET /users/{tenant_user_id}/tenants` 也查不到。参考实现 `console/marketplace-demo/broker/handler.py` 展示了整体流程骨架,以本节口径为准。

**Step 4 · 进入 AI 助手**
用户点「进入 AI 助手」→ 跳你域名下的 chat UI → chat UI 经你的平台后端拿到该租户的 `gateway_token`(平台后端用 KMS 解 revealToken 返回的密文)→ 经两级边缘路由到 microVM 原生 gateway 发起实时对话。当前接入方式(浏览器 → 平台后端 → CloudFront → ALB → OpenResty 边缘 → 宿主 iptables DNAT → microVM 原生 gateway)详见 [第 13 章 · 数据面两级路由](13-data-plane-redesign.md)。

**Step 5 · 按用户管理**

- 查某用户所有 AI 助手:`GET {CTRL_API}/users/{tenant_user_id}/tenants`
- 该用户节点汇总:`GET {CTRL_API}/users/{tenant_user_id}/summary`
- 批量启停(如用户退订):`POST {CTRL_API}/users/{tenant_user_id}/action {action:stop}`

## 4. 隔离与安全保证(给你的用户的承诺)

- **跨用户隔离**:每个用户的 openclaw 是独立 microVM(独立内核),用户 A 无法访问用户 B 的会话/数据/节点。实时通道三段闸:令牌绑单一租户 + 服务端验证的用户身份撮合 + 授权查服务端账本(绝不信客户端自报)。
- **你的用户凭据不出你平台**:联邦模式下,用户密码只在你的 IdP;ClawPool 只收到签发的 JWT,不碰你的用户密码。
- **信任边界**:ClawPool 的 channel 机器身份走独立的内部 Cognito Pool(与你的用户入口 Pool 隔离),你的用户联邦进来不会与内部机器身份混淆。
- **凭据保管**:后端代开用的 `x-api-key` 是 server-side 凭据,绝不放前端;定期轮换。

## 5. 多 region

你的平台与 ClawPool pool 可以不在同一 region(联邦跨 region 工作)。测试床示例把电商前端放日本 region、openclaw pool 放新加坡,验证了跨 region 集成。

## 6. 参考实现

`console/marketplace-demo/`(开发测试床,不随产品交付)给了一套最小可跑的集成参考:二手电商 SPA(`marketplace.html`)+ 后端代开(`broker/handler.py`)+ 联邦配置脚本(`setup-federation.sh`)+ 测试矩阵(`TESTPLAN.md`)。你可照它的结构接自己的平台。

> 状态:本指南描述的联邦对接是 ClawPool 档 A 方案(`ADR-dataplane-external-saas-auth`)。控制面 API(创建/查询/生命周期/按用户管理)已上线可用;外部 IdP 联邦 + Pre-Token-Generation 注入 custom claim 属档 A 落地项,以 ClawPool 运营确认的可用范围为准。
