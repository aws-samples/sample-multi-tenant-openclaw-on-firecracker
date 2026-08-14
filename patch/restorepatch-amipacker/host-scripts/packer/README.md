# deploy/packer — 使用 Packer 构建 host golden AMI

Packer 版 host golden AMI 构建模板，与 `deploy/stacks/host_image.py`（EC2 Image
Builder）产出**同一份镜像内容**。两套实现并存，不是二选一。

## ⚠️ 前置依赖：#435 尚未合并

本模板的 S3 相关能力**依赖 #435（Firecracker 二进制迁自家 S3）落地后才生效**。在
#435 合并前的 `gitlab/bb` 基线上：

| 本模板提供的 | 在当前基线上的实际行为 |
|---|---|
| `assets_bucket` 参数 | 传入但不被读取 —— `provision-host.sh` 尚无 `OC_ASSETS_BUCKET` |
| `OC_ASSETS_BUCKET` 环境变量 | 同上，`provision-host.sh` 引用次数为 0 |
| `iam_instance_profile` 的 `s3:GetObject` 要求 | 不必需（无 S3 拉取） |
| `CUSTOMER-GUIDE.md` §3 预置 Firecracker 二进制 | 该步骤当前无效，可跳过 |
| 「init 日志 `grep -c github.com` 为 0」这条验收 | **不成立** —— 当前仍从 GitHub 拉 |

模板保留这些接口是因为它们与 #435 的实现同源（同一个 `provision-host.sh`），#435
合并后无需改动本目录即自动生效。**在 #435 合并前不要把 `CUSTOMER-GUIDE.md` 交付给
客户** —— §3 与 §1.8 的 S3 权限会让客户配置一批当前不起作用的东西，而 §6 之后的
「零第三方依赖」承诺在那时并不成立。

下方「实测记录」中的真机证据取自带 #435 改动的工作树，**不是本分支的提交状态**。

## 为什么复用 provision-host.sh，而不在 HCL 中重写安装步骤

`host-golden.pkr.hcl` 的 provisioner 直接调用仓库内的
`deploy/userdata/provision-host.sh`（传入 `OC_PROVISION_BAKE=1`），因此 HCL 中不体现
8 节安装细节。这是有意的取舍：

双轨维护必然产生漂移。以下修复均位于该脚本内，复用即自动生效，重写则需手工同步两处：

| issue | 变更内容 | 若在 HCL 中重写的后果 |
|---|---|---|
| #440 | `gpg --batch --yes` 使 keyring 覆盖操作幂等 | HCL 中遗漏 `--yes` → 构建阶段弹出确认提示并阻塞 |
| #451 | marker 移至 §7 scrub **之后** | 顺序写反 → scrub 失败的镜像仍标记为构建完成 |
| #435 | Firecracker 二进制改为从自有 S3 获取 | HCL 中仍指向 GitHub → 违反"零 github 请求"验收 |

一致性优先于可读性。如需了解安装内容，阅读 `provision-host.sh` 的 8 节小节标题。

## 两套工具的能力差异

Image Builder 原生提供两项能力，Packer 需自行实现（均已在 HCL 中显式实现）：

| 能力 | Image Builder | Packer 侧实现 |
|---|---|---|
| 将 AMI id 写入 SSM 参数 | `CfnDistributionConfiguration.ssm_parameter_configurations` | `post-processor "shell-local"` 调用 `aws ssm put-parameter` |
| AMI 产出后自动启动验证 | `image_tests_enabled=True` | ❌ **未实现** —— 见下方「未覆盖项」 |
| validate 阶段断言 | component 的 `validate` phase | 两个 `provisioner "shell"`（零下载检查 + 幂等检查） |
| CFN 依赖顺序 | recipe 与 component 均为 CFN 资源，与 ASG 处于同一依赖图 | ❌ Packer 位于 CFN 之外，依赖流程保证执行顺序 |

Packer 相对提供的能力：本地可执行（无需 AWS 编排即可 `packer build`）、HCL 可读性优于
AWSTOE YAML、`packer validate` 可在提交前校验配置。

## 使用方式

```bash
packer init deploy/packer
packer fmt deploy/packer

# validate 阶段会实际读取 SSM 解析 parent AMI，因此同时验证了
# 执行者具备 ssm:GetParameter 权限且 Canonical 的指针路径存在
packer validate -var-file=deploy/packer/apse1.pkrvars.hcl deploy/packer

# 执行构建（实测约 14 分钟，创建一台一次性 c7g.large）
packer build -var-file=deploy/packer/apse1.pkrvars.hcl deploy/packer
```

### 生产构建的两个必填参数

`apse1.pkrvars.hcl` 中以下两项留空仅适用于 `packer validate`：

- **`iam_instance_profile`** — 构建实例需读取 `assets_bucket` 的
  `deployment/binaries/firecracker/` 前缀（#435）。未指定角色时
  `provision-host.sh` 无法获取制品，构建将在第 3 步失败（脚本有意未实现公网回落）。
- **`ssm_parameter`** — 未指定时仅产出 AMI 而不发布指针，`ha_edge.py:661` 的
  `resolve_ssm_parameter_at_launch` 无法读取到新镜像。首次构建建议留空，确认镜像无
  问题后再发布。

## 一致性防线

```bash
deploy/packer/assert-parity.sh
```

不比对 AMI 块设备（其内容会因时间戳与日志而不同），而比对**决定镜像内容的输入**：
两侧执行同一份脚本、`recipe_version` 取值一致、EBS/IMDS/parent-AMI 参数逐项对应、
Packer 已复刻 Image Builder 的 validate 断言、SSM 分发已实现、pipefail 内联块均声明
bash shebang、AMI 字段为纯 ASCII。共 26 项检查，任一项漂移则退出码 1。

任一侧变更后均需执行。该脚本检查的正是双轨维护最易失效的位置。

## 客户侧的前置工具只有三项

`CUSTOMER-GUIDE.md` §1.1 承诺客户只需 Packer、AWS CLI v2、packer-plugin-amazon 三项
（`session-manager-plugin` 仅在选用该连接方式时追加）。这个承诺约束了模板的实现：

SSM 分发那步原先用 `python3 -c` 解析 `manifest.json` 取 AMI id，现改为 `grep`+`sed`
——要取的只是 `"region:ami-xxxx"` 里的后半段，用不着 JSON 解析器，而多一个解释器就多
一条客户前置依赖。提取失败时显式 `exit 1`：空字符串写进 SSM 会让 `ha_edge` 的
`resolve:ssm` 解析到空值，ASG 起不来且原因难查。四种坏输入（空 builds、非 ami 值、
非 JSON、文件缺失）均已验证拒绝发布。

**在模板里新增步骤时不要引入 jq、python、yq 等工具** —— 那会静默作废该承诺，而客户
只会在自己环境上构建失败时才发现。`assert-parity.sh` 与 `build-guide-html.sh` 用
python3 是可以的：它们是开发侧脚本，不在客户执行路径上。

## 客户自定义扩展

客户可通过 `custom_script` 变量注入自建构建步骤，无需修改本目录任何文件：

```hcl
custom_script     = "customize.sh"
custom_script_env = { COMPANY_APT_MIRROR = "https://apt.example.internal" }
```

`customize.sh.example` 是客户模板，`customize.sh.default` 是未配置时执行的无操作脚本
（用无操作默认值而非条件分支：packer 的 provisioner 不支持 dynamic block，
`only`/`except` 只能筛选 source，而 file provisioner 拿到空路径会直接报错）。

自定义脚本的执行位置被三项约束夹死在 provision 之后、validate 断言之前：scrub 属于
`provision-host.sh` 内部步骤，在自定义脚本执行前已完成，因此自定义脚本写入的内容
**不会再被清理**，而两条 validate 断言是唯一防线。若把该阶段挪到断言之后，客户脚本
引入的身份泄漏将无人检出且**构建仍显示成功** —— `assert-parity.sh` 第 9 项按行号
校验这一顺序，正是为了拦住这类无声回归。

开放该扩展点同时补了一条断言：客户脚本可能重装 `openssh-server` 或执行
`dpkg-reconfigure`，两者都会重新生成 SSH host key。scrub 会删除它们，但此前没有断言
兜住。整个机队共享一把 host key 意味着任何能创建实例者都可冒充其余实例。

`customize.sh.default` 与 `customize.sh.example` 的后缀不是 `.sh`，而
`scripts/checks/shell.sh` 只收 `*.sh` —— 这两个文件因此永久逃过仓库的 shell 门，
而 `customize.sh.example` 恰好是客户照着改、写法会被复制进生产构建的那一份。
`assert-parity.sh` 第 11 项补上了它们的 `bash -n` 与 shellcheck（`-S info`，
实测 `-S warning` 门槛过松，连未加引号的外部变量展开 SC2086 都放过）。

## 交付客户的 HTML 手册

```bash
deploy/packer/build-guide-html.sh [输出路径]     # 默认 deploy/packer/dist/
```

产出单文件 HTML：样式全部内联、无外部资源引用、含目录与打印样式，离线可读。
产物在 `.gitignore` 中 —— HTML 是 md 的派生物，两份都入库必然出现「改了 md 忘了
重新生成」的漂移，交付时现场生成即可。

## 实测记录（2026-08-12，ap-southeast-1）

本模板已完成真机构建验证，双架构各一次：

| 架构 | AMI | 构建耗时 | 备注 |
|---|---|---|---|
| arm64 | `openclaw-host-1.0.0-arm64-*` | 13 分 08 秒 | 构建实例 `c7g.large` |
| amd64 | `openclaw-host-1.0.0-amd64-*` | 约 14 分钟 | 构建实例 `c7i.large` |

两次构建的 `provision_sha256` 一致，证明两个架构执行的是同一份脚本。arm64 产出的
AMI 已用于启动一台 `m7g.metal`，init 日志确认 `step2: AMI pre-provisioned ... —
skipping component install`，且 `host_vm_key` 生成于启动时刻而非构建时刻（scrub 有效，
不存在机队级共享私钥）。

构建过程中暴露并修复了两个缺陷，`packer validate` 均无法检出：

1. **内联 shell 的默认 shebang 为 `/bin/sh -e`**，而 Ubuntu 的 `/bin/sh` 是 dash，
   不支持 `set -o pipefail` → provisioner 立即退出，两条 validate 断言完全未执行。
   修复：所有含 pipefail 的内联块显式声明 `inline_shebang = "/usr/bin/env bash"`。
2. **`ami_description` 含非 ASCII 字符**（em-dash）→ `ModifyImageAttribute` 返回
   400 `Character sets beyond ASCII are not supported`。该调用发生在 AMI 生成之后，
   故表现为"AMI 已存在但构建失败、manifest 与 SSM 分发均未执行"。修复：改用 ASCII
   连字符。

3. **断言体内 `ls glob` 在 `set -e` 下使整条断言静默失效** —— 新增 SSH host key 检查时
   写成 `_sshkeys="$(ls /etc/ssh/ssh_host_* 2>/dev/null | ...)"`，glob 无匹配时 `ls`
   返回非零，`2>/dev/null` 只压制 stderr 不改退出码，叠加 `pipefail` 与 `set -e` 后
   **断言脚本在该行直接退出**，其后所有检查均未执行。packer 仅报
   `Script exited with non-zero exit status: 2`，无法看出是断言自身失败。修复：改用
   `find`（无匹配时返回 0 且输出为空）。已复核断言体内其余命令替换均无此风险。

三项均已加入 `assert-parity.sh` 作为静态检查（当前共 26 项）。

## 两阶段断言：存在性 vs 不变性

`assert-image.sh` 在两个时机各跑一次，但**判据不同** —— 这是独立评审的核心 finding
带来的设计：

| 阶段 | 判据 |
|---|---|
| `post-provision` | 存在性（组件齐、身份已擦净），并把关键项指纹存档到 `/opt/openclaw/.image-fingerprint` |
| `post-rerun` | 存在性 + **不变性**（与存档逐行 diff） |

为什么必须比不变性：存在性检查通不过幂等这道题。重跑把 Fluent Bit 卸了重装、把 ADOT
换个版本、把 baked vmlinux 替换掉，文件仍然"存在"，断言照样绿 —— 而幂等的定义是
**最终状态不变**，不是"东西还在"。

指纹用内容摘要而非"有没有执行动作"：重装同一个 deb 产出相同字节，那本身就是幂等的，
不该报警；真换了版本或重新编译，摘要会变，必须报警。刻意排除 marker 的
`provisioned_at`（时间戳每次 provision 合法地会变），其余 marker 字段全纳入。

`post-rerun` 阶段找不到存档时 **fail loud** 而不是跳过比对 —— 静默跳过等于悄悄丢掉
整条幂等断言。

## 断言逻辑有可执行测试，不只是静态门

`tests/test_477_assert_image_behavior.py`（31 项）真正**执行** `assert-image.sh`，
喂它一个假的根文件系统（`dpkg`/`systemctl` 用 stub，`aws` 按真机布局造完整 symlink
链 `PATH aws → <root>/bin/aws → ../dist/aws`，`dist` 下除入口外还有 `.so` 与数据
文件）。这两处细节都是承重的：放普通文件会让解引用逻辑没被执行到，`dist` 里只有入口
则"非入口文件变更"的用例无从构造。`assert-parity.sh` 是 grep 门 —— 它证明"某个字符串
在文件里"，不证明"这段逻辑真会失败"；两者合起来才是完整覆盖。

覆盖含 10 个参数化的非幂等变更（Fluent Bit 重装、guest kernel 替换、firecracker 换版、
ADOT 升级、SSM unit 移除、marker recipe 变更、aws 升级、aws 入口同版本换字节、
**bundle 里的 `.so` 或数据文件变更而入口与版本串都不变**），每个都必须报
`NOT IDEMPOTENT` 并打印漂移项；外加只有时间戳变时必须仍过、失败聚合、顺序守卫、
全部五条跨租户泄漏路径各一个用例，以及 `ls <glob>` 提前退出那个 bug 的回归。

验红是分机制独立做的，不是笼统跑一遍：

| 抽掉什么 | 转红 |
|---|---|
| `post-rerun` 的 diff（恒判 unchanged） | 10 项非幂等用例全红 |
| `sha256tree.aws` 退化为只摘入口 | 两个非入口 bundle 用例（`.so` 与数据文件） |
| `sha256tree.aws` 整条移除 | aws 入口同版本换字节用例 + 指纹内容断言 |
| 任一条泄漏路径检查 | 该路径对应的那一个用例 |

`aws` 的两个指纹信号分工不同：`sha256tree.aws` 是判非幂等的那一个 —— 摘整棵 bundle 树，
因为 aws v2 是 PyInstaller bundle，入口只是 `dist` 下的一个文件，只摘入口时改动任一
非入口文件仍是假绿。成本实测（awscli 2.34.37，173 MB / 9351 文件）完整树摘要 **2.7 秒**。
布局不认识时退回只摘入口并把值标成 `ENTRYONLY:`，不静默降级。`version.aws` 对"抓到
变化"是冗余的，留着是为了可诊断 —— diff 直接读出 `2.15.0 → 2.99.0`，纯哈希只能说"变了"。

## 未覆盖项（明确声明）

1. **AMI 产出后的自动启动测试** — Image Builder 的 `image_tests_enabled=True` 会在
   AMI 产出后自动创建实例验证可启动，Packer 无内置对应能力。如需补齐须自行实现
   （创建实例 → 等待 `.ami-provisioned` → 终止实例）。当前依赖人工使用该 AMI 创建
   一台 host 进行验证。
2. **CFN 依赖顺序** — Image Builder 的 recipe 与 component 是 CFN 资源，与 host ASG
   处于同一依赖图；Packer 产出的 AMI 通过 SSM 参数解耦，**不具备**"AMI 就绪前 ASG
   不得启动"的保证。切换至 Packer 后该顺序需由流程保证。
3. **Image Builder 侧从未执行过** — `golden_ami.build_pipeline` 与 `use` 当前均为
   `false`，Image Builder 流水线本身未部署（实测无 Self 组件）。因此两套工具的
   **产出物比对尚未进行**，`assert-parity.sh` 校验的是输入一致性而非产出一致性。
4. **跨 region 复制** — Image Builder 的 distribution 支持多 region 分发；本模板仅
   发布至 `var.region` 单个区域。

## 替换 Image Builder 的前提条件

暂不建议替换。原因：Image Builder 流水线**从未执行过**，用已验证的新方案替换未验证的
旧方案，无法确认两者产出等价。建议顺序：

1. 先执行 Image Builder（设 `build_pipeline: true`，构建一次以取得基准耗时与 AMI）
2. 与本模板产出的 AMI 进行组件清单对账，并执行 `assert-parity.sh`
3. 补齐上述「未覆盖项」第 1、2 条
4. 并行运行一段时间确认无回归后再切换

替换工具的正当理由应是出现明确痛点（构建耗时过长、AWSTOE 无法表达某个步骤、需支持
跨云），而非表达形式的偏好。
