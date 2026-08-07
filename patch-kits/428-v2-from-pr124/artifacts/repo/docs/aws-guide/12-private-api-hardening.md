# 私有控制面 API 与凭据加密

本节是给要上生产的对接方的加固附录,覆盖两件事,并给一个把两者串起来的可跑 demo:

1. **控制面 API Gateway 私有化加固** —— 在默认公网 EDGE 端点之外,再开一个 `PRIVATE` REST API,只能从 VPC 内经 execute-api VPC Endpoint 访问,method 走 IAM 授权(调用方须 SigV4 签名)。机器/生产流量走私有,浏览器/调试仍走 EDGE。
2. **凭据 KMS 加密注入(#118)** —— 平台把要注入租户 microVM 的凭据(如 AWS AKSK)先用 ClawPool CMK 加密成密文再提交;明文不进 API、不进 DynamoDB、不进日志,只有 EC2 host 用自己的 IAM role 解密。

> 两者都是 **config-gated,默认关**,开了才建对应资源(不影响现有部署)。私有化是"生产加固选项",demo 现状用默认 EDGE 端点即可跑通;需要机器面收敛到私有网络时按本节启用。

AWS 依据:[API Gateway private REST APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-private-apis.html)、
[private API resource policy 教程](https://docs.aws.amazon.com/apigateway/latest/developerguide/private-api-tutorial.html)、
[API Key 与 usage plan 最佳实践](https://docs.aws.amazon.com/apigateway/latest/developerguide/api-gateway-api-usage-plans.html)。

架构图见同目录 `arch-private-api-hardening.svg`(私有 API 数据流)与 `../../engineering/research/design-exploration/credential-kms-injection-e2e-diagram.svg`(凭据注入端到端)。

---

## 1. 私有化目标与三层控制

默认 API Gateway 是 **EDGE** 端点(AWS 托管 CloudFront 前置,公网可达),对浏览器/调试友好,但机器面/生产流量暴露在公网。加固目标是给机器面一条"不出 AWS 网络 + 强身份"的通道,同时不打断现有浏览器路径。

加固后一次请求要过三道关(层层收紧,任一不过即拒):

1. **网络层 —— 只能从 VPC 内经 execute-api VPC Endpoint 到达**。`PRIVATE` 端点 + PrivateLink,流量不出 AWS 骨干网,公网根本连不上。
2. **资源策略层 —— 只接受来自指定 VPCE 的流量**。私有 API 的 resource policy `Deny` 掉 `aws:SourceVpce` 不等于本 VPCE 的请求(私有 API 无 resource policy 无法部署,这是 fail-closed 硬约束)。
3. **身份与配额层 —— IAM 授权(SigV4)+ usage-plan key**。method 授权类型设 `AWS_IAM`(调用方须 SigV4 签名 + IAM 带 `execute-api:Invoke`)并保留 `api_key_required=true` 用于客户识别、配额和限流。真正的认证/授权来自 IAM、resource policy 和 Lambda 内 RBAC，不把 API Key 当第二认证因子。

> **高频坑一**:只挂 IAM policy、忘了把 method 授权类型设成 `AWS_IAM`,method 会对全网公开、IAM policy 根本不参与评估。本方案 CDK 用 `default_method_options` 统一设死。
>
> **私有 API 的 usage-plan key**:PRIVATE 与 EDGE 端点沿用同一客户标识和限流维度,也可给 platform authorizer 提供 scope 映射输入。它不是 secret-grade 身份证明。仓库默认 `default_no_jwt_role=viewer`,所以无 Bearer 的私有调用即使 SigV4 与 key 都通过,写路由仍会被 RBAC 拒绝；受信自动化要写入时必须显式配置 operator/admin 角色或提供相应 Bearer 身份。

---

## 2. 启用私有 API(CDK,`api.private_api_enabled`)

`config.yml` 里打开开关即随 `cdk deploy` 建全套(默认关 → synth 字节不变):

```yaml
api:
  private_api_enabled: true # 建 PRIVATE API + execute-api VPCE + IAM 授权
```

开了之后 `deploy/stacks/network_vpc.py` 会建这些资源:

- **execute-api Interface VPC Endpoint** —— `InterfaceVpcEndpointAwsService.APIGATEWAY`(即 `com.amazonaws.<region>.execute-api`),`private_dns_enabled=True`,专用 SG 只放 443 入站(来源限 VPC CIDR,`open=False` 严格由 SG 控,绝不 `0.0.0.0/0`)。
- **PRIVATE REST API** —— `endpoint_configuration=PRIVATE` 且关联上面的 VPCE,指向同一个控制面 Lambda(`api_fn`),用 `{proxy+}` ANY 一条集成覆盖全部路由(与 EDGE 同后端;`PRIVATE`/`EDGE` 端点类型互斥,所以是两个独立 RestApi 指同一 Lambda,这是 AWS 官方受支持的模式)。
- **resource policy** —— `grant_invoke_from_vpc_endpoints_only([vpce])` 一步生成"只允许该 VPCE"的策略(`Deny` 非本 VPCE + `Allow *`,官方模板)。
- **method 授权** —— `default_method_options` 统一 `AuthorizationType.IAM`。

部署后从 CloudFormation Outputs 拿两个值:`PrivateApiUrl`(私有 API 地址)、`ExecuteApiVpceId`(VPCE id)。

> **网络前提**：VPCE 挂到 `deploy/stacks/network_vpc.py` 解析出的目标 VPC。VPC 需启用 DNS support/hostnames。启用 execute-api private DNS 会影响同一 VPC 内公网 execute-api 默认域名解析；同时访问公私 API 时按 AWS 官方建议使用独立 private hosted zone。

### 2.1 调用方 IAM 权限(最小)

调用私有 API 的身份(EC2 实例角色 / 对接方后端的 role)要挂:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["execute-api:Invoke"],
      "Resource": [
        "arn:aws:execute-api:<region>:<acct>:<private-api-id>/v1/POST/tenants"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["kms:Encrypt"],
      "Resource": ["<ClawPoolCmkArn>"]
    }
  ]
}
```

`execute-api:Invoke` 用于调 API(收窄到具体 stage/verb/path,别一把 `api-id/*`);`kms:Encrypt` 用于加密要注入的凭据(见 §3）。解密权限只在 EC2 host role 上,调用方不需要也不应有 `kms:Decrypt`。

除 IAM 外,调用方还须发送私有 API 的 **`openclaw-private-key`**(`x-api-key` 头,与 EDGE key 分开、可独立轮换),用于 usage plan 和可选 platform scope。虽然 API Key 不是独立认证机制,仍应按敏感配置管理,经 env 传给脚本(`PRIVATE_API_KEY`),不落命令行/代码。

---

## 3. 凭据 KMS 加密注入(#118)——提交前多一层加密

要把凭据(AWS AKSK、业务 API key 等)注入某个租户的 microVM,**在调 create_tenant 之前**先用 ClawPool CMK 把每条凭据值加密成密文,再随 `injected_credentials` 提交。契约:

- `owner_id`(平台用户身份)是 **EncryptionContext 绑定**：加密时 `EncryptionContext={"owner_id": <id>}`,host 侧解密时用同一个 `owner_id`。密文只能在同一 owner_id 下解开,跨用户拿到也解不开。选 owner_id 不选 tenant_id 是因为——用户注册中心创建凭据时租户还不存在（tenant_id 是 create_tenant 内部才生成的），而 owner_id 注册时就有。
- `create_tenant` 的 body 带 `owner_id`（代开路径 `#143`，`body.owner_id`）+ `injected_credentials`。create 路径只校验并存储密文，不解密。API Lambda 仅为 `GET /tenants/{id}/credentials` 的 recipient-key 重包持有 ClawPool CMK `kms:Decrypt`; lifecycle consumer 保持 encrypt-only。
- host 在 VM 启动时从 DynamoDB 自取密文，用自己的 IAM role `kms:Decrypt`（EncryptionContext=owner_id）解密后写进 per-VM **只读盘** 的 `.env`，OpenClaw 原生 dotenv 把值喂给 agent。明文不进 `openclaw.json`、不过 SSM 命令行、不进 CloudTrail。

`injected_credentials` 结构（`core/utils.py` `_validate_injected_credentials` 校验）：

```json
{
  "kms_encrypted": true,
  "kms_key_arn": "<ClawPoolCmkArn>",
  "items": [
    { "name": "AWS_ACCESS_KEY_ID", "ciphertext": "<base64 CMK 密文>" },
    { "name": "AWS_SECRET_ACCESS_KEY", "ciphertext": "<base64 CMK 密文>" }
  ]
}
```

校验规则（不满足即 400，不落库）：`kms_encrypted` 必须 `true`（拒明文注入）；`kms_key_arn` 必须等于本栈 ClawPool CMK；`items[].name` 必须是 POSIX 环境变量名 `^[A-Z_][A-Z0-9_]*$`（防 dotenv 行注入）；`ciphertext` 为 base64、≤ 上限、去重；且**必须带 `owner_id`**（EC 绑定，缺则 400 而非让 host 启动时才失败）。

---

## 4. 一条龙 demo：CMK 加密 → SigV4 签名 → 经私有 API 创建租户

可跑脚本 `private-api-create-tenant-sigv4.py`（同目录）。它把 §2 的私有 API/SigV4 和 §3 的 CMK 加密串起来：用 CMK 加密 AKSK（EC=owner_id）→ 组 `injected_credentials` → 对 `POST /tenants` 做 SigV4 签名 → 经私有 API 提交。

```bash
pip install boto3 botocore requests
export AWS_REGION=us-east-1
# 要注入的凭据经环境变量传,绝不上命令行(命令行参数落 shell history + 进程表,破坏
# 本节 "明文不落命令行" 红线)。生产用临时凭据 / 从受控 secret 源读入这两个 env。
export INJECT_AWS_ACCESS_KEY_ID=AKIAEXAMPLE...
export INJECT_AWS_SECRET_ACCESS_KEY=wJalr...
# 私有 API 双因子:SigV4(IAM 身份,走标准凭据解析)+ x-api-key(openclaw-private-key)。
export PRIVATE_API_KEY=$(aws apigateway get-api-key --region us-east-1 --include-value \
  --api-key "$(aws apigateway get-api-keys --region us-east-1 --name-query openclaw-private-key \
                 --query 'items[0].id' --output text)" --query value --output text)

python3 private-api-create-tenant-sigv4.py \
  --api-url  "$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
                 --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='PrivateApiUrl'].OutputValue" --output text)" \
  --cmk-arn  "$(aws cloudformation describe-stacks --stack-name OpenClawOrchestrator \
                 --region us-east-1 --query "Stacks[0].Outputs[?OutputKey=='ClawPoolCmkArn'].OutputValue" --output text)" \
  --owner-id 11111111-2222-3333-4444-555555555555 \
  --name     my-agent
```

脚本关键点（都是 SigV4 调 execute-api 的踩坑规避，见代码注释）：

- **要注入的凭据经环境变量传,不上命令行** —— 命令行参数会落 shell history + `/proc` 进程表,与"明文不落命令行"红线冲突;脚本从 `INJECT_AWS_ACCESS_KEY_ID` / `INJECT_AWS_SECRET_ACCESS_KEY` 读。
- **必须从私有 API 可达的网络内跑** —— 同 VPC 的 EC2、或经该 VPCE 的网络。公网跑连不上 PRIVATE 端点（这正是加固目标）。
- SigV4 用 `botocore.auth.SigV4Auth`，service name 固定 `execute-api`（不是 `apigateway`），region = API 部署 region（填错签名必不匹配）。
- **body 只序列化一次**，签名的 `data` 和发送的 `body` 必须同一份字节 —— 别用 `requests` 的 `json=`（会重新序列化 → payload hash 不匹配 → 403，最高频错因）。
- 临时凭据（STS）自带 session token，`SigV4Auth` 会自动注入 `X-Amz-Security-Token`，不用手加。

预期返回 `202`（异步创建，返回 `{id, status}`）。之后 `GET /tenants/{id}` 轮询到 `running`，该租户的 VM 内 agent 即可用注入的 AKSK 调 AWS。

---

## 5. 验证 / 排错

| 现象                                         | 原因                                                                       | 处理                                                                      |
| -------------------------------------------- | -------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| 连不上私有 API（超时）                       | 从公网或非本 VPC 跑                                                        | 到同 VPC 的 EC2 或经该 VPCE 的网络跑                                      |
| `403 Missing Authentication Token`           | 没做 SigV4 签名 / method 是 AWS_IAM 但请求未签                             | 用脚本的 SigV4 路径,别裸 `curl`                                           |
| `403` 且签了名                               | 调用方 IAM 缺 `execute-api:Invoke`,或来源不是本 VPCE(resource policy Deny) | 补 IAM policy(§2.1);确认经本 VPCE                                         |
| `403 {"message":"Forbidden"}` SigV4/IAM 已通过 | 缺 `x-api-key`(usage-plan 客户标识)                                        | 带 `x-api-key: <openclaw-private-key>`(§2.1;脚本经 `PRIVATE_API_KEY` env) |
| `SignatureDoesNotMatch`                      | body 双重编码 / 时钟偏移 >5min                                             | body 统一一份字节;NTP 同步                                                |
| create 返 `400 injected_credentials...`      | 缺 owner_id / kms_key_arn 不匹配栈 CMK / name 非法 / 传了明文              | 按 §3 校验规则修正                                                        |
| VM 起来 agent 拿不到凭据                     | host 解密失败(EC 不匹配 / 无权限)                                          | 确认 create 时 owner_id 与加密时一致;host role 有本 CMK 的 `kms:Decrypt`  |

安全边界(本节加固对应的红线)：VPCE SG 只放 443 from VPC CIDR，绝不 `0.0.0.0/0`；私有 API 无 resource policy 无法部署（默认全拒）；本节注入调用方只需 `kms:Encrypt`,host 负责启动期解密；API Lambda 的 Decrypt 仅限 `/credentials` 重包路径。明文凭据不落 DDB、日志或命令行。
