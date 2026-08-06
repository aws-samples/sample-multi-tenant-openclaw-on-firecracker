# marketplace-demo — 二手电商平台联邦参考实现(交付,随 chatui 一起)

> **定位**:一个极简的「二手电商买卖平台」参考实现,演示 [[ADR-dataplane-external-saas-auth]] 档 A「外部 IdP 联邦」——客户平台的自由用户 → 联邦进本平台 Cognito → 交易平台后端代开独立 openclaw → 进 AI pro 对话。**给客户平台开发者照着集成用**。
>
> **交付,随 chatui 一起**:放在 `console/marketplace-demo/`,和 `console/chat`(AI pro 前端)同属交付区。生产中二手电商是客户自己的平台,AI pro chat UI 嵌在客户电商 domain 下——本 demo 就是这套集成的最小可跑参考(SPA + 后端代开 broker + 联邦配置脚本),客户拿它对接自己的平台。日本 region 是示例部署点。

## 为什么要这个 demo

档 A 的核心是「外部 SaaS 平台的 IdP 联邦进本平台单 Cognito Pool」。光看代码/画图不够,必须真起一个「外部平台 + 外部 IdP」把联邦、Pre-Token-Gen 注入 claim、交易平台后端代开租户、AI pro 无缝进入全链路跑通,才算档 A 成立(铁律 #8 真机验证)。二手电商是这个「外部平台」的最小化身。

## 角色映射(对照 ADR 时序图)

| demo 组件                               | 对应生产角色            | 本 demo 实现                                                                                        |
| --------------------------------------- | ----------------------- | --------------------------------------------------------------------------------------------------- |
| 二手电商 SPA(`marketplace.html`)        | 客户自有交易平台前端    | 极简单页:登录 + 商品列表 + "开通 AI Pro 助手"按钮                                                   |
| 二手电商 IdP                            | 客户自有 OIDC IdP       | **本 demo 用一个独立的 Cognito User Pool 当电商 IdP**(entry pool),联邦进本平台 ClawPool Cognito     |
| 交易平台后端(`broker/`)                 | 客户后端                | 极简 Lambda/脚本:收到"购买"→ 持 x-api-key 调本平台 `POST /tenants`(代开,非用户自助)→ 轮询到 running |
| AI pro 前端                             | 嵌客户电商域的 chat UI  | 复用现有 `console/chat/index.html`(已 Hosted UI + PKCE + 联邦就绪)                                  |
| 本平台 Cognito / 控制面 / hub / microVM | ClawPool 平台(被集成方) | 现有  部署                                                                                       |

## 端到端流程(照 ADR 时序图 v2,禁自助注册)

1. 用户访问二手电商 SPA → 点登录 → 走 authorization_code + PKCE。
2. 电商 IdP(entry Cognito pool)认证用户(demo 用账号密码;生产是电商自有账号体系)。
3. 用户在电商内点"开通 AI Pro" → 电商前端带用户 id_token 调**电商后端**。
4. **电商后端(broker)** 校验用户 id_token 后调本平台 `POST /tenants` → 本平台建独立 microVM。**这一步是后端代开,用户全程不直接碰本平台控制面。**

   > **要让「按用户查 fleet」(`GET /users/{tenant_user_id}/tenants`)生效,租户必须落上 `tenant_user_id`。** 控制面的 `tenant_user_id` 只从请求 Bearer 的 `custom:tenant_user_id` claim 读取(Pre-Token-Gen 注入),**不读 body**。因此后端代开应**转发用户的 id_token 作为 `Authorization: Bearer`**(可与 `x-api-key` 并存),而不是把 `tenant_user_id` 塞进 body。本目录 `broker/handler.py` 现在只带 `x-api-key`,建出的租户 `tenant_user_id` 会是空——集成到生产前需按上面口径改为转发 Bearer(见控制面 API 对接文档「租户归属」段)。

5. 电商后端轮询 `GET /tenants/{id}` 到 `running`。
6. 用户点"进入 AI Pro" → 跳到 AI pro 前端(生产嵌电商域)→ 该前端用**本平台 Cognito 联邦签发的 id_token** 换 hub token → 建 wss → 对话。

## 双 Cognito Pool(对照 #101)

- **entry pool(电商入口)**:demo 里模拟二手电商自己的用户体系 + 联邦源。终端用户登录用。
- **ClawPool(内部)**:本平台的 channel machine-user 身份 pool,纯内部,demo 不碰。

demo 先用现有「单 Pool 两 client」跑通联邦验证即可(能完整模拟);双 Pool 是 #101 的生产加固,demo 验通后再拆。

## 目录

- `marketplace.html` — 二手电商极简 SPA(登录 + 商品 + 开通 AI Pro)。
- `broker/handler.py` — 电商后端代开租户的最小实现(调本平台 `POST /tenants`)。
- `setup-federation.sh` — 配置脚本:在本平台 Cognito 注册电商 IdP 为 upstream OIDC provider + 建 entry pool(日本 region)。
- `TESTPLAN.md` — 端到端联邦测试矩阵(联邦登录成功/失败、伪造 JWT 拒、跨平台越权拒、代开租户、AI pro 无缝进入)。

## 部署坐标

固定日本 region(`ap-northeast-1`,独立 CDK 栈,见下节)。本平台被集成方部署在另一 region——demo 跨 region 联邦正好也验证了「客户平台与 openclaw pool 不在同 region」的真实形态。凭据写本地配置,不入库。

> 状态:**联邦链路已真机验证通过**(:entry pool 当二手电商 IdP + 联邦进 ClawPool Cognito + Pre-Token-Gen 注入 claim + broker 代开 + 完整浏览器登录,id_token 带 `custom:tenant_user_id/platform_id`;证据见档A #97 evidence)。本目录 = 可交付的客户参考实现 + 可 `cdk deploy` 到日本的独立栈(见下节)。手工验证时建的  资源已固化进 `cdk/stack.py`。

## 一键 CDK 部署(独立栈,日本 region)

本 demo 可作为**独立 CDK 栈单独 `cdk deploy` 到日本 region**(`cdk/stack.py`),起齐客户平台侧全套:entry Cognito pool(模拟二手电商 IdP)+ Hosted UI 域 + PKCE app client + broker Lambda(代开租户)+ S3/CloudFront 托管 `marketplace.html`。

```bash
cd console/marketplace-demo/cdk
pip install -r requirements.txt
cdk deploy --context region=ap-northeast-1 \
  --context ctrl_api_base=<ClawPool 控制面 API base> \
  --context ctrl_api_key_secret=<Secrets Manager ARN(存 x-api-key)> \
  --context clawpool_idpresponse=<ClawPool Cognito 的 /oauth2/idpresponse>
```

输出:EntryPoolId / EntryIssuer / SpaClientId / FederationClientId / SiteUrl / BrokerUrl。

**部署后一步(在本平台 ClawPool 侧做)**:把 `EntryIssuer` + `FederationClientId` 注册为 ClawPool Cognito 的 upstream OIDC provider(provider-name = `demo-marketplace`),挂 Pre-Token-Gen trigger(见档A #97)。之后二手电商用户经此 entry pool 联邦进 ClawPool,id_token 带 `custom:tenant_user_id/platform_id`,broker 代开租户,进 AI pro chat。此联邦链路已在  真机跑通(证据 #97 evidence)。

凭据(x-api-key / client secret)走 Secrets Manager + context,不硬编码(CLAUDE.md 铁律)。
