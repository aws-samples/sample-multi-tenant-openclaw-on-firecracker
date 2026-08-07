# Host / Edge 启动脚本版本 API（golden AMI + bootstrap 版本切换）

本文是 issue #389 v2 交付的**使用说明**：怎么把 host 开机装组件的过程搬进一个预烤好的 golden AMI，怎么改开机脚本，以及怎么用 API 在**已发布的** bootstrap 版本之间切换（含回滚）。

按**改动谁**分为两个部分，因为「换镜像里装了什么」和「换开机时读哪个脚本」是两条独立的链路，触发方式和生效时机都不同：

- **Part A · Golden AMI（烤镜像）**：把所有需要联网下载的安装步骤预先烤进一个 AMI。走 EC2 Image Builder，用 `cdk deploy` + 一条 `start-image-pipeline-execution`，**没有 API**。
- **Part B · Bootstrap 版本切换 API**：在**通过四项校验的已发布**开机脚本版本之间切换默认版本（四项校验见 2.4）。两个同步 HTTP 接口，admin-only。

> 一句话区分：**Part A 换的是"机器上预装了什么"，Part B 换的是"开机时读哪份脚本"**。两者都只影响**下次启动的实例**，都不动存量在跑的 host（见 2.5 K1）。

运维手册（含加软件、改脚本的完整步骤和真机验收清单）在 `engineering/runbooks/HOST-EDGE-BOOTSTRAP-389.md`；本文是面向调用方的接口与操作说明。

## 0. API Gateway / OpenAPI 文档约定

仓库控制面定义保留 **OpenAPI 3.1**（`engineering/backend/openapi-control-plane.yaml`，机读单一真相源）；本文是面向调用方的规范化集成说明，按 Amazon API Gateway 的 **Resource + Method / OpenAPI Operation** 结构展开，并与代码实现保持一致。

- 每个接口以 `HTTP method + resource path + operationId` 唯一标识；
- `x-api-key` 由 API Gateway usage plan 校验；启用 RBAC 时，`Authorization: Bearer <id_token>` 由 Lambda 校验；
- API Gateway/Lambda proxy 返回的 HTTP 状态码和 JSON body 共同构成契约，客户端应优先根据稳定的 `code` 分支；
- Part A 不是 API，它的"接口"是 `config.yml` 的开关 + CDK stack + Image Builder 流水线 ARN。

## 1. 两个部分与 API 总览

### 1.1 Part A · Golden AMI（无 API，走 CDK + Image Builder）

| # | 触发方式 | 作用域 | 执行方式 | 语义 |
|---:|---|---|---|---|
| A1 | `cdk deploy OpenClawHostImage` | 全 fleet | 同步（CloudFormation） | 部署/更新 Image Builder 的 component + recipe + distribution，**不烤镜像** |
| A2 | `aws imagebuilder start-image-pipeline-execution --image-pipeline-arn <ARN>` | 全 fleet | **异步**（约 15–30 分钟） | 真正烤一次镜像；内含零下载断言与 image-tests，任一失败则不产出 AMI |
| A3 | `config.yml` 的 `host.golden_ami.use: true` + `cdk deploy` | 全 fleet | 同步 | 让 host LaunchTemplate 改用 golden AMI（`resolve:ssm:`），只影响之后 launch 的实例 |

`<ARN>` 从 `OpenClawHostImage` stack 的 output **`HostImagePipelineArn`** 读；SSM 参数名从 output **`HostGoldenAmiParameter`** 读（默认 `/imagebuilder/openclaw/host-ami<region后缀>`）。

### 1.2 Part B · Bootstrap 版本切换 API

| # | Method + resource path | `operationId` | 作用域 | 执行方式 | 语义 |
|---:|---|---|---|---|---|
| B1 | `GET /bootstrap/versions` | `listBootstrapVersions` | host + edge 两个 fleet | 同步查询 | 列每个 fleet **可切换**的 bootstrap 版本，并标出当前默认 |
| B2 | `POST /bootstrap/promote` | `promoteBootstrapVersion` | 单个 fleet | 同步 | 把该 fleet 的默认版本切到一个**已发布**的版本 |

两个接口都是 **admin-only**。完整 bootstrap surface 就是这 2 个接口，**没有** create/upload/delete —— 这是刻意的，见 3.1。

**这两个接口不做的事**（安全边界的核心，务必先读）：

- **不创建 LaunchTemplate 版本**，也**不上传任何脚本**。请求体里传的是 sha256 摘要，**永远不传脚本内容**。
- **不触发 instance refresh**，**不替换任何存量在跑的实例**。
- **不修改 ASG**（没有 `UpdateAutoScalingGroup` 权限），**不启动实例**（没有 `RunInstances`/`PassRole`）。
- **只能切到一个通过全部四项校验的已发布 LT 版本**（2.4）：它相对当前默认只差一个 bootstrap 摘要，且那个摘要的 S3 对象字节自洽。切不到一个顺带改了 AMI / IAM role / 网络配置的版本，也切不到一个 user-data 里夹带了额外命令的版本。

后果边界（**只针对这两个 bootstrap 接口**）：**即使 API key 泄露，攻击者通过这两个接口能做的最坏的事是把默认版本切到某个满足四项校验的既有已发布版本**——即一个相对当前默认只差一个 bootstrap 摘要、且该摘要的 S3 对象在 `ASSETS_BUCKET` 里字节自洽的版本。拿不到写 user-data 的能力，也就注入不了新代码。

> 这条边界的强度取决于**谁能创建 LT 版本、谁能往 `ASSETS_BUCKET` 的 bootstrap 前缀写对象**——那是另一组权限，不由这两个接口授予（它们既不 `CreateLaunchTemplateVersion` 也不 `PutObject`）。候选集必须由别的途径预先造出来；正常情况下那就是 `cdk deploy` / `setup.sh`，但代码校验的是字节而不是来源，所以任何同时持有 LT 写权限与该 bucket 前缀写权限的主体都能造出合规候选。要收紧这一面，收紧那两组权限，不要指望这两个接口。

> 这**不是**关于 API key 整体权限的声明。api-key 路径在 `core/auth.py` 里解析成 `is_admin=True`（受信自动化全权，见 3.1），泄露的 key 对**控制面其他 admin 路由**同样有效。上面这条边界只说明"即便持有 key，也无法借由 bootstrap 接口注入新的开机代码"，不代表泄露 key 后果轻微——key 泄露要按凭据泄露处理并轮换。

> 精确表述很重要：约束**不是**"版本必须由 `cdk deploy` 创建"（代码里没有不可伪造的部署来源校验），而是 2.4 那四项**逐字节**校验。二者的实际效果一致——能过四项校验的版本在字节上就等价于 CDK 会为该摘要产出的版本——但把它说成"来源保证"会让人误以为存在一个来源 allowlist。

## 2. 集成概念

### 2.1 provision 与 configure 的分界

host 开机分成两段，分界线只有一个问题：**这一步要不要联网或装包？**

| 阶段 | 脚本 | 做什么 | 能不能烤进 AMI |
|---|---|---|---|
| **provision** | `deploy/userdata/provision-host.sh` | 装组件：基础包、awscli、Firecracker + jailer、guest kernel、ADOT collector、Fluent Bit。**整个开机链路里的所有外网下载都在这里** | **能**（无 per-host、无 per-deployment 输入） |
| **configure** | `deploy/userdata/init-host.sh` | 渲染 `/etc/platform.env`、挂 `/data` 数据盘、生成**本机**的 ed25519 密钥、注册进 DynamoDB | **绝对不能**（需要 per-host 身份和 per-deployment 密钥） |

这条线为什么画在这里：golden AMI 的存在理由是**联网装组件不可靠**。错架构的 awscli zip、缺 aarch64 后缀的 vmlinux、404 的 Firecracker tarball 都真实发生过，每一次都让 host 丢掉 600 秒的 lifecycle hook，把 ASG 推进 ABANDON-重建循环。把所有下载烤进镜像，golden host 开机就是**零外网下载**，这类失败从根上消失。

`init-host.sh` 里的分支（`deploy/userdata/init-host.sh:145`）：

```bash
if [ -f /etc/openclaw/.ami-provisioned ]; then
  # golden AMI:provision 已经在烤制时跑过,跳过组件安装
else
  # 普通 Ubuntu AMI:现在 inline 跑一遍 provision-host.sh(旧路径逐字节不变)
fi
```

所以**普通 AMI 路径完全没变**，golden AMI 是纯增量的可选项。

### 2.2 `.ami-provisioned` marker

provision 成功后**最后一步**写 `/etc/openclaw/.ami-provisioned`（部分完成不能看起来像完成）。内容记录这台机器的 provenance：

```
recipe_version=1.0.2
provisioned_at=2026-08-04T12:34:56Z
provisioned_arch=aarch64
firecracker_version=v1.13.1
guest_kernel=vmlinux-6.1.102
baked_dir=/opt/openclaw/firecracker-assets
```

排障时这是第一个要看的文件：它同时回答"这台是不是 golden 起的"和"烤的是哪个 recipe 版本"。

### 2.3 bootstrap 摘要寻址

`init-host.sh`（以及 edge 那棵树打成的 `edge-bundle.tar.gz.b64`）不放在可变的 S3 前缀里，而是按内容的 sha256 存到**不可变的 key**。LaunchTemplate 的 user-data 里编入这个 sha256，开机时下载并校验字节，**不符就 fail-closed**。

这治的是 #265 的病根：旧的 `setup.sh` 往可变前缀 `deployment/edge/` 手工上传，patch 时忘了传就"改了 S3 但静默用旧版"。摘要寻址下这种漂移不可能静默发生。

**已知副作用（H1 决策时明确接受）**：改 `deploy/edge/` 下任何文件不再"改 S3 即生效"——整棵树是一个 bundle、一个 sha256。要么 `cdk deploy`，要么用 Part B 的 API 在已发布版本间切。

### 2.4 可切换版本集

`GET /bootstrap/versions` 的 `available` **不是 S3 桶里的对象列表**，而是 LaunchTemplate **已发布版本**中满足严格条件的子集。真值来源是 **ASG 实际跟踪的那个 LT 的 `$Default` 版本的 user-data**（从 ASG 反查 LT id，所以 edge LT 名带部署后缀也无妨）。

一个版本**可切**，当且仅当：

1. 它是**已发布**的 LT 版本；且
2. 它与当前默认版本**除 UserData 外逐字段相等**（AMI、IamInstanceProfile、NetworkInterfaces、BlockDeviceMappings、MetadataOptions… 全部一致）；且
3. 它的 user-data 恰好是把默认版本 user-data 的 bootstrap 摘要换成它自己摘要的**逐字节 rekey** 结果 —— 纯摘要替换，多出来任何字节都不成立；且
4. 它的 bootstrap 对象从**本 stack 的 `ASSETS_BUCKET`** 下载。

当前默认版本自身**在它也满足条件 4 时**可切（这是幂等目标）。反过来说：如果当前默认的 bootstrap 指向别的 bucket，它自己也过不了条件 4，于是该 fleet 的 `available` 整体为空——**包括当前默认在内**，此时任何 promote（含幂等重放）都返回 `404`。这与下面条件 4 的整体后果是同一件事，不是两条互相矛盾的规则。

条件 2 + 3 是真正的授权边界，挡两件事：user-data 里夹带了额外 shell 命令的"版本"；以及一个顺带改了 AMI 或实例角色的版本被当成"只切 bootstrap"切上去，悄悄改掉整个机队的启动安全边界。

条件 4 有个整体后果：**若当前默认版本的 bootstrap 指向别的 bucket，该 fleet 的 `available` 会是空的**（fail-closed）。因为 promote 前的 S3 字节复核只查 `ASSETS_BUCKET`，切到别的 bucket 的版本会让实例开机拿不到脚本——那是 false-green。

> 关于"必须来自 CDK"：这里**没有**做不可伪造的部署来源 allowlist。条件 2+3 已经让可切候选 = **与当前默认逐字节只差一个 bootstrap 摘要**的版本，且目标对象还要过字节摘要复核。所以即便有人手工建了一个满足这些条件的版本，它在字节上就等价于 CDK 会为该摘要产出的 rekey 版本，攻击者仍然拿不到写脚本的能力。真正的写入权归 CDK / `setup.sh`。（更强的来源绑定——给 LT 版本打 CDK 部署 tag 再校验——留作后续加固，不在本 scope。）

### 2.5 K1 — 不动存量实例

两台 ASG 都跟踪 LT 的 `$Default`，EC2 在**每次 launch 时**解析默认版本。所以：

- **promote = 翻默认版本指针**，下次开机的实例读新版本，**存量在跑的实例一律不动**，随自然替换（scale-in/out、AZ 故障转移、rebuild）接手新版本。
- 同理，Part A 的 host LT 用 `resolve:ssm:` 而不是 CDK synth 时 lookup，所以**新烤出的 AMI 在下次 scale-out 时自动生效，不需要 `cdk deploy`**，也同样不触碰存量实例。

想立刻全量换掉存量实例，需要你自己做一次滚动替换（本 scope 刻意不提供自动 instance refresh）。

### 2.6 promote 与 `cdk deploy` 的关系（重要）

**promote 是临时切换，`cdk deploy` 是事实源。**

`SetDefaultLTVersion` 自定义资源会在**bootstrap 内容发生变化的那次 `cdk deploy`**（LT 产生新版本 → CR 的 `physical_resource_id` 变 → CR 重跑）把默认版本重新设成 CDK 当次声明的版本，**覆盖之前的 API promote**。

**但**：一次**内容无变化的 no-op `cdk deploy`** 不会触发该 CR 更新，因此**不会**主动纠正 API 造成的默认版本漂移。

结论：要让某个版本**长期**成为默认，改源码走 `cdk deploy`；API promote 只用于两次**有内容变更的**部署之间做回滚或切换。若担心 drift，改一次源码内容再 deploy 即可强制归位。

## 3. 通用约定

### 3.1 鉴权与权限

两个接口都需要：

```http
x-api-key: <api-key>
```

| 操作 | 要求 |
|---|---|
| `GET /bootstrap/versions` | **admin** |
| `POST /bootstrap/promote` | **admin** |

授权门是 `_get_caller_identity(event).get("is_admin")`，判在 handler 内部（`deploy/lambda/api/handler.py:2023` 和 `:2037`），**不是**前置的 RBAC 角色门。

原因：api-key 路径在 `core/auth.py` 里 `role` 解析成 `viewer` 但 `is_admin=True`（受信自动化全权）。若走按 role 判定的前置 RBAC 门，持 key 的运维脚本会被 `viewer < operator` 挡在门外。所以这两条路由列入 `_RBAC_SKIP`，由 handler 内的 `is_admin` 独家把关。

> 给未来的读者：**别把这个 `is_admin` 当 bug 改成 `role == "admin"`** —— 那会锁死所有持 API key 的运维脚本。这个前提本身被 `tests/test_389_bootstrap_promote_authz_adversarial.py` 钉住了，一改就红。

被拒时（非 admin 的 JWT、伪造签名、`alg:none`、过期、错 issuer、垃圾 token）：

```http
403 Forbidden
```

```json
{"error": "bootstrap promote requires admin role", "code": "ACCESS_DENIED"}
```

注意错误信封的形状差异，排障时可用它区分是哪道门拒的：**handler 内的门**返回的 body **不含** `rbac` 键；前置 RBAC 门（作用于其他路由）返回的 body **含** `rbac: {role, required}`。

**本 scope 为在线 Lambda 新加的 EC2 权限只有两条**（`ha_edge.py:966` / `:977` / `:1766`）：

- `ec2:DescribeLaunchTemplateVersions` → `"*"`（该 action 不支持资源级权限，纯只读）
- `ec2:ModifyLaunchTemplate` → **资源级死锁**到具体的 host / edge LT ARN。这是 promote 的**唯一**写权限

promote 流程还会用到 api Lambda **早已具备的、本 scope 未新增**的权限：`autoscaling:DescribeAutoScalingGroups`（从 ASG 反查 LT id，`lambdas.py:795`）、assets bucket 的读权限（S3 字节复核，`lambdas.py:523`）、`hosts_table` 读写（fleet 锁是该表里一条 `__bootstrap_promote_lock__` 前缀的合成记录）、`audit_table` 读写（强制审计）。这些都是 api Lambda 服务其他路由本来就有的，promote 只是复用。

**刻意不授予** `ec2:CreateLaunchTemplateVersion`、`ec2:RunInstances`、`iam:PassRole`、`autoscaling:UpdateAutoScalingGroup`。给在线 Lambda 授 `CreateLaunchTemplateVersion` 等于给它写任意 user-data / AMI 的能力（继承实例角色 → 任意代码执行），即便代码本身只做 rekey 也无法从 IAM 层保证。synth 测试断言 api role 不含这四个 action。

### 3.2 幂等、CAS 与并发

**CAS（compare-and-swap）是必填的**：`promote` 的 `expected_current_sha` 没有默认值，缺了直接 `400`。它证明你是从**刚读到的那个版本**切走的，挡住"验证了 A 却切了 C"。

**幂等**：目标已经是当前默认时返回 `200` + `already_promoted: true`，带同参重试安全。**幂等判定排在 CAS 之前**（5.3 第 5 步）：机队已经是目标态时，即便 `expected_current_sha` 已过期也返回 `200`，因为终态已符合你的意图，而这条路径同时是 `503` 之后重试的对账落点，不能被过期的 CAS 挡住。

**fleet 级互斥**：整段 check-and-flip 在一把 fleet 级锁内串行（锁内重读 current 再判）。CAS 本身不是原子的——两个并发 promote 带同一 `expected`、不同 `target` 会都过校验、依次翻默认，后者赢而前者已经回了 `200`。锁把这条路堵死。撞锁返回 `409 PROMOTE_IN_PROGRESS`，稍后重试。

**`503 OPERATION_STATUS_UNKNOWN` 的正确处理**：这个码表示**默认版本状态未知**——`ModifyLaunchTemplate` 可能已在服务端生效但回执/回读失败。带同参**安全重试**：重试会重读默认版本对账，确认已生效则补一条 `SUCCEEDED` 审计（标 `reconciled`）再返回 `200`。也可以先 `GET /bootstrap/versions` 看 `current_sha` 自行确认。**不要**把它当作"没生效"。

### 3.3 强制审计（fail-closed）

每次 promote 往 audit 表写记录：`before → after` sha、actor（`owner_id` / `role` / `api_key_only`）、LT 版本号、状态。三个刻意的设计：

- **变更前先落 `INTENT`**：intent 写不下就**不动机队**。保证任何真实变更之前都已有痕迹。
- **`OPERATION_STATUS_UNKNOWN` 也审计**：状态未知不等于没发生。
- **审计写失败 → 返回 `503 AUDIT_UNAVAILABLE` 而不是 `200`**，即便机队变更已经生效。高权限机队变更不允许没有审计痕迹；重试走幂等对账路径补上审计再 `200`。所以「**每次 200 都对应一条已持久化的 SUCCEEDED 审计**」是个不变量。

审计记录保留 90 天（TTL）。

### 3.4 responses 与错误信封

应用层错误统一信封：

```json
{"error": "<人读的诊断信息>", "code": "<稳定的机读码>"}
```

客户端应先按 HTTP status 分类，再按稳定的 `code` 分支，`error` 只用于日志和诊断。请求未进入 Lambda 时（如无效 API key），API Gateway 直接返回不同信封：`{"message":"Forbidden"}`。

两个接口都是**同步**的：`200` 即代表该操作已完成。

## 4. Part A · Golden AMI 操作

### 4.1 config.yml 开关

```yaml
host:
  golden_ami:
    build_pipeline: false   # true=部署 Image Builder 流水线(component/recipe/distribution)
    use: false              # true=host LT 用 resolve:ssm 读 golden AMI;false=用 Canonical 公共 AMI
    recipe_version: "1.0.0" # 改这里=新版本 component+recipe(必须 x.y.z;同版本号重复部署会被 Image Builder 拒绝)
    ssm_parameter: ""       # 留空=用默认 /imagebuilder/openclaw/host-ami<region后缀>
    build_instance_type: "" # 留空=按 arch 选 c7g.large / c7i.large
```

`use` 的语义（`deploy/stacks/ha_edge.py:596`）：

- `use: true` → `ec2.MachineImage.resolve_ssm_parameter_at_launch(<param>)`
- `use: false` → `ec2.MachineImage.lookup(name="ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-{amd64|arm64}-server-*", owners=["099720109477"])`

用 `resolve:ssm` 而不是 lookup 是刻意的：EC2 在每次 launch 时解析，所以**新烤的 AMI 下次 scale-out 自动生效，不需要 `cdk deploy`**；同时改这个参数**不触碰**任何运行中的实例——这就是免费得到的 K1。

### 4.2 首次启用：顺序不能反

```bash
# 1. 先只开流水线,不开 use
#    config.yml: build_pipeline: true, use: false
cdk deploy OpenClawHostImage

# 2. 读 stack output 拿流水线 ARN
aws cloudformation describe-stacks --stack-name OpenClawHostImage \
  --query "Stacks[0].Outputs[?OutputKey=='HostImagePipelineArn'].OutputValue" --output text

# 3. 烤一次(约 15-30 分钟,异步)
aws imagebuilder start-image-pipeline-execution --image-pipeline-arn <ARN>

# 4. 等 AMI AVAILABLE,确认 SSM 参数里真的写进了 AMI id
aws ssm get-parameter --name /imagebuilder/openclaw/host-ami<region后缀> \
  --query Parameter.Value --output text

# 5. 参数里确认有 AMI id 之后,再开 use
#    config.yml: use: true
cdk deploy
```

**第 5 步不能提前**：`use: true` 但 SSM 参数里还没有 AMI id 时，ASG 的**每一次 launch 都会失败**。

### 4.3 烤制内含的两条强制断言

烤制包含 BUILD 和 TEST 两个 workflow，任一失败则**不产出 AMI**。component 的 `validate` 阶段
声明了两个断言 step（`deploy/stacks/host_image.py`）：

| 断言 step | 验什么 |
|---|---|
| `AssertZeroDownloadBootPath` | golden AMI 的开机路径**零外网下载**——这是 golden image 存在的理由，必须被证明而不是假设。断言 `aws`/`firecracker`/`jailer` 在 PATH 上，`/opt/fluent-bit/bin/fluent-bit`、`/opt/openclaw/baked/vmlinux`、`/etc/openclaw/.ami-provisioned` 非空，`aws-otel-collector` 已装；同时断言 `host_vm_key`/`host_vm_key.pub`/`platform.env` **不在**镜像里 |
| `AssertProvisionIsIdempotent` | 同一台机器上跑两遍 provision：marker 前后 sha256 一致，再复查 `firecracker`/`aws`/`vmlinux` 仍在 |

身份物的擦除**不是**一个独立 component step：它在 `provision-host.sh` 第 8 节里，只在
`OC_PROVISION_BAKE=1`（即烤制）时执行，并在结尾自查 `host_vm_key`/`platform.env` 确实不在，
不在就 `die` 拒绝出镜像（见 4.5）。`AssertZeroDownloadBootPath` 是这件事的第二道独立验证。

image-tests 还会用新 AMI **真起一次实例**。

### 4.4 往 golden AMI 加一个软件

安装步骤只有一个入口：`deploy/userdata/provision-host.sh`。

1. **编辑 `provision-host.sh`** 加安装步骤。硬要求：
   - **幂等**：再跑一遍是 no-op（golden host 会跳过 provision；plain AMI 会跑；re-bake 会跑两遍）。
   - **不写任何 per-host / per-deployment 值**——这会进全机队共享的 AMI。
   - 装到 **root 卷**，不要装到 `/data`（烤镜像时数据卷还不存在，且每台 host 会重新格式化它）。
   - 若新增外网下载，确认 URL 的 arch/version 布局正确（错 arch 会让 plain AMI 路径开机失败）。
2. **升 `recipe_version`**（必须 `x.y.z`；同版本号重复部署会被 Image Builder 拒绝）。
3. `cdk deploy OpenClawHostImage` —— 这只更新流水线定义，**不会自动烤**。
4. 触发烤制（4.2 第 3 步）。
5. 烤成功 → AMI id 写进 SSM → 下次 scale-out 的 host 自动用新镜像。

> **关于 component 大小**：AWSTOE component 的 `data` 字段上限是 **16000 字节**（2026-08-05 真机 CFN 部署实测报 `Model validation failed (#/Data: expected maxLength: 16000)`）。`provision-host.sh` + `install-fluent-bit.sh` 合计约 19KB 已经放不下，所以它们作为 CDK 资产上传、烤制时 S3Download 并 `sha256sum -c` 校验字节。component 文档本身远小于上限，但若往文档里塞大的内联块又踩线，`host_image.py` 会 fail-loud 报出来。
>
> 这**不**破坏 golden 启动路径的"零下载"承诺：那次下载发生在**烤制的一次性构建机**上，不在 host 的开机路径上。

### 4.5 为什么不能直接对生产机器打快照

`OC_PROVISION_BAKE=1` 模式下有一段 scrub，快照前擦掉所有 host-identifying 的内容。**AMI 是全机队共享的**，所以：

- 一把烤进 AMI 的 per-host ed25519 私钥，会变成**所有 host 共用一把**——而它的公钥被注入**每一个**租户 microVM。任何 host 的私钥就能 SSH 进任何 host 上任何租户的 VM。
- provision 侧**从不**创建这把密钥，bake 模式**拒绝**快照一把已存在的密钥；configure 侧另外验证磁盘上的密钥属于**本实例**。这是双向防御。

**直接对一台跑着的生产机器打快照会带上 token、密钥和个人配置**，绝不要这样做。

### 4.6 改 init-host / edge 脚本本体（configure 侧）

改 `deploy/userdata/init-host.sh` 或 `deploy/edge/` 下任意文件后，**走 `cdk deploy`**：CDK 重新渲染、算新 sha256、发布到新的不可变 key，并同步更新 LaunchTemplate user-data 里的摘要。

这类改动**不需要**重烤 AMI —— configure 侧不在镜像里。

## 5. Part B · Bootstrap 版本切换 API

### 5.1 `GET /bootstrap/versions`

列两个 fleet 各自**可切换**的版本并标出当前默认。这是 promote 前的**必要**一步（你需要 `current_sha` 做 CAS）。

**Request**

```http
GET /v1/bootstrap/versions
x-api-key: <api-key>
```

无 query 参数、无 body。

**Response `200`**

```json
{
  "fleets": {
    "host": {
      "asg": "openclaw-hosts-asg",
      "launch_template_id": "lt-0abc123def4567890",
      "current_sha": "a1b2c3...(64 hex)",
      "current_launch_template_version": 7,
      "available": [
        {"sha256": "d4e5f6...", "launch_template_version": 8, "is_current": false},
        {"sha256": "a1b2c3...", "launch_template_version": 7, "is_current": true}
      ]
    },
    "edge": {
      "asg": "openclaw-edge-asg",
      "launch_template_id": "lt-0fedcba9876543210",
      "current_sha": "…",
      "current_launch_template_version": 3,
      "available": [ … ]
    }
  }
}
```

`available` 按 `launch_template_version` **降序**（最新的在前）。

**逐 fleet 降级，不整体失败**：某个 fleet 读不到时，**只有那个 fleet** 的条目变成 `{"available": [], "current_sha": null, "error": {...}}`，另一个 fleet 照常返回。所以拿到 `200` 不等于两个 fleet 都健康——**必须检查每个 fleet 有没有 `error` 键**。

字段含义：

| 字段 | 含义 |
|---|---|
| `asg` | 该 fleet 的 Auto Scaling group 名 |
| `launch_template_id` | 从 ASG 反查出的、它**实际跟踪**的 LT |
| `current_sha` | 当前 `$Default` 版本的 user-data 里编入的 bootstrap 摘要 = **下次开机会读的那份脚本** |
| `current_launch_template_version` | 当前 `$Default` 的**真实**版本号 |
| `available[].sha256` | 该版本的 bootstrap 摘要，promote 时作为 `target_sha` |
| `available[].launch_template_version` | 对应的 LT 版本号（同 sha 有多个等价版本时这里是最高的那个） |
| `available[].is_current` | 是否就是当前默认 |

### 5.2 `POST /bootstrap/promote`

把一个 fleet 的默认版本切到 `available` 里的另一个版本。

**Request**

```http
POST /v1/bootstrap/promote
x-api-key: <api-key>
Content-Type: application/json
```

```json
{
  "fleet": "host",
  "target_sha": "d4e5f6...(64 hex)",
  "expected_current_sha": "a1b2c3...(64 hex)"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `fleet` | string | 是 | `"host"` 或 `"edge"` |
| `target_sha` | string | 是 | 64 位小写 hex sha256，必须在该 fleet 的 `available` 里 |
| `expected_current_sha` | string | 是 | **CAS**：你刚从 `GET /bootstrap/versions` 读到的 `current_sha`。无默认值，缺了 `400` |

**Response `200`（切换成功）**

```json
{
  "message": "fleet bootstrap version promoted",
  "fleet": "host",
  "already_promoted": false,
  "previous_sha": "a1b2c3...",
  "current_sha": "d4e5f6...",
  "launch_template_version": 8,
  "note": "existing running instances are unchanged (no instance refresh); new launches boot the promoted version",
  "promoted_at": "2026-08-05T13:24:01Z"
}
```

**Response `200`（幂等 no-op）**

```json
{
  "message": "fleet already boots the target version",
  "fleet": "host",
  "already_promoted": true,
  "current_sha": "d4e5f6...",
  "current_launch_template_version": 8
}
```

用 `already_promoted` 区分"这次真切了"和"本来就是它"。幂等路径也**写审计**（标 `reconciled`），因为它同时是 `503` 之后重试的对账落点。

### 5.3 一次 promote 的内部顺序

理解失败语义需要知道校验的顺序（`deploy/lambda/api/services/bootstrap_version_service.py`）：

0. **admin 门** —— 在 handler 里，`promote()` 之前（3.1）
1. **形参校验**（`ASSETS_BUCKET`/`fleet`/两个 sha 的类型与格式）→ 然后**取 fleet 锁**，整段 check-and-flip 串行到一台 fleet，锁内重读当前态
2. **枚举 LT 已发布版本**（分页），逐个从 user-data 解析 bootstrap 摘要；当前 = `$Default` 解析出的那个（读不出 → `409 CURRENT_UNPARSEABLE`）
3. **算可切换集**（2.4 的四个条件）；`target_sha` 不在集内 → `404 VERSION_NOT_FOUND`
4. **S3 GetObject 复核**目标版本对应的 bootstrap 对象在且字节自洽 —— 排在任何成功返回（含幂等 no-op）之前：对象没了就不能声称能切到它
5. **幂等 no-op 判定**：`current == target_sha` → 补一条 `SUCCEEDED`（`reconciled`）审计后返回 `200 already_promoted`。**注意这一步在 CAS 之前**：机队已经是目标态时，即便你的 `expected_current_sha` 是过期值也返回 `200`（终态已符合意图，且这是 `503` 重试的对账落点，不能被过期的 CAS 挡住）
6. **CAS**：`expected_current_sha == current`，不符 → `409 CAS_MISMATCH`
7. **变更前落 `INTENT` 审计** —— 落不下就不动机队
8. **`ModifyLaunchTemplate`** 翻默认版本（抛不抛异常都继续 read-back：它可能服务端已生效）
9. **read-back 校验**：重读 `$Default`，**sha 和版本号都必须**对上目标才算成功。只比 sha 会被"并发把另一个同 sha 但改了 AMI 的版本设成默认"骗过
10. **终态审计**；不确认则 `503 OPERATION_STATUS_UNKNOWN`，**不谎报成功**

### 5.4 典型调用序列（回滚到上一个版本）

```bash
API=https://<api-base>/v1
KEY=<api-key>

# 1. 读当前态,拿 current_sha 和目标 sha
curl -sS -H "x-api-key: $KEY" "$API/bootstrap/versions"

# 2. 用第 1 步读到的值切换(CAS)
curl -sS -X POST -H "x-api-key: $KEY" -H "Content-Type: application/json" \
  "$API/bootstrap/promote" \
  -d '{"fleet":"host","target_sha":"<目标>","expected_current_sha":"<第1步读到的 current>"}'

# 3. 从 AWS 侧独立确认默认版本真的翻了(不只信 API 回执)
aws ec2 describe-launch-template-versions \
  --launch-template-id <第1步返回的 launch_template_id> \
  --versions '$Default' \
  --query 'LaunchTemplateVersions[0].VersionNumber'
```

私有 API 要在能连到它的网络里跑（如 VPC 内的 bastion）。

## 6. 主要错误码

### 6.1 `POST /bootstrap/promote`

| HTTP | `code` | 含义 | 该怎么办 |
|---|---|---|---|
| 400 | `VALIDATION` | `fleet` 不是 `host`/`edge`；body 不是 JSON 对象；`target_sha`/`expected_current_sha` 不是字符串；`target_sha` 不是 64 位 hex；`expected_current_sha` 缺失 | 修请求 |
| 403 | `ACCESS_DENIED` | 调用方不是 admin | 用 admin 凭据 |
| 404 | `FLEET_NOT_DEPLOYED` | 该 fleet 的 ASG 不存在（这个环境没部署它） | 确认部署了 |
| 404 | `VERSION_NOT_FOUND` | 两种原因，看 `error` 文本区分：**(a)** `target_sha` 不在可切换集——它根本不是已发布的 LT 版本，或它相对当前默认改动了 UserData 之外的东西（AMI/role/网络/夹带命令）；**(b)** 版本在，但它对应的 S3 bootstrap 对象**已经不存在**（被 prune 掉了），不能把机队指向不存在的字节 | (a) `GET /bootstrap/versions` 看真正可切的集合；(b) 换目标版本或重新 `cdk deploy` |
| 409 | `CAS_MISMATCH` | 当前默认已不是你的 `expected_current_sha`（别人切过了） | 重新 GET 再试，**不要**盲目重发 |
| 409 | `PROMOTE_IN_PROGRESS` | 同 fleet 另一个 promote 正持锁 | 稍后重试 |
| 409 | `DIGEST_MISMATCH` | 目标版本的 S3 对象字节 sha 与摘要不符（桶被动过） | 查桶；不要绕过 |
| 409 | `CURRENT_UNPARSEABLE` | 读不出当前默认版本的 bootstrap 摘要，拒绝从未知状态切换 | 查该 LT 的 `$Default` user-data |
| 409 | `ASG_NOT_TRACKING_DEFAULT` | ASG 固定到某个数字版本或 `$Latest`，翻默认对它无效 | 让 ASG 跟踪 `$Default` |
| 409 | `ASG_OVERRIDES_LAUNCH_TEMPLATE` | MixedInstancesPolicy 里有 override 自带 `LaunchTemplateSpecification`，翻基础 LT 默认对那部分实例无效 | 去掉 override 或改用 `cdk deploy` |
| 503 | `NOT_CONFIGURED` | `ASSETS_BUCKET` 未配置 | 查部署 |
| 503 | `DEPENDENCY_UNAVAILABLE` | describe / S3 读抖动 | **安全重试** |
| 503 | `AUDIT_UNAVAILABLE` | 审计写不下。**变更可能已生效**（见 3.3） | 带同参重试，走幂等对账补审计 |
| 503 | `OPERATION_STATUS_UNKNOWN` | 翻默认或 read-back 没确认。**默认版本状态未知** | 带同参**安全重试**（幂等对账）；或先 GET 确认 |

`409 ASG_NOT_TRACKING_DEFAULT` / `ASG_OVERRIDES_LAUNCH_TEMPLATE` 这两条是刻意的 fail-closed：那两种配置下翻默认版本**不会**对全部新实例生效，返回 `200` 就是 false-green。

### 6.2 `GET /bootstrap/versions`

顶层永远 `200`（除 `403`）。**per-fleet 的失败在 `fleets.<fleet>.error` 里**，可能的 code 是 `FLEET_NOT_DEPLOYED`、`ASG_OVERRIDES_LAUNCH_TEMPLATE`、`LT_NOT_RESOLVABLE`、`DEPENDENCY_UNAVAILABLE`。客户端**必须**检查每个 fleet 有没有 `error`，不能只看 HTTP status。

## 7. 排障

| 现象 | 先看哪里 |
|---|---|
| host 开机就失败、ASG 反复替换 | 是不是 `golden_ami.use: true` 但 SSM 参数里没有 AMI id（见 4.2 顺序） |
| 不确定某台 host 是不是 golden 起的 | `cat /etc/openclaw/.ami-provisioned`（不存在 = 走的 plain AMI 路径） |
| 改了 `deploy/edge/` 但没生效 | edge 整棵树是一个 bundle + 一个 sha256，改 S3 不再即时生效，要 `cdk deploy`（见 2.3） |
| promote 回了 200 但新实例还是旧版本 | 用 `describe-launch-template-versions --versions '$Default'` 独立确认；再查 ASG 是否真跟踪 `$Default`（否则本该是 `409`） |
| promote 回了 `503 UNKNOWN` | 先 `GET /bootstrap/versions` 看 `current_sha`；带同参重试走幂等对账（见 3.2） |
| `cdk deploy` 之后 promote 的版本没了 | 预期行为：有内容变更的 deploy 会把 `$Default` 重置回 CDK 声明版本（见 2.6） |
| 想查谁在什么时候切了版本 | audit 表，`operation = "bootstrap-promote <fleet>"`，记 `detail_old_sha` → `detail_new_sha` + actor（保留 90 天） |

## 8. 相关文档与测试

- 运维手册（端到端设计图、加软件/烤镜像/切版本三个手册、真机验收清单）：`engineering/runbooks/HOST-EDGE-BOOTSTRAP-389.md`
- 单测与对抗测试：`tests/test_389_bootstrap_version_api.py`、`tests/test_389_bootstrap_promote_authz_adversarial.py`、`tests/test_389_edge_bundle_bootstrap.py`、`tests/test_389_lt_bootstrap_dual_form.py`、`tests/test_389_golden_ami_pipeline.py`、`tests/test_389_provision_configure_split.py`、`tests/test_389_provision_synth_binding.py`
- API 回归：`tests/api-regress/checks-admin.sh` §2.12–§2.13
