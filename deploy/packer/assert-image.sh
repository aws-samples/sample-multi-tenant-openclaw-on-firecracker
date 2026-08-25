#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
#
# assert-image.sh — 在【构建实例上】断言镜像内容,复刻 Image Builder 的
# AssertZeroDownloadBootPath + 跨租户红线检查。
#
# 为什么是独立脚本而不是两段内联:这套检查要在两个时机各跑一次(provision 之后、
# 以及 provision 重跑之后),而两处各维护一份内联脚本必然漂移。实测 2026-08-13 的
# 真缺陷正是如此:幂等阶段只重查了 2 个命令和 2 个泄漏路径,漏掉 Fluent Bit、
# vmlinux、ADOT、SSH host key、cloud-init 态 —— 于是"重跑重装了这些东西"这类回归
# 恰好落在断言的盲区里,而"幂等"这条断言存在的意义就是发现它。单一事实源之后,
# 加一项检查两个时机自动都有。
#
# 两个阶段的判据【不同】,这是本脚本的核心:
#   post-provision  存在性 —— 组件齐、身份已擦净。并把关键项的指纹存档。
#   post-rerun      存在性 + 【不变性】 —— 与存档逐项比对。
#
# 为什么必须比不变性:存在性检查通不过幂等这道题。重跑把 Fluent Bit 卸了重装、把 ADOT
# 换个版本、把 baked vmlinux 替换掉,文件仍然"存在",断言照样绿 —— 而幂等的定义是
# 【最终状态不变】,不是"东西还在"。指纹用内容摘要而非"有没有执行动作":重装同一个 deb
# 产出相同字节,那本身就是幂等的,不该报警;真换了版本或重新编译,摘要会变,必须报警。
#
# 用法: assert-image.sh <phase>
#   phase = post-provision | post-rerun
# 退出码: 0=全过, 1=有失败项(逐条打印)
#
# 由 host-golden.pkr.hcl 的两个 shell provisioner 调用。不在 host 启动路径上执行。

set -euo pipefail

PHASE="${1:?usage: assert-image.sh <post-provision|post-rerun>}"
case "$PHASE" in
  post-provision | post-rerun) ;;
  *) echo "FATAL: unknown phase '$PHASE'" >&2; exit 1 ;;
esac

# 指纹存档。放 /opt/openclaw/ 而不是 /tmp:/tmp 在部分环境开机被清,而这份存档要能被
# 起来的 host 读到(排查"这批镜像的组件指纹是什么"时有用),与 marker 同一去处。
FP_FILE=/opt/openclaw/.image-fingerprint

fail=0
_bad() { echo "$1"; fail=1; }

# ── 组件零安装判据 ───────────────────────────────────────────────────────────
# #523 判据 5:这里原本写的目标是「golden host 必须能在【零下载】的前提下起到
# running」,那个目标**不成立**,而且从来没成立过 —— init-host.sh 在
# LOGGING_ENABLED=true 时【无条件】从 S3 拉 install-fluent-bit.sh(S3 miss 即
# `exit 1`),后面还要拉 oc-guest-log-reader.py、rootfs manifest.json、以及 step4 的
# 全部生命周期脚本。把目标写成做不到的样子有实际代价:下一个人会按「零下载」去排查
# 启动失败,而客户那两次 canary ABANDON 恰好发生在「AMI 已经有、启动仍跑 S3 那份」
# 这条路上(根因是 S3 上的旧 installer 用交互式 gpg,#520 A21 实例①)。
#
# 真正成立、也真正值钱的判据是**组件零安装**:下面每个缺失项 = boot path 要补做的
# 一次【安装】(apt / dpkg / 解包),那才是不可靠的部分(错架构 awscli、FC tgz 404、
# aarch64 vmlinux 404 各自 ABANDON 过一台 metal)。所以在这里一次性断言,而不是等
# lifecycle hook 已经在计时的 host 上才发现。配置与脚本仍从 S3 拉,那条路由
# init-host.sh 自己的 fail-loud 兜(拉不到即 exit 1),不在本脚本的判据里。
for b in aws firecracker jailer; do
  command -v "$b" >/dev/null 2>&1 || _bad "MISSING binary: $b"
done

# Fluent Bit 官方包装到 /opt/fluent-bit/bin,不在 PATH 上(真机 2026-08-05)。按
# PATH 探会在装好的镜像上误报缺失,而 installer 里同样的错探针会让每次 golden boot
# 都重新联网装一遍。断言打包位置。
for f in /opt/fluent-bit/bin/fluent-bit \
  /opt/openclaw/baked/vmlinux \
  /etc/openclaw/.ami-provisioned; do
  [ -s "$f" ] || _bad "MISSING file: $f"
done

dpkg -s aws-otel-collector >/dev/null 2>&1 || _bad "MISSING pkg: aws-otel-collector"

# SSM agent 必须留在镜像里。控制面靠 SSM 驱动 host(launch-vm/stop-vm 批量、
# host-agent 命令),没有 agent 的 host 从控制面看就是不可达 —— 它会注册成 active
# 然后每条指令都超时。Image Builder 侧必须显式声明 uninstall_after_build=False
# (host_image.py 的 AdditionalInstanceConfiguration),因为它默认会卸载;Packer 走
# SSH 不碰 agent,所以这里【断言】而不是配置,免得"Packer 不会卸载"这个前提在换
# communicator 或加清理步骤后静默失效。
systemctl list-unit-files 'snap.amazon-ssm-agent.*' 'amazon-ssm-agent.*' 2>/dev/null |
  grep -q 'ssm-agent' || _bad "MISSING: amazon-ssm-agent unit not present in image"

# ── 客户 hook 的可审计性与两侧对账(#537 D5 / V-3)────────────────────────────
# 自定义阶段的产物要可审计:镜像里必须留着实际执行过的那批脚本与一份执行记录,这样起
# 一台 host 就能核对这批镜像装了什么客户内容(US-2 / US-6 的可见结果)。
#
# 更要紧的是【对账】:构建侧(build-golden-ami.sh 用 bash find)算出 HooksSha 并经
# packer 注进 OC_EXPECT_HOOKS_SHA,这里从【镜像里实际存在的那批】重算一次再比。两侧
# 独立算才能发现 file provisioner 少传了文件、两处 glob 语义漂了、或客户在 packer
# 起来之后又往 hooks/ 里丢了东西 —— 本地算两次都察觉不到这三种。
HOOKS_DIR="${OC_HOOKS_DIR:-/opt/openclaw/custom/hooks}"
HOOKS_MANIFEST="${OC_HOOKS_MANIFEST:-/opt/openclaw/custom/hooks-manifest}"

# 期望值缺失必须 FAIL,不能跳过比对。静默跳过等于悄悄丢掉整条对账断言 —— 而"环境变量
# 没传进来"恰好是最容易发生的那种回归(改 provisioner 时漏一行、sudo 少个 -E)。
if [ -z "${OC_EXPECT_HOOKS_SHA:-}" ] || [ -z "${OC_EXPECT_HOOKS_COUNT:-}" ]; then
  _bad "MISSING OC_EXPECT_HOOKS_SHA / OC_EXPECT_HOOKS_COUNT — hook 对账无从进行(provisioner 少传环境变量,或 sudo 缺 -E)"
else
  [ -d "$HOOKS_DIR" ] || _bad "MISSING dir: $HOOKS_DIR"
  [ -s "$HOOKS_MANIFEST" ] || _bad "MISSING file: $HOOKS_MANIFEST — run-hooks.sh 没跑过,或跑在本断言之后"

  if [ -d "$HOOKS_DIR" ]; then
    # 与 build-golden-ami.sh 和 run-hooks.sh 同款枚举:find -maxdepth 1 -type f
    # -name '*.sh' | sort。摘要口径也必须逐字相同("<sha256>  <basename>" 逐行,
    # 再对整串取一次)—— 口径差一个空格,两侧就永远不等,那会把这条门变成恒红,
    # 同样没用。
    _hook_lines=""
    _hook_n=0
    while IFS= read -r _h; do
      [ -n "$_h" ] || continue
      _hook_n=$((_hook_n + 1))
      _hook_lines="${_hook_lines}$(sha256sum "$_h" | cut -d' ' -f1)  $(basename "$_h")"$'\n'
    done < <(find "$HOOKS_DIR" -maxdepth 1 -type f -name '*.sh' -print 2>/dev/null | sort)

    _hook_sha="$(printf '%s' "$_hook_lines" | sha256sum | cut -d' ' -f1)"

    if [ "$_hook_n" != "$OC_EXPECT_HOOKS_COUNT" ]; then
      _bad "HOOK COUNT MISMATCH: 镜像里有 $_hook_n 个,构建侧枚举 $OC_EXPECT_HOOKS_COUNT 个"
    fi
    if [ "$_hook_sha" != "$OC_EXPECT_HOOKS_SHA" ]; then
      _bad "HOOK SHA MISMATCH: 镜像里重算 $_hook_sha,构建侧注入 $OC_EXPECT_HOOKS_SHA —— 上传的和执行的不是同一批"
    fi
    if [ "$_hook_n" = "$OC_EXPECT_HOOKS_COUNT" ] && [ "$_hook_sha" = "$OC_EXPECT_HOOKS_SHA" ]; then
      echo "hooks reconciled: $_hook_n hook(s), sha=$_hook_sha"
    fi

    # 执行记录里的条数必须与目录里的一致。目录有 3 个而 manifest 只记了 2 条 = 第 3 个
    # 没被执行(run-hooks.sh 在它之前就失败了,而 set -e 本该让构建红)—— 这条是那种
    # "构建红了但被谁吞掉"情况的兜底。
    if [ -s "$HOOKS_MANIFEST" ]; then
      # 用 awk 数而不是 `grep -c ... || echo 0`:grep -c 在无匹配时【输出 0 但退出码 1】,
      # 于是 `|| echo 0` 的分支也会执行,变量拿到的是两行 "0\n0" —— 与 "0" 不等,这条
      # 断言在 hook 目录为空(最常见的情况)时恒假红。实测 2026-08-21 第一次真机构建
      # 正是死在这里,而 bash -n 与 packer validate 都抓不到它。
      # awk 总是退出码 0 且只输出一个数(END 里 n+0 把未初始化的 n 变成 0)。
      _ran="$(awk '/^[0-9a-f]{64}  /{n++} END{print n+0}' "$HOOKS_MANIFEST")"
      [ "$_ran" = "$_hook_n" ] ||
        _bad "HOOK RUN MISMATCH: 目录里 $_hook_n 个,hooks-manifest 只记录了 $_ran 个执行成功"
    fi
  fi
fi


# ── 跨租户红线 ───────────────────────────────────────────────────────────────
# host_vm_key 是 per-host 的,公钥半边注进每个租户 microVM,一把被预置于镜像的私钥
# = 任意 host 能 SSH 进任意 host 上任意租户的 microVM。provision 的 scrub 已经查过;
# 这里再查一遍,因为本脚本跑在所有 build 步骤【之后】—— 能抓到"后来新增的步骤又造了
# 一把 key",也能抓到客户自定义脚本引入的。
for leak in /etc/openclaw/host_vm_key /etc/openclaw/host_vm_key.pub /etc/platform.env \
  /data/agentcore.env /etc/openclaw/host_vm_key.instance; do
  [ ! -e "$leak" ] || _bad "LEAK: $leak present in image"
done

# SSH host key。scrub 删了它们(`rm -f /etc/ssh/ssh_host_*`),但客户脚本重装
# openssh-server、或跑 `dpkg-reconfigure openssh-server`,都会重新生成一对。预置于
# 镜像后整个机队共享同一把 host key:任何能起一台 host 的人都能冒充其余每一台,
# MITM 检测失效。正常路径下 cloud-init 在首次启动时按实例重新生成,所以镜像里应当
# 一把都没有。
#
# 用 find 而不是 `ls ssh_host_*`:glob 无匹配时 ls 返回非零,叠加 set -e 与 pipefail
# 会让整个脚本【在这一行就退出】—— 后面的检查全部不执行,而 packer 只报 "Script
# exited with non-zero exit status: 2",看不出是断言自己死了。实测 2026-08-12:这行的
# 第一版正是如此,把本该兜住自定义脚本的防线整条废掉。find 在无匹配时返回 0 且输出
# 为空,是这里唯一安全的写法。
_sshkeys="$(find /etc/ssh -maxdepth 1 -name 'ssh_host_*' -print 2>/dev/null | tr '\n' ' ')"
[ -z "$_sshkeys" ] || _bad "LEAK: SSH host keys present in image: $_sshkeys"

# cloud-init 实例态残留会让新实例复用旧 instance-id 的判定,首启逻辑被跳过。
[ ! -d /var/lib/cloud/instances ] || [ -z "$(ls -A /var/lib/cloud/instances 2>/dev/null)" ] ||
  _bad "LEAK: cloud-init instance state present"

# ── 不变性:采集指纹 ─────────────────────────────────────────────────────────
# 只收【重跑不该改变】的项。刻意排除 marker 的 provisioned_at —— 它是时间戳,每次
# provision 合法地会变;marker 的其余字段(recipe/arch/fc 版本/guest kernel)不该变。
# 每项一行 key=value,排序固定,便于 diff 直接指出是哪一项漂了。
_fingerprint() {
  local b f _awsbin _awsreal _awsroot
  for b in aws firecracker jailer; do
    printf 'path.%s=%s\n' "$b" "$(command -v "$b" 2>/dev/null || echo ABSENT)"
  done
  # aws 上面只记了 resolved path,那个值在"同路径换掉背后的二进制"时不变。
  # aws v2 的官方安装器(provision-host.sh 第 114 行的 `aws/install`)默认
  # --install-dir /usr/local/aws-cli --bin-dir /usr/local/bin,于是 /usr/local/bin/aws
  # 是个 symlink,指向 /usr/local/aws-cli/v2/<version>/bin/aws,后者又指向 ../dist/aws。
  # 两个信号,分工不同:
  #   sha256tree.aws  整个版本 bundle 的【确定性树摘要】。判非幂等靠这一个。
  #   version.aws     版本串。对"抓到变化"是冗余的(内容变了树摘要必然先变),留着是为了
  #                   【可诊断】:diff 直接读出 2.15.0 → 2.99.0,纯哈希只能说"变了"。
  #                   排查一批镜像为什么不一致时,这一行省掉一次登机器。
  #
  # 为什么摘整棵树而不是入口文件:aws v2 是 PyInstaller bundle,入口只是 dist 下的一个
  # 可执行文件,同目录还有上千个 .so 与数据文件。只摘入口时,改动任一非入口文件而保持
  # 入口字节与版本串不变 → 指纹完全不动,仍是假绿(第十轮评审 finding)。
  # 成本实测(2026-08-14,awscli 2.34.37,173 MB / 9351 文件):逐文件 sha256 再汇总
  # 【2.7 秒】。本注释此前写的"上千文件数百 MB 换不来额外覆盖"是基于估算的高估 ——
  # 在数分钟的 provision 里 2.7s 可忽略,那条理由不成立,故改为摘整棵树。
  # find -print0 | sort -z 保证摘要与 readdir 顺序无关;摘要输入含文件路径,所以版本
  # 目录改名也会让摘要变。
  printf 'version.aws=%s\n' "$(aws --version 2>&1 || echo ABSENT)"
  _awsbin="$(command -v aws 2>/dev/null || true)"
  _awsreal="$(readlink -f "$_awsbin" 2>/dev/null || true)"
  if [ -n "$_awsreal" ] && [ -f "$_awsreal" ]; then
    # bundle 根 = 解引用后入口的上两级:官方安装器解到 <root>/dist/aws,包管理器
    # (homebrew)解到 <root>/bin/aws,两者上两级都是 bundle 根。
    _awsroot="$(dirname "$(dirname "$_awsreal")")"
    if [ -d "$_awsroot/dist" ] || [ -d "$_awsroot/bin" ]; then
      printf 'sha256tree.aws=%s\n' \
        "$(find "$_awsroot" -type f -print0 | sort -z | xargs -0 sha256sum |
          sha256sum | cut -d' ' -f1)"
    else
      # 布局不认识时退回只摘入口,并【在指纹里标出来】—— 静默换判据会让"树摘要"这条
      # 断言在布局变化后悄悄降级成入口摘要,而 diff 看不出降级发生过。
      printf 'sha256tree.aws=ENTRYONLY:%s\n' "$(sha256sum "$_awsreal" | cut -d' ' -f1)"
    fi
  else
    printf 'sha256tree.aws=ABSENT\n'
  fi
  for f in /usr/local/bin/firecracker /usr/local/bin/jailer \
    /opt/fluent-bit/bin/fluent-bit /opt/openclaw/baked/vmlinux; do
    if [ -f "$f" ]; then
      printf 'sha256.%s=%s\n' "$f" "$(sha256sum "$f" | cut -d' ' -f1)"
    else
      printf 'sha256.%s=ABSENT\n' "$f"
    fi
  done
  printf 'pkg.aws-otel-collector=%s\n' \
    "$(dpkg-query -W -f='${Version}' aws-otel-collector 2>/dev/null || echo ABSENT)"
  printf 'unit.ssm-agent=%s\n' \
    "$(systemctl list-unit-files 'snap.amazon-ssm-agent.*' 'amazon-ssm-agent.*' 2>/dev/null |
      grep -o '[a-z.-]*ssm-agent[a-z.-]*' | sort -u | tr '\n' ',' || echo ABSENT)"
  # marker 除时间戳外的字段。grep -v 而不是挑字段:新增字段会自动纳入比对,
  # 而"新增了字段"本身也是重跑该发现的变化。
  if [ -f /etc/openclaw/.ami-provisioned ]; then
    grep -v '^provisioned_at=' /etc/openclaw/.ami-provisioned | sed 's/^/marker./'
  else
    echo 'marker.=ABSENT'
  fi
}

if [ "$PHASE" = "post-provision" ]; then
  install -d -m 0755 /opt/openclaw
  _fingerprint > "$FP_FILE"
  chmod 0644 "$FP_FILE"
  echo "fingerprint recorded: $(wc -l < "$FP_FILE" | tr -d ' ') items -> $FP_FILE"
else
  # post-rerun:存档必须已在。缺了说明 provisioner 顺序被改动过(post-provision 没跑,
  # 或跑在这之后)—— 那种情况下比对无从进行,必须 fail loud 而不是静默跳过。
  if [ ! -s "$FP_FILE" ]; then
    _bad "MISSING $FP_FILE — post-provision phase did not run before this one; idempotency cannot be proven"
  else
    _now="$(mktemp)"
    _fingerprint > "$_now"
    if ! diff -u "$FP_FILE" "$_now" > /tmp/fp.diff 2>&1; then
      _bad "NOT IDEMPOTENT: the re-run changed the image. Differences (recorded vs now):"
      sed 's/^/    /' /tmp/fp.diff
    else
      echo "fingerprint unchanged across the re-run ($(wc -l < "$FP_FILE" | tr -d ' ') items)"
    fi
    rm -f "$_now"
  fi
fi

if [ "$fail" != 0 ]; then
  echo "golden AMI validation failed (phase=$PHASE)"
  exit 1
fi
echo "golden AMI validated (phase=$PHASE): components present, no host identity"
