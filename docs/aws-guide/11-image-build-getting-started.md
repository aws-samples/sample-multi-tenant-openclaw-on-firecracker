# 镜像构建快速上手（Getting Started）

本节面向**第一次给这个项目构建镜像的人**：假设你会用 Linux 命令行、有一个 AWS 账号，但没接触过 Firecracker、也是头一回打这个 OpenClaw 镜像。跟着走，你会在一台 Linux 机器上，把一个带身份和技能的 OpenClaw agent 打成镜像、传到 Amazon S3。

一句话理解要做的事：拿一个 Ubuntu 根文件系统，往里装上 OpenClaw 和一套预设的身份与技能，封成一个只读磁盘镜像，传到 S3。之后平台起 microVM 时，每台都用这个镜像开机，所以每个租户拿到的 agent 身份、技能、安全护栏都一模一样。

> **这份文档只讲"构建镜像"这一件事**，不涉及部署整个平台控制面（那是另一条线，走 `./setup.sh` + `cdk deploy`，本节完全用不到）。构建镜像不需要先部署任何东西。

## 先认识几个词（够用即可，不用深究）

- **Firecracker / microVM**：AWS 开源的轻量虚拟机技术。平台给每个租户起一台独立的小虚拟机（microVM），彼此隔离。你构建的镜像就是这些 microVM 的开机盘。
- **golden image（黄金镜像）**：一个预装好一切、开机即用的标准镜像。所有租户共用同一个，保证行为一致、可审计。就是本节要产出的东西。
- **rootfs**：root filesystem，根文件系统，就是虚拟机里 `/` 下的全部内容。构建的核心产物。
- **debootstrap**：Linux 上一个命令，从零拉一套干净的 Ubuntu 根文件系统到指定目录。构建脚本用它打底，它**只能在 Linux 上跑**。
- **persona**：agent 的身份与行为设定（性格、名字、能干什么、资金操作要不要二次确认），一组 Markdown 文件。换 persona = 换 agent 的"人设"。

> **为什么基座用 Ubuntu**：Firecracker 官方自己的快速上手就用 Ubuntu —— 官方在 `c5.metal` + Ubuntu 24.04 上跑，示例 rootfs 也是 Ubuntu（见官方文档 [firecracker getting-started.md](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md)）。本项目跟官方一致：golden image 基座是 **Ubuntu 24.04（debootstrap `noble`）**。Firecracker 不挑 rootfs 发行版（换 Amazon Linux 2023 理论可行），但换基座不是改个参数——见文末"关于换 Amazon Linux 2023 基座"。

## 开始之前

准备三样东西：

**1）一台 Linux 机器。** 最直接的路径（下面"最快路径"）用 `debootstrap`，只能在 Linux 上跑。Ubuntu 22.04/24.04 最省事。手头只有 mac/Windows 也没关系，见后面"我没有 Linux 机器怎么办"。机器要求：**内存 ≥ 2GB（建议 4GB，否则装 OpenClaw 时可能被系统 OOM 杀掉）、`/tmp` 剩余空间 ≥ 10GB**（镜像和压缩临时文件都写在这）。

**2）拿到项目代码。** 克隆本仓库并进目录（用你拿到的仓库地址）：

```bash
git clone <本仓库地址> openclaw-firecracker
cd openclaw-firecracker
```

样本 `samples/finance-agent/`（一套现成的身份 + 技能 + 护栏）**仓库自带，不用自己造**，构建默认就用它。

**3）AWS CLI + 能写目标 S3 桶的凭据。** 装 AWS CLI v2 并配好凭据：

```bash
aws configure                 # 或 aws configure --profile <名字> 配一个具名 profile
aws sts get-caller-identity   # 确认凭据能用、账号对
```

构建产物要传到一个 S3 桶。先建一个（名字自取，须全局唯一）：

```bash
aws s3 mb s3://my-openclaw-images --region us-east-1
```

## OpenClaw 是怎么被打进镜像的

构建脚本 `build-rootfs.sh` 从头到尾就做这几步（看懂这条主线，其余都是细节）：

1. `debootstrap` 拉一套干净的 Ubuntu 根文件系统到 `/tmp` 下的目录。
2. 在这个根文件系统里（chroot 进去）装 Node.js 22。
3. **`npm install -g openclaw@<版本>`** —— 这就是把 OpenClaw 本体装进镜像的那一步（脚本里版本钉在 `2026.5.6`，见 `build-rootfs.sh` 的 `OPENCLAW_PIN` 变量，在 `[7/8]` 那步附近，要换版本改这里）。
4. `openclaw onboard` 生成初始配置。
5. **把样本的身份和技能烤进去**：`samples/<name>/persona/*` 复制进 `/home/agent/.openclaw/workspace/`，`samples/<name>/skills/*` 复制进 `/home/agent/.openclaw/skills/`，安全护栏插件（`sentinel-guard`/`acl-guard`）一并启用。
6. 做安全加固：根盘和身份盘设为只读、关掉 guest 串口、装审计与文件完整性基线、烤进默认 seccomp 过滤器。
7. 把做好的根文件系统封成磁盘镜像、压缩、传到 S3，并写一份 `manifest.json` 版本指针。

`npm install openclaw` 峰值吃约 1GB 内存，这就是前面要求机器 ≥2GB 的原因。

## 最快路径：在 Linux 上本地构建

有一台 Linux 机器，这是最省事的一条，一条命令出镜像。

先装构建依赖（Ubuntu 上）：

```bash
sudo apt-get update
sudo apt-get install -y debootstrap e2fsprogs pigz curl awscli
```

脚本要读一个 `.env.deploy` 文件拿目标 S3 桶名和区域。**不用跑 `setup.sh`，手写一份即可**（这三行就够构建用）：

```bash
cat > .env.deploy <<'EOF'
ASSETS_BUCKET=my-openclaw-images
REGION=us-east-1
PROFILE=
EOF
```

（`PROFILE` 留空表示用默认凭据；配了具名 profile 就填 profile 名。）

还要一份 OpenClaw 运行时配置，从样例复制一份即可：

```bash
cp templates/openclaw.json.example templates/openclaw.json
```

然后构建。默认烤主机同架构，跨架构用 `ARCH=` 指定：

```bash
# 版本号自取（会成为产物文件名的一部分，也是 S3 里区分版本的标识）
sudo ARCH=arm64  ./build-rootfs.sh v1.0     # arm64 (Graviton) 镜像
# 或
sudo ARCH=x86_64 ./build-rootfs.sh v1.0     # x86_64 (Intel/AMD) 镜像
```

跑约 5–10 分钟，看到 `✓ rootfs v1.0 uploaded` 就成了。产物在 `s3://my-openclaw-images/deployment/rootfs/`。

> **在 mac 上直接跑会被挡**：`build-rootfs.sh` 一开头就检查操作系统，非 Linux 会报错退出并提示改用 EC2 构建机（见下）。这是有意的，因为 `debootstrap` 只能在 Linux 上跑。

## 换成你自己的 agent（改身份与 persona）

想要自己品牌的 agent，不用从零写，复制样本改内容即可：

```bash
cp -a samples/finance-agent samples/my-brand
```

然后编辑 `samples/my-brand/` 下的文件：

- `persona/IDENTITY.md` —— agent 的名字、头像、定位。
- `persona/SOUL.md` —— 性格基线、语气、核心价值。
- `persona/AGENTS.md` —— 行为模式、资金类操作的二次确认门。
- `skills/` —— 增删技能，每个技能是一个 `skills/<名字>/SKILL.md`。
- `persona/TOOLS.md` —— 工具清单，注意**不要往里写任何凭据**（凭据由平台在运行时注入）。

改完，构建时用 `SAMPLE` 指定你的样本目录名：

```bash
sudo SAMPLE=my-brand ARCH=arm64 ./build-rootfs.sh v1.0
```

改身份只能重新烤镜像，不能改运行中的 microVM —— 这是刻意的安全设计（身份烤在只读盘里，租户改不了）。

## 构建产出什么

一次构建产出三个压缩磁盘镜像 + 一份清单，都传到 `s3://<桶>/deployment/rootfs/`：

- **rootfs**（`openclaw-rootfs-<版本>.ext4.gz`）：根文件系统，含 Ubuntu、Node.js、OpenClaw 和全部技能，开机时只读挂载。
- **data template**（`openclaw-data-template-<版本>.ext4.gz`）：每租户可写数据盘的模板。
- **immutable template**（`openclaw-immutable-<版本>.ext4.gz`）：身份和运维技能的只读权威盘，租户改不了。
- **manifest.json**：版本指针，内容形如 `{"version":"v1.0","rootfs":"...","data_template":"...","immutable":"..."}`，宿主机据此决定拉哪个版本。

## 验证镜像有效（可选）

在**任意 Linux 机器**上下载、解压、挂载检查。注意分工：**OpenClaw 本体在 rootfs 盘**，而**身份/persona 文件烤在 immutable 盘**（`/home/agent` 在 rootfs 里是空挂载点，运行时才挂上数据盘和身份盘，所以别去 rootfs 里找 persona）。

先验 rootfs 是有效文件系统、装了 OpenClaw：

```bash
aws s3 cp s3://my-openclaw-images/deployment/rootfs/openclaw-rootfs-v1.0.ext4.gz .
gunzip openclaw-rootfs-v1.0.ext4.gz
file openclaw-rootfs-v1.0.ext4              # 应显示 "Linux ... ext4 filesystem data"
sudo mount -o ro,loop openclaw-rootfs-v1.0.ext4 /mnt
ls /mnt/usr/lib/node_modules/openclaw       # 能看到 OpenClaw 已装进去
sudo umount /mnt
```

再验 persona 确实烤进了 immutable 盘：

```bash
aws s3 cp s3://my-openclaw-images/deployment/rootfs/openclaw-immutable-v1.0.ext4.gz .
gunzip openclaw-immutable-v1.0.ext4.gz
sudo mount -o ro,loop openclaw-immutable-v1.0.ext4 /mnt
ls /mnt/workspace/                          # 能看到 IDENTITY.md / SOUL.md 等身份文件
sudo umount /mnt
```

构建阶段确认镜像是有效文件系统、OpenClaw 和身份文件都在，即可。想更进一步验证"镜像真能开机、服务真能起"，见下节。

## 进一步：真启动成 microVM 验证服务（需裸金属机）

上面的 `mount` 只验证了镜像内容对。要验证镜像**真能开机、里面的 OpenClaw gateway 服务真能起来**，得把它启动成一台真的 Firecracker microVM。

**关键前提——机器必须是裸金属（bare metal）**：Firecracker 靠 `/dev/kvm` 跑虚拟机，只有裸金属实例才有裸机 KVM。普通虚拟实例（`c5.xlarge`、`c7i.xlarge`、`m7i.xlarge` 这些）**没有 `/dev/kvm`，跑不了 Firecracker**。要 `.metal` 结尾的机型：x86 用 `c5.metal` 或 `c7i.metal-24xl`，arm64 用 `c7g.metal` / `r8g.metal-*`。判断方法：`aws ec2 describe-instance-types --instance-types <类型> --query 'InstanceTypes[0].BareMetal'`，返回 `true` 才行。

启动一台 microVM 要三样东西（都是 Firecracker 官方那套）：

1. **firecracker 二进制** —— 从官方 releases 下载：`github.com/firecracker-microvm/firecracker/releases`（本项目 `init-host.sh` pin 在 `v1.15.1`）。
2. **guest 内核 vmlinux** —— 用 Firecracker CI 提供的：`https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/<主版本>/<arch>/vmlinux-*`（x86 用 `-no-acpi` 变体）。
3. **你烤的四个盘**，按固定顺序挂给 firecracker（顺序是启动契约，别弄乱）：

| guest 设备 | 盘        | 只读? | 说明                                                                                                  |
| ---------- | --------- | ----- | ----------------------------------------------------------------------------------------------------- |
| `/dev/vda` | rootfs    | 只读  | 根文件系统                                                                                            |
| `/dev/vdb` | overlay   | 可写  | copy-on-write 层（格式化好的 ext4；或 boot 参数用 `overlay_root=ram` 走内存）                         |
| `/dev/vdc` | data      | 可写  | 每租户数据盘，`/home/agent` 挂在这（`openclaw-data.service` 写死挂 `/dev/vdc`——盘序错了这里就挂不上） |
| `/dev/vdd` | immutable | 只读  | 身份/技能权威盘                                                                                       |

pubkey 注入：把你的 SSH 公钥写进 **data 盘**的 `.ssh/authorized_keys`（属主 `1000:1000`、权限 700/600），guest 起来后 data 盘挂到 `/home/agent`，就能免密 SSH 进 agent 用户。这跟生产的 `launch-vm.sh:406` 是同一套做法。

启动后进 VM 验服务：

```bash
ssh -i <你的私钥> agent@<guest-ip>
# 在 VM 里：
systemctl --user is-active openclaw-gateway   # 应为 active
ss -tlnp | grep 18789                          # gateway 监听 :18789
openclaw --version                             # 确认版本
# 或从 host 侧直接探：
curl -o /dev/null -w '%{http_code}' http://<guest-ip>:18789/   # 应为 200
```

完整的启动逻辑（起 tap 网络、配 boot-source/drives、InstanceStart）本项目已封装在 `deploy/userdata/launch-vm.sh`，生产就用它。附录给了本节在裸金属上真跑的实测结果。

## 我没有 Linux 机器怎么办 / 其它两条路径

除了本地 Linux，还有两条路径，都调同一个 `build-rootfs.sh`，只是换个地方跑：

**A. 一次性 EC2 构建机（适合 mac/Windows 用户）** —— 脚本自动起一台临时 Ubuntu EC2、在上面构建、传完 S3 自动销毁。本机只要有 AWS CLI，不需要 Linux：

```bash
./scripts/build-rootfs-on-ec2.sh v1.0 arm64      # 版本号 架构
```

（这条同样读 `.env.deploy` 拿桶名，先按上面写好那三行。**但 EC2 路径要求 `PROFILE` 填具名 profile 名、不能留空**——它靠这个 profile 起 EC2、调 SSM；空值会直接报错退出。所以先 `aws configure --profile myprofile` 配一个，再把 `.env.deploy` 里 `PROFILE=` 改成 `PROFILE=myprofile`。）

**B. AWS CodeBuild 流水线（适合做可复用、可审计的交付）** —— 在 CodeBuild 里用容器构建，本机零依赖（只要 AWS CLI），也不需要 Linux。它自己建独立 S3 桶、独立最小权限角色、CodeBuild 项目，**不读 `.env.deploy`、不需要 `setup.sh`**：

```bash
./deploy/codebuild/build-image-demo.sh <PROFILE> <REGION> --arch arm64 --version demo-arm
```

脚本依次：建独立 demo 桶（开 S3 版本管理 + 阻断公开）→ 打包源码传 S3 → 建最小权限 CodeBuild 角色 → 建 CodeBuild 项目（开 `privilegedMode` 跑容器内构建）→ 触发并轮询到成功 → 列出产物和 S3 版本号。这条比本地路径重（要建桶、角色、项目，`BUILD_GENERAL1_LARGE` 机型按分钟计费），价值在于"照着搭一套可复用的构建流水线"。

**该选哪条**：手头有 Linux、只想快点出镜像看看 → 用"最快路径"（本地）。只有 mac/Windows → 用 A（EC2 构建机）。要做长期可复用、可审计、交给团队的构建流水线 → 用 B（CodeBuild）。

## x86_64 与 arm64 的差异

同一套脚本两种架构都能烤，只有两处不同，都由脚本自动处理，你只管传对架构参数：

- **本地路径**：`ARCH=arm64` 或 `ARCH=x86_64`。**EC2 路径**：位置参数 `arm64`/`x86_64`。**CodeBuild 路径**：`--arch arm64` 用 `ARM_CONTAINER` + `amazonlinux2-aarch64-standard:3.0`；`--arch x86_64` 用 `LINUX_CONTAINER` + `amazonlinux2-x86_64-standard:5.0`，脚本按 `--arch` 自动选。
- 脚本内部按架构选 `debootstrap --arch`，arm64 的软件包会自动切到 `ports.ubuntu.com`（你无感知）。

换架构只改一个参数，其余步骤零差异。

## S3 版本管理

版本管理有两层：

**第一层——版本号即文件名。** 每次构建产物文件名都带你传的版本号（如 `openclaw-rootfs-v1.0.ext4.gz`）。不同版本在 S3 里并存、不互相覆盖。`manifest.json` 是指针，指向当前生效版本；宿主机读它决定拉哪个。发新版 = 烤个新版本号再更新 manifest 指过去；回滚 = 把 manifest 指回旧版本号，旧镜像一直都在。

**第二层——S3 Bucket Versioning。** 给桶开启对象版本管理后，同一文件被覆盖写时 S3 保留每次历史版本（各有唯一 Version ID），是对象级兜底。CodeBuild demo 桶默认开了这个；本地路径用的桶要不要开由你决定：

```bash
aws s3api put-bucket-versioning --bucket my-openclaw-images \
  --versioning-configuration Status=Enabled
```

> 在**生产桶**里想烤新版但暂不切换 live 指针（新旧并存、验证期旧版仍是默认），给 `build-rootfs.sh` 传 `SKIP_MANIFEST=1`：新版镜像照传，但不动 `manifest.json`。

## 常见卡点

- **`❌ .env.deploy not found`**：本地/EC2 路径要那个文件，按上面手写三行即可，不用跑 `setup.sh`。
- **`❌ 缺少依赖: debootstrap ...`**：`sudo apt-get install -y debootstrap e2fsprogs pigz curl awscli`。
- **`❌ 可用内存不足` / npm 装到一半进程被杀**：机器内存 <2GB，加内存或加 swap。
- **`❌ /tmp 空间不足`**：`/tmp` 要 ≥10GB。
- **mac 上跑 `build-rootfs.sh` 直接报错退出**：正常，本地路径只能在 Linux 跑，改用 EC2 构建机（路径 A）。
- **CodeBuild 烤完想验证却没有 Linux 机器**：看 CodeBuild 构建输出确认成功即可，或找一台 Linux 跑上面的 `file`/`mount` 验证。

## 关于换 Amazon Linux 2023 基座

常有人问：golden image 基座能不能从 Ubuntu 换成 Amazon Linux 2023（AL2023）？

先分清两层"镜像"，别混：

- **构建环境的 OS**：在哪台机器/容器上跑构建。这层跟 AL2023 无关也可以是 AL2023——CodeBuild 路径的构建宿主本来就是 Amazon Linux（`amazonlinux2-*-standard` 环境镜像），只是它内部再 `docker run ubuntu:22.04` 跑 `debootstrap`。
- **golden image 基座**：microVM 开机跑的那个根文件系统。这层现在是 **Ubuntu 24.04**，也是这个问题真正问的。

**技术上能不能换**：Firecracker 本身不挑 rootfs 发行版（它只管跑内核 + rootfs），所以基座换 AL2023 理论可行。但**不是改个参数的事**，要重做一整套并重新验证：

- `build-rootfs.sh` 用的 `debootstrap` 是 Debian/Ubuntu 专用，装不了 AL2023（RPM 系）。换 AL2023 要把这套换成 `dnf --installroot` 那套 bootstrap 流程。
- **要重新测整个 Firecracker 注入标准 + 内核兼容性**：AL2023 默认内核/initramfs、`overlay-init` 的 `pivot_root`、`/dev/vd*` 盘序挂载、systemd user service（gateway）在 AL2023 上的行为，都得在裸金属上真启动重验一遍。
- 只读盘（EROFS/ext4）、seccomp、auditd、`8250.nr_uarts=0` 串口关闭这些加固，要逐条在 AL2023 上确认一样成立。

所以这是一个**独立的改造 + 重测任务**，不在本 get-start 范围。当前结论：跟 Firecracker 官方示例保持一致，基座用 Ubuntu 24.04。

## 附录：实测环境与结果

以下为 CodeBuild 路径在真实环境跑通的记录，供对照预期。

**测试环境：**

- **区域**：us-east-1
- **构建方式**：AWS CodeBuild，`privilegedMode` + `BUILD_GENERAL1_LARGE`
- **样本输入**：`samples/finance-agent`
- **OpenClaw 版本**：openclaw@2026.5.6
- **基座**：Ubuntu 22.04（容器内 debootstrap Ubuntu 根文件系统）

**arm64 与 x86_64 均实测通过：**

| 架构   | CodeBuild 环境                                         | 构建耗时（build 阶段） | rootfs 压缩后 | data template | immutable | 结果      |
| ------ | ------------------------------------------------------ | ---------------------- | ------------- | ------------- | --------- | --------- |
| arm64  | `amazonlinux2-aarch64-standard:3.0` / `ARM_CONTAINER`  | 约 3 分钟              | 843.6 MiB     | 9.0 MiB       | 46.1 KiB  | SUCCEEDED |
| x86_64 | `amazonlinux2-x86_64-standard:5.0` / `LINUX_CONTAINER` | 约 3.5 分钟            | 868.1 MiB     | 9.0 MiB       | 46.1 KiB  | SUCCEEDED |

两架构的产物都通过 `file` 确认为有效 ext4 只读文件系统镜像；demo 桶开启的 S3 Versioning 对每个镜像对象都记录了唯一 Version ID。以上为 2026-07-07 在 us-east-1 用 `deploy/codebuild/build-image-demo.sh` 实测。

**真启动成 microVM + 服务存活实测（裸金属）：** 把 x86_64 镜像下载到裸金属机、按上面"进一步"那节的四盘顺序 + 官方 firecracker v1.15.1 + CI kernel 真启动，两种 x86 metal 机型都通过：

| 机型             | BareMetal | 启动到                                      | ping guest               | gateway :18789 | gateway user service          |
| ---------------- | --------- | ------------------------------------------- | ------------------------ | -------------- | ----------------------------- |
| `c5.metal`       | true      | Ubuntu 24.04 login（内核 5.10.245+ x86_64） | 0% 丢包，RTT 0.13–0.30ms | HTTP 200       | active（`node` LISTEN 18789） |
| `c7i.metal-24xl` | true      | 同上                                        | 0% 丢包                  | HTTP 200       | active（`node` LISTEN 18789） |

同时验证：SSH 用注入 data 盘的 pubkey 免密进 agent 用户成功；rootfs 里 `/usr/lib/node_modules/openclaw` 实测 `OpenClaw 2026.5.6`；immutable 盘 `/workspace/` 有 7 个 persona 身份文件。

对比：普通虚拟实例（如 `c7i.xlarge`，BareMetal=false）没有 `/dev/kvm`，起不了 Firecracker——这也是"启动验证必须用 `.metal` 机型"的原因。两种 x86 metal 机型的启动流程与结果完全一致，换机型只是 CPU 代次不同（`c5.metal` Cascade Lake / `c7i.metal` Sapphire Rapids），启动契约、盘序、服务行为无差异。

**一个真机踩到的启动契约坑**：盘必须按 rootfs=vda / overlay=vdb / data=vdc / immutable=vdd 的顺序挂给 firecracker。若为省事跳过 overlay 盘（用 `overlay_root=ram`），data 会顺移成 vdb，而 guest 的 `openclaw-data.service` 写死挂 `/dev/vdc`，导致数据盘挂不上、`/home/agent` 为空、gateway user service 因配置缺失不自启。补回格式化好的 overlay 盘、恢复正确盘序后，gateway 正常自启。
