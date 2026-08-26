# edge-balancer-cosocket-606 — apply by reading, no CloudFormation redeploy

> **P0。** `balancer_by_lua*` 阶段调 Redis cosocket 被 OpenResty 硬禁止,导致任何触发 upstream
> 重投的租户 WSS 全部 502。缺陷自 **2026-07-21** 起就在在役 edge bundle 里,只有「租户被重建 /
> 恢复 / 迁移」这类会让 L1 缓存失效的场景才暴露。

`status: MANUAL_REVIEW`。本 kit 有 3 个 `MANUAL_CLI_REVIEW` 操作,必须逐个人工复核后才动手。
**任何步骤都不要运行触发 CloudFormation 栈更新的命令。**

- `base_sha` = `e57e9eaea1154caa6020dca6735af92af2fe9185`
- `patch_sha` = `58bd07de4f603cb803fbc72dade14bde6921e811`

两端都在公开仓可解析,所以下面每条校验命令你都能自己跑通。

## 先读四条会静默毁掉本次交付的事实

**① 这个 kit 的 `apply_cli` 真的下发到在役 edge。**
它不是 `lifecycle-op-patch` 里 `deploy-other` 的那种形态 —— 那一层只 `cp` 到 `$REPO_ROOT`,
**打完在役不生效**。edge 的交付结构决定了三件事:

| 事实 | 后果 |
|---|---|
| edge 是独立数据面 ASG | 控制面 API、Lambda overlay、host 脚本下发都碰不到它 |
| bundle 由 LaunchTemplate 钉 sha | 改 S3 对象未必影响新实例,取决于 userdata 有无 sha 校验(见第 ④ 条) |
| openresty 从 `lualib` 加载、从 `/opt/openclaw-edge` bootstrap | **两处都要写**,只改 `lualib` 会在重启或重新 bootstrap 后丢失修复 |

**② 四个文件是一整套,但可以分开安装 —— 因为 reload 才是生效点。**
`lua_code_cache` 让已启动的 worker 继续用已 `require` 的旧模块,所以文件落盘但未 reload 时
**在役行为完全不变**。这就是本 kit 的原子性保证:部分安装是安全的,风险只在 reload 那一刻兑现。
因此守卫不落在「按顺序装」的嘱咐上,而是机械地落在 reload 操作里 —— 它会**逐台断言四个文件的
sha256 全部就位才允许 reload**。依赖关系:

- `balancer.lua` 调用 `backend.peek_cached` / `backend.mark_retry_stale` / `backend.SOURCE_L2`
  —— 这三个是本次在 `backend.lua` 新增的
- `route.lua` 提供 `on_header_filter`
- `nginx.conf` 提供 `header_filter_by_lua_block`,把 balancer 阶段 fail-closed 的 503
  从 nginx 生成的 500 改写回真实状态码

缺任一文件的表现:`attempt to call a nil value`,或者 fail-closed 时客户端拿到 500 而非 503。

**③ 在役 edge 可能已经带着这个修复了 —— 先核对再动手。**
这个缺陷在一次运维轮次里被**热补丁**修过,并落到了 `lualib`、`/opt` 与 S3 三处。
所以施加前必须先跑 Step 0 的基线核对:若在役 sha 已等于 `manifest.json` 的 `patch_sha256`,
该台**跳过**。跳过不是失败 —— 本 kit 的价值在于把当时的手工动作变成可复现、可核验的交付物。

**④ Step 1–3 只止住当前实例的血。新实例是否继承修复,取决于一个还没回答的问题。**
任何扩容、健康检查替换、AZ 重平衡起来的新 edge 都会从 S3 拉 bundle。它继承不继承修复,
取决于 LaunchTemplate 的 userdata 拉 bundle 时**有没有 sha256 校验**。这条由
`verify-606-new-instance-inheritance` 这个判定门回答,**不回答它就等于不知道**。

## Step 0 — 环境、edge 实例集合、基线核对

```bash
: "${REGION:?先 export REGION}"
: "${ASSETS_BUCKET:?先 export ASSETS_BUCKET}"
: "${EDGE_ASG:=openclaw-edge-asg}"
export REPO_ROOT="${REPO_ROOT:-$PWD}"

EDGE_IDS="$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names "$EDGE_ASG" \
  --query 'AutoScalingGroups[0].Instances[?LifecycleState==`InService`].InstanceId' \
  --output text)"
export EDGE_IDS
test -n "$EDGE_IDS" || { echo "没取到 InService 的 edge 实例" >&2; exit 1; }
echo "edge 实例: $EDGE_IDS"
```

基线核对(逐台读四个在役文件的 sha256,与 manifest 比):

```bash
python3 - <<'PY'
import json, subprocess, os
m = json.load(open("manifest.json"))
live = {
    "deploy/edge/lib/balancer.lua": "/usr/local/openresty/lualib/edge/lib/balancer.lua",
    "deploy/edge/lib/backend.lua": "/usr/local/openresty/lualib/edge/lib/backend.lua",
    "deploy/edge/route.lua": "/usr/local/openresty/lualib/edge/route.lua",
    "deploy/edge/nginx.conf": "/usr/local/openresty/nginx/conf/nginx.conf",
}
cmds = ["set -e"] + [
    'echo "%s  %s" | sha256sum -c - || echo "DIFFERS %s"' % (m["paths"][p]["patch_sha256"], t, t)
    for p, t in live.items()
]
print(json.dumps({"commands": cmds}))
PY
```

把上面输出的 JSON 存成文件,用 `aws ssm send-command --parameters file://...` 逐台跑。
四条都 `OK` 的实例已经带修复,跳过它的 Step 2。

## Step 1 — 制品上传到 S3(零在役影响,完全可逆)

按 `manifest.json` 里四个 `deploy/edge/*` 路径各自 `operations[0].apply_cli` 的**前半段**执行。
制品放在**新前缀 `deployment/edge-606/`**,不覆盖任何现有 bundle 对象,回滚就是删对象。

## Step 2 — 下发到在役两处落点(逐台,不 reload)

`operations[0].apply_cli` 的后半段。要点:

- 备份锚点 `.pre-606` **只建一次**(`[ -f ... ] ||` 守卫)。重跑若覆盖锚点,备份就变成已修补的
  内容,回滚从此无效
- 下发后 `sha256sum -c -` 逐文件核对,**两处落点都要核**
- **这一步刻意不 reload**。见开头第 ② 条

## Step 3 — 一次性 reload(自守卫,`MANUAL_CLI_REVIEW`)

`deploy/edge/nginx.conf` 的 `operations[1]`。它先逐台断言四个文件的 sha256 全部就位,
再 `openresty -t`、`openresty -s reload`,然后回读 journald 确认没有 `API disabled`。

`openresty -t` 可以先跑,但它**不加载 lua 模块** —— 通过不代表 lua 能载入。lua 的加载与运行
失败只出现在 journald 与 `error.log`。

## Step 4 — 新实例继承判定门(`MANUAL_CLI_REVIEW`,只读)

`deploy/edge/nginx.conf` 的 `operations[2]`。它只读 LaunchTemplate 的 userdata 并落一份回执:

```bash
aws ec2 describe-launch-template-versions --region "$REGION" \
  --launch-template-name "$EDGE_LT_NAME" --versions "$EDGE_LT_VERSION" \
  --query 'LaunchTemplateVersions[0].LaunchTemplateData.UserData' --output text \
  | base64 -d > edge-lt-userdata.txt
grep -nE 'sha256|checksum|digest' edge-lt-userdata.txt > edge-bundle-sha-gate.txt \
  || echo 'no sha256/checksum/digest hit in userdata' > edge-bundle-sha-gate.txt
```

然后**人工**在 `edge-bundle-sha-gate.txt` 末尾追加一行结论,`verify` 只认这两个词之一:

- `VERDICT: unpinned` → userdata 不校验 bundle 摘要,可以直接更新现有 bundle 对象让新实例继承。
  先记下旧 `VersionId` 作回滚锚点(S3 versioning 使字节替换可逆)
- `VERDICT: pinned` → 改 bundle 内容后 sha 不匹配、新实例起不来。必须 S3 与 LaunchTemplate
  同步改,属换机级操作,**单独排期**,不要接在本次施加后面

## Step 5 — 客户仓库副本里的记账文件(可跳过)

`fix-606-kit-bookkeeping` 那一组的 5 条路径,是**本 kit 自己的记账文件**。修复提交
`58bd07de` 把 4 个 edge 产品文件与 kit 目录放进了同一个提交,所以 `base..patch` 的 range 必然
把 kit 目录也框进来,而 `patch/` 不在允许排除的前缀里。这一组只更新仓库副本,**不触碰任何
在役资源**;不保留仓库克隆的客户可以整组跳过。它的 `apply_cli` 是「缺则装、在则断言」——
内容与记录不同就拒绝覆盖,不会悄悄改掉你的副本。等本 patch 重新锚定到一次 publish 提交
(publish 流不写 `patch/`),这几条会自然离开 range。

## 验证:只有一个探针能证明修复生效

`manifest.json` 的 `verifications[]` 有 6 条,按 `phase` 分两批:

- **Phase A-readonly(只读,4 条)**:`verify-606-no-api-disabled`、
  `verify-606-both-landing-spots`、`verify-606-new-instance-inheritance`、
  `verify-606-repo-copy-digests`
- **Phase B-lifecycle(走真实产品入口,2 条,核心)**:`verify-606-retry-path-selfheal`、
  `verify-606-fail-closed-no-reuse`

**正常请求不进 retry 分支。** 所以「WSS 能连上」「对话正常」这类验证**完全没有验到这个 fix**。
`verify-606-retry-path-selfheal` 是唯一能证明修复生效的探针:让某个租户的 route descriptor
指向不可达的 host(先记下它的 `host:port`,再让那台 VM 停止),从**外部真实发起** WSS,断言
L1 TTL 内的下一次请求连到新 peer 且无 `API disabled`。

修复后的行为:balancer 发现失败 → 从 shared_dict 找一个**不同**的 peer;找不到就
`mark_retry_stale` 后 fail closed 返 503。route 变更由 L1 TTL 过期驱动,下一次请求在
`rewrite` 阶段(cosocket 合法)读 Redis 拿到新 desc 自愈。

`verify-606-fail-closed-no-reuse` 验的是安全边界:构造共享缓存里找不到任何不同 peer 的状态,
断言客户端拿 **503**,而且**没有**把请求投到那个已知失败的 peer 上。拿到 500 说明 `nginx.conf`
没有下发成功(`header_filter_by_lua_block` 缺失)。

## 陷阱清单

| 陷阱 | 说明 |
|---|---|
| **`openresty -t` 通过不代表 lua 能载入** | `-t` 不加载 lua 模块。lua 的加载与运行失败只出现在 journald 与 `error.log` |
| **只改 `lualib` 会在重启后丢** | `/opt/openclaw-edge` 是 bootstrap 源,两处都要写 |
| **只验正常请求等于没验** | 正常请求不进 retry 分支,验不到本 fix。必须主动触发 retry |
| **故障表现为「部分租户断」** | 首连即命中有效 desc 的租户不受影响,容易误判成租户个体问题或 route 数据错误 |
| **不要重用失败的 desc** | 那个坐标可能已被回收重分配给另一个在役租户,重投会连上别人的 VM。这是安全边界,不是性能优化 |
| **回滚到更早的 bundle 无效** | 本缺陷自 2026-07-21 起就在。回滚到更早版本 = 重新引入同一缺陷 |
| **`systemctl status` 可能失真** | 若 openresty 非 systemd 管理,active/inactive 判断不可靠,需同时看进程与端口监听 |
| **备份锚点被重跑覆盖** | `.pre-606` 若在第二次施加时被覆盖,备份内容就是已修补版,回滚从此无效 |

## 回滚

每台的回滚见四个 `deploy/edge/*` 路径各自的 `rollback_cli`:先**预检**(断言在役内容仍是本
patch 装上去的那份,否则拒绝动手),再从 `.pre-606` 还原两处落点。四个文件还原完之后,用
`nginx.conf` 的 `operations[1].rollback_cli` 做一次 reload 让还原生效。

Step 1 的回滚:删掉 `s3://$ASSETS_BUCKET/deployment/edge-606/` 下的对象。

**注意**:回滚意味着恢复到带缺陷的版本,触发 retry 的租户会重新开始 502。只在下发本身出问题
(lua 载入失败、openresty 起不来)时回滚,不要因为「部分租户仍连不上」就回滚 —— 那可能是
另一层的问题,例如 guest 内 gateway 的 Origin 白名单,与本 kit 无关。

## 本 kit 不修什么

- **gateway 应用层的 `controlUi.allowedOrigins` 校验**。`INVALID_REQUEST: origin not allowed`
  是 guest 内 openclaw gateway 的 Origin 白名单问题,与 edge balancer 无关,需要单独处置
- **Redis route 键的回收**。route 键不会被自动回收,恢复租户靠新写入覆盖
- **guest 出网策略(egress)**。与本缺陷无因果关系

## 溯源与测试覆盖

| 项 | 值 |
|---|---|
| 上游 issue | #606(fail-closed 语义 #628,坐标回收风险 #605) |
| 修复提交 | `58bd07de4f603cb803fbc72dade14bde6921e811` |
| 现有测试 | `deploy/edge/test/balancer_failover_adversarial_spec.lua`、`balancer_spec.lua` |

**单元测试覆盖不到这类缺陷**:用假 redis 模块的测试会全绿通过,因为它模拟不出
`balancer_by_lua*` 的上下文限制;`openresty -t` 也不触发运行时阶段错误。这是一条独立的
**测试假绿**缺陷,补一个「在真实 balancer 阶段断言不得发起 cosocket / shared_dict 写」的
集成用例才算闭环 —— 验收标准是:故意在 balancer 阶段引入一次 cosocket 调用,测试必须红。
该用例尚未纳入本 kit 的覆盖面。
