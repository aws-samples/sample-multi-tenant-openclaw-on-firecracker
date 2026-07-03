# 外部平台集成指南(客户视角)

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

> **Note** 租户归属不靠 body 里塞 id:`owner_id` 由控制面从调用者身份自动落定,`tenant_user_id`(你平台的用户稳定 id)来自 JWT 的 `custom:tenant_user_id` claim——由 ClawPool 侧 Pre-Token-Generation 在联邦登录时注入。也就是说,你的后端**用哪个用户的 id_token 调创建接口,新租户就归属那个用户**,而不是在 body 里指定。`platform_id` 只用于登录前的 IdP 路由查询(`GET /tenantmatch`),不是创建租户的入参。

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
         ③ 持 x-api-key + 转发用户的 Bearer id_token 调 POST {CTRL_API}/tenants
            body: {name, client_token:<幂等键>}
            (租户自动归属 id_token 的 custom:tenant_user_id,无需在 body 里指定)
         ④ 轮询 GET {CTRL_API}/tenants/{id} 到 status:running
         ⑤ 回前端 {tenant_id, status}
```

`client_token` 用 `<platform_id>:<用户id>` 保证同用户重复开通幂等(不双开)。

> **Important** 要让「按用户管理 fleet」(Step 5)生效,创建租户时必须让控制面拿到用户的联邦身份——**转发用户的 `id_token`(联邦签发,含 `custom:tenant_user_id`)**给 `POST /tenants`,控制面据此把租户 `tenant_user_id` 落成该用户的稳定 id。若只用 `x-api-key` 而不带用户 Bearer,租户不会关联到具体用户,`GET /users/{tenant_user_id}/tenants` 就查不到它。参考实现 `console/marketplace-demo/broker/handler.py` 展示了整体流程骨架,以本节的转发 id_token 口径为准。

**Step 4 · 进入 AI 助手**
用户点「进入 AI 助手」→ 跳你域名下的 chat UI → chat UI 用联邦 `id_token` 调 `POST {HUB}/hub/token`(带 `tenant_id`)换实时令牌 → 建 `wss {HUB}/hub/ws?token=` → 对话。详见《控制面 API 对接文档》§4-5。

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
