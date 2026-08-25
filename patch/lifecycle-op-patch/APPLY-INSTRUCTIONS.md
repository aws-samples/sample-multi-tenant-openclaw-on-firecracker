# lifecycle-op-patch — apply by reading, no CloudFormation redeploy

`status: MANUAL_REVIEW`。本 kit 有 17 个 `MANUAL_CLI_REVIEW` 操作,必须逐个人工复核后才动手。
**任何步骤都不要运行 CDK 的部署子命令,也不要运行任何触发 CloudFormation 栈更新的命令** —— 这套环境是一次 CDK
部署之后又手工改过很多次的,栈更新会覆盖那些手工改动。

- `base_sha` = `81f3b884ca1226afdb107e7f8392d9334fd33493`
- `patch_sha` = `c9fd494ff4a76929f205f52464047a9185c7c49a`

两端都在公开仓可解析,所以下面每条校验命令你都能自己跑通(spire-agent 那版 kit 记的两个 SHA 只存在于
构建机本地,客户跑不了 —— 从这一版起修掉了)。

## 先读四条会静默毁掉本次交付的事实

**① 死线的运行时载体是 SSM 参数,改 Lambda 环境变量【完全不生效】。**
流量走 `live` 别名 → 已发布 version,而已发布 version 的环境变量是冻结的。所以八档死线必须写到
`/openclaw/lifecycle/deadline-sec/<action>` 这八个 SSM 参数上(进程内缓存 60 秒);
`create-deadline-config.py --live` 比对的是 `$LATEST` 的环境变量,**它的绿不能证明死线生效**。
八个动作是 `create` / `suspend` / `restore` / `restart` / `start` / `rebuild`(各 180 秒)与
`backup` / `delete`(各 600 秒)。**`start` 容易被漏** —— 它与 `restart` 同档(同一条通道、同为不含
数据步骤的动作),只列七个的清单是不完整的。

**②b `POST /hosts/egress` 目前会被上游的 RBAC 前置门挡成 403。**
`core/auth.py` 的 `_RBAC_SKIP` 里没有这条路由,而 api-key 路径的 role 会被解析成 `viewer`,在门口
就被 `viewer < operator` 挡掉 —— 该文件同一处注释记着 `/hosts/{instance_id}/taint` 踩过一模一样的坑
且真机复现过。`core/auth.py` 在本次区间**没有变更**,所以修不进本 kit,属上游缺口。验证时若拿到 403,
先按这条排查,不要当成 patch 没打对。

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
lib/apply-lt.sh refresh "$ASG" "$REGION"
lib/apply-lt.sh verify "$ASG" "$REGION"
```

**`pull` 与 `push` 之间必须停下来人工改**,而且这个闸落在 `lt-edit-done.txt` 里存的**编辑后摘要**上:
重跑 `apply_cli` 时摘要相符就跳过 `pull`(可恢复续跑),不符或缺失就只做 `pull` 并以退出码 10 停下 ——
**这样重跑绝不会用 `pull` 覆盖掉已经做好的人工编辑**。
**不要把闸写成 shell 注释** —— `#` 之后的内容会被整条吃掉,`push`/`promote`/`refresh` 一条都不会跑。
`push` 只读 `push` 只读
`$HOME/.oc-apply-lt/$ASG.init-host.sh`,不读任何别的临时文件。对照
`launch-template/init-host.sh.patched` 与已渲染那份的差异,**只改本次变更的那几段**,
不要整文件替换(整替会把 CDK 已替换好的约 31 个值换回占位符)。
`promote` 之后还要 **`refresh`** 才会滚在役机队,`verify` 是最后的读回确认。
**`pull` 会覆盖唯一的回滚锚点** —— 在 `refresh` 成功且 `verify` 通过之前不要重复 `pull`。

上面那条 `grep -c` **必须为 0**。`init-host.sh` 是 `ha_edge.py` 在 synth 时读入、替换约 31 个占位符后
烤进 UserData 的,所以**必须在【已渲染】的那份上改,不能拿仓库里的模板直接烤** —— 直接烤会让新
host 带着字面 `{{...}}` 起不来。新的启动模板版本**不会**自动更新在役 ASG(它钉的是具体版本),要按 `apply-lt.sh` 的受控
instance-refresh 路径滚。验证只起**一台**新 host,盯三个信号:解码后的 UserData 没有 `{{`、
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
