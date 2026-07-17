# Patch #311 · 第 3 层:CDK / 权限 / VPCE(需 cdk deploy)

这一层是**基础设施变更**,靠替换文件无法生效,必须 `cdk deploy`(或按下面的热补等价物)。这也是你们特别关注的 **dependency + 权限** 所在。

## 涉及的修复

| 修复 | 文件 | 解决什么 |
| ---- | ---- | -------- |
| #307 | `deploy/stacks/compute.py` | 给 host instance-role 授 `openclaw-tenant-secrets` 的读权限(`grant_read_data`)。**这是 #290/#306 DDB fallback 的硬前提** —— 缺它则 launch-vm.sh 自取 token 时 AccessDenied → VM 起不来卡 creating。 |
| #310 | `deploy/stacks/observability.py`、`config.yml.example` | secretsmanager Interface VPCE 加幂等门 `logging.aos.create_secretsmanager_vpce`(默认 true)。避免 VPC 已有同服务 VPCE 时 private-dns 冲突导致整栈 ROLLBACK。 |

## ⚠️ 依赖 / 权限:必须先看

### 1. #307 host_role 读权限 = fail-closed 前提(最先处理)

`launch-vm.sh`(host-scripts 层)在恢复路径会从 `openclaw-tenant-secrets` 自取 token,**读不到就中止启动**。所以在替换 host 脚本 / 重建租户**之前**,host_role 必须已有这张表的读权限:

- **纯 CDK 环境**:`cdk deploy` 即带上(#307 已在 `compute.py` 加 `grant_read_data`)。
- **来不及 deploy**:先跑 `../iam/apply-iam.sh <host-role> <region> <account>` 打等价 inline policy(deploy 后可删)。

验证(host 上跑,返回 `{}`/条目而非 AccessDenied 即 OK):
```bash
aws dynamodb get-item --table-name openclaw-tenant-secrets \
  --key '{"tenant_id":{"S":"__probe__"}}' --region <region>
```

### 2. #310 secretsmanager VPCE private-dns 冲突(deploy 前必查)

AWS 硬规则:**同一服务在同一 VPC 只允许一个 Interface VPCE 开 private DNS**。若你的 VPC 里已有一个 secretsmanager VPCE(上轮部署 RETAIN 残留 / 你自建),直接 `cdk deploy` 会因栈要建第二个开 private DNS 的而**冲突 → 整栈 ROLLBACK**。

**deploy 前先查**:
```bash
aws ec2 describe-vpc-endpoints --region <region> \
  --filters "Name=service-name,Values=com.amazonaws.<region>.secretsmanager" \
            "Name=vpc-id,Values=<your-vpc-id>" \
  --query 'VpcEndpoints[].[VpcEndpointId,PrivateDnsEnabled]' --output text
```

- **返回空**(没有现存 VPCE)→ 保持默认 `create_secretsmanager_vpce: true`,栈自己建。
- **已有一个开 private DNS 的** → 二选一:
  - **(a) 复用现有**(推荐):`config.yml` 设 `logging.aos.create_secretsmanager_vpce: false`,栈不建自己的,复用现有那个(private DNS 让标准域名 `secretsmanager.<region>.amazonaws.com` 照样解到它,rolesmapping Lambda 透明可用)。
  - **(b) 删残留再让栈建**:仅当那个 VPCE 确定是废弃残留、且不在被别的东西用时,`aws ec2 delete-vpc-endpoints --vpc-endpoint-ids <id>`,再 deploy。**只删 secretsmanager 这个 service、且在本 openclaw VPC 里的;绝不碰 execute-api 等别的 VPCE。**

## 怎么应用

```bash
# 从仓库根(config.yml 已按上面 #310 处理好):
bash setup.sh <region> <profile-or-dash>       # profile 传 "-" = 走 instance role
# 或: cdk deploy OpenClawOrchestrator --require-approval never -c region=<region>
```

`cdk deploy` 一次带上 #307(IAM grant)+ #310(VPCE 幂等)+ 第 2 层的 Lambda 代码(#298/#303-305)。

## 验证

- **#307**:见上面的 get-item 探针,不再 AccessDenied。
- **#310**:deploy 不再因 VPCE private-dns 冲突 ROLLBACK;栈到 CREATE_COMPLETE。
