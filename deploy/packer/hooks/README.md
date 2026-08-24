# deploy/packer/hooks — 客户自定义构建步骤

把你自己的脚本放进本目录，构建时会**按文件名字典序**逐个执行。不需要改
`host-golden.pkr.hcl`、不需要改 `provision-host.sh`、不需要 fork 仓库。

改动主流程文件时构建会被前置门直接拒绝（`build-golden-ami.sh` 的 V-1 门），所以本目录
是唯一的扩展点。

## 用法

```bash
cp deploy/packer/hooks/50-example.sh.example deploy/packer/hooks/50-install-agent.sh
chmod +x deploy/packer/hooks/50-install-agent.sh
# 按需编辑，然后正常构建
bash deploy/packer/build-golden-ami.sh --var-file "$PWD/deploy/packer/my.pkrvars.hcl"
```

`hooks/*.sh` 在 `.gitignore` 中 —— 它们是你的私有内容，不进我们的版本控制。本目录的
`README.md` 与 `*.sh.example` 入库。

### 命名与顺序

用两位数字前缀控制顺序，中间留空档便于以后插入：

```
10-corporate-ca.sh      # 先装企业根证书，后面的 apt/curl 才能走内网镜像
50-security-agent.sh
80-sysctl-tuning.sh
```

排序是 `LC_ALL=C` 字典序（纯 ASCII 文件名下就是逐字节比较），所以 `10-` 在 `9-` **之前**
—— 要么全部补零到同宽，要么别混用宽度。执行顺序会打进构建日志和镜像里的
`hooks-manifest`，构建后可以核对。

### 参数从哪里来

不要把参数硬编码进脚本。在 pkrvars 里声明，它们会作为环境变量传进每个 hook：

```hcl
hook_env = {
  COMPANY_APT_MIRROR = "https://apt.example.internal"
  AGENT_ENDPOINT     = "https://collector.example.internal:4318"
}
```

**不要经由 `hook_env` 传密钥** —— 值会出现在构建日志里。运行期需要的密钥应由 host 的
instance profile 在启动时从 Secrets Manager 或 SSM Parameter Store（SecureString）取。

## 执行时机

```
1. 上传 provision-host.sh / install-fluent-bit.sh / assert-image.sh
2. 执行 provision-host.sh（装组件 → scrub 清除实例身份 → 写 marker）
3. 【本目录的 hook 按字典序执行】   ← 此处
4. 组件零安装断言
5. 幂等断言（重跑 provision + 指纹逐项比对）
6. 生成 AMI
```

这个位置由三项约束夹死，不可调整：

- **在 provision 之后** —— firecracker、jailer、awscli、ADOT collector、Fluent Bit 均已
  装好，hook 可以依赖它们。
- **在 scrub 之后** —— scrub 是 `provision-host.sh` 内部的第 7 节，在 hook 之前就跑完了。
  因此 **hook 写入的任何内容都不会再被清理**，会原样进镜像并被整个机队共享。
- **在断言之前** —— 两轮断言是 hook 的唯一防线。若挪到断言之后，hook 引入的身份泄漏
  将无人检出，而构建仍然显示成功。

## 允许做什么

只做**与具体机器无关**的事：

| 类别 | 例子 |
|---|---|
| 装软件包 | 安全 agent、合规采集器、监控 exporter |
| 写静态配置 | 配置模板、CA 证书、systemd unit、sysctl 参数 |
| 预置静态资产 | 容器镜像、二进制、字体、语言包 |
| 建目录与系统用户 | 不含实例身份信息的用户与目录结构 |

## 禁止做什么

以下操作会让产物变成**机队级共享状态**。断言会检出并让**构建失败、不产出 AMI**：

| 禁止项 | 后果 |
|---|---|
| 生成主机密钥或 SSH host key | 整个机队共享同一把，任何能创建实例者可冒充其余实例 |
| 写 `/etc/platform.env` 或其他 per-host 配置 | 所有实例读到同一份实例专属配置 |
| 硬编码密码、API key、token | 镜像可被任何有权创建实例者读取 |
| 触发 cloud-init 重新初始化 | 新实例复用旧 instance-id 判定，首启逻辑被跳过 |
| 启动会在构建阶段注册到外部系统的服务 | 那条注册记录被整个机队复用 |

## 写 hook 的硬性要求

前置门（`build-golden-ami.sh` 的 V-1）会逐条检查，不满足就拒绝构建：

1. **必须有执行位**（`chmod +x`）。位丢了在构建中途才炸，反馈太晚。
2. **必须 `bash -n` 通过**。语法错误不该等到已经起了构建实例、跑完 20 分钟 provision
   之后才发现。
3. **首行应为 `set -euo pipefail`**。hook 以 `bash` 执行，任一命令失败即中止构建 ——
   避免产出组件不完整的镜像。
4. **禁止交互式提示**。构建阶段没有终端，任何等待输入的操作都会阻塞到 Packer 超时。
   `apt` 要设 `DEBIAN_FRONTEND=noninteractive`；涉及覆盖确认的命令显式传 `--yes`。

## 超时

全部 hook 合计上限 **30 分钟**。装大体积软件包可能触及该上限，此时应把安装内容改为从
自有 S3 获取而不是从公网下载。

## 构建后怎么核对

hook 目录整体、以及一份执行记录都留在镜像里：

```bash
ls -l /opt/openclaw/custom/hooks/          # 实际上传执行的那批脚本
cat   /opt/openclaw/custom/hooks-manifest  # 执行顺序 + 每个 hook 的 sha256
```

AMI 上还有一个 `HooksSha` tag，是这批 hook 的集合摘要。它由构建侧独立计算，与镜像里的
`hooks-manifest` 对账 —— 两侧独立算才能发现「上传的和执行的不是同一批」。晋级到下一级
环境时，`HooksSha` 必须与上一级相等，否则「测试环境验过的」和「生产要上的」装的东西
就不是一回事（#537 V-6）。
