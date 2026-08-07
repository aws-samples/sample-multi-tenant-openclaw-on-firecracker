# demo-marketplace 端到端联邦测试矩阵(ADR 档 A · 铁律 #8 五步链条 step 2)

> 这份是「将要验证什么」写在跑之前(step 2)。测试床是 [[README]] 描述的二手电商联邦环境。
> 每条声明:命题 / 输入 / 预期 / 判定标准。真跑后回填 ✅通过 / ❌失败 / ⚠️未测,证据落 evidence。

## 命题分组

### A. 联邦登录(正向 + 反向)

| #   | 命题                        | 输入                                               | 预期                                                      | 判定                                | 状态                               |
| --- | --------------------------- | -------------------------------------------------- | --------------------------------------------------------- | ----------------------------------- | ---------------------------------- |
| A1  | 用户能经电商 entry IdP 登录 | marketplace.html 点登录 → entry pool 账号密码      | 拿到本平台 id_token(含 custom:tenant_user_id/platform_id) | 解码 id_token 看到两个 custom claim | ⬜待真机                           |
| A2  | 未登录不能开通              | 未登录点「开通 AI Pro」                            | 按钮 disabled,不发请求                                    | UI 禁用                             | ✅骨架已实现(前端 disabled)        |
| A3  | 伪造 id_token 被 broker 拒  | 构造 exp 过期 / 改 iss / 无 sub 的 token 调 broker | 400 auth 错误,不代开                                      | broker 返 `{error:auth:*}`          | ✅骨架自测通过(过期/坏/无sub 全拒) |
| A4  | 跨 issuer token 拒          | 拿别的 pool 签的 token 调 broker                   | 400 issuer mismatch                                       | broker 校验 iss                     | ✅骨架自测(iss 校验分支在)         |

### B. 租户代开(交易平台后端,非自助)

| #   | 命题                           | 输入                                             | 预期                                              | 判定                            | 状态                                                                   |
| --- | ------------------------------ | ------------------------------------------------ | ------------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------- |
| B1  | broker 持 x-api-key 代开租户   | 合法 id_token 调 broker                          | POST /tenants 202,拿到 tenant_id                  | 控制面真建 microVM              | ⬜待真机(需  x-api-key)                                             |
| B2  | 按 tenant_user_id 归属         | 代开时**转发用户 id_token 作 Bearer**(非塞 body) | 租户 record 的 tenant_user_id = 电商 sub          | GET /users/{sub}/tenants 能查到 | ⬜待真机 · **前提:broker 需先改为转发 Bearer**;现只带 x-api-key 会落空 |
| B3  | 同用户重复开通幂等             | 同一用户点两次「开通」                           | client_token 命中,不双开(复用/409)                | 只有一个 running 租户           | ⬜待真机(骨架已带 client_token 幂等)                                   |
| B4  | 用户不能绕过 broker 自助建租户 | 用户直接 POST /tenants/self                      | 电商流程不暴露该入口;即便调也受 per-user 上限门控 | 流程文档禁用 self;门控在控制面  | ✅设计约束(ADR 明确禁自助)                                             |
| B5  | broker 轮询到 running          | 代开后轮询 GET /tenants/{id}                     | 最终 status=running                               | broker 返 running               | ⬜待真机                                                               |

### C. AI pro 无缝进入 + 数据面对话

| #   | 命题                  | 输入                                    | 预期                                        | 判定                         | 状态                        |
| --- | --------------------- | --------------------------------------- | ------------------------------------------- | ---------------------------- | --------------------------- |
| C1  | 登录态无缝进 AI pro   | 电商内点「进入 AI Pro」跳 chat UI       | chat UI 用本平台 id_token 换 hub token 成功 | POST /hub/token 返前端 token | ⬜待真机                    |
| C2  | 建 wss + 发消息收回复 | 进 AI pro 发一条消息                    | 收到 openclaw 流式回复                      | wss reply_delta              | ⬜待真机                    |
| C3  | 跨租户隔离            | 用户 A 的前端 token 试访问用户 B 的租户 | hub authorizeSubForTenant 拒(403)           | 三段闸                       | ⬜待真机(需两 Cognito 身份) |

### D. 双 Pool(#101,可选加固,demo 先单 Pool 两 client)

| #   | 命题                                     | 预期                                      | 状态                             |
| --- | ---------------------------------------- | ----------------------------------------- | -------------------------------- |
| D1  | 前端 token 与 channel token 分 Pool 验签 | entry pool 验前端、claw-pool 验 channel   | ⬜规划(#101,demo 先单 Pool 跑通) |
| D2  | 跨 Pool token 互拒                       | entry pool 签的 token 不能当 channel 身份 | ⬜规划(#101)                     |

## 跑法

- 前端/broker 逻辑单测:已做(A2/A3/A4 骨架自测通过,见 broker/handler.py 自测)。
- 真机端到端(A1/B*/C*):`setup-federation.sh` 起 entry pool + 联邦 → 浏览器 chrome-cdp 驱动 marketplace.html 走完整流程 → 核对渲染 + 抓 wss。凭据见 CLAUDE.local.md。
- 结果落 `engineering/evidence/`,截图存证。

## 当前诚实标注

骨架层(前端禁用门 A2、broker fail-loud A3/A4、设计约束 B4)已实现并自测通过;所有需要真起云资源的端到端项(A1/B1-B5/C1-C3/D\*)标 ⬜待真机,按 ADR 档 A step 4 推进,未真跑前不写「通过」。
