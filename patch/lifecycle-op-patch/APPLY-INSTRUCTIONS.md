# lifecycle-op-patch — apply by reading, no CloudFormation redeploy

`status: MANUAL_REVIEW`。本 kit 有 17 个 `MANUAL_CLI_REVIEW` 操作,必须逐个人工复核后才动手。
**任何步骤都不要运行 CDK 的部署子命令,也不要运行任何触发 CloudFormation 栈更新的命令** —— 这套环境是一次 CDK
部署之后又手工改过很多次的,栈更新会覆盖那些手工改动。

- `base_sha` = `81f3b884ca1226afdb107e7f8392d9334fd33493`
- `patch_sha` = `c9fd494ff4a76929f205f52464047a9185c7c49a`

两端都在公开仓可解析,所以下面每条校验命令你都能自己跑通(spire-agent 那版 kit 记的两个 SHA 只存在于
构建机本地,客户跑不了 —— 从这一版起修掉了)。

## 前置依赖:先施加 `edge-balancer-cosocket-606`,否则 edge 数据面带 P0 缺陷

**本 kit 不修 edge 的 `balancer_by_lua*` cosocket 缺陷(#606),而且携带的 edge 制品本身是缺陷版。**

两个原因叠加:

- `deploy/edge/lib/*.lua` 在本 kit 里属 **`deploy-other` 层** —— `apply_cli` 只
  `install` 到 `$REPO_ROOT`,**不下发在役 edge**。edge 是独立数据面 ASG、bundle 由
  LaunchTemplate 钉 sha,控制面 API 与本 kit 的任何步骤都碰不到它
- 本 kit 的 `patch_sha`(`c9fd494f`)**早于** #606 的修复,所以 `artifacts` 里那几个
  edge lua 是修复前的版本。即使有人手工把它们推到在役 edge,推上去的也是缺陷版

缺陷形态:`balancer.lua` 在 `balancer_by_lua*` 阶段经 `backend.lookup_backend` →
`redis_client.get_route` → `resty.redis:new()` 重查 Redis,而 OpenResty 在该阶段
**禁用 cosocket API**。每次需要选 upstream 都抛
`API disabled in the context of balancer_by_lua*`,upstream 选取失败。

后果:**任何需要新建 WSS 连接的租户全部 502**。已建立的长连接不受影响,所以症状表现为
「只有部分租户连不上」——最容易被误判成租户个体问题或 Redis route 数据错误,
从而把排查方向带到完全错误的地方(实测曾误判为 route stale、guest 出网策略拦截、
openresty 进程停止三个方向,全部排除后才定位到本缺陷)。

处置:**先施加 `patch/edge-balancer-cosocket-606`**,它的 `apply_cli` 真的下发到在役
edge(S3 放制品 → SSM 写 `lualib` 与 `/opt/openclaw-edge` 两处落点 → reload)。
两个 kit 之间没有代码依赖,顺序可换,但**都要做**。

排查这一层时的两个陷阱,先知道能省很多时间:

- **`nginx -t` 不加载 lua 模块** —— `-t` 通过不代表 lua 能载入。加载与运行失败只出现在
  journald 与 `error.log`
- **`systemctl is-active` 可能报 inactive 而进程其实在跑** —— 若 openresty 不经 systemd
  启动(由 `install-edge.sh` 直接拉起 nginx binary),service 状态不反映真实进程。
  判断存活要看 `ps` 与端口监听,不要只看 `systemctl`

## 先读四条会静默毁掉本次交付的事实

**① 死线有【两个】载体,两个都必须落,漏掉 env 会让租户写路径全 5xx。**

上一版这里写的是「改 Lambda 环境变量完全不生效」——**那句话是错的,照它执行会打断业务**。
真实分工(源码为准):

- **env `LIFECYCLE_DEADLINE_SEC_<ACTION>` 决定进程能不能活**。
  `core/create_deadline.py` 的 `deadline_sec_for()` 只读 env:`raw = os.environ.get(_ENV_PREFIX
  + key.upper())`;当 `raw is None` 且 `_require_env()` 为真(判据是 `AWS_LAMBDA_FUNCTION_NAME`
  存在,即「跑在 Lambda 里」)就 **raise**,消息是「死线 env … 未注入而本进程跑在 Lambda 里 ——
  fail-closed 拒绝用默认值继续」。该模块**零 boto3**,所以它**看不见 SSM 参数里的值**
  (函数自己的注释写明了这一点)。**建完八个 SSM 参数并不能救它。**
- **SSM 参数 `/openclaw/lifecycle/deadline-sec/<action>` 决定运行时生效值**,由
  `core/deadline_config.py` 读(进程内缓存 60 秒)。

所以顺序是:**先注入八个 env,再建八个 SSM 参数,然后才做 Lambda overlay**。CDK 平时两边一起
建(`deploy/stacks/lambdas.py` 从 `config.yml` 的 `lifecycle.deadline_sec` 同源注入 env、并创建
同名 SSM 参数;该段注释写明「缺段/缺项时两个载体都不建/不注入」)。客户的 `config.yml` 里没有
`lifecycle.deadline_sec` 段时,在役 Lambda 上**一个 env 都没有** —— overlay 一落地就踩这个坑。

**故障形态:冷启动就挂,每一条路由都 502 —— 不是「只有写路径炸」。** 真机捕获的是模块导入链,
不是懒加载:`handler.py:14` import `tenant_query_service` → `tenant_service:43` import
`core.create_deadline` → `create_deadline.py:1058` 在**模块作用域**跑 `assert_deadline_config_sane()`
→ `:640` 调 `deadline_sec_for(action)` → `:593` `raise ValueError`。所以 handler 根本装不起来,
实测 `GET /system/info` 与 `POST /tenants/{id}/rebuild` **同时 502**。源码里那句「懒加载,读路径仍 200」
是**过时注释**,一手运行时输出优先。

**受影响的是 5 个函数,不止 openclaw-api**(真机实测各 +8 个 env):`openclaw-api` 73→81、
`openclaw-lifecycle-consumer` 68→76、`openclaw-backup` 7→15、`openclaw-health-check` 12→20、
`openclaw-scaler` 15→23。其中 `openclaw-lifecycle-consumer` 与 `openclaw-api` 是**同一个包**
(实测 CodeSha256 完全相同),必须一起注入、一起 overlay;而**只有 `openclaw-api` 有 `live` 别名**,
另三个没有 —— 给无别名函数套别名流程会失败或只改一半。

**决定性依据:这 8 个 env 本来就写在本 kit 自带的 CFN 闭包里。** `resources/cloudformation/` 两侧
按函数逐 env 比对,`ApiHandler5E7490E8` 的八个 `LIFECYCLE_DEADLINE_SEC_*` 在 base 侧全 `null`、
patch 侧全有值 —— 是 kit 把 CDK 变更改写成手工 CLI 时漏掉了这一项。

注入命令(八档一次写完;`start` 与 `restart` 同 180 档,只列七个的清单是不完整的):

```bash
# 逐个动作按权威默认值拼 env(create/suspend/restore/restart/start/rebuild=180,backup/delete=600)
ENVJSON=$(python3 - <<'PY'
import json
d={"create":180,"suspend":180,"restore":180,"restart":180,"start":180,"rebuild":180,
   "backup":600,"delete":600}
print(json.dumps({f"LIFECYCLE_DEADLINE_SEC_{k.upper()}":str(v) for k,v in d.items()}))
PY
)
# 与现有 env 合并,绝不整体覆盖 —— update-function-configuration 的 Variables 是全量替换语义,
# 直接传这八个会把在役的其余 env 全部删掉(那会让函数彻底起不来)。
CUR=$(aws lambda get-function-configuration --function-name openclaw-api --region "$REGION"         --query 'Environment.Variables' --output json)
echo "$CUR" > prev-api-env.json          # 回滚锚点:改之前的完整 env
MERGED=$(python3 -c 'import json,sys;a=json.load(open("prev-api-env.json"));a.update(json.loads(sys.argv[1]));print(json.dumps({"Variables":a}))' "$ENVJSON")
aws lambda update-function-configuration --function-name openclaw-api --region "$REGION" --environment "$MERGED"
aws lambda wait function-updated --function-name openclaw-api --region "$REGION"
# 断言八个都在【且值正确】—— 只查键存在是假绿(codex review 指出);逐键比对期望值,任一不符退 1
aws lambda get-function-configuration --function-name openclaw-api --region "$REGION" --query 'Environment.Variables' --output json | python3 -c '
import json,sys
v=json.load(sys.stdin)
want={"CREATE":"180","SUSPEND":"180","RESTORE":"180","RESTART":"180","START":"180","REBUILD":"180","BACKUP":"600","DELETE":"600"}
bad=[f"LIFECYCLE_DEADLINE_SEC_{k}({v.get(\"LIFECYCLE_DEADLINE_SEC_\"+k)!r}!={val!r})" for k,val in want.items() if v.get("LIFECYCLE_DEADLINE_SEC_"+k)!=val]
print("BAD:",bad); sys.exit(1 if bad else 0)'
```

> **回滚锚点要连别名一起存,不只 env。** `update-function-configuration` 的 `Variables` 是全量替换,
> 上面已用 `prev-api-env.json` 存了改前的完整 env;但真正会被别名流量看到的是 Step 3 publish 出的
> 那个 version。Step 3 的 overlay 操作用 `RevisionId` CAS 绑定 publish 与别名翻转(见该步 `apply_cli`),
> 回滚必须同时还原 `$LATEST` 与 `live` 别名指向 —— 只还 env 不还别名,别名流量仍停在坏 version 上。
> **权威的「env 真的进了在役 version」判据不是查 `$LATEST`,而是 Step 3 翻转别名前对新 published
> version 直接 `invoke` 一次,断言 `FunctionError` 为空、响应里的八档值正确**(见 Step 3 verify)。

**env 改的是 `$LATEST`,而流量走 `live` 别名 → 已发布 version(其 env 是冻结的)。** 所以注入
env 之后**必须重新发版并翻转别名**,新 version 才带上这八个 env —— 这正好与第 3 步的 overlay
发版是同一次动作,把 env 注入放在 overlay **之前**就能一次发版同时带上代码和 env。顺序颠倒
(先 overlay 发版、后注入 env)会让新 version 缺 env,租户写路径立刻开始 5xx。

`create-deadline-config.py --live` 比对的是 `$LATEST` 的环境变量,**它的绿既不能证明死线生效
(生效值在 SSM),也不能证明在役 version 带了 env(在役是别名指向的那个 version)**。
八个动作是 `create` / `suspend` / `restore` / `restart` / `start` / `rebuild`(各 180 秒)与
`backup` / `delete`(各 600 秒)。**`start` 容易被漏** —— 它与 `restart` 同档(同一条通道、同为不含
数据步骤的动作),只列七个的清单是不完整的。

**①c `backup-data.sh` 必须装成 0755,照 manifest 的 `install -m 0644` 会让这台 host 完全没有备份。**
本 kit 内部有一处自相矛盾:manifest 的 `apply_cli` 是 `install -m 0644`(忠实沿用源码 git mode),
但同 kit 下发的 `host-agent.py` 在 `:2552` 有 `os.access(_BACKUP_SCRIPT, os.X_OK)` 检查,**要求执行位**
—— 两者必然冲突。真机后果(每分钟一条持续 8 分钟):`backup: REFUSING to run … NO local backups are
happening on this host`,并把所有 `suspend` 打成 `suspend_fail_reason: "backup_failed"`。

**这条错误消息是误导的**:它说「does not contain all required sentinels」,但三个哨兵
(`oc_flush_guest`/`OC_BACKUP_SOURCE_ABSENT`/`_RUN_ID`)在 host 上那份文件里全部存在,文件摘要也正
等于 manifest 的 `patch_sha256`(Step 3 的 `sha256sum -c -` 断言 17/17 过)。所以「脚本旧了」的结论
是错的,照消息去重推没用 —— 重推出来还是 0644。真正的变量只有权限位:

```
before: -rw-r--r--  os.access(X_OK)=False  → NO local backups are happening on this host
chmod 0755 → after: -rwxr-xr-x  os.access(X_OK)=True  → backup: 2/2 tenant(s) backed up
```

**本 kit 的 manifest 已把这一条的 `install -m` 修成 `0755`(durable,不再是事后 chmod 绕过)**,
`backup-data.sh` 的 `verify_cli` 也加了 `test -x` 逐 host 断言。照 manifest 的 `apply_cli`/`verify_cli`
正常走即可,施加后确认下一个备份轮次(~60s)journal 里不再有 `REFUSING`。这是本文件里唯一一条**故意
不沿用源码 git mode(0644)**的安装位——因为 host-agent 用 `os.X_OK` 检查它;其余 host 脚本
(`launch-vm.sh`/`host-agent.py`)仍是 0644,那些经 `bash`/`python3` 调用不需要执行位。命中时
`GET /hosts`、`status` 全正常,只有 host 侧 journal 与 `openclaw_backup_script_stale` 指标能看出来。

**①b 这个 fail-closed 的报错会把自己藏起来,不要按 502 的字面去查。**
真机捕获到:`awslambdaric` 回传 init error 时按 latin-1 编码 HTTP body,中文 `raise` 消息触发
`UnicodeEncodeError: 'latin-1' codec can't encode characters … Body ('死线') is not valid Latin-1`,
真因被掩盖成 `Runtime.ExitError` / HTTP 502 —— 日志里看不到「死线 env 未注入」这句话。**所以判据
不是读 502 的错误文本,而是直接查在役 version 上八个 env 的值**(逐值比,不是数个数 —— `grep -c`
无论多少都退 0,是假绿):

```bash
# 在役 version(别名指向的那个)上查,不是查 $LATEST
LIVEV=$(aws lambda get-alias --function-name openclaw-api --name live --region "$REGION" --query FunctionVersion --output text)
aws lambda get-function-configuration --function-name "openclaw-api:$LIVEV" --region "$REGION" --query 'Environment.Variables' --output json | python3 -c '
import json,sys
v=json.load(sys.stdin)
want={"CREATE":"180","SUSPEND":"180","RESTORE":"180","RESTART":"180","START":"180","REBUILD":"180","BACKUP":"600","DELETE":"600"}
bad=[k for k,val in want.items() if v.get("LIFECYCLE_DEADLINE_SEC_"+k)!=val]
print("missing/wrong:",bad); sys.exit(1 if bad else 0)'
```

拿到 `Runtime.ExitError` / 502 且这条计数不是 8,就是本条,不是别的故障。

**②b `POST /hosts/egress` 的 403 预警是错的,真机实测返 202;真问题是反过来的 —— 那个 202
证明不了 RBAC 通过。**

上一版这里写「api-key 路径的 role 会被解析成 `viewer`,门口就被挡成 403」。**这句说反了。**
api-key **本身不携带角色**:无 Bearer 时角色取自 `default_no_jwt_role`,有 Bearer 时取自
`cognito:groups`。所以在 `default_no_jwt_role: operator` 的环境里,**任何** api-key 都已经是
operator,`POST /hosts/egress` 实测返 **202**。

反过来这带出一个真问题:既然不带 token 也能过门,那 **202 就不构成「RBAC 验证过了」的判据**。
kit 自己的 `lib/discover-env.sh` 会把 `role_identity` 报成 `BLOCKED`,正是这个意思。所以
`verify-egress-fleet` 在这种环境**仍记 `MANUAL_CLI_REVIEW`,但理由是「门太松,202 不构成判据」,
不是「被 403 挡住测不了」**。要真验 RBAC,得在 `default_no_jwt_role` 不是 operator 的环境里、
用一个明确低于 operator 的身份打同一条路由,看它是否被拒。

**② 八档里只有 `create` 有权威的最坏执行值(128 秒)。**
`suspend/restore/restart/start/rebuild/backup/delete` 这七个目前**没有下界守护**。往小调可能小于该
操作单次最坏执行,于是判死之后 SSM 还在跑,留下没人认领的 microVM(占容量且计费)。不要为了
"更快收敛"下调。

**③ 本次把可观测性资产的分发从部署脚本搬到了 CDK,而 kit 不允许跑 CDK 部署。**
部署脚本里 12 处 `_obs_upload` 在 `patch_sha` 上归零,替代它的 10 个 BucketDeployment 自定义资源
**建不出来**。桶里现有对象还在(旧部署脚本传过),所以 host 照常起 —— 这是**潜伏**缺陷:
**以后这批资产再变,就没有任何自动分发路径了**。第 4.3 步给了手工等价物,请记进运维手册。

**④ workspace 的 7 个身份文件与运维类 skill 现在烤在只读镜像盘上,【下发文件的方式更新不了它们】。**
`SOUL.md` / `AGENTS.md` / `IDENTITY.md` / `HEARTBEAT.md` / `COMMUNICATION_STYLE.md` / `TOOLS.md` /
`USER.md` 与 `~/.openclaw/skills/` 一起以只读方式挂载覆盖到 workspace 上,microVM 内即使 root 写入
也会拿到 `EROFS` —— 写请求在到达文件之前就被虚拟设备拒了,这是防篡改设计,不是配置问题。
所以要换身份文件只有一条路:**重建镜像 → 让机队拿到新镜像 → 用 `action: "restart"` 的滚动升级让新盘
生效**。用 `rebuild` 换身份文件是不必要的重操作(它重建整个 microVM 且需要管理员权限)。
如果按"推一份新文件上去"的老习惯操作,会静默地什么都没变。

## Step 0 — DISCOVER(只读)与制品真伪

```bash
bash lib/discover-env.sh "$REGION"
```

`region` 是必填参数,而且这个脚本**自己**写 `environment.json`(写在 kit 根)——**不要重定向它的 stdout**,那会把它要写的文件清空。

`environment.json` 落地后,后面每一步都从它取坐标,不要手打。然后逐个证明制品 == 锚定树:

```bash
python3 - <<'PY'
import json, hashlib, subprocess, sys, pathlib
m = json.load(open("manifest.json"))
bad = 0

# ① 每个 shipped 制品:kit 内文件 == manifest 记录 == 锚定树上的内容,三者必须一致
for path, pv in m["paths"].items():
    art = pv.get("artifact")
    if not art:
        continue
    want = pv["patch_sha256"]
    got = hashlib.sha256(open(art, "rb").read()).hexdigest()
    src = subprocess.run(["git", "show", f"{m['patch_sha']}:{path}"], capture_output=True)
    ref = hashlib.sha256(src.stdout).hexdigest() if src.returncode == 0 else "unreadable"
    if not (want == got == ref):
        print("ARTIFACT MISMATCH", path, want[:12], got[:12], ref[:12]); bad += 1

# ② lib/ 下每个工具的哈希(它们直接驱动生产变更,被改过一定要看得见)
for rel, want in (m.get("kit_files") or {}).items():
    want = want if isinstance(want, str) else want.get("sha256")
    f = pathlib.Path(rel)
    got = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "missing"
    if got != want:
        print("KIT FILE MISMATCH", rel, str(want)[:12], got[:12]); bad += 1

# ③ CloudFormation 闭包快照的哈希(第 4 步逐资源决策全靠它)
for st in (m.get("cloudformation") or {}).get("stacks", []):
    for side in ("base_template", "patch_template"):
        decl = st.get(side) or {}
        rel, want = decl.get("artifact"), decl.get("sha256")
        if not rel:
            continue
        f = pathlib.Path(rel)
        got = hashlib.sha256(f.read_bytes()).hexdigest() if f.is_file() else "missing"
        if got != want:
            print("CLOSURE MISMATCH", rel, str(want)[:12], got[:12]); bad += 1

# ④ IAM 策略:钉住摘要(它会被 put-role-policy 装进生产,只解析不够 —— 改过的策略照样能解析)
IAM_SHA = "4633b1a0cea733f83db92bc9bbb679af90c7cff06d82f82141f6942321b021e8"
pol = pathlib.Path("iam/lifecycle-deadline-read.json")
if not pol.is_file():
    print("IAM POLICY MISSING", pol); bad += 1
else:
    got = hashlib.sha256(pol.read_bytes()).hexdigest()
    if got != IAM_SHA:
        print("IAM POLICY MISMATCH", got[:12], IAM_SHA[:12]); bad += 1
    else:
        try:
            json.loads(pol.read_text())
        except Exception as exc:
            print("IAM POLICY UNPARSEABLE", exc); bad += 1

print("mismatched:", bad)
sys.exit(1 if bad else 0)
PY
```

### Step 0b — `$HOST_IDS` 的取法(manifest 的命令里用了 27 次,此前哪儿都没给)

**不要裸 `dynamodb scan openclaw-hosts` 取 id。** 这张表里除了真实 host 行,还存着几行
**singleton 伪行**作为机队级期望态:`__fleet_egress_policy__`、`__route_reclaim_state__`、
以及 `__egress_rev__` 前缀的多条修订行(`services/egress_admin_service.py:603`
`_REV_PREFIX = "__egress_rev__"`)。裸 scan 会把这些伪行当成 instance-id 喂给
`ssm send-command`,那一整条命令会失败,或者更糟 —— 部分成功后你以为全机队都下发了。

按 tag 取,不从表里取(机队枚举的权威是 tag,不是 ASG 也不是这张表):

```bash
HOST_IDS=$(aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Role,Values=metal-host" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
echo "HOST_IDS=$HOST_IDS"
test -n "$HOST_IDS" || { echo "no running metal host found — stop, do not proceed" >&2; exit 1; }
# 与账本对账:排掉 __ 前缀伪行【也排掉非在役状态行】(仅比 status/state=active 的),
# 否则历史 deleted/terminated 行会混进来让对账假报不一致
aws dynamodb scan --table-name openclaw-hosts --region "$REGION" --output json \
  | python3 -c 'import json,sys
rows=json.load(sys.stdin)["Items"]
def alive(r):
    s=(r.get("status") or r.get("state") or {}).get("S","")
    return s in ("active","running","ready","") 
ids=[r["instance_id"]["S"] for r in rows if not r["instance_id"]["S"].startswith("__") and alive(r)]
print(" ".join(sorted(ids)))'
# 两边不一致就先查清楚(可能有 host 失联或刚扩容),不要带着差异往下发命令
```

**每条 `ssm send-command` 之后都要逐个校验结果**,不能只看 `send-command` 自己返回成功 ——
它只表示「命令已投递」。manifest 里的 `verify_cli` 用的是 `ssm wait command-executed` 加逐 instance
读 `get-command-invocation` 的 `ResponseCode`,照那个走。下发前先 `ssm describe-instance-information`
确认每台 `PingStatus=Online`;机队 **> 50 台**时 `--instance-ids` 有上限,要么分批 50 台,要么改用
`--targets Key=tag:Role,Values=metal-host`。

### Step 0c — `deploy-other` 层只改仓库副本,不下发在役(78 个文件)

这一层(含 `console-bff` 与 `deploy/edge/*.lua`)的 `apply_cli` 形态是
`s3cp=False send-command=False install=False` —— 它们只把文件装到**部署机的仓库副本**
(`$REPO_ROOT/...`),**不碰任何在役资源**。对照 `B-s3` 那 9 条是
`s3cp=True send-command=True install=True`(进桶 + 下发 host + 落盘)。这 8 条只进桶的
(`migrate-vm.sh` / `oc-egress-chain.sh` / `oc-egress-sim.py` / `provision-host.sh` /
`required-scripts.list` / `spire-kit/guest/agent.conf.tmpl` / `spire-join-broker.py` /
`sync-shared-skills.py`)形态是 `s3cp=True send-command=False install=False`:**它们的
`apply_cli` 不触在役 host,但不是「零风险」** —— `aws s3 cp` 会改 assets 桶里的对象,而这些对象会被
**下一次开机 / reconcile / 新机** 取用(例如 `oc-egress-*` 在实际下发时会重新从 S3 下载),所以属于
**延迟生效**而非无副作用。覆盖前记 `VersionId` 作回滚锚点、先传临时 key 复验再 promote(与 Step 4.3
的 obs 资产同一套做法)。要复核的是「要不要额外手工落 host + 何时会被自动取用」,不是「这条命令敢不敢跑」。

后果说清楚:**文档里那几条 edge 修复,只做完 `deploy-other` 是不会在役生效的。** edge 是独立
ASG,它的 lua bundle 由自己的启动模板钉住 bundle 摘要,不走 `deployment/scripts/` 那条 host 分发面。
要让 edge 侧真生效,得走 edge 自己的 bundle 发布 + LT 版本 + 实例替换那条路,**本 kit 不含那条路**。
所以这一层的正确期望是:**把仓库副本对齐,让下一次正常的 edge 发布带上它们**;需要立刻在役生效
的,单独走 edge 发布流程并另行验证。这条不是缺陷,是交付面边界 —— 但上一版没写明,容易被读成
「照文档做完 edge 就修好了」。

### Step 0c-2 — `apply-api-routes.sh verify` 的 CORS FATAL 是假红,不要据此回滚

Step 4.2 的 `verify` 会报 `FATAL: OPTIONS /hosts/egress CORS config differs from spec`,而 `GET`/`POST`
同时 PASS。差异全部是同一组 4 个键,且它们都是 **API Gateway 服务端补的默认值**、spec 里没写:
`cacheKeyParameters` / `cacheNamespace` / `passthroughBehavior` / `timeoutInMillis`。

**决定性判据**:把同一比较逻辑用在**从未被本 kit 触碰的 CDK 原生路由**(`/hosts`、`/tenants`)上,
同样报 differs、命中同一组 4 个键;而本 kit 建出来的 OPTIONS 与 CDK 原生形态逐字一致(都是
`methodResponses=['204']` + `MOCK` + `integrationResponses=['204']`)。**独立复现**:对 6 条新路由各发一次
真实 `OPTIONS` 预检,6/6 返 204 且三个 `Access-Control-*` 响应头逐字节等于 spec 声明。所以 CORS 行为
是对的,错的是校验器的字段比较。

**处置**:拿到这条 FATAL **不要回滚**(会回滚掉一个其实正确的变更)。自证之后在回执里记「CORS FATAL
判定为校验器缺陷,已用真实预检反验 6/6 204」,并**保留不跑 `finalize`**以留住回滚能力,直到校验器修好。

### Step 0d — `lib/apply-cfn-resources.sh` 是只读评审器,不是写工具

它对**整个闭包**做一次逐资源评审输出,所以 6 条 D-cdk 路径的 `apply_cli` 是**同一条命令** ——
跑一次即可,不要按路径跑 6 遍(结果完全相同,只是刷屏 6 遍)。它**不发任何 AWS 写调用**
(全脚本里 `aws` 只出现在第 34 行的 `command -v aws` 存在性检查)。`plan` 与 `rollback`
**故意退 25**(脚本注释:nothing was applied, so a driver must not read this as done),
`verify` 退 0 但**不读任何在役资源**。所以它的绿不能当验证门;真正的施加命令在 Step 4.1/4.2/4.3。

它需要 **bash 4+**(用了 `mapfile`)。macOS 自带 bash 3.2 会 `exit 3` 并打印
`FATAL: bash ... is too old` —— 那不是 kit 坏了,换 `brew` 装的 bash 或在 Linux 上跑。

摘要是 SHA-256。**四类都要过**:shipped 制品、`lib/` 工具、CloudFormation 闭包快照、IAM 策略(摘要钉死在脚本里)——
只核制品是不够的,后三类同样直接驱动生产变更(`lib/` 里的脚本会改机队,闭包快照决定第 4 步逐资源
怎么决策,IAM 策略是第 2 步的 fail-closed 前置)。任何一条不等就停下,不要继续 —— 那说明 kit 被
重新打包过或下载不完整。

## Step 1 — 备份(每个操作的 backup 必须先成功)

```bash
for pair in "api openclaw-api" "backup openclaw-backup" "health_check openclaw-health-check" "scaler openclaw-scaler"; do
  set -- $pair; d=$1; fn=$2
  # 锚点只建一次:重跑这一步若覆盖,备份就变成【已打补丁】的内容,回滚从此无效
  [ -f "backup-version-$d.txt" ] || aws lambda get-alias --function-name "$fn" --name live --region "$REGION" --query FunctionVersion --output text > "backup-version-$d.txt" 2>/dev/null || true
  [ -f "live-$d.zip" ] || aws lambda get-function --function-name "$fn" --region "$REGION" --query Code.Location --output text | xargs curl -s -o "live-$d.zip"
done
aws ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --region "$REGION" --query 'LaunchTemplateVersions[?DefaultVersion==`true`].VersionNumber' --output text
```

**锚点一律只建一次**(上面每条都带 `[ -f … ] ||`):重跑备份步骤若覆盖锚点,备份就成了已打补丁的
内容,回滚从此无效 —— 这是最容易在「重试一下」时悄悄毁掉的东西。**四个函数都要单独备份** —— 本 kit 替换的是 `openclaw-api` / `openclaw-backup` /
`openclaw-health-check` / `openclaw-scaler` 四个。**只有 `openclaw-api` 有 `live` 别名**,
所以只有它需要备份别名版本号(回滚要回到别名当时真正在服务的那个版本);另外三个只需备份在役包。
上面的循环对无别名的函数会在 `get-alias` 处报错,那是预期的,忽略即可。
机队会有版本漂移,**按 host 分别备份**,让每台各自回到自己的版本。

## Step 2 — 先补授权(fail-closed 前置,不能放到后面)

`api` 与 `lifecycle-consumer` 两个角色都要能读死线参数前缀;现有的 dispatch 前缀授权**不覆盖**它。
漏了这一步会把一个软问题变成"读不到参数一路回落默认值,而日志上看不出来"。

> **⚠ 本步给的授权不完整 —— 本 kit 的 CFN 闭包要求的其余授权在役全部缺失。** 真机逐条对比
> (探针先用两条已在役的语句自证不是假 0)的结果:除下面这条 `deadline-sec` 读权限,其余 patch 期望
> 的授权全是 ABSENT —— `api cloudwatch:PutMetricData(OpenClaw/Dispatch)`、
> `api/backup/health/scaler ssm:ListCommandInvocations`、`health ssm:DescribeInstanceInformation`、
> `health cloudwatch:PutMetricData`、`api/backup sqs:SendMessage(DeadLetter)`,以及两条 **Deny**:
> `host dynamodb:UpdateItem` 对 `egress_pinned` 与 `__fleet_egress_policy__`。
>
> 失效形态是**不报错不告警**:调未授权的 API 拿 `AccessDenied` 落进「本轮跳过」分支,日志不红指标不动。
> 真机已取到因果证据:补齐授权后 `openclaw-health-check` 的指标**首次流出**,此前一直静默为空。
> 那两条 `Deny` 更要紧 —— 本 kit 把机队出网期望态放进 `openclaw-hosts` 的 singleton 行(见 Step 0b),
> 这两条 Deny 就是防被管机器反向篡改整个机队期望态的闸;只装代码不装 Deny = 新增高价值写入目标却没装保护。
>
> **比对时必须连 `Resource` 与 `Condition` 一起比**:`api cloudwatch:PutMetricData` 有一条 PRESENT,
> 但其 Condition 限定 `cloudwatch:namespace = OpenClaw/ControlPlane`,与 patch 要的 `OpenClaw/Dispatch`
> 不是同一条 —— 只比 action 名会判成「已授权」。处置:按 `resources/cloudformation/` 两侧闭包把每条语句
> 逐一 `put-role-policy` 再 `get-role-policy` 回读,结论写进 `cfn-verify-receipt.txt`。

```bash
aws iam put-role-policy --role-name "$API_ROLE" --policy-name oc-lifecycle-deadline-read --policy-document file://iam/lifecycle-deadline-read.json
aws iam put-role-policy --role-name "$LIFECYCLE_CONSUMER_ROLE" --policy-name oc-lifecycle-deadline-read --policy-document file://iam/lifecycle-deadline-read.json
```

`iam/lifecycle-deadline-read.json` 里的资源 ARN 写成 `arn:aws:ssm:*:*:parameter/openclaw/lifecycle/deadline-sec/*`
——它只覆盖死线这一个前缀,但 region/account 用了通配。CDK 那边是按本区本账号收窄的,所以**建议你
先把两个通配替换成自己的 region 与 account id 再 put**;保持通配也只是读自己这个前缀,不扩大到
`/openclaw/*`(那会顺带让该角色能递归读 dispatch 的密文清单)。

只读授权,`rollback_policy: RETAIN` —— **不要回滚它**:撤掉会让已回滚的旧代码也读不到参数。

## Step 3 — 热修在役机器(先恢复服务,后管未来机器)

按 `manifest.json` 里 `layer: B-s3` 的 13 个路径,把 `host-scripts/<rel>.patched` 推到桶再拉到 host。
**落地路径不是一律 `/home/ubuntu`**:`host-agent.py` / `route_ops.py` / `oc-guest-log-reader.py` 落
`/opt/openclaw/`,其余落 `/home/ubuntu/`(`lib/*` 落 `/home/ubuntu/lib/`)。权威来源是 `init-host.sh`
里那些 `aws s3 cp … <目标路径>` 行,每条 `apply_cli` 已按它逐个生成。
**`host-agent.py` 改完必须 `systemctl restart host-agent.service`** —— 服务跑的是
`/opt/openclaw/host-agent.py`(见其 unit 的 `ExecStart`),只换文件不重启,在役进程仍跑旧代码。

两个执行细节写进了每条 `apply_cli`,照抄就行:
- `AWS-RunShellScript` 的 shell 是 **`/bin/sh`**,**不能用 bash 进程替换**(`< <(...)`);
  下发内容要么先 `aws s3 cp` 到 `/tmp` 再 `install`,要么走纯 POSIX 写法。
- **install 与 restart 必须在同一条 `send-command` 里**,分两条是异步的,restart 可能先于 install 落地。
  下发后用 `ssm wait command-executed` + `get-command-invocation` 逐机核 `Status`,不要发完就算完。
- 每机在覆盖前先留 `<dest>.pre-patch`(用 `cp -p`,连权限位一起留):**新增文件在桶里没有旧版本**,
  回滚只能靠这份每机备份(原本不存在的则直接移除),否则会留下混版机队。
- **权限位沿用源码,不一律写 0644**:源码里 `delete-vm.sh` / `rebuild-vm.sh` / `reset-vm.sh` /
  `migrate-vm.sh` / `delete-all-vms.sh` / `oc-egress-chain.sh` / `oc-egress-sim.py` 是 `0755`,
  `backup-data.sh` / `launch-vm.sh` / `host-agent.py` 等是 `0644`。init-host.sh 的调用点全是
  `sh …` / `bash …`(所以 0644 对那些够用),但把 0755 的那批降成 0644 会打断任何**直接执行**的
  调用点。每条 `apply_cli` 里的 `install -m` 已按源码取值。
- **校验要断言,不能只打印**:每条 `verify_cli` 用 `echo "<manifest 里的 patch_sha256>  <dest>" |
  sha256sum -c -`,摘要不符即非零退出;只 `sha256sum` 打印一行等于没验。
- **回滚前先证在役内容仍是本 patch**:不是就 fail-closed 停下 —— 否则会把之后更新的部署覆盖掉。
每个路径的 `operations[0].apply_cli` 就是该文件的确切命令,`verify_cli` 是它的校验命令。
主机通常在私有子网:命令写成 `ssh/scp` 便于阅读,实际走 SSM(`send-command`;传文件用 base64)。

Lambda 走 **overlay**(复用在役包里的依赖,不要预打包 zip —— 那会把构建机的依赖版本焊到客户函数上)。
**归档根必须是函数目录本身**:`lambda/api/handler.py` 里那个 `handler.py` 就是函数入口,打包时
**不能把 `api/` 这一层带进归档**,否则入口变成 `api/handler.py`,函数一上线就 import 失败。四个函数
目录 `api` / `backup` / `health_check` / `scaler` 各自的根里都有 `handler.py`。逐个函数:

```bash
# 目录 → 真实函数名(health_check 目录对应连字符名)。**只有 openclaw-api 有 live 别名**
d=api; fn=openclaw-api
OLDREV=$(aws lambda get-function --function-name "$fn" --region "$REGION" --query Configuration.RevisionId --output text)
aws lambda get-alias --function-name "$fn" --name live --region "$REGION" --query "[FunctionVersion,RevisionId]" --output text > "backup-alias-$d.txt"
rm -rf "work-$d" && mkdir "work-$d" && (cd "work-$d" && unzip -q "../live-$d.zip")
cp -a "lambda/$d/." "work-$d/" && (cd "work-$d" && zip -qr "../overlay-$d.zip" .)
unzip -p "overlay-$d.zip" handler.py > /dev/null
aws lambda update-function-code --function-name "$fn" --region "$REGION" --zip-file "fileb://overlay-$d.zip" --revision-id "$OLDREV"
aws lambda wait function-updated --function-name "$fn" --region "$REGION"
NEWREV=$(aws lambda get-function --function-name "$fn" --region "$REGION" --query Configuration.RevisionId --output text)
NEWV=$(aws lambda publish-version --function-name "$fn" --region "$REGION" --revision-id "$NEWREV" --query Version --output text)
WANT=$(openssl dgst -sha256 -binary "overlay-$d.zip" | base64)
GOT=$(aws lambda get-function --function-name "$fn:$NEWV" --region "$REGION" --query Configuration.CodeSha256 --output text)
[ "$WANT" = "$GOT" ] || { echo "CodeSha256 mismatch — do not flip the alias" >&2; exit 1; }
aws lambda update-alias --function-name "$fn" --name live --region "$REGION" --function-version "$NEWV" --revision-id "$(cut -f2 "backup-alias-$d.txt")"
```

**另外三个函数没有 `live` 别名**(实测该栈里只有 `ApiHandler` 有 Alias 资源),所以
`openclaw-backup` / `openclaw-health-check` / `openclaw-scaler` **直接更新 `$LATEST` 即可,
不要发版、也没有别名可翻**;校验同样用 `CodeSha256`(对不带限定符的函数查),回滚就是把
`live-$d.zip` 重新覆盖回去。给它们套别名流程会直接失败或只改了一半。

顺序上的三条讲究:

- **`update-function-code` 会改 `RevisionId`**,旧 revision 只能给它自己做 CAS;`publish-version`
  必须**重新取**新 revision,否则会在 `$LATEST` 已改之后才失败。
- **别名翻转也要 CAS**:带上翻转前读到的别名 `RevisionId`,否则会覆盖掉并发的另一次部署。
- **不要用 `invoke` 做校验**:`backup` / `health-check` / `scaler` 被 `{}` 唤起会真的跑它们的
  生产工作流(scaler 会动机队)。用 `CodeSha256` 比对 —— Lambda 就是按 `base64(sha256(zip))`
  算的,零副作用且显式失败(`--query FunctionError` 即使 handler 报错也退出 0,不能当门)。

若该环境还部署了共用同一份 api 包的消费者(例如生命周期消费者),对它做**同样的** overlay。
`discover-env.sh` 只报控制面 API 那一条链,**不出通用 Lambda 清单**,所以自己列:

```bash
aws lambda list-functions --region "$REGION" --query 'Functions[?starts_with(FunctionName,`openclaw-`)].FunctionName' --output text
aws lambda get-function-configuration --function-name openclaw-api --region "$REGION" --query Role --output text | awk -F/ '{print $NF}'
```

第二条同时给出 Step 2 要授权的角色名 —— **注意 `--query Role` 返回的是 ARN**,`put-role-policy --role-name` 要的是**名字**,所以要取 ARN 的最后一段(上面的 `awk -F/` 就是干这个的);
若列表里出现别的消费者,对它各跑一次同样的 `Role` 查询,
两个角色都要授。列不出来或拿不准就**停下问**,不要猜函数名。


先解在役包再覆盖,未改动的模块与依赖因此原样保留。**校验一律用 `CodeSha256` 比对,不要 `invoke`** ——
理由见下面那条:`backup` / `health-check` / `scaler` 被唤起会真的跑生产工作流。

## Step 4 — CDK 变更改走手工 CLI(逐个复核,绝不 stack update)

`resources/cloudformation/` 里是 `base_sha` 与 `patch_sha` 两侧的完整合成模板,60 项资源变更在
`manifest.json` 的 `operations[].resource_refs` 里被逐个拥有一次。通用入口:

```bash
lib/apply-cfn-resources.sh plan resources/cloudformation "$REGION"
```

它逐个资源打印 before/after 与所需的人工决策,然后**停下**等你判断 —— 它不会替 `AWS::IAM::Policy`
和 `AWS::CodeBuild::Project` 编一条通用命令。

**它的 `verify` 按设计只打印「该核对什么」并退出 0,不读任何在役资源**(源码注释原话:reporting is
its whole job)。所以**不能把它单独当验证门** —— 自动化会把「没核过」读成「已核过」。这些操作的
`verify_cli` 因此额外要求一份人工核验回执:你按它列出的每个资源真去 `describe` 过之后,把资源名与
结论写进 `cfn-verify-receipt.txt`,验证命令会检查该文件里有对应条目。

三类必须单独说明。

**4.1 八个死线 SSM 参数** —— 平时由 CDK 创建,这里自己建;`put-parameter` 幂等,先 `get-parameter`
区分"新建"还是"接管已有"(决定回滚是删除还是保留)。注意动作清单里**必须包含 `start`**(与 `restart`
同 180 秒档),漏掉它会留下一个没有死线的动作:

```bash
for a in create suspend restore restart start rebuild backup delete; do
  case "$a" in backup|delete) v=600 ;; *) v=180 ;; esac
  if CUR=$(aws ssm get-parameter --name "/openclaw/lifecycle/deadline-sec/$a" --region "$REGION" --query Parameter.Value --output text 2>/dev/null); then
    echo "already set: $a = $CUR (left untouched)"
  else
    aws ssm put-parameter --name "/openclaw/lifecycle/deadline-sec/$a" --type String --value "$v" --region "$REGION"
    echo "created: $a = $v"
  fi
done
```

**只创建缺失的,不要盲目 `--overwrite`** —— 这一步会被重跑,而客户可能已经按自己的口径调过某几档;
无条件覆盖会把他们的取值悄悄改回默认。要改已有值就单独做,并先记下原值。

**每条区域性命令都要带 `--region "$REGION"`** —— 省掉它会落到 CLI 的默认区域,于是参数写到了别的区,
而目标 Lambda 仍在用回落默认值,且没有任何报错。

**4.2 API 路由 `POST /hosts/egress`** —— 用 spec 驱动,精确增删:

```bash
lib/apply-api-routes.sh apply lib/api-routes.spec.json "$API_ID" v1 "$REGION"
lib/apply-api-routes.sh verify lib/api-routes.spec.json "$API_ID" v1 "$REGION"
lib/apply-api-routes.sh finalize lib/api-routes.spec.json "$API_ID" v1 "$REGION"
```

`verify` 过了再 `finalize`(删掉被替换的旧 Deployment 并释放状态位)。**回滚只在 finalize 之前可用。**

**4.3 可观测性资产(见开头第 ③ 条)** —— 10 个 BucketDeployment 自定义资源**故意不创建**;手工等价物
是按 `deploy/stacks/obs_assets.py` 的 `OBS_ASSETS` 清单逐个上传。本次 10 个源文件里**只有
`install-fluent-bit.sh` 变了**,其余 9 个是"确认在位"而非"新建":

```bash
KEY=deployment/observability/fluent-bit/install-fluent-bit.sh
# 覆盖前先记该对象自己的前置版本(区分 404 与其他错误,瞬时错误不能被记成 ABSENT)
if [ -f prev-obs-version.txt ]; then echo "anchor already recorded: $(cat prev-obs-version.txt)"
elif OUT=$(aws s3api head-object --bucket "$ASSETS_BUCKET" --key "$KEY" --region "$REGION" --query VersionId --output text 2>err.txt); then echo "$OUT" > prev-obs-version.txt
elif grep -q "Not Found\|404" err.txt; then echo ABSENT > prev-obs-version.txt
else echo "head-object failed — refusing to guess the anchor" >&2; cat err.txt >&2; exit 1; fi
aws s3 cp host-scripts/edge/fluent-bit/install-fluent-bit.sh "s3://$ASSETS_BUCKET/$KEY" --region "$REGION"
# 读回并【断言】摘要一致,不是打印两行让人肉眼比
WANT=$(sha256sum host-scripts/edge/fluent-bit/install-fluent-bit.sh | cut -c1-64)
GOT=$(aws s3 cp "s3://$ASSETS_BUCKET/$KEY" - --region "$REGION" | sha256sum | cut -c1-64)
[ "$WANT" = "$GOT" ] || { echo "readback digest mismatch" >&2; exit 1; }
# 记下本次写出的版本,回滚前要用它做 CAS
aws s3api head-object --bucket "$ASSETS_BUCKET" --key "$KEY" --region "$REGION" --query VersionId --output text > post-obs-version.txt
aws s3 ls "s3://$ASSETS_BUCKET/deployment/observability/" --recursive --region "$REGION"
```

**S3 侧回滚**(manifest 里那条只管仓库文件,不管桶):

```bash
# 先 CAS:桶里还必须是本次写出的那个版本,否则说明之后有人又发过,不能盖回去
CURV=$(aws s3api head-object --bucket "$ASSETS_BUCKET" --key "$KEY" --region "$REGION" --query VersionId --output text)
[ "$CURV" = "$(cat post-obs-version.txt)" ] || { echo "object moved past this patch — refusing to roll back" >&2; exit 1; }
PV=$(cat prev-obs-version.txt)
if [ "$PV" != ABSENT ]; then aws s3api copy-object --bucket "$ASSETS_BUCKET" --key "$KEY" --copy-source "$ASSETS_BUCKET/$KEY?versionId=$PV" --region "$REGION"
else aws s3 rm "s3://$ASSETS_BUCKET/$KEY" --region "$REGION"; fi
```

不记前置版本就没法回滚 —— 那会让未来起的 host 一直拿到打过补丁的那份。期望 10 个键都在。配套的 10 个 `LayerVersion` 是仅为跑自定义资源存在的 CDK 管道,手工路径不需要,
**故意不创建**;将来若真跑一次 CDK 部署,它会把这些补齐 —— 这是一条已知且已披露的偏离。

## Step 5 — 未来机器的源与启动模板

本 kit 的启动模板变更**止于 `promote`**:它只把 ASG 指向新的启动模板版本,**不做机队换机**。
host 的 ASG 终止钩子只有 120s,一次把整组机器换掉会硬杀在役 microVM,直接影响租户;而且这种
按启动模板变更全量替换的爆炸半径是整个机队。在役机器的修复走前面几个 Step 的热修路径,不靠换机。

`lib/apply-lt.sh verify` 在第一台自然新增的 host 出现之前**必然 FAIL**,这是**预期**,不是缺陷。
等第一台自然新增的 host 起来之后再跑它,它会写出 verified 回执。连带后果是:下一个 kit 在
`OC_REQUIRE_VERIFIED_PULL=1` 下跑 `pull` 会 fail-closed,因为当前还没有 verified 回执。解开办法是
等第一台自然新增的 host 出现后补跑 `lib/apply-lt.sh verify`;在那之前若必须继续,由操作者显式承担
风险,不设默认绕过。**本 kit 不给任何换机配方。**

先把 `host-scripts/` 推到 `deployment/scripts/`(临时键 → 校验 → 提升;留旧 version id 备回滚)。
`init-host.sh` 是**烤进启动模板**的,单独处理:

`apply-lt.sh` 的子命令是 `pull|push|promote|refresh|rollback <asg> <region>`,以及
`verify <asg> <region> [instance-id]` —— **两个参数都必填**。`lt-userdata.py` 的动词只有
`decode|repack|inspect|rekey`(**没有 graft**)。

```bash
lib/apply-lt.sh pull "$ASG" "$REGION"
# —— 人工闸:pull 把【已渲染】那份写到下面这个文件,就在这份上改本次变更的几段 ——
#    $HOME/.oc-apply-lt/$ASG.init-host.sh          <-- push 读的就是它
! grep -q '{{' "$HOME/.oc-apply-lt/$ASG.init-host.sh"   # 必须为真(用 ! grep -q,不要用 grep -c:计数 0 时 grep 退出 1)
sha256sum "$HOME/.oc-apply-lt/$ASG.init-host.sh" | cut -c1-64 > lt-edit-done.txt   # 回执存【编辑后的摘要】
# 确认无误后再继续:
lib/apply-lt.sh push "$ASG" "$REGION"
lib/apply-lt.sh promote "$ASG" "$REGION"
lib/apply-lt.sh verify "$ASG" "$REGION"
```

**`pull` 与 `push` 之间必须停下来人工改**,而且这个闸落在 `lt-edit-done.txt` 里存的**编辑后摘要**上:
重跑 `apply_cli` 时摘要相符就跳过 `pull`(可恢复续跑),不符或缺失就只做 `pull` 并以退出码 10 停下 ——
**这样重跑绝不会用 `pull` 覆盖掉已经做好的人工编辑**。
**不要把闸写成 shell 注释** —— `#` 之后的内容会被整条吃掉,`push`/`promote` 两条都不会跑。
`push` 只读 `push` 只读
`$HOME/.oc-apply-lt/$ASG.init-host.sh`,不读任何别的临时文件。对照
`launch-template/init-host.sh.patched` 与已渲染那份的差异,**只改本次变更的那几段**,
不要整文件替换(整替会把 CDK 已替换好的约 31 个值换回占位符)。
`promote` 是本步的**终态** —— 它只把 ASG 指向新的启动模板版本,**不动在役机器**;在役机器的修复
由前面几个 Step 的热修路径负责,本 kit **不换机**。`verify` 是读回确认。
**`pull` 会覆盖唯一的回滚锚点** —— 在 `promote` 成功且 `verify` 通过之前不要重复 `pull`。

上面那条 `grep -c` **必须为 0**。`init-host.sh` 是 `ha_edge.py` 在 synth 时读入、替换约 31 个占位符后
烤进 UserData 的,所以**必须在【已渲染】的那份上改,不能拿仓库里的模板直接烤** —— 直接烤会让新
host 带着字面 `{{...}}` 起不来。新的启动模板版本**不会**自动更新在役 ASG(它钉的是具体版本),
而且本 kit **不去滚在役机队** —— 新版本只对之后**自然新增**的 host 生效(扩容、健康检查替换、
AZ 重平衡)。那三个信号留给**第一台自然新增的 host**去读:解码后的 UserData 没有 `{{`、
它注册进 hosts 表、ASG 生命周期是 CONTINUE 而不是 Heartbeat-Timeout。

## Step 6 — 逐个 fix 的可证伪验证

`manifest.json` 的 `verifications[]` 有 20 条,每条都写了 `action` / `observable` / `pass_when` /
`fail_when` / `timeout_s` / `cleanup`,按 `phase` 分三批:

- **Phase A-readonly(只读,零副作用,始终先跑,9 条)**:`verify-config-preflight`、
  `verify-consistency-cli`、`verify-host-manifest-fanout`、`verify-hosts-reporting`、
  `verify-image-provenance`、`verify-observability-boot`、`verify-pagination-query`、
  `verify-prior-kit-artifacts`、`verify-release-tooling`。
- **Phase B-optional(只在客户决定启用该开关时才跑,2 条)**:`verify-backup-lifecycle`、
  `verify-egress-fleet`。
- **Phase B-lifecycle(走真实产品入口,9 条)**:`verify-config-reapply`、`verify-copyfile-toctou`、
  `verify-edge-availability`、`verify-host-taint`、`verify-lifecycle-converge`、
  `verify-lifecycle-deadline`、`verify-lifecycle-lease-port`、`verify-rolling-upgrade`、
  `verify-tenant-isolation`。

每条 `fix` 的分组是按 bb 的提交标题逐条核实的,不是按主题猜的 —— 所以 `fixes[].summary` 描述的就是该组
路径真正改了什么。有三条客户更新说明里提到、但**本 patch 区间内没有对应提交**的条目(创建接口参数校验
过严、编号冲突消耗 503、有空闲容量却被误判已满):它们不由本 patch 交付,请按现网实际表现单独确认。
跑 `verify-egress-fleet` 时有几个调用要点,单独说明:`POST /hosts/egress` 是**改机队**的写操作,
按设计是 **admin-only**(动全机队网络隔离,爆炸半径最大),`operator` 不够。**同时**带 `x-api-key` 与
**admin 身份的 Bearer JWT** —— 这条方法在模板里是 `ApiKeyRequired=true`,API Gateway 会在 RBAC 之前
就因缺 key 拒掉;而 admin 身份来自 Bearer。两者各管一段,缺一不可。body 用
`{"mode":"off","wait":true}`:`wait=true` 会返回**逐机 `apply_exit` / `rules_sha256` / `consistent`**,
那才是可证伪的观测量,比只拿 `command_id` 强。先用 `GET /hosts` 记下原 `egress_mode` 以便恢复。
**不要用 api-key 调这条** —— 它会在 RBAC 前置门被挡成 403,原因见开头第 ②b 条,那是上游缺口不是本
patch 没打对。

同理,`verify-rolling-upgrade` 的 `action: "rebuild"` 也需要管理员权限,`"restart"` 没有这个限制;
`client_token` 是**必填**的幂等键(4 至 128 个可打印非空格 ASCII 字符),作业中断后用同一个
`client_token` 续跑不会重复处理已完成的机器。请求体不接受未列出的字段,多传会返回 400 并列出字段名。

"创建了一个租户,它 running 了"**不算**验证 —— 那只证明代码加载了。三条必须落到不变量上:
没有租户卡在 `creating`;不存在 `tenant=running` 而 `assignment=failed` 的跨表指纹;
`used_vcpu <= cap` 没有超卖。写查询前先在同架构环境上确认表名与字段真的存在。

## Step 7 — 精确清场(一对一,零通配)

真实 host 上有成百上千个真实租户。**绝不使用前缀通配的递归删除。**
测试租户用一个唯一的零填充前缀;删除时**只**循环你在创建时记下的那批精确 id:

```bash
for id in $TEST_IDS; do
  curl -X DELETE "$CTRL_API_BASE/tenants/$id?keep_data=false" -H "x-api-key: $KEY"
done
```

`keep_data` 默认是 `true`(软删除,磁盘还在),所以显式传 `false`。逐个轮询到 `deleted`,再用 SSM
确认 `/data/firecracker-vms/<完整id>` 已消失且没有孤儿 firecracker 进程;确有残留才对**完整 id**
做精确删除。最后确认真实租户计数前后完全一致。

## 回滚

每个操作的 `rollback_cli` 与 `rollback_policy` 在 `manifest.json` 里逐条写明。三条要记住:

- `openclaw-api` 的回滚要**同时**覆盖别名与 `$LATEST` —— dispatch 的 SQS 事件源绑的是 `$LATEST`,
  只翻别名不会让 dispatch 退回去。
- IAM 只读授权是 `RETAIN`,**不回滚**。
- API 路由的回滚只在 `finalize` 之前可用。
