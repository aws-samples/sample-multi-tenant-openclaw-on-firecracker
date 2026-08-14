# amipatch — 逐文件热应用指引（无 CloudFormation 重新部署）

写给执行者（人或 Claude Code executor）。**全程禁止触发 CloudFormation 栈更新**，也不要跑
`setup.sh`。目标环境是先用 CDK 部署过一次、之后在活系统上做过多处手工改动的环境；任何栈更新会
覆盖那些手工改动。

| 项 | 值 |
| --- | --- |
| patch id | `amipatch` |
| `base_sha` | `b1d4e4ddb1429f9d89bfb0114c47b99f72b7b327` |
| `patch_sha` | `f7c477096b4adf900a331e1c87ba1711fd436877` |
| 交付面 | 控制面 = 覆盖 Lambda 代码；数据面 = 用 Packer 烤新 golden AMI 并换 LaunchTemplate |
| 覆盖 fix | 10 个（#440 #445 #449 #456 #470 #477 #482 #485 #488 #495）|

`base_sha` 取自本环境的部署移交记录（该环境按此 commit 部署）。若你的环境实际部署点不同，
**停下**，不要继续 —— 范围会有缺口或重复。

---

## Step 0.0 真伪校验（先做，失败即停）

kit 里每个 `*.patched` / 源文件都必须与 `patch_sha` 的 Git 对象逐字节相同。

```bash
# 对 manifest.paths 里每条有 artifact 的记录：
#   sha256sum <kit>/<artifact>  ==  manifest.paths[<repo路径>].patch_sha256
python3 - <<'PY'
import hashlib, json, pathlib
kit = pathlib.Path(".")
m = json.load(open(kit / "manifest.json"))
bad = []
for p, v in m["paths"].items():
    a = v.get("artifact")
    if not a:
        continue
    got = hashlib.sha256((kit / a).read_bytes()).hexdigest()
    if got != v["patch_sha256"]:
        bad.append((a, v["patch_sha256"], got))
print("checked", sum(1 for v in m["paths"].values() if v.get("artifact")), "artifact(s)")
if bad:
    for a, want, got in bad:
        print("MISMATCH", a, "want", want, "got", got)
    raise SystemExit("STOP: kit 被篡改或打包错误")
print("OK: 全部 artifact 与 patch_sha 一致")
PY
```

哈希一律 SHA-256。若你的校验工具默认 SHA-1，会报假不匹配。

---

## Step 0 只读探针（一切下游步骤读它的输出）

```bash
bash lib/discover-env.sh <region> [aws-profile]
```

它写出 `environment.json`，本文档所有 `{{environment.*}}` 占位符都从那里取值。

**每条 AWS 命令都必须显式带 `--region`。** shell 里残留的 `AWS_REGION` 会把 CLI 指到另一个
区域，那里同名资源齐全，返回结果看起来合理但全是错的。

探针必须给出且不得为 null 才能继续：

| 占位符 | 含义 | 判定方式 |
| --- | --- | --- |
| `region` / `account` | 目标区域与账号 | `sts get-caller-identity` |
| `assets_bucket` | 资产桶 | 从在役 LaunchTemplate UserData 反查，不要按名字拼 |
| `host_asg` | host ASG 名 | 必须与 `openclaw-hosts` 台账的实例集**完全一致**；不一致则为 null |
| `host_lt_id` / `host_lt_current_version` | LT id 与 ASG **实际固定**的版本 | 读 ASG 自身，不取第一个正则命中，不假定 `$Default` |
| `host_canary_instance_id` | 一台隔离的已批准 host | 由操作者指定 |
| `api_function_name` | 控制面函数名 | 默认 `openclaw-api`；以探针实测为准 |

> host 机队按 **tag** 枚举，不按 ASG。曾出现 `Role=metal-host` 标签 5 台而 ASG 只有 4 台，
> 漏的那台承载真实租户。按 ASG 枚举会造成盲区。

---

## Step 1 取证与影响评估（无评估不动手）

```bash
# 控制面现状
aws lambda get-function --function-name {{environment.api_function_name}} \
  --region {{environment.region}} \
  --query 'Configuration.[Runtime,Architectures,MemorySize,Timeout,CodeSha256,RevisionId]'
aws lambda get-function-configuration --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --query 'length(Environment.Variables)'   # 记下这个数

# 数据面现状：在役 LT 版本里的 bootstrap 内容前缀
aws ec2 describe-launch-template-versions --launch-template-id {{environment.host_lt_id}} \
  --versions {{environment.host_lt_current_version}} --region {{environment.region}} \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
  | base64 -d | grep -o 'deployment/bootstrap/host/[0-9a-f]\{64\}'
# 期望看到 base 前缀 29901cb4b92f93eed6995f4737b29b2e2558836c911631209cd23260fe07af3b
# 看到别的值 = 环境不在 base_sha 上，停下

# 机队与台账
aws ec2 describe-instances --region {{environment.region}} \
  --filters "Name=tag:Role,Values=metal-host" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].[InstanceId,InstanceType,ImageId]' --output table
aws dynamodb scan --table-name openclaw-hosts --region {{environment.region}} --select COUNT

# 租户基数（收尾时要对得上）
aws dynamodb scan --table-name openclaw-tenants --region {{environment.region}} --select COUNT
```

写下：谁受影响、当前症状、根因、修复后的预期状态。**这一步没做完不要进 Step 2。**

---

## Step 2 恢复服务：覆盖控制面 Lambda 代码

这是本 patch 唯一的"热修"。它修的是 #456 / #470 / #482 / #485 四组请求路径缺陷。

### 2.1 备份（apply 之前必须成功）

```bash
aws lambda publish-version --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --description 'pre-amipatch anchor'
# 记下返回的 Version → {{environment.api_backup_version}}

aws lambda get-function --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --query 'Code.Location' --output text \
  | xargs curl -s -o /tmp/openclaw-api-backup.zip
sha256sum /tmp/openclaw-api-backup.zip     # 记下
```

### 2.2 用 overlay 打包，**不要**预打包整个包

该函数带 arm64 原生 wheel（cryptography / PyJWT / powertools）。预打包会把生成方的依赖版本
冻到你的函数上 —— 属于未请求的变更，而且跨平台易碎。overlay 用**你自己包里的依赖**，只换一方源码。

```bash
rm -rf /tmp/oc-api && mkdir -p /tmp/oc-api && cd /tmp/oc-api
unzip -q /tmp/openclaw-api-backup.zip
# 只覆盖这四个模块，其余一律不动
cp <kit>/lambda/api/services/action_idem.py         services/action_idem.py
cp <kit>/lambda/api/services/host_service.py        services/host_service.py
cp <kit>/lambda/api/services/tenant_query_service.py services/tenant_query_service.py
cp <kit>/lambda/api/services/tenant_service.py      services/tenant_service.py
zip -qr /tmp/openclaw-api-new.zip .
```

```bash
# 用变量装 unzip 输出再 grep,不要直接管道：
#   set -o pipefail + `unzip -l big.zip | grep -q` 会 SIGPIPE 杀掉 unzip(141) 造成假失败
LIST="$(unzip -l /tmp/openclaw-api-new.zip)"
printf '%s' "$LIST" | grep -c 'services/tenant_service.py'    # 必须 >=1
```

### 2.3 应用（确认门：先把命令读给操作者过一遍）

```bash
aws lambda update-function-code --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --zip-file fileb:///tmp/openclaw-api-new.zip
aws lambda wait function-updated --function-name {{environment.api_function_name}} \
  --region {{environment.region}}
```

**绝不覆写环境变量。** 该函数有 57 个 env key，你环境里的值是权威，仓库默认值只是制品来源。
`update-function-code` 不动 env，别顺手加 `update-function-configuration`。

### 2.4 验证

```bash
aws lambda get-function --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --query 'Configuration.CodeSha256'
#   必须与 2.1 备份时不同

aws lambda invoke --function-name {{environment.api_function_name}} \
  --region {{environment.region}} \
  --payload '{"path":"/ping","httpMethod":"GET"}' /tmp/oc-inv.json
#   判据是 FunctionError 缺席,不是 200 body。私有 API 上 /ping 返 404 是预期,不是失败

aws lambda get-function-configuration --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --query 'length(Environment.Variables)'
#   必须仍为 Step 1 记下的那个数
```

### 2.5 回滚（必须同时覆盖 alias 与 `$LATEST`）

```bash
aws lambda update-function-code --function-name {{environment.api_function_name}} \
  --region {{environment.region}} --zip-file fileb:///tmp/openclaw-api-backup.zip
aws lambda wait function-updated --function-name {{environment.api_function_name}} \
  --region {{environment.region}}
aws lambda update-alias --function-name {{environment.api_function_name}} --name live \
  --region {{environment.region}} --function-version {{environment.api_backup_version}}
```

只翻 alias 不够：生命周期 dispatch 的 SQS 事件源绑 `$LATEST`，alias 回滚不覆盖它。

---

## Step 2b（可选止血）把 host 脚本先推到在役机器

数据面的耐久修复走 Step 3 的新 AMI，而新 AMI 只对**新起的**实例生效。若你现在就需要在役 host
拿到 #449（journal 不再滞后）和 #485（reset 事务），可以先 SSM 推文件止血。这是 stopgap，
不是耐久修复 —— 换实例即失效，耐久性靠 Step 3。

```bash
# host 常在私有子网,SSH 可能不通;用 SSM 传文件(base64 单行,不要用 commands=[] 传多行脚本 ——
# shorthand 会把换行压成字面 n,而 Status 仍返 Success,骗过一整轮探针)
B64="$(base64 -i <kit>/host-scripts/reset-vm.sh.patched)"
aws ssm send-command --region {{environment.region}} \
  --instance-ids {{environment.host_canary_instance_id}} \
  --document-name AWS-RunShellScript \
  --cli-input-json "$(printf '{"Parameters":{"commands":["echo %s | base64 -d > /home/ubuntu/reset-vm.sh && chmod 0644 /home/ubuntu/reset-vm.sh"]}}' "$B64")"
```

`0644` 是对的：这些脚本由显式解释器调用（`bash x.sh` / `python3 x.py`），而且经 S3 下发时
Unix 权限位根本不被保存或传播。"必须 +x 否则 Linux 不执行"对这类文件是误解。

---

## Step 3 耐久修复：用 Packer 烤新 golden AMI 并换 LaunchTemplate

数据面的正式交付路径。完整参数说明见 `host-scripts/packer/CUSTOMER-GUIDE.md`，本节只给顺序和判据。

本步交付：#440（fluent-bit 装不上导致 host 永久 ABANDON）、#449（host-agent 加
`PYTHONUNBUFFERED=1`）、#485（`reset-vm.sh` op 专属事务）、#445（init-host 重跑安全）。

### 3.1 前置（缺一即停）

```bash
packer version          # CUSTOMER-GUIDE §1.2
aws --version           # CUSTOMER-GUIDE §1.3
packer init host-scripts/packer/host-golden.pkr.hcl    # §1.5 装 amazon 插件

# §3：构建前 Firecracker 制品必须已在资产桶,否则构建第 3 步报 S3 miss
aws s3 ls s3://{{environment.assets_bucket}}/deployment/binaries/firecracker/ \
  --recursive --region {{environment.region}}
```

> `CUSTOMER-GUIDE.md §3 方案 A` 引用的镜像同步脚本位于内部仓库路径，**公开发行版里不存在**。
> 用 **§3 方案 B（手工上传）**：它自带完整的 curl + sha256 校验 + `s3 cp`，双架构摘要齐全，
> 标题就是"脚本无法执行的环境"。

```bash
# 记录刷新前容量,收尾要对上
aws autoscaling describe-auto-scaling-groups \
  --auto-scaling-group-names {{environment.host_asg}} --region {{environment.region}} \
  --query 'AutoScalingGroups[0].[MinSize,MaxSize,DesiredCapacity,length(Instances)]'
```

### 3.2 配置构建参数（CUSTOMER-GUIDE §2）

```bash
cp host-scripts/packer/apse1.pkrvars.hcl host-scripts/packer/my.pkrvars.hcl
```

必填：`assets_bucket`（`openclaw-assets-<12位账号>`）、`recipe_version`、`root_volume_gb`、
`iam_instance_profile`（生产构建必须给，否则拉不到 Firecracker 制品会回落公网 GitHub）。

> `my.pkrvars.hcl` 会含你的账号 id 与子网。**公开发行版的 `.gitignore` 里没有这条忽略规则**
> （`.gitignore` 是公开仓库自有文件，不随同步更新），所以别把它提交进你自己的仓库。

### 3.3 构建并核对内容对等

```bash
packer build -var-file=host-scripts/packer/my.pkrvars.hcl \
  host-scripts/packer/host-golden.pkr.hcl
# 记下产出的 AMI id → {{environment.new_ami_id}}

bash host-scripts/packer/assert-parity.sh
# 断言新 AMI 与 Image Builder 产物内容对等。不通过就停,不要换 LT
```

### 3.4 备份当前 LT 状态

```bash
aws ec2 describe-launch-templates --launch-template-id {{environment.host_lt_id}} \
  --region {{environment.region}} --query 'LaunchTemplates[0].DefaultVersionNumber'
#   → {{environment.host_lt_backup_version}}
aws ec2 describe-launch-template-versions --launch-template-id {{environment.host_lt_id}} \
  --versions {{environment.host_lt_current_version}} --region {{environment.region}} \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.ImageId' --output text
#   → {{environment.host_lt_backup_ami}}
```

### 3.5 同时换 ImageId 与 bootstrap 前缀（一个新 LT 版本里一起改）

`init-host.sh` 走内容寻址 S3 asset，LT UserData 里只嵌那个 64 位 hex 前缀。先传对象，再建版本。

```bash
# a) 自检：新文件的 sha256 必须等于目标前缀
sha256sum host-scripts/init-host.sh.patched
#   必须 = 938e619b7c6e1b292733e9161d3f0b71603aa32f4930e15db9d551624bc72d90

# b) 传到内容寻址路径
aws s3 cp host-scripts/init-host.sh.patched \
  s3://{{environment.assets_bucket}}/deployment/bootstrap/host/938e619b7c6e1b292733e9161d3f0b71603aa32f4930e15db9d551624bc72d90/init-host.sh \
  --region {{environment.region}}

# c) 取在役 UserData,只替换那一个字面前缀。不做模板渲染
aws ec2 describe-launch-template-versions --launch-template-id {{environment.host_lt_id}} \
  --versions {{environment.host_lt_current_version}} --region {{environment.region}} \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
  | base64 -d > /tmp/ud.txt
sed -i.bak \
  's/29901cb4b92f93eed6995f4737b29b2e2558836c911631209cd23260fe07af3b/938e619b7c6e1b292733e9161d3f0b71603aa32f4930e15db9d551624bc72d90/g' \
  /tmp/ud.txt
grep -c '{{' /tmp/ud.txt      # 必须为 0。出现未渲染占位符立即中止
grep -c 938e619b7c6e /tmp/ud.txt   # 必须 >=1

# d) 建新版本：新 AMI + 新 bootstrap 前缀
aws ec2 create-launch-template-version --launch-template-id {{environment.host_lt_id}} \
  --source-version {{environment.host_lt_current_version}} --region {{environment.region}} \
  --launch-template-data "ImageId={{environment.new_ami_id}},UserData=$(base64 -i /tmp/ud.txt)"

# e) 翻默认版本
aws ec2 modify-launch-template --launch-template-id {{environment.host_lt_id}} \
  --region {{environment.region}} --default-version '$Latest'
```

`lib/lt-userdata.py` 提供确定性的解码/重打包（CDK 同款 base64+gzip 与 16KB 限制，会拒绝含
`{{ }}` 的模板，round-trip 自验）。UserData 结构复杂时用它而不是手工 base64。

### 3.6 受控刷新实例

**新 LT 版本本身不会更新在役 ASG** —— ASG 固定某个版本，只有新实例用新版本。

```bash
bash lib/apply-lt.sh refresh --asg {{environment.host_asg}} \
  --region {{environment.region}} --min-healthy 90
# 或直接：
aws autoscaling start-instance-refresh --auto-scaling-group-name {{environment.host_asg}} \
  --region {{environment.region}} \
  --preferences '{"MinHealthyPercentage":90,"InstanceWarmup":900,"SkipMatching":false}'
```

高 `MinHealthyPercentage` 保证一台一台换。**不要一次性替换整个机队** —— host 上跑着真实租户。

### 3.7 回滚

```bash
aws ec2 create-launch-template-version --launch-template-id {{environment.host_lt_id}} \
  --source-version {{environment.host_lt_current_version}} --region {{environment.region}} \
  --launch-template-data ImageId={{environment.host_lt_backup_ami}}
aws ec2 modify-launch-template --launch-template-id {{environment.host_lt_id}} \
  --region {{environment.region}} --default-version {{environment.host_lt_backup_version}}
aws autoscaling start-instance-refresh --auto-scaling-group-name {{environment.host_asg}} \
  --region {{environment.region}} --preferences '{"MinHealthyPercentage":90}'
```

旧的 bootstrap 对象是内容寻址的，天然还在，不需要恢复 S3。

---

## Step 4 资源配置变更：全部为可选，本 patch 不强制执行任何一条

本区间的合成模板差异里只有两条运行时参数，且**都源自示例配置的默认值**，不是代码语义变更。
你环境里的值以你自己的配置为权威。

| 参数 | 示例配置 base → patch | 建议 |
| --- | --- | --- |
| `InitHook.HeartbeatTimeout` | 1200 → 3600 | **建议改**。放宽方向，安全。imported VPC + metal 实测 1200s 不足，会连续 lifecycle ABANDON 换机 |
| `HostASG.MinSize` | 2 → 0 | **不要对在役机队改**。这是【首次部署】才对的值（黄金镜像烤制是非阻塞独立栈，min≥1 时 host 抢在镜像就绪前启动、拉不到镜像 → ABANDON → 反复换机）。对在役机队设 0 会允许缩到零台，承载真实租户的 host 可能被回收 |

若确认要放宽钩子超时：

```bash
aws autoscaling put-lifecycle-hook --auto-scaling-group-name {{environment.host_asg}} \
  --lifecycle-hook-name openclaw-host-init --region {{environment.region}} \
  --lifecycle-transition autoscaling:EC2_INSTANCE_LAUNCHING \
  --heartbeat-timeout 3600 --default-result ABANDON
# 验证
aws autoscaling describe-lifecycle-hooks --auto-scaling-group-name {{environment.host_asg}} \
  --region {{environment.region}} \
  --query 'LifecycleHooks[?LifecycleHookName==`openclaw-host-init`].[HeartbeatTimeout,DefaultResult]'
# 回滚：同一命令把 --heartbeat-timeout 改回 Step 1 记录的原值（RESTORE）
```

`TrackDefaultLTVersion`、`Assets` 桶的 CDK 归属 tag、`GoldenImageBuilder` 与其 IAM 策略、
`ApiHandlerCurrentVersion` 的增删 —— 这些是派生或内容哈希产物，**无需任何操作**。判据：
一次只删了两个内部文件的提交也会让 `GoldenImageBuilder` 变化。

---

## Step 5 部署机文件替换（无在役 AWS 写操作）

这些是下次部署才生效的部署机工具与模板。替换即完成。

| kit 内路径 | 替换到仓库 |
| --- | --- |
| `host-scripts/deploy-machine/scripts/checks/tenant-query-rollout.py` | `scripts/checks/tenant-query-rollout.py` |
| `host-scripts/deploy-machine/deploy/stacks/tenant_query_rollout.py` | `deploy/stacks/tenant_query_rollout.py` |
| `host-scripts/deploy-machine/scripts/preflight-check.sh` | `scripts/preflight-check.sh` |
| `host-scripts/deploy-machine/setup.sh` | `setup.sh` |
| `host-scripts/deploy-machine/config.yml.example` | `config.yml.example` |
| `host-scripts/deploy-machine/samples/config-sg-prod.yaml` | `samples/config-sg-prod.yaml` |

前两个是 #495 的耐久修复：这套环境首次部署时曾在部署机上手工改过
`scripts/checks/tenant-query-rollout.py` 绕过"一次最多建 1 个 GSI"的误报，本 patch 把上游
两半（模块签名加 `table_exists` 参数 + 调用方传参）都补齐，手工改动不再需要。

替换后必须能跑通：

```bash
python3 scripts/checks/tenant-query-rollout.py --region {{environment.region}}
# 表不存在时应输出 "does not exist yet; CFN creates it with all GSIs at once" 并退出 0
bash -n scripts/preflight-check.sh && bash -n setup.sh
python3 -m py_compile scripts/checks/tenant-query-rollout.py deploy/stacks/tenant_query_rollout.py
```

---

## Step 6 新机验证（本 patch 必做）

本 patch 改了未来实例的来源（新 AMI + 新 bootstrap 前缀），所以**必须**起一台新 host 干净启动，
不做任何 Step 2b 的手工止血。

三个信号缺一即失败：

```bash
# 1) 新实例的 UserData 解码后含新前缀且无未渲染占位符
aws ec2 describe-instance-attribute --instance-id <新实例> --attribute userData \
  --region {{environment.region}} --query 'UserData.Value' --output text \
  | base64 -d | grep -c 938e619b7c6e      # 必须 >=1
aws ec2 describe-instance-attribute --instance-id <新实例> --attribute userData \
  --region {{environment.region}} --query 'UserData.Value' --output text \
  | base64 -d | grep -c '{{'              # 必须为 0

# 2) 注册进台账
aws dynamodb scan --table-name openclaw-hosts --region {{environment.region}} \
  --filter-expression 'instance_id = :i' \
  --expression-attribute-values '{":i":{"S":"<新实例>"}}' --select COUNT   # 必须 >=1

# 3) 生命周期钩子 CONTINUE 而非 Heartbeat Timeout
aws autoscaling describe-scaling-activities \
  --auto-scaling-group-name {{environment.host_asg}} --region {{environment.region}} \
  --max-items 10 --query 'Activities[].[StatusCode,Description]' --output table
#   不得出现 Heartbeat Timeout / ABANDON
```

机队版本不齐是正常的：刷新过程中新旧 AMI 会共存。逐 host 备份自己的原版本号，patch 最终收敛。

---

## Step 7 逐 fix 验证计划

"建了个租户，它 running 了"**不算验证** —— 那只证明代码装上了，没证明每个 fix 生效。
以下每条都有硬信号与明确的 pass / fail。完整定义见 `manifest.json` 的 `verifications[]`。

### Phase A — 只读，零副作用（每次都跑）

| fix | 动作 | pass | fail |
| --- | --- | --- | --- |
| #449 | `systemctl show host-agent -p Environment`；比对 `journalctl -u host-agent -n 1` 时间戳与当前时间 | 含 `PYTHONUNBUFFERED=1` 且时间差在秒级 | 缺该 env，或时间差达分钟级 |
| #456 | 以 operator（非 admin）身份 `POST /tenants/{id}/rebuild` | 非 admin 被拒；admin 返 **200**（无法确认采用时 503） | 非 admin 也能调用，或仍返 202 |
| #470 | 新建租户后 `GET /tenants/{id}`；再用条件查询取同一租户 | 新租户含 `rootfs_version`；条件与非条件查询返回同一字段集（含 `app_health`/`metrics`） | 缺 `rootfs_version`，或条件查询裁掉了那两个字段 |
| #482 | 不传 `image_channel` 调 rebuild；再对空 canary 槽的 host 调 canary | 缺省走 `live`；空槽位返回明确错误 | 缺省不是 live，或空槽位被静默接受 |
| #485 | 用**同一** `client_token` 连续两次 `POST /tenants/{id}/reset` | 第二次命中幂等记录，返回同一 op 身份，无第二次 reset 副作用 | 两次都真执行了 reset |
| #445 | 对同一 host 记录 `used_vcpu`/`used_mem_mb`/`vm_count`，重跑 init-host 后再读 | 计数器保持事实值 | 被打回初值 |
| #488 | `bash scripts/preflight-check.sh <region>` | 退出 0，或给出明确阻断原因 | 报示例配置自相矛盾 |
| #495 | 在 tenant-query 表不存在的环境跑该检查 | 输出 `does not exist yet; CFN creates it with all GSIs at once` 且退出 0 | 报 `at most one GSI` 且非零退出 |

### Phase B — 整生命周期，全部走真实 API（核心，跑一次）

| fix | 动作 | pass | fail |
| --- | --- | --- | --- |
| #440 | 在新 AMI 起的 host 上：`systemctl is-active fluent-bit`；`ls -l /usr/share/keyrings/fluentbit*.gpg` | fluent-bit active 且 keyring 存在 | 安装返 rc=2，或 host 卡到 ABANDON |
| #477 | `bash host-scripts/packer/assert-parity.sh`；并让一台新 host 走完 lifecycle | 对等断言通过；新 host 注册进台账且钩子 CONTINUE | 断言失败，或出现 Heartbeat Timeout / ABANDON |

然后跑一遍真实链路：并发建 N 个租户（10/s）→ 全部轮询到 running（任何一个卡在 `creating`
超过启动窗口即失败）→ 取一个租户走 get-credentials（用 KMS 解 `openclaw-tenant-secrets` 条目
与 guest 内 `openclaw.json` 的 `.gateway.auth.token` 比对一致性，**不要**直接解信封）→ 连 wss
（认证在 hello/challenge **帧**，不在 101 握手）→ stop / start / rebuild（都经
`POST /tenants/{id}/{action}`）→ 确认数据盘保留。

不变量（可证伪）：

- 无租户卡在 `creating`
- 不存在 `assignment=failed` 而 `tenant=running` 的跨表矛盾
- 无超卖：`used_vcpu <= cap`

---

## Step 8 精确收尾（一对一，零通配）

host 上有几百个真实租户（`thr*` / `t-*`）。一个手滑的通配删除就是数据丢失。

测试租户用唯一的零填充前缀。**只删创建时记录下来的确切 id，逐个删，绝不用前缀 glob。**

```bash
# 对记录下来的每一个 id：
curl -s -X DELETE -H "x-api-key: $KEY" \
  "$BASE/tenants/<确切id>?keep_data=false"
#   默认 keep_data=true 是软删,盘还在,必须显式传 false
# 轮询到 deleted,然后 SSM 确认 /data/firecracker-vms/<确切id> 已消失且无孤儿 firecracker 进程
# 仅当确有残留时,对【完整 id】做精确 rm -rf,不要对前缀操作
```

收尾后真实租户总数必须与 Step 1 记录的完全一致。

---

## 状态与已知限制

- 本 kit 的 `status` 由 `validate-patch.sh` 从 `operations[].class` **推导**，不是手写。
  含 `MANUAL_CLI_REVIEW` 的操作（写在役 LaunchTemplate、翻默认版本、刷 ASG 实例）需要操作者复核，
  因此不能声称"无需任何审阅"。
- `docs/` 下 5 个文件与 `patch/` 下 4 个发布收据不产生任何客户操作，只随 kit 携带。
- 本环境使用**独立的** LiteLLM 网关（`ai_gateway.url` 已填），本区间不涉及网关资源改动。
