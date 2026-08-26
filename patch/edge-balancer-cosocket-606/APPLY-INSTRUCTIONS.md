# edge-balancer-cosocket-606 — apply by reading, no CloudFormation redeploy

> 本 kit 把 edge 数据面同步到内部源的同一版本:**Redis 坐标改走单通道**(修掉 readiness 门恒
> fail-open —— `/healthz` 在从未验证 Redis 可达的情况下就翻 200)、**balancer 阶段的真实相位探针**
> (补上原来那批用假 redis 模块的单测覆盖不到的那一层),外加租户标识与工具模块、日志转发配置、
> 安装脚本的收敛。`balancer_by_lua*` 的 cosocket 修复本身在更早一批已经进入公开树,本 kit 不重复交付。

`status: MANUAL_REVIEW`。本 kit 有 3 个 `MANUAL_CLI_REVIEW` 操作,必须逐个人工复核后才动手。
**任何步骤都不要运行触发 CloudFormation 栈更新的命令。**

- `base_sha` = `fae91796206da1d2961d1c5537285278bc0a80f8`
- `patch_sha` = `9e399b7b834822ce02d7dd8f21a0491a9638f113`

range 内只有 `deploy/edge/**` 的 19 个文件,没有别的层被卷进来。两端都在公开仓可解析。

## 先读四条会静默毁掉本次交付的事实

**① `nginx.conf` 是模板,不能逐字节装到在役路径。**
`install-edge.sh` 用 `envsubst` 渲染五个占位符 —— `ENGINE_REDIS_HOST`、`ENGINE_REDIS_PORT`、
`ENGINE_REDIS_READER_HOST`、`ENGINE_REDIS_READER_PORT`、`EDGE_SELF_IP`,其中 **`EDGE_SELF_IP`
每台不同**,所以不存在一份能通用的预渲染制品。把裸模板装上去,在役配置里就会留下字面
`${ENGINE_REDIS_HOST}`,而本次修复恰恰是把坐标写进 `init_by_lua_block` —— 占位符没渲染,
坐标就是字符串,等于修复没生效。

本 kit 因此在**每台上重新渲染**:四个 Redis 坐标取自在役 `claw-edge.service` 的 `Environment`
(那四行 `ENGINE_REDIS_*_HINT` 正是本次修复要删掉的,所以在役旧 unit 上仍在),`EDGE_SELF_IP`
取自 IMDS;渲染后**断言零残留占位符**、且结果里有 `init_by_lua_block`,才允许安装。任何一步取不到
值就 fail loud,不猜。存进 bundle 源的是**未渲染的模板**,因为下次 bootstrap 由 `install-edge.sh`
自己渲染。

**② 文件装完之后才 reload,而且只 reload 一次 —— 守卫是机械的,不是嘱咐。**
`lua_code_cache` 让已启动的 worker 继续用已 `require` 的旧模块,所以文件落盘但未 reload 时
**在役行为完全不变**。这就是原子性保证:部分安装是安全的,风险只在 reload 那一刻兑现。因此
reload 操作会**逐台断言本 kit 装的每一个在役文件都就位**(4 个 lua 的 sha256 + 在役 `nginx.conf`
零残留占位符且含 `init_by_lua_block`)才允许翻。缺任一个,reload 之后立刻
`attempt to call a nil value`,或者坐标变字面量、warmup 又退回无坐标分支。

**③ 新实例继承不了本次修复 —— 这不是待查的问题,是代码里已经定死的事实。**
CDK 把整棵 edge 树打成**一个摘要寻址对象** `deployment/bootstrap/edge/<sha256>/edge-bundle.tar.gz.b64`,
而 LaunchTemplate 的 userdata 里**内联了同一个 sha256 并真的执行 `sha256sum -c`**,校验通过才解到
`/opt/openclaw-edge/<sha256>/`。所以:

- 换 S3 对象的字节**不可能**让新实例继承 —— 摘要不匹配,新实例直接起不来
- 要让新实例带上修复,只有出**新的 bundle 版本 + 新的 LaunchTemplate 版本**,那是部署级动作
- 本 kit 只止在役实例的血。任何扩容、健康检查换机、AZ 重平衡起来的新 edge 仍带缺陷

Step 5 是一个**决定门**:现在就走部署级动作,还是先只止在役的血、把出版本单独排期。它要求你写下
结论,不允许沉默跳过。

**④ 有 4 个文件只进 bundle 源,不改在役行为。**
`install-edge.sh` 与三个 `fluent-bit/` 配置装到 bundle 目录后,在役 openresty 与在役 fluent-bit
**行为不变** —— 前者只在 bootstrap 时执行,后者继续用它当前的配置。而且按第 ③ 条,写 bundle 目录
**也不会**让新实例继承;它只对「有人在这台上重跑 `install-edge.sh`」这一种情况有意义,
userdata 再跑一次会用原始 bundle 覆盖回去。要让在役 fluent-bit 立即换配置是另一件事,
爆炸半径与验证方式都不同,不在本 kit 范围内。

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

Step 5 还需要 LaunchTemplate 的坐标:

```bash
EDGE_LT_NAME="$(aws autoscaling describe-auto-scaling-groups --region "$REGION" \
  --auto-scaling-group-names "$EDGE_ASG" \
  --query 'AutoScalingGroups[0].MixedInstancesPolicy.LaunchTemplate.LaunchTemplateSpecification.LaunchTemplateName' \
  --output text)"
test "$EDGE_LT_NAME" != None || EDGE_LT_NAME="$(aws autoscaling describe-auto-scaling-groups \
  --region "$REGION" --auto-scaling-group-names "$EDGE_ASG" \
  --query 'AutoScalingGroups[0].LaunchTemplate.LaunchTemplateName' --output text)"
export EDGE_LT_NAME EDGE_LT_VERSION="${EDGE_LT_VERSION:-\$Latest}"
echo "LT: $EDGE_LT_NAME @ $EDGE_LT_VERSION"
```

**基线核对**:先确认每台的 bundle 目录能被发现、在役文件当前是什么。bundle 目录是
`/opt/openclaw-edge/<bundle-sha256>/`,**每个版本一个独立目录**,所以本 kit 的每条命令都在目标机上
现场发现它,发现不到就拒绝执行 —— 不要在任何地方写死 `/opt/openclaw-edge/lib` 这类路径。

## Step 1 — 制品上传到 S3(零在役影响,完全可逆)

19 个路径各自 `operations[0].apply_cli` 的前半段:把制品放到**新前缀 `deployment/edge-606/`**,
不覆盖任何现有 bundle 对象。回滚就是删对象。

## Step 2 — 4 个在役 lua 装到两处落点(不 reload)

`lib/hints.lua`、`lib/tenant.lua`、`lib/utils.lua`、`route.lua`。每个都装到
`/usr/local/openresty/lualib/edge/...`(运行时加载路径)与发现出来的 bundle 目录两处。
备份锚点 `.pre-606` 只建一次(文件原本不存在时记 `.absent`),重跑不覆盖 —— 覆盖了备份就是已修补
内容,回滚从此无效。

## Step 3 — `nginx.conf` 每台重新渲染后安装(`MANUAL_CLI_REVIEW`)

见开头第 ① 条。这一条是本 kit 唯一会改在役配置文件内容的操作,复核时请确认三件事:
坐标来源(在役 unit)、`EDGE_SELF_IP` 是每台自己的、以及渲染后零残留占位符的断言在安装之前。

## Step 4 — 一次性 reload(自守卫,`MANUAL_CLI_REVIEW`)

`deploy/edge/nginx.conf` 上那条 reload 操作。它先逐台断言四个 lua 的 sha256 与在役 conf 的两项
判据,再 `openresty -t`、`openresty -s reload`,然后回读 journald **两个**信号:

- 不再有 `API disabled in the context of balancer_by_lua`
- 不再有 `marking ready without probe`

`openresty -t` 可以先跑,但它**不加载 lua 模块** —— 通过不代表 lua 能载入。lua 的加载与运行失败
只出现在 journald 与 `error.log`。

## Step 5 — 新实例继承的决定门(`MANUAL_CLI_REVIEW`,只读)

读 LT userdata,确认它拉的是摘要寻址 bundle 且真的做 `sha256sum -c`(这是第 ③ 条的现场证据),
然后**人工**在 `edge-inherit-gate.txt` 末尾写一行结论,`verify` 只认这两个之一:

- `DECISION: live-only` — 先只止在役的血,出新 bundle/LT 版本单独排期。**代价是从现在起到出版本
  之间,任何新起的 edge 都带缺陷**,请把这条写进值班交接
- `DECISION: reroll-bundle` — 现在就走部署级动作。那超出本 kit 范围,按部署流程另行执行

## Step 6 — 4 个只进 bundle 源的文件

`install-edge.sh` 与三个 `fluent-bit/` 配置。见开头第 ④ 条:**不改在役行为**。

## Step 7 — 10 个测试资产进仓库副本(可跳过)

只更新仓库副本,不触碰在役资源。其中
`deploy/edge/test/integration/balancer_phase_integration.sh` 在**真实 openresty、真实
`balancer_by_lua*` 阶段**下断言不得发起 cosocket —— 这正是原缺陷当年能通过每一道检查的那个缺口
(假 redis 模块模拟不出上下文限制,`openresty -t` 也不触发运行时相位错误)。不保留仓库克隆的客户
可以整组跳过;它的 `apply_cli` 是「缺则装、在则断言」,内容与记录不同就拒绝覆盖。

## 验证

`manifest.json` 的 `verifications[]` 有 8 条,按 `phase` 分三批:

- **Phase A-readonly(只读,6 条)**:`verify-639-no-marking-ready-without-probe`、
  `verify-639-conf-rendered-and-init-block`、`verify-606-no-api-disabled`、
  `verify-606-live-and-bundle-digests`、`verify-edge-new-instance-decision`、
  `verify-edge-bundle-source-digests`
- **Phase B-lifecycle(走真实产品入口,1 条,核心)**:`verify-606-retry-path-selfheal`
- **Phase B-optional(1 条)**:`verify-633-probe-fails-when-cosocket-reintroduced`

**正常请求不进 retry 分支。** 所以「WSS 能连上」「对话正常」这类验证**完全没有验到那个 fix**。
`verify-606-retry-path-selfheal` 是唯一能证明它生效的探针:让某个租户的 route descriptor 指向
不可达的 host(先记下 `host:port`,再让那台 VM 停止),从**外部真实发起** WSS,断言 L1 TTL 内的
下一次请求连到新 peer 且无 `API disabled`。

`verify-633-probe-fails-when-cosocket-reintroduced` 用的是反证:原样跑绿之后,**故意**在 balancer
阶段加一次 cosocket 调用,探针必须**变红**。注入后仍绿,说明这个探针和它要取代的假绿单测一样判不动。

## 陷阱清单

| 陷阱 | 说明 |
|---|---|
| **把 `nginx.conf` 当普通文件装** | 它是模板,五个占位符必须渲染,其中 `EDGE_SELF_IP` 每台不同 |
| **写死 `/opt/openclaw-edge/lib`** | bundle 解到 `/opt/openclaw-edge/<bundle-sha256>/`,每版一个目录,必须现场发现 |
| **以为写了 bundle 源新实例就继承** | 新实例拉的是摘要寻址的 S3 bundle,userdata 内联 sha 校验;换字节它起不来 |
| **`openresty -t` 通过不代表 lua 能载入** | `-t` 不加载 lua 模块。失败只出现在 journald 与 `error.log` |
| **只改 `lualib` 不改 bundle 目录** | 有人重跑 `install-edge.sh` 时会被原始 bundle 内容覆盖回去 |
| **只验正常请求等于没验** | 正常请求不进 retry 分支,验不到那个 fix。必须主动触发 retry |
| **靠 `/healthz` 判 edge 健康** | 本次修的就是「healthy 这个信号不可信」。修复前它在从未探过 Redis 时也返 200 |
| **备份锚点被重跑覆盖** | `.pre-606` 若在第二次施加时被覆盖,备份内容就是已修补版,回滚从此无效 |
| **回滚到更早的 bundle 无效** | 缺陷自 2026-07-21 起就在。回滚到更早版本 = 重新引入同一缺陷 |

## 回滚

每个路径各自的 `rollback_cli` 都先**预检**(断言在役内容仍是本 patch 装上去的那份,否则拒绝动手),
再从 `.pre-606` 还原(原本不存在的按 `.absent` 删除)。四个 lua 与 `nginx.conf` 都还原完之后,用
reload 操作的 `rollback_cli` 做一次 reload 让还原生效。

Step 1 的回滚:删掉 `s3://$ASSETS_BUCKET/deployment/edge-606/` 下的对象。

**注意**:回滚意味着 readiness 门重新恒 fail-open(`/healthz` 又会在没探过 Redis 时返 200)。
只在下发本身出问题(lua 载入失败、`openresty -t` 不过、reload 后起不来)时回滚。

## 本 kit 不修什么

- **`controlUi.allowedOrigins` 的 origin 白名单**。那是 guest 内 openclaw gateway 的校验,
  取值来自 host 开机固化的 `platform.env`,与 edge 无关,改的是 host provisioning
- **Redis route 键的回收**。route 键不会被自动回收,恢复租户靠新写入覆盖
- **guest 出网策略(egress)**。与本缺陷无因果关系
- **新实例继承**。见第 ③ 条,需要新的 bundle 与 LaunchTemplate 版本

## 溯源与测试覆盖

| 项 | 值 |
|---|---|
| 内部源提交 | 逐字节取自内部源的一个提交,文件 mode 同源 |
| range | `deploy/edge/**` 19 个文件,无其它层 |
| 真实相位探针 | `deploy/edge/test/integration/balancer_phase_integration.sh`(随 Step 7 交付) |

上一版本的说明里写着「集成测试尚未纳入覆盖面」——**这一版已经纳入**,并且用注入反证的方式给了
可证伪的验收标准(`verify-633-probe-fails-when-cosocket-reintroduced`)。
