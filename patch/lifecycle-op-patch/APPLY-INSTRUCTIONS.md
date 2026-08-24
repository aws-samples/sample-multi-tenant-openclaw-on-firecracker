# lifecycle-op-patch — apply by reading, no CloudFormation redeploy

`status: MANUAL_REVIEW`。本 kit 有 8 个 `MANUAL_CLI_REVIEW` 操作,必须逐个人工复核后才动手。
**任何步骤都不要运行 CDK 的部署子命令,也不要运行任何触发 CloudFormation 栈更新的命令** —— 这套环境是一次 CDK
部署之后又手工改过很多次的,栈更新会覆盖那些手工改动。

- `base_sha` = `81f3b884ca1226afdb107e7f8392d9334fd33493`
- `patch_sha` = `25afc511ccfd319d4e7ae37e973c55f12081ed79`

两端都在公开仓可解析,所以下面每条校验命令你都能自己跑通(上一版 kit 记的两个 SHA 只存在于构建机
本地,客户跑不了 —— 这一版修掉了)。

## 先读三条会静默毁掉本次交付的事实

**① 死线的运行时载体是 SSM 参数,改 Lambda 环境变量【完全不生效】。**
流量走 `live` 别名 → 已发布 version,而已发布 version 的环境变量是冻结的。所以七档死线必须写到
`/openclaw/lifecycle/deadline-sec/<action>` 这七个 SSM 参数上(进程内缓存 60 秒);
`create-deadline-config.py --live` 比对的是 `$LATEST` 的环境变量,**它的绿不能证明死线生效**。

**② 七档里只有 `create` 有权威的最坏执行值(128 秒)。**
`suspend/restore/restart/rebuild/backup/delete` 目前**没有下界守护**。往小调可能小于该操作单次最坏
执行,于是判死之后 SSM 还在跑,留下没人认领的 microVM(占容量且计费)。不要为了"更快收敛"下调。

**③ 本次把可观测性资产的分发从部署脚本搬到了 CDK,而 kit 不允许跑 CDK 部署。**
部署脚本里 12 处 `_obs_upload` 在 `patch_sha` 上归零,替代它的 10 个 BucketDeployment 自定义资源
**建不出来**。桶里现有对象还在(旧部署脚本传过),所以 host 照常起 —— 这是**潜伏**缺陷:
**以后这批资产再变,就没有任何自动分发路径了**。第 4.3 步给了手工等价物,请记进运维手册。

## Step 0 — DISCOVER(只读)与制品真伪

```bash
bash lib/discover-env.sh > environment.json
```

`environment.json` 落地后,后面每一步都从它取坐标,不要手打。然后逐个证明制品 == 锚定树:

```bash
python3 - <<'PY'
import json, hashlib, subprocess, sys
m = json.load(open("manifest.json"))
bad = 0
for path, pv in m["paths"].items():
    art = pv.get("artifact")
    if not art:
        continue
    want = pv["patch_sha256"]
    got = hashlib.sha256(open(art, "rb").read()).hexdigest()
    src = subprocess.run(["git", "show", f"{m['patch_sha']}:{path}"], capture_output=True)
    ref = hashlib.sha256(src.stdout).hexdigest() if src.returncode == 0 else "unreadable"
    if not (want == got == ref):
        print("MISMATCH", path, want[:12], got[:12], ref[:12])
        bad += 1
print("artifacts checked:", sum(1 for v in m["paths"].values() if v.get("artifact")), "mismatched:", bad)
sys.exit(1 if bad else 0)
PY
```

摘要是 SHA-256。任何一条不等就停下,不要继续 —— 那说明 kit 被重新打包过或下载不完整。

## Step 1 — 备份(每个操作的 backup 必须先成功)

```bash
aws lambda get-function --function-name openclaw-api --query Configuration.RevisionId --output text
aws lambda publish-version --function-name openclaw-api --description "pre lifecycle-op-patch"
aws lambda get-function --function-name openclaw-api --query Code.Location --output text | xargs curl -s -o live-api.zip
aws ec2 describe-launch-template-versions --launch-template-id "$LT_ID" --query 'LaunchTemplateVersions[?DefaultVersion==`true`].VersionNumber' --output text
```

记下 `publish-version` 返回的版本号与 LT 的当前默认版本号 —— 回滚只认这两个数。
机队会有版本漂移,**按 host 分别备份**,让每台各自回到自己的版本。

## Step 2 — 先补授权(fail-closed 前置,不能放到后面)

`api` 与 `lifecycle-consumer` 两个角色都要能读死线参数前缀;现有的 dispatch 前缀授权**不覆盖**它。
漏了这一步会把一个软问题变成"读不到参数一路回落默认值,而日志上看不出来"。

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

按 `manifest.json` 里 `layer: B-s3` 的 14 个路径,把 `host-scripts/<rel>.patched` 推到桶再拉到 host。
每个路径的 `operations[0].apply_cli` 就是该文件的确切命令,`verify_cli` 是它的校验命令。
主机通常在私有子网:命令写成 `ssh/scp` 便于阅读,实际走 SSM(`send-command`;传文件用 base64)。

Lambda 走 **overlay**(复用在役包里的依赖,不要预打包 zip —— 那会把构建机的依赖版本焊到客户函数上):
下载在役包 → 只删第一方源码目录 → 覆盖 `lambda/` 下的源码树 → 重新打包 → `update-function-code`
→ `aws lambda wait function-updated` → `invoke` 验 `FunctionError` 为空 → 再翻 `live` 别名。

`invoke` 的判据是 `FunctionError` 为空,**不是 200 响应体**:私有 API 上合成的 `/ping` 返回 404 是
预期的(按路径路由),不是失败。

## Step 4 — CDK 变更改走手工 CLI(逐个复核,绝不 stack update)

`resources/cloudformation/` 里是 `base_sha` 与 `patch_sha` 两侧的完整合成模板,60 项资源变更在
`manifest.json` 的 `operations[].resource_refs` 里被逐个拥有一次。通用入口:

```bash
lib/apply-cfn-resources.sh plan resources/cloudformation "$REGION"
```

它逐个资源打印 before/after 与所需的人工决策,然后**停下**等你判断 —— 它不会替 `AWS::IAM::Policy`
和 `AWS::CodeBuild::Project` 编一条通用命令。三类必须单独说明。

**4.1 七个死线 SSM 参数** —— 平时由 CDK 创建,这里自己建;`put-parameter` 幂等,先 `get-parameter`
区分"新建"还是"接管已有"(决定回滚是删除还是保留):

```bash
for a in create suspend restore restart rebuild backup delete; do
  case "$a" in backup|delete) v=600 ;; *) v=180 ;; esac
  aws ssm get-parameter --name "/openclaw/lifecycle/deadline-sec/$a" >/dev/null 2>&1 && echo "adopt existing: $a" || echo "will create: $a"
  aws ssm put-parameter --name "/openclaw/lifecycle/deadline-sec/$a" --type String --value "$v" --overwrite
done
```

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
aws s3 cp host-scripts/edge/fluent-bit/install-fluent-bit.sh "s3://$ASSETS_BUCKET/deployment/observability/fluent-bit/install-fluent-bit.sh"
aws s3 ls "s3://$ASSETS_BUCKET/deployment/observability/" --recursive
```

期望 10 个键都在。配套的 10 个 `LayerVersion` 是仅为跑自定义资源存在的 CDK 管道,手工路径不需要,
**故意不创建**;将来若真跑一次 CDK 部署,它会把这些补齐 —— 这是一条已知且已披露的偏离。

## Step 5 — 未来机器的源与启动模板

先把 `host-scripts/` 推到 `deployment/scripts/`(临时键 → 校验 → 提升;留旧 version id 备回滚)。
`init-host.sh` 是**烤进启动模板**的,单独处理:

```bash
bash lib/apply-lt.sh pull
python3 lib/lt-userdata.py graft --rendered ./lt-current.userdata --artifact launch-template/init-host.sh.patched --out ./lt-next.userdata
python3 lib/lt-userdata.py decode --version next | grep -c '{{'
bash lib/apply-lt.sh push
bash lib/apply-lt.sh promote
```

上面那条 `grep -c` **必须为 0**。**必须在【已渲染】的 UserData 上嫁接,不能拿仓库里的模板直接烤** ——
那份文件带约 31 个 `{{PLACEHOLDER}}`,CDK 在 synth 时才替换;直接烤会让新 host 带着字面 `{{...}}`
起不来。新的启动模板版本**不会**自动更新在役 ASG(它钉的是具体版本),要按 `apply-lt.sh` 的受控
instance-refresh 路径滚。验证只起**一台**新 host,盯三个信号:解码后的 UserData 没有 `{{`、
它注册进 hosts 表、ASG 生命周期是 CONTINUE 而不是 Heartbeat-Timeout。

## Step 6 — 逐个 fix 的可证伪验证

`manifest.json` 的 `verifications[]` 有 11 条,每条都写了 `action` / `observable` / `pass_when` /
`fail_when` / `timeout_s` / `cleanup`,按 `phase` 分两批:

- **Phase A(只读,零副作用,始终先跑)**:`verify-egress-allowlist`(对新路由发一次真实带 api-key
  的请求,**200 而不是 404** 才算路由建成)、`verify-ddb-scan-pagination`、`verify-config-profile-gate`、
  `verify-consistency-cli`、`verify-copyfile-toctou`、`verify-backup-lifecycle`。
- **Phase B(走真实产品入口的完整生命周期,核心,跑一次)**:`verify-lifecycle-deadline`、
  `verify-lifecycle-converge`、`verify-lifecycle-lease-port`、`verify-lifecycle-host-fanout`、
  `verify-observability-boot`。

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
