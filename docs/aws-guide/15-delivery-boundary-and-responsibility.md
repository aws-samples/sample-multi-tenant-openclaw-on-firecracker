# 交付边界:部署后必配项清单 + 配置责任矩阵

> 面向"平台交出去、客户自建生产 pool"的场景。把「入口谁配、证书谁配、部署后还要配什么、哪段 config 归谁改」钉死,避免每个客户上线靠联调会口口相传。
> 本文覆盖 R17 的 W4(部署后必配项清单)+ W5(配置责任矩阵)。BYO-ALB 入口拆分(W1)、私有 API SigV4(W2,见 `12-private-api-hardening.md`)、mTLS 挂载点(W3)的 config 项落地属独立 MR,本文只登记责任归属。

---

## 一、部署后必配项清单(W4)

下列每项 = "配什么 / 在哪配 / 不配的后果"。**加粗**项是客户侧上线前必改,否则功能不可用或有安全缺口。

| #   | 必配项                                               | 在哪配                                                                                      | 不配的后果                                                                       | 责任方                      |
| --- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------- | --------------------------- |
| 1   | **模型访问激活**(新账号首次)                         | AWS 控制台 Bedrock → Model access,激活所用模型;新账号还需确认组织与 partner 隔离            | LiteLLM→Bedrock 调用 AccessDenied,agent 全不可用                                 | 客户侧                      |
| 2   | **LiteLLM url**(SSM `/openclaw/litellm-host`)        | `config.yml` `ai_gateway.url`(部署时写 SSM);或部署后改 SSM                                  | 实例继承的默认 url 错 → 模型调用失败(纪要3 踩过手工改 SSM 止血)                  | 客户侧                      |
| 3   | **LiteLLM key**(SSM `/openclaw/litellm-shared-vkey`) | 部署后 setup/运营写 SSM(SecureString);per-tenant vkey 优先,shared 兜底                      | 无 key → agent 拿占位符调 LiteLLM 401                                            | 客户侧                      |
| 4   | **config_template registry 指针**                    | 空模板(→`default`)现已自愈补种(R15.3);具名模板需 admin `POST /registry/{tpl}` 发布          | 具名模板无 current pointer → `400 no current pointer`(新加坡真机踩过)            | 平台侧建默认 / 客户侧建具名 |
| 5   | **证书 ACM ARN**(console BFF)                        | `config.yml` `console_auth.bff_certificate_arn`(区域内 ACM);CloudFront 域名证书须 us-east-1 | 不配 → BFF 就位但 ALB 不开 443 listener,console 登录门不生效                     | 客户侧                      |
| 6   | ALB(导入既有 vs 平台新建)                            | `config.yml` edge/console 入口段(W1 导入开关,独立 MR 落地)                                  | 默认平台新建;客户已有 ALB 要导入须显式配                                         | 客户侧选择                  |
| 7   | mTLS truststore + 服务端证书                         | `config.yml` mTLS 段(W3,独立 MR);客户集团内置 CA 自配                                       | 需双向 TLS 而不配 → 入口层不校验客户端证书                                       | 客户侧(平台不代签发)        |
| 8   | SG / 域名                                            | `config.yml` `host.ssh_ingress_sg` / `cloudfront.*_domain`                                  | SG 留空=无 SSH 入站(生产正确);域名留空=用默认 \*.cloudfront.net                  | 客户侧                      |
| 9   | 私有 API(收私网)                                     | `config.yml` `api.private_api_enabled` + 跨账号 VPCE resource policy                        | 默认走 EDGE;要收进集团网络才开(见 `12-private-api-hardening.md`)                 | 客户侧                      |
| 10  | **client_token 复用**(客户端接入)                    | 客户端创建请求代码                                                                          | 重试时换 token → 串号双开(R16.1,见 `09-api-integration.md` openapi client_token) | 客户侧                      |
| 11  | 客户端创建超时                                       | 客户端 HTTP 超时配置(建议 ≥10s)                                                             | 2s 超时遇 gateway 冷启(~4.7s)必超时重发,叠加 #10 放大串号(R16.3 止痛)            | 客户侧                      |
| 12  | 测试环境 host 台数                                   | `config.yml` `asg.min_capacity` / 运营扩容                                                  | 单台测不出并发/排队/串号类 bug(R16.5 前置)                                       | 客户侧                      |

**交叉引用**:私有 API SigV4 加固 → `12-private-api-hardening.md` + `private-api-create-tenant-sigv4.py`;client_token 幂等 → `09-api-integration.md`;registry 模板发布 → R15.3。

---

## 二、配置责任矩阵(W5,config.yml.example 全 28 段)

每段标:**谁改**(客户侧 C / 平台侧 P)· **在哪改** · **影响面**(是否需重建栈)。以 `config.yml.example` 逐段核实(2026-07-12)。

| 配置段           | 谁改 | 在哪改                          | 改动影响面                                                                                        |
| ---------------- | ---- | ------------------------------- | ------------------------------------------------------------------------------------------------- |
| `host`           | C    | config.yml                      | 重建栈(arch/机型/盘换=换 host)                                                                    |
| `balloon`        | P    | config.yml                      | 重建栈(内存回收参数,冷注入)                                                                       |
| `runtime`        | P    | config.yml                      | 重建栈(hypervisor 选型)                                                                           |
| `vm`             | C    | config.yml                      | 重建栈(默认 vcpu/mem/盘/端口基址)                                                                 |
| `s3`             | P    | config.yml                      | 重建栈(备份前缀/节拍/保留)                                                                        |
| `asg`            | C    | config.yml                      | 重建栈(min/max/超时);min_capacity 客户按需抬                                                      |
| `multi_az`       | C    | config.yml                      | 重建栈(跨 AZ HA;关省跨 AZ 传输费)                                                                 |
| `network`        | C    | config.yml + setup 交互         | 重建栈(default_vpc/self_managed/imported,切模式必重建)                                            |
| `redis`          | P    | config.yml                      | 重建栈(两级路由路由表;开必配 edge)                                                                |
| `edge`           | C/P  | config.yml                      | 重建栈(数据面边缘 ASG + DNAT 端口段;端口段与 SG/位图同源)                                         |
| `scaler`         | P    | config.yml(部分只重部署 Lambda) | idle_reclaim_enabled/队列开关=重部署 Lambda;GSI=重建栈                                            |
| `dispatch`       | P    | config.yml + 运行时 SSM(andon)  | 重建栈(队列);andon 急停走运行时 SSM 不重建                                                        |
| `health_check`   | P    | config.yml                      | 重部署 health_check Lambda                                                                        |
| `api`            | C    | config.yml                      | 限流=重部署;private_api/scoped-key=重建栈                                                         |
| `waf`            | C    | config.yml                      | 重建栈(WAF 关联 API)                                                                              |
| `flow_logs`      | P    | config.yml                      | 重建栈(VPC Flow Logs,安全默认开)                                                                  |
| `agentcore`      | C    | config.yml                      | 重建栈(AgentCore 接入)                                                                            |
| `console_auth`   | C    | config.yml + setup(证书)        | 重建栈;真 admin key 部署后 setup 写 Lambda env 不进模板                                           |
| `dynamodb`       | P    | config.yml                      | 重部署(PITR 开关,安全默认开)                                                                      |
| `audit`          | P    | config.yml                      | 重建栈;CMK/WORM 只能全新账号首次开(存量切会丢历史)                                                |
| `external_authz` | C    | config.yml + Secrets Manager    | 重建栈(映射写权威外置;HMAC 密钥走 CFN 动态引用)                                                   |
| `exchange_idp`   | C    | config.yml + Secrets Manager    | 重建栈(OIDC 联邦;端点由平台侧提供,不硬编造)                                                     |
| `cloudfront`     | C    | config.yml + setup(证书)        | 重建栈(自定义域名;证书须 us-east-1)                                                               |
| `notifications`  | C    | config.yml                      | 重建栈(SNS topic;部署后订阅)                                                                      |
| `metrics`        | C    | config.yml                      | 重建栈;use_managed=true 走 AMP+AMG(强制 SSO),默认自建 Prom/Grafana                                |
| `security`       | C/P  | config.yml                      | 多为重建栈;CMK/WORM 全新账号首次;egress allowlist 改部署代码重建;guardrail 切 true 前须删带外同名 |
| `hub`            | P    | config.yml                      | 重建栈(历史 hub 端点;数据面转型后多为留空)                                                        |
| `ai_gateway`     | C    | config.yml + SSM                | 重建栈;url 填全 https 直接透传(R15.1),端口/path 走 host env                                       |
| `image`          | P    | config.yml                      | 重建栈(黄金镜像版本;CodeBuild 栈内烤)                                                             |

**维护纪律**(R17.16 / 承接 R9 配置纪律):`config.yml.example` 新增/删除配置段时,本矩阵同步更新;新增项须在必配项清单(§一)中标"客户侧上线前必改"或"平台侧"。

---

## 三、W 线范围边界(明确不做,R17.19-R17.20)

- 客户自定义 build-rootfs 内网化改造脚本(公网拉取 → 内部 Git/离线包)由**客户自维护**,平台不代管/代改。
- 模型版本升级清单(如 4.7→4.8)由**客户自配**,不在本交付边界内。
